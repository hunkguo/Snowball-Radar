@echo off
REM ==========================================================================
REM  雪球雷达 · 后台保活专用 Chrome 启动器
REM
REM  作用：用「禁用后台节流 / 禁用后台冻结 / 禁用窗口遮挡判定」的参数启动一个
REM        **独立的** Chrome 实例，这样即使遇到下列情况脚本也照常抓：
REM          - 电脑锁屏
REM          - 远程桌面连过去（本地会话窗口被判定为不可见）
REM          - 标签页在桌面 2 / 不是最前端 / 被别的窗口完全遮挡
REM
REM  为什么用独立 profile：和日常用的 Chrome 完全隔离，你平时关浏览器、
REM  开一堆标签都不会影响它，它自己常驻即可。
REM
REM  用法：双击本文件。第一次需要在这个窗口里登录一次雪球（之后一直保留）。
REM ==========================================================================

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [X] 没找到 chrome.exe，请修改本文件里的 CHROME 路径。
  pause
  exit /b 1
)

REM 独立 profile 目录（已被 .gitignore 忽略，不会进仓库）
set "PROF=%~dp0..\..\chrome-radar-profile"

REM 关键参数（缺一个都可能被节流）：
REM   --disable-background-timer-throttling   后台定时器不降频
REM   --disable-backgrounding-occluded-windows 窗口被遮挡/在别的虚拟桌面也当作前台
REM   --disable-renderer-backgrounding         不把后台标签置为低优先级（防冻结）
REM   --disable-features=CalculateNativeWinOcclusion  不做原生窗口遮挡计算
REM   --disable-features=IntensiveWakeUpThrottling    不做 1 分钟对齐的强制节流
REM   --autoplay-policy=no-user-gesture-required      允许脚本的保活音频自动播放
set "FLAGS=--disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check --disable-ipc-flooding-protection"

echo 正在启动「后台保活 Chrome」（独立 profile）...
echo profile: %PROF%
echo 提示：把它开着就行，可以最小化；锁屏 / 远程桌面 / 切虚拟桌面都不影响。
start "" "%CHROME%" --user-data-dir="%PROF%" %FLAGS% https://www.xueqiu.com/
