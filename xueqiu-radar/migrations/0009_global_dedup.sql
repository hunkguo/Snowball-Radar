-- 雪球雷达 · 线索台 D1 迁移（0009）· 全局去重 + 集中写入优化
-- 作用：
--   1) clues 表增加 clue_id 唯一索引 → 同一条评论跨轮只存一份（全局去重）
--   2) 清理迁移前已存在的重复行（保留最早出现的一条）
--   3) 增加 clues.last_seen_round / hit_count → 记录"重复命中"但不重复存行
--   4) rounds 表增加 deduped_count → 记录本轮被判定为重复而跳过的条数
--
-- 背景（2026-10-22 实测）：
--   原主键为 (round_id, clue_id)，只能保证「轮内幂等」。
--   雪球热门帖会在多轮抓取中反复出现，同一条评论（clue_id 相同）
--   在不同 round_id 下会被插入多行 → 前端列表出现重复条目，
--   且每轮都在重复消耗 D1 写入额度（免费版每日 10 万行写）。
--
-- 本地: wrangler d1 migrations apply xueqiu-radar-db --local
-- 线上: wrangler d1 migrations apply xueqiu-radar-db --remote

-- ────────────────────────────────────────────────────────────
-- 1) 先清理历史重复行
--    保留 ts 最小（最早出现）的那条，其余删除
-- ────────────────────────────────────────────────────────────
DELETE FROM clues
WHERE rowid NOT IN (
  SELECT MIN(rowid) FROM clues GROUP BY clue_id
);

-- ────────────────────────────────────────────────────────────
-- 2) 新增追踪列
-- ────────────────────────────────────────────────────────────
-- last_seen_round：最近一次命中该线索的轮次（便于追溯"何时又被抓到"）
ALTER TABLE clues ADD COLUMN last_seen_round TEXT DEFAULT '';
-- hit_count：该线索累计被各轮命中的次数（>1 说明它反复出现在热门帖里）
ALTER TABLE clues ADD COLUMN hit_count INTEGER DEFAULT 1;
-- first_seen_round：首次出现的轮次（等价于 round_id，冗余一份便于查询）
ALTER TABLE clues ADD COLUMN first_seen_round TEXT DEFAULT '';

UPDATE clues SET first_seen_round = round_id WHERE first_seen_round IS NULL OR first_seen_round = '';

-- ────────────────────────────────────────────────────────────
-- 3) clue_id 全局唯一索引（去重的核心）
--    注意：这是 UNIQUE 而非 PRIMARY KEY，因为 SQLite 的
--    "INSERT OR REPLACE" 在存在多个唯一约束时行为不可预期；
--    改用 "INSERT ... ON CONFLICT(clue_id) DO UPDATE" 明确语义。
-- ────────────────────────────────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS idx_clues_clue_id ON clues(clue_id);

-- ────────────────────────────────────────────────────────────
-- 4) rounds 记录本轮去重统计（前端可展示"本轮新增 N 条 / 重复 M 条"）
-- ────────────────────────────────────────────────────────────
ALTER TABLE rounds ADD COLUMN deduped_count INTEGER DEFAULT 0;

-- ────────────────────────────────────────────────────────────
-- 5) 索引优化：集中写入 + 按时间排序查询
-- ────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_clues_ts_score ON clues(ts DESC, score DESC);
CREATE INDEX IF NOT EXISTS idx_clues_section_ts ON clues(section, ts DESC);
