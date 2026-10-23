@echo off
REM ==========================================================================
REM  Xueqiu Radar -- keep-awake Chrome launcher
REM
REM  Starts an ISOLATED Chrome instance with background-throttling disabled,
REM  so the userscript keeps running even when:
REM    - the PC is locked
REM    - you connect via Remote Desktop
REM    - the tab sits on virtual desktop 2 / is not the foreground window
REM
REM  Usage: double-click this file. Log in to Xueqiu ONCE in the new window.
REM  (Full Chinese explanation: README-?????.md, section 0)
REM ==========================================================================

cd /d "%~dp0"

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [X] chrome.exe not found. Please edit the CHROME path in this file.
  echo     Current: "%CHROME%"
  pause
  exit /b 1
)

REM Isolated profile dir (gitignored). Convert to an absolute path first.
set "PROF=%~dp0..\..\chrome-radar-profile"
for %%I in ("%PROF%") do set "PROFABS=%%~fI"

echo.
echo Starting keep-awake Chrome with an isolated profile...
echo   chrome : %CHROME%
echo   profile: %PROFABS%
echo.

REM Use cmd's own START (fully detached) as the primary path.
REM Note: the --disable-features value is wrapped in quotes so cmd.exe does not
REM treat "=" and "," inside it as argument delimiters.
start "Xueqiu Radar" "%CHROME%" --user-data-dir="%PROFABS%" --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling" --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check "https://www.xueqiu.com/"

if errorlevel 1 (
  echo [!] START failed, falling back to PowerShell...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%CHROME%' -ArgumentList @('--user-data-dir=%PROFABS%','--disable-background-timer-throttling','--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding','--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling','--autoplay-policy=no-user-gesture-required','--no-first-run','--no-default-browser-check','https://www.xueqiu.com/')"
)

echo.
echo Chrome should open in a moment. If nothing happens, read the message above.
echo NOTE: this window is a NEW isolated Chrome -- log in to Xueqiu once here.
timeout /t 8 >nul
