-- Migration: 001_initial_schema.sql
-- Version: 1.0.0
-- Description: 初始 Schema - M1 基础设施层（包含版本管理表）
-- Author: 幽浮酱
-- Date: 2026-02-14

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

-- 1. 创建 Schema 版本管理表
CREATE TABLE IF NOT EXISTS schema_version (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,           -- 版本号（如 "1.0.0"）
    description TEXT,                       -- 变更描述
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT                         -- 执行者（可选）
);

-- 2. 创建主知识表
CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT,
    summary_one_sentence TEXT,
    summary_100_words TEXT,
    keywords TEXT,
    tags TEXT,
    outline TEXT,
    source_type TEXT NOT NULL CHECK(source_type IN (
        'wechat', 'zhihu', 'bilibili', 'webpage',
        'article', 'document', 'generic', 'personal',
        'ai_chat', 'text', 'test'
    )),
    source_url TEXT UNIQUE,
    search_strategy TEXT CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured')),
    file_path TEXT NOT NULL UNIQUE,
    word_count INTEGER DEFAULT 0,
    event_time TIMESTAMP,
    published_at TIMESTAMP,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. 创建内容分块表
CREATE TABLE IF NOT EXISTS content_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    context_before TEXT,
    context_after TEXT,
    section_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, chunk_index)
);

-- 4. 创建标签表
CREATE TABLE IF NOT EXISTS tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    tag_group TEXT,
    count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. 创建知识-标签关联表
CREATE TABLE IF NOT EXISTS knowledge_tags (
    knowledge_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (knowledge_id, tag_id),
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
);

-- 6. 创建视频时间轴表（Phase 2）
CREATE TABLE IF NOT EXISTS video_timestamps (
    timestamp_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL,
    timestamp_seconds INTEGER NOT NULL,
    segment_text TEXT NOT NULL,
    chapter_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, timestamp_seconds)
);

-- 7. 创建 FTS5 全文搜索虚拟表
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    knowledge_id UNINDEXED,
    title,
    content,
    keywords,
    tags,
    tokenize = 'porter unicode61'
);

-- 8. 创建主表索引
CREATE INDEX IF NOT EXISTS idx_knowledge_source_type ON knowledge_items(source_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time);
CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_archived_at ON knowledge_items(archived_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_search_strategy ON knowledge_items(search_strategy);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags ON knowledge_items(tags);

-- 9. 创建分块表索引
CREATE INDEX IF NOT EXISTS idx_chunks_knowledge_id ON content_chunks(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_chunks_index ON content_chunks(knowledge_id, chunk_index);

-- 10. 创建标签关联表索引
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_knowledge_id ON knowledge_tags(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_tag_id ON knowledge_tags(tag_id);

-- 11. 创建视频时间轴索引
CREATE INDEX IF NOT EXISTS idx_timestamps_knowledge_id ON video_timestamps(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_timestamps_time ON video_timestamps(knowledge_id, timestamp_seconds);

-- 12. 创建 FTS5 同步触发器
-- 触发器：插入时同步到 FTS5
CREATE TRIGGER IF NOT EXISTS knowledge_fts_insert
AFTER INSERT ON knowledge_items
BEGIN
    INSERT INTO knowledge_fts (knowledge_id, title, content, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.content, new.keywords, new.tags);
END;

-- 触发器：更新时同步到 FTS5
CREATE TRIGGER IF NOT EXISTS knowledge_fts_update
AFTER UPDATE ON knowledge_items
BEGIN
    DELETE FROM knowledge_fts WHERE knowledge_id = old.knowledge_id;
    INSERT INTO knowledge_fts (knowledge_id, title, content, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.content, new.keywords, new.tags);
END;

-- 触发器：删除时清理 FTS5
CREATE TRIGGER IF NOT EXISTS knowledge_fts_delete
AFTER DELETE ON knowledge_items
BEGIN
    DELETE FROM knowledge_fts WHERE knowledge_id = old.knowledge_id;
END;

-- 13. 插入初始版本记录
INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.0.0', '初始 Schema - M1 基础设施层');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- 注意：初始 Schema 不建议回滚，仅用于全新环境
-- 如果需要清空数据库，执行以下 SQL：

-- DROP TRIGGER IF EXISTS knowledge_fts_delete;
-- DROP TRIGGER IF EXISTS knowledge_fts_update;
-- DROP TRIGGER IF EXISTS knowledge_fts_insert;
-- DROP TABLE IF EXISTS knowledge_fts;
-- DROP TABLE IF EXISTS video_timestamps;
-- DROP TABLE IF EXISTS knowledge_tags;
-- DROP TABLE IF EXISTS tags;
-- DROP TABLE IF EXISTS content_chunks;
-- DROP TABLE IF EXISTS knowledge_items;
-- DROP TABLE IF EXISTS schema_version;
