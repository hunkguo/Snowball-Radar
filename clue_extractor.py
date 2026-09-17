# -*- coding: utf-8 -*-
"""
雪球评论 · 价值线索提取（Layer 1 规则打分）— 通用模块

被两个抓取程序共用，保证同一套打分口径，避免逻辑漂移：
  - scraper.py          抓取「推荐 / 热门」板块帖子的评论
  - insight_extractor.py 抓取指定「话题(hashtag)」下的评论

核心能力：
  - is_meaningless(text)        过滤灌水 / 空 / 纯表情 / 极短评论
  - score_comment(text, like)   规则打分 -> (分数, 标签, 命中标的)
  - extract_clues(comments, ...) 批量筛选 + 按标的分组
  - render_clues_markdown/json  生成可直接阅读 / 发给大模型的成品

用法（库）：
    from clue_extractor import score_comment, is_meaningless, extract_clues, render_clues_markdown, render_clues_json
"""

import os
import re
import json
from datetime import datetime

# ──────────────────────────────────────────────
# 已知标的（别名 -> 标准名），按需扩充
# ──────────────────────────────────────────────
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
    "东方财富": "东方财富",
    "同花顺": "同花顺",
    "中信证券": "中信证券",
    "中国平安": "中国平安",
    "招商银行": "招商银行",
    "隆基绿能": "隆基绿能", "隆基": "隆基绿能",
    "阳光电源": "阳光电源",
    "立讯精密": "立讯精密", "立讯": "立讯精密",
    "歌尔股份": "歌尔股份", "歌尔": "歌尔股份",
    "三一重工": "三一重工", "三一": "三一重工",
    "美的集团": "美的集团", "美的": "美的集团",
    "格力电器": "格力电器", "格力": "格力电器",
}

# 方向 / 事件 / 传闻 关键词 -> 权重
EVENT_KEYWORDS = {
    "利好": 3, "利空": 3, "传闻": 2, "小道消息": 2, "消息称": 2, "据悉": 2,
    "据传": 2, "听说": 2, "圈内": 2, "渠道": 2, "内部": 2, "独家": 2,
    "合作": 2, "建厂": 2, "扩产": 2, "投产": 2, "量产": 2, "试产": 1,
    "产能": 1, "良率": 1, "订单": 2, "中标": 2, "签约": 2, "供货": 2,
    "定点": 2, "认证": 1, "客户": 1, "并购": 2, "重组": 2, "收购": 1,
    "业绩": 1, "超预期": 2, "不及预期": 2, "预增": 1, "预减": 1, "扭亏": 1,
    "涨价": 2, "降价": 1, "提价": 2, "获批": 2, "临床": 1, "研发": 1,
    "减持": 2, "增持": 2, "回购": 2, "定增": 1, "调研": 1, "交流": 1,
    "机构": 1, "涨停": 2, "跌停": 2, "异动": 1, "突破": 1, "上调": 2, "下调": 2,
}

# 灌水 / 无意义词（不区分大小写）
_MEANINGLESS_WORDS = {
    "", "顶", "沙发", "板凳", "地板", "前排", "插眼", "马克", "留名",
    "mark", "m", "mm", "dddd", "好的", "嗯", "哦", "啊", "哈", "哈哈",
    "666", "6", "111", "。。", "...", "。。。", "？", "!",
    "支持", "赞", "好", "对", "是", "否", "路过", "围观", "签到",
    "收藏", "关注", "已阅", "呵呵", "啊啊", "学习了", "看不懂", "不明觉厉",
}


def is_meaningless(text):
    """判断评论是否无意义（灌水 / 空 / 纯表情 / 极短 / 纯重复）

    返回 True 表示该评论应被过滤掉、不参与价值打分。
    """
    if not text:
        return True
    t = text.strip()
    if not t:
        return True

    # 去掉所有空白与标点（保留中文、英文、数字）
    stripped = re.sub(r"[\s\u3000\W_]+", "", t, flags=re.UNICODE)
    if not stripped:
        return True
    if len(stripped) < 2:
        return True

    # 纯重复单个字符（如「哈哈哈」「。。。。。」），长度 <= 12
    if len(set(stripped)) == 1 and len(stripped) <= 12:
        return True
    if re.fullmatch(r"(.)\1{2,}", stripped) and len(stripped) <= 12:
        return True

    # 纯短数字（如「111」「666」）
    if stripped.isdigit() and len(stripped) <= 3:
        return True

    # 常见灌水词（不区分大小写）
    if t.lower() in _MEANINGLESS_WORDS:
        return True

    return False


