# -*- coding: utf-8 -*-
"""
Jev（TypeSafe System One）零依赖客户端 —— 用于判断雪球评论的「投资参考价值」。

设计原则（与 uploader.py 一致）：
  - 只用标准库 urllib，避免引入第三方 SDK 导致 PyInstaller 打包膨胀
  - **opt-in**：仅当显式传入 api_key 才启用；否则调用方应跳过，绝不联网
  - 失败一律降级返回默认值（ok=False），绝不中断本地抓取/上传主流程
  - 支持 HTTP_PROXY / HTTPS_PROXY 环境变量（大陆访问 api.typesafe.ai 可能需代理）

用法（库）：
    from jev_client import judge_comment

    res = judge_comment(text, api_key)
    # res = {"value":0.0~1.0, "is_valuable":f, "has_signal":f, "is_noise":f,
    #         "model":"jev-1.13.0", "ok":True}
"""

import json
import os
import time
import urllib.request
import urllib.error

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"

# 三个 noul 问题：覆盖「有无参考价值 / 是否有具体信号 / 是否噪声」
# 问题之间并行且隔离，互不影响；一个调用同时拿到三个概率。
_JEV_QUESTIONS = {
    "is_valuable": {
        "type": "noul",
        "instructions": (
            "这条雪球评论是否对A股投资决策有参考价值？"
            "含基本面、行业/政策动态、资金面、具体数据、明确观点或风险提示的判为是；"
            "纯情绪发泄、无信息量的闲聊、与个股无关的泛泛而谈判为否。"
        ),
    },
    "has_signal": {
        "type": "noul",
        "instructions": (
            "这条评论是否包含具体可操作的信息？"
            "如股票代码/名称、价格、仓位、时间节点、明确的方向判断。"
        ),
    },
    "is_noise": {
        "type": "noul",
        "instructions": (
            "这条评论是否是噪声？如纯广告、@喊单、诱导关注、"
            "与投资无关的闲聊、纯情绪宣泄、无实质内容的附和。"
        ),
    },
}


def _proxy_handlers():
    """读 HTTP(S)_PROXY 环境变量，构造代理 handler（无则空列表）。"""
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    if proxy:
        return [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    return []


def judge_comment(text, api_key, model=JEV_MODEL, timeout=15):
    """判断单条评论对 A 股投资者的价值。

    返回 dict：
      {
        "value":       float 0~1,  # 综合价值分 = is_valuable * (1 - is_noise)
        "is_valuable": float,      # 有无参考价值（noul 概率）
        "has_signal":  float,      # 含具体可操作信息（noul 概率）
        "is_noise":    float,      # 噪声/喊单/广告（noul 概率）
        "model":       str,        # 实际模型版本（如 jev-1.13.0）
        "ok":          bool,       # 调用是否成功
      }
    任何失败（无 key、空文本、网络/超时/解析异常）均返回 ok=False 的默认值，
    各分默认 0，调用方据此跳过即可。
    """
    default = {
        "value": 0.0, "is_valuable": 0.0, "has_signal": 0.0,
        "is_noise": 0.0, "model": model, "ok": False,
    }
    if not api_key:
        return default
    text = (text or "").strip()
    if not text:
        return default
    # state 上限约 3.2 万 token / 15 万字符，取保险截断
    if len(text) > 2000:
        text = text[:2000]

    body = {
        "model": model,
        "state": text,
        "questions": _JEV_QUESTIONS,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(JEV_ENDPOINT, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("User-Agent", "xueqiu-radar/1.0")

    try:
        opener = urllib.request.build_opener(*_proxy_handlers())
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        obj = json.loads(raw)
    except urllib.error.HTTPError:
        return default
    except Exception:
        return default

    try:
        ans = obj.get("answers", {})
        is_valuable = float(ans.get("is_valuable", {}).get("noul", 0) or 0)
        has_signal = float(ans.get("has_signal", {}).get("noul", 0) or 0)
        is_noise = float(ans.get("is_noise", {}).get("noul", 0) or 0)
        model_ver = obj.get("model", model) or model
        # 综合价值分：有参考价值，且扣除噪声权重（噪声越高越不值钱）
        value = max(0.0, min(1.0, is_valuable * (1.0 - is_noise)))
        return {
            "value": round(value, 4),
            "is_valuable": round(is_valuable, 4),
            "has_signal": round(has_signal, 4),
            "is_noise": round(is_noise, 4),
            "model": model_ver,
            "ok": True,
        }
    except Exception:
        return default
