-- Migration: 007_add_timeline_time_fields.sql
-- Version: 1.2.1
-- Description: 为 knowledge_items 补充 event_time / published_at 真实时间字段
-- Author: 幽浮酱
-- Date: 2026-03-30

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

ALTER TABLE knowledge_items ADD COLUMN event_time TIMESTAMP;
ALTER TABLE knowledge_items ADD COLUMN published_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time);
CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.1', '为 knowledge_items 补充 event_time / published_at 真实时间字段');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- SQLite 不支持直接删除列。
-- 如需回滚，请备份数据后重建 knowledge_items 表并移除 event_time / published_at。
