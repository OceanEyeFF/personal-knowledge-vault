-- Migration: 008_align_fts_contract.sql
-- Version: 1.2.2
-- Description: 统一 FTS 表名与触发器合同到 knowledge_items_fts
-- Author: 幽浮酱
-- Date: 2026-04-01

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

DROP TRIGGER IF EXISTS knowledge_fts_delete;
DROP TRIGGER IF EXISTS knowledge_fts_update;
DROP TRIGGER IF EXISTS knowledge_fts_insert;
DROP TRIGGER IF EXISTS knowledge_items_ad;
DROP TRIGGER IF EXISTS knowledge_items_au;
DROP TRIGGER IF EXISTS knowledge_items_ai;
DROP TABLE IF EXISTS knowledge_items_fts;
DROP TABLE IF EXISTS knowledge_fts;

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

INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
SELECT knowledge_id, title, summary_100_words, keywords, tags
FROM knowledge_items;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.2', '统一 FTS 表名与触发器合同到 knowledge_items_fts');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- SQLite FTS 兼容迁移不提供自动回滚。
