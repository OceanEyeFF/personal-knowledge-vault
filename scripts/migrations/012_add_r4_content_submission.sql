-- Migration: 012_add_r4_content_submission.sql
-- Version: 1.2.6
-- Description: 新增 R4 Q1′ 内容提交、durable handoff 与 Q2 阻塞任务
-- Author: Codex
-- Date: 2026-09-01

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

-- Q0 accepts an ingress request before any crawler, parser, or Provider work.
-- Source bodies stay in the private ingress spool; SQLite retains only a
-- fenced, recoverable reference to that immutable request.
CREATE TABLE IF NOT EXISTS ingress_tasks (
    task_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    request_kind TEXT NOT NULL CHECK(request_kind IN ('url', 'text', 'file')),
    request_ref TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
    prepared_ref TEXT,
    prepared_sha256 TEXT CHECK(prepared_sha256 IS NULL OR length(prepared_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN (
        'accepted', 'processing', 'prepared', 'submitted', 'retry_required',
        'rejected', 'repair_required', 'superseded'
    )),
    claim_token TEXT,
    claimed_until TIMESTAMP,
    owner_fence INTEGER NOT NULL DEFAULT 0 CHECK(owner_fence >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    not_before TIMESTAMP,
    last_error_code TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingress_tasks_eligibility
ON ingress_tasks(state, not_before, created_at, task_id);

-- Q1′ never stores a document body in SQLite.  The immutable prepared payload
-- lives in the private runtime spool; this row retains only its identity and
-- digest together with the operation-bound content mutation state.
CREATE TABLE IF NOT EXISTS content_mutation_tasks (
    task_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK(action IN ('archive', 'delete', 'apply_ai_patch')),
    prepared_ref TEXT,
    prepared_sha256 TEXT,
    patch_ref TEXT,
    patch_sha256 TEXT,
    target_knowledge_id INTEGER,
    target_revision_sha256 TEXT CHECK(
        target_revision_sha256 IS NULL OR length(target_revision_sha256) = 64
    ),
    state TEXT NOT NULL CHECK(state IN (
        'accepted', 'processing', 'core_committed', 'ai_handoff_pending',
        'completed', 'retry_required', 'repair_required', 'rejected', 'superseded'
    )),
    claim_token TEXT,
    claimed_until TIMESTAMP,
    owner_fence INTEGER NOT NULL DEFAULT 0 CHECK(owner_fence >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    not_before TIMESTAMP,
    last_error_code TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(prepared_sha256 IS NULL OR length(prepared_sha256) = 64),
    CHECK(patch_sha256 IS NULL OR length(patch_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS idx_content_mutation_tasks_eligibility
ON content_mutation_tasks(state, not_before, created_at, task_id);

CREATE INDEX IF NOT EXISTS idx_content_mutation_tasks_operation
ON content_mutation_tasks(operation_id);

-- The handoff is the recovery authority between the proven Markdown+SQLite
-- core mutation and the later AI-derived work.  It contains no Provider
-- payload, credential or document body.
CREATE TABLE IF NOT EXISTS content_ai_handoffs (
    operation_id TEXT PRIMARY KEY REFERENCES content_mutation_tasks(operation_id)
        ON DELETE RESTRICT,
    derivation_task_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'binding_published', 'q2_activated', 'completed',
        'retry_required', 'repair_required'
    )),
    source_digest TEXT CHECK(source_digest IS NULL OR length(source_digest) = 64),
    binding_state TEXT,
    last_error_code TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_content_ai_handoffs_state_created
ON content_ai_handoffs(state, created_at, operation_id);

-- ``storage_operation_commits`` remains the archive/delete proof authority.
-- Q1′ apply_ai_patch has a different immutable revision contract, so it gets a
-- separate proof table rather than weakening the committed 010 check constraint.
CREATE TABLE IF NOT EXISTS r4_content_operation_commits (
    operation_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK(action = 'apply_ai_patch'),
    knowledge_id INTEGER NOT NULL,
    relative_file_path TEXT NOT NULL,
    previous_revision_sha256 TEXT NOT NULL CHECK(length(previous_revision_sha256) = 64),
    resulting_revision_sha256 TEXT NOT NULL CHECK(length(resulting_revision_sha256) = 64),
    committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_r4_content_operation_commits_knowledge
ON r4_content_operation_commits(knowledge_id, committed_at);

-- Q2 starts blocked.  A task can only become claimable after Q1′ has recorded
-- the durable handoff and published a non-ready generation binding.
CREATE TABLE IF NOT EXISTS ai_derivation_tasks (
    task_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE REFERENCES content_mutation_tasks(operation_id)
        ON DELETE RESTRICT,
    target_knowledge_id INTEGER,
    target_revision_sha256 TEXT CHECK(
        target_revision_sha256 IS NULL OR length(target_revision_sha256) = 64
    ),
    source_digest TEXT CHECK(source_digest IS NULL OR length(source_digest) = 64),
    policy_fingerprint TEXT CHECK(policy_fingerprint IS NULL OR length(policy_fingerprint) = 64),
    patch_ref TEXT,
    patch_sha256 TEXT CHECK(patch_sha256 IS NULL OR length(patch_sha256) = 64),
    patch_applied INTEGER NOT NULL DEFAULT 0 CHECK(patch_applied IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN (
        'blocked_handoff', 'pending', 'processing', 'retry_required',
        'budget_paused', 'authorization_required', 'completed', 'superseded'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    claim_token TEXT,
    claimed_until TIMESTAMP,
    owner_fence INTEGER NOT NULL DEFAULT 0 CHECK(owner_fence >= 0),
    not_before TIMESTAMP,
    last_error_code TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_derivation_tasks_eligibility
ON ai_derivation_tasks(state, not_before, created_at, task_id);

CREATE INDEX IF NOT EXISTS idx_ai_derivation_tasks_source
ON ai_derivation_tasks(source_digest, created_at, task_id);

CREATE TABLE IF NOT EXISTS ai_derivation_reservations (
    reservation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES ai_derivation_tasks(task_id) ON DELETE RESTRICT,
    claim_token TEXT NOT NULL CHECK(length(claim_token) = 32),
    owner_fence INTEGER NOT NULL CHECK(owner_fence >= 0),
    policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint) = 64),
    timezone TEXT NOT NULL,
    local_day TEXT NOT NULL,
    local_month TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens >= 0),
    reserved_micros INTEGER,
    settled_tokens INTEGER CHECK(settled_tokens >= 0),
    settled_micros INTEGER,
    currency TEXT,
    state TEXT NOT NULL CHECK(state IN ('reserved', 'settled', 'released')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    settled_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ai_derivation_reservations_budget
ON ai_derivation_reservations(
    policy_fingerprint, timezone, local_day, local_month, state
);

CREATE INDEX IF NOT EXISTS idx_ai_derivation_reservations_task
ON ai_derivation_reservations(task_id, state);

CREATE TABLE IF NOT EXISTS ai_derivation_usage (
    usage_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES ai_derivation_tasks(task_id) ON DELETE RESTRICT,
    reservation_id TEXT REFERENCES ai_derivation_reservations(reservation_id) ON DELETE RESTRICT,
    stage TEXT NOT NULL CHECK(stage IN ('summary', 'tags', 'embedding')),
    source TEXT NOT NULL CHECK(source IN (
        'provider_reported', 'local_estimate', 'conservative_reservation'
    )),
    uncached_input_tokens INTEGER CHECK(uncached_input_tokens >= 0),
    cached_input_tokens INTEGER CHECK(cached_input_tokens >= 0),
    generated_tokens INTEGER CHECK(generated_tokens >= 0),
    embedding_input_tokens INTEGER CHECK(embedding_input_tokens >= 0),
    amount_micros INTEGER CHECK(amount_micros >= 0),
    currency TEXT,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        uncached_input_tokens IS NOT NULL
        OR cached_input_tokens IS NOT NULL
        OR generated_tokens IS NOT NULL
        OR embedding_input_tokens IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_derivation_usage_task
ON ai_derivation_usage(task_id, recorded_at, usage_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.6', '新增 R4 Q1′ 内容提交、durable handoff 与 Q2 阻塞任务');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- Developer Preview 不执行历史库原地降级。
