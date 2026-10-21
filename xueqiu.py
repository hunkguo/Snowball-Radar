# -*- coding: utf-8 -*-
"""
雪球抓取 · 统一入口 (xueqiu.py)

把两个抓取引擎整合到一个程序，运行时用参数区分抓什么：
  - 推荐/热门 板块帖子下的评论   (原 scraper.py)
  - 指定话题(hashtag) 下的评论    (原 hashtag_comments.py)
  - 两者同时抓取                   (--mode all)

无论哪种模式，策略一致：抓到的评论经 Layer 1 规则打分，剔除灌水，
筛选「可能有价值」的线索，输出
    - data/exports/clues_*.md       整理内容 + 可直接复制的大模型提示词（默认只产出 md）
    - data/exports/insight_*.md     话题版同上

用法:
    python xueqiu.py                         # 默认：推荐/热门 + 话题 都抓
    python xueqiu.py --mode recommend        # 仅抓 推荐/热门
    python xueqiu.py --mode hashtag          # 仅抓 话题
    python xueqiu.py --mode all              # 两者都抓（默认，每轮顺序执行）
    python xueqiu.py --once                  # 两者各抓一轮后退出
    python xueqiu.py --clues                 # 仅基于已抓评论提取线索，不抓取
    python xueqiu.py --json                  # 额外产出结构化 JSON（默认不产）
    python xueqiu.py --mode hashtag --url "..." --name "..." --short "xxx"
    python xueqiu.py --interval-min 30 --interval-max 45
"""

import argparse
import atexit
import os
import re
import signal
import subprocess
import sys
import time
import random
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ── 路径配置（支持 EXE 打包）──
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 引擎与共用模块 ──
import clue_extractor  # noqa: E402
from clue_extractor import generate_clue_files  # noqa: E402

from scraper import (  # noqa: E402
    XueqiuScraper,
    XueqiuDB,
    DB_PATH as RECOMMEND_DB,
    JSON_EXPORT_DIR as RECOMMEND_EXPORT,
    GEN_CLUES,
    CLUE_THRESHOLD,
    CLUE_MAX_CANDIDATES,
    CLUE_SEEN_PATH,
    LIMIT_SUSPECT_ROUNDS,
    resolve_profile_dir,
)
import hashtag_comments as hm  # noqa: E402
from hashtag_comments import (  # noqa: E402
    HashtagDB,
    XueqiuHashtagScraper,
    DB_PATH as HASH_DB,
    EXPORT_DIR as HASH_EXPORT,
    HASHTAG_URL,
    HASHTAG_NAME,
    HASHTAG_SHORT,
    SCROLL_ROUNDS,
    MAX_COMMENT_PAGES,
    HEADLESS as HASH_HEADLESS,
    AUTO_DISCOVER_HOT_TOPIC,
)
import insight_extractor  # noqa: E402
from insight_extractor import load_comments, build_comment_dicts  # noqa: E402


# ──────────────────────────────────────────────
#  价值线索提取（统一产出）
# ──────────────────────────────────────────────
def gen_recommend_clues(write_json=False, incremental=True, include_prompt=False,
                        upload=False, upload_url=None, upload_token=None,
                        jev_api_key=None):
    """读取 推荐/热门 评论库，生成 clues_<ts>.md（可选 .json）。

    incremental=True 时只分析「上次之后新出现」的评论，避免每小时观点雷同。
    include_prompt=True 时在文末追加「发给大模型的提示词」区块（默认 False，适合人工阅读）。
    """
    db = XueqiuDB(RECOMMEND_DB)
    try:
        comments = db.get_all_comments_for_clues()
    finally:
        db.close()
    total = len(comments)
    meta = {
        "title": "雪球 推荐/热门 板块评论 · 价值线索（Layer 1 规则）",
        "context_desc": (
            "雪球「推荐 / 热门」板块帖子下的用户评论，经规则筛选出的「可能有价值」线索"
            "（含小道消息、产业链、业绩/订单、多空方向等信号）。"
        ),
        "threshold": CLUE_THRESHOLD,
        "total_comments": total,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "max_llm_candidates": CLUE_MAX_CANDIDATES,
    }
    md_path, json_path, candidates = generate_clue_files(
        comments, meta, RECOMMEND_EXPORT, "clues", write_json=write_json,
        seen_path=CLUE_SEEN_PATH, incremental=incremental,
        include_prompt=include_prompt,
        upload=upload, upload_url=upload_url, upload_token=upload_token,
        upload_source="recommend", jev_api_key=jev_api_key)
    return md_path, json_path, candidates


