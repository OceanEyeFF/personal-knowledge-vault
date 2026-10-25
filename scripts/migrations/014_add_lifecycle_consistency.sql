-- Migration: 014_add_lifecycle_consistency.sql
-- Version: 1.2.8
-- Description: Outcome 输入绑定、不可变发布与版本审阅凭据
-- Author: Codex
-- Date: 2026-10-25

-- This DDL is one half of the migration. MigrationManager runs the bound
-- outcome_migration hook before committing or recording version 1.2.8.
-- Only a private work database may temporarily disable foreign-key enforcement.
CREATE TABLE _outcome_topics_v2 (
    topic_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'closed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision) = 'integer' AND revision >= 1),
    next_evidence_ordinal INTEGER NOT NULL DEFAULT 1 CHECK(next_evidence_ordinal >= 1),
    next_claim_ordinal INTEGER NOT NULL DEFAULT 1 CHECK(next_claim_ordinal >= 1),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    outcome_id TEXT NOT NULL UNIQUE,
    input_revision INTEGER NOT NULL CHECK(typeof(input_revision) = 'integer' AND input_revision >= 1),
    input_digest TEXT NOT NULL CHECK(typeof(input_digest) = 'text' AND length(input_digest) = 64 AND input_digest NOT GLOB '*[^0-9a-f]*'),
    render_contract_version TEXT NOT NULL,
    UNIQUE(topic_id, outcome_id)
);

CREATE TABLE _outcome_briefs_v2 (
    outcome_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL UNIQUE,
    outcome_type TEXT NOT NULL CHECK(outcome_type = 'research_brief'),
    state TEXT NOT NULL CHECK(state IN ('draft', 'awaiting_review', 'approved', 'returned', 'needs_review')),
    revision INTEGER NOT NULL CHECK(typeof(revision) = 'integer' AND revision >= 1),
    markdown_relative_path TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK(typeof(content_sha256) = 'text' AND length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    binding_state TEXT NOT NULL CHECK(binding_state IN ('verified', 'legacy_unverified')),
    publication_id TEXT UNIQUE CHECK(publication_id IS NULL OR (typeof(publication_id) = 'text' AND length(publication_id) = 64 AND publication_id NOT GLOB '*[^0-9a-f]*')),
    rendered_input_revision INTEGER CHECK(rendered_input_revision IS NULL OR (typeof(rendered_input_revision) = 'integer' AND rendered_input_revision >= 1)),
    rendered_input_digest TEXT CHECK(rendered_input_digest IS NULL OR (typeof(rendered_input_digest) = 'text' AND length(rendered_input_digest) = 64 AND rendered_input_digest NOT GLOB '*[^0-9a-f]*')),
    render_contract_version TEXT,
    FOREIGN KEY(topic_id, outcome_id) REFERENCES outcome_topics(topic_id, outcome_id) ON DELETE CASCADE,
    CHECK(
        (binding_state = 'verified' AND publication_id IS NOT NULL AND rendered_input_revision IS NOT NULL AND rendered_input_digest IS NOT NULL AND render_contract_version IS NOT NULL)
        OR
        (binding_state = 'legacy_unverified' AND publication_id IS NULL AND rendered_input_revision IS NULL AND rendered_input_digest IS NULL AND render_contract_version IS NULL)
    )
);

CREATE TABLE outcome_review_receipts (
    receipt_id TEXT PRIMARY KEY CHECK(typeof(receipt_id) = 'text' AND length(receipt_id) = 64 AND receipt_id NOT GLOB '*[^0-9a-f]*'),
    outcome_id TEXT NOT NULL REFERENCES outcome_topics(outcome_id) ON DELETE CASCADE,
    expected_revision INTEGER NOT NULL CHECK(typeof(expected_revision) = 'integer' AND expected_revision >= 1),
    content_sha256 TEXT NOT NULL CHECK(typeof(content_sha256) = 'text' AND length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    expected_input_revision INTEGER NOT NULL CHECK(typeof(expected_input_revision) = 'integer' AND expected_input_revision >= 1),
    action TEXT NOT NULL CHECK(action IN ('approve', 'return')),
    reason_code TEXT,
    result_state TEXT NOT NULL CHECK(result_state IN ('approved', 'returned')),
    created_at TEXT NOT NULL,
    UNIQUE(outcome_id, expected_revision, action),
    CHECK(
        (action = 'approve' AND reason_code IS NULL AND result_state = 'approved')
        OR
        (action = 'return' AND reason_code IN ('needs_revision', 'insufficient_evidence', 'other') AND reason_code IS NOT NULL AND result_state = 'returned')
    )
);
CREATE INDEX idx_outcome_review_receipts_revision ON outcome_review_receipts(outcome_id, expected_revision);

-- No INSERT schema_version here: the transaction-owned hook records the version
-- only after copying, binding, schema checks and foreign-key validation succeed.
-- No inplace downgrade is supported.
