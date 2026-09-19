-- 雪球雷达 · 线索台 D1 建表
-- 用法：
--   本地: wrangler d1 execute xueqiu-radar-db --local  --file=./migrations/0001_init.sql
--   线上: wrangler d1 execute xueqiu-radar-db --remote --file=./migrations/0001_init.sql

-- 轮次元信息（每轮 exe 上传一次）
CREATE TABLE IF NOT EXISTS rounds (
  round_id        TEXT PRIMARY KEY,
  source          TEXT,            -- recommend | hashtag
  title           TEXT,
  hashtag         TEXT,
  generated_at    TEXT,
  candidate_count INTEGER,
  total_comments  INTEGER,
  created_at      INTEGER
);

-- Layer1 打分后的候选线索（按 (round_id, clue_id) 幂等覆盖）
CREATE TABLE IF NOT EXISTS clues (
  round_id   TEXT NOT NULL,
  clue_id    TEXT NOT NULL,
  user_name  TEXT,
  time_str   TEXT,
  like_count INTEGER DEFAULT 0,
  reply_count INTEGER DEFAULT 0,
  text       TEXT,
  score      INTEGER DEFAULT 0,
  tags       TEXT,                -- JSON 数组字符串
  stocks     TEXT,                -- JSON 数组字符串
  section    TEXT,
  PRIMARY KEY (round_id, clue_id)
);

-- 原始评论全量（按 id 全局去重，跨轮只更新 round_id 指向最新一轮）
CREATE TABLE IF NOT EXISTS raw_comments (
  id          TEXT PRIMARY KEY,
  round_id    TEXT,
  user_name   TEXT,
  time_str    TEXT,
  like_count  INTEGER DEFAULT 0,
  reply_count INTEGER DEFAULT 0,
  text        TEXT,
  hashtag     TEXT,
  post_id     TEXT,
  created_at  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_clues_round   ON clues(round_id);
CREATE INDEX IF NOT EXISTS idx_clues_stock   ON clues(stocks);
CREATE INDEX IF NOT EXISTS idx_rounds_created ON rounds(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_round     ON raw_comments(round_id);
