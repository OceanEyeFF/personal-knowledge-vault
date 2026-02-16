-- Migration: 002_add_cli_tables.sql
-- Version: 1.1.0
-- Description: 新增 CLI 使用统计表（M6 CLI 入口与交互界面）
-- Author: 幽浮酱
-- Date: 2026-02-16

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

-- 1. 创建 CLI 命令历史表
CREATE TABLE IF NOT EXISTS cli_command_history (
    command_id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,                  -- 命令名称（archive/search/list/show/config/stats）
    args TEXT,                              -- 命令参数（JSON 格式）
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    execution_time_ms INTEGER,              -- 执行时长（毫秒）
    status TEXT NOT NULL CHECK(status IN ('success', 'error')),
    error_message TEXT,                     -- 错误信息（如果 status = 'error'）
    user_agent TEXT                         -- 可选：记录执行环境（如 'cli-v0.6.0'）
);

-- 2. 创建索引（优化查询性能）
CREATE INDEX IF NOT EXISTS idx_cli_history_command ON cli_command_history(command);
CREATE INDEX IF NOT EXISTS idx_cli_history_executed_at ON cli_command_history(executed_at);
CREATE INDEX IF NOT EXISTS idx_cli_history_status ON cli_command_history(status);

-- 3. 创建 CLI 配置表（可选：用于存储用户自定义配置）
CREATE TABLE IF NOT EXISTS cli_user_preferences (
    preference_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,               -- 配置键（如 'default_output_format'）
    value TEXT NOT NULL,                    -- 配置值（如 'json'）
    description TEXT,                       -- 配置说明
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 插入默认配置（示例）
INSERT OR IGNORE INTO cli_user_preferences (key, value, description)
VALUES
    ('default_output_format', 'table', 'CLI 默认输出格式（table/json/markdown）'),
    ('enable_colors', 'true', '是否启用彩色输出'),
    ('enable_progress_bar', 'true', '是否显示进度条');

-- 5. 更新版本号
INSERT INTO schema_version (version, description)
VALUES ('1.1.0', '新增 CLI 使用统计表和用户偏好设置（M6）');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- 如果需要回滚到 v1.0.0，执行以下 SQL：

-- DROP TABLE IF EXISTS cli_user_preferences;
-- DROP TABLE IF EXISTS cli_command_history;
-- DELETE FROM schema_version WHERE version = '1.1.0';