def score_comment(text, like_count=0):
    """对单条评论做 Layer 1 规则打分。

    返回 (score, tags, stocks)：
      score  : 整数，越高越可能是有价值线索
      tags   : 命中的标签列表，如 ["code:600xxx", "stock:澜起科技", "event:合作"]
      stocks : 命中的标准标的名列表
    """
    if is_meaningless(text):
        return -100, [], []

    s = 0
    tags = []
    stocks = []

    # 1) 股票代码（6 位数字，独立出现）
    codes = re.findall(r"(?<!\d)\d{6}(?!\d)", text)
    if codes:
        s += 3
        tags.append("code:" + ",".join(codes))

    # 2) 已知标的名称
    for k, v in STOCK_NAMES.items():
        if k in text and v not in stocks:
            stocks.append(v)
    if stocks:
        s += 2 * len(stocks)
        tags.append("stock:" + ",".join(stocks))

    # 3) 方向 / 事件 / 传闻 关键词
    ev = []
    for k, w in EVENT_KEYWORDS.items():
        if k in text:
            s += w
            ev.append(k)
    if ev:
        tags.append("event:" + ",".join(ev))

    # 4) 信息密度
    if len(text) >= 30:
        s += 1
    if re.search(r"\d", text) or "%" in text:
        s += 1

    # 5) 点赞加权（社区认可度）
    if like_count >= 50:
        s += 2
    elif like_count >= 10:
        s += 1

    return s, tags, stocks


def extract_clues(comments, threshold=5, max_candidates=80):
    """从评论列表中筛选有价值线索并按标的分组。

    comments: 元素为 dict，建议包含字段
        id, text, like_count, user_name, time_str,
        以及可选 section / post_id / post_author / hashtag 用于上下文。
    返回 (candidates, groups)：
        candidates: 排序后的候选列表（分数降序 -> 点赞降序）
        groups    : { 标的名: [candidate,...] }，按组内最高分降序
    """
    candidates = []
    for c in comments:
        text = c.get("text", "") or ""
        like = c.get("like_count", 0) or 0
        score, tags, stocks = score_comment(text, like)
        if score < threshold:
            continue
        candidates.append({
            "id": c.get("id", ""),
            "user_name": c.get("user_name", "") or "",
            "post_author": c.get("post_author", "") or "",
            "time_str": c.get("time_str", "") or "",
            "like_count": like,
            "text": text,
            "score": score,
            "tags": tags,
            "stocks": stocks,
            "section": c.get("section", "") or "",
            "post_id": c.get("post_id", "") or "",
            "hashtag": c.get("hashtag", "") or "",
        })

    candidates.sort(key=lambda x: (x["score"], x["like_count"]), reverse=True)
    if max_candidates:
        candidates = candidates[:max_candidates]

    # 按标的分组；未识别标的归入「（未识别标的/综合）」
    groups = {}
    for c in candidates:
        keys = c["stocks"] if c["stocks"] else ["（未识别标的/综合）"]
        for k in keys:
            groups.setdefault(k, []).append(c)
    groups = dict(sorted(groups.items(),
                         key=lambda kv: max(x["score"] for x in kv[1]),
                         reverse=True))
    return candidates, groups


# ──────────────────────────────────────────────
# 成品渲染
# ──────────────────────────────────────────────
def split_new_comments(comments, seen_ids):
    """把评论分成「新评论」与「已分析过的评论」（增量分析用）。

    comments : comment dict 列表（需含 id）
    seen_ids : set/list，已分析过的评论 id 集合
    返回 (new_comments, skipped_count)
    """
    if not seen_ids:
        return list(comments), 0
    s = set(seen_ids)
    new = [c for c in comments if str(c.get("id", "")) not in s]
    return new, len(comments) - len(new)


