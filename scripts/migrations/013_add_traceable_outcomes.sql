-- Migration: 013_add_traceable_outcomes.sql
-- Version: 1.2.7
-- Description: 新增 P1-B Topic、Evidence、Claim 与 research brief 成果合同
-- Author: Codex
-- Date: 2026-09-04

-- ========================================
-- 向上迁移（Upgrade）
-- ========================================

-- These tables retain only structured user-authored outcome state, public PKV
-- locators, revision digests and stable limitation codes.  They never duplicate
-- source bodies, raw webpages, provider responses, local paths or credentials.
-- Source knowledge rows are intentionally not foreign keys: deleting an Entry
-- must make retained evidence unavailable during revalidation rather than
-- silently deleting or rebinding the historical claim.
CREATE TABLE IF NOT EXISTS outcome_topics (
    topic_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'closed')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    next_evidence_ordinal INTEGER NOT NULL DEFAULT 1 CHECK(next_evidence_ordinal >= 1),
    next_claim_ordinal INTEGER NOT NULL DEFAULT 1 CHECK(next_claim_ordinal >= 1),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outcome_evidence (
    evidence_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES outcome_topics(topic_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    locator TEXT NOT NULL,
    locator_kind TEXT NOT NULL CHECK(locator_kind IN ('entry', 'chunk', 'relation')),
    knowledge_id INTEGER,
    chunk_id INTEGER,
    relation_id INTEGER,
    source_revision_sha256 TEXT NOT NULL CHECK(length(source_revision_sha256) = 64),
    acquisition_method TEXT NOT NULL CHECK(
        acquisition_method IN ('user_selected', 'bm25', 'relation')
    ),
    retrieval_status TEXT NOT NULL CHECK(
        retrieval_status IN ('not_requested', 'success', 'degraded')
    ),
    limitation_codes TEXT NOT NULL DEFAULT '[]',
    recorded_state TEXT NOT NULL CHECK(
        recorded_state IN ('current', 'stale', 'unavailable')
    ),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(topic_id, ordinal),
    UNIQUE(topic_id, locator),
    CHECK(
        (locator_kind = 'entry'
            AND knowledge_id IS NOT NULL
            AND chunk_id IS NULL
            AND relation_id IS NULL)
        OR
        (locator_kind = 'chunk'
            AND knowledge_id IS NOT NULL
            AND chunk_id IS NOT NULL
            AND relation_id IS NULL)
        OR
        (locator_kind = 'relation'
            AND knowledge_id IS NULL
            AND chunk_id IS NULL
            AND relation_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_outcome_evidence_topic
ON outcome_evidence(topic_id, ordinal);

CREATE TABLE IF NOT EXISTS outcome_claims (
    claim_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES outcome_topics(topic_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    statement TEXT NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN ('draft', 'supported', 'contested', 'insufficient', 'withdrawn')
    ),
    limitation_codes TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(topic_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_outcome_claims_topic
ON outcome_claims(topic_id, ordinal);

CREATE TABLE IF NOT EXISTS outcome_claim_evidence (
    claim_id TEXT NOT NULL REFERENCES outcome_claims(claim_id) ON DELETE CASCADE,
    evidence_id TEXT NOT NULL REFERENCES outcome_evidence(evidence_id) ON DELETE CASCADE,
    polarity TEXT NOT NULL CHECK(polarity IN ('support', 'counter')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(claim_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_outcome_claim_evidence_evidence
ON outcome_claim_evidence(evidence_id, claim_id);

-- A topic has one public research-brief identity.  Each render publishes a new
-- immutable Markdown revision before this SQLite pointer is committed.  If a
-- process stops before the DB transaction, the unreferenced file is harmless;
-- readers only follow this stored pointer and verify its digest.
CREATE TABLE IF NOT EXISTS outcome_briefs (
    outcome_id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL UNIQUE REFERENCES outcome_topics(topic_id) ON DELETE CASCADE,
    outcome_type TEXT NOT NULL CHECK(outcome_type = 'research_brief'),
    state TEXT NOT NULL CHECK(
        state IN ('draft', 'awaiting_review', 'approved', 'returned', 'needs_review')
    ),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    markdown_relative_path TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outcome_briefs_state
ON outcome_briefs(state, topic_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES ('1.2.7', '新增 P1-B Topic、Evidence、Claim 与 research brief 成果合同');

-- ========================================
-- 向下迁移（Rollback）
-- ========================================

-- Developer Preview 不执行历史库原地降级。
