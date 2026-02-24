-- Migration 005: Add Review System
-- 审核系统数据库迁移
--
-- 新增两张表:
-- 1. review_queue     - 审核队列（存放待审核的 AI 生成内容）
-- 2. review_history   - 审核操作历史（记录每次用户操作）
--
-- 关联: review_queue.knowledge_id -> knowledge_items.knowledge_id (可为 NULL，草稿未入库)

-- 审核队列表
CREATE TABLE IF NOT EXISTS review_queue (
    review_id               INTEGER PRIMARY KEY AUTOINCREMENT,

    -- AI 生成内容（必填）
    ai_generated_summary    TEXT NOT NULL,
    ai_generated_tags       TEXT NOT NULL DEFAULT '',    -- 逗号分隔字符串
    source_type             TEXT NOT NULL DEFAULT 'unknown',

    -- AI 生成的清洗内容（可选）
    ai_cleaned_content      TEXT NOT NULL DEFAULT '',

    -- AI 模型信息
    ai_generation_model     TEXT NOT NULL DEFAULT 'deepseek-chat',

    -- 内容预览（原始内容前 500 字）
    original_content_preview TEXT NOT NULL DEFAULT '',

    -- 来源信息（可选）
    source_url              TEXT,
    knowledge_id            INTEGER,                     -- 关联知识条目（审核通过后填充）

    -- 用户审核内容（可选，用户修改前为 NULL）
    user_summary            TEXT,
    user_tags               TEXT,                        -- 逗号分隔字符串
    user_comments           TEXT,

    -- AI 重新生成追踪
    regeneration_count      INTEGER NOT NULL DEFAULT 0,
    regeneration_prompts    TEXT NOT NULL DEFAULT '[]',  -- JSON 数组字符串，记录每次重生成的 prompt

    -- 审核状态
    review_status           TEXT NOT NULL DEFAULT 'pending'
                            CHECK(review_status IN ('pending', 'approved', 'rejected', 'draft')),
    review_version          INTEGER NOT NULL DEFAULT 1,

    -- 时间戳
    created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP

    -- 外键约束（knowledge_id 可为 NULL，表示未入库的草稿）
    -- 注意：knowledge_items 表由 001_initial_schema.sql 创建，此处仅作文档说明
    -- FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL
);

-- 审核历史表
CREATE TABLE IF NOT EXISTS review_history (
    history_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       INTEGER NOT NULL,           -- 关联审核队列
    action          TEXT NOT NULL,              -- 操作类型: edit_summary, edit_tags, add_comment, regenerate, approve, reject, restore
    details         TEXT NOT NULL DEFAULT '',   -- 操作详情（JSON 格式）
    operator        TEXT NOT NULL DEFAULT 'user',   -- 操作人: user / system
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (review_id) REFERENCES review_queue(review_id) ON DELETE CASCADE
);

-- 索引：按状态查询（列出草稿、待审核等）
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(review_status);

-- 索引：按知识条目 ID 查询
CREATE INDEX IF NOT EXISTS idx_review_queue_knowledge_id ON review_queue(knowledge_id);

-- 索引：按创建时间倒序查询
CREATE INDEX IF NOT EXISTS idx_review_queue_created_at ON review_queue(created_at DESC);

-- 索引：历史记录按 review_id 查询
CREATE INDEX IF NOT EXISTS idx_review_history_review_id ON review_history(review_id);

-- 触发器：更新 review_queue 时自动刷新 updated_at
CREATE TRIGGER IF NOT EXISTS trg_review_queue_updated_at
AFTER UPDATE ON review_queue
FOR EACH ROW
BEGIN
    UPDATE review_queue SET updated_at = CURRENT_TIMESTAMP WHERE review_id = OLD.review_id;
END;