def render_clues_markdown(candidates, groups, meta):
    """生成可直接阅读的 markdown 价值线索日报，含可复制的大模型提示词。"""
    title = meta.get("title", "雪球评论 · 价值线索（Layer 1 规则）")
    context_desc = meta.get("context_desc", "雪球用户评论，经规则筛选出的「可能有价值」线索。")
    threshold = meta.get("threshold", 5)
    total = meta.get("total_comments", 0)
    n = len(candidates)
    max_llm = meta.get("max_llm_candidates", 80)

    L = []
    L.append(f"# {title}\n")
    L.append(f"> 生成时间：{meta.get('generated_at', '')}")
    L.append(f"> 候选评论：**{n}** 条 / 本轮分析 {total} 条（规则打分 ≥ {threshold}，已剔除灌水）")
    if meta.get("incremental"):
        skipped = meta.get("skipped_count", 0)
        seen_total = meta.get("seen_total", 0)
        L.append(f"> 模式：**增量分析**（本轮只分析新出现的评论）"
                 f"｜跳过已分析 {skipped} 条｜累计已分析 {seen_total} 条")
    L.append(f"> 说明：{context_desc}复制文末【提示词】区块发给任意大模型，即可获得结构化分析与小道消息日报。\n")

    if not candidates:
        L.append("## 本轮无新增价值线索\n")
        if meta.get("incremental"):
            L.append("本轮没有新出现的评论（或新评论未达打分阈值），"
                     "说明社区观点与上一轮雷同，**无需重复分析**。\n")
        else:
            L.append("（本轮未筛出达到阈值的候选评论）\n")

    # 一、按标的分组
    L.append("## 一、按标的分组\n")
    if not groups:
        L.append("（本轮未筛出达到阈值的候选评论）\n")
    for stock, items in groups.items():
        L.append(f"### {stock}（{len(items)} 条）\n")
        for i, c in enumerate(items, 1):
            like = c["like_count"]
            who = c["user_name"]
            when = c["time_str"]
            sec = f" [{c['section']}]" if c.get("section") else ""
            L.append(f"{i}. 【赞{like}】{who}{sec}（{when}）：{c['text']}")
        L.append("")

    # 二、按分数排名 Top 30
    L.append("## 二、按分数排名（Top 30）\n")
    L.append("| 分数 | 标的 | 事件标签 | 用户 | 板块 | 赞 | 内容 |")
    L.append("|------|------|----------|------|------|----|------|")
    for c in candidates[:30]:
        stocks = ",".join(c["stocks"]) or "—"
        ev = ",".join(t.replace("event:", "") for t in c["tags"] if t.startswith("event:")) or "—"
        sec = c.get("section", "") or "—"
        text = c["text"].replace("|", "丨").replace("\n", " ")
        if len(text) > 40:
            text = text[:40] + "…"
        L.append(f"| {c['score']} | {stocks} | {ev} | {c['user_name']} | {sec} | {c['like_count']} | {text} |")
    L.append("")

    # 三、发给大模型的提示词
    L.append("## 三、发给大模型的提示词（复制此区块）\n")
    L.append("```text")
    L.append("你是一名 A 股题材与小道消息分析助手。下面是雪球评论中、")
    L.append("经规则筛选出的「可能有价值」评论候选池（已剔除灌水）。")
    L.append(f"【背景】{context_desc}")
    if meta.get("incremental"):
        gen = meta.get("generated_at", "")[:16]
        seen_total = meta.get("seen_total", 0)
        L.append(f"【增量说明】本候选池**只包含 {gen} 之后新出现的评论**"
                 f"（历史累计已分析 {seen_total} 条，不在此重复提供）。")
        L.append("请只针对这些**新增**评论做分析，不要复述或推测已分析过的旧内容；")
        L.append("若新评论只是重复既有观点（无新信息），请明确标注「无新增信息」，不要展开。\n")
        L.append("请基于这些新增评论完成以下任务：\n")
    else:
        L.append("请基于这些评论完成以下任务：\n")
    L.append("1. 逐条评估：是否有投资参考价值？信息可信度如何？")
    L.append("2. 去重合并：同一事件的多条评论合并为一条，保留最有信息量的一条。")
    if meta.get("incremental"):
        L.append("3. 按重要性与可信度排序，输出一份**聚焦增量信息**的「小道消息 / 价值线索日报」；")
        L.append("   每条都注明它相对社区既有讨论**新增了什么**（新事件/新数据/新方向/仅情绪）。\n")
    else:
        L.append("3. 按重要性与可信度排序，输出一份「小道消息 / 价值线索日报」。\n")
    L.append("【评论候选池】")
    for i, c in enumerate(candidates[:max_llm], 1):
        stocks = ",".join(c["stocks"]) or "未识别"
        sec = f"[{c['section']}]" if c.get("section") else ""
        L.append(f"{i}. {sec}[{stocks}] {c['user_name']}（赞{c['like_count']}）：{c['text']}")
    L.append("")
    L.append("【输出要求】")
    L.append("- 直接输出 markdown 日报正文，不要返回 JSON，不要写代码块，不要附带解释性开场白。")
    if meta.get("incremental"):
        L.append("- 只写**本轮新增**的信息，不要重复已有结论；无新增内容时直接写「本轮无新增可分析信息」。")
    L.append("- 按标的分组，每组包含：标的代码/名称、核心事件、方向（利好/利空/中性）、")
    L.append("  来源类型（官方/媒体/个人传闻/个人判断）、置信度（高/中/低）、要点摘要。")
    L.append("- 无法核实或明显臆测的内容，单独归入「存疑/待验证」，并说明理由。")
    L.append("- 文末附「一句话总结」与「风险提示」（注明信息来自社区评论，非投资建议）。")
    L.append("```\n")

    return "\n".join(L)


