# -*- coding: utf-8 -*-
"""
雪球雷达 · 上传模块（零第三方依赖）

负责把 xueqiu.exe 每轮产出的线索 + 原始评论，通过 HTTPS POST 上传到
Cloudflare Worker 的 /api/ingest 接口。

只用标准库 urllib，避免引入 requests 导致 PyInstaller 打包膨胀与依赖问题。
上传失败一律静默处理（返回错误码/信息），绝不中断本地抓取主流程。

用法（库）：
    from uploader import upload_round, make_round_id, build_payload

    payload = build_payload(meta, candidates, comments, source="hashtag")
    code, body = upload_round(payload, url, token)
"""

import json
import time
import urllib.request
import urllib.error


def make_round_id(meta, source="unknown"):
    """根据 meta 生成稳定且唯一的轮次 id（用于 D1 幂等覆盖）。"""
    g = (meta.get("generated_at") or "").replace(" ", "_").replace(":", "-")
    slug = (meta.get("hashtag") or meta.get("title") or "")[:20]
    return f"{g}__{source}__{abs(hash(slug)) % 100000}"


def build_payload(meta, candidates, comments, source="unknown"):
    """组装上传 body：{ round_id, meta(含 source), candidates, comments }。

    注意：服务端 /api/ingest 已不再落库原始评论（raw_comments 已删除），
    仅用 meta.total_comments 做统计。因此上传 body 直接丢弃 comments 数组，
    仅当 meta 未带 total_comments 时，用 comments 长度兜底补上计数。
    这样可显著缩小请求体，避免触发 Cloudflare 边缘对超大请求体的 RST（10054）。
    """
    m = dict(meta or {})
    if m.get("total_comments") is None and comments:
        m["total_comments"] = len(comments)
    m["source"] = source
    return {
        "round_id": make_round_id(m, source),
        "meta": m,
        "candidates": candidates or [],
        "comments": [],   # 服务端不存储原始评论，仅统计用 meta.total_comments
    }


# 浏览器风格 UA（避免 urllib 默认 "Python-urllib/x" 被 Cloudflare 当 bot 拦截，见 1010）
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 上传重试 / 分片参数
_DEFAULT_RETRIES = 4          # 网络层错误（含 10054）最多重试次数
_DEFAULT_BACKOFF = 1.5        # 退避基数（秒），实际等待 = backoff * 2**attempt
_DEFAULT_CHUNK = 200          # 每个 HTTP 请求的候选条数（拆分以降低单请求 D1 负载 / CPU）


def _do_post(url, token, payload, timeout, user_agent):
    """单次 POST，成功返回 (http_code, body_text)；HTTP 层错误抛 HTTPError；网络层错误抛异常。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", user_agent or _BROWSER_UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read().decode("utf-8", "replace")


def upload_round(payload, url, token, timeout=60, user_agent=None,
                 retries=None, backoff=None, chunk_size=None):
    """POST 一个轮次的线索数据到 Worker。

    参数:
        payload : build_payload() 的返回值（dict）
        url     : Worker ingest 地址，如 https://xueqiu.你的域名.com/api/ingest
        token   : 与 Worker 端 INGEST_TOKEN 一致的 Bearer token
        timeout : 单次请求超时（秒）
        user_agent : 自定义 UA
        retries    : 网络层错误（10054 / 超时等）重试次数，默认 _DEFAULT_RETRIES
        backoff    : 退避基数（秒），默认 _DEFAULT_BACKOFF
        chunk_size : 每个请求最多携带的候选条数，默认 _DEFAULT_CHUNK（拆分上传）
    返回: (http_code, body_text)
        http_code 为 -1 表示重试耗尽仍为网络/异常错误（非 HTTP 层）

    说明:
        - 候选按 chunk_size 拆成多片，逐片以「同一 round_id」POST；
          服务端为 INSERT OR REPLACE，幂等累积，分片失败重试不会重复/丢失。
        - 网络层错误（WinError 10054 远程主机强迫关闭连接、超时等）按指数退避重试；
          HTTP 401/4xx 直接返回（不可重试），5xx 视为可重试。
    """
    if not url or not token:
        return -1, "missing url or token"
    retries = _DEFAULT_RETRIES if retries is None else retries
    backoff = _DEFAULT_BACKOFF if backoff is None else backoff
    chunk_size = _DEFAULT_CHUNK if chunk_size is None else max(1, int(chunk_size))

    candidates = list(payload.get("candidates") or [])
    chunks = [candidates[i:i + chunk_size] for i in range(0, len(candidates), chunk_size)] or [[]]

    last_code, last_body = -1, ""
    for idx, ch in enumerate(chunks):
        chunk_payload = dict(payload)
        chunk_payload["candidates"] = ch
        ok = False
        for attempt in range(retries + 1):
            try:
                code, body = _do_post(url, token, chunk_payload, timeout, user_agent)
                last_code, last_body = code, body
                if 200 <= code < 300:
                    ok = True
                    break
                if code == 401:
                    return code, body          # 鉴权失败，无需重试
                if 400 <= code < 500:
                    return code, body          # 4xx（非401）不可重试
                # 5xx：继续重试
            except urllib.error.HTTPError as e:
                try:
                    b = e.read().decode("utf-8", "replace")
                except Exception:
                    b = ""
                last_code, last_body = e.code, b
                if e.code == 401:
                    return e.code, b
                if 400 <= e.code < 500:
                    return e.code, b
                # 5xx：继续重试
            except Exception as e:
                # 网络层错误（WinError 10054 / 超时 / 连接重置等）：可重试
                last_code, last_body = -1, str(e)
                if attempt < retries:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                return -1, str(e)
        if not ok:
            # 该分片重试耗尽仍失败 —— 非致命，本地已落盘，直接返回
            return last_code, last_body
        if idx < len(chunks) - 1:
            time.sleep(0.3)  # 分片间隔，降低频率，规避限流
    return last_code, last_body