def gen_hashtag_clues(name=HASHTAG_NAME, short=HASHTAG_SHORT, write_json=False,
                      incremental=True, seen_path=None, include_prompt=False,
                      upload=False, upload_url=None, upload_token=None,
                      jev_api_key=None):
    """读取 话题 评论库，生成 insight_<short>_<ts>.md（可选 .json）。

    incremental=True 时只分析「上次之后新出现」的评论，避免每小时观点雷同。
    include_prompt=True 时在文末追加「发给大模型的提示词」区块（默认 False，适合人工阅读）。
    """
    # 确保库表结构存在（首次运行/空库时不报错）
    _hdb = HashtagDB(HASH_DB)
    _hdb.close()
    rows = load_comments()
    comments = build_comment_dicts(rows)
    total = len(comments)
    meta = {
        "title": "雪球话题评论 · 价值候选提炼（Layer 1 规则）",
        "context_desc": f"雪球用户讨论：{name}。以下评论集中于相关题材的个股联动与产业链消息。",
        "threshold": insight_extractor.SCORE_THRESHOLD,
        "total_comments": total,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "max_llm_candidates": insight_extractor.MAX_LLM_CANDIDATES,
    }
    prefix = f"insight_{short}"
    sp = seen_path or insight_extractor.seen_path_for(short)
    md_path, json_path, candidates = generate_clue_files(
        comments, meta, HASH_EXPORT, prefix, write_json=write_json,
        seen_path=sp, incremental=incremental, include_prompt=include_prompt,
        upload=upload, upload_url=upload_url, upload_token=upload_token,
        upload_source="hashtag", jev_api_key=jev_api_key)
    return md_path, json_path, candidates


# ──────────────────────────────────────────────
#  单实例保护
# ──────────────────────────────────────────────
def _pid_alive(pid):
    """指定 PID 的进程是否仍在运行（Windows: tasklist）。

    注意：tasklist 输出是系统本地编码（中文 Windows 为 GBK），必须 errors="replace"
    解码，否则读取线程会因 UnicodeDecodeError 崩溃、stdout 变空 → 恒定返回 False。
    """
    try:
        pid = int(pid)
        raw = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, timeout=10).stdout or b""
        text = raw.decode("utf-8", "replace")
        if str(pid) not in text:
            try:
                text = raw.decode("mbcs", "replace")
            except Exception:
                pass
        return bool(re.search(rf"\b{pid}\b", text))
    except Exception:
        return False


