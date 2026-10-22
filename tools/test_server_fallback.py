"""
服务端兜底去重验证：直接对 mock worker 发两次完全相同的 ingest，
验证即使客户端不做任何去重，数据库也不会出现重复行。
（真实 Worker 的 ingest 逻辑已由 tools/test_worker_dedup.mjs 用 node:sqlite 验证）
"""
import json
import sys
import threading
import time
import urllib.request
from http.server import HTTPServer

sys.path.insert(0, __import__("os").path.dirname(__file__))
import mock_worker

PORT = 8801
mock_worker.TOKEN = "test-token-12345"


def post(path, body, token="test-token-12345"):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.getcode(), json.loads(r.read().decode())


def main():
    srv = HTTPServer(("127.0.0.1", PORT), mock_worker.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    cands = [{"id": f"x{i}", "user_name": "u", "text": "t", "score": 8, "tags": [],
               "stocks": [], "section": "hot", "time_str": "2026-10-22 10:00",
               "like_count": 5, "reply_count": 1, "post_id": "1"} for i in range(10)]

    print("=" * 60)
    print("服务端兜底去重验证")
    print("=" * 60)

    # 第 1 次：正常上传
    code, r1 = post("/api/ingest", {"round_id": "R1", "meta": {"source": "auto"},
                                    "candidates": cands, "comments": []})
    print(f"\n第1次 ingest → {code}  {r1}")

    # 第 2 次：完全相同的内容，换个 round_id（模拟跨轮重复）
    code, r2 = post("/api/ingest", {"round_id": "R2", "meta": {"source": "auto"},
                                    "candidates": cands, "comments": []})
    print(f"第2次 ingest → {code}  {r2}")
    print(f"  >>> inserted={r2.get('inserted')} updated={r2.get('updated')} "
          f"{'✓ 未新增行（走 UPSERT）' if r2.get('inserted') == 0 else '✗ 新增了重复行'}")

    print(f"\nmock worker 库内 clue_id 数: {len(mock_worker.CLUES)}  "
          f"{'✓ 无重复' if len(mock_worker.CLUES) == 10 else '✗ 有重复'}")

    # 探针验证
    code, ex = post("/api/exists", {"clue_ids": ["x0", "x1", "x99"]})
    print(f"\n/api/exists → {code}  {ex}")
    print(f"  >>> exists={ex.get('exists')} missing={ex.get('missing')}")

    # hits 上报
    code, hr = post("/api/hits", {"clue_ids": ["x0", "x1"], "round_id": "R3"})
    print(f"\n/api/hits → {code}  {hr}")
    print(f"  >>> x0.hit_count = {mock_worker.CLUES['x0']['hit_count']}  "
          f"x1.hit_count = {mock_worker.CLUES['x1']['hit_count']}")
    print(f"  >>> last_seen_round = {mock_worker.CLUES['x0']['last_seen_round']}")

    # 鉴权验证
    try:
        post("/api/exists", {"clue_ids": ["x0"]}, token="wrong-token")
        print("\n鉴权: ✗ 错误 token 竟然通过了")
    except urllib.error.HTTPError as e:
        print(f"\n鉴权: ✓ 错误 token 被拒（HTTP {e.code}）")

    srv.shutdown()
    # hit_count 累加路径：第1次 ingest +1，第2次 ingest(UPSERT) +1，/api/hits +1 = 3
    ok = (r2.get("inserted") == 0 and len(mock_worker.CLUES) == 10
          and mock_worker.CLUES["x0"]["hit_count"] == 3)
    print(f"\n{'✓ 全部通过' if ok else '✗ 存在问题'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
