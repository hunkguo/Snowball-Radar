@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  雪球雷达 · 无人值守启动器（v2 · 后台保活版）
REM
REM  作用：用「禁用后台节流」的参数启动 Chrome + 雪球页面，
REM        这样锁屏 / 远程桌面 / 标签页在桌面 2 非最前端时脚本照常跑。
REM
REM  ⚠ 关键：这些参数**只有在 Chrome 完全退出后的首次启动**才生效。
REM    如果 Chrome 已经在运行，新命令会被转交给已运行的实例，
REM    参数全部丢失（脚本看起来在跑，但一锁屏就停）。
REM    所以本脚本会先检查，并询问是否退出重启。
REM
REM  想保留日常 Chrome 不关？改用 radar-chrome.bat（独立 profile，
REM  与你日常 Chrome 并存，但需要重新登录雪球并重填 Worker 配置）。
REM ============================================================

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [错误] 未找到 Chrome，请先安装 Google Chrome
  pause
  exit /b 1
)

REM ── 反节流 / 防冻结参数 ──
REM   --disable-background-timer-throttling      后台定时器不降频
REM   --disable-backgrounding-occluded-windows    窗口被遮挡或在别的虚拟桌面也算前台
REM   --disable-renderer-backgrounding            不把后台标签降级（防冻结）
REM   --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling
REM   --autoplay-policy=no-user-gesture-required  允许脚本的保活音频自动播放
set "FLAGS=--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check"

REM ── 检查 Chrome 是否已在运行 ──
tasklist /FI "IMAGENAME eq chrome.exe" 2>nul | find /I "chrome.exe" >nul
if errorlevel 1 goto :START

echo.
echo [!] 检测到 Chrome 正在运行。
echo     反节流参数只在 Chrome 完全退出后的首次启动生效，
echo     否则会被转交给已运行的实例，参数全部丢失。
echo.
set /p "ANS=是否退出所有 Chrome 并以保活参数重新启动？(Y/N): "
if /I not "%ANS%"=="Y" (
  echo 已取消。若继续用当前 Chrome，脚本在锁屏后可能被节流或冻结。
  pause
  exit /b 0
)
echo 正在关闭 Chrome...
taskkill /F /IM chrome.exe >nul 2>&1
timeout /t 4 /nobreak >nul

:START
set "PROFILE=Default"
echo.
echo [雪球雷达] 正在启动 Chrome（profile: %PROFILE%，已启用后台保活参数）
start "Xueqiu Radar" "%CHROME%" --profile-directory="%PROFILE%" --start-maximized "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling" %FLAGS% "https://xueqiu.com/"

timeout /t 8 /nobreak >nul
echo [雪球雷达] Chrome 已启动，油猴脚本应在数秒内接管
echo.
echo 提示：
echo   · 页面左下角会出现「雪球雷达 v3」面板
echo   · 面板那行应显示：主控 本页 · 页面 可见 · 保活 开
echo   · 不要关闭这个雪球标签页（关掉后脚本停止，重开即恢复）
echo   · 启动后可以最小化、锁屏、远程桌面，都不影响
echo.
pause
