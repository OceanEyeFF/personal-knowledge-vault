-- Migration: 009_repair_fts_storage_contract.sql
-- Version: 1.2.3
-- Description: 修复 knowledge_items_fts 存储合同并清理重复索引
-- Author: 幽浮酱
-- Date: 2026-04-03

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

DROP TRIGGER IF EXISTS knowledge_items_ad;
DROP TRIGGER IF EXISTS knowledge_items_au;
DROP TRIGGER IF EXISTS knowledge_items_ai;
DROP TABLE IF EXISTS knowledge_items_fts;

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_items_fts USING fts5(
    title,
    summary_100_words,
    keywords,
    tags
);

CREATE TRIGGER IF NOT EXISTS knowledge_items_ai
AFTER INSERT ON knowledge_items
BEGIN
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_items_au
AFTER UPDATE ON knowledge_items
BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_items_ad
AFTER DELETE ON knowledge_items
BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
END;

DROP INDEX IF EXISTS idx_source_type;
DROP INDEX IF EXISTS idx_event_time;
DROP INDEX IF EXISTS idx_published_at;
DROP INDEX IF EXISTS idx_archived_at;
DROP INDEX IF EXISTS idx_search_strategy;
DROP INDEX IF EXISTS idx_knowledge_chunk;
DROP INDEX IF EXISTS idx_knowledge_id;
DROP INDEX IF EXISTS idx_knowledge_timestamp;
DROP INDEX IF EXISTS idx_vt_knowledge_id;

CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url);
CREATE INDEX IF NOT EXISTS idx_knowledge_source_type ON knowledge_items(source_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time);
CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_archived_at ON knowledge_items(archived_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_search_strategy ON knowledge_items(search_strategy);
CREATE INDEX IF NOT EXISTS idx_file_path ON knowledge_items(file_path);
CREATE INDEX IF NOT EXISTS idx_chunks_knowledge_id ON content_chunks(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_chunks_index ON content_chunks(knowledge_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_knowledge_id ON knowledge_tags(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_tag_id ON knowledge_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_timestamps_knowledge_id ON video_timestamps(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_timestamps_time ON video_timestamps(knowledge_id, timestamp_seconds);

INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
SELECT knowledge_id, title, summary_100_words, keywords, tags
FROM knowledge_items;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.3', '修复 knowledge_items_fts 存储合同并清理重复索引');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- SQLite FTS 合同修复迁移不提供自动回滚。
