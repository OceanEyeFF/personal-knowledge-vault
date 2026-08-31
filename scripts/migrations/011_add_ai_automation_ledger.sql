-- Migration: 011_add_ai_automation_ledger.sql
-- Version: 1.2.5
-- Description: 新增内部 AI 自动任务与 token 用量账本
-- Author: Codex
-- Date: 2026-08-31

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

CREATE TABLE IF NOT EXISTS ai_automation_tasks (
    task_id TEXT PRIMARY KEY,
    mutation_id TEXT NOT NULL UNIQUE,
    source_digest TEXT NOT NULL CHECK(length(source_digest) = 64),
    policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint) = 64),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'processing', 'retry_required', 'budget_paused',
        'authorization_required', 'completed', 'superseded'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    claim_token TEXT,
    last_error_code TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_automation_tasks_state_created
ON ai_automation_tasks(state, created_at, task_id);

CREATE INDEX IF NOT EXISTS idx_ai_automation_tasks_source
ON ai_automation_tasks(source_digest, created_at, task_id);

CREATE TABLE IF NOT EXISTS ai_token_reservations (
    reservation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES ai_automation_tasks(task_id) ON DELETE RESTRICT,
    claim_token TEXT NOT NULL CHECK(length(claim_token) = 32),
    policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint) = 64),
    timezone TEXT NOT NULL,
    local_day TEXT NOT NULL,
    local_month TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens >= 0),
    settled_tokens INTEGER CHECK(settled_tokens >= 0),
    state TEXT NOT NULL CHECK(state IN ('reserved', 'settled', 'released')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_token_reservations_budget
ON ai_token_reservations(policy_fingerprint, timezone, local_day, local_month, state);

CREATE INDEX IF NOT EXISTS idx_ai_token_reservations_task
ON ai_token_reservations(task_id, state);

CREATE TABLE IF NOT EXISTS ai_token_usage (
    usage_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES ai_automation_tasks(task_id) ON DELETE RESTRICT,
    reservation_id TEXT REFERENCES ai_token_reservations(reservation_id) ON DELETE RESTRICT,
    source TEXT NOT NULL CHECK(source IN (
        'provider_reported', 'local_estimate', 'conservative_reservation'
    )),
    uncached_input_tokens INTEGER CHECK(uncached_input_tokens >= 0),
    cached_input_tokens INTEGER CHECK(cached_input_tokens >= 0),
    generated_tokens INTEGER CHECK(generated_tokens >= 0),
    embedding_input_tokens INTEGER CHECK(embedding_input_tokens >= 0),
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        uncached_input_tokens IS NOT NULL
        OR cached_input_tokens IS NOT NULL
        OR generated_tokens IS NOT NULL
        OR embedding_input_tokens IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_token_usage_task
ON ai_token_usage(task_id, recorded_at, usage_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.5', '新增内部 AI 自动任务与 token 用量账本');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- Developer Preview 不执行历史库原地降级。
