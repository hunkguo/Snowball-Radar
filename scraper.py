"""
雪球网爬虫 v7 — 持续运行版
- 登录后持续运行，每隔一段时间自动抓取（默认 30 分钟）
- 模拟鼠标点击 + 慢速行为
- 数据存入 SQLite，按帖子/评论 ID 去重
- 每 6 小时从 SQLite 导出一份带时间戳的 JSON
- 支持 EXE 打包（PyInstaller frozen 路径检测）
"""

import json
import os
import time
import random
import re
import sys
import sqlite3
import signal
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ── 路径配置（支持 EXE 打包）──
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "xueqiu.db")
JSON_EXPORT_DIR = os.path.join(DATA_DIR, "exports")


def _detect_chrome_user_data():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "AppData", "Local", "Google", "Chrome", "User Data"),
        os.path.join(home, ".config", "google-chrome"),
        os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


CHROME_USER_DATA = _detect_chrome_user_data()

# ── 抓取间隔配置 ──
SCRAPE_INTERVAL_MIN = 30 * 60   # 30 分钟（最小间隔）
SCRAPE_INTERVAL_MAX = 45 * 60   # 45 分钟（最大间隔）
JSON_EXPORT_INTERVAL = 6 * 3600  # 6 小时导出一次 JSON

# ── API 端点 ──
API_RECOMMEND = "https://xueqiu.com/statuses/fundx/public/list.json?source=fund_public&page={page}"
API_HOT = "https://xueqiu.com/statuses/hot/listV2.json?since=-1&max_id=-1&size=15"
API_COMMENTS = "https://xueqiu.com/statuses/comments.json?id={post_id}&page={page}&count=20"

