-- 雪球雷达 · 线索台 D1 迁移（0004）
-- 作用：clues 表新增 date 列（YYYY-MM-DD，评论真实发布日期，北京时间），用于前端
--       按日期分组与「今天/昨天/X小时前」相对显示，彻底消除「未知日期」。
--
-- 本地: wrangler d1 migrations apply xueqiu-radar-db --local
-- 线上: wrangler d1 migrations apply xueqiu-radar-db --remote

ALTER TABLE clues ADD COLUMN date TEXT DEFAULT '';

-- 对历史行回填：time_str 形如 "2026-09-19 21:16:48"（推荐板块，含完整年月日）的取前 10 位；
-- 话题板块 time_str 为 "09-19 21:16"（无年份）的留空，由前端用当前年兜底（极少出现）。
UPDATE clues SET date = substr(time_str, 1, 10) WHERE time_str LIKE '____-__-__ %' AND (date IS NULL OR date = '');
