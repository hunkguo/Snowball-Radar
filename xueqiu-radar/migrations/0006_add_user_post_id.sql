-- 候选线索增加 user_id / post_id，供前端拼「用户主页 / 原文」跳转链接
-- user_id：评论者数值型 ID，用户主页 https://xueqiu.com/u/<user_id>
-- post_id：帖子 ID（话题引擎提供；推荐/热门引擎取评论所属帖）
-- 注：clue_id 即评论 ID，原文页 https://xueqiu.com/<user_id>/<clue_id>（评论含所属帖上下文）
ALTER TABLE clues ADD COLUMN IF NOT EXISTS user_id TEXT DEFAULT '';
ALTER TABLE clues ADD COLUMN IF NOT EXISTS post_id TEXT DEFAULT '';
