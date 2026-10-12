-- 为 clues.post_id 建索引：定向回填/查询按 post_id 过滤时避免全表扫描。
-- 背景：2026-10-12 历史回填用 4966 条 UPDATE 逐条按 post_id 更新，因无索引每条全表扫描，
-- 单次读取 1200 万行，打爆 D1 免费版每日 500 万行读取额度。
CREATE INDEX IF NOT EXISTS idx_clues_post_id ON clues(post_id);
