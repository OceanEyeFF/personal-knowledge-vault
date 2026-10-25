"""Public P1-B façade for traceable, explicitly reviewed research outcomes.

This module is intentionally Provider-free.  It exposes Topic → Evidence →
Claim → revisioned ``research_brief`` actions through the Kernel rather than
letting a wrapper import Application, Storage, writer-lease, or runtime-layout
implementation details.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import ClassVar
import warnings

from src.kernel import KnowledgeKernel, get_kernel
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.outcomes.models import (
    ClaimEvidenceLink,
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
    TopicState,
)


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,95}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RENDER_VERSION = re.compile(r"[a-z][a-z0-9._-]{0,95}")


def _invalid_v2() -> PKVRuntimeError:
    return PKVRuntimeError(
        ErrorCode.OUTCOME_INVALID,
        "研究成果 v2 参数或值对象无效。",
        stage="outcome_validation",
        recoverable=False,
    )


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _invalid_v2()
    return value


def _positive_integer(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise _invalid_v2()
    return value


def _sha256(value: object) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _invalid_v2()
    return value


def _render_version(value: object) -> str:
    if type(value) is not str or _RENDER_VERSION.fullmatch(value) is None:
        raise _invalid_v2()
    return value


@dataclass(frozen=True)
class ResearchBriefReviewConfirmation:
    """The exact publication and input revision the caller has reviewed."""

    outcome_id: str
    expected_revision: int
    content_sha256: str
    expected_input_revision: int

    def __post_init__(self) -> None:
        _identifier(self.outcome_id)
        _positive_integer(self.expected_revision)
        _sha256(self.content_sha256)
        _positive_integer(self.expected_input_revision)

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome_id": self.outcome_id,
            "expected_revision": self.expected_revision,
            "content_sha256": self.content_sha256,
            "expected_input_revision": self.expected_input_revision,
        }


class ResearchBriefReturnReason(str, Enum):
    NEEDS_REVISION = "needs_revision"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    OTHER = "other"


@dataclass(frozen=True)
class ResearchBriefRecordV2:
    """A closed, immutable publication view; never an execution capability."""

    schema: ClassVar[str] = "pkv.outcome.brief.v2"
    outcome_type: ClassVar[str] = "research_brief"
    outcome_id: str
    topic_id: str
    revision: int
    content_sha256: str
    state: OutcomeState
    effective_state: OutcomeState
    markdown_relative_path: str
    projection_state: str
    publication_id: str | None
    rendered_input_revision: int | None
    rendered_input_digest: str | None
    current_input_revision: int
    current_input_digest: str
    render_contract_version: str | None
    current_render_contract_version: str
    binding_state: str
    review_confirmation: ResearchBriefReviewConfirmation | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _identifier(self.outcome_id)
        _identifier(self.topic_id)
        _positive_integer(self.revision)
        _sha256(self.content_sha256)
        _positive_integer(self.current_input_revision)
        _sha256(self.current_input_digest)
        _render_version(self.current_render_contract_version)
        if type(self.state) is not OutcomeState or type(self.effective_state) is not OutcomeState:
            raise _invalid_v2()
        if type(self.projection_state) is not str or self.projection_state not in {"current", "stale", "unavailable"}:
            raise _invalid_v2()
        path = self.markdown_relative_path
        if (
            type(path) is not str or not path or "\\" in path or ":" in path
            or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise _invalid_v2()
        if type(self.created_at) is not str or type(self.updated_at) is not str:
            raise _invalid_v2()
        binding = (
            self.publication_id, self.rendered_input_revision,
            self.rendered_input_digest, self.render_contract_version,
        )
        if self.binding_state == "verified":
            _sha256(self.publication_id)
            _positive_integer(self.rendered_input_revision)
            _sha256(self.rendered_input_digest)
            _render_version(self.render_contract_version)
        elif self.binding_state == "legacy_unverified":
            if any(value is not None for value in binding):
                raise _invalid_v2()
        else:
            raise _invalid_v2()
        if self.review_confirmation is not None:
            if type(self.review_confirmation) is not ResearchBriefReviewConfirmation:
                raise _invalid_v2()
            expected = _review_confirmation(self)
            if expected is None or self.review_confirmation != expected:
                raise _invalid_v2()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "outcome_id": self.outcome_id,
            "topic_id": self.topic_id,
            "outcome_type": self.outcome_type,
            "revision": self.revision,
            "content_sha256": self.content_sha256,
            "state": self.state.value,
            "effective_state": self.effective_state.value,
            "markdown_relative_path": self.markdown_relative_path,
            "projection_state": self.projection_state,
            "publication_id": self.publication_id,
            "rendered_input_revision": self.rendered_input_revision,
            "rendered_input_digest": self.rendered_input_digest,
            "current_input_revision": self.current_input_revision,
            "current_input_digest": self.current_input_digest,
            "render_contract_version": self.render_contract_version,
            "current_render_contract_version": self.current_render_contract_version,
            "binding_state": self.binding_state,
            "review_confirmation": (
                self.review_confirmation.to_dict() if self.review_confirmation is not None else None
            ),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_TOPIC_V2_FIELDS = frozenset({
    "topic_id", "title", "question", "state", "revision", "created_at", "updated_at",
    "outcome_id", "input_revision", "input_digest", "render_contract_version",
})


@dataclass(frozen=True)
class OutcomeBundleV2:
    """A snapshot of body-free inputs and their committed publication binding."""

    schema: ClassVar[str] = "pkv.outcome.bundle.v2"
    topic: Mapping[str, object]
    evidence: tuple[EvidenceRecord, ...]
    claims: tuple[ClaimRecord, ...]
    brief: ResearchBriefRecordV2 | None

    def __post_init__(self) -> None:
        if not isinstance(self.topic, Mapping) or set(self.topic) != _TOPIC_V2_FIELDS:
            raise _invalid_v2()
        topic = dict(self.topic)
        _identifier(topic["topic_id"])
        _identifier(topic["outcome_id"])
        _positive_integer(topic["input_revision"])
        _sha256(topic["input_digest"])
        _render_version(topic["render_contract_version"])
        try:
            base = TopicRecord(
                topic_id=topic["topic_id"], title=topic["title"], question=topic["question"],
                state=TopicState(topic["state"]), revision=topic["revision"],
                created_at=topic["created_at"], updated_at=topic["updated_at"],
            )
        except (TypeError, ValueError):
            raise _invalid_v2() from None
        for key, value in base.to_dict().items():
            topic[key] = value
        if type(topic["created_at"]) is not str or type(topic["updated_at"]) is not str:
            raise _invalid_v2()
        evidence = tuple(self.evidence)
        claims = tuple(self.claims)
        if any(type(item) is not EvidenceRecord or item.topic_id != topic["topic_id"] for item in evidence):
            raise _invalid_v2()
        if any(type(item) is not ClaimRecord or item.topic_id != topic["topic_id"] for item in claims):
            raise _invalid_v2()
        if self.brief is not None and (
            type(self.brief) is not ResearchBriefRecordV2
            or self.brief.topic_id != topic["topic_id"]
            or self.brief.outcome_id != topic["outcome_id"]
            or self.brief.current_input_revision != topic["input_revision"]
            or self.brief.current_input_digest != topic["input_digest"]
            or self.brief.current_render_contract_version != topic["render_contract_version"]
        ):
            raise _invalid_v2()
        object.__setattr__(self, "topic", MappingProxyType(topic))
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "claims", claims)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "topic": dict(self.topic),
            "evidence": [item.to_dict() for item in self.evidence],
            "claims": [item.to_dict() for item in self.claims],
            "brief": self.brief.to_dict() if self.brief is not None else None,
        }


@dataclass(frozen=True)
class ResearchBriefReviewResultV2:
    schema: ClassVar[str] = "pkv.outcome.review-result.v2"
    brief: ResearchBriefRecordV2
    review_receipt_id: str
    idempotent: bool

    def __post_init__(self) -> None:
        if type(self.brief) is not ResearchBriefRecordV2 or type(self.idempotent) is not bool:
            raise _invalid_v2()
        _sha256(self.review_receipt_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "brief": self.brief.to_dict(),
            "review_receipt_id": self.review_receipt_id,
            "idempotent": self.idempotent,
        }


def _review_confirmation(brief: object) -> ResearchBriefReviewConfirmation | None:
    effective = (
        brief.effective_state if type(brief) is ResearchBriefRecordV2 else brief.visible_state
    )
    if (
        brief.binding_state != "verified"
        or brief.projection_state != "current"
        or effective not in {OutcomeState.AWAITING_REVIEW, OutcomeState.APPROVED, OutcomeState.RETURNED}
        or brief.rendered_input_revision is None
        or brief.rendered_input_revision != brief.current_input_revision
        or brief.rendered_input_digest is None
        or brief.rendered_input_digest != brief.current_input_digest
        or brief.render_contract_version is None
        or brief.render_contract_version != brief.current_render_contract_version
    ):
        return None
    return ResearchBriefReviewConfirmation(
        outcome_id=brief.outcome_id,
        expected_revision=brief.revision,
        content_sha256=brief.content_sha256,
        expected_input_revision=brief.rendered_input_revision,
    )


def _brief_v2(brief: ResearchBriefRecord) -> ResearchBriefRecordV2:
    if type(brief) is not ResearchBriefRecord:
        raise _invalid_v2()
    return ResearchBriefRecordV2(
        outcome_id=brief.outcome_id, topic_id=brief.topic_id,
        revision=brief.revision, content_sha256=brief.content_sha256,
        state=brief.state, effective_state=brief.visible_state,
        markdown_relative_path=brief.markdown_relative_path,
        projection_state=brief.projection_state, publication_id=brief.publication_id,
        rendered_input_revision=brief.rendered_input_revision,
        rendered_input_digest=brief.rendered_input_digest,
        current_input_revision=brief.current_input_revision,
        current_input_digest=brief.current_input_digest,
        render_contract_version=brief.render_contract_version,
        current_render_contract_version=brief.current_render_contract_version,
        binding_state=brief.binding_state, review_confirmation=_review_confirmation(brief),
        created_at=brief.created_at, updated_at=brief.updated_at,
    )


def _resolve_kernel(kernel: KnowledgeKernel | None) -> KnowledgeKernel:
    if kernel is None:
        return get_kernel()
    if not isinstance(kernel, KnowledgeKernel):
        raise TypeError("kernel必须是pkv_kernel.KnowledgeKernel或None")
    return kernel


def create_topic(
    *,
    title: str,
    question: str,
    kernel: KnowledgeKernel | None = None,
) -> TopicRecord:
    """Create an inquiry container under the Application-owned writer lease."""

    return _resolve_kernel(kernel).create_outcome_topic(title=title, question=question)


def get_topic(
    topic_id: str,
    *,
    kernel: KnowledgeKernel | None = None,
) -> TopicRecord | None:
    """Read one Topic without source resolution, file creation, or Provider use."""

    return _resolve_kernel(kernel).get_outcome_topic(topic_id=topic_id)


def list_topics(
    *,
    limit: int = 100,
    offset: int = 0,
    kernel: KnowledgeKernel | None = None,
) -> tuple[TopicRecord, ...]:
    """List Topics in deterministic creation/id order."""

    return _resolve_kernel(kernel).list_outcome_topics(limit=limit, offset=offset)


def get_outcome(
    topic_id: str,
    *,
    kernel: KnowledgeKernel | None = None,
) -> OutcomeBundle:
    """Read a revalidated Topic/Evidence/Claim/Brief bundle with zero writes."""

    return _resolve_kernel(kernel).get_outcome_bundle(topic_id=topic_id)


def attach_evidence(
    *,
    topic_id: str,
    locator: str,
    acquisition_method: EvidenceAcquisitionMethod | str = EvidenceAcquisitionMethod.USER_SELECTED,
    retrieval_status: str = "not_requested",
    limitation_codes: tuple[str, ...] | list[str] = (),
    kernel: KnowledgeKernel | None = None,
) -> EvidenceRecord:
    """Bind a deliberately selected public locator to its current digest."""

    return _resolve_kernel(kernel).attach_outcome_evidence(
        topic_id=topic_id,
        locator=locator,
        acquisition_method=(
            acquisition_method.value
            if isinstance(acquisition_method, EvidenceAcquisitionMethod)
            else acquisition_method
        ),
        retrieval_status=retrieval_status,
        limitation_codes=limitation_codes,
    )


def detach_evidence(
    evidence_id: str,
    *,
    kernel: KnowledgeKernel | None = None,
) -> None:
    """Detach an evidence selection; no successor source is inferred."""

    _resolve_kernel(kernel).detach_outcome_evidence(evidence_id)


def suggest_evidence(
    *,
    topic_id: str,
    query: str,
    limit: int = 10,
    kernel: KnowledgeKernel | None = None,
) -> EvidenceSuggestionResult:
    """Return explicit-attachment candidates from strict BM25 only."""

    return _resolve_kernel(kernel).suggest_outcome_evidence(
        topic_id=topic_id,
        query=query,
        limit=limit,
    )


def create_claim(
    *,
    topic_id: str,
    statement: str,
    state: ClaimState | str = ClaimState.DRAFT,
    limitation_codes: tuple[str, ...] | list[str] = (),
    kernel: KnowledgeKernel | None = None,
) -> ClaimRecord:
    """Create an explicit claim in its declared epistemic state."""

    return _resolve_kernel(kernel).create_outcome_claim(
        topic_id=topic_id,
        statement=statement,
        state=state.value if isinstance(state, ClaimState) else state,
        limitation_codes=limitation_codes,
    )


def revise_claim(
    *,
    claim_id: str,
    statement: str | None = None,
    state: ClaimState | str | None = None,
    limitation_codes: tuple[str, ...] | list[str] | None = None,
    expected_revision: int | None = None,
    kernel: KnowledgeKernel | None = None,
) -> ClaimRecord:
    """Revise a claim, optionally fencing against a stale prior revision."""

    return _resolve_kernel(kernel).revise_outcome_claim(
        claim_id=claim_id,
        statement=statement,
        state=state.value if isinstance(state, ClaimState) else state,
        limitation_codes=limitation_codes,
        expected_revision=expected_revision,
    )


def link_claim_evidence(
    *,
    claim_id: str,
    evidence_id: str,
    polarity: ClaimEvidencePolarity | str,
    kernel: KnowledgeKernel | None = None,
) -> ClaimRecord:
    """Record one support/counter link; cross-topic links fail closed."""

    return _resolve_kernel(kernel).link_outcome_claim_evidence(
        claim_id=claim_id,
        evidence_id=evidence_id,
        polarity=polarity.value if isinstance(polarity, ClaimEvidencePolarity) else polarity,
    )


def unlink_claim_evidence(
    *,
    claim_id: str,
    evidence_id: str,
    kernel: KnowledgeKernel | None = None,
) -> ClaimRecord:
    """Remove one explicit Claim→Evidence edge."""

    return _resolve_kernel(kernel).unlink_outcome_claim_evidence(
        claim_id=claim_id,
        evidence_id=evidence_id,
    )


def render_research_brief(
    *,
    topic_id: str,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecord:
    """Revalidate sources and publish a new immutable Markdown revision."""

    return _resolve_kernel(kernel).render_outcome_research_brief(topic_id=topic_id)


def approve_research_brief(
    *,
    topic_id: str,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecord:
    """Explicitly approve only a current, review-ready brief."""

    warnings.warn(
        "approve_research_brief is deprecated; use approve_research_brief_v2 with an exact "
        "review confirmation. Unversioned review will be rejected in API major 2.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _resolve_kernel(kernel).approve_outcome_research_brief(topic_id=topic_id)


def return_research_brief(
    *,
    topic_id: str,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecord:
    """Return a brief for a new authored/rendered revision."""

    warnings.warn(
        "return_research_brief is deprecated; use return_research_brief_v2 with an exact "
        "review confirmation and reason_code. Unversioned review will be rejected in API major 2.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _resolve_kernel(kernel).return_outcome_research_brief(topic_id=topic_id)


def get_research_brief(
    *,
    topic_id: str | None = None,
    outcome_id: str | None = None,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecord | None:
    """Read a safe brief pointer and its effective drift-aware review state."""

    return _resolve_kernel(kernel).get_outcome_research_brief(
        topic_id=topic_id,
        outcome_id=outcome_id,
    )


def get_outcome_v2(
    topic_id: str, *, kernel: KnowledgeKernel | None = None,
) -> OutcomeBundleV2:
    """Read one coherent input/publication binding without writing or upgrading."""

    _identifier(topic_id)
    bundle = _resolve_kernel(kernel).get_outcome_bundle_v2(topic_id=topic_id)
    topic = bundle.topic
    projection = {
        "topic_id": topic.topic_id, "title": topic.title, "question": topic.question,
        "state": topic.state.value, "revision": topic.revision,
        "created_at": topic.created_at, "updated_at": topic.updated_at,
        "outcome_id": topic.outcome_id, "input_revision": topic.input_revision,
        "input_digest": topic.input_digest, "render_contract_version": topic.render_contract_version,
    }
    return OutcomeBundleV2(
        topic=projection, evidence=bundle.evidence, claims=bundle.claims,
        brief=_brief_v2(bundle.brief) if bundle.brief is not None else None,
    )


def get_research_brief_v2(
    *, topic_id: str | None = None, outcome_id: str | None = None,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecordV2 | None:
    """Look up exactly one selector; a missing valid selector has no result."""

    if (topic_id is None) == (outcome_id is None):
        raise _invalid_v2()
    _identifier(topic_id if topic_id is not None else outcome_id)
    brief = _resolve_kernel(kernel).get_outcome_research_brief_v2(
        topic_id=topic_id, outcome_id=outcome_id,
    )
    return _brief_v2(brief) if brief is not None else None


def render_research_brief_v2(
    *, topic_id: str, kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecordV2:
    """Publish using the persistent renderer contract and current input binding."""

    _identifier(topic_id)
    return _brief_v2(_resolve_kernel(kernel).render_outcome_research_brief_v2(topic_id=topic_id))


def _confirmation_values(confirmation: ResearchBriefReviewConfirmation) -> dict[str, object]:
    if type(confirmation) is not ResearchBriefReviewConfirmation:
        raise _invalid_v2()
    # Revalidate even an object altered via object.__setattr__; a DTO is data,
    # never permission to bypass the service's durable review checks.
    return ResearchBriefReviewConfirmation(
        outcome_id=confirmation.outcome_id,
        expected_revision=confirmation.expected_revision,
        content_sha256=confirmation.content_sha256,
        expected_input_revision=confirmation.expected_input_revision,
    ).to_dict()


def approve_research_brief_v2(
    *, confirmation: ResearchBriefReviewConfirmation, kernel: KnowledgeKernel | None = None,
) -> ResearchBriefReviewResultV2:
    """Approve the supplied exact four-tuple; never fetch or substitute latest."""

    values = _confirmation_values(confirmation)
    result = _resolve_kernel(kernel).approve_outcome_research_brief_v2(**values)
    return ResearchBriefReviewResultV2(_brief_v2(result.brief), result.receipt_id, result.idempotent)


def return_research_brief_v2(
    *, confirmation: ResearchBriefReviewConfirmation,
    reason_code: ResearchBriefReturnReason | str,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefReviewResultV2:
    """Return the supplied publication with one closed, non-text reason code."""

    values = _confirmation_values(confirmation)
    if type(reason_code) is ResearchBriefReturnReason:
        reason_code = reason_code.value
    if type(reason_code) is not str:
        raise _invalid_v2()
    try:
        reason = ResearchBriefReturnReason(reason_code)
    except ValueError:
        raise _invalid_v2() from None
    result = _resolve_kernel(kernel).return_outcome_research_brief_v2(
        **values, reason_code=reason.value,
    )
    return ResearchBriefReviewResultV2(_brief_v2(result.brief), result.receipt_id, result.idempotent)


__all__ = [
    "ClaimEvidenceLink",
    "ClaimEvidencePolarity",
    "ClaimRecord",
    "ClaimState",
    "EvidenceAcquisitionMethod",
    "EvidenceLocator",
    "EvidenceLocatorKind",
    "EvidenceRecord",
    "EvidenceState",
    "EvidenceSuggestion",
    "EvidenceSuggestionResult",
    "OutcomeBundle",
    "OutcomeBundleV2",
    "OutcomeState",
    "ResearchBriefRecord",
    "ResearchBriefRecordV2",
    "ResearchBriefReturnReason",
    "ResearchBriefReviewConfirmation",
    "ResearchBriefReviewResultV2",
    "TopicRecord",
    "TopicState",
    "approve_research_brief",
    "approve_research_brief_v2",
    "attach_evidence",
    "create_claim",
    "create_topic",
    "detach_evidence",
    "get_outcome",
    "get_outcome_v2",
    "get_research_brief",
    "get_research_brief_v2",
    "get_topic",
    "link_claim_evidence",
    "list_topics",
    "render_research_brief",
    "render_research_brief_v2",
    "return_research_brief",
    "return_research_brief_v2",
    "revise_claim",
    "suggest_evidence",
    "unlink_claim_evidence",
]
