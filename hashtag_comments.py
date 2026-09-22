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
import subprocess
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


def _profile_has_login(p):
    """判断某个 Chrome profile 目录是否存在且已登录（含 Cookies）。"""
    if not os.path.isdir(p):
        return False
    default = os.path.join(p, "Default")
    if not os.path.isdir(default):
        return False
    return (os.path.exists(os.path.join(default, "Cookies")) or
            os.path.exists(os.path.join(default, "Network", "Cookies")))


def resolve_profile_dir():
    """返回 Chrome 持久化 profile 目录。

    - 默认：DATA_DIR/chrome_profile
    - 若为 EXE(frozen) 且自身目录无登录态，则向上查找项目目录 / 用户级固定目录里的
      已登录 profile，让 EXE 自动复用 python 运行时的登录态。
      否则 EXE 会以未登录的全新 profile 打开雪球，导致右侧「热门话题」列表不渲染、
      自动发现失败（no hashtag links）。
    """
    default = os.path.join(DATA_DIR, "chrome_profile")
    if getattr(sys, "frozen", False):
        candidates = [
            default,
            os.path.join(BASE_DIR, "..", "..", "data", "chrome_profile"),
            os.path.join(os.path.expanduser("~"), ".xueqiu_spider", "chrome_profile"),
        ]
        for c in candidates:
            c = os.path.abspath(c)
            if _profile_has_login(c):
                return c
    return default


def _kill_stale_chrome(profile_dir, log=None):
    """关闭任何仍占用本爬虫专用 Profile 的 Chrome 进程，避免每次运行堆积新窗口。

    只匹配命令行含本 profile 目录的 chrome 进程，绝不动用户的默认浏览器。
    """
    if not profile_dir:
        return 0
    pd_bs = os.path.abspath(profile_dir).replace("/", "\\")
    pd_fs = pd_bs.replace("\\", "/")
    try:
        if sys.platform.startswith("win"):
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -and ("
                "$_.CommandLine -like '*%s*' -or $_.CommandLine -like '*%s*') } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
                % (pd_bs, pd_fs)
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=30,
            )
        else:
            subprocess.run(["pkill", "-f", pd_bs], capture_output=True, text=True, timeout=30)
        if log:
            log(f"    已清理残留 Chrome 进程（仅限本 profile，若有的话）")
        time.sleep(1.5)  # 等待 SingletonLock 释放
        return 1
    except Exception as e:
        if log:
            log(f"    清理残留 Chrome 异常(可忽略): {e}")
        return 0


def _is_waf_page_text(text):
    """判断页面文本是否为雪球 WAF/风控挑战页（而非正常内容）。"""
    if not text:
        return False
    t = text.lower()
    return ("_waf" in t or "renderdata" in t or "人机验证" in text
            or "请求过于频繁" in text or "访问验证" in text or "security challenge" in t
            or "请完成安全验证" in text)