def render_clues_json(meta, candidates, groups):
    """生成结构化 JSON（供程序消费 / 二次分析）。"""
    return {
        "generated_at": meta.get("generated_at", ""),
        "title": meta.get("title", ""),
        "context_desc": meta.get("context_desc", ""),
        "score_threshold": meta.get("threshold", 5),
        "total_comments": meta.get("total_comments", 0),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "groups": {k: [c["id"] for c in v] for k, v in groups.items()},
    }


def _load_seen(path):
    """读取已分析评论 id 集合（JSON 文件，缺失则返回空集）"""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            return {str(x) for x in data.get("seen_ids", [])}
    except Exception:
        pass
    return set()


def _save_seen(path, seen):
    """原子写回已分析评论 id 集合"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "count": len(seen),
                       "seen_ids": sorted(seen)}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def generate_clue_files(comments, meta, export_dir, prefix="clues",
                        write_json=False, seen_path=None, incremental=True):
    """价值线索成品的「单一产出入口」：筛选 + 渲染 + 落盘。

    两个抓取程序（推荐/热门、话题）与统一入口 xueqiu.py 都走这里，
    保证输出格式完全一致。

    默认**只产出 md**（整理内容 + 可直接复制的大模型提示词，供人工发给大模型）；
    结构化 JSON 仅在 write_json=True 时额外产出（可通过 --json 开启）。

    增量分析（incremental=True 且给了 seen_path 时）：
      - 只分析「上次之后新出现」的评论，已分析过的直接跳过，避免每小时观点雷同
      - 分析完成后把本轮分析过的 id 写入 seen_path，供下轮去重

    参数:
        comments   : clue_extractor.extract_clues 期望的 comment dict 列表
        meta       : 渲染元信息（title / context_desc / threshold / max_llm_candidates / total_comments / generated_at）
        export_dir : 导出目录（data/exports）
        prefix     : 文件名前缀，如 "clues" / "insight_walsh_rate_hike"
        write_json : 是否同时写出结构化 JSON（默认 False）
        seen_path  : 已分析 id 记录文件路径（None 则不做增量记忆）
        incremental: 是否启用增量分析（False = 每次都分析全量）
    返回: (md_path, json_path, candidates)；未写 JSON 时 json_path 为 None
    """
    os.makedirs(export_dir, exist_ok=True)

    total_all = len(comments)
    skipped = 0
    seen = set()
    use_incremental = bool(incremental and seen_path)

    if use_incremental:
        seen = _load_seen(seen_path)
        comments, skipped = split_new_comments(comments, seen)
        m = dict(meta)
        m["incremental"] = True
        m["skipped_count"] = skipped
        m["seen_total"] = len(seen)
        meta = m

    total = len(comments)
    meta = dict(meta)
    meta["total_comments"] = total

    candidates, groups = extract_clues(
        comments,
        threshold=meta.get("threshold", 5),
        max_candidates=meta.get("max_llm_candidates", 80),
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = None
    if write_json:
        json_path = os.path.join(export_dir, f"{prefix}_{ts}.json")
        out = render_clues_json(meta, candidates, groups)
        out["incremental"] = bool(meta.get("incremental"))
        out["skipped_count"] = meta.get("skipped_count", 0)
        out["analyzed_count"] = total
        out["db_total_comments"] = total_all
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(export_dir, f"{prefix}_{ts}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_clues_markdown(candidates, groups, meta))

    # 标记本轮分析过的评论（含未达阈值的），下轮不再重复分析
    if use_incremental:
        seen |= {str(c.get("id", "")) for c in comments}
        seen.discard("")
        _save_seen(seen_path, seen)

    return md_path, json_path, candidates
