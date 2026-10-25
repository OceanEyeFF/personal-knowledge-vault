"""Traceable, user-reviewed knowledge outcome domain services.

The outcome domain intentionally contains no Provider integration.  A Topic
records a specific inquiry, binds Claims to immutable-at-capture Evidence
locators, and publishes revisioned Markdown research briefs from SQLite
authority.  Adapters reach it through :class:`KnowledgeApplication` and the
public ``pkv_kernel.outcomes`` facade.
"""

from src.outcomes.models import (
    ClaimEvidenceLink,
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

__all__ = [
    "ClaimEvidenceLink",
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
]