def _nav_get_json(page, url, max_retry=3, log=None):
    """真实浏览器导航拉取 JSON 接口（不用页内 fetch，避免被 WAF 抓特征）。

    返回解析后的 dict；命中风控挑战页或解析失败时返回 None。
    与 scraper._fetch_api 路径2 同思路：page.goto 是真实浏览器导航，由 Chromium
    自带完整请求头 + 持久化 cookie，最难被风控标记为机器人。
    """
    for attempt in range(1, max_retry + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            if log:
                log(f"    导航异常(尝试{attempt}): {e}")
        # 轮询等待挑战 JS 执行/重定向完成（最多 ~15s）
        deadline = time.time() + 15
        text = ""
        while time.time() < deadline:
            try:
                text = page.evaluate(
                    "() => {"
                    "  const pre = document.querySelector('pre');"
                    "  if (pre) return (pre.innerText || pre.textContent || '');"
                    "  return (document.body ? document.body.innerText : '') "
                    "    || (document.documentElement ? document.documentElement.innerText : '');"
                    "}"
                ) or ""
            except Exception:
                text = ""
            if not _is_waf_page_text(text):
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break
        if _is_waf_page_text(text):
            if log:
                log(f"    ⚠ 命中雪球风控挑战页(尝试{attempt})，等待 {8 + attempt*2}s 后重试…")
            try:
                page.wait_for_timeout((8 + attempt * 2) * 1000)
            except Exception:
                pass
            continue
        try:
            return json.loads(text)
        except Exception:
            if _is_waf_page_text(text):
                continue
            if log:
                log(f"    导航响应非 JSON: {text[:120]}")
            return None
    return None


def _cleanup_restored_tabs(context):
    """关闭持久化 profile 启动时 Chrome 自动恢复的旧标签页，只保留一个干净页面。

    雪球的持久化 profile 每次启动都会恢复上次未关闭的窗口/标签页；若不处理，
    每轮重复启动后标签页越积越多、内存持续膨胀。这里在启动后只留一个页面，
    抓取时只在该页面内导航/刷新，不再开新标签。
    """
    try:
        pages = list(context.pages)
        if len(pages) > 1:
            for p in pages[1:]:
                try:
                    p.close()
                except Exception:
                    pass
        if not context.pages:
            context.new_page()
    except Exception:
        pass



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
MAX_COMMENT_PAGES = 25     # 单帖评论最多翻页数（上调以收集更多评论；注意请求量/WAF 风险）
POST_DELAY = (3, 6)        # 帖子间随机停顿（秒）
COMMENT_PAGE_DELAY = (1, 3)
HEADLESS = True            # 无头模式（可后台运行）；需看登录过程改为 False

# ── 热点发现（非登录）──
# 雪球「雪球热点」榜单【只在非登录状态】才展示于 ?category=hotspot；
# 登录态下该页会变成个性化信息流，看不到热点榜。
# 故本引擎全程用【非登录】上下文，与「推荐/关注」的登录态引擎（scraper.py）严格区分（"以区分"）。
HOTSPOT_URL = "https://www.xueqiu.com/?category=hotspot"
# 每轮抓取的热点话题数（取榜单前 N）。
# 注：雪球热点榜页面（匿名态）稳定只渲染约 10 个话题，懒加载不会追加，
# 故这里设为 10 即吃满页面能给的全部候选；如需降负载可调小。
HOTSPOT_TOP_N = 10           # 每轮抓取的热点话题数（取榜单前 N，页面上限约 10）
MAX_POSTS_PER_TOPIC = 20    # 单个热点话题最多抓取的帖子数（上调以收集更多评论；注意单轮时长）
# 非登录桌面 UA：让话题详情页沿用 article.timeline__item + a[data-id] 结构（已有提取逻辑）
GUEST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

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
                reply_count INTEGER DEFAULT 0,
                first_seen TEXT,
                last_updated TEXT
            )
        """)
        # 迁移：老库 comments 表可能无 reply_count 列，补齐（评论收到的回复数，用于「有交互」打分）
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(comments)")]
            if "reply_count" not in cols:
                c.execute("ALTER TABLE comments ADD COLUMN reply_count INTEGER DEFAULT 0")
        except Exception:
            pass
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
               like_count,created_at,time_str,reply_count,first_seen,last_updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 text=excluded.text, like_count=excluded.like_count,
                 reply_count=excluded.reply_count, last_updated=excluded.last_updated""",
            (c["id"], c["post_id"], c["post_author"], c.get("hashtag", HASHTAG_NAME), c["text"],
             c["user_id"], c["user_name"], c["like_count"], c["created_at"],
             c["time_str"], c.get("reply_count", 0) or 0, now, now))
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
        # 常驻浏览器会话（run_forever / --mode hashtag 只开一次，避免每轮重复启动 Chrome 导致标签页堆积）
        self._pw = None
        self._browser = None
        self._owns_login = True  # --mode all 借用推荐引擎 context 时为 False
        self._login_ctx = None
        self._login_page = None
        self._guest_ctx = None
        self._guest_page = None

    def _extract_post_ids(self, page):
        return page.evaluate("""
            () => {
              const ids = new Map();
              // 桌面/移动版共用 timeline__item；移动版若改了外层 class 则退而求其次
              // 直接在所有 article 里找带 data-id 的链接（雪球帖子链接固定带 data-id）。
              const items = document.querySelectorAll('article.timeline__item');
              const articles = items.length ? items
                : document.querySelectorAll('article');
              articles.forEach(a => {
                const link = a.querySelector('a[data-id]');
                const authorEl = a.querySelector('.user-name, .timeline__user, [class*="user"] .name');
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
        """拉取某帖评论（多页）。改用真实浏览器导航，不再用页内 fetch。

        每页一次 page.goto（真实导航，难被风控标记），读取 <pre>/innerText 里的
        JSON 解析累加；空页或返回非 JSON 即停止。
        """
        all_comments = []
        for p in range(1, self.max_comment_pages + 1):
            url = (f"https://xueqiu.com/statuses/comments.json"
                   f"?id={post_id}&page={p}&count=20")
            j = _nav_get_json(page, url, max_retry=2, log=_log)
            if not j:
                break
            list_ = j.get("comments") or j.get("list") or []
            if not list_:
                break
            all_comments.extend(list_)
            if len(list_) < 20:
                break
        return all_comments

    def _discover_hotspots(self, page, top_n=HOTSPOT_TOP_N):
        """非登录状态访问热点榜，取前 top_n 个热点话题，返回 [(url, title), ...]。

        关键：雪球「雪球热点」榜单【只在非登录】时才展示于 ?category=hotspot；
        登录态下该页会变成个性化信息流，看不到热点榜。故调用方必须用非登录上下文。
        命中话题链接形如 /hashtag/I-...（话题详情页，沿用 article.timeline__item 结构）。

        鲁棒性：该页常遇 WAF 挑战页（真实导航后挑战 JS 写 cookie 再重定向），
        故最多重试 3 次，每次检测 _waf / renderData / 人机验证 等特征，命中则
        等待挑战 JS 执行完再重导航。
        """
        _log(f"  阶段1 发现热点：以【匿名/非登录】状态访问 {HOTSPOT_URL}（热点榜仅匿名可见，不携带登录 Cookie）")
        for attempt in range(3):
            try:
                page.goto(HOTSPOT_URL, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                _log(f"  打开热点榜失败(尝试{attempt+1}/3): {e}")
                page.wait_for_timeout(2000)
                continue
            # 等待列表渲染 + 滚动触发懒加载
            page.wait_for_timeout(3000)
            for _ in range(3):
                try:
                    page.mouse.wheel(0, 700)
                    page.wait_for_timeout(800)
                except Exception:
                    break
            try:
                txt = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                txt = ""
            if _is_waf_page_text(txt):
                _log(f"  ⚠ 热点榜命中 WAF 挑战页(尝试{attempt+1}/3)，等 6s 重试…")
                page.wait_for_timeout(6000)
                continue
            items = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll("a[href^='/hashtag/']").forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && text && !out.some(o => o.url === href))
                        out.push({url: href, title: text.slice(0, 50)});
                });
                return out;
            }""")
            total_found = len(items)
            out = []
            for it in items[:top_n]:
                url = it["url"]
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("/"):
                    url = "https://xueqiu.com" + url
                out.append((url, it["title"]))
            if out:
                _log(f"  ✅ 匿名访问成功：页面共解析到 {total_found} 个热点话题链接，本轮将抓取前 {len(out)} 个:")
                for i, (_, t) in enumerate(out, 1):
                    _log(f"     {i}. {t}")
                return out
            _log(f"  ⚠ 热点榜未解析到话题链接(尝试{attempt+1}/3)，可能命中 WAF 或页面改版，重试…")
            page.wait_for_timeout(2000)
        _log("  ⚠ 匿名发现热点失败：3 次尝试均未解析到话题链接。请检查网络/WAF，或关闭 AUTO_DISCOVER 手动指定 HASHTAG_URL。")
        return []

    def start_session(self, pw, login_ctx=None, login_page=None):
        """启动并复用单一浏览器会话（整个持续运行/--mode hashtag 生命周期内只开一次）。

        设计（回应"chrome 每轮重复打开、标签页越积越多"的诉求）：
        - 登录态抓评论（comments.json 需登录）；
        - 同一进程上另开一个【非登录】guest context（不携带持久化 cookie）用于热点榜发现
          （?category=hotspot 的热点榜仅非登录可见），满足"以区分"且不再额外开浏览器；
        - 两个 context 各自只保一个页面，抓取时只在该页面内导航/刷新，绝不开新标签、绝不复开。

        login_ctx/login_page 可选：由 --mode all 的统一调度器传入【推荐引擎已启动的同一
        持久化登录 context】，此时本引擎不再另开浏览器，直接复用该 context 抓评论 +
        在其浏览器上开 guest context 做非登录发现——实现【单一浏览器跑两个引擎】。
        返回 (login_ctx, login_page, guest_page)；缓存在 self 上供 run_forever 复用。
        """
        if self._browser is not None:
            return (self._login_ctx, self._login_page, self._guest_page)

        if login_ctx is not None and login_page is not None:
            # 借用推荐引擎的登录 context（--mode all 单一浏览器）
            self._owns_login = False
            self._login_ctx = login_ctx
            self._login_page = login_page
            browser = login_ctx.browser
        else:
            # 自行启动登录持久化 context
            self._owns_login = True
            profile_dir = resolve_profile_dir()
            _kill_stale_chrome(profile_dir, log=_log)
            login_ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir, channel="chrome", headless=self.headless,
                accept_downloads=False, user_agent=GUEST_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            _cleanup_restored_tabs(login_ctx)  # 关掉 Chrome 自动恢复的旧标签页，只留一个干净页面
            self._login_ctx = login_ctx
            self._login_page = login_ctx.pages[0] if login_ctx.pages else login_ctx.new_page()
            browser = login_ctx.browser

        self._browser = browser
        # 非登录 guest context：同一浏览器进程上的独立 context，不读持久化 cookie
        self._guest_ctx = browser.new_context(
            user_agent=GUEST_USER_AGENT, viewport={"width": 1280, "height": 900})
        try:
            self._guest_ctx.clear_cookies()
        except Exception:
            pass
        self._guest_page = self._guest_ctx.new_page()
        _log("[热点引擎] 浏览器会话已就绪：单一常驻进程（登录态抓评论 + 非登录发现热点）")
        return (self._login_ctx, self._login_page, self._guest_page)

    def close_session(self):
        """关闭常驻浏览器会话（Ctrl+C 退出或单次运行结束时调用）。

        若 login context 是 --mode all 下向推荐引擎借用的（_owns_login=False），
        则只关自己的 guest context，不动共享的登录 context（由推荐引擎负责关闭）。
        """
        try:
            if self._guest_ctx is not None:
                self._guest_ctx.close()
        except Exception:
            pass
        try:
            if self._owns_login and self._login_ctx is not None:
                self._login_ctx.close()
        except Exception:
            pass
        self._browser = None
        self._login_ctx = None
        self._login_page = None
        self._guest_ctx = None
        self._guest_page = None
        self._owns_login = True

    @staticmethod
    def _page_alive(page):
        """页面/浏览器是否仍可用（未被关闭、未崩溃）。"""
        try:
            if page is None or page.is_closed():
                return False
            page.evaluate("1+1")
            return True
        except Exception:
            return False

    def ensure_guest_context(self, force=False):
        """确保【非登录】guest context/page 可用；失效或 force 时在同一浏览器上重建。

        非登录发现热点榜的前提是 guest context 不携带持久化登录 cookie。
        浏览器（self._login_ctx.browser）若被调度器重连换过，这里会跟随到新浏览器。
        返回 True/False。
        """
        if not force and self._page_alive(self._guest_page):
            return True
        try:
            if self._guest_ctx is not None:
                try:
                    self._guest_ctx.close()
                except Exception:
                    pass
                self._guest_ctx = None
                self._guest_page = None
            ctx = self._login_ctx
            if ctx is None:
                return False
            try:
                self._browser = ctx.browser or self._browser
            except Exception:
                pass
            if self._browser is None:
                return False
            self._guest_ctx = self._browser.new_context(
                user_agent=GUEST_USER_AGENT, viewport={"width": 1280, "height": 900})
            try:
                self._guest_ctx.clear_cookies()
            except Exception:
                pass
            self._guest_page = self._guest_ctx.new_page()
            return True
        except Exception as e:
            _log(f"  [!] 重建非登录 guest 上下文失败: {e}")
            self._guest_ctx = None
            self._guest_page = None
            return False

    def ensure_session_alive(self, pw):
        """话题单跑模式(--mode hashtag)自愈：登录会话失效则整体重连，并确保 guest 可用。"""
        if not self._page_alive(self._login_page):
            _log("  [!] 热点引擎浏览器已失效（被关闭/崩溃），正在自动重连…")
            try:
                self.close_session()
                self.start_session(pw)
            except Exception as e:
                _log(f"  [!] 热点引擎重连失败: {e}")
                return False
        return self.ensure_guest_context()

    def _scrape_round(self, login_page, guest_page):
        """执行一轮热点抓取（发现 + 逐话题抓评论），不负责浏览器的开关。

        两阶段上下文严格区分（"以区分"）：发现用非登录 guest_page，抓取用登录 login_page。
        若页面/浏览器已被外部关闭（崩溃、被其它进程清理），先尝试自愈重建再继续，
        避免整轮以 "Target page, context or browser has been closed" 失败。
        """
        # ── 阶段0：会话健康检查（自愈）──
        if not self._page_alive(guest_page):
            _log("  [!] 非登录发现页面已失效（浏览器可能被关闭/崩溃），尝试自动重建…")
            if self.ensure_guest_context(force=True):
                guest_page = self._guest_page
                _log("  [ok] 非登录发现页面已重建")
            else:
                _log("  [!] 非登录发现页面重建失败，本轮跳过（下一轮会自动重试）")
                return 0
        if not self._page_alive(login_page):
            if self._page_alive(self._login_page):
                login_page = self._login_page
            else:
                _log("  [!] 登录页面已失效，本轮跳过评论抓取"
                     "（--mode all 下由调度器重连浏览器后自动恢复）")
                return 0

        # ── 阶段1：非登录发现热点榜 ──
        if self.auto_discover:
            _log("[热点引擎] 阶段1 发现热点：使用【非登录】上下文（热点榜仅非登录可见）")
            try:
                topics = self._discover_hotspots(guest_page)
            except Exception as e:
                _log(f"  发现热点异常，本轮跳过: {e}")
                topics = []
            if not topics:
                _log("  未从热点榜发现任何话题，本轮跳过")
                return 0
        else:
            topics = [(self.url, self.name)]

        # ── 阶段2：登录态抓取每个热点的帖子+评论（评论接口需登录）──
        _log("[热点引擎] 阶段2 抓取评论：使用【登录】持久化 profile（评论接口需登录态）")
        total_new = 0
        n = min(len(topics), HOTSPOT_TOP_N)
        for idx, (url, title) in enumerate(topics[:HOTSPOT_TOP_N], 1):
            _log(f"\n{'='*16} 热点 {idx}/{n}: {title} {'='*16}")
            total_new += self._scrape_topic(login_page, url, title)
        _log(f"\n本轮热点抓取完成，共入库评论 {total_new} 条")
        return total_new

    def scrape_once(self, session=None):
        """一轮热点抓取。

        session=(login_ctx, login_page, guest_page) 由 run_forever / xueqiu.py 复用
        （单一浏览器常驻，只刷新页面不复开，节约内存）；为 None 时（单次运行 / --mode all
        每轮）自行启动并关闭浏览器（同样带标签页清理，避免旧标签堆积）。
        """
        if session is not None:
            return self._scrape_round(*session)
        with sync_playwright() as pw:
            login_ctx, login_page, guest_page = self.start_session(pw)
            try:
                return self._scrape_round(login_page, guest_page)
            finally:
                self.close_session()

    def _scrape_topic(self, page, url, title):
        """抓取单个热点话题页的帖子评论（登录上下文，评论接口需登录）。返回本轮入库评论数。"""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            _log(f"  [!] 打开话题页失败（可能已失效/触发下载）: {e}，跳过该话题")
            return 0
        page.wait_for_timeout(4000)
        # 滚动加载更多帖子
        for i in range(self.scroll_rounds):
            try:
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(random.uniform(1.5, 3.0))
            except Exception:
                break
        page.wait_for_timeout(2000)

        posts = self._extract_post_ids(page)
        # 限制单话题帖子数，控制单轮运行时长与请求量
        if MAX_POSTS_PER_TOPIC and len(posts) > MAX_POSTS_PER_TOPIC:
            posts = posts[:MAX_POSTS_PER_TOPIC]
        _log(f"提取到 {len(posts)} 个帖子")

        total_new = 0
        for idx, p in enumerate(posts, 1):
            pid = p["id"]
            author = p.get("author", "")
            self.db.save_post(pid, author, title)
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
                    "hashtag": title,
                    "user_id": str(user.get("id", "")),
                    "user_name": user.get("screen_name", ""),
                    "like_count": cm.get("like_count", 0) or 0,
                    "created_at": int(ca) // 1000 if ca > 10**12 else int(ca),
                    "time_str": cm.get("timeStr") or cm.get("timeBefore") or "",
                    "reply_count": cm.get("reply_count", 0) or 0,
                }
                self.db.save_comment(row)
                saved_this += 1
            total_new += saved_this
            _log(f"      评论 {len(raw)} 条, 入库 {saved_this} 条（累计 {self.db.count()}）")
            page.wait_for_timeout(random.uniform(*POST_DELAY))

        # 每话题单独生成价值线索（按话题独立短标识，避免串味）
        try:
            self._gen_insight(short=_slugify(title), name=title)
        except Exception as e:
            _log(f"  生成价值候选失败(可忽略): {e}")
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

    def _gen_insight(self, short=None, name=None):
        """生成 Layer1 价值候选（可选，需 insight_extractor.py）。

        可传 short/name 指定单个热点话题；缺省则回退到实例默认的 self.short/self.name。
        hashtag 传话题标题，确保 insight_extractor 只分析本话题评论（按话题独立，不串味）。
        """
        try:
            import insight_extractor
            insight_extractor.main(
                short=short or self.short, name=name or self.name, hashtag=name or self.name)
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
        _log("  单一浏览器常驻（登录态抓评论 + 非登录发现热点），只刷新页面不复开，节约内存")
        _log("  按 Ctrl+C 可退出程序")
        _log("=" * 60)
        round_no = 0
        with sync_playwright() as pw:
            self.start_session(pw)  # 整个运行期只开一次浏览器
            try:
                while self._running:
                    round_no += 1
                    _log(f"\n{'='*60}")
                    _log(f"  第 {round_no} 轮抓取  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    _log(f"{'='*60}")
                    try:
                        new_count = self._scrape_round(self._login_page, self._guest_page)
                    except Exception as e:
                        _log(f"  ⚠ 本轮抓取异常: {e}")
                        new_count = 0
                    self._export_round()
                    # 价值线索已按话题在 _scrape_topic 内逐个生成，无需再整体生成
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
                self.close_session()
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
