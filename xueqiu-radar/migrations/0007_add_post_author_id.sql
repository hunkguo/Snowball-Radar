-- 候选线索增加 post_author_id（帖子作者数值型 ID），
-- 供前端拼「原文」跳转链接。雪球原文地址格式为 /<帖子作者ID>/<帖子ID>，
-- 此前误用「评论者ID + 评论ID」（clue_id）导致 404。
ALTER TABLE clues ADD COLUMN post_author_id TEXT DEFAULT '';
