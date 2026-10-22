"""
本地 mock Worker：模拟 Cloudflare Worker 的 /api/ingest、/api/exists、/api/hits 契约。
用内存 dict 模拟 D1 的 clue_id 唯一约束（含 UPSERT 计数语义）。
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

RECEIVED = []          # 收到的 ingest 请求体
TOKEN = "test-token-12345"
CLUES = {}             # clue_id -> {round_id, hit_count, like_count, reply_count}
ROUND_HITS = []        # 收到的 hits 请求体
EXISTS_QUERIES = []    # 收到的 exists 请求体


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        if auth != "Bearer " + TOKEN:
            self.send_response(401)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _reply(self, obj, code=200):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def do_POST(self):
        if not self._authed():
            return
        if self.path == "/api/ingest":
            body = self._body()
            RECEIVED.append(body)
            cands = body.get("candidates") or []
            inserted = updated = 0
            for c in cands:
                cid = str(c.get("id", ""))
                if not cid:
                    continue
                if cid in CLUES:
                    CLUES[cid]["hit_count"] += 1
                    CLUES[cid]["last_seen_round"] = body.get("round_id", "")
                    CLUES[cid]["like_count"] = max(CLUES[cid]["like_count"], int(c.get("like_count") or 0))
                    CLUES[cid]["reply_count"] = max(CLUES[cid]["reply_count"], int(c.get("reply_count") or 0))
                    updated += 1
                else:
                    CLUES[cid] = {
                        "round_id": body.get("round_id", ""),
                        "first_seen_round": body.get("round_id", ""),
                        "last_seen_round": body.get("round_id", ""),
                        "hit_count": 1,
                        "like_count": int(c.get("like_count") or 0),
                        "reply_count": int(c.get("reply_count") or 0),
                        "text": c.get("text", ""),
                    }
                    inserted += 1
            return self._reply({"ok": True, "round_id": body.get("round_id"),
                                 "clues": len(cands), "inserted": inserted, "updated": updated})

        if self.path == "/api/exists":
            body = self._body()
            EXISTS_QUERIES.append(body)
            ids = [str(x) for x in (body.get("clue_ids") or [])]
            exists = [x for x in ids if x in CLUES]
            exist_set = set(exists)
            missing = [x for x in ids if x not in exist_set]
            return self._reply({"exists": exists, "missing": missing, "total": len(ids)})

        if self.path == "/api/hits":
            body = self._body()
            ROUND_HITS.append(body)
            for cid in (body.get("clue_ids") or []):
                if str(cid) in CLUES:
                    CLUES[str(cid)]["hit_count"] += 1
                    CLUES[str(cid)]["last_seen_round"] = body.get("round_id", "")
            return self._reply({"ok": True, "updated": len(body.get("clue_ids") or [])})

        self.send_response(404)
        self._cors()
        self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    srv = HTTPServer(("127.0.0.1", port), H)
    print("mock worker on http://127.0.0.1:%d/api/ingest  token=%s" % (port, TOKEN))
    srv.serve_forever()
