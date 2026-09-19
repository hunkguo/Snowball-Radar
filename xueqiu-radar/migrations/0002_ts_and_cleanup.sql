-- 雪球雷达 · 线索台 D1 增量迁移（0002）
-- 作用：新增 ts（unix 秒）列，用于「按真实发布时间排序」与「自动清理 10 天前数据」。
--
-- 本地: wrangler d1 migrations apply xueqiu-radar-db --local
-- 线上: wrangler d1 migrations apply xueqiu-radar-db --remote
-- （推荐直接用 `wrangler d1 migrations apply`，会按序跑完 0001 + 0002；
--   0001 全部用 IF NOT EXISTS，对已存在表是空操作，安全）

ALTER TABLE clues ADD COLUMN ts INTEGER DEFAULT 0;
ALTER TABLE raw_comments ADD COLUMN ts INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_clues_ts ON clues(ts DESC);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_comments(ts DESC);
