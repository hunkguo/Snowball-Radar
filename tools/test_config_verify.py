"""
验证「保存配置后立即自检 + 立即跑一轮」：
1. 打开设置 → 填地址和 token → 保存
2. 应在几秒内看到「上传通道验证通过」
3. 首次配置应自动跑一轮
4. 上传成功后状态栏应显示「上传成功」
5. token 错误时应在对话框内直接看到错误（不关闭对话框）
"""
import json
import os
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

SAMPLES = os.path.join(HERE, "real_samples.json")
samples = json.load(open(SAMPLES, encoding="utf-8")) if os.path.exists(SAMPLES) else None

# 记录各接口调用
calls = {"exists": 0, "ingest": 0, "hits": 0}
wrong_token_mode = {"on": False}


def make_stub():
    def stub_candidates():
        return [{"id": f"w{i}", "user_name": f"用户{i}", "text": f"宁德时代订单超预期 {i}",
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
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))
        elif p == "/statuses/fundx/public/list.json":
            body = samples["rec"] if samples else {"list": []}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))
        elif p == "/statuses/comments.json":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"comments": stub_candidates(), "list": []}, ensure_ascii=False))
        elif p == "/api/exists":
            calls["exists"] += 1
            if wrong_token_mode["on"]:
                route.fulfill(status=401, content_type="application/json", body='{"error":"unauthorized"}')
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"exists": [], "missing": ["__xr3_probe__"], "total": 1}))
        elif p == "/api/ingest":
            calls["ingest"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "clues": 4, "inserted": 4, "updated": 0}))
        elif p == "/api/hits":
            calls["hits"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        else:
            route.continue_()

    return handle


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=False,
                                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    ctx = browser.new_context(user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
                              viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/*", make_stub())

    page.add_init_script(bridge)
    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    page.add_script_tag(content=open(SCRIPT, encoding="utf-8").read())
    page.wait_for_timeout(2500)

    def log_text():
        return page.evaluate("() => document.querySelector('#xr3 .log')?.innerText || ''")

    def status_text():
        return page.evaluate("() => document.querySelector('#xr3 .st')?.textContent || ''")

    def dlg_msg():
        return page.evaluate("() => document.querySelector('#xr3-cfg .msg')?.textContent || ''")

    print("=" * 64)
    print("场景 A：token 错误 → 应在对话框内直接报错，不关闭")
    print("=" * 64)
    wrong_token_mode["on"] = True
    page.evaluate("() => { const b=[...document.querySelectorAll('#xr3 button')].find(x=>x.textContent.includes('设置')); if(b) b.click(); }")
    page.wait_for_timeout(1000)
    page.fill("#xr3-url", "https://xueqiu.com/api/ingest")
    page.fill("#xr3-token", "bad-token")
    page.click('[data-t="save"]')
    page.wait_for_timeout(4000)
    msg = dlg_msg()
    print(f"  对话框提示: {msg!r}")
    still_open = page.locator("#xr3-cfg").count() > 0
    print(f"  对话框保持打开: {'✓' if still_open else '✗'}")
    print(f"  >>> {'✓ 错误就地提示，可直接改' if '401' in msg and still_open else '✗ 表现不符预期'}")

    print()
    print("=" * 64)
    print("场景 B：token 正确 → 应验证通过 + 自动跑一轮 + 状态栏显示上传成功")
    print("=" * 64)
    wrong_token_mode["on"] = False
    page.fill("#xr3-token", "good-token")
    calls["ingest"] = 0
    page.click('[data-t="save"]')
    page.wait_for_timeout(2000)
    print(f"  保存后 2 秒，exists 探针调用次数: {calls['exists']}")

    # 等待自动跑一轮完成
    deadline = time.time() + 90
    while time.time() < deadline:
        if "上传成功" in log_text():
            break
        time.sleep(2)
    t = log_text()
    print("\n  日志尾部:")
    for line in t.split("\n")[-10:]:
        if line.strip():
            print("   ", line)

    verified = "上传通道验证通过" in t
    uploaded = "上传成功" in t
    print(f"\n  >>> 验证通过: {'✓' if verified else '✗'}")
    print(f"  >>> 自动跑一轮并上传成功: {'✓' if uploaded else '✗'}")
    print(f"  >>> ingest 调用次数: {calls['ingest']}")
    print(f"  状态栏: {status_text()!r}")
    print(f"  对话框已关闭: {'✓' if page.locator('#xr3-cfg').count() == 0 else '✗'}")

    print("\n  面板 hint:")
    hint = page.evaluate("() => document.querySelector('#xr3 .hint')?.innerText || ''")
    for line in hint.split("\n"):
        if line.strip():
            print("   ", line)

    print(f"\n  页面错误: {errors if errors else '✓ 无'}")
    browser.close()
