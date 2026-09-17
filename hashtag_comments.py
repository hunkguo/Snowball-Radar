# -*- coding: utf-8 -*-
"""
雪球话题评论抓取器 — hashtag_comments.py

功能:
- 进入指定雪球话题页 (hashtag), 提取该话题下的帖子
- 逐条抓取每个帖子的评论 (小道消息 / 有价值信息主要在此)
- SQLite 去重存储 (按评论 ID)
- 每次运行增量导出 JSON (首次全量, 后续只导出新评论), 文件控制在 1MB 以内

用法:
    python hashtag_comments.py
可调参数见文件底部 CONFIG 与 XueqiuHashtagScraper(...) 调用。
"""

import json
import os
import re
import sqlite3
import sys
import time
import random
from datetime import datetime

from playwright.sync_api import sync_playwright

# ── 路径配置（支持 EXE 打包）──
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "hashtag_comments.db")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

# ── 抓取目标配置 ──
HASHTAG_URL = "https://xueqiu.com/hashtag/I-ayg-S7gO-8muWKoOaBrzI15Z-654K56IezNCXvvIzpgJrog4Dpmr7pmY3kvYblsLHkuJrkuI3kvKQj"
HASHTAG_NAME = "沃什：加息25基点至4%，通胀难降但就业不伤"  # 话题标题(用于标注/检索)
HASHTAG_SHORT = "walsh_rate_hike"  # 导出文件名用的短标识

# ── 行为参数 ──
SCROLL_ROUNDS = 8          # 滚动加载帖子次数
MAX_COMMENT_PAGES = 15     # 单帖评论最多翻页数
POST_DELAY = (3, 6)        # 帖子间随机停顿（秒）
COMMENT_PAGE_DELAY = (1, 3)
HEADLESS = True            # 无头模式（可后台运行）；需看登录过程改为 False