# ── 反检测 JS ──
STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'PDF Viewer', filename: 'internal-pdf-viewer'},
        {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer'},
        {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'},
        {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer'},
        {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer'},
    ],
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
});
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) window.chrome.runtime = {};
const origQuery = window.navigator.permissions?.query;
if (origQuery) {
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(p);
}
"""


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</p>', '\n', text, flags=re.I)
    text = re.sub(r'<p[^>]*>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'")
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


class XueqiuDB:
    """SQLite 数据库管理 — 帖子/评论存储与去重"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                section TEXT NOT NULL,
                title TEXT,
                description TEXT,
                text TEXT,
                created_at INTEGER,
                time_str TEXT,
                reply_count INTEGER DEFAULT 0,
                retweet_count INTEGER DEFAULT 0,
                like_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0,
                source TEXT,
                type TEXT,
                mark TEXT,
                user_id TEXT,
                user_screen_name TEXT,
                user_description TEXT,
                user_followers INTEGER DEFAULT 0,
                user_friends INTEGER DEFAULT 0,
                user_statuses INTEGER DEFAULT 0,
                url TEXT,
                retweeted_id TEXT,
                retweeted_title TEXT,
                retweeted_text TEXT,
                retweeted_screen_name TEXT,
                target TEXT,
                first_seen TEXT,
                last_updated TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL,
                text TEXT,
                created_at INTEGER,
                time_str TEXT,
                like_count INTEGER DEFAULT 0,
                user_id TEXT,
                user_screen_name TEXT,
                first_seen TEXT,
                last_updated TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS scrape_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT,
                end_time TEXT,
                recommend_count INTEGER DEFAULT 0,
                following_count INTEGER DEFAULT 0,
                hot_count INTEGER DEFAULT 0,
                comment_count INTEGER DEFAULT 0,
                new_posts INTEGER DEFAULT 0,
                new_comments INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_section ON posts(section)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments(post_id)")
        self.conn.commit()

    def upsert_post(self, post, section):
        """插入或更新帖子（按 ID 去重）"""
        now = datetime.now().isoformat()
        c = self.conn.cursor()
        pid = post["id"]
        # 检查是否已存在
        c.execute("SELECT 1 FROM posts WHERE id=?", (pid,))
        existed = c.fetchone() is not None

        u = post.get("user", {})
        rt = post.get("retweeted") or {}
        c.execute("""
            INSERT INTO posts (
                id, section, title, description, text, created_at, time_str,
                reply_count, retweet_count, like_count, view_count,
                source, type, mark,
                user_id, user_screen_name, user_description,
                user_followers, user_friends, user_statuses,
                url, retweeted_id, retweeted_title, retweeted_text,
                retweeted_screen_name, target, first_seen, last_updated
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
                section=excluded.section,
                title=excluded.title,
                description=excluded.description,
                text=excluded.text,
                reply_count=excluded.reply_count,
                retweet_count=excluded.retweet_count,
                like_count=excluded.like_count,
                view_count=excluded.view_count,
                last_updated=excluded.last_updated
        """, (
            pid, section,
            post.get("title", ""), post.get("description", ""), post.get("text", ""),
            post.get("created_at", 0), post.get("time_str", ""),
            post.get("reply_count", 0), post.get("retweet_count", 0),
            post.get("like_count", 0), post.get("view_count", 0),
            post.get("source", ""), post.get("type", ""), post.get("mark", ""),
            str(u.get("id", "")), u.get("screen_name", ""), u.get("description", ""),
            u.get("followers_count", 0), u.get("friends_count", 0), u.get("statuses_count", 0),
            post.get("url", ""),
            str(rt.get("id", "")), rt.get("title", ""), rt.get("text", ""),
            rt.get("screen_name", ""),
            json.dumps(post.get("target")) if post.get("target") else None,
            now if not existed else None, now
        ))
        self.conn.commit()
        return not existed  # True = 新帖子

    def upsert_comment(self, comment, post_id):
        """插入或更新评论（按 ID 去重）"""
        now = datetime.now().isoformat()
        c = self.conn.cursor()
        cid = comment["id"]
        c.execute("SELECT 1 FROM comments WHERE id=?", (cid,))
        existed = c.fetchone() is not None

        u = comment.get("user", {})
        c.execute("""
            INSERT INTO comments (
                id, post_id, text, created_at, time_str,
                like_count, user_id, user_screen_name,
                first_seen, last_updated
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
                text=excluded.text,
                like_count=excluded.like_count,
                last_updated=excluded.last_updated
        """, (
            cid, post_id,
            comment.get("text", ""), comment.get("created_at", 0), comment.get("time_str", ""),
            comment.get("like_count", 0),
            str(u.get("id", "")), u.get("screen_name", ""),
            now if not existed else None, now
        ))
        self.conn.commit()
        return not existed

    def start_run(self):
        c = self.conn.cursor()
        c.execute("INSERT INTO scrape_runs (start_time) VALUES (?)",
                  (datetime.now().isoformat(),))
        self.conn.commit()
        return c.lastrowid

    def end_run(self, run_id, counts):
        c = self.conn.cursor()
        c.execute("""
            UPDATE scrape_runs SET
                end_time=?,
                recommend_count=?, following_count=?, hot_count=?,
                comment_count=?, new_posts=?, new_comments=?
            WHERE run_id=?
        """, (
            datetime.now().isoformat(),
            counts.get("recommend", 0), counts.get("following", 0), counts.get("hot", 0),
            counts.get("comments", 0),
            counts.get("new_posts", 0), counts.get("new_comments", 0),
            run_id
        ))
        self.conn.commit()

    def export_to_json(self, output_path, hours_back=48, max_comments_per_post=10):
        """从 SQLite 导出最近数据到 JSON（控制在 1MB 以内）

        优化策略:
        - 只导出最近 hours_back 小时内的帖子
        - 去掉冗余字段（description 与 text 相同时只保留 text）
        - 去掉空值字段
        - 每帖最多导出 max_comments_per_post 条评论
        - 紧凑 JSON 序列化（无缩进）
        """
        c = self.conn.cursor()

        # 计算时间 cutoff（created_at 是秒级时间戳）
        cutoff_ts = 0
        if hours_back and hours_back > 0:
            cutoff_ts = int((datetime.now() - timedelta(hours=hours_back)).timestamp())

        # 导出帖子（仅最近 hours_back 小时）
        if cutoff_ts > 0:
            c.execute(
                "SELECT * FROM posts WHERE created_at >= ? ORDER BY created_at DESC",
                (cutoff_ts,)
            )
        else:
            c.execute("SELECT * FROM posts ORDER BY created_at DESC")
        posts_rows = c.fetchall()

        # 收集帖子 ID，只导出这些帖子的评论
        post_ids = set(pr["id"] for pr in posts_rows)
        if post_ids:
            placeholders = ",".join("?" * len(post_ids))
            c.execute(
                f"SELECT * FROM comments WHERE post_id IN ({placeholders}) ORDER BY created_at ASC",
                tuple(post_ids)
            )
        else:
            c.execute("SELECT * FROM comments WHERE 0")
        comments_rows = c.fetchall()

        # 按板块组织
        sections = {"recommend": [], "following": [], "hot": []}
        comments_by_post = {}
        for cr in comments_rows:
            pid = cr["post_id"]
            if pid not in comments_by_post:
                comments_by_post[pid] = []
            comments_by_post[pid].append({
                "id": cr["id"],
                "text": cr["text"],
                "time_str": cr["time_str"] or "",
                "like_count": cr["like_count"],
                "user": cr["user_screen_name"] or "",
            })

        all_ids = set()
        for pr in posts_rows:
            section = pr["section"]
            if section not in sections:
                sections[section] = []

            # 精简字段：去掉空值
            post_dict = {
                "id": pr["id"],
                "title": pr["title"] or "",
                "text": pr["text"] or "",
                "time_str": pr["time_str"] or "",
                "like_count": pr["like_count"],
                "reply_count": pr["reply_count"],
                "user": pr["user_screen_name"] or "",
                "url": pr["url"] or "",
                "comments": comments_by_post.get(pr["id"], [])[:max_comments_per_post],
            }
            # 只在有内容时添加可选字段
            if pr["view_count"]:
                post_dict["view_count"] = pr["view_count"]
            if pr["retweet_count"]:
                post_dict["retweet_count"] = pr["retweet_count"]
            if pr["source"]:
                post_dict["source"] = pr["source"]

            # 转发内容（精简）
            if pr["retweeted_id"]:
                rt_text = pr["retweeted_text"] or pr["retweeted_title"] or ""
                if rt_text:
                    post_dict["retweeted"] = {
                        "text": rt_text,
                        "user": pr["retweeted_screen_name"] or "",
                    }

            # target（如有）
            if pr["target"]:
                try:
                    post_dict["target"] = json.loads(pr["target"])
                except Exception:
                    pass

            sections[section].append(post_dict)
            all_ids.add(pr["id"])

        # 导出 scrape_runs 摘要（最近 20 轮）
        c.execute("SELECT * FROM scrape_runs ORDER BY run_id DESC LIMIT 20")
        runs = []
        for r in c.fetchall():
            runs.append({
                "run_id": r["run_id"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "new_posts": r["new_posts"],
                "new_comments": r["new_comments"],
            })

        # 数据库总量统计
        c.execute("SELECT COUNT(*) FROM posts")
        db_total_posts = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM comments")
        db_total_comments = c.fetchone()[0]

        output = {
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "platform": "xueqiu",
            "window_hours": hours_back,
            "exported_posts": len(all_ids),
            "exported_comments": len(comments_rows),
            "db_total_posts": db_total_posts,
            "db_total_comments": db_total_comments,
            "sections": sections,
            "recent_runs": runs,
        }

        # 紧凑序列化（无缩进），减小文件体积
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

        # 检查文件大小，如仍超 1MB 则进一步裁剪
        file_size = os.path.getsize(output_path)
        if file_size > 1024 * 1024:
            # 裁剪策略：每帖只保留 3 条评论 + 截断长文本
            for section_posts in sections.values():
                for p in section_posts:
                    if len(p.get("comments", [])) > 3:
                        p["comments"] = p["comments"][:3]
                    # 截断超长文本
                    for key in ("text", "title"):
                        val = p.get(key, "")
                        if len(val) > 500:
                            p[key] = val[:500] + "..."
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, separators=(",", ":"))
            file_size = os.path.getsize(output_path)

        output["_file_size_kb"] = round(file_size / 1024, 1)
        return output

    def get_stats(self):
        c = self.conn.cursor()
        c.execute("SELECT COUNT(*) as cnt FROM posts")
        total_posts = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM comments")
        total_comments = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM posts WHERE section='recommend'")
        rec = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM posts WHERE section='following'")
        foll = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM posts WHERE section='hot'")
        hot = c.fetchone()["cnt"]
        c.execute("SELECT COUNT(*) as cnt FROM scrape_runs")
        runs = c.fetchone()["cnt"]
        return {
            "total_posts": total_posts,
            "total_comments": total_comments,
            "recommend": rec,
            "following": foll,
            "hot": hot,
            "runs": runs,
        }

    def close(self):
        self.conn.close()