def acquire_single_instance(allow_multi=False):
    """单实例保护：同一台机器、同一份 data/ 只允许一个爬虫实例运行。

    两个实例会争抢同一个 chrome_profile：后启动者的清理逻辑可能关掉前者的浏览器，
    对方随即出现 "Target page, context or browser has been closed"（此后每轮都失败）。
    返回 True 表示可以继续运行。
    """
    if allow_multi:
        _print("  [单实例] 已加 --allow-multi，跳过单实例检查"
               "（不推荐：并行实例可能互相关闭浏览器，导致热点发现失败）")
        return True

    lock_path = os.path.join(os.path.dirname(HASH_DB), ".xueqiu.lock")
    try:
        if os.path.exists(lock_path):
            old_pid = 0
            try:
                old_pid = int((open(lock_path, encoding="utf-8").read() or "0").strip())
            except Exception:
                old_pid = 0
            if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
                _print(f"  [!] 检测到已有雪球爬虫实例在运行（PID={old_pid}），本次启动中止。")
                _print("      两个实例会争抢同一个 Chrome Profile，互相把对方的浏览器关掉，")
                _print("      导致出现 'Target page, context or browser has been closed'。")
                _print("      处理：先关闭旧实例（在其窗口按 Ctrl+C）再启动；")
                _print("      确需并行请加 --allow-multi（不推荐）。")
                _print(f"      若确认已无实例在运行，删除锁文件后重试: {lock_path}")
                return False
            _print(f"  [单实例] 发现陈旧锁文件（PID={old_pid} 已不在运行），已接管。")

        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        def _release_lock():
            try:
                if os.path.exists(lock_path):
                    cur = (open(lock_path, encoding="utf-8").read() or "").strip()
                    if cur == str(os.getpid()):
                        os.remove(lock_path)
            except Exception:
                pass

        atexit.register(_release_lock)
        _print(f"  [单实例] 已获取运行锁（PID={os.getpid()}）")
        return True
    except Exception as e:
        _print(f"  [单实例] 锁检查异常，继续运行: {e}")
        return True


# ──────────────────────────────────────────────
#  夜间停抓（降低「7×24 不间断轮询」的机器人特征）
# ──────────────────────────────────────────────
NIGHT_PAUSE_START = 23   # 23:00 起停止抓取
NIGHT_PAUSE_END = 7      # 07:00 恢复（设为 None 可关闭夜间停抓）


def _in_night_pause(now=None):
    """当前是否处于夜间停抓时段。"""
    if NIGHT_PAUSE_START is None or NIGHT_PAUSE_END is None:
        return False
    h = (now or datetime.now()).hour
    if NIGHT_PAUSE_START <= NIGHT_PAUSE_END:
        return NIGHT_PAUSE_START <= h < NIGHT_PAUSE_END
    return h >= NIGHT_PAUSE_START or h < NIGHT_PAUSE_END