# ── 工具函数 ──
def _strip_tags(html):
    if not html:
        return ""
    txt = re.sub(r"<br\s*/?>", "\n", html)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = (txt.replace("&amp;", "&").replace("&lt;", "<")
              .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── 数据库 ──
class HashtagDB:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                post_id TEXT,
                post_author TEXT,
                hashtag TEXT,
                text TEXT,
                user_id TEXT,
                user_name TEXT,
                like_count INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT 0,
                time_str TEXT,
                first_seen TEXT,
                last_updated TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_hc_post ON comments(post_id)")
        c.execute("CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, author TEXT, hashtag TEXT, first_seen TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.commit()

    def get_last_export_time(self):
        c = self.conn.cursor()
        c.execute("SELECT value FROM meta WHERE key='last_export_time'")
        row = c.fetchone()
        return row["value"] if row else None

    def set_last_export_time(self, t):
        c = self.conn.cursor()
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_export_time',?)", (t,))
        self.conn.commit()

    def save_post(self, pid, author):
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO posts(id,author,hashtag,first_seen) VALUES(?,?,?,?)",
            (pid, author, HASHTAG_NAME, now))
        self.conn.commit()

    def save_comment(self, c):
        now = datetime.now().isoformat()
        self.conn.execute(
            """INSERT INTO comments(id,post_id,post_author,hashtag,text,user_id,user_name,
               like_count,created_at,time_str,first_seen,last_updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 text=excluded.text, like_count=excluded.like_count, last_updated=excluded.last_updated""",
            (c["id"], c["post_id"], c["post_author"], HASHTAG_NAME, c["text"],
             c["user_id"], c["user_name"], c["like_count"], c["created_at"],
             c["time_str"], now, now))
        self.conn.commit()

    def count(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) FROM comments")
        return c.fetchone()[0]

    def export_incremental(self, output_path):
        c = self.conn.cursor()
        last = self.get_last_export_time()
        is_first = last is None
        if is_first:
            c.execute("SELECT * FROM comments ORDER BY created_at DESC")
        else:
            c.execute("SELECT * FROM comments WHERE first_seen > ? ORDER BY created_at DESC", (last,))
        rows = c.fetchall()

        comments = []
        for r in rows:
            comments.append({
                "id": r["id"],
                "post_id": r["post_id"],
                "post_author": r["post_author"],
                "user_id": r["user_id"],
                "user_name": r["user_name"],
                "text": r["text"],
                "like_count": r["like_count"],
                "time_str": r["time_str"] or "",
            })

        out = {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": "xueqiu",
            "hashtag": HASHTAG_NAME,
            "export_type": "full" if is_first else "incremental",
            "since": last or "",
            "comment_count": len(comments),
            "db_total": self.count(),
            "comments": comments,
        }

        # 紧凑写入
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

        # 1MB 安全阀：超限则截断评论文本
        if os.path.getsize(output_path) > 1024 * 1024:
            for cm in comments:
                if len(cm["text"]) > 500:
                    cm["text"] = cm["text"][:500] + "..."
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

        self.set_last_export_time(datetime.now().isoformat())
        return out

    def close(self):
        self.conn.close()


# ── 主抓取器 ──
class XueqiuHashtagScraper:
    def __init__(self, db, hashtag_url, hashtag_name, headless=True,
                 scroll_rounds=8, max_comment_pages=15):
        self.db = db
        self.url = hashtag_url
        self.name = hashtag_name
        self.headless = headless
        self.scroll_rounds = scroll_rounds
        self.max_comment_pages = max_comment_pages

    def _extract_post_ids(self, page):
        return page.evaluate("""
            () => {
              const ids = new Map();
              document.querySelectorAll('article.timeline__item').forEach(a => {
                const link = a.querySelector('a[data-id]');
                const authorEl = a.querySelector('.user-name');
                if (link) {
                  const v = link.getAttribute('data-id');
                  if (/^\\d{6,}$/.test(v)) {
                    ids.set(v, authorEl ? authorEl.textContent.trim() : '');
                  }
                }
              });
              return Array.from(ids.entries()).map(([id, author]) => ({id, author}));
            }
        """)

    def _fetch_comments(self, page, post_id):
        return page.evaluate("""
            async (postId) => {
              let all = [];
              for (let p = 1; p <= %d; p++) {
                const url = `https://xueqiu.com/statuses/comments.json?id=${postId}&page=${p}&count=20`;
                try {
                  const r = await fetch(url, { credentials: 'include' });
                  const t = await r.text();
                  let j; try { j = JSON.parse(t); } catch(e) { break; }
                  const list = j.comments || j.list || [];
                  if (!list.length) break;
                  all = all.concat(list);
                  if (list.length < 20) break;
                } catch(e) { break; }
              }
              return all;
            }
        """ % self.max_comment_pages, post_id)

    def run(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                user_data_dir=os.path.join(DATA_DIR, "chrome_profile"),
                channel="chrome",
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(self.url, wait_until="domcontentloaded")
            _log(f"已打开话题页: {self.name}")
            page.wait_for_timeout(5000)

            # 滚动加载更多帖子
            for i in range(self.scroll_rounds):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(random.uniform(1.5, 3.0))
            page.wait_for_timeout(2000)

            posts = self._extract_post_ids(page)
            _log(f"提取到 {len(posts)} 个帖子")

            total_new = 0
            for idx, p in enumerate(posts, 1):
                pid = p["id"]
                author = p.get("author", "")
                self.db.save_post(pid, author)
                _log(f"  ({idx}/{len(posts)}) 抓取帖子 {pid} 的评论…")
                raw = self._fetch_comments(page, pid)
                saved_this = 0
                for cm in raw:
                    text = _strip_tags(cm.get("text", ""))
                    if not text:
                        continue
                    user = cm.get("user", {}) or {}
                    ca = cm.get("created_at") or 0
                    if isinstance(ca, str):
                        try:
                            ca = int(ca)
                        except Exception:
                            ca = 0
                    row = {
                        "id": str(cm.get("id")),
                        "post_id": pid,
                        "post_author": author,
                        "text": text,
                        "user_id": str(user.get("id", "")),
                        "user_name": user.get("screen_name", ""),
                        "like_count": cm.get("like_count", 0) or 0,
                        "created_at": int(ca) // 1000 if ca > 10**12 else int(ca),
                        "time_str": cm.get("timeStr") or cm.get("timeBefore") or "",
                    }
                    self.db.save_comment(row)
                    saved_this += 1
                total_new += saved_this
                _log(f"      评论 {len(raw)} 条, 入库 {saved_this} 条（累计 {self.db.count()}）")
                page.wait_for_timeout(random.uniform(*POST_DELAY))

            _log(f"本轮完成，共入库评论 {total_new} 条")
            page.wait_for_timeout(1500)
            browser.close()

        # 导出 JSON
        os.makedirs(EXPORT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(EXPORT_DIR, f"hashtag_comments_{HASHTAG_SHORT}_{ts}.json")
        res = self.db.export_incremental(export_path)
        size_kb = os.path.getsize(export_path) / 1024
        _log(f"JSON 导出: {export_path}  ({size_kb:.1f} KB, 类型={res['export_type']}, 评论={res['comment_count']}条)")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    db = HashtagDB(DB_PATH)
    scraper = XueqiuHashtagScraper(
        db, HASHTAG_URL, HASHTAG_NAME,
        headless=HEADLESS,
        scroll_rounds=SCROLL_ROUNDS,
        max_comment_pages=MAX_COMMENT_PAGES,
    )
    try:
        scraper.run()
    finally:
        db.close()
        _log("数据库已关闭")


if __name__ == "__main__":
    main()
