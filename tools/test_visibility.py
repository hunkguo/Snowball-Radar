"""
验证「页面重新可见」补跑机制：
1. 触发 visibilitychange → 是否正确补跑
2. 补跑期间再触发 → 是否被 ST.running 挡住（不重复跑）
3. 补跑后 nextAt 是否正确更新（不因旧值导致重复触发）
4. 补跑产生的上传是否正常（走完整链路）
"""
import json
import os
import re
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "xueqiu-radar", "userscript", "xueqiu-radar-auto.user.js")
GM_BRIDGE = os.path.join(HERE, "gm_bridge.js")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

bridge = open(GM_BRIDGE, encoding="utf-8").read()
bridge += "\nwindow.GM_setClipboard = (t) => { window.__clip = t; };\n"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=False,
                                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    ctx = browser.new_context(user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
                              viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    # 记录上传请求次数
    uploads = []
    page.on("request", lambda r: uploads.append(r.url)
            if "/api/ingest" in r.url and r.method == "POST" else None)

    # 桩：让脚本认为「已登录」且能抓到评论
    import json as _json
    SAMPLES = os.path.join(HERE, "real_samples.json")
    samples = _json.load(open(SAMPLES, encoding="utf-8")) if os.path.exists(SAMPLES) else None

    def stub_candidates():
        return [{"id": f"v{i}", "user_name": f"用户{i}", "text": f"宁德时代订单超预期 {i}",
                 "description": f"宁德时代订单超预期 {i}",
                 "created_at": 1792600000 + i * 3600, "like_count": 10 + i,
                 "reply_count": 2, "user": {"screen_name": f"用户{i}", "id": 1000 + i}}
                for i in range(4)]

    def handle(route):
        from urllib.parse import urlparse
        u = urlparse(route.request.url)
        p = u.path
        if p == "/statuses/hot/listV2.json":
            body = samples["hot"] if samples else {"items": []}
            route.fulfill(status=200, content_type="application/json", body=_json.dumps(body, ensure_ascii=False))
        elif p == "/statuses/fundx/public/list.json":
            body = samples["rec"] if samples else {"list": []}
            route.fulfill(status=200, content_type="application/json", body=_json.dumps(body, ensure_ascii=False))
        elif p == "/statuses/comments.json":
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"comments": stub_candidates(), "list": []}, ensure_ascii=False))
        elif p == "/api/ingest" or p == "/api/exists" or p == "/api/hits":
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"ok": True, "clues": 0, "exists": [], "missing": []}))
        else:
            route.continue_()

    page.route("**/*", handle)

    page.add_init_script(bridge)
    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)

    # 预置配置 + 把首轮时间设为"很久以前"（模拟错过轮次）
    page.evaluate("""() => {
        window.__gm_store = window.__gm_store || {};
        window.__gm_store['xr3_cfg'] = {
            url: 'https://xueqiu.com/api/ingest', token: 'x',
            threshold: 5, postsPerRound: 1, commentPages: 1,
            minIntervalMin: 90, maxIntervalMin: 120,
            sections: 'recommend,hot', autoStart: true, notify: false,
            batch_min: 2, batch_interval: 5
        };
        // next_run 设为 1 小时前 → 模拟「错过了轮次」
        window.__gm_store['xr3_next_run'] = Date.now() - 3600000;
    }""")

    # 测试专用：把闭包内的 ST 暴露出来，便于直接操控 nextAt
    # （仅修改内存中的副本，正式文件不受影响）
    src = open(SCRIPT, encoding="utf-8").read()
    # 用正则匹配 ST 声明（字段可能变动），避免硬编码字符串失配
    m = re.search(r"^(\s*)const ST = \{[^;]*?\};\s*$", src, re.M)
    assert m, "未找到 ST 声明，脚本结构变了？"
    src = src[:m.end()] + "\n  window.__ST = ST;" + src[m.end():]

    page.add_script_tag(content=src)
    page.wait_for_timeout(2500)

    def log_text():
        try:
            return page.evaluate("() => document.querySelector('#xr3 .log')?.innerText || ''")
        except Exception:
            return ""

    def state():
        return page.evaluate("""() => {
            const s = window.__gm_store || {};
            return {
                next_run: s['xr3_next_run'] || 0,
                now: Date.now(),
                status: document.querySelector('#xr3 .st')?.textContent || '',
            };
        }""")

    print("=" * 62)
    print("场景：模拟错过轮次（next_run 设为 1 小时前）")
    print("=" * 62)
    s = state()
    print(f"  next_run 距今: {(s['now'] - s['next_run'])/60000:.0f} 分钟前（已过期）")
    print(f"  面板状态: {s['status']}")

    print("\n== 1. 触发 visibilitychange（模拟切回标签页）==")
    # 先等首轮跑完
    deadline = time.time() + 90
    while time.time() < deadline:
        s = state()
        if s["next_run"] > s["now"]:
            break
        time.sleep(2)
    # 把内存里的 ST.nextAt 改到过去 → 制造「真错过」状态
    page.evaluate("() => { window.__ST.nextAt = Date.now() - 7200000; }")
    st = page.evaluate("() => ({nextAt: window.__ST.nextAt, now: Date.now(), running: window.__ST.running})")
    print(f"  人为设置 nextAt 距今 {(st['now']-st['nextAt'])/60000:.0f} 分钟前，running={st['running']}")

    n_upload_before = len(uploads)
    page.evaluate("""() => {
        Object.defineProperty(document, 'visibilityState',
            { configurable: true, get: () => 'visible' });
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.wait_for_timeout(2500)
    t = log_text()
    print("  日志尾部:")
    for line in t.split("\n")[-6:]:
        if line.strip():
            print("   ", line)

    caught_up = "立即补跑" in t
    print(f"  >>> {'✓ 正确触发补跑' if caught_up else '✗ 未触发补跑'}")

    print("\n== 2. 补跑期间再触发 visibilitychange（应被 ST.running 挡住）==")
    n_before = len(uploads)
    for _ in range(5):
        page.evaluate("""() => {
            Object.defineProperty(document, 'visibilityState',
                { configurable: true, get: () => 'visible' });
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.wait_for_timeout(200)
    t2 = log_text()
    catch_count = t2.count("立即补跑")
    print(f"  '立即补跑' 出现次数: {t2.count('立即补跑')}（应为 1，补跑只触发一次）")

    print("\n== 3. 等待补跑完成，检查 nextAt 是否被更新 ==")
    deadline = time.time() + 90
    while time.time() < deadline:
        s = state()
        if s["next_run"] > s["now"]:
            break
        time.sleep(2)
    s = state()
    in_future = s["next_run"] > s["now"]
    mins = (s["next_run"] - s["now"]) / 60000
    print(f"  补跑后 next_run 距今: {mins:.0f} 分钟 → {'✓ 已更新到未来（不会重复触发）' if in_future else '✗ 仍是过期值'}")

    print("\n== 4. 上传情况 ==")
    print(f"  上传请求数: {len(uploads)}")
    for u in uploads:
        print(f"    {u}")

    print("\n== 5. 页面错误 ==")
    print(f"  {errors if errors else '✓ 无'}")

    browser.close()
