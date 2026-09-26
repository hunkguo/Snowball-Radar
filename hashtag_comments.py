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
from urllib.parse import urljoin, quote

from playwright.sync_api import sync_playwright

import stealth
from stealth import STEALTH_JS

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
    """判断页面文本是否为雪球 WAF/风控页。

    特征表统一维护在 stealth 模块（与推荐引擎共用，避免两处口径漂移），覆盖：
      ① 挑战页：`<textarea id="renderData">{"_waf_...">` / 人机验证 / 访问验证
      ② 403 拦截页：`Sorry, your request has been blocked as it may cause
         potential threats to the server's security.`
      ③ 频率提示：请求过于频繁 / 请完成安全验证
    """
    return stealth.is_challenge_text(text)


def _nav_get_json(page, url, max_retry=3, log=None, state=None):
    """真实浏览器导航拉取 JSON 接口（不用页内 fetch，避免被 WAF 抓特征）。

    返回解析后的 dict；命中风控页或解析失败时返回 None。
    与 scraper._fetch_api 路径2 同思路：page.goto 是真实浏览器导航，由 Chromium
    自带完整请求头 + 持久化 cookie，最难被风控标记为机器人。

    state：可选 dict，跨调用累计风控命中次数（state["waf"] 自增），调用方据此
    判断"是否已被风控盯上"并提前止损（避免持续轰炸加重封禁）。2026-09-26 补：
    此前该参数只声明未写入，导致止损逻辑形同虚设。
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
            if state is not None:
                state["waf"] = state.get("waf", 0) + 1
            if log:
                log(f"    [!] 命中雪球风控/拦截页(尝试{attempt}/{max_retry})"
                    f"（累计 {state.get('waf', 0) if state is not None else '?'} 次），"
                    f"退避 {8 + attempt*2}s 后重试…")
            try:
                page.wait_for_timeout((8 + attempt * 2) * 1000)
            except Exception:
                pass
            continue
        try:
            return json.loads(text)
        except Exception:
            if _is_waf_page_text(text):
                if state is not None:
                    state["waf"] = state.get("waf", 0) + 1
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

# ── 拟人化（2026-09-26 新增，回应"被识别出来要完成验证"）──
# True  = 每个帖子都真实打开详情页 + 拟人浏览（鼠标/滚轮/阅读停顿）后页内 XHR 取评论。
#         最接近真人行为，实测约 15-18s/帖（一轮 200 帖约 50-60 分钟）。
# False = 不打开详情页，直接在话题页上下文用 XHR 取（仍然不是"地址栏打开 JSON"，
#         真实度仍高于旧实现），约 3-5s/帖，适合想快跑或流量紧张时。
HUMAN_BROWSE = True
# 命中人机验证时，有头模式下等待人工完成的秒数（无头模式无法人工过，会退避重试）
CHALLENGE_WAIT_SEC = 180

# ── 行为参数 ──
SCROLL_ROUNDS = 3          # 话题列表拟人滚动次数（每次为一段变速真实滚轮）
MAX_COMMENT_PAGES = 25     # 单帖评论最多翻页数（上调以收集更多评论；注意请求量/WAF 风险）
# 帖子间随机停顿：2026-09-26 起每个帖子都会真实打开详情页并拟人浏览（约 4-8s），
# 已提供足够的自然间隔，故此处由 (3,6) 收紧到 (1.5,3.5)，避免单轮过长。
POST_DELAY = (1.5, 3.5)
COMMENT_PAGE_DELAY = (1, 3)
HEADLESS = True            # 无头模式（可后台运行）；需人工过验证/看登录过程时改 False

# ── 热点发现（登录态优先，非登录仅作兜底）──
# 实测结论（2026-09-22）：
#   · 登录态首页右侧「热门话题」表 (table.board__list.topic-hot__list) 与匿名
#     ?category=hotspot 页榜单【内容完全一致】（同为 10 条），条目链接形如
#     /k?q=%23<话题名>%23，点击后自动 301 到 /hashtag/I-... 标准话题页。
#   · 匿名上下文抓 comments.json 返回【空列表】(count=None)，等于抓不到评论；
#     只有登录态才能拿到评论。故【发现 + 抓取全程使用登录态】，不再依赖匿名。
LOGIN_HOT_URL = "https://www.xueqiu.com/"   # 登录态首页（右侧含热门话题榜）
HOT_TOPIC_TABLE_SEL = "table.board__list.topic-hot__list"
# 每轮抓取的热点话题数（取榜单前 N，页面上限约 10）
HOTSPOT_TOP_N = 10
# 匿名兜底源（仅当登录态首页取不到热门话题时才回退使用）
HOTSPOT_URL = "https://www.xueqiu.com/?category=hotspot"
MAX_POSTS_PER_TOPIC = 20    # 单个热点话题最多抓取的帖子数（上调以收集更多评论；注意单轮时长）
# 风控止损：本轮累计命中风控次数达到该值，即中止本轮（避免持续请求加深封禁）
WAF_ABORT_THRESHOLD = 3
# 非登录桌面 UA（仅匿名兜底发现时使用，保持与登录浏览器一致的指纹）
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
        # Windows GBK 控制台无法输出 ✅/⚠ 等字符：按当前编码降级替换，保证不崩线程
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


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
        self._challenge_waited = False   # 本轮是否已等待过人工验证（防止整轮被拖死）

    def _extract_post_ids(self, page):
        """从话题页提取帖子 ID。

        2026-09-26 重要修正：雪球帖子链接形态为 /<用户ID>/<帖子ID>（两段纯数字），
        而【用户主页链接】是 /<用户ID>（单段）。此前实现只校验 data-id 是 6 位以上数字，
        一旦页面把用户卡片（头像/昵称链接）排在前面，就会把【用户 ID】当成帖子 ID ——
        表现是日志里出现 10 位 ID、逐个请求 comments.json 全部拿不到评论
        （用户 ID 与帖子 ID 量级不同：用户 ID 约 10 亿-90 亿即 10 位，帖子 ID 当前为 9 位）。

        故改为**从 href 形态判别**：必须是 /数字/数字 两段式，取第二段为帖子 ID；
        若严格判别取不到（雪球改版），回退到宽松逻辑（原 data-id 规则）以免整体失效。
        """
        strict = page.evaluate("""
            () => {
              const ids = new Map();
              const items = document.querySelectorAll('article.timeline__item');
              const articles = items.length ? items : document.querySelectorAll('article');
              articles.forEach(a => {
                const authorEl = a.querySelector('.user-name, .timeline__user, [class*="user"] .name');
                const author = authorEl ? authorEl.textContent.trim() : '';
                // 遍历卡片内所有 data-id 链接，取【第一个 href 为 /<用户ID>/<帖子ID> 的】
                // （卡片里可能还有头像/昵称等指向 /<用户ID> 单段的链接，必须跳过而非中断）
                for (const link of a.querySelectorAll('a[data-id]')) {
                  const href = link.getAttribute('href') || '';
                  const m = href.match(/^\\/(\\d+)\\/(\\d+)$/);
                  if (!m) continue;
                  if (!/^\\d{6,}$/.test(m[2])) continue;
                  ids.set(m[2], {id: m[2], author: author, href: href});
                  break;
                }
              });
              return Array.from(ids.values());
            }
        """)
        if strict:
            return strict
        # 回退：宽松逻辑（保持旧行为），并告警提示可能改版/未登录导致卡片混入
        _log("    [!] 严格链接判别未取到帖子（页面可能改版），回退宽松规则，请注意核对 ID")
        return page.evaluate("""
            () => {
              const ids = new Map();
              const items = document.querySelectorAll('article.timeline__item');
              const articles = items.length ? items : document.querySelectorAll('article');
              articles.forEach(a => {
                const authorEl = a.querySelector('.user-name, .timeline__user, [class*="user"] .name');
                const author = authorEl ? authorEl.textContent.trim() : '';
                for (const link of a.querySelectorAll('a[data-id]')) {
                  const href = link.getAttribute('href') || '';
                  if (/^\\/\\d+$/.test(href)) continue;   // 明确的用户主页链接，排除
                  const v = link.getAttribute('data-id');
                  if (/^\\d{6,}$/.test(v)) {
                    ids.set(v, {id: v, author: author, href: href});
                    break;
                  }
                }
              });
              return Array.from(ids.values());
            }
        """)

    def _page_is_challenge(self, page):
        """当前页面是否显示雪球验证/风控页。"""
        try:
            txt = page.evaluate("() => (document.body ? document.body.innerText : '')") or ""
        except Exception:
            return False
        return _is_waf_page_text(txt)

    def _wait_for_human_challenge(self, page, timeout=180):
        """命中验证页时的处置：有头模式等用户手工过验证，无头模式退避重试。

        用户遇到的"提示要完成什么验证"就是这里处理：不再闷头重试或直接放弃，
        而是把浏览器窗口交给用户，验证通过后自动继续抓取。

        重要：同一轮内【最多只等一次】—— 否则每个帖子都可能等满 timeout，
        整轮会被拖死（推荐引擎实测踩过这个坑）。
        返回 True 表示（已恢复/已通过），False 表示超时或无法处理。
        """
        if not self._page_is_challenge(page):
            return True
        if getattr(self, "_challenge_waited", False):
            _log("    [!] 本轮已等待过一次人工验证，改为短退避 15s（避免整轮被拖死）")
            try:
                page.wait_for_timeout(15000)
            except Exception:
                return False
            return not self._page_is_challenge(page)
        if self.headless:
            _log("    [!] 命中雪球验证页，但当前是无头模式无法人工验证 —— 退避 15s 后重试")
            try:
                page.wait_for_timeout(15000)
            except Exception:
                return False
            return not self._page_is_challenge(page)
        self._challenge_waited = True
        _log("")
        _log("  " + "=" * 58)
        _log("  [!] 雪球拦截了当前访问，请在【浏览器窗口】中处理：")
        _log("      · 页面是验证码 / 滑块 → 完成验证即可")
        _log("      · 页面是「请求异常已被安全策略拦截」→ 请点击页面上的「登录」重新登录，")
        _log("        或等待几分钟后刷新页面")
        _log(f"      程序等待最多 {timeout} 秒，页面恢复后自动继续抓取（Ctrl+C 可退出）")
        _log("      若长时间无法恢复：通常是该 IP/账号被临时限制，需冷却 30-60 分钟，")
        _log("      或换个网络出口（手机热点）后重试。")
        _log("  " + "=" * 58)
        waited = 0
        while waited < timeout and self._running:
            try:
                page.wait_for_timeout(3000)
            except Exception:
                return False
            waited += 3
            if not self._page_is_challenge(page):
                _log(f"  [ok] 验证已通过（等待 {waited}s），继续抓取")
                return True
            if waited % 30 == 0:
                _log(f"      仍在等待人工验证… 已等 {waited}s / {timeout}s")
        _log("  [!] 等待人工验证超时，本轮跳过")
        return False

    def _xhr_json(self, page, path, state=None):
        """在当前页面上下文用 XHR 拉 JSON（真人流量模式，实现见 stealth.xhr_fetch）。

        为什么优先用 XHR 而不是 page.goto：
          - 真人浏览时评论是页面里的 XHR 拉取的，浏览器**绝不会**在地址栏打开
            `…/comments.json`；直接导航到 .json 是明显的自动化特征（也是最容易被
            风控盯上的行为）。
          - XHR 自带正确的 Referer（当前帖子页）、Accept、X-Requested-With，
            与页面真实业务请求完全一致。
        失败（非 200 / 非 JSON / 抛异常）返回 None，由调用方回退到真实导航兜底。
        """
        status, text = stealth.xhr_fetch(page, path)
        if status != 200:
            if text and _is_waf_page_text(text) and state is not None:
                state["waf"] = state.get("waf", 0) + 1
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    def _fetch_comments(self, page, post_id, state=None):
        """拉取某帖评论（多页，登录态）。

        取数优先级：
          1) 【首选】页面内 XHR —— 真人流量模式，最不易被识别；
          2) 【兜底】真实浏览器导航到 .json —— 仅在 XHR 失败时使用。

        空页或返回非 JSON 即停止。state 用于跨帖累计风控命中次数
        （达 WAF_ABORT_THRESHOLD 时置 state["abort"]=True 让调用方中止本轮）。
        """
        all_comments = []
        for p in range(1, self.max_comment_pages + 1):
            path = f"/statuses/comments.json?id={post_id}&page={p}&count=20"
            j = self._xhr_json(page, path, state=state)
            if j is None and not (state or {}).get("abort"):
                # 兜底：真实导航（保留旧路径，避免页面状态异常时完全失效）
                j = _nav_get_json(page, "https://xueqiu.com" + path,
                                  max_retry=2, log=_log, state=state)
            if state is not None and state.get("waf", 0) >= WAF_ABORT_THRESHOLD:
                state["abort"] = True
                break
            if not j:
                break
            list_ = j.get("comments") or j.get("list") or []
            if not list_:
                break
            all_comments.extend(list_)
            if len(list_) < 20:
                break
            # 翻页间隔（降低请求密度，减小触发风控概率）
            try:
                page.wait_for_timeout(random.uniform(*COMMENT_PAGE_DELAY) * 1000)
            except Exception:
                break
        return all_comments

    def _page_text(self, page):
        """安全读取当前页面可见文本（失败返回空串）。"""
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""

    def _wait_page_recover(self, page, seconds=25):
        """原地等待风控页"自愈"（挑战页常见机制：执行 JS → 写 cookie → 自动刷新）。

        为什么不在命中后立刻重新 goto：重新导航往往会**再触发一次挑战**，
        形成"越刷新越拦"的循环。这里改为在原地轮询，等页面自己跳转/恢复。
        返回恢复后的页面文本（未恢复则返回风控页文本）。
        """
        deadline = time.time() + seconds
        txt = self._page_text(page)
        while time.time() < deadline:
            try:
                page.wait_for_timeout(2500)
            except Exception:
                break
            txt = self._page_text(page)
            if not _is_waf_page_text(txt):
                return txt
        return txt

    def _topics_from_recent(self, limit=HOTSPOT_TOP_N):
        """兜底话题列表：用【上一轮抓过的话题】继续本轮抓取。

        为什么需要：热点榜（登录态右侧榜 / 匿名热点榜）都取不到时（如命中验证页
        且短时间无法恢复），原逻辑会让整轮一条都抓不到。但热点话题本身变化很慢
        （通常几小时才轮换），用上一轮的话题继续抓评论，比整轮空跑有价值得多。

        话题 URL 用 `/k?q=%23<话题名>%23` 搜索入口 —— 与首页右侧榜的链接同形态，
        浏览器会自动 301 到对应话题页（已验证）。
        """
        try:
            rows = self.db.conn.execute(
                "SELECT hashtag, MAX(first_seen) AS t FROM comments "
                "WHERE hashtag IS NOT NULL AND hashtag != '' "
                "GROUP BY hashtag ORDER BY t DESC LIMIT ?",
                (max(limit * 3, limit),)).fetchall()
        except Exception:
            return []
        out = []
        seen = set()
        for r in rows:
            name = (r["hashtag"] or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append((f"https://xueqiu.com/k?q=%23{quote(name)}%23", name))
            if len(out) >= limit:
                break
        return out

    def _discover_hot_from_home(self, page, top_n=HOTSPOT_TOP_N):
        """登录态访问雪球首页，读取右侧「热门话题」榜，返回 [(url, title), ...]。

        为什么用登录态：
        - 首页右侧「热门话题」表（table.board__list.topic-hot__list）在登录态下
          正常渲染，内容与匿名 ?category=hotspot 榜完全一致（实测同为 10 条）；
        - 而匿名上下文抓评论接口只会拿到空列表 —— 所以整条链路统一走登录态，
          不再需要额外的匿名 guest context（少一个 context，内存与指纹都更干净）。
        条目 href 形如 /k?q=%23<话题名>%23，点击后自动跳转到 /hashtag/I-... 话题页。
        """
        _log(f"  登录态访问雪球首页 {LOGIN_HOT_URL}，读取右侧「热门话题」…")
        for attempt in range(1, 4):
            try:
                page.goto(LOGIN_HOT_URL, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                _log(f"  打开雪球首页失败(尝试{attempt}/3): {e}")
                page.wait_for_timeout(2000)
                continue
            page.wait_for_timeout(3000)
            # 拟人化：曲线鼠标 + 真实滚轮轻滚，促使右侧「热门话题」渲染
            try:
                stealth.human_move(page)
                stealth.human_wheel(page, total=random.randint(400, 900))
                time.sleep(random.uniform(0.5, 1.2))
            except Exception:
                pass
            txt = self._page_text(page)
            if _is_waf_page_text(txt):
                # ① 先给人工处理的机会（有头模式会打印提示并等待用户操作）
                if self._wait_for_human_challenge(page):
                    txt = self._page_text(page)
                else:
                    # ② 挑战页常见机制：执行 JS → 写 cookie → 自动刷新。
                    #    命中后立刻重新 goto 往往会**再触发一次挑战**（越刷新越拦），
                    #    故改为原地轮询等待页面自愈。
                    _log("  [!] 首页命中风控/拦截页，原地等待页面自动恢复（最多 25s）…")
                    txt = self._wait_page_recover(page, 25)
                if _is_waf_page_text(txt):
                    _log(f"  [!] 首页仍为风控/拦截页(尝试{attempt}/3)，6s 后重试…")
                    page.wait_for_timeout(6000)
                    continue
                _log("  [ok] 页面已恢复，继续解析「热门话题」…")
            items = page.evaluate("""() => {
                const tbl = document.querySelector('table.board__list.topic-hot__list');
                if (!tbl) return [];
                const out = [];
                tbl.querySelectorAll('tr').forEach(tr => {
                    const a = tr.querySelector('a');
                    if (!a) return;
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    if (href && text && !out.some(o => o.url === href))
                        out.push({url: href, title: text.slice(0, 60)});
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
                _log(f"  ✅ 登录态取到右侧「热门话题」{total_found} 条，本轮抓取前 {len(out)} 个:")
                for i, (_, t) in enumerate(out, 1):
                    _log(f"     {i}. {t}")
                return out
            _log(f"  [!] 首页未解析到「热门话题」表格(尝试{attempt}/3)，可能页面改版/未登录，重试…")
            page.wait_for_timeout(2000)
        return []

    def _discover_hotspots(self, page, top_n=HOTSPOT_TOP_N):
        """【兜底】非登录状态访问热点榜，取前 top_n 个热点话题，返回 [(url, title), ...]。

        仅在登录态首页取不到热门话题时才调用。注意：匿名上下文只能用来「发现」
        列表，抓评论必须回到登录态 page（匿名抓 comments.json 只会拿到空列表）。

        关键：雪球「雪球热点」榜单在匿名态展示于 ?category=hotspot；
        登录态下该页会变成个性化信息流，看不到热点榜，故兜底路径必须用非登录上下文。
        命中话题链接形如 /hashtag/I-...（话题详情页，沿用 article.timeline__item 结构）。

        鲁棒性：该页常遇 WAF 挑战页（真实导航后挑战 JS 写 cookie 再重定向），
        故最多重试 3 次，每次检测 _waf / renderData / 人机验证 等特征，命中则
        等待挑战 JS 执行完再重导航。
        """
        _log(f"  【兜底】匿名访问 {HOTSPOT_URL} 发现热点（仅列表发现，抓评论仍回登录态）")
        for attempt in range(3):
            try:
                page.goto(HOTSPOT_URL, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                _log(f"  打开热点榜失败(尝试{attempt+1}/3): {e}")
                page.wait_for_timeout(2000)
                continue
            # 等待列表渲染 + 拟人滚动触发懒加载（真实滚轮，非 JS 滚动）
            page.wait_for_timeout(3000)
            try:
                stealth.browse_list(page, scroll_times=2)
            except Exception:
                pass
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

    def _discover_hotspots_fallback(self, page):
        """兼容旧调用名（等价于 _discover_hotspots）。"""
        return self._discover_hotspots(page)

    def start_session(self, pw, login_ctx=None, login_page=None):
        """启动并复用单一浏览器会话（整个持续运行/--mode hashtag 生命周期内只开一次）。

        设计（回应"chrome 每轮重复打开、标签页越积越多"的诉求）：
        - 登录态抓评论 + 登录态发现热门话题（首页右侧「热门话题」），全程单一 context；
        - 非登录 guest context 改为【惰性创建】：只有在登录态取不到热门话题、需要
          匿名兜底发现时才临时新建（实测两者榜单内容一致，正常情况下根本用不到，
          少一个 context 更省内存、也少一份被风控关联的指纹）；
        - 只保一个页面，抓取时只在该页面内导航/刷新，绝不开新标签、绝不复开。

        login_ctx/login_page 可选：由 --mode all 的统一调度器传入【推荐引擎已启动的同一
        持久化登录 context】，此时本引擎不再另开浏览器，直接复用该 context。
        返回 (login_ctx, login_page, guest_page)；缓存在 self 上供 run_forever 复用。
        """
        if self._browser is not None:
            return (self._login_ctx, self._login_page, self._guest_page)

        if login_ctx is not None and login_page is not None:
            # 借用推荐引擎的登录 context（--mode all 单一浏览器）
            self._owns_login = False
            self._login_ctx = login_ctx
            self._login_page = login_page
            self._browser = login_ctx.browser
            # 幂等补注（推荐引擎通常已注入；这里兜底，避免版本不一致导致漏注入）
            try:
                login_ctx.add_init_script(STEALTH_JS)
            except Exception:
                pass
        else:
            # 自行启动登录持久化 context
            self._owns_login = True
            profile_dir = resolve_profile_dir()
            _kill_stale_chrome(profile_dir, log=_log)
            login_ctx = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir, channel="chrome", headless=self.headless,
                accept_downloads=False, user_agent=GUEST_USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN", timezone_id="Asia/Shanghai",
                args=["--disable-blink-features=AutomationControlled"],
            )
            # 注入反自动化检测脚本（此前话题引擎完全没有注入，是重要短板）
            try:
                login_ctx.add_init_script(STEALTH_JS)
            except Exception as e:
                _log(f"    [!] stealth 脚本注入失败(可忽略): {e}")
            _cleanup_restored_tabs(login_ctx)  # 关掉 Chrome 自动恢复的旧标签页，只留一个干净页面
            self._login_ctx = login_ctx
            self._login_page = login_ctx.pages[0] if login_ctx.pages else login_ctx.new_page()
            self._browser = login_ctx.browser

        # guest context 惰性创建（默认 None）；只有兜底发现时才由 ensure_guest_context() 建
        self._guest_ctx = None
        self._guest_page = None
        _log("[热点引擎] 浏览器会话已就绪：单一常驻进程（登录态发现热点 + 登录态抓评论）")
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
                user_agent=GUEST_USER_AGENT, viewport={"width": 1440, "height": 900})
            try:
                self._guest_ctx.add_init_script(STEALTH_JS)
            except Exception:
                pass
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
        """话题单跑模式(--mode hashtag)自愈：登录会话失效则整体重连。

        注意：这里【不再】顺带创建非登录 guest context —— 发现热点已改为登录态优先，
        匿名 context 只在登录态取不到榜单时才惰性建立（见 _scrape_round）。
        """
        if not self._page_alive(self._login_page):
            _log("  [!] 热点引擎浏览器已失效（被关闭/崩溃），正在自动重连…")
            try:
                self.close_session()
                self.start_session(pw)
            except Exception as e:
                _log(f"  [!] 热点引擎重连失败: {e}")
                return False
        return self._page_alive(self._login_page)

    def _scrape_round(self, login_page, guest_page=None, state=None):
        """执行一轮热点抓取（发现 + 逐话题抓评论），不负责浏览器的开关。

        全程登录态：
        - 阶段1 用【登录】page 打开雪球首页，读右侧「热门话题」榜；
        - 阶段2 仍是同一个登录 page 抓评论（comments.json 需登录态；匿名只会返回空列表）。
        仅当登录态取不到热门话题时，才临时创建非登录 guest context 做兜底发现。

        state：本轮共享的 dict，累计风控命中次数（state["waf"]）与中止标记（state["abort"]）。
        若页面/浏览器已被外部关闭（崩溃、被其它进程清理），先尝试自愈重建再继续，
        避免整轮以 "Target page, context or browser has been closed" 失败。
        """
        if state is None:
            state = {}
        self._challenge_waited = False   # 每轮重置：一轮内最多只等一次人工验证

        # ── 阶段0：登录会话健康检查（自愈，必须通过）──
        if not self._page_alive(login_page):
            if self._page_alive(self._login_page):
                login_page = self._login_page
            else:
                _log("  [!] 登录页面已失效，本轮跳过"
                     "（--mode all 下由调度器重连浏览器后自动恢复）")
                return 0

        # ── 阶段1：登录态发现热门话题（首页右侧「热门话题」榜）──
        if self.auto_discover:
            _log("[热点引擎] 阶段1 发现热点：登录态打开雪球首页，读取右侧「热门话题」榜")
            topics = []
            try:
                topics = self._discover_hot_from_home(login_page)
            except Exception as e:
                _log(f"  登录态发现热点异常: {e}")
            if not topics:
                # 兜底：非登录热点榜（仅在登录态取不到时才惰性建匿名 context）
                _log("  [!] 登录态未取到热门话题，启用【非登录】兜底发现（仅用于列表发现）…")
                try:
                    if not self._page_alive(guest_page):
                        if self.ensure_guest_context():
                            guest_page = self._guest_page
                    if self._page_alive(guest_page):
                        topics = self._discover_hotspots(guest_page)
                except Exception as e:
                    _log(f"  非登录兜底发现异常: {e}")
            if not topics:
                # 第三层兜底：热点榜取不到 ≠ 话题不可抓。热点话题变化很慢，
                # 用上一轮的话题继续抓评论，比整轮空跑有价值得多。
                recent = self._topics_from_recent()
                if recent:
                    _log(f"  [!] 热点榜不可用，降级使用【上一轮话题】继续抓取（{len(recent)} 个）：")
                    for i, (_, t) in enumerate(recent, 1):
                        _log(f"       {i}. {t}")
                    _log("       （这是降级策略：热点榜恢复后会自动回到实时榜单）")
                    topics = recent
            if not topics:
                _log("  未发现任何热点话题，本轮跳过（热点榜与历史话题均不可用）")
                return 0
        else:
            topics = [(self.url, self.name)]

        # ── 阶段2：登录态抓取每个热点的帖子+评论（评论接口需登录）──
        _log("[热点引擎] 阶段2 抓取评论：使用【登录】持久化 profile（评论接口需登录态）")
        total_new = 0
        n = min(len(topics), HOTSPOT_TOP_N)
        for idx, (url, title) in enumerate(topics[:HOTSPOT_TOP_N], 1):
            if state.get("abort"):
                _log(f"\n  [!] 本轮已累计命中风控 {state.get('waf', 0)} 次，提前结束"
                     f"（已完成 {idx-1}/{n} 个话题），留待下一轮再抓")
                _log("      处置建议：① 等 30-60 分钟让风控冷却（403 拦截页是 IP/账号级限流，"
                     "短退避无效）；② 若持续命中，检查登录态是否失效并重新登录；"
                     "③ 可调小 MAX_COMMENT_PAGES / MAX_POSTS_PER_TOPIC 降低请求密度")
                break
            _log(f"\n{'='*16} 热点 {idx}/{n}: {title} {'='*16}")
            total_new += self._scrape_topic(login_page, url, title, state=state)
        _log(f"\n本轮热点抓取完成，共入库评论 {total_new} 条")
        return total_new

    def scrape_once(self, session=None):
        """一轮热点抓取。

        session=(login_ctx, login_page, guest_page) 由 run_forever / xueqiu.py 复用
        （单一浏览器常驻，只刷新页面不复开，节约内存）；为 None 时（单次运行 / --mode all
        每轮）自行启动并关闭浏览器（同样带标签页清理，避免旧标签堆积）。
        """
        if session is not None:
            # session = (login_ctx, login_page, guest_page)
            return self._scrape_round(session[1], session[2])
        with sync_playwright() as pw:
            login_ctx, login_page, guest_page = self.start_session(pw)
            try:
                return self._scrape_round(login_page, guest_page)
            finally:
                self.close_session()

    def _scrape_topic(self, page, url, title, state=None):
        """抓取单个热点话题页的帖子评论（登录态 page）。返回本轮入库评论数。

        url 可能是两种形态，均落在同一话题页：
        - /k?q=%23<话题名>%23（首页右侧「热门话题」条目，浏览器会自动跳转到话题页）
        - /hashtag/I-...（话题页直链，匿名兜底路径给出）
        state：本轮共享风控计数（见 _scrape_round）。
        """
        if state is None:
            state = {}
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as e:
            _log(f"  [!] 打开话题页失败（可能已失效/触发下载）: {e}，跳过该话题")
            return 0
        # /k?q= 是搜索跳转入口，需等它 301 到 /hashtag/... 话题页再解析
        if "/k?" in url or "/k?" in (page.url or ""):
            page.wait_for_timeout(2500)
        page.wait_for_timeout(3000)
        # 拟人化浏览话题列表：曲线鼠标 + 真实滚轮（原先用 mouse.wheel 直跳 + 固定间隔）
        try:
            stealth.browse_list(page, scroll_times=self.scroll_rounds)
        except Exception:
            pass
        page.wait_for_timeout(1500)

        # 话题页本身可能就是验证页
        if not self._wait_for_human_challenge(page):
            _log("  [!] 话题页处于验证状态且未通过，跳过该话题")
            return 0

        posts = self._extract_post_ids(page)
        # 限制单话题帖子数，控制单轮运行时长与请求量
        if MAX_POSTS_PER_TOPIC and len(posts) > MAX_POSTS_PER_TOPIC:
            posts = posts[:MAX_POSTS_PER_TOPIC]
        _log(f"提取到 {len(posts)} 个帖子")

        total_new = 0
        fetched_total = 0        # 接口返回的评论条数（与入库数不同：入库会按 id 去重）
        waf_before = state.get("waf", 0)
        for idx, p in enumerate(posts, 1):
            if state.get("abort"):
                _log(f"  [!] 已累计命中风控 {state.get('waf', 0)} 次，跳过本话题剩余 "
                     f"{len(posts) - idx + 1} 个帖子（避免继续请求加深封禁）")
                break
            pid = p["id"]
            author = p.get("author", "")
            href = p.get("href", "")
            self.db.save_post(pid, author, title)
            _log(f"  ({idx}/{len(posts)}) 抓取帖子 {pid} 的评论…")

            # ── 真人路径：打开帖子详情页 → 拟人浏览 → 页内 XHR 取评论 ──
            # 原实现直接 page.goto("…/comments.json")（等于在地址栏打开 JSON 文件），
            # 是最明显的自动化特征；改为「像真人一样点进帖子看评论」。
            # HUMAN_BROWSE=False 时跳过打开详情页，直接在话题页上下文 XHR（更快）。
            if HUMAN_BROWSE and href:
                post_url = href if href.startswith("http") else urljoin("https://xueqiu.com", href)
                try:
                    page.goto(post_url, wait_until="domcontentloaded", timeout=25000)
                    if not self._wait_for_human_challenge(page, timeout=CHALLENGE_WAIT_SEC):
                        _log("      帖子页处于验证状态且未通过，跳过该帖")
                        continue
                    stealth.browse_post(page)
                except Exception as e:
                    _log(f"      打开帖子页异常（改走 XHR 兜底）: {e}")

            raw = self._fetch_comments(page, pid, state=state)
            fetched_total += len(raw)
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
            waf_tip = ""
            if state.get("waf", 0) > waf_before:
                waf_tip = f"  [风控命中累计 {state['waf']} 次]"
                waf_before = state["waf"]
            _log(f"      评论 {len(raw)} 条, 入库 {saved_this} 条（累计 {self.db.count()}）{waf_tip}")
            page.wait_for_timeout(random.uniform(*POST_DELAY))

        # 诊断：接口一条评论都没返回 —— 多半是帖子 ID 提取有误（如把 10 位用户 ID 当帖子 ID），
        # 而非"该话题无人评论"。给明确提示，避免静默空跑一整轮。
        if posts and fetched_total == 0 and state.get("waf", 0) == waf_before:
            lens = sorted({len(p["id"]) for p in posts})
            _log(f"  [!] 本话题 {len(posts)} 个帖子均未返回评论数据（ID 位数={lens}）。"
                 f"若 ID 为 10 位，很可能误把【用户 ID】当成【帖子 ID】—— 请检查页面提取规则。")

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
