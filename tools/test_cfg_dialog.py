"""
验证新版设置对话框：真实 Chrome 打开雪球页面 → 点「设置」→ 截图 + 断言
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "xueqiu-radar", "userscript", "xueqiu-radar-auto.user.js")
GM_BRIDGE = os.path.join(HERE, "gm_bridge.js")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# 扩展 GM 桥接：加 GM_setClipboard
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

    page.add_init_script(bridge)
    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)

    page.add_script_tag(content=open(SCRIPT, encoding="utf-8").read())
    page.wait_for_timeout(2500)

    print("== 1. 打开设置对话框 ==")
    page.evaluate("() => { const b=[...document.querySelectorAll('#xr3 button')].find(x=>x.textContent.includes('设置')); if(b) b.click(); }")
    page.wait_for_timeout(1200)

    dlg = page.locator("#xr3-cfg").count()
    print(f"   对话框渲染: {'✓' if dlg else '✗'}")
    if not dlg:
        print("   错误:", errors[:3]); browser.close(); sys.exit(1)

    # 字段检查
    fields = {
        "地址输入框": "#xr3-url",
        "Token输入框(应为password)": "#xr3-token",
        "测试按钮": '[data-t="test"]',
        "导出按钮": '[data-t="export"]',
        "导入按钮": '[data-t="import"]',
        "保存按钮": '[data-t="save"]',
    }
    for name, sel in fields.items():
        print(f"   {name}: {'✓' if page.locator(sel).count() else '✗'}")

    tok_type = page.evaluate("() => document.querySelector('#xr3-token')?.type")
    print(f"   Token 输入框类型: {tok_type}  {'✓ 遮蔽' if tok_type == 'password' else '✗ 明文!'}")

    # 地址默认应为空
    url_val = page.evaluate("() => document.querySelector('#xr3-url')?.value")
    tok_val = page.evaluate("() => document.querySelector('#xr3-token')?.value")
    print(f"   默认地址: {url_val!r}  {'✓ 空' if not url_val else '✗ 有默认值'}")
    print(f"   默认 Token: {tok_val!r}  {'✓ 空' if not tok_val else '✗ 有默认值'}")

    page.screenshot(path=os.path.join(HERE, "cfg_dialog.png"))

    print("\n== 2. 测试连通性（故意用错 token，应报 401）==")
    page.evaluate("() => { document.querySelector('#xr3-url').value='https://127.0.0.1:8799/api/ingest'; document.querySelector('#xr3-token').value='wrong-token'; }")
    page.click('[data-t="test"]')
    page.wait_for_timeout(6000)
    msg = page.evaluate("() => document.querySelector('#xr3-cfg .msg')?.textContent")
    print(f"   提示: {msg}")

    print("\n== 3. 保存配置（不填 → 应提示未配置）==")
    page.evaluate("() => { document.querySelector('#xr3-url').value=''; document.querySelector('#xr3-token').value=''; }")
    page.click('[data-t="save"]')
    page.wait_for_timeout(800)
    panel_log = page.evaluate("() => document.querySelector('#xr3 .log')?.innerText || ''")
    for line in panel_log.split("\n")[-3:]:
        if line.strip():
            print("  ", line)

    print("\n== 4. 对话框已关闭 ==")
    print(f"   关闭: {'✓' if page.locator('#xr3-cfg').count() == 0 else '✗'}")

    print("\n== 5. 页面错误 ==")
    print(f"   {errors if errors else '✓ 无'}")

    browser.close()
