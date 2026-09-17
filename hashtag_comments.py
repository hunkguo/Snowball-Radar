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
HASHTAG_SHORT = "walsh_rate_hike"  # 导出文件名用的短标识（仅兜底；自动发现启用后会被最新话题名覆盖）
# 是否自动从雪球首页右侧「热门话题」取当前最热话题来抓（False 则始终抓上面写死的 HASHTAG_URL）
AUTO_DISCOVER_HOT_TOPIC = True

# ── 行为参数 ──
SCROLL_ROUNDS = 8          # 滚动加载帖子次数
MAX_COMMENT_PAGES = 15     # 单帖评论最多翻页数
POST_DELAY = (3, 6)        # 帖子间随机停顿（秒）
COMMENT_PAGE_DELAY = (1, 3)
HEADLESS = True            # 无头模式（可后台运行）；需看登录过程改为 False

# ── 持续运行参数 ──
CONTINUOUS = True               # True=持续运行; False=单次运行后退出
RUN_INTERVAL_MIN = 45           # 抓取间隔下限（分钟）
RUN_INTERVAL_MAX = 60           # 抓取间隔上限（分钟）
GEN_INSIGHT_EACH_ROUND = True   # 每轮同时生成 Layer1 价值候选(insight_*.md)
EXPORT_JSON = False             # 是否导出每轮原始评论 JSON（默认关闭，只产出价值线索 md）

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


def _slugify(text, max_len=24):
    """把话题标题转成可用于文件名的安全短标识（中文保留，其他替换为下划线）。"""
    if not text:
        return "topic"
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "_", text)
    s = s.strip("_")
    return s[:max_len] or "topic"


