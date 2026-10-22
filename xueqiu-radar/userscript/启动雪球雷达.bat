@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  雪球雷达 · 无人值守启动器
REM  作用：开机后自动打开 Chrome + 雪球页面，油猴脚本即开始后台运行
REM  用法：右键本文件 → 发送到桌面快捷方式；或放到「启动」文件夹
REM ============================================================

set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME%" (
  echo [错误] 未找到 Chrome，请先安装 Google Chrome
  pause
  exit /b 1
)

REM 用独立 profile 运行，避免干扰你日常浏览的 Chrome
REM 如需多账号轮换，可复制本文件改用 Profile 1 / Profile 2
set "PROFILE=Default"

echo [雪球雷达] 正在启动 Chrome（profile: %PROFILE%）
start "" "%CHROME%" --profile-directory="%PROFILE%" --start-maximized "https://xueqiu.com/"

REM 等待页面加载后收起窗口（最小化，不关闭 —— 关掉页面脚本就没了）
timeout /t 25 /nobreak >nul
echo [雪球雷达] Chrome 已启动，油猴脚本应在 5 秒内自动接管
echo.
echo 提示：
echo   · 页面左下角会出现「雪球雷达 v3」面板
echo   · 首次使用请点面板「设置」填入 Worker 地址和 Token
echo   · 不要关闭这个雪球标签页（关掉后脚本停止，重开即恢复）
echo.
pause
