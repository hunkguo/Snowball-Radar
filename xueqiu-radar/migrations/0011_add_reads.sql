-- 已读标记（跨设备共享：状态存服务端，按 clue_id 标记，所有设备同一份进度）
CREATE TABLE IF NOT EXISTS reads (
  clue_id TEXT PRIMARY KEY,
  read_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reads_read_at ON reads(read_at);
