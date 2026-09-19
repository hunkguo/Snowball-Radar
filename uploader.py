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
import urllib.request
import urllib.error


def make_round_id(meta, source="unknown"):
    """根据 meta 生成稳定且唯一的轮次 id（用于 D1 幂等覆盖）。"""
    g = (meta.get("generated_at") or "").replace(" ", "_").replace(":", "-")
    slug = (meta.get("hashtag") or meta.get("title") or "")[:20]
    return f"{g}__{source}__{abs(hash(slug)) % 100000}"


def build_payload(meta, candidates, comments, source="unknown"):
    """组装上传 body：{ round_id, meta(含 source), candidates, comments }。"""
    m = dict(meta or {})
    m["source"] = source
    return {
        "round_id": make_round_id(m, source),
        "meta": m,
        "candidates": candidates or [],
        "comments": comments or [],
    }


# 浏览器风格 UA（避免 urllib 默认 "Python-urllib/x" 被 Cloudflare 当 bot 拦截，见 1010）
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def upload_round(payload, url, token, timeout=60, user_agent=None):
    """POST 一个轮次的线索数据到 Worker。

    参数:
        payload : build_payload() 的返回值（dict）
        url     : Worker ingest 地址，如 https://xueqiu.你的域名.com/api/ingest
        token   : 与 Worker 端 INGEST_TOKEN 一致的 Bearer token
        timeout : 超时（秒）
        user_agent : 自定义 UA（默认浏览器风格，规避 Cloudflare Bot 检测 1010）
    返回: (http_code, body_text)
        http_code 为 -1 表示网络/异常错误（非 HTTP 层）
    """
    if not url or not token:
        return -1, "missing url or token"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", user_agent or _BROWSER_UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            b = e.read().decode("utf-8", "replace")
        except Exception:
            b = ""
        return e.code, b
    except Exception as e:  # 网络错误、超时等
        return -1, str(e)
