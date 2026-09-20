# -*- coding: utf-8 -*-
"""
雪球话题评论 — Layer 1 价值提取器 (insight_extractor.py)

功能:
- 读取 hashtag_comments.db 中已抓取的评论
- 调用通用 clue_extractor 做规则打分 + 剔除灌水, 筛选"可能有价值"的候选评论
- 按 [标的] / [分数] 整理, 生成可直接发给大模型的提示词文档

用法:
    python insight_extractor.py
产出:
    data/exports/insight_<short>_<ts>.md   整理内容 + 提示词(可直接复制, 默认只产出 md)
    data/exports/insight_<short>_<ts>.json 结构化候选数据(供程序消费, 需 write_json=True)

注: 本文件的打分口径与 scraper.py 完全一致, 均来自 clue_extractor.py, 避免逻辑漂移。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

import clue_extractor
from clue_extractor import extract_clues, render_clues_markdown, render_clues_json, generate_clue_files

# ── 路径配置（支持 EXE 打包）──
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "hashtag_comments.db")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
# 已分析评论 id 记录（增量分析：分析过的评论下轮不再重复）
SEEN_PATH = os.path.join(DATA_DIR, "seen_hashtag_comments.json")


def seen_path_for(short):
    """按话题 short 生成独立的「已分析评论」记录文件，避免不同话题串味。"""
    return os.path.join(DATA_DIR, f"seen_hashtag_{short}.json")

# ── 话题标识（与 hashtag_comments.py 保持一致，便于文件名对应）──
HASHTAG_SHORT = "walsh_rate_hike"
HASHTAG_NAME = "沃什：加息25基点至4%，通胀难降但就业不伤"

# ── 参数 ──
SCORE_THRESHOLD = 5          # 进入候选池的最低分
MAX_LLM_CANDIDATES = 80      # 发给大模型的候选上限（按分数截取）
INCREMENTAL = True           # True=只分析新出现的评论（分析过的不再重复）
INCLUDE_LLM_PROMPT = False   # False=只输出适合人工阅读的内容（默认自己看，不需要 AI 提示词区块）

# ── 上传到 Cloudflare Worker（雪球雷达 · 线索台）──
UPLOAD_ENABLED = False
UPLOAD_URL = ""               # 如 https://xueqiu.你的域名.com/api/ingest
UPLOAD_TOKEN = ""             # 与 Worker 端 INGEST_TOKEN 一致
UPLOAD_SOURCE = "hashtag"


def load_comments():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM comments ORDER BY like_count DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def build_comment_dicts(rows):
    """把 DB 行转换为 clue_extractor 期望的 comment dict 列表"""
    # 兼容老库：个别字段（如 reply_count）可能尚未迁移，缺失时按默认值处理
    cols = set(rows[0].keys()) if rows else set()

    def _g(r, k, d=0):
        return r[k] if k in cols else d

    out = []
    for r in rows:
        out.append({
            "id": _g(r, "id"),
            "user_name": _g(r, "user_name") or "",
            "post_author": _g(r, "post_author") or "",
            "time_str": _g(r, "time_str") or "",
            "created_at": _g(r, "created_at") or 0,
            "like_count": _g(r, "like_count") or 0,
            "reply_count": _g(r, "reply_count") or 0,
            "text": _g(r, "text") or "",
            "hashtag": _g(r, "hashtag") or "",
        })
    return out


def main(write_json=False, incremental=INCREMENTAL, short=None, name=None,
         seen_path=None, include_prompt=INCLUDE_LLM_PROMPT,
         upload=UPLOAD_ENABLED, upload_url=UPLOAD_URL, upload_token=UPLOAD_TOKEN,
         upload_source=UPLOAD_SOURCE, jev_api_key=None):
    if not os.path.exists(DB_PATH):
        print(f"[错误] 未找到数据库：{DB_PATH}\n请先运行 hashtag_comments.py 抓取评论。")
        return

    short = short or HASHTAG_SHORT
    name = name or HASHTAG_NAME
    seen_path = seen_path or seen_path_for(short)

    rows = load_comments()
    total = len(rows)
    comment_dicts = build_comment_dicts(rows)

    os.makedirs(EXPORT_DIR, exist_ok=True)

    meta = {
        "title": "雪球话题评论 · 价值候选提炼（Layer 1 规则）",
        "context_desc": f"雪球用户讨论：{name}。以下评论集中于相关题材的个股联动与产业链消息。",
        "threshold": SCORE_THRESHOLD,
        "total_comments": total,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "max_llm_candidates": MAX_LLM_CANDIDATES,
    }

    # 统一走 clue_extractor.generate_clue_files（与推荐版/统一入口同一产出函数）
    prefix = f"insight_{short}"
    md_path, json_path, candidates = generate_clue_files(
        comment_dicts, meta, EXPORT_DIR, prefix,
        write_json=write_json, seen_path=seen_path, incremental=incremental,
        include_prompt=include_prompt,
        upload=upload, upload_url=upload_url, upload_token=upload_token,
        upload_source=upload_source, jev_api_key=jev_api_key)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Layer 1 提取完成"
          + ("（增量：只分析新评论）" if incremental else "（全量）"))
    print(f"  库内评论: {total}  候选(≥{SCORE_THRESHOLD}分): {len(candidates)}")
    print(f"  MD   : {md_path}  (含可直接复制的大模型提示词)")
    if json_path:
        print(f"  JSON : {json_path}")


if __name__ == "__main__":
    main()