def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("utf-8", "replace").decode("utf-8"), flush=True)


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

    def save_post(self, pid, author, hashtag=HASHTAG_NAME):
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO posts(id,author,hashtag,first_seen) VALUES(?,?,?,?)",
            (pid, author, hashtag, now))
        self.conn.commit()

    def save_comment(self, c):
        now = datetime.now().isoformat()
        self.conn.execute(
            """INSERT INTO comments(id,post_id,post_author,hashtag,text,user_id,user_name,
               like_count,created_at,time_str,first_seen,last_updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 text=excluded.text, like_count=excluded.like_count, last_updated=excluded.last_updated""",
            (c["id"], c["post_id"], c["post_author"], c.get("hashtag", HASHTAG_NAME), c["text"],
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
                 scroll_rounds=8, max_comment_pages=15,
                 auto_discover=True, short=None):
        self.db = db
        self.url = hashtag_url
        self.name = hashtag_name
        self.short = short or _slugify(hashtag_name)
        self.headless = headless
        self.scroll_rounds = scroll_rounds
        self.max_comment_pages = max_comment_pages
        self.auto_discover = auto_discover
        self._running = True

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

    def _discover_hot_topic(self, page):
        """打开雪球首页，从右侧「热门话题」框取第一个（最热）话题的链接与标题。

        返回 (url, title)；解析失败时返回 (None, None)。
        """
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(6000)  # 等右侧栏异步加载
        res = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('*'));
            let container = null;
            for (const el of els) {
                const t = (el.textContent || '').trim();
                if (t && t.indexOf('热门话题') !== -1 && t.length <= 20) {
                    let node = el;
                    for (let i = 0; i < 5; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        if (node.querySelector('a[href*="/hashtag/"]')) { container = node; break; }
                    }
                    if (container) break;
                }
            }
            if (!container) return {error: 'no 热门话题 container'};
            const links = Array.from(container.querySelectorAll('a[href*="/hashtag/"]'))
                .filter(a => !/查看|更多|全部|话题榜|换一换/.test(a.textContent || ''));
            if (!links.length) return {error: 'no hashtag links'};
            const a = links[0];
            return {url: a.href, title: (a.getAttribute('title') || a.textContent || '').trim()};
        }""")
        if isinstance(res, dict) and res.get("url"):
            return res["url"], res.get("title") or ""
        return None, None

    def scrape_once(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                user_data_dir=os.path.join(DATA_DIR, "chrome_profile"),
                channel="chrome",
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            if self.auto_discover:
                try:
                    url, title = self._discover_hot_topic(page)
                    if url:
                        self.url = url
                        if title:
                            self.name = title
                        _log(f"自动发现最新热门话题: {self.name}")
                    else:
                        _log("  自动发现未返回链接，沿用配置话题")
                except Exception as e:
                    _log(f"  自动发现热门话题失败，沿用配置: {e}")
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
                self.db.save_post(pid, author, self.name)
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
                        "hashtag": self.name,
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

        return total_new


    def _export_round(self):
        """每轮生成独立增量 JSON 文件（默认关闭，见 EXPORT_JSON）"""
        if not EXPORT_JSON:
            return None
        os.makedirs(EXPORT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(EXPORT_DIR, f"hashtag_comments_{HASHTAG_SHORT}_{ts}.json")
        res = self.db.export_incremental(export_path)
        size_kb = os.path.getsize(export_path) / 1024
        _log(f"  本轮 JSON 导出: {export_path}  ({size_kb:.1f} KB, 类型={res['export_type']}, 评论={res['comment_count']}条)")
        return export_path

    def _gen_insight(self):
        """每轮同时生成 Layer1 价值候选（可选，需 insight_extractor.py）"""
        try:
            import insight_extractor
            insight_extractor.main(short=self.short, name=self.name)
        except Exception as e:
            _log(f"  生成价值候选失败(可忽略): {e}")

    def _interruptible_sleep(self, seconds):
        """可中断的等待（Ctrl+C 立即响应）"""
        step = 1.0
        waited = 0.0
        while waited < seconds and self._running:
            time.sleep(step)
            waited += step

    def run_forever(self):
        from datetime import timedelta
        _log("=" * 60)
        _log(f"  持续运行模式启动：每 {RUN_INTERVAL_MIN}-{RUN_INTERVAL_MAX} 分钟抓取一轮")
        _log("  数据增量去重存储，每轮生成独立的价值线索 md 文件（不重复）")
        _log("  按 Ctrl+C 可退出程序")
        _log("=" * 60)
        round_no = 0
        try:
            while self._running:
                round_no += 1
                _log(f"\n{'='*60}")
                _log(f"  第 {round_no} 轮抓取  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                _log(f"{'='*60}")
                try:
                    new_count = self.scrape_once()
                except Exception as e:
                    _log(f"  ⚠ 本轮抓取异常: {e}")
                    new_count = 0
                self._export_round()
                if GEN_INSIGHT_EACH_ROUND:
                    self._gen_insight()
                if not self._running:
                    break
                wait_min = random.randint(RUN_INTERVAL_MIN, RUN_INTERVAL_MAX)
                next_t = datetime.now() + timedelta(minutes=wait_min)
                _log(f"\n  本轮结束，新增评论 {new_count} 条")
                _log(f"  下次执行时间: {next_t.strftime('%Y-%m-%d %H:%M:%S')}（约 {wait_min} 分钟后）")
                _log(f"  按 Ctrl+C 退出程序\n")
                self._interruptible_sleep(wait_min * 60)
        except KeyboardInterrupt:
            _log("\n收到 Ctrl+C，准备退出…")
        finally:
            _log("数据库已关闭")
            self.db.close()


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

    # 信号处理：Ctrl+C 优雅退出（本轮结束后停止）
    import signal
    def _handler(signum, frame):
        _log("\n收到退出信号，将在本轮结束后退出…")
        scraper._running = False
    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)

    if CONTINUOUS:
        scraper.run_forever()
    else:
        new_count = scraper.scrape_once()
        scraper._export_round()
        if GEN_INSIGHT_EACH_ROUND:
            scraper._gen_insight()
        _log(f"单次运行完成，新增评论 {new_count} 条")
        db.close()


if __name__ == "__main__":
    main()
