# -*- coding: utf-8 -*-
"""拟人化浏览 · 反自动化检测（两个抓取引擎共用）

2026-09-26 新增。背景：话题引擎此前【没有注入任何 stealth 脚本】，
且鼠标用 `page.mouse.wheel()` 直跳、滚动用 `window.scrollBy()`（JS 滚动，
不产生真实 wheel 事件）、抓评论直接 `page.goto("…/comments.json")`
（真人绝不会在地址栏打开 JSON 文件）—— 这些都是明显的自动化特征。

本模块提供：
  - STEALTH_JS        注入到每个页面，抹掉 navigator.webdriver 等指纹
  - human_move()      贝塞尔曲线鼠标轨迹（带抖动/变速，非直线插值）
  - human_wheel()     真实滚轮事件 + 分段变速（模拟惯性滚动）
  - reading_pause()   按内容长度估算的阅读停顿（人看东西要时间）
  - browse_post()     进入帖子后的完整拟人动作序列

设计原则：所有动作都带随机量，绝不出现固定间隔/固定坐标/固定步数。
"""

import random
import time

# ── 反检测 JS（在文档创建前注入）──
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
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) window.chrome.runtime = {};
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(p);
}
// WebGL 厂商/渲染器：headless 下常被识别为 SwiftShader，改成常见独显字符串
try {
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
        if (p === 37445) return 'Intel Inc.';                 // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return 'Intel Iris OpenGL Engine';   // UNMASKED_RENDERER_WEBGL
        return getParam.call(this, p);
    };
} catch (e) {}
// 隐藏 CDP 痕迹
try {
    delete Object.getPrototypeOf(navigator).webdriver;
} catch (e) {}
"""

VIEWPORT_W = 1440
VIEWPORT_H = 900

# 模块级记忆上一次鼠标位置（Playwright 不提供读取接口）
_last_pos = [random.randint(500, 900), random.randint(300, 500)]


def _bezier(p0, c1, c2, p1, steps):
    """三次贝塞尔曲线离散点。"""
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = (mt ** 3) * p0[0] + 3 * (mt ** 2) * t * c1[0] + 3 * mt * (t ** 2) * c2[0] + (t ** 3) * p1[0]
        y = (mt ** 3) * p0[1] + 3 * (mt ** 2) * t * c1[1] + 3 * mt * (t ** 2) * c2[1] + (t ** 3) * p1[1]
        pts.append((x, y))
    return pts


def human_move(page, to_x=None, to_y=None):
    """曲线鼠标移动：从上次落点沿贝塞尔曲线移到目标，带控制点抖动与变速。

    真实人类鼠标轨迹是弧线且速度不均（起步慢-中段快-收尾慢），
    而 Playwright 的 `mouse.move(steps=N)` 是等速直线插值 —— 容易被轨迹分析识别。
    """
    global _last_pos
    x0, y0 = _last_pos
    x1 = to_x if to_x is not None else random.randint(80, VIEWPORT_W - 80)
    y1 = to_y if to_y is not None else random.randint(80, VIEWPORT_H - 160)
    # 控制点：垂直于连线方向随机偏移，形成自然弧度
    c1 = (x0 + (x1 - x0) * random.uniform(0.2, 0.4) + random.uniform(-90, 90),
          y0 + (y1 - y0) * random.uniform(0.0, 0.3) + random.uniform(-70, 70))
    c2 = (x0 + (x1 - x0) * random.uniform(0.6, 0.85) + random.uniform(-90, 90),
          y0 + (y1 - y0) * random.uniform(0.7, 1.0) + random.uniform(-70, 70))
    steps = random.randint(16, 32)
    for (px, py) in _bezier((x0, y0), c1, c2, (x1, y1), steps):
        try:
            page.mouse.move(px, py)
        except Exception:
            return None
        time.sleep(random.uniform(0.004, 0.018))
    _last_pos = [x1, y1]
    return (x1, y1)


def human_wheel(page, total=None, direction=1):
    """真实滚轮滚动：分段 + 变速 + 微停顿，模拟人类惯性滚动。

    必须用 `mouse.wheel`（派发真实 wheel 事件），而不是 `window.scrollBy`
    —— 后者不产生 wheel 事件、滚动位置为整数跳变，是明显的脚本特征。
    """
    total = total if total is not None else random.randint(400, 1100)
    remaining = float(total)
    while remaining > 0:
        step = min(remaining, random.randint(70, 260))
        try:
            page.mouse.wheel(0, step * direction)
        except Exception:
            return
        remaining -= step
        time.sleep(random.uniform(0.06, 0.24))
    # 惯性收尾：偶尔回滚一点点（真人看到感兴趣内容会轻微上滑）
    if random.random() < 0.25:
        try:
            page.mouse.wheel(0, random.randint(-120, -40))
            time.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass


def reading_pause(text_len=0, base=1.2, cap=5.0):
    """按可见内容长度估算阅读停顿（人看东西要花时间）。

    text_len 为页面/段落字符数；估算 0.004s/字，并叠加随机抖动。
    """
    est = min(cap, base + max(0, text_len) * 0.004)
    time.sleep(random.uniform(est * 0.65, est * 1.35))


def visible_text_len(page, limit=4000):
    """页面可见文本长度（用于估算阅读时长），异常返回 0。"""
    try:
        n = page.evaluate("() => (document.body && document.body.innerText ? document.body.innerText.length : 0)")
        return min(int(n or 0), limit)
    except Exception:
        return 0


def browse_post(page, scroll_times=None):
    """进入一个帖子详情页后的拟人动作序列：看标题 → 缓慢下滚 → 停顿阅读。

    返回本页可见文本长度（供调用方判断加载是否正常）。
    """
    human_move(page)
    time.sleep(random.uniform(0.3, 0.9))
    n = visible_text_len(page)
    reading_pause(n, base=1.0, cap=3.5)          # 先看主要内容
    for _ in range(scroll_times or random.randint(1, 2)):
        human_wheel(page, total=random.randint(350, 800))
        reading_pause(visible_text_len(page), base=0.8, cap=2.5)
    if random.random() < 0.2:                     # 偶尔回看一下
        human_wheel(page, total=random.randint(150, 400), direction=-1)
        time.sleep(random.uniform(0.4, 1.0))
    human_move(page)
    return n


def browse_list(page, scroll_times=None):
    """列表页（话题页/首页）的拟人滚动：边滚边停，模拟找感兴趣的内容。"""
    human_move(page)
    time.sleep(random.uniform(0.4, 1.0))
    for _ in range(scroll_times or random.randint(2, 4)):
        human_wheel(page, total=random.randint(500, 1100))
        time.sleep(random.uniform(0.5, 1.6))
    if random.random() < 0.3:
        human_wheel(page, total=random.randint(200, 500), direction=-1)
        time.sleep(random.uniform(0.3, 0.8))
