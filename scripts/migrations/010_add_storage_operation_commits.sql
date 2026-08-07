-- Migration: 010_add_storage_operation_commits.sql
-- Version: 1.2.4
-- Description: 新增跨存储操作提交凭据，消除 SQLite 提交结果歧义
-- Author: Codex
-- Date: 2026-08-03

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

CREATE TABLE IF NOT EXISTS storage_operation_commits (
    operation_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK(action IN ('archive', 'delete')),
    knowledge_id INTEGER NOT NULL,
    relative_file_path TEXT NOT NULL,
    projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256) = 64),
    committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_storage_operation_knowledge_id
ON storage_operation_commits(knowledge_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.4', '新增跨存储操作提交凭据，消除 SQLite 提交结果歧义');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- Developer Preview 不执行历史库原地降级。