def _sleep_until_night_end(state):
    """睡到 NIGHT_PAUSE_END 点；每 30 秒检查一次 Ctrl+C，保证可随时退出。"""
    now = datetime.now()
    target = now.replace(hour=NIGHT_PAUSE_END, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    _print(f"  [夜间停抓] 当前 {now:%H:%M}，暂停抓取至 {target:%m-%d %H:%M}"
           f"（降低风控风险，Ctrl+C 可退出）")
    while state["running"] and datetime.now() < target:
        time.sleep(30)


# ──────────────────────────────────────────────
#  参数
# ──────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="雪球抓取统一入口：推荐/热门 + 话题，统一参数驱动")
    p.add_argument("--mode", choices=["recommend", "hashtag", "all"],
                   default="all",
                   help="抓取模式：recommend=推荐/热门, hashtag=话题, all=两者都抓（默认）")
    p.add_argument("--once", action="store_true",
                   help="只抓一轮即退出（不进入持续循环）")
    p.add_argument("--clues", action="store_true",
                   help="仅提取价值线索（基于已抓评论，不启动浏览器/不抓取）")
    p.add_argument("--no-clues", action="store_true",
                   help="跳过每轮的价值线索生成")
    p.add_argument("--json", dest="write_json", action="store_true",
                   help="额外导出结构化 JSON（默认只产出 md）")
    p.add_argument("--prompt", dest="include_prompt", action="store_true",
                   help="在文末追加「发给大模型的提示词」区块（默认关闭，适合自己直接阅读）")
    p.add_argument("--full", dest="full", action="store_true",
                   help="全量分析（默认增量：只分析新出现的评论，分析过的不再重复）")
    p.add_argument("--reset-seen", dest="reset_seen", action="store_true",
                   help="清空「已分析评论」记录（下次运行重新全量分析一遍，然后恢复增量）")
    p.add_argument("--headless", action="store_true",
                   help="话题抓取使用无头 Chrome（推荐模式始终需要可见窗口登录）")
    p.add_argument("--no-headless", action="store_true",
                   help="话题抓取使用可见 Chrome")
    p.add_argument("--fast", action="store_true",
                   help="话题抓取走快速模式：不打开帖子详情页，直接在话题页上下文用 XHR 取评论"
                        "（约 3-5s/帖）。默认拟人模式会真实打开每个帖子并像真人一样浏览"
                        "（鼠标轨迹/滚轮/阅读停顿），约 15-18s/帖，被风控概率更低")
    p.add_argument("--url", default=None, help="话题页 URL（覆盖默认）")
    p.add_argument("--name", default=None, help="话题标题（覆盖默认，用于标注）")
    p.add_argument("--short", default=None, help="文件名短标识（覆盖默认）")
    p.add_argument("--interval-min", type=int, default=120,
                   help="抓取间隔下限（分钟）。默认 120；2026-10-21 因账号被封由 45 上调，勿随意调小")
    p.add_argument("--interval-max", type=int, default=180,
                   help="抓取间隔上限（分钟）。默认 180；高频轮询是封号主因之一")
    p.add_argument("--allow-multi", dest="allow_multi", action="store_true",
                   help="允许并行运行多个实例（默认单实例保护：两实例会争抢同一 Chrome Profile、互相关闭浏览器）")
    # ── 上传到 Cloudflare Worker（雪球雷达 · 线索台）──
    p.add_argument("--upload", action="store_true",
                   help="每轮自动上传线索到 Worker（默认关闭；也可用环境变量 WORKER_URL/ WORKER_TOKEN 配置）")
    p.add_argument("--worker-url", default=None,
                   help="Worker 接收地址，如 https://xueqiu.你的域名.com/api/ingest")
    p.add_argument("--worker-token", default=None,
                   help="上传鉴权 token（与 Worker 端 INGEST_TOKEN 一致）")
    # ── Jev 语义价值判断（opt-in，需 token，默认关闭）──
    p.add_argument("--jev", action="store_true",
                   help="启用 Jev（TypeSafe System One）对候选评论做投资价值打分；"
                        "需同时提供 token（--jev-token 或环境变量 JEV_API_KEY / TYPESAFE_API_KEY）")
    p.add_argument("--jev-token", "--jev-key", default=None, dest="jev_token",
                   help="Jev API Token / Key（也可设环境变量 JEV_API_KEY 或 TYPESAFE_API_KEY）")
    p.add_argument("--reset-profile", action="store_true",
                   help="启动前把旧 Chrome Profile 改名备份（chrome_profile.bak-时间戳）。"
                        "换账号 / 登录态损坏时用：备份后启动会弹 Chrome，请用新账号登录。不删数据。")
    return p.parse_args()


def _print(msg):
    """打印运行日志（对 Windows GBK 控制台做编码降级，避免特殊字符导致崩溃）"""
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = (getattr(sys.stdout, "encoding", None) or "utf-8")
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


# ──────────────────────────────────────────────
#  主流程
# ──────────────────────────────────────────────
def main():
    args = parse_args()
    mode = args.mode
    do_recommend = mode in ("recommend", "all")
    do_hashtag = mode in ("hashtag", "all")

    # 单实例保护（--clues 是只读模式、不启动浏览器，无需加锁）
    if not args.clues and not acquire_single_instance(args.allow_multi):
        sys.exit(2)

    # 同步两个引擎的开关
    import scraper as _scraper_mod  # noqa: E402
    _scraper_mod.EXPORT_JSON = args.write_json
    hm.EXPORT_JSON = args.write_json
    _scraper_mod.GEN_CLUES = not args.no_clues
    _scraper_mod.CLUE_INCREMENTAL = not args.full
    _do_incremental = not args.full

    # 上传到 Worker 的配置（命令行优先，其次环境变量 WORKER_URL / WORKER_TOKEN）
    upload_enabled = args.upload
    worker_url = args.worker_url or os.environ.get("WORKER_URL") or ""
    worker_token = args.worker_token or os.environ.get("WORKER_TOKEN") or ""
    if upload_enabled:
        _scraper_mod.UPLOAD_ENABLED = True
        _scraper_mod.UPLOAD_URL = worker_url
        _scraper_mod.UPLOAD_TOKEN = worker_token
        insight_extractor.UPLOAD_ENABLED = True
        insight_extractor.UPLOAD_URL = worker_url
        insight_extractor.UPLOAD_TOKEN = worker_token
        _print(f"  [上传] 已开启 → {worker_url or '(未配置 WORKER_URL)'}")
    else:
        _print("  [上传] 未开启（如需上传请加 --upload 或设环境变量 WORKER_URL/ WORKER_TOKEN）")

    # Jev 语义价值判断（opt-in）：仅 --jev 且能拿到 key 才启用，否则保持离线
    jev_key = (args.jev_token
               or os.environ.get("JEV_API_KEY")
               or os.environ.get("TYPESAFE_API_KEY") or "")
    jev_api_key = jev_key if (args.jev and jev_key) else None
    if args.jev and not jev_key:
        _print("  [Jev] 已加 --jev 但未找到 token（--jev-token / 环境变量 JEV_API_KEY / TYPESAFE_API_KEY），将跳过 Jev 打分")
    elif jev_api_key:
        _print("  [Jev] 已开启 → 候选评论将经 TypeSafe Jev 做投资价值打分（仅规则高分候选）")

    # 清空「已分析评论」记录（如需重新基线）
    if args.reset_seen:
        for p in (CLUE_SEEN_PATH, insight_extractor.SEEN_PATH):
            try:
                if os.path.exists(p):
                    os.remove(p)
                    _print(f"  已清空已分析记录: {p}")
            except Exception as e:
                _print(f"  [!] 清空失败 {p}: {e}")
        _do_incremental = False  # 本次强制全量，重建基线

    _print(f"雪球统一抓取启动 | 模式={mode} | 仅线索={args.clues} | 单次={args.once} | 导出JSON={args.write_json} | 增量={_do_incremental}")

    # ── 仅提取线索 ──
    if args.clues:
        if do_recommend and not args.no_clues:
            try:
                md, _, c = gen_recommend_clues(write_json=args.write_json,
                                               incremental=_do_incremental,
                                               include_prompt=args.include_prompt,
                                               upload=upload_enabled,
                                               upload_url=worker_url,
                                               upload_token=worker_token,
                                               jev_api_key=jev_api_key)
                _print(f"推荐线索: {len(c)} 条候选 -> {md}")
            except Exception as e:
                _print(f"推荐线索提取失败: {e}")
        if do_hashtag and not args.no_clues:
            try:
                md, _, c = gen_hashtag_clues(write_json=args.write_json,
                                             incremental=_do_incremental,
                                             include_prompt=args.include_prompt,
                                             upload=upload_enabled,
                                             upload_url=worker_url,
                                             upload_token=worker_token,
                                             jev_api_key=jev_api_key)
                _print(f"话题线索: {len(c)} 条候选 -> {md}")
            except Exception as e:
                _print(f"话题线索提取失败: {e}")
        return

    # ── 换号 / 登录态重置：把旧 Profile 改名备份（不删除），启动后用新账号登录 ──
    if args.reset_profile:
        _prof = resolve_profile_dir()
        if os.path.isdir(_prof):
            try:
                from scraper import _kill_stale_chrome
                _kill_stale_chrome(_prof)   # 先释放占用，否则 Windows 上改不了名
            except Exception:
                pass
            _bak = f"{_prof}.bak-{datetime.now():%Y%m%d_%H%M%S}"
            try:
                os.rename(_prof, _bak)
                _print(f"  [换号] 旧 Chrome Profile 已备份为: {_bak}")
                _print("         启动后请在弹出的 Chrome 窗口用【新账号】登录雪球。")
                _print("         ⚠ 该参数只需用一次：登录成功后请从启动命令/run.bat 中删除 --reset-profile，")
                _print("           否则每次启动都会再次备份并强制重新登录。")
            except Exception as e:
                _print(f"  [!] Profile 备份失败（可能仍被 Chrome 占用）: {e}")
                _print("      请先关闭所有使用该 Profile 的 Chrome 窗口，再重试。")
        else:
            _print(f"  [换号] 未发现旧 Profile（{_prof}），将以全新 Profile 启动。")

    # ── 构建引擎 ──
    hashtag_headless = HASH_HEADLESS
    if args.headless:
        hashtag_headless = True
    if args.no_headless:
        hashtag_headless = False
    if args.fast:
        # 关闭「打开每个帖子详情页 + 拟人浏览」，改为在话题页上下文直接 XHR（更快）
        import hashtag_comments as _hcm
        _hcm.HUMAN_BROWSE = False
        _print("  话题抓取：--fast 快速模式（不打开帖子详情页，直接在话题页 XHR 取评论；"
               "被风控概率高于默认拟人模式）")

    rec = None
    htag = None
    hdb = None
    if do_recommend:
        rec = XueqiuScraper(
            max_pages=3, max_comment_pages=1, max_comment_posts=8, login_wait=300)
    if do_hashtag:
        hurl = args.url or HASHTAG_URL
        hname = args.name or HASHTAG_NAME
        hshort = args.short or HASHTAG_SHORT
        # 未显式指定 --url 时，自动从首页「热门话题」取最新最热话题
        auto_discover = AUTO_DISCOVER_HOT_TOPIC and not args.url
        hdb = HashtagDB(HASH_DB)
        htag = XueqiuHashtagScraper(
            hdb, hurl, hname,
            headless=hashtag_headless,
            scroll_rounds=SCROLL_ROUNDS,
            max_comment_pages=MAX_COMMENT_PAGES,
            auto_discover=auto_discover,
            short=hshort,
        )
        _print(f"话题目标: {hname}  (自动发现最新热门={auto_discover}, 无头={hashtag_headless})")

    # ── 信号处理：Ctrl+C 优雅退出 ──
    state = {"running": True}

    def _handler(signum, frame):
        _print(f"收到退出信号({signum})，将在本轮结束后退出…")
        state["running"] = False

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)

    round_no = 0
    limit_streak = 0   # 连续「疑似账号受限」轮数（见下方账号受限检测）
    # 单一浏览器常驻：所有引擎共用一个 Chrome 进程（登录持久化 profile），
    # 推荐与话题顺序复用同一 context/page；话题在其上另开非登录 guest context 做热点发现。
    # 只刷新页面不复开，避免每轮重复启动 Chrome 导致标签页堆积、内存膨胀。
    pw_cm = None
    if do_recommend or do_hashtag:
        pw_cm = sync_playwright()
        pw = pw_cm.__enter__()

        def _start_engines():
            if do_recommend:
                rec.start_session(pw)            # 推荐引擎拥有登录持久化 context
            if do_hashtag:
                if do_recommend:
                    htag.start_session(pw, login_ctx=rec._context, login_page=rec._page)  # 借用同一 context
                else:
                    htag.start_session(pw)       # 话题单独运行，自行拥有登录 context

        # 启动浏览器带重试：Profile 被占用时 Chrome 会把命令行转交给已有实例并 exit(0)，
        # 表现为 "Target page, context or browser has been closed"。给 3 次机会，
        # 仍失败则打印可操作的处理建议后**优雅退出**（不再抛未捕获异常刷 traceback）。
        _started = False
        for _attempt in range(1, 4):
            try:
                _start_engines()
                _started = True
                break
            except Exception as e:
                _print(f"  [!] 浏览器启动失败（第 {_attempt}/3 次）: {e}")
                for _eng in (rec, htag):
                    try:
                        if _eng is not None:
                            _eng.close_session()
                    except Exception:
                        pass
                if _attempt < 3:
                    _print("      60 秒后重试（请关闭占用该 Profile 的 Chrome 窗口 / 其他 xueqiu 实例）…")
                    time.sleep(60)

        if not _started:
            _print("")
            _print("  [x] 无法启动浏览器，程序退出（未做任何抓取）。")
            _print("      请依次确认：")
            _print("        1) 没有另一个 xueqiu.exe 在运行（旧实例请在其窗口按 Ctrl+C 退出）")
            _print("        2) 没有 Chrome 窗口占用 data\\chrome_profile（关掉它）")
            _print("        3) 仍失败：备份后删除 data\\chrome_profile，重新启动并登录一次")
            try:
                pw_cm.__exit__(None, None, None)
            except Exception:
                pass
            for _db in ([rec.db] if rec is not None else []) + ([hdb] if hdb is not None else []):
                try:
                    _db.close()
                except Exception:
                    pass
            return
    try:
        while state["running"]:
            round_no += 1
            _print(f"\n{'='*60}")
            _print(f"  第 {round_no} 轮抓取  {datetime.now():%Y-%m-%d %H:%M:%S}")
            _print(f"{'='*60}")

            # ── 会话健康检查（自愈）：浏览器被关闭/崩溃时自动重连，而不是此后每轮都失败 ──
            rec_ready = False
            if do_recommend:
                if not rec.is_session_alive():
                    _print("  [!] 检测到浏览器已关闭/失效（可能被外部关闭或崩溃），正在自动重连…")
                    try:
                        rec.close_session()
                        rec.start_session(pw)
                        _print("  [ok] 浏览器已重连")
                    except Exception as e:
                        _print(f"  [!] 浏览器重连失败: {e}（本轮跳过抓取，下一轮自动重试）")
                if rec.is_session_alive():
                    # 登录校验：失效 → 暂停并提示人工登录；仍未登录则跳过，绝不匿名硬抓
                    rec_ready = rec.ensure_login_for_round(rec._page)
                    if not rec_ready:
                        _print("  [!] 登录态无效：本轮跳过推荐/热门抓取（登录恢复后自动继续）")

            hashtag_ready = False
            if do_hashtag:
                if do_recommend:
                    # 推荐侧可能刚重连换了 context → 话题引擎同步其借用的登录上下文
                    if rec._context is not None and htag._login_ctx is not rec._context:
                        htag._login_ctx = rec._context
                        htag._login_page = rec._page
                        try:
                            htag._browser = rec._context.browser
                        except Exception:
                            htag._browser = None
                    # 与推荐引擎共用同一登录 context → 登录态一致
                    hashtag_ready = rec_ready
                    # 非登录 guest context 已改为【惰性创建】：热点发现走登录态首页右侧
                    # 「热门话题」榜，匿名 context 仅在登录态取不到榜单时由 _scrape_round
                    # 内部按需建立，这里不再每轮强制重建（少一个 context，省内存 + 少一份指纹）。
                else:
                    if not htag.ensure_session_alive(pw):
                        _print("  [!] 热点引擎会话不可用（本轮跳过，下一轮自动重试）")
                    else:
                        hashtag_ready = htag.ensure_login(htag._login_page)
                        if not hashtag_ready:
                            _print("  [!] 登录态无效：本轮跳过话题抓取（登录恢复后自动继续）")

            if do_recommend:
                if not rec_ready:
                    _print("  本轮跳过推荐/热门抓取（登录态无效，不匿名硬抓）。")
                else:
                    try:
                        rid = rec._do_one_scrape(rec._page)
                        _print(f"  推荐/热门抓取完成 (run_id={rid})")
                    except Exception as e:
                        _print(f"  [!] 推荐抓取异常: {e}")

            # ── 账号受限检测 ──
            # 账号被封时 cookie 里的 xq_a_token 可能仍在 → 登录检测查不出来，
            # 表现为「接口都拿到了，但评论全是空」。这里用「明确受限信息」或
            # 「连续多轮全部取不到评论」判定，命中即停手，不再空转（避免加重处罚）。
            if do_recommend and rec_ready:
                _suspect, _reason, _hard = rec.account_suspect
                if _suspect:
                    limit_streak = LIMIT_SUSPECT_ROUNDS if _hard else limit_streak + 1
                    _print(f"  [!] 疑似账号受限（{limit_streak}/{LIMIT_SUSPECT_ROUNDS} 轮）：{_reason}")
                else:
                    if limit_streak:
                        _print("  [ok] 本轮数据正常，账号受限计数清零")
                    limit_streak = 0
                if limit_streak >= LIMIT_SUSPECT_ROUNDS:
                    _print("")
                    _print("  [x] 连续多轮取不到数据，判定账号已被限制/封禁 → 已停止抓取。")
                    _print("      请用该 Chrome 窗口打开 xueqiu.com，确认账号是否被禁言/封禁；")
                    _print("      确为封禁请申诉或更换账号后再启动（不要继续空转，以免加重处罚）。")
                    state["running"] = False
                    break

            if do_hashtag:
                if not hashtag_ready:
                    _print("  本轮跳过话题抓取（登录态无效，不匿名硬抓）。")
                else:
                    try:
                        n = htag._scrape_round(htag._login_page, htag._guest_page)
                        _print(f"  话题抓取完成 (新增 {n} 条评论)")
                        if not args.no_clues:
                            # 注：各话题已在 _scrape_topic 内按话题独立生成 insight_<话题>_*.md；
                            # 这里额外产出一份「全部热门话题」汇总（名称中立，不再沿用写死的旧话题名）。
                            md, _, c = gen_hashtag_clues(
                                name="雪球热门话题（全部）", short="hashtag_all",
                                write_json=args.write_json, incremental=_do_incremental,
                                include_prompt=args.include_prompt,
                                upload=upload_enabled, upload_url=worker_url,
                                upload_token=worker_token, jev_api_key=jev_api_key)
                            _print(f"  话题线索 {len(c)} 条 -> {md}")
                    except Exception as e:
                        _print(f"  [!] 话题抓取/线索异常: {e}")

            # ── 价值线索提取（推荐侧，统一策略）──
            if not args.no_clues and do_recommend and rec_ready:
                try:
                    md, _, c = gen_recommend_clues(write_json=args.write_json,
                                                   incremental=_do_incremental,
                                                   include_prompt=args.include_prompt,
                                                   upload=upload_enabled,
                                                   upload_url=worker_url,
                                                   upload_token=worker_token,
                                                   jev_api_key=jev_api_key)
                    _print(f"  推荐线索 {len(c)} 条 -> {md}")
                except Exception as e:
                    _print(f"  [!] 推荐线索提取失败: {e}")

            if args.once or not state["running"]:
                break

            # ── 夜间停抓（降低机器人特征）：睡到次日 NIGHT_PAUSE_END，期间可 Ctrl+C 退出 ──
            if _in_night_pause():
                _sleep_until_night_end(state)
                if not state["running"]:
                    break

            wait = random.randint(args.interval_min, args.interval_max)
            next_t = datetime.now() + timedelta(minutes=wait)
            _print(f"\n  本轮结束。下次执行: {next_t:%Y-%m-%d %H:%M:%S} "
                   f"（约 {wait} 分钟后，Ctrl+C 退出）")

            # 分段等待，便于响应 Ctrl+C
            waited = 0
            while waited < wait * 60 and state["running"]:
                time.sleep(5)
                waited += 5
    finally:
        if pw_cm is not None:
            try:
                if do_hashtag:
                    htag.close_session()      # 关 guest；若 --mode all 借用则不关共享登录 context
            except Exception:
                pass
            try:
                if do_recommend:
                    rec.close_session()       # 关共享登录 context（--mode all 由推荐引擎持有）
            except Exception:
                pass
            try:
                pw_cm.__exit__(None, None, None)
            except Exception:
                pass
        if rec is not None:
            try:
                rec.db.close()
            except Exception:
                pass
        if hdb is not None:
            try:
                hdb.close()
            except Exception:
                pass
        _print("程序已退出。")


if __name__ == "__main__":
    main()

    # EXE 打包后，窗口不会自动关闭
    if getattr(sys, "frozen", False):
        print("\n" + "=" * 60)
        print("程序已退出。按 Enter 键关闭窗口...")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
