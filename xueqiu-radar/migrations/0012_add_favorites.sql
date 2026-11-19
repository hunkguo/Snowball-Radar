-- 收藏（跨设备共享，与 reads 同构）；clue_id 为全局去重后的线索 ID
CREATE TABLE IF NOT EXISTS favorites (
  clue_id TEXT PRIMARY KEY,
  fav_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_favorites_fav_at ON favorites(fav_at DESC);
