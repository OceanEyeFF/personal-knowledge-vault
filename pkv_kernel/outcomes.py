"""Public P1-B façade for traceable, explicitly reviewed research outcomes.

This module is intentionally Provider-free.  It exposes Topic → Evidence →
Claim → revisioned ``research_brief`` actions through the Kernel rather than
letting a wrapper import Application, Storage, writer-lease, or runtime-layout
implementation details.
"""

from __future__ import annotations

from src.kernel import KnowledgeKernel, get_kernel
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

    return _resolve_kernel(kernel).approve_outcome_research_brief(topic_id=topic_id)


def return_research_brief(
    *,
    topic_id: str,
    kernel: KnowledgeKernel | None = None,
) -> ResearchBriefRecord:
    """Return a brief for a new authored/rendered revision."""

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
    "OutcomeState",
    "ResearchBriefRecord",
    "TopicRecord",
    "TopicState",
    "approve_research_brief",
    "attach_evidence",
    "create_claim",
    "create_topic",
    "detach_evidence",
    "get_outcome",
    "get_research_brief",
    "get_topic",
    "link_claim_evidence",
    "list_topics",
    "render_research_brief",
    "return_research_brief",
    "revise_claim",
    "suggest_evidence",
    "unlink_claim_evidence",
]