class XueqiuScraper:
    def __init__(self, max_pages=3, max_comment_pages=2, max_comment_posts=15,
                 login_wait=300):
        self.max_pages = max_pages
        self.max_comment_pages = max_comment_pages
        self.max_comment_posts = max_comment_posts
        self.login_wait = login_wait
        self.sections_data = {}
        self._running = True
        self._context = None
        self._page = None

        # 初始化 SQLite
        self.db = XueqiuDB(DB_PATH)

        # 初始化日志
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(JSON_EXPORT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(LOG_DIR, f"scrape_{ts}.log")
        self._log_fp = open(self.log_file, "w", encoding="utf-8")

        self._log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 雪球爬虫 v7 启动")
        self._log(f"  BASE_DIR: {BASE_DIR}")
        self._log(f"  DATA_DIR: {DATA_DIR}")
        self._log(f"  DB_PATH:  {DB_PATH}")
        self._log(f"  CHROME_USER_DATA: {CHROME_USER_DATA}")
        self._log(f"  抓取间隔: {SCRAPE_INTERVAL_MIN//60}-{SCRAPE_INTERVAL_MAX//60} 分钟")
        self._log(f"  JSON导出间隔: {JSON_EXPORT_INTERVAL//3600} 小时")

        # 信号处理 (Ctrl+C)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        self._log(f"\n  收到退出信号 ({signum})，正在优雅退出…")
        self._running = False

    def _log(self, msg):
        line = str(msg)
        print(line, flush=True)
        try:
            self._log_fp.write(line + "\n")
            self._log_fp.flush()
        except Exception:
            pass

    def _rsleep(self, lo, hi):
        time.sleep(random.uniform(lo, hi))

    # ──────────────────────────────────────────────
    #  Chrome Profile 复制
    # ──────────────────────────────────────────────

    def _copy_profile(self, dest_dir):
        self._log("  正在复制 Chrome 关键文件（智能模式）…")
        src_default = os.path.join(CHROME_USER_DATA, "Default")
        dst_default = os.path.join(dest_dir, "Default")
        os.makedirs(dst_default, exist_ok=True)
        copied = 0

        for f in ["Local State", "First Run"]:
            src = os.path.join(CHROME_USER_DATA, f)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(dest_dir, f))
                    copied += 1
                except PermissionError:
                    pass

        essential_files = [
            "Cookies", "Cookies-journal",
            "Preferences", "Secure Preferences",
            "Login Data", "Login Data For Account",
            "Web Data", "Favicons",
        ]
        for f in essential_files:
            src = os.path.join(src_default, f)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(dst_default, f))
                    copied += 1
                except PermissionError:
                    pass

        src_network = os.path.join(src_default, "Network")
        if os.path.exists(src_network):
            dst_network = os.path.join(dst_default, "Network")
            os.makedirs(dst_network, exist_ok=True)
            for f in ["Cookies", "Cookies-journal"]:
                src = os.path.join(src_network, f)
                if os.path.exists(src):
                    try:
                        shutil.copy2(src, os.path.join(dst_network, f))
                        copied += 1
                    except PermissionError:
                        pass

        for d in ["Local Storage", "Session Storage"]:
            src = os.path.join(src_default, d)
            if os.path.exists(src):
                try:
                    shutil.copytree(src, os.path.join(dst_default, d),
                                    dirs_exist_ok=True, symlinks=True)
                    copied += 1
                except Exception:
                    pass

        self._log(f"  Profile 复制完成（{copied} 个关键文件）")

    # ──────────────────────────────────────────────
    #  模拟人类行为 — 鼠标移动、滚动、点击
    # ──────────────────────────────────────────────

    def _human_move(self, page):
        try:
            x = random.randint(100, 1000)
            y = random.randint(100, 600)
            page.mouse.move(x, y, steps=random.randint(5, 15))
            self._rsleep(0.5, 1.5)
        except Exception:
            pass

    def _human_scroll(self, page, times=3):
        for i in range(times):
            amt = random.randint(300, 1200)
            try:
                page.evaluate(f"window.scrollBy(0, {amt})")
            except Exception:
                pass
            self._rsleep(2, 5)

    def _human_click(self, page, selector=None, x=None, y=None):
        """模拟人类鼠标点击：移动 → 停顿 → 点击"""
        try:
            if selector:
                el = page.query_selector(selector)
                if el:
                    box = el.bounding_box()
                    if box:
                        # 先移动到目标附近
                        target_x = box["x"] + box["width"] / 2
                        target_y = box["y"] + box["height"] / 2
                        # 加一点随机偏移模拟真人
                        offset_x = random.uniform(-5, 5)
                        offset_y = random.uniform(-5, 5)
                        page.mouse.move(target_x + offset_x, target_y + offset_y,
                                        steps=random.randint(8, 20))
                        self._rsleep(0.3, 0.8)
                        page.mouse.click(target_x + offset_x, target_y + offset_y)
                        self._rsleep(0.5, 1.5)
                        return True
            elif x is not None and y is not None:
                page.mouse.move(x, y, steps=random.randint(8, 20))
                self._rsleep(0.3, 0.8)
                page.mouse.click(x, y)
                self._rsleep(0.5, 1.5)
                return True
        except Exception:
            pass
        return False

    def _simulate_browsing(self, page):
        """模拟浏览行为：随机移动、滚动、偶尔点击"""
        self._log("    模拟浏览行为…")
        self._human_move(page)
        self._rsleep(1, 3)
        self._human_scroll(page, random.randint(2, 4))
        self._rsleep(1, 2)
        self._human_move(page)
        self._rsleep(1, 2)
        # 偶尔滚动回去
        if random.random() < 0.3:
            try:
                page.evaluate(f"window.scrollBy(0, -{random.randint(200, 500)})")
            except Exception:
                pass
            self._rsleep(1, 2)

    # ──────────────────────────────────────────────
    #  登录检测与等待
    # ──────────────────────────────────────────────

    def _check_login(self, page):
        try:
            result = page.evaluate("""
                () => {
                    let body = document.body ? document.body.innerText : '';
                    let hasLoginForm = body.includes('立即登录') || body.includes('验证码登录') || body.includes('账号密码登录');
                    let hasAvatar = document.querySelector('.user-avatar, .nav__avatar, [class*="avatar"] img, .nav__user__info') !== null;
                    let hasToken = document.cookie.includes('xq_a_token');
                    return {
                        hasLoginForm: hasLoginForm,
                        hasAvatar: hasAvatar,
                        hasToken: hasToken,
                        isLoggedIn: hasToken || (hasAvatar && !hasLoginForm),
                    };
                }
            """)
            return result
        except Exception as e:
            self._log(f"  登录检测异常: {e}")
            return {"isLoggedIn": False, "error": str(e)}

    def _ensure_login(self, page):
        self._log("  检查登录状态 …")
        status = self._check_login(page)

        if status.get("isLoggedIn"):
            self._log("  ✓ 已登录 (cookie token 验证通过)")
            return True

        self._log("  ⚠ 未检测到登录状态！")
        self._log("\n" + "=" * 60)
        self._log("  ✨ 请在弹出的 Chrome 窗口中登录雪球！")
        self._log(f"  程序将等待最多 {self.login_wait} 秒")
        self._log("  登录成功后程序会自动继续。")
        self._log("  登录态会保存在持久化 Profile 中，后续无需重复登录。")
        self._log("=" * 60 + "\n")

        try:
            page.goto("https://xueqiu.com/user/login", wait_until="domcontentloaded")
        except Exception:
            try:
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
            except Exception:
                pass

        check_interval = 5
        elapsed = 0
        while elapsed < self.login_wait:
            time.sleep(check_interval)
            elapsed += check_interval
            remaining = self.login_wait - elapsed
            try:
                status = self._check_login(page)
                if status.get("isLoggedIn"):
                    self._log(f"\n  ✓ 登录成功！剩余等待 {remaining} 秒时检测到登录态。\n")
                    page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
                    self._rsleep(3, 5)
                    return True
                else:
                    if elapsed % 30 == 0:
                        self._log(f"    仍在等待登录… 剩余 {remaining} 秒")
            except Exception:
                pass

        self._log("\n  ⚠ 登录等待超时，将尝试继续抓取（关注板块可能无法获取）\n")
        return False

    # ──────────────────────────────────────────────
    #  API 调用
    # ──────────────────────────────────────────────

    def _fetch_api(self, page, url):
        try:
            result = page.evaluate("""
                async (apiUrl) => {
                    try {
                        const resp = await fetch(apiUrl, {
                            headers: {"Accept": "application/json"},
                            credentials: "include",
                        });
                        const text = await resp.text();
                        try {
                            return JSON.parse(text);
                        } catch {
                            return {error: "json_parse_failed", status: resp.status, text: text.substring(0, 200)};
                        }
                    } catch(e) {
                        return {error: e.toString()};
                    }
                }
            """, url)
            if result and "error" in result and "list" not in result and "items" not in result:
                self._log(f"    API 错误: {result.get('error', '?')}")
                if "text" in result:
                    self._log(f"    响应预览: {result['text'][:100]}")
                return None
            return result
        except Exception as e:
            self._log(f"    fetch 异常: {e}")
            return None

    # ──────────────────────────────────────────────
    #  帖子解析
    # ──────────────────────────────────────────────

    def _extract_posts_from_response(self, data):
        if not isinstance(data, dict):
            return []
        raw_items = None
        for key in ("list", "statuses", "items", "data"):
            v = data.get(key)
            if isinstance(v, list):
                raw_items = v
                break
        if not raw_items:
            return []
        posts = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            post = self._parse_item(item)
            if post:
                posts.append(post)
        return posts

    def _parse_item(self, item):
        if item.get("id") and (item.get("description") or item.get("title")):
            return self._normalize_post(item)

        data_field = item.get("data")
        if isinstance(data_field, str):
            try:
                parsed = json.loads(data_field)
                if isinstance(parsed, dict) and parsed.get("id"):
                    orig = item.get("original_status")
                    if isinstance(orig, dict) and orig.get("id"):
                        parsed["_original_status"] = orig
                    return self._normalize_post(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        orig = item.get("original_status")
        if isinstance(orig, dict) and orig.get("id"):
            if orig.get("description") or orig.get("title"):
                return self._normalize_post(orig)

        if isinstance(data_field, dict) and data_field.get("id"):
            return self._normalize_post(data_field)

        return None

    def _normalize_post(self, post):
        user = post.get("user") or {}
        if not user.get("screen_name") and post.get("user_id"):
            user = {"id": post.get("user_id"), "screen_name": post.get("user_screen_name", "")}

        raw_desc = post.get("description") or ""
        raw_text = post.get("text") or raw_desc
        if not raw_text and raw_desc:
            raw_text = raw_desc

        pid = str(post.get("id", ""))
        sn = user.get("screen_name") or ""

        result = {
            "id":            pid,
            "title":         clean_html(post.get("title") or ""),
            "description":   clean_html(raw_desc),
            "text":          clean_html(raw_text),
            "created_at":    post.get("created_at", 0),
            "time_str":      self._ts_to_str(post.get("created_at", 0)),
            "reply_count":   post.get("reply_count", 0),
            "retweet_count": post.get("retweet_count", 0),
            "like_count":    post.get("like_count", 0),
            "view_count":    post.get("view_count", 0),
            "source":        post.get("source") or "",
            "type":          post.get("type") or "",
            "mark":          post.get("mark") or "",
            "user": {
                "id":              str(user.get("id", "")),
                "screen_name":     sn,
                "description":     user.get("description") or "",
                "followers_count": user.get("followers_count", 0),
                "friends_count":   user.get("friends_count", 0),
                "statuses_count":  user.get("statuses_count", 0),
            },
            "comments": [],
        }
        result["url"] = f"https://xueqiu.com/{sn}/{pid}" if sn else f"https://xueqiu.com/{pid}"

        rt = post.get("retweeted_status")
        if isinstance(rt, dict):
            rtu = rt.get("user") or {}
            result["retweeted"] = {
                "id":          str(rt.get("id", "")),
                "title":       clean_html(rt.get("title") or ""),
                "text":        clean_html(rt.get("description") or rt.get("text") or ""),
                "screen_name": rtu.get("screen_name") or "",
            }
        orig = post.get("_original_status")
        if isinstance(orig, dict) and orig.get("id") and not rt:
            ou = orig.get("user") or {}
            result["retweeted"] = {
                "id":          str(orig.get("id", "")),
                "title":       clean_html(orig.get("title") or ""),
                "text":        clean_html(orig.get("description") or orig.get("text") or ""),
                "screen_name": ou.get("screen_name") or "",
            }

        if post.get("target"):
            result["target"] = post["target"]
        return result

    # ──────────────────────────────────────────────
    #  三个板块的抓取
    # ──────────────────────────────────────────────

    def _scrape_section(self, page, section_key, section_name, fetch_fn):
        self._log(f"\n{'='*60}")
        self._log(f"正在抓取: {section_name}")
        self._log(f"{'='*60}")

        posts = fetch_fn(page)

        seen = {}
        for p in posts:
            if p["id"] not in seen:
                seen[p["id"]] = p
        posts = list(seen.values())
        posts = [p for p in posts if p.get("description") or p.get("title")]

        self.sections_data[section_key] = posts
        self._log(f"  ✓ {section_name}: {len(posts)} 条有效帖子")
        if posts:
            for p in posts[:3]:
                preview = (p["title"] or p["description"] or "")[:40]
                self._log(f"    [{p['id']}] {p['user']['screen_name']}: {preview}")
        return posts

    def _scrape_recommend(self, page):
        def fetch_fn(p):
            self._log("  调用 API: fundx/public/list.json")
            all_posts = []
            for pg in range(1, self.max_pages + 1):
                url = API_RECOMMEND.format(page=pg)
                self._log(f"    页 {pg}/{self.max_pages}")
                # 模拟浏览行为
                self._simulate_browsing(p)
                data = self._fetch_api(p, url)
                if not data:
                    break
                posts = self._extract_posts_from_response(data)
                self._log(f"    -> {len(posts)} 条帖子")
                all_posts.extend(posts)
                has_next = data.get("has_next_page", True)
                if not has_next:
                    self._log("    没有更多页面")
                    break
                # 慢速：页间间隔 5-10 秒
                self._rsleep(5, 10)
                self._human_move(p)
            return all_posts
        return self._scrape_section(page, "recommend", "推荐", fetch_fn)

    def _scrape_hot(self, page):
        def fetch_fn(p):
            self._log("  调用 API: hot/listV2.json")
            all_posts = []
            self._log(f"    请求 1/1")
            self._simulate_browsing(p)
            url = API_HOT
            data = self._fetch_api(p, url)
            if data:
                posts = self._extract_posts_from_response(data)
                self._log(f"    -> {len(posts)} 条帖子")
                all_posts.extend(posts)
            return all_posts
        return self._scrape_section(page, "hot", "热门", fetch_fn)

    def _scrape_following(self, page):
        def fetch_fn(p):
            all_posts = []

            # 方法 1: 导航到 /follow 并拦截 API
            self._log("  方法 1: 导航到 /follow 并拦截 API…")
            captured_posts = []
            api_urls_seen = []

            def on_response(response):
                try:
                    if response.status != 200:
                        return
                    u = response.url
                    if "xueqiu.com" not in u:
                        return
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct and ".json" not in u:
                        return
                    skip = ["config", "analytics", "upload", "csrf", "behavior",
                            "security", "quote.json", "hot_event", "hot_stock",
                            "searchFund", "qrcode", "taichi", "minute.json",
                            "fundx/public/list"]
                    if any(s in u for s in skip):
                        return
                    api_urls_seen.append(u[:150])
                    data = response.json()
                    posts = self._extract_posts_from_response(data)
                    if posts:
                        captured_posts.extend(posts)
                        self._log(f"    [拦截] {u[:80]} -> {len(posts)} 帖")
                except Exception:
                    pass

            p.on("response", on_response)
            try:
                p.goto("https://xueqiu.com/follow", wait_until="domcontentloaded")
                self._rsleep(5, 8)
                self._log(f"  当前 URL: {p.url}")
                self._simulate_browsing(p)
                self._human_scroll(p, 4)
                self._rsleep(2, 4)
            except Exception as e:
                self._log(f"  /follow 页面出错: {e}")
            finally:
                try:
                    p.remove_listener("response", on_response)
                except Exception:
                    pass

            if api_urls_seen:
                self._log(f"  拦截到 {len(api_urls_seen)} 个 API 请求:")
                for u in api_urls_seen[:5]:
                    self._log(f"    -> {u}")

            if captured_posts:
                all_posts.extend(captured_posts)

            # 方法 2: 直接 API
            if not all_posts:
                self._log("  方法 2: 直接调用关注 API…")
                follow_apis = [
                    "/v4/statuses/follow_timeline.json?page=1",
                    "/statuses/follow_timeline.json?page=1",
                    "/v4/statuses/follow_timeline_by_category.json?source=all&page=1",
                ]
                for ep in follow_apis:
                    url = "https://xueqiu.com" + ep
                    self._log(f"    尝试: {ep[:60]}")
                    data = self._fetch_api(p, url)
                    if data:
                        posts = self._extract_posts_from_response(data)
                        self._log(f"    -> {len(posts)} 条帖子")
                        if posts:
                            all_posts.extend(posts)
                            break
                    self._rsleep(2, 4)

            # 方法 3: 首页 tab 点击
            if not all_posts:
                self._log("  方法 3: 在首页查找并点击'关注'标签…")
                try:
                    p.goto("https://xueqiu.com/", wait_until="domcontentloaded")
                    self._rsleep(3, 5)

                    tab_captured = []
                    def on_resp2(resp):
                        try:
                            if resp.status != 200:
                                return
                            u = resp.url
                            if "xueqiu.com" not in u:
                                return
                            ct = resp.headers.get("content-type", "")
                            if "json" not in ct and ".json" not in u:
                                return
                            skip = ["config", "analytics", "upload", "csrf", "behavior",
                                    "security", "quote.json", "hot_event", "hot_stock",
                                    "searchFund", "qrcode", "taichi", "minute.json",
                                    "fundx/public/list"]
                            if any(s in u for s in skip):
                                return
                            data = resp.json()
                            posts = self._extract_posts_from_response(data)
                            if posts:
                                tab_captured.extend(posts)
                                self._log(f"    [拦截] {u[:80]} -> {len(posts)} 帖")
                        except Exception:
                            pass

                    p.on("response", on_resp2)

                    # 模拟点击"关注"标签
                    clicked = p.evaluate("""
                        () => {
                            let allEls = document.querySelectorAll('*');
                            for (let el of allEls) {
                                if (el.textContent.trim() === '关注') {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    if clicked:
                        self._log("  点击了'关注'标签")
                    self._rsleep(4, 6)
                    self._human_scroll(p, 3)
                    p.remove_listener("response", on_resp2)

                    if tab_captured:
                        all_posts.extend(tab_captured)
                except Exception as e:
                    self._log(f"  首页查找出错: {e}")

            if not all_posts:
                self._log("  ⚠ 关注板块未获取到帖子（可能需要登录或无关注内容）")

            return all_posts
        return self._scrape_section(page, "following", "关注", fetch_fn)

    # ──────────────────────────────────────────────
    #  评论抓取
    # ──────────────────────────────────────────────

    def _scrape_all_comments(self, page):
        self._log(f"\n{'='*60}")
        self._log("正在抓取评论（慢速模式）…")
        self._log(f"{'='*60}")

        total = sum(min(len(p), self.max_comment_posts)
                    for p in self.sections_data.values())
        done = 0

        for section_key, posts in self.sections_data.items():
            n = min(len(posts), self.max_comment_posts)
            for i in range(n):
                if not self._running:
                    break
                post = posts[i]
                done += 1
                pid = post["id"]
                if not pid:
                    continue
                comments = self._fetch_comments(page, pid)
                post["comments"] = comments
                preview = (post["title"] or post["description"] or "")[:30]
                self._log(f"  [{done}/{total}] {section_key}  {pid}  "
                          f"{len(comments)} 评论  «{preview}»")
                # 慢速：评论间间隔 3-6 秒
                self._rsleep(3, 6)

    def _fetch_comments(self, page, post_id):
        comments = []
        for pg in range(1, self.max_comment_pages + 1):
            url = API_COMMENTS.format(post_id=post_id, page=pg)
            result = self._fetch_api(page, url)
            if not result:
                break
            if not result.get("comments"):
                break
            for c in result["comments"]:
                cu = c.get("user") or {}
                comments.append({
                    "id":          str(c.get("id", "")),
                    "text":        clean_html(c.get("text") or ""),
                    "created_at":  c.get("created_at", 0),
                    "time_str":    self._ts_to_str(c.get("created_at", 0)),
                    "like_count":  c.get("like_count", 0),
                    "user": {
                        "id":          str(cu.get("id", "")),
                        "screen_name": cu.get("screen_name") or "",
                    },
                })
            max_page = result.get("maxPage", 1)
            if pg >= max_page:
                break
            # 慢速：评论页间间隔 2-4 秒
            self._rsleep(2, 4)
        return comments

    # ──────────────────────────────────────────────
    #  Chrome 检测
    # ──────────────────────────────────────────────

    @staticmethod
    def _find_chrome_exe():
        candidates = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    @staticmethod
    def _is_chrome_running():
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                capture_output=True, text=True, timeout=10
            )
            return "chrome.exe" in result.stdout.lower()
        except Exception:
            return False

    # ──────────────────────────────────────────────
    #  持久化 Profile
    # ──────────────────────────────────────────────

    def _connect_via_copy(self, playwright):
        persistent_profile = os.path.join(DATA_DIR, "chrome_profile")

        # 检查持久化 Profile 是否已有 cookie（Chrome 115+ 存放在 Network/ 子目录）
        has_cookies = (
            os.path.exists(os.path.join(persistent_profile, "Default", "Cookies")) or
            os.path.exists(os.path.join(persistent_profile, "Default", "Network", "Cookies"))
        )
        if not os.path.exists(persistent_profile) or not has_cookies:
            self._log("  首次运行，正在复制 Chrome 关键文件…")
            os.makedirs(persistent_profile, exist_ok=True)
            self._copy_profile(persistent_profile)
        else:
            self._log("  已有持久化 Profile，直接复用。")

        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=persistent_profile,
                channel="chrome",
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--window-size=1366,768",
                    "--lang=zh-CN",
                ],
            )
            context.add_init_script(STEALTH_JS)
            return context
        except Exception as e:
            self._log(f"Profile 启动失败: {e}")
            return None

    # ──────────────────────────────────────────────
    #  单轮抓取
    # ──────────────────────────────────────────────

    def _do_one_scrape(self, page):
        """执行一轮完整抓取，结果存入 SQLite"""
        run_id = self.db.start_run()
        self._log(f"\n{'#'*60}")
        self._log(f"#  第 {run_id} 轮抓取开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._log(f"{'#'*60}")

        # 重置 sections_data
        self.sections_data = {}

        # 访问首页
        self._log("正在访问雪球首页 …")
        try:
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
        except Exception:
            pass
        self._rsleep(5, 8)
        self._human_move(page)
        try:
            page.evaluate("window.scrollBy(0, 300)")
        except Exception:
            pass
        self._rsleep(2, 4)

        # 抓取三个板块
        self._scrape_recommend(page)
        self._rsleep(5, 10)
        self._simulate_browsing(page)

        self._scrape_following(page)
        self._rsleep(5, 10)
        self._simulate_browsing(page)

        self._scrape_hot(page)
        self._rsleep(5, 10)

        # 回到首页
        try:
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
        except Exception:
            pass
        self._rsleep(3, 5)

        # 抓取评论
        self._scrape_all_comments(page)

        # 存入 SQLite
        new_posts = 0
        new_comments = 0
        counts = {}
        for section_key, posts in self.sections_data.items():
            counts[section_key] = len(posts)
            comment_count = 0
            for post in posts:
                is_new = self.db.upsert_post(post, section_key)
                if is_new:
                    new_posts += 1
                for comment in post.get("comments", []):
                    is_new_c = self.db.upsert_comment(comment, post["id"])
                    if is_new_c:
                        new_comments += 1
                    comment_count += 1
            counts["comments"] = counts.get("comments", 0) + comment_count

        counts["new_posts"] = new_posts
        counts["new_comments"] = new_comments
        self.db.end_run(run_id, counts)

        stats = self.db.get_stats()
        self._log(f"\n{'='*60}")
        self._log(f"第 {run_id} 轮抓取完成！")
        self._log(f"{'='*60}")
        self._log(f"  本轮帖子: {sum(len(p) for p in self.sections_data.values())}")
        self._log(f"  本轮评论: {counts.get('comments', 0)}")
        self._log(f"  新帖子:   {new_posts}")
        self._log(f"  新评论:   {new_comments}")
        self._log(f"  ────────────────────────")
        self._log(f"  累计帖子: {stats['total_posts']}")
        self._log(f"  累计评论: {stats['total_comments']}")
        self._log(f"  累计轮次: {stats['runs']}")
        self._log(f"    推荐: {stats['recommend']}")
        self._log(f"    关注: {stats['following']}")
        self._log(f"    热门: {stats['hot']}")

        return run_id

    def _export_json(self):
        """从 SQLite 导出 JSON 文件（最近 48 小时数据，控制在 1MB 以内）"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = os.path.join(JSON_EXPORT_DIR, f"xueqiu_export_{ts}.json")
        result = self.db.export_to_json(export_path, hours_back=48)
        self._log(f"\n{'='*60}")
        self._log(f"JSON 导出完成！")
        self._log(f"{'='*60}")
        self._log(f"  文件: {export_path}")
        self._log(f"  大小: {result.get('_file_size_kb', 0)} KB")
        self._log(f"  时间窗口: 最近 {result.get('window_hours', 48)} 小时")
        self._log(f"  导出帖子: {result.get('exported_posts', 0)}")
        self._log(f"  导出评论: {result.get('exported_comments', 0)}")
        self._log(f"  数据库累计: {result.get('db_total_posts', 0)} 帖 / {result.get('db_total_comments', 0)} 评")
        return export_path

    # ──────────────────────────────────────────────
    #  主流程 — 持续运行
    # ──────────────────────────────────────────────

    def run(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(JSON_EXPORT_DIR, exist_ok=True)

        with sync_playwright() as p:
            # 确保 Chrome 已关闭
            if self._is_chrome_running():
                self._log("  检测到残留 Chrome 进程，正在清理…")
                subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                               capture_output=True, timeout=15)
                time.sleep(3)

            self._log("--- 启动 Chrome（智能复制 Profile）---")
            context = self._connect_via_copy(p)

            if context is None:
                self._log("无法启动浏览器，程序终止。")
                self._log_fp.close()
                sys.exit(1)

            self._context = context
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(60000)
            self._page = page

            # 访问首页 + 登录检测
            self._log("正在访问雪球首页 …")
            try:
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
            except Exception:
                pass
            self._rsleep(5, 8)
            self._human_move(page)
            self._rsleep(2, 4)

            # 登录检测（仅第一次需要）
            self._ensure_login(page)

            # ── 持续运行循环 ──
            last_export_time = time.time()
            scrape_round = 0

            while self._running:
                scrape_round += 1
                self._log(f"\n{'*'*60}")
                self._log(f"*  持续运行 — 第 {scrape_round} 轮")
                self._log(f"*  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                self._log(f"{'*'*60}")

                # 执行一轮抓取
                try:
                    self._do_one_scrape(page)
                except Exception as e:
                    self._log(f"  抓取出错: {e}")
                    # 检查浏览器是否还活着
                    try:
                        page.evaluate("1+1")
                    except Exception:
                        self._log("  浏览器可能已关闭，尝试重新连接…")
                        try:
                            page = context.pages[0] if context.pages else context.new_page()
                            page.set_default_timeout(60000)
                            self._page = page
                            page.goto("https://xueqiu.com/", wait_until="domcontentloaded")
                            self._rsleep(3, 5)
                        except Exception:
                            self._log("  无法恢复浏览器连接，退出循环。")
                            break

                # 检查是否到了 JSON 导出时间
                now = time.time()
                elapsed_since_export = now - last_export_time
                if elapsed_since_export >= JSON_EXPORT_INTERVAL:
                    self._log(f"\n  达到 {JSON_EXPORT_INTERVAL//3600} 小时，导出 JSON…")
                    try:
                        self._export_json()
                        last_export_time = now
                    except Exception as e:
                        self._log(f"  JSON 导出出错: {e}")

                # 等待下一轮
                if not self._running:
                    break

                wait_sec = random.randint(SCRAPE_INTERVAL_MIN, SCRAPE_INTERVAL_MAX)
                next_time = datetime.now() + timedelta(seconds=wait_sec)
                next_time_str = next_time.strftime("%Y-%m-%d %H:%M:%S")
                self._log(f"\n  本轮抓取已完成，下次执行时间: {next_time_str}")
                self._log(f"  （约 {wait_sec // 60} 分钟后，按 Ctrl+C 可退出程序）")

                # 分段等待，便于响应 Ctrl+C
                waited = 0
                while waited < wait_sec and self._running:
                    sleep_chunk = min(10, wait_sec - waited)
                    time.sleep(sleep_chunk)
                    waited += sleep_chunk

            # ── 退出前的清理 ──
            self._log("\n  程序正在退出…")

            # 最后导出一次 JSON
            self._log("  最终导出 JSON…")
            try:
                self._export_json()
            except Exception as e:
                self._log(f"  最终导出出错: {e}")

            # 关闭浏览器
            try:
                context.close()
            except Exception:
                pass

            # 关闭数据库
            self.db.close()

            self._log(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 程序已退出。")
            self._log(f"日志已保存: {self.log_file}")
            try:
                self._log_fp.close()
            except Exception:
                pass

    @staticmethod
    def _ts_to_str(ts):
        if not ts:
            return ""
        try:
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""


if __name__ == "__main__":
    scraper = XueqiuScraper(
        max_pages=3,
        max_comment_pages=2,
        max_comment_posts=15,
        login_wait=300,
    )
    scraper.run()

    # EXE 打包后，窗口不会自动关闭
    if getattr(sys, 'frozen', False):
        print("\n" + "="*60)
        print("程序已退出。按 Enter 键关闭窗口...")
        input()
