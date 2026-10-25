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
import re
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
    _require_identifier,
)
from src.outcomes.identity import outcome_input_digest, outcome_publication_id
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
        return _require_identifier(value, label="topic_id")

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

    def _stored_bundle(self, topic_id: str) -> OutcomeBundle:
        stored = self._outcome_store.get_bundle_rows(topic_id)
        if stored is None:
            raise self._not_found(
                stage="outcome_topic", message="指定的研究主题不存在。"
            )
        topic, evidence, claims, brief = stored
        return OutcomeBundle(topic, evidence, claims, brief)

    def _bundle_from_store(self, topic_id: str) -> OutcomeBundle:
        return self._revalidated_bundle(self._stored_bundle(topic_id))

    def _revalidated_bundle(self, stored: OutcomeBundle) -> OutcomeBundle:
        """Overlay observations on one captured SQLite input/pointer snapshot."""

        topic, brief = stored.topic, stored.brief
        revalidated = tuple(self._revalidate_evidence(item) for item in stored.evidence)
        effective_claims = self._effective_claims(stored.claims, revalidated)
        if brief is not None:
            brief = replace(
                brief,
                current_input_revision=topic.input_revision,
                current_input_digest=topic.input_digest,
                current_render_contract_version=topic.render_contract_version,
            )
            brief = self._revalidate_brief_projection(brief)
            if not self._input_binding_matches(topic, brief) or any(
                item.recorded_state is not EvidenceState.CURRENT
                for item in revalidated
            ):
                brief = brief.with_effective_state(OutcomeState.NEEDS_REVIEW)
        return OutcomeBundle(topic, revalidated, effective_claims, brief)

    @staticmethod
    def _input_binding_matches(topic: TopicRecord, brief: ResearchBriefRecord) -> bool:
        return (
            brief.binding_state == "verified"
            and topic.outcome_id == brief.outcome_id
            and topic.input_revision is not None
            and topic.input_digest is not None
            and topic.render_contract_version is not None
            and brief.rendered_input_revision == topic.input_revision
            and brief.rendered_input_digest == topic.input_digest
            and brief.render_contract_version == topic.render_contract_version
        )

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
        except FileNotFoundError:
            return None
        except PKVRuntimeError:
            raise
        except OSError as error:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED, "无法安全确认研究证据来源状态。",
                stage="outcome_evidence", recoverable=True,
            ) from error
        except (TypeError, ValueError, KeyError):
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
        return evidence

    def detach_evidence(self, evidence_id: str) -> None:
        self._require_evidence_id(evidence_id)
        self._outcome_store.delete_evidence(evidence_id)

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
        return claim

    def unlink_claim_evidence(self, *, claim_id: str, evidence_id: str) -> ClaimRecord:
        self._require_claim_id(claim_id)
        self._require_evidence_id(evidence_id)
        claim = self._outcome_store.unlink_claim_evidence(
            claim_id=claim_id,
            evidence_id=evidence_id,
        )
        return claim

    def _next_brief_identity(
        self,
        *,
        bundle: OutcomeBundle,
    ) -> tuple[str, int, str]:
        topic = bundle.topic
        if (
            topic.outcome_id is None or topic.input_revision is None
            or topic.input_digest is None or topic.render_contract_version is None
        ):
            raise PKVRuntimeError(
                ErrorCode.DATABASE_UPGRADE_REQUIRED,
                "研究成果需要先完成数据库升级。",
                stage="outcome_brief", recoverable=True,
            )
        if outcome_input_digest(topic=topic, evidence=bundle.evidence, claims=bundle.claims) != topic.input_digest:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED, "研究主题输入摘要不一致。",
                stage="outcome_brief", recoverable=True,
            )
        revision = 1 if bundle.brief is None else bundle.brief.revision + 1
        if revision > 9223372036854775807:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_STATE_CONFLICT, "研究成果版本已达到上限。",
                stage="outcome_brief", recoverable=False,
            )
        return topic.outcome_id, revision, outcome_publication_id(
            outcome_id=topic.outcome_id, candidate_revision=revision,
            input_digest=topic.input_digest,
        )

    def _publish_brief_markdown(
        self,
        *,
        topic_id: str,
        outcome_id: str,
        revision: int,
        publication_id: str,
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
        target = topic_dir / f"{outcome_id}-r{revision:04d}-p{publication_id}.md"
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
        bundle = self._stored_bundle(topic_id)
        # Legacy observations may have persisted a stale binding.  A recovered
        # source does not silently rewrite those inputs: explicit reattachment
        # must establish a current binding before rendering their claims.
        self._require_current_evidence(bundle)
        self._require_current_evidence(self._revalidated_bundle(bundle))
        outcome_id, revision, publication_id = self._next_brief_identity(
            bundle=bundle,
        )
        content = render_research_brief(
            replace(bundle, claims=self._effective_claims(bundle.claims, bundle.evidence)),
            outcome_id=outcome_id,
            revision=revision,
        )
        relative_path, content_sha256 = self._publish_brief_markdown(
            topic_id=topic_id,
            outcome_id=outcome_id,
            revision=revision,
            publication_id=publication_id,
            content=content,
        )
        # External files can change without the PKV writer lease.  Recheck
        # sources and our exact publication before attempting the SQLite CAS.
        self._require_current_evidence(self._revalidated_bundle(bundle))
        candidate = ResearchBriefRecord(
            outcome_id=outcome_id, topic_id=topic_id, state=OutcomeState.AWAITING_REVIEW,
            revision=revision, markdown_relative_path=relative_path,
            content_sha256=content_sha256, created_at="", updated_at="",
        )
        if self._revalidate_brief_projection(candidate).projection_state != "current":
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_STATE_CONFLICT, "研究成果发布文件已变化。",
                stage="outcome_brief", recoverable=True,
            )
        self._outcome_store.upsert_rendered_brief(
            outcome_id=outcome_id,
            topic_id=topic_id,
            revision=revision,
            markdown_relative_path=relative_path,
            content_sha256=content_sha256,
            expected_previous_revision=bundle.brief.revision if bundle.brief else None,
            expected_previous_pointer=bundle.brief,
            expected_input_revision=bundle.topic.input_revision,
            expected_input_digest=bundle.topic.input_digest,
            render_contract_version=bundle.topic.render_contract_version,
            publication_id=publication_id,
        )
        return self._bundle_from_store(topic_id).brief

    def approve_research_brief(self, *, topic_id: str) -> ResearchBriefRecord:
        self._require_topic_id(topic_id)
        bundle = self._bundle_from_store(topic_id)
        self._require_current_evidence(bundle)
        brief = bundle.brief
        if brief is None:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_APPROVAL_BLOCKED,
                "请先生成研究成果，再进行批准。",
                stage="outcome_review",
                recoverable=False,
            )
        self._require_approval_inputs(bundle)
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

    @staticmethod
    def _require_approval_inputs(bundle: OutcomeBundle) -> None:
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
        _require_identifier(outcome_id, label="outcome_id")
        brief = self._outcome_store.get_brief(outcome_id)
        if brief is None:
            return None
        return self._bundle_from_store(brief.topic_id).brief

    def get_research_brief_v2(
        self, *, topic_id: str | None = None, outcome_id: str | None = None,
    ) -> ResearchBriefRecord | None:
        if (topic_id is None) == (outcome_id is None):
            raise ValueError("必须且只能提供topic_id或outcome_id")
        if topic_id is not None:
            self._require_topic_id(topic_id)
            stored = self._outcome_store.get_bundle_rows(topic_id)
            return self._revalidated_bundle(OutcomeBundle(*stored)).brief if stored else None
        _require_identifier(outcome_id, label="outcome_id")
        brief = self._outcome_store.get_brief(outcome_id)
        if brief is None:
            return None
        return self._bundle_from_store(brief.topic_id).brief

    def review_research_brief_v2(
        self, *, outcome_id: str, expected_revision: int, content_sha256: str,
        expected_input_revision: int, action: str, reason_code: str | None = None,
    ) -> Any:
        """Revalidate the displayed revision, then commit state and receipt once."""

        _require_identifier(outcome_id, label="outcome_id")
        if any(type(value) is not int or not 1 <= value <= 9223372036854775807
               for value in (expected_revision, expected_input_revision)):
            raise ValueError("review revision无效")
        if type(content_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
            raise ValueError("review content_sha256无效")
        if action not in {"approve", "return"} or (
            action == "approve" and reason_code is not None
        ) or (action == "return" and reason_code not in {
            "needs_revision", "insufficient_evidence", "other",
        }):
            raise ValueError("review action/reason_code无效")
        current = self._outcome_store.get_brief(outcome_id)
        if current is None:
            raise self._not_found(stage="outcome_review", message="指定的研究成果不存在。")
        bundle = self._bundle_from_store(current.topic_id)
        brief = bundle.brief
        assert brief is not None
        if brief.binding_state != "verified":
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_APPROVAL_BLOCKED, "历史研究成果的输入绑定尚未验证，请重新生成。",
                stage="outcome_review", recoverable=True,
            )
        if (
            brief.outcome_id != outcome_id or brief.revision != expected_revision
            or brief.content_sha256 != content_sha256
            or brief.rendered_input_revision != expected_input_revision
            or bundle.topic.input_revision != expected_input_revision
            or not self._input_binding_matches(bundle.topic, brief)
        ):
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_STATE_CONFLICT, "审阅确认与当前研究成果或输入版本不一致。",
                stage="outcome_review", recoverable=True,
            )
        self._require_current_evidence(bundle)
        if brief.visible_state in {OutcomeState.DRAFT, OutcomeState.NEEDS_REVIEW}:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_APPROVAL_BLOCKED, "当前研究成果尚不满足审阅条件。",
                stage="outcome_review", recoverable=True,
            )
        if action == "approve":
            if brief.visible_state is OutcomeState.RETURNED:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_STATE_CONFLICT, "已退回的研究成果必须重新生成后再批准。",
                    stage="outcome_review", recoverable=True,
                )
            self._require_approval_inputs(bundle)
        committed = self._outcome_store.review_brief(
            outcome_id=outcome_id, expected_revision=expected_revision,
            content_sha256=content_sha256, expected_input_revision=expected_input_revision,
            action=action, reason_code=reason_code,
        )
        # Return the committed receipt with its captured input context.  A later
        # public read independently revalidates external sources/projection.
        return replace(committed, brief=replace(
            committed.brief, current_input_revision=bundle.topic.input_revision,
            current_input_digest=bundle.topic.input_digest,
            current_render_contract_version=bundle.topic.render_contract_version,
        ))


__all__ = ["OutcomeService"]
