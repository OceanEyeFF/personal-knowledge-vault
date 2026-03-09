-- Migration: 006_add_relations_foundation.sql
-- Version: 1.2.0
-- Description: 新增关系层基础表 knowledge_relations（Phase A / Batch1）
-- Author: 幽浮酱
-- Date: 2026-03-09

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

-- 兼容早期通过 initialize() 创建、但还没有 schema_version 表的数据库
CREATE TABLE IF NOT EXISTS schema_version (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    description TEXT,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_relations (
    relation_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_knowledge_id      INTEGER NOT NULL,
    target_knowledge_id      INTEGER NOT NULL,
    relation_type            TEXT NOT NULL
                            CHECK(relation_type IN (
                                'references',
                                'related_document',
                                'parent_of',
                                'version_of'
                            )),
    relation_source_type     TEXT NOT NULL
                            CHECK(relation_source_type IN (
                                'markdown_link',
                                'frontmatter_related_docs',
                                'frontmatter_field',
                                'manual',
                                'backfill'
                            )),
    direction                TEXT NOT NULL DEFAULT 'directed'
                            CHECK(direction IN ('directed', 'bidirectional')),
    weight                   REAL NOT NULL DEFAULT 1.0
                            CHECK(weight > 0),
    evidence_payload         TEXT NOT NULL DEFAULT '{}',
    created_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (source_knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    FOREIGN KEY (target_knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    CHECK(source_knowledge_id != target_knowledge_id),
    UNIQUE(source_knowledge_id, target_knowledge_id, relation_type, relation_source_type)
);

CREATE INDEX IF NOT EXISTS idx_relations_source_knowledge_id
ON knowledge_relations(source_knowledge_id);

CREATE INDEX IF NOT EXISTS idx_relations_target_knowledge_id
ON knowledge_relations(target_knowledge_id);

CREATE INDEX IF NOT EXISTS idx_relations_type
ON knowledge_relations(relation_type);

CREATE INDEX IF NOT EXISTS idx_relations_source_type
ON knowledge_relations(relation_source_type);

CREATE TRIGGER IF NOT EXISTS trg_knowledge_relations_updated_at
AFTER UPDATE ON knowledge_relations
FOR EACH ROW
BEGIN
    UPDATE knowledge_relations
    SET updated_at = CURRENT_TIMESTAMP
    WHERE relation_id = OLD.relation_id;
END;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.0', '新增关系层基础表 knowledge_relations（Phase A / Batch1）');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- 如需回滚，取消下面的注释并执行：
-- DROP TRIGGER IF EXISTS trg_knowledge_relations_updated_at;
-- DROP INDEX IF EXISTS idx_relations_source_type;
-- DROP INDEX IF EXISTS idx_relations_type;
-- DROP INDEX IF EXISTS idx_relations_target_knowledge_id;
-- DROP INDEX IF EXISTS idx_relations_source_knowledge_id;
-- DROP TABLE IF EXISTS knowledge_relations;
-- DELETE FROM schema_version WHERE version = '1.2.0';
