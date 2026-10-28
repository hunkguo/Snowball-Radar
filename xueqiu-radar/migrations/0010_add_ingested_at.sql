-- 0010_add_ingested_at.sql
-- 目的：把「数据保留」的计时口径从「评论/帖子时间」改成「入库时间」。
--
-- 背景（2026-10-28 排查）：
--   旧清理逻辑 DELETE FROM clues WHERE ts < now-10天，用的是评论自身时间。
--   但热帖下的老评论可能今天才被抓到（ts 是 10 天前），刚入库就被判定"过期"，
--   当晚 00:00 的 Cron 立刻删掉 → 计数出现「今天涨、明天掉」的抖动，
--   而且「今天新收集的数据」竟然会被"追溯删除"，不符合直觉。
--
-- 改法：新增 ingested_at（毫秒），首次入库时写入；清理按它来算 10 天。
--       upsert 命中已存在的行不更新该字段（保住"首次收集时间"）。

ALTER TABLE clues ADD COLUMN ingested_at INTEGER NOT NULL DEFAULT 0;

-- 回填历史行：用首现轮次的 created_at（rounds.created_at 本来就是毫秒级入库时间）
UPDATE clues
   SET ingested_at = COALESCE(
         (SELECT r.created_at FROM rounds r WHERE r.round_id = clues.first_seen_round),
         0
       )
 WHERE ingested_at = 0;
