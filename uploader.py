# -*- coding: utf-8 -*-
"""
雪球雷达 · 上传模块（零第三方依赖）

负责把 xueqiu.exe 每轮产出的线索，通过 HTTPS POST 上传到
Cloudflare Worker 的 /api/ingest 接口。

只用标准库 urllib，避免引入 requests 导致 PyInstaller 打包膨胀与依赖问题。
上传失败一律静默处理（返回错误码/信息），绝不中断本地抓取主流程。

抗重置要点（针对 Windows 上 urllib 上传 Cloudflare 偶发 WinError 10054）：
  - 强制走 IPv4（AF_INET）。不少 Windows 机器 getaddrinfo 先返回 IPv6，
    而本地 IPv6 出口到 Cloudflare 被重置；Chrome 用 Happy-Eyeballs 回退到
    正常 IPv4，urllib 不回退 → 这里手动强制 IPv4，对齐 Chrome 的成功路径。
  - 网络层错误（10054 / 超时 / 连接重置）指数退避重试。
  - 失败时返回带诊断信息（解析到的 IP / 是否走代理 / 异常类型 / winerror），
    便于远端定位是网络还是服务端问题。

用法（库）：
    from uploader import upload_round, make_round_id, build_payload

    payload = build_payload(meta, candidates, comments, source="hashtag")
    code, body = upload_round(payload, url, token)
"""

import json
import os
import re
import time
import socket
import http.client
import urllib.request
import urllib.error
import urllib.parse


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
    m["sync_version"] = compute_sync_version(m)
    return {
        "round_id": make_round_id(m, source),
        "version": m["sync_version"],
        "meta": m,
        "candidates": candidates or [],
        "comments": [],   # 服务端不存储原始评论，仅统计用 meta.total_comments
    }


def compute_sync_version(meta):
    """根据 meta.generated_at 计算单调递增版本号（毫秒 epoch）；缺字段则用当前时间。

    用途：同一轮重跑 generated_at 相同 → 版本相同 → 本地 sync_state 可判定为重复，跳过上传。
    """
    g = (meta or {}).get("generated_at") or ""
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})", g)
    if m:
        try:
            import datetime as _dt
            dt = _dt.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
            )
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
    return int(time.time() * 1000)


