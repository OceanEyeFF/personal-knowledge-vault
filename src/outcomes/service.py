"""Application-facing P1-B outcome service.

The service provides the domain policy that cannot live in a low-level store:
canonical source capture/revalidation, strict BM25-only suggestions, immutable
Markdown publication, and explicit review gates.  It has no Provider,
Embedding, QueryRouter, or network dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.outcomes.models import (
    ClaimEvidencePolarity,
    ClaimRecord,
    ClaimState,
    EvidenceAcquisitionMethod,
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceRecord,
    EvidenceState,
    EvidenceSuggestion,
    EvidenceSuggestionResult,
    OutcomeBundle,
    OutcomeState,
    ResearchBriefRecord,
    TopicRecord,
    normalize_limitation_codes,
    normalize_outcome_text,
)
from src.outcomes.renderer import render_research_brief
from src.relations.citations import build_entry_locator
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import ensure_safe_directory
from src.storage.outcome_store import OutcomeStore
from src.storage.sqlite_store import entry_projection_sha256, row_projection_sha256


def _new_identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class OutcomeService:
    """Traceable Topic/Evidence/Claim/Brief policy for one Application graph."""

    def __init__(
        self,
        *,
        outcome_store: OutcomeStore,
        sqlite_store: Any,
        markdown_store: Any,
        relation_store: Any,
        layout: Any,
        bm25_retriever_factory: Callable[[], Any],
    ) -> None:
        self._outcome_store = outcome_store
        self._sqlite_store = sqlite_store
        self._markdown_store = markdown_store
        self._relation_store = relation_store
        self._layout = layout
        self._bm25_retriever_factory = bm25_retriever_factory

    @staticmethod
    def _require_topic_id(value: Any) -> str:
        if type(value) is not str or not value.startswith("topic_"):
            raise ValueError("topic_id无效")
        return value

    @staticmethod
    def _require_claim_id(value: Any) -> str:
        if type(value) is not str or not value.startswith("claim_"):
            raise ValueError("claim_id无效")
        return value

    @staticmethod
    def _require_evidence_id(value: Any) -> str:
        if type(value) is not str or not value.startswith("evidence_"):
            raise ValueError("evidence_id无效")
        return value

    @staticmethod
    def _not_found(*, stage: str, message: str) -> PKVRuntimeError:
        return PKVRuntimeError(
            ErrorCode.OUTCOME_NOT_FOUND,
            message,
            stage=stage,
            recoverable=False,
        )

    def _bundle_from_store(self, topic_id: str) -> OutcomeBundle:
        stored = self._outcome_store.get_bundle_rows(topic_id)
        if stored is None:
            raise self._not_found(
                stage="outcome_topic", message="指定的研究主题不存在。"
            )
        topic, evidence, claims, brief = stored
        revalidated = tuple(self._revalidate_evidence(item) for item in evidence)
        effective_claims = self._effective_claims(claims, revalidated)
        if brief is not None:
            brief = self._revalidate_brief_projection(brief)
            if any(
                item.recorded_state is not EvidenceState.CURRENT
                for item in revalidated
            ):
                brief = brief.with_effective_state(OutcomeState.NEEDS_REVIEW)
        return OutcomeBundle(topic, revalidated, effective_claims, brief)

    def _revalidate_brief_projection(
        self,
        brief: ResearchBriefRecord,
    ) -> ResearchBriefRecord:
        """Verify the SQLite pointer's immutable Markdown projection on reads.

        SQLite is authoritative for structural state, but its pointer is only
        usable while the projected file is still a safe contained regular file
        with the recorded digest.  This check is deliberately read-only: a
        missing or altered projection becomes a visible repair/review condition
        rather than an implicit re-render or a silent pointer rewrite.
        """

        target = self._layout.outcomes_dir / brief.markdown_relative_path
        # ``open_user_file(..., "rb")`` intentionally reports a missing path as
        # PATH_STATE_UNDETERMINED because it is a generic safety gateway.  The
        # Outcome read contract has one narrower recoverable state: only a
        # direct, successful lstat followed by ENOENT may mean this immutable
        # projection is absent.  Permission, I/O and containment uncertainty
        # must remain a safe public failure rather than a false success.
        try:
            self._layout.validate_user_file(
                target,
                label="research brief",
                allow_missing=True,
            )
            os.lstat(target)
        except FileNotFoundError:
            return brief.with_projection_state("unavailable")
        except PKVRuntimeError:
            raise
        except OSError as error:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "无法安全确认研究成果投影状态。",
                stage="outcome_brief",
                recoverable=True,
            ) from error

        digest = hashlib.sha256()
        try:
            with self._layout.open_user_file(
                target,
                "rb",
                label="research brief",
            ) as projection:
                while block := projection.read(64 * 1024):
                    digest.update(block)
        except FileNotFoundError:
            return brief.with_projection_state("unavailable")
        except PKVRuntimeError:
            raise
        except OSError as error:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "无法安全确认研究成果投影状态。",
                stage="outcome_brief",
                recoverable=True,
            ) from error
        if digest.hexdigest() != brief.content_sha256:
            return brief.with_projection_state("stale")
        return brief.with_projection_state("current")

    @staticmethod
    def _effective_claims(
        claims: tuple[ClaimRecord, ...],
        evidence: tuple[EvidenceRecord, ...],
    ) -> tuple[ClaimRecord, ...]:
        """Overlay the public Claim state from currently valid evidence only.

        ``state`` is an explicitly authored classification in SQLite, but it
        cannot be displayed as supported/contested while its required evidence
        is absent, stale, or unavailable.  The overlay is deliberately
        read-only: a reader must not acquire a lease or silently mutate a
        claim merely because a source was inspected.
        """

        evidence_by_id = {item.evidence_id: item for item in evidence}
        effective: list[ClaimRecord] = []
        for claim in claims:
            current_links = tuple(
                link
                for link in claim.evidence_links
                if evidence_by_id.get(link.evidence_id) is not None
                and evidence_by_id[link.evidence_id].recorded_state
                is EvidenceState.CURRENT
            )
            if claim.state is ClaimState.WITHDRAWN:
                effective.append(claim)
                continue
            if not current_links:
                effective.append(replace(claim, state=ClaimState.INSUFFICIENT))
                continue
            polarities = {link.polarity for link in current_links}
            if (
                claim.state is ClaimState.SUPPORTED
                and ClaimEvidencePolarity.SUPPORT not in polarities
            ) or (
                claim.state is ClaimState.CONTESTED
                and ClaimEvidencePolarity.COUNTER not in polarities
            ):
                effective.append(replace(claim, state=ClaimState.INSUFFICIENT))
                continue
            effective.append(claim)
        return tuple(effective)

    def _entry_revision(
        self,
        locator: EvidenceLocator,
        *,
        reject_inconsistent: bool = False,
    ) -> str | None:
        """Observe one Entry/Chunk source across its Markdown and SQLite views.

        Markdown remains the human-owned primary record.  A captured source
        digest is valid only while its Markdown projection agrees with the
        SQLite/chunk projection.  An initial split-brain source is rejected;
        an already-bound source that later diverges is surfaced as stale rather
        than silently treating the SQLite side as the sole truth.
        """

        if locator.knowledge_id is None:
            return None
        row = self._sqlite_store.query_by_id(locator.knowledge_id)
        if row is None:
            return None
        chunks = self._sqlite_store.get_chunks_by_knowledge_id(locator.knowledge_id)
        if locator.kind is EvidenceLocatorKind.CHUNK:
            if locator.chunk_id is None:
                return None
            chunk = self._sqlite_store.get_chunk_by_id(locator.chunk_id)
            if (
                chunk is None
                or int(chunk.get("knowledge_id", 0)) != locator.knowledge_id
            ):
                return None
        sqlite_revision = row_projection_sha256(row, chunks)
        relative_path = row.get("file_path")
        if type(relative_path) is not str or not relative_path:
            return None
        try:
            markdown_entry = self._markdown_store.load(Path(relative_path))
            markdown_revision = entry_projection_sha256(
                markdown_entry,
                relative_path,
                [str(chunk["chunk_text"]) for chunk in chunks],
            )
        except (OSError, TypeError, ValueError, KeyError, PKVRuntimeError):
            return None
        if markdown_revision == sqlite_revision:
            return sqlite_revision
        if reject_inconsistent:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_EVIDENCE_STALE,
                "证据来源的 Markdown 与索引投影不一致；请先修复来源后再绑定。",
                stage="outcome_evidence",
                recoverable=True,
            )
        # Preserve the observed mismatch only as an opaque in-memory digest.
        # It intentionally differs from the captured SQLite digest so a reader
        # overlays the existing binding as stale without leaking source content.
        return _canonical_sha256(
            {
                "markdown_projection_sha256": markdown_revision,
                "sqlite_projection_sha256": sqlite_revision,
            }
        )

    def _relation_revision(self, locator: EvidenceLocator) -> str | None:
        if locator.relation_id is None:
            return None
        relation = self._relation_store.get_relation(locator.relation_id)
        if relation is None:
            return None
        # The relation payload may contain local implementation data.  It is
        # hashed internally for change detection and is never persisted or
        # returned by this outcome contract.
        return _canonical_sha256(relation.to_dict())

    def _capture_source_revision(self, locator: EvidenceLocator) -> str:
        if locator.kind in {EvidenceLocatorKind.ENTRY, EvidenceLocatorKind.CHUNK}:
            revision = self._entry_revision(locator, reject_inconsistent=True)
        else:
            revision = self._relation_revision(locator)
        if revision is None:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_NOT_FOUND,
                "指定的证据来源不可用，无法绑定。",
                stage="outcome_evidence",
                recoverable=False,
            )
        return revision

    def _revalidate_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        locator = evidence.locator
        if locator.kind in {EvidenceLocatorKind.ENTRY, EvidenceLocatorKind.CHUNK}:
            revision = self._entry_revision(locator)
        else:
            revision = self._relation_revision(locator)
        if revision is None:
            return evidence.with_recorded_state(EvidenceState.UNAVAILABLE)
        if revision != evidence.source_revision_sha256:
            return evidence.with_recorded_state(EvidenceState.STALE)
        return evidence.with_recorded_state(EvidenceState.CURRENT)

    def _persist_revalidation(self, bundle: OutcomeBundle) -> OutcomeBundle:
        """Durably record observed drift only from a mutation-owned operation."""

        changes = {
            current.evidence_id: current.recorded_state
            for current, stored in zip(
                bundle.evidence,
                self._outcome_store.get_bundle_rows(bundle.topic.topic_id)[1],
                strict=True,
            )
            if current.recorded_state is not stored.recorded_state
        }
        if changes:
            self._outcome_store.update_evidence_recorded_states(changes)
        if any(
            evidence.recorded_state is not EvidenceState.CURRENT
            for evidence in bundle.evidence
        ):
            self._outcome_store.mark_brief_needs_review(bundle.topic.topic_id)
        return self._bundle_from_store(bundle.topic.topic_id)

    @staticmethod
    def _require_current_evidence(bundle: OutcomeBundle) -> None:
        if any(
            evidence.recorded_state is not EvidenceState.CURRENT
            for evidence in bundle.evidence
        ):
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_EVIDENCE_STALE,
                "证据来源已变化或不可用；请重新核对后再生成或批准成果。",
                stage="outcome_evidence",
                recoverable=True,
            )

    def create_topic(self, *, title: str, question: str) -> TopicRecord:
        return self._outcome_store.create_topic(
            topic_id=_new_identifier("topic"),
            title=title,
            question=question,
        )

    def get_topic(self, topic_id: str) -> TopicRecord | None:
        self._require_topic_id(topic_id)
        return self._outcome_store.get_topic(topic_id)

    def list_topics(
        self, *, limit: int = 100, offset: int = 0
    ) -> tuple[TopicRecord, ...]:
        return self._outcome_store.list_topics(limit=limit, offset=offset)

    def get_bundle(self, topic_id: str) -> OutcomeBundle:
        self._require_topic_id(topic_id)
        return self._bundle_from_store(topic_id)

    def attach_evidence(
        self,
        *,
        topic_id: str,
        locator: str,
        acquisition_method: EvidenceAcquisitionMethod
        | str = EvidenceAcquisitionMethod.USER_SELECTED,
        retrieval_status: str = "not_requested",
        limitation_codes: tuple[str, ...] | list[str] = (),
    ) -> EvidenceRecord:
        self._require_topic_id(topic_id)
        parsed = EvidenceLocator.parse(locator)
        method = EvidenceAcquisitionMethod(acquisition_method)
        if (
            parsed.kind is EvidenceLocatorKind.RELATION
            and method is not EvidenceAcquisitionMethod.RELATION
        ):
            raise ValueError("relation locator必须标记为relation acquisition")
        if (
            parsed.kind is not EvidenceLocatorKind.RELATION
            and method is EvidenceAcquisitionMethod.RELATION
        ):
            raise ValueError("relation acquisition仅适用于relation locator")
        evidence = self._outcome_store.create_evidence(
            evidence_id=_new_identifier("evidence"),
            topic_id=topic_id,
            locator=parsed,
            source_revision_sha256=self._capture_source_revision(parsed),
            acquisition_method=method,
            retrieval_status=retrieval_status,
            limitation_codes=normalize_limitation_codes(limitation_codes),
        )
        self._outcome_store.mark_brief_needs_review(topic_id)
        return evidence

    def detach_evidence(self, evidence_id: str) -> None:
        self._require_evidence_id(evidence_id)
        topic_id = self._outcome_store.delete_evidence(evidence_id)
        self._outcome_store.mark_brief_needs_review(topic_id)

    def suggest_evidence(
        self,
        *,
        topic_id: str,
        query: str,
        limit: int = 10,
    ) -> EvidenceSuggestionResult:
        """Suggest entry locators using only the credential-free BM25 port."""

        self._require_topic_id(topic_id)
        if self._outcome_store.get_topic(topic_id) is None:
            raise self._not_found(
                stage="outcome_topic", message="指定的研究主题不存在。"
            )
        normalized_query = normalize_outcome_text(
            query, label="evidence query", maximum=4_000
        )
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("evidence suggestion limit必须在1到50之间")
        response = self._bm25_retriever_factory().search(normalized_query, limit=limit)
        issues = tuple(issue.to_dict() for issue in getattr(response, "issues", ()))
        suggestions = tuple(
            EvidenceSuggestion(
                knowledge_id=result.knowledge_id,
                locator=build_entry_locator(result.knowledge_id),
                title=result.title,
                score=result.score,
            )
            for result in getattr(response, "results", ())
        )
        return EvidenceSuggestionResult(
            status=str(getattr(response, "status", "error")),
            suggestions=suggestions,
            issues=issues,
        )

    def create_claim(
        self,
        *,
        topic_id: str,
        statement: str,
        state: ClaimState | str = ClaimState.DRAFT,
        limitation_codes: tuple[str, ...] | list[str] = (),
    ) -> ClaimRecord:
        self._require_topic_id(topic_id)
        claim = self._outcome_store.create_claim(
            claim_id=_new_identifier("claim"),
            topic_id=topic_id,
            statement=statement,
            state=ClaimState(state),
            limitation_codes=normalize_limitation_codes(limitation_codes),
        )
        self._outcome_store.mark_brief_needs_review(topic_id)
        return claim

    def revise_claim(
        self,
        *,
        claim_id: str,
        statement: str | None = None,
        state: ClaimState | str | None = None,
        limitation_codes: tuple[str, ...] | list[str] | None = None,
        expected_revision: int | None = None,
    ) -> ClaimRecord:
        self._require_claim_id(claim_id)
        claim = self._outcome_store.update_claim(
            claim_id=claim_id,
            statement=statement,
            state=ClaimState(state) if state is not None else None,
            limitation_codes=(
                normalize_limitation_codes(limitation_codes)
                if limitation_codes is not None
                else None
            ),
            expected_revision=expected_revision,
        )
        self._outcome_store.mark_brief_needs_review(claim.topic_id)
        return claim

    def link_claim_evidence(
        self,
        *,
        claim_id: str,
        evidence_id: str,
        polarity: ClaimEvidencePolarity | str,
    ) -> ClaimRecord:
        self._require_claim_id(claim_id)
        self._require_evidence_id(evidence_id)
        claim = self._outcome_store.link_claim_evidence(
            claim_id=claim_id,
            evidence_id=evidence_id,
            polarity=ClaimEvidencePolarity(polarity),
        )
        self._outcome_store.mark_brief_needs_review(claim.topic_id)
        return claim

    def unlink_claim_evidence(self, *, claim_id: str, evidence_id: str) -> ClaimRecord:
        self._require_claim_id(claim_id)
        self._require_evidence_id(evidence_id)
        claim = self._outcome_store.unlink_claim_evidence(
            claim_id=claim_id,
            evidence_id=evidence_id,
        )
        self._outcome_store.mark_brief_needs_review(claim.topic_id)
        return claim

    @staticmethod
    def _initial_brief_input_fingerprint(bundle: OutcomeBundle) -> str:
        """Return the body-free canonical input identity for first publication.

        This never enters a public DTO or filename directly.  It distinguishes
        a retry of the exact render that left an orphan Markdown file from a
        later user revision made before that first SQLite pointer could commit.
        """

        return _canonical_sha256(
            {
                "topic": bundle.topic.to_dict(),
                "evidence": [item.to_dict() for item in bundle.evidence],
                "claims": [item.to_dict() for item in bundle.claims],
            }
        )

    def _next_brief_identity(
        self,
        *,
        bundle: OutcomeBundle,
    ) -> tuple[str, int, int | None]:
        topic_id = bundle.topic.topic_id
        existing = bundle.brief
        if existing is None:
            # The first pointer is published only after Markdown.  Deriving
            # this opaque identifier from the Topic and complete render input
            # lets a fresh process find the exact same r0001 projection after
            # an interruption, without pre-committing a SQLite row.  A later
            # Claim/Evidence change receives a new immutable r0001 instead of
            # being permanently blocked by the unmatched orphan.
            input_fingerprint = self._initial_brief_input_fingerprint(bundle)
            outcome_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"pkv:research-brief:{topic_id}:{input_fingerprint}",
            ).hex
            return f"outcome_{outcome_id}", 1, None
        return existing.outcome_id, existing.revision + 1, existing.revision

    def _publish_brief_markdown(
        self,
        *,
        topic_id: str,
        outcome_id: str,
        revision: int,
        content: str,
    ) -> tuple[str, str]:
        topic_dir = self._layout.validate_user_directory(
            self._layout.outcomes_dir / f"topic-{topic_id}",
            label="research brief目录",
            allow_missing=True,
        )
        ensure_safe_directory(topic_dir, label="research brief目录")
        self._layout.validate_user_directory(
            topic_dir,
            label="research brief目录",
            allow_missing=False,
        )
        target = topic_dir / f"{outcome_id}-r{revision:04d}.md"
        self._layout.validate_user_file(
            target,
            label="research brief",
            allow_missing=True,
        )
        relative_path = target.relative_to(self._layout.outcomes_dir).as_posix()
        encoded = content.encode("utf-8")
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        if os.path.lexists(target):
            # Markdown is published before its SQLite pointer.  If a process
            # stops between those two operations, a fresh writer may complete
            # the same deterministic revision, but it must never overwrite an
            # unrecognized existing file.
            with self._layout.open_user_file(
                target,
                "rb",
                label="research brief",
            ) as existing:
                if existing.read(len(encoded) + 1) == encoded:
                    return relative_path, content_sha256
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_STATE_CONFLICT,
                "该研究成果版本已存在，请重新生成。",
                stage="outcome_brief",
                recoverable=True,
            )

        def ensure_unpublished() -> None:
            self._layout.validate_user_file(
                target,
                label="research brief",
                allow_missing=True,
            )
            if os.path.lexists(target):
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_STATE_CONFLICT,
                    "该研究成果版本已存在，请重新生成。",
                    stage="outcome_brief",
                    recoverable=True,
                )

        self._layout.atomic_publish_user_file(
            target,
            label="research brief",
            data=encoded,
            pre_replace=ensure_unpublished,
        )
        return relative_path, content_sha256

    def render_research_brief(self, *, topic_id: str) -> ResearchBriefRecord:
        self._require_topic_id(topic_id)
        bundle = self._persist_revalidation(self._bundle_from_store(topic_id))
        self._require_current_evidence(bundle)
        outcome_id, revision, expected_previous = self._next_brief_identity(
            bundle=bundle,
        )
        content = render_research_brief(
            bundle,
            outcome_id=outcome_id,
            revision=revision,
        )
        relative_path, content_sha256 = self._publish_brief_markdown(
            topic_id=topic_id,
            outcome_id=outcome_id,
            revision=revision,
            content=content,
        )
        return self._outcome_store.upsert_rendered_brief(
            outcome_id=outcome_id,
            topic_id=topic_id,
            revision=revision,
            markdown_relative_path=relative_path,
            content_sha256=content_sha256,
            expected_previous_revision=expected_previous,
        )

    def approve_research_brief(self, *, topic_id: str) -> ResearchBriefRecord:
        self._require_topic_id(topic_id)
        bundle = self._persist_revalidation(self._bundle_from_store(topic_id))
        self._require_current_evidence(bundle)
        brief = bundle.brief
        if brief is None:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_APPROVAL_BLOCKED,
                "请先生成研究成果，再进行批准。",
                stage="outcome_review",
                recoverable=False,
            )
        if not bundle.evidence:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_APPROVAL_BLOCKED,
                "批准研究成果前至少需要一条当前证据。",
                stage="outcome_review",
                recoverable=False,
            )
        blocking = {
            ClaimState.DRAFT,
            ClaimState.INSUFFICIENT,
            ClaimState.WITHDRAWN,
        }
        if any(claim.state in blocking for claim in bundle.claims):
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_APPROVAL_BLOCKED,
                "仍有未满足批准条件的主张，请先明确其状态。",
                stage="outcome_review",
                recoverable=False,
            )
        for claim in bundle.claims:
            polarities = {link.polarity for link in claim.evidence_links}
            if (
                claim.state is ClaimState.SUPPORTED
                and ClaimEvidencePolarity.SUPPORT not in polarities
            ):
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_APPROVAL_BLOCKED,
                    "支持状态的主张至少需要一条明确的支持证据。",
                    stage="outcome_review",
                    recoverable=False,
                )
            if (
                claim.state is ClaimState.CONTESTED
                and ClaimEvidencePolarity.COUNTER not in polarities
            ):
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_APPROVAL_BLOCKED,
                    "争议状态的主张至少需要一条明确的反证。",
                    stage="outcome_review",
                    recoverable=False,
                )
        if brief.visible_state not in {
            OutcomeState.AWAITING_REVIEW,
            OutcomeState.APPROVED,
        }:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_STATE_CONFLICT,
                "当前研究成果状态不能直接批准；请重新生成后再审阅。",
                stage="outcome_review",
                recoverable=True,
            )
        return self._outcome_store.set_brief_state(
            topic_id=topic_id,
            state=OutcomeState.APPROVED,
        )

    def return_research_brief(self, *, topic_id: str) -> ResearchBriefRecord:
        self._require_topic_id(topic_id)
        bundle = self._bundle_from_store(topic_id)
        if bundle.brief is None:
            raise self._not_found(
                stage="outcome_brief", message="该主题尚未生成研究成果。"
            )
        if bundle.brief.visible_state not in {
            OutcomeState.AWAITING_REVIEW,
            OutcomeState.APPROVED,
            OutcomeState.RETURNED,
        }:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_STATE_CONFLICT,
                "当前研究成果状态不能退回。",
                stage="outcome_review",
                recoverable=True,
            )
        return self._outcome_store.set_brief_state(
            topic_id=topic_id,
            state=OutcomeState.RETURNED,
        )

    def get_research_brief(
        self, *, topic_id: str | None = None, outcome_id: str | None = None
    ) -> ResearchBriefRecord | None:
        if (topic_id is None) == (outcome_id is None):
            raise ValueError("必须且只能提供topic_id或outcome_id")
        if topic_id is not None:
            self._require_topic_id(topic_id)
            bundle = self._bundle_from_store(topic_id)
            return bundle.brief
        if type(outcome_id) is not str or not outcome_id.startswith("outcome_"):
            raise ValueError("outcome_id无效")
        brief = self._outcome_store.get_brief(outcome_id)
        if brief is None:
            return None
        return self._bundle_from_store(brief.topic_id).brief


__all__ = ["OutcomeService"]
