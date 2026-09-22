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

    def _discover_hot_topic(self, page):
        """从雪球首页自动取当前最热话题链接。

        返回 (url, title)；全部失败时返回 (None, None)，交由调用方跳过本轮。

        发现源（2026-09-22 增补，用户建议）：优先用移动版 m.xueqiu.com 首页 ——
        它同时展示「雪球热点」和「热门话题」链接，结构稳定，且命中 WAF 挑战页的
        概率比桌面版低。拿不到再回退桌面版 xueqiu.com 首页。

        链接形态：话题搜索页 /k?q=%23话题名%23（即 /k?q=#话题#），并非 /hashtag/。
        这些搜索页与话题页共用 article.timeline__item 帖子结构和 comments 评论接口，
        抓取逻辑完全通用。

        鲁棒性：首页常遇 WAF 挑战页（真实浏览器导航后挑战 JS 写 cookie 再重定向），
        故每次最多重试 3 次，等待挑战 JS 执行完（检测 _waf / renderData / 人机验证 等）；
        选择器优先级：① 右侧热门话题盒子 → ② 页面任意 /k?q= 链接 → ③ 任意含 %23 的话题链接。
        """
        homes = ["https://m.xueqiu.com/", "https://xueqiu.com/"]
        for home in homes:
            for attempt in range(3):
                try:
                    page.goto(home, wait_until="domcontentloaded", timeout=25000)
                except Exception as e:
                    _log(f"  打开首页失败({home}, 尝试{attempt+1}): {e}")
                    break  # 该首页打不开，换下一个来源
                # 选择器优先级：热门话题盒子 → 任意 /k?q= → 任意 %23 话题链接
                selectors = [
                    "div.board.board__topic a[href*='/k?q=']",
                    "a[href*='/k?q=']",
                    "a[href*='%23']",
                ]
                sel = None
                for cand in selectors:
                    try:
                        page.wait_for_selector(cand, timeout=8000)
                        sel = cand
                        break
                    except Exception:
                        continue
                if not sel:
                    # 可能仍处 WAF 挑战页
                    try:
                        txt = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
                    except Exception:
                        txt = ""
                    if _is_waf_page_text(txt):
                        _log(f"  首页({home}) 命中 WAF 挑战页(尝试{attempt+1})，等 6s 重试…")
                        page.wait_for_timeout(6000)
                        continue
                    # 非挑战页但无话题链接 → 该首页无话题，换下一个来源
                    _log(f"  首页({home}) 无热门话题链接，尝试下一个来源")
                    break
                page.wait_for_timeout(800)  # 让列表完全渲染
                res = page.evaluate("""(sel) => {
                    const box = document.querySelector('div.board.board__topic')
                              || document.querySelector('.topic-hot__list')
                              || document;
                    const a = box.querySelector(sel)
                              || document.querySelector("a[href*='/k?q=']")
                              || document.querySelector("a[href*='%23']");
                    if (!a) return {error: 'no topic link'};
                    const href = a.getAttribute('href') || '';
                    const title = (a.textContent || '').trim();
                    return {href, title};
                }""", sel)
                if isinstance(res, dict) and res.get("href"):
                    url = res["href"]
                    if url.startswith("//"):
                        url = "https:" + url
                    elif url.startswith("/"):
                        url = "https://xueqiu.com" + url
                    title = res.get("title", "")
                    if not title:
                        # 链接文本为空时，从 q=%23话题%23 反解
                        import urllib.parse as _up
                        q = _up.urlparse(url).query
                        title = (_up.parse_qs(q).get("q", [""])[0]
                                .replace("%23", "").replace("#", "").strip()) or url
                    _log(f"  自动发现最新热门话题({home}): {title or url}")
                    return url, title
                if attempt == 2:
                    _log(f"  热门话题解析失败({home}): {res}")
                page.wait_for_timeout(2000)
            # 当前 home 用尽 3 次仍未拿到 → 换下一个来源
        return None, None

    def scrape_once(self):
        with sync_playwright() as pw:
            profile_dir = resolve_profile_dir()
            _log(f"  Chrome profile: {profile_dir}")
            # 先清理上一次运行可能残留的 Chrome 窗口（同一 profile 只允许一个实例）
            _kill_stale_chrome(profile_dir, log=_log)
            browser = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                channel="chrome",
                headless=self.headless,
                accept_downloads=False,
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
                            self.short = _slugify(title)  # 按话题独立 seen 文件，避免串味
                        _log(f"自动发现最新热门话题: {self.name}")
                    else:
                        # 自动发现失败：清空 URL 干净跳过，不再去 goto 失效的写死配置
                        self.url = None
                        _log("  自动发现未返回链接，本轮跳过话题抓取（不回退失效配置）")
                except Exception as e:
                    self.url = None
                    _log(f"  自动发现热门话题异常，本轮跳过: {e}")

            # 防御：配置/回退的话题 URL 可能已失效（雪球改版/重定向到下载），
            # 打开失败时不要让它拖累整轮抓取，跳过话题、直接收尾。
            if not self.url or not str(self.url).startswith("http"):
                _log("  无有效话题 URL，跳过本轮话题抓取")
                browser.close()
                return 0
            try:
                page.goto(self.url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                _log(f"  [!] 打开话题页失败（可能已失效/触发下载）: {e}")
                _log("      已跳过本轮话题抓取，不影响推荐/热门主流程")
                browser.close()
                return 0
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
                        "reply_count": cm.get("reply_count", 0) or 0,
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
