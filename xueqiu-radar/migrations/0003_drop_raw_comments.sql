-- 雪球雷达 · 线索台 D1 迁移（0003）
-- 作用：删除 raw_comments 表。
--
-- 该表自 0001 起随 ingest 写入，但前端 / 所有 API 从不读取（只写不读），
-- 属于死数据。clues 已保存全部过阈值的候选评论，raw_comments 仅为全量原始集，
-- 当前无用途。删除以精简存储与每轮写入量。
--
-- 本地: wrangler d1 migrations apply xueqiu-radar-db --local
-- 线上: wrangler d1 migrations apply xueqiu-radar-db --remote

DROP TABLE IF EXISTS raw_comments;
