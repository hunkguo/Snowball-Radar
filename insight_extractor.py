# -*- coding: utf-8 -*-
"""
雪球话题评论 — Layer 1 价值提取器 (insight_extractor.py)

功能:
- 读取 hashtag_comments.db 中已抓取的评论
- 规则打分 + 剔除灌水, 筛选"可能有价值"的候选评论
- 按 [标的] / [分数] 整理, 生成可直接发给大模型的提示词文档

用法:
    python insight_extractor.py
产出:
    data/exports/insight_<short>_<ts>.md   整理内容 + 提示词(可直接复制)
    data/exports/insight_<short>_<ts>.json 结构化候选数据(供程序消费)
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime

# ── 路径配置（支持 EXE 打包）──
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "hashtag_comments.db")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

# ── 话题标识（与 hashtag_comments.py 保持一致，便于文件名对应）──
HASHTAG_SHORT = "walsh_rate_hike"
HASHTAG_NAME = "沃什：加息25基点至4%，通胀难降但就业不伤"

# ── 参数 ──
SCORE_THRESHOLD = 5          # 进入候选池的最低分
MAX_LLM_CANDIDATES = 80      # 发给大模型的候选上限（按分数截取）

# 已知标的（别名 -> 标准名），按需扩充
STOCK_NAMES = {
    "澜起科技": "澜起科技", "澜起": "澜起科技",
    "英特尔": "英特尔", "intel": "英特尔", "Intel": "英特尔",
    "海力士": "SK海力士", "SK海力士": "SK海力士",
    "中芯国际": "中芯国际", "中芯": "中芯国际",
    "韦尔股份": "韦尔股份", "韦尔": "韦尔股份",
    "兆易创新": "兆易创新", "兆易": "兆易创新",
    "北方华创": "北方华创",
    "卓胜微": "卓胜微",
    "圣邦股份": "圣邦股份", "圣邦": "圣邦股份",
    "长电科技": "长电科技", "长电": "长电科技",
    "通富微电": "通富微电",
    "寒武纪": "寒武纪",
    "英伟达": "英伟达", "nvidia": "英伟达", "NVIDIA": "英伟达",
    "台积电": "台积电", "tsmc": "台积电", "TSMC": "台积电",
    "苹果": "苹果", "apple": "苹果", "Apple": "苹果",
    "华为": "华为", "比亚迪": "比亚迪", "宁德时代": "宁德时代",
    "贵州茅台": "贵州茅台", "茅台": "贵州茅台",
}

# 方向 / 事件关键词 -> 权重
EVENT_KEYWORDS = {
    "利好": 3, "利空": 3, "传闻": 2, "小道消息": 2, "消息称": 2, "据悉": 2,
    "合作": 2, "建厂": 2, "扩产": 2, "投产": 2, "量产": 2, "试产": 1,
    "产能": 1, "良率": 1, "订单": 2, "中标": 2, "签约": 2, "供货": 2,
    "定点": 2, "认证": 1, "客户": 1, "并购": 2, "重组": 2, "收购": 1,
    "业绩": 1, "超预期": 2, "不及预期": 2, "预增": 1, "预减": 1, "扭亏": 1,
    "涨价": 2, "降价": 1, "提价": 2, "获批": 2, "临床": 1, "研发": 1,
    "减持": 2, "增持": 2, "回购": 2, "定增": 1, "调研": 1, "交流": 1,
    "机构": 1,
}


def _is_meaningless(text):
    """剔除灌水 / 无意义评论"""
    if not text or not text.strip():
        return True
    stripped = re.sub(r"[\s\u3000]+", "", text)
    if len(stripped) < 2:
        return True
    # 纯标点 / 表情
    if re.fullmatch(r"[\W_]+", stripped):
        return True
    words = ["顶", "沙发", "板凳", "前排", "马克", "mark", "Mark", "MARK",
             "路过", "围观", "签到", "支持", "赞", "👍", "好", "学习了",
             "收藏", "关注", "已阅", "哈哈", "呵呵", "啊啊"]
    if stripped in words:
        return True
    # 纯重复字符 (如 哈哈哈, 。。。。。) <= 12
    if len(set(stripped)) == 1 and len(stripped) <= 12:
        return True
    # 纯短数字 (如 111, 666)
    if stripped.isdigit() and len(stripped) <= 3:
        return True
    # 重复短模式 (如 哈哈哈哈哈) <= 12
    if re.fullmatch(r"(.)\1{2,}", stripped) and len(stripped) <= 12:
        return True
    return False


def score_comment(text, like_count):
    """返回 (score, tags, stocks)"""
    if _is_meaningless(text):
        return -100, [], []
    s = 0
    tags = []
    stocks = []

    # 股票代码 (6 位数字)
    codes = re.findall(r"(?<!\d)\d{6}(?!\d)", text)
    if codes:
        s += 3
        tags.append("code:" + ",".join(codes))

    # 已知标的名称
    for k, v in STOCK_NAMES.items():
        if k in text and v not in stocks:
            stocks.append(v)
    if stocks:
        s += 2 * len(stocks)
        tags.append("stock:" + ",".join(stocks))

    # 事件 / 方向关键词
    ev = []
    for k, w in EVENT_KEYWORDS.items():
        if k in text:
            s += w
            ev.append(k)
    if ev:
        tags.append("event:" + ",".join(ev))

    # 信息密度
    if len(text) >= 30:
        s += 1
    if re.search(r"\d", text) or "%" in text:
        s += 1

    # 点赞加权
    if like_count >= 50:
        s += 2
    elif like_count >= 10:
        s += 1

    return s, tags, stocks


def load_comments():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM comments ORDER BY like_count DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def build_candidates(rows):
    candidates = []
    for r in rows:
        text = r["text"] or ""
        like = r["like_count"] or 0
        score, tags, stocks = score_comment(text, like)
        if score < SCORE_THRESHOLD:
            continue
        candidates.append({
            "id": r["id"],
            "user_name": r["user_name"] or "",
            "post_author": r["post_author"] or "",
            "time_str": r["time_str"] or "",
            "like_count": like,
            "text": text,
            "score": score,
            "tags": tags,
            "stocks": stocks,
        })
    # 排序：分数降序 -> 点赞降序
    candidates.sort(key=lambda c: (c["score"], c["like_count"]), reverse=True)
    return candidates


def group_by_stock(candidates):
    groups = {}
    for c in candidates:
        keys = c["stocks"] if c["stocks"] else ["（未识别标的/综合）"]
        for k in keys:
            groups.setdefault(k, []).append(c)
    # 按组内最高分排序
    return dict(sorted(groups.items(),
                       key=lambda kv: max(x["score"] for x in kv[1]),
                       reverse=True))


def render_markdown(candidates, groups, total_comments):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = len(candidates)
    L = []
    L.append("# 雪球话题评论 · 价值候选提炼（Layer 1 规则）\n")
    L.append(f"> 话题：{HASHTAG_NAME}")
    L.append(f"> 生成时间：{ts}")
    L.append(f"> 候选评论：**{n}** 条 / 总评论 {total_comments} 条（规则打分 ≥ {SCORE_THRESHOLD}，已剔除灌水）")
    L.append("> 说明：以下为经规则筛选的「可能有价值」评论。复制文末【提示词】区块发给任意大模型，即可获得结构化分析与小道消息日报。\n")

    # 一、按标的分组
    L.append("## 一、按标的分组\n")
    for stock, items in groups.items():
        L.append(f"### {stock}（{len(items)} 条）\n")
        for i, c in enumerate(items, 1):
            like = c["like_count"]
            time_s = c["time_str"]
            L.append(f"{i}. 【赞{like}】{c['user_name']}（{time_s}）：{c['text']}")
        L.append("")

    # 二、按分数排名 Top 30
    L.append("## 二、按分数排名（Top 30）\n")
    L.append("| 分数 | 标的 | 事件标签 | 用户 | 赞 | 内容 |")
    L.append("|------|------|----------|------|----|------|")
    for c in candidates[:30]:
        stocks = ",".join(c["stocks"]) or "—"
        ev = ",".join(t.replace("event:", "") for t in c["tags"] if t.startswith("event:")) or "—"
        text = c["text"].replace("|", "丨").replace("\n", " ")
        if len(text) > 40:
            text = text[:40] + "…"
        L.append(f"| {c['score']} | {stocks} | {ev} | {c['user_name']} | {c['like_count']} | {text} |")
    L.append("")

    # 三、发给大模型的提示词
    L.append("## 三、发给大模型的提示词（复制此区块）\n")
    L.append("```text")
    L.append("你是一名 A 股题材与小道消息分析助手。下面是雪球某话题下、")
    L.append("经规则筛选出的「可能有价值」评论候选池（已剔除灌水）。")
    L.append(f"【话题背景】雪球用户讨论：{HASHTAG_NAME}。以下评论集中于相关题材的个股联动与产业链消息。")
    L.append("请基于这些评论完成以下任务：\n")
    L.append("1. 逐条评估：是否有投资参考价值？信息可信度如何？")
    L.append("2. 结构化抽取：每条输出 { 标的, 事件, 方向(利好/利空/中性),")
    L.append("   来源类型(官方/媒体/个人传闻/个人判断), 置信度(0-1), 摘要 }")
    L.append("3. 去重合并：同一事件的多条评论合并为一条，保留最有信息量的一条。")
    L.append("4. 输出一份「小道消息日报」markdown，按标的分组，标注多空方向与置信度。\n")
    L.append("【评论候选池】")
    for i, c in enumerate(candidates[:MAX_LLM_CANDIDATES], 1):
        stocks = ",".join(c["stocks"]) or "未识别"
        L.append(f"{i}. [{stocks}] {c['user_name']}（赞{c['like_count']}）：{c['text']}")
    L.append("")
    L.append("请严格以如下格式返回：先一段「小道消息日报」markdown，")
    L.append("再一个 JSON 数组（字段：标的/事件/方向/来源类型/置信度/摘要/原始序号）。")
    L.append("```\n")

    return "\n".join(L)


def main():
    if not os.path.exists(DB_PATH):
        print(f"[错误] 未找到数据库：{DB_PATH}\n请先运行 hashtag_comments.py 抓取评论。")
        return

    rows = load_comments()
    total = len(rows)
    candidates = build_candidates(rows)
    groups = group_by_stock(candidates)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON
    json_path = os.path.join(EXPORT_DIR, f"insight_{HASHTAG_SHORT}_{ts}.json")
    payload = {
        "generated_at": datetime.now().isoformat(),
        "hashtag": HASHTAG_NAME,
        "score_threshold": SCORE_THRESHOLD,
        "total_comments": total,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Markdown（含提示词）
    md_path = os.path.join(EXPORT_DIR, f"insight_{HASHTAG_SHORT}_{ts}.md")
    md = render_markdown(candidates, groups, total)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Layer 1 提取完成")
    print(f"  总评论: {total}  候选(≥{SCORE_THRESHOLD}分): {len(candidates)}")
    print(f"  JSON : {json_path}")
    print(f"  MD   : {md_path}  (含可直接复制的大模型提示词)")


if __name__ == "__main__":
    main()