# ───────────────────────────────────────────────────────────
# 本地同步状态（版本号 + 已上传轮次），用于增量比对、避免重复上传
# ───────────────────────────────────────────────────────────
def load_sync_state(path):
    """读取 data/sync_state.json：{ uploaded_rounds: {round_id: version}, last_version, total_uploaded }。
    文件缺失/损坏时返回空状态，不影响上传主流程。"""
    if not path:
        return {"uploaded_rounds": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            return {"uploaded_rounds": {}}
        s.setdefault("uploaded_rounds", {})
        return s
    except Exception:
        return {"uploaded_rounds": {}}


def save_sync_state(path, state):
    """原子写入同步状态（先写临时文件再 rename，避免半截文件）。"""
    if not path:
        return
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ───────────────────────────────────────────────────────────
# 线上全量比对：拉取某轮已有 clue_id，剔除重复（复用现有 /api/clues 端点）
# ───────────────────────────────────────────────────────────
def _do_get(url, token, timeout, user_agent):
    """单次 GET（强制 IPv4）。成功返回 (code, text)；网络层错误抛异常。"""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", user_agent or _BROWSER_UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    opener = urllib.request.build_opener(_IPv4HTTPSHandler())
    with opener.open(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read().decode("utf-8", "replace")


def fetch_existing_clue_ids(url, token, round_id, timeout=30, user_agent=None):
    """拉取 round_id 在 Worker 端已有的全部 clue_id 集合（分页遍历 /api/clues?round=）。

    返回 set(str)；任何失败（网络/鉴权/非 200）返回 None，调用方据此降级为「全量上传」。
    """
    if not url or not token or not round_id:
        return None
    base = url.split("/api/")[0]
    existing = set()
    offset = 0
    for _ in range(50):  # 安全上限，避免异常分页死循环
        q = "/api/clues?round=" + urllib.parse.quote(str(round_id)) + f"&limit=200&offset={offset}"
        try:
            code, text = _do_get(base + q, token, timeout, user_agent)
        except Exception:
            return None
        if code != 200:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        clues = data.get("clues") or []
        for c in clues:
            cid = c.get("clue_id")
            if cid is not None and cid != "":
                existing.add(str(cid))
        if not data.get("has_more") or len(clues) == 0:
            break
        offset += len(clues)
    return existing


def upload_round_dedup(payload, url, token, sync_state_path=None, timeout=60, user_agent=None,
                       retries=None, backoff=None, chunk_size=None, enable_online_check=True):
    """带「版本号增量 + 线上全量比对」的去重上传。

    流程：
      1) 本地版本去重：round_id 已记录于 sync_state → 整轮回传跳过（增量核心，零网络开销）。
      2) 线上全量比对：GET /api/clues?round=R 拉该轮已有 clue_id，剔除本地已存在者，
         只上传差异；接口不可达时降级为全量上传（不破坏现有行为）。
      3) 无差异则跳过，并标记该轮已上传（避免下一轮重复比对）。
      4) 上传差异（复用 upload_round 的分片 + 指数退避重试）。
      5) 成功后才把 round_id 写入 sync_state，保证「记录成功 = 确实上传成功」。

    返回: (http_code, body_text, uploaded_count)
        http_code 为 0 表示被去重跳过（非错误），uploaded_count 为实际新增上传条数。
    """
    if not url or not token:
        return -1, "missing url or token", 0

    round_id = payload.get("round_id")
    candidates = list(payload.get("candidates") or [])
    version = compute_sync_version(payload.get("meta", {}))

    # 1) 本地版本去重：同 round_id 已上传过 → 跳过
    state = load_sync_state(sync_state_path) if sync_state_path else {"uploaded_rounds": {}}
    state.setdefault("uploaded_rounds", {})
    if round_id and round_id in state["uploaded_rounds"]:
        return 0, "skipped(round already uploaded): " + str(round_id), 0

    # 2) 线上全量比对：剔除该轮已有 clue_id
    if enable_online_check and round_id and candidates:
        try:
            existing = fetch_existing_clue_ids(url, token, round_id, timeout=timeout, user_agent=user_agent)
        except Exception:
            existing = None
        if existing is not None:
            before = len(candidates)
            candidates = [c for c in candidates if str(c.get("id", "")) not in existing]
            if len(candidates) != before:
                p2 = dict(payload)
                p2["candidates"] = candidates
                payload = p2

    # 3) 无差异可上传
    if not candidates:
        if sync_state_path and round_id:
            state["uploaded_rounds"][round_id] = version
            state["last_version"] = max(state.get("last_version", 0), version)
            save_sync_state(sync_state_path, state)
        return 0, "skipped(no new candidates vs server)", 0

    # 4) 上传差异（分片 + 重试）
    code, body = upload_round(payload, url, token, timeout, user_agent, retries, backoff, chunk_size)

    # 5) 成功后记录版本，确保「记录 = 确实成功」
    if 200 <= code < 300 and sync_state_path and round_id:
        state["uploaded_rounds"][round_id] = version
        state["last_version"] = max(state.get("last_version", 0), version)
        state["total_uploaded"] = state.get("total_uploaded", 0) + 1
        save_sync_state(sync_state_path, state)

    return code, body, len(candidates)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 上传重试 / 分片参数
_DEFAULT_RETRIES = 4          # 网络层错误（含 10054）最多重试次数
_DEFAULT_BACKOFF = 1.5        # 退避基数（秒），实际等待 = backoff * 2**attempt
_DEFAULT_CHUNK = 200          # 每个 HTTP 请求的候选条数（拆分以降低单请求 D1 负载 / CPU）


# ───────────────────────────────────────────────────────────
# 强制 IPv4 的 HTTPS 传输（解决 Windows 上 IPv6 出口到 Cloudflare 被 RST）
# ───────────────────────────────────────────────────────────
class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def _new_conn(self):
        # 仅用 IPv4 建立 TCP 连接，规避本地 IPv6 出口被 Cloudflare 重置的问题
        return socket.create_connection(
            (self.host, self.port),
            self.timeout,
            self.source_address,
            family=socket.AF_INET,
        )


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4HTTPSConnection, req)


def _resolve_ipv4(host):
    """解析 host 的第一个 A 记录 IPv4，供诊断；失败返回空串。"""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        if infos:
            return infos[0][4][0]
    except Exception:
        pass
    return ""


def _using_proxy():
    """粗略判断是否走代理（环境代理会影响 urllib 直连 Cloudflare）。"""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if __import__("os").environ.get(k):
            return True
    return False


def _diagnose_net(url, err):
    """拼装传输层诊断串，便于远端定位 10054 是网络还是服务端问题。"""
    from urllib.parse import urlparse
    host = ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        pass
    ip = _resolve_ipv4(host) if host else ""
    winerr = ""
    reason = getattr(err, "reason", None)
    if reason is not None:
        we = getattr(reason, "winerror", None)
        en = getattr(reason, "errno", None)
        if we is not None:
            winerr = f" winerror={we}"
        elif en is not None:
            winerr = f" errno={en}"
    proxy = " proxy=yes" if _using_proxy() else " proxy=no"
    return f"[host={host} ipv4={ip}{proxy}{winerr} exc={type(err).__name__}]"


def _do_post(url, token, payload, timeout, user_agent):
    """单次 POST（强制 IPv4）。成功返回 (http_code, body_text)；
    HTTP 层错误抛 HTTPError；网络层错误抛 URLError/异常。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", user_agent or _BROWSER_UA)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    # 用强制 IPv4 的 opener（不带 ProxyHandler，避免坏代理重置 Cloudflare 连接；
    # 若确需代理才能出网，可改为 build_opener(_IPv4HTTPSHandler(), ProxyHandler(...))）
    opener = urllib.request.build_opener(_IPv4HTTPSHandler())
    with opener.open(req, timeout=timeout) as resp:
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
        http_code 为 -1 表示重试耗尽仍为网络/异常错误（非 HTTP 层）；
        body_text 在网络错误时会带传输层诊断信息。

    说明:
        - 候选按 chunk_size 拆成多片，逐片以「同一 round_id」POST；
          服务端为 INSERT OR REPLACE，幂等累积，分片失败重试不会重复/丢失。
        - 网络层错误（WinError 10054 / 超时 / 连接重置等）按指数退避重试；
          HTTP 401/4xx 直接返回（不可重试），5xx 视为可重试。
        - 全程强制 IPv4 连接，规避 Windows 上 IPv6 出口到 Cloudflare 被 RST。
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
                diag = _diagnose_net(url, e)
                last_code, last_body = -1, f"{e} {diag}"
                if attempt < retries:
                    time.sleep(backoff * (2 ** attempt))
                    continue
                return -1, last_body
        if not ok:
            # 该分片重试耗尽仍失败 —— 非致命，本地已落盘，直接返回
            return last_code, last_body
        if idx < len(chunks) - 1:
            time.sleep(0.3)  # 分片间隔，降低频率，规避限流
    return last_code, last_body
