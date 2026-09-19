# -*- coding: utf-8 -*-
"""
回填历史数据到 Cloudflare D1（经 Worker /api/ingest）。

读取本地 SQLite 评论库（xueqiu.db / hashtag_comments.db），用 Layer1 规则
重新打分生成候选，按「话题」分组为若干轮次，逐轮上传到 Worker。
Worker 端按 (round_id, clue_id) 幂等去重，重复运行不会翻倍。

用法：
  # 只预览将要上传哪些轮次 / 各轮候选条数（不真正上传）
  python backfill_d1.py --dry-run

  # 上传到本地 wrangler dev（先 `wrangler dev` 起在 127.0.0.1:8787）
  python backfill_d1.py --worker-url http://127.0.0.1:8787/api/ingest --worker-token <token>

  # 上传到线上 Worker（部署后）
  python backfill_d1.py --worker-url https://xueqiu.你的域名.com/api/ingest --worker-token <token>
  # 或用环境变量：WORKER_URL / WORKER_TOKEN
"""

import os
import sys
import sqlite3
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uploader
import clue_extractor as ce

BASE = os.path.dirname(os.path.abspath(__file__))


def read_comments(db_path, cols_of_interest):
    """读取某个 DB 的 comments 表，返回 [{...}]（防御性取列）。"""
    if not os.path.exists(db_path):
        return [], set()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(comments)")}
        rows = conn.execute("SELECT * FROM comments").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = {}
        for k in cols_of_interest:
            if k in cols:
                d[k] = r[k]
        # 归一化用户名
        if not d.get("user_name") and d.get("user_screen_name"):
            d["user_name"] = d["user_screen_name"]
        d["like_count"] = d.get("like_count") or 0
        d["reply_count"] = 0  # 老库无此列，回填记为 0
        out.append(d)
    return out, cols


def row_to_group_key(d, group_by):
    if group_by == "hashtag":
        return (d.get("hashtag") or "").strip() or "（未分类话题）"
    return None


def backfill(source, db_path, group_by, url, token, dry_run, threshold=5):
    cols_of_interest = [
        "id", "post_id", "post_author", "hashtag", "text",
        "user_name", "user_screen_name", "like_count", "time_str",
    ]
    comments, cols = read_comments(db_path, cols_of_interest)
    if not comments:
        print(f"  [{source}] {db_path} 无评论数据，跳过")
        return 0, 0

    # 按 group_by 分组（每个话题 = 一轮）
    groups = {}
    for d in comments:
        key = row_to_group_key(d, group_by) or f"{source}-历史"
        groups.setdefault(key, []).append(d)

    total_cands = 0
    total_uploads = 0
    print(f"  [{source}] 读取 {len(comments)} 条评论，分成 {len(groups)} 个话题/轮次")

    for gkey, gcomments in sorted(groups.items()):
        candidates, _ = ce.extract_clues(
            gcomments, threshold=threshold, max_candidates=5000
        )
        meta = {
            "title": gkey,
            "hashtag": gkey if group_by == "hashtag" else "",
            "generated_at": "2026-09-19 12:00:00",
            "total_comments": len(gcomments),
        }
        payload = uploader.build_payload(meta, candidates, gcomments, source=source)
        total_cands += len(candidates)
        total_uploads += 1

        if dry_run:
            print(f"    话题「{gkey}」: 评论 {len(gcomments)} 条 -> 候选 {len(candidates)} 条 | round_id={payload['round_id'][:40]}…")
        else:
            code, body = uploader.upload_round(payload, url, token)
            ok = "OK" if code == 200 else f"FAIL({code})"
            print(f"    [{ok}] 话题「{gkey}」: 候选 {len(candidates)} 条 -> {body[:80]}")

    return total_uploads, total_cands


def main():
    ap = argparse.ArgumentParser(description="回填历史评论到 D1")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不真正上传")
    ap.add_argument("--worker-url", default=None, help="Worker ingest 地址")
    ap.add_argument("--worker-token", default=None, help="Bearer token")
    ap.add_argument("--threshold", type=int, default=0,
                    help="候选最低分（回填默认 0 = 收录所有非灌水评论；线上实时上传用 5）")
    args = ap.parse_args()

    url = args.worker_url or os.environ.get("WORKER_URL") or ""
    token = args.worker_token or os.environ.get("WORKER_TOKEN") or ""

    if not args.dry_run and (not url or not token):
        print("[!] 非 dry-run 模式需要 --worker-url 与 --worker-token（或环境变量 WORKER_URL/WORKER_TOKEN）")
        sys.exit(2)

    print("=== 雪球雷达 历史回填 ===")
    if not args.dry_run:
        # 回显脱敏 token，方便与 .dev.vars 里的 INGEST_TOKEN 对齐（两边必须一致）
        masked = (token[:4] + "***" + token[-2:]) if len(token) > 6 else "***"
        print(f"（上传 token: {masked}  —— 须与本地 Worker .dev.vars 的 INGEST_TOKEN 一致）\n")
    if args.dry_run:
        print("(dry-run 模式：仅预览，不会上传)\n")
    else:
        print(f"目标: {url}\n")

    total_rounds = 0
    total_cands = 0

    # 话题库（真实历史所在）
    n, c = backfill(
        "hashtag", os.path.join(BASE, "data", "hashtag_comments.db"),
        group_by="hashtag", url=url, token=token,
        dry_run=args.dry_run, threshold=args.threshold,
    )
    total_rounds += n
    total_cands += c

    # 推荐/热门库（当前为空，保留兼容）
    n, c = backfill(
        "recommend", os.path.join(BASE, "data", "xueqiu.db"),
        group_by=None, url=url, token=token,
        dry_run=args.dry_run, threshold=args.threshold,
    )
    total_rounds += n
    total_cands += c

    print(f"\n=== 完成：{total_rounds} 个轮次，共 {total_cands} 条候选"
          + ("（dry-run，未上传）" if args.dry_run else "（已上传）") + " ===")


if __name__ == "__main__":
    main()
