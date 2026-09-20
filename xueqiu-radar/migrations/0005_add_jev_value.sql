-- 候选线索增加 Jev 价值判断分（0~1，A股投资参考价值概率）
-- opt-in：仅当 xueqiu.exe 启用了 --jev 且携带 key 时才有值，其余默认 0
ALTER TABLE clues ADD COLUMN jev_value REAL DEFAULT 0;
