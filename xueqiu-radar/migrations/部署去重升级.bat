@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo  雪球雷达 · 去重升级部署
echo  1) 应用数据库迁移 0009（全局去重 + 集中写入优化）
echo  2) 部署新版 Worker（含 /api/exists 与 /api/hits）
echo ============================================================
echo.

where wrangler >nul 2>nul
if errorlevel 1 (
  echo [!] 未找到 wrangler，请先安装：
  echo     npm i -g wrangler
  echo.
  pause
  exit /b 1
)

echo [1/2] 应用迁移到【线上】D1 ...
call wrangler d1 migrations apply xueqiu-radar-db --remote
if errorlevel 1 (
  echo [!] 迁移失败，已中止（未部署 Worker，避免版本不匹配）
  pause
  exit /b 1
)

echo.
echo [2/2] 部署 Worker ...
call wrangler deploy
if errorlevel 1 (
  echo [!] Worker 部署失败
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  完成。
echo.
echo  本次升级内容：
echo   · clues 表加 clue_id 唯一索引 - 跨轮同一条评论只存一份
echo   · 重复线索走 UPSERT，只更新命中次数，不新增行
echo   · 所有写入合并为最少 DB.batch 事务（省 99% 往返）
echo   · 新增 /api/exists 探针 - 客户端只上传库里没有的
echo   · 新增 /api/hits 上报 - 统计线索被反复命中的次数
echo   · 前端线索卡片显示 ×N 标记（被多轮抓到的热度）
echo.
echo  注意：油猴脚本需同步更新为 v3 才有客户端攒批与探针，
echo        未更新也能用（服务端有兜底去重）。
echo ============================================================
pause
