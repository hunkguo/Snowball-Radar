"""
PyInstaller build script for Xueqiu Scraper
Usage: python build_exe.py
Output: dist/xueqiu_scraper/xueqiu_scraper.exe
"""

import subprocess
import sys
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRAPER_FILE = os.path.join(PROJECT_DIR, "scraper.py")
DIST_DIR = os.path.join(PROJECT_DIR, "dist")
BUILD_DIR = os.path.join(PROJECT_DIR, "build")
APP_NAME = "xueqiu_scraper"
ICON_FILE = os.path.join(PROJECT_DIR, "favicon.ico")

# 使用当前 Python 解释器
PYTHON = sys.executable


def run():
    print("=" * 60)
    print("  Building Xueqiu Scraper EXE")
    print("=" * 60)

    # 检查 scraper.py
    if not os.path.exists(SCRAPER_FILE):
        print(f"ERROR: {SCRAPER_FILE} not found!")
        sys.exit(1)

    # 检查图标
    if not os.path.exists(ICON_FILE):
        print(f"WARNING: {ICON_FILE} not found, building without custom icon")
        icon_arg = []
    else:
        print(f"  Icon: {ICON_FILE} ({os.path.getsize(ICON_FILE) / 1024:.1f} KB)")
        icon_arg = ["--icon", ICON_FILE]

    # 检查 playwright
    result = subprocess.run(
        [PYTHON, "-c", "import playwright; print('OK')"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("ERROR: playwright not installed!")
        print(f"Run: pip install playwright")
        sys.exit(1)
    print("  Playwright: installed")

    # 检查 pyinstaller
    result = subprocess.run(
        [PYTHON, "-c", "import PyInstaller; print(PyInstaller.__version__)"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("ERROR: pyinstaller not installed!")
        print(f"Run: pip install pyinstaller")
        sys.exit(1)
    print(f"  PyInstaller version: {result.stdout.strip()}")

    # 清理上次构建
    print("\n  Cleaning previous build artifacts...")
    for d in [BUILD_DIR, DIST_DIR]:
        if os.path.exists(d):
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", d],
                               capture_output=True, timeout=120)
            else:
                subprocess.run(["rm", "-rf", d], capture_output=True, timeout=120)
    spec_file = os.path.join(PROJECT_DIR, f"{APP_NAME}.spec")
    if os.path.exists(spec_file):
        os.remove(spec_file)
    print("  Done")

    # 构建命令
    # --onedir: 比 --onefile 更可靠（Playwright driver 需要解压）
    # --collect-all playwright: 包含 Node.js driver 和所有数据文件
    # --console: 保留控制台窗口用于用户交互（登录等待）
    cmd = [
        PYTHON, "-m", "PyInstaller",
        "--onedir",
        "--name", APP_NAME,
        "--collect-all", "playwright",
        "--console",
        "--noconfirm",
        "--clean",
        *icon_arg,
        SCRAPER_FILE,
    ]

    print("\n  Running PyInstaller...")
    print(f"  Command: {' '.join(cmd[:6])}...")
    print()

    result = subprocess.run(cmd, cwd=PROJECT_DIR)

    if result.returncode != 0:
        print("\nERROR: PyInstaller build failed!")
        sys.exit(1)

    # 验证输出
    exe_path = os.path.join(DIST_DIR, APP_NAME, f"{APP_NAME}.exe")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"\n{'='*60}")
        print(f"  BUILD SUCCESS!")
        print(f"{'='*60}")
        print(f"  EXE: {exe_path}")
        print(f"  Size: {size_mb:.1f} MB")
        print(f"  Dir:  {os.path.join(DIST_DIR, APP_NAME)}")
        print(f"\n  Usage:")
        print(f"    1. Copy the '{APP_NAME}' folder to any location")
        print(f"    2. Run xueqiu_scraper.exe")
        print(f"    3. Results saved to 'data/' next to the EXE")
        print(f"       - xueqiu.db (SQLite database)")
        print(f"       - exports/xueqiu_export_YYYYMMDD_HHMMSS.json (JSON export)")
        print(f"       - logs/scrape_YYYYMMDD_HHMMSS.log (run log)")
    else:
        print(f"\nERROR: EXE not found at {exe_path}")
        print("Check build output above for errors.")
        sys.exit(1)


if __name__ == "__main__":
    run()
