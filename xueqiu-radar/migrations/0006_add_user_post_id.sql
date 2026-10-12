-- 候选线索增加 user_id / post_id，供前端拼「用户主页 / 原文」跳转链接
-- user_id：评论者数值型 ID，用户主页 https://xueqiu.com/u/<user_id>
-- post_id：帖子 ID（话题引擎提供；推荐/热门引擎取评论所属帖）
-- 注：原文页正确地址是 /<帖子作者ID>/<帖子ID>（见 0007 的 post_author_id），
--     此前误用「评论者ID + 评论ID（clue_id）」会 404。
ALTER TABLE clues ADD COLUMN user_id TEXT DEFAULT '';
ALTER TABLE clues ADD COLUMN post_id TEXT DEFAULT '';
