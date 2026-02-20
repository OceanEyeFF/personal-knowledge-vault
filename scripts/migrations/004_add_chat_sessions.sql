-- Version: 1.1.0
-- Description: 添加 AI 对话会话表（M12 - AI 对话功能）
-- Author: Claude Code
-- Date: 2026-02-20

-- ============================================================
-- 向上迁移（创建 chat_sessions 表）
-- ============================================================

-- 创建 chat_sessions 表
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,                           -- UUID 格式主键
    title TEXT NOT NULL,                                   -- 会话标题（用户可编辑）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,        -- 创建时间
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,        -- 更新时间
    messages TEXT NOT NULL,                                -- 完整对话历史（JSON 格式）
    summary TEXT,                                          -- AI 生成的精粹版本（可选）
    total_tokens INTEGER DEFAULT 0,                        -- 累计 Token 消耗
    round_count INTEGER DEFAULT 0,                         -- 对话轮数（User 消息计数）
    is_archived BOOLEAN DEFAULT 0,                         -- 归档标志（0=活跃，1=归档）
    knowledge_id INTEGER,                                  -- 可选关联：关联到 knowledge_items
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL,
    CHECK(round_count >= 0),                               -- 确保轮数非负
    CHECK(total_tokens >= 0)                               -- 确保 Token 数非负
);

-- 创建索引（优化查询性能）
CREATE INDEX IF NOT EXISTS idx_chat_created_at ON chat_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_updated_at ON chat_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_is_archived ON chat_sessions(is_archived);
CREATE INDEX IF NOT EXISTS idx_chat_knowledge_id ON chat_sessions(knowledge_id);

-- ============================================================
-- 向下迁移（回滚）
-- ============================================================

-- 注释：如需回滚，取消下面的注释并执行
-- DROP INDEX IF EXISTS idx_chat_knowledge_id;
-- DROP INDEX IF EXISTS idx_chat_is_archived;
-- DROP INDEX IF EXISTS idx_chat_updated_at;
-- DROP INDEX IF EXISTS idx_chat_created_at;
-- DROP TABLE IF EXISTS chat_sessions;
