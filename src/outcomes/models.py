"""Strict, body-free value contracts for traceable knowledge outcomes.

These objects deliberately retain user-authored inquiry/claim text but never
retain a source body, raw URL, local filesystem path, Provider response, or
credential.  Evidence is instead bound to one canonical PKV locator and a
captured source-projection digest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from src.relations.citations import is_local_reference
from src.utils.config import url_contains_credentials

_ID_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,95}$")
_LIMITATION_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ENTRY_LOCATOR = re.compile(r"^pkv://entries/([1-9][0-9]*)$")
_CHUNK_LOCATOR = re.compile(r"^pkv://entries/([1-9][0-9]*)/chunks/([1-9][0-9]*)$")
_RELATION_LOCATOR = re.compile(r"^pkv://relations/([1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TITLE_LENGTH = 240
_MAX_QUESTION_LENGTH = 4_000
_MAX_STATEMENT_LENGTH = 8_000
_BRIEF_PROJECTION_STATES = frozenset({"current", "stale", "unavailable"})
_URL_REFERENCE = re.compile(
    r"(?i)(?:(?:https?|ftp|wss?):[\\/]*|//)[^\s<>\"'`]+"
)
_CREDENTIAL_URL_USERINFO = re.compile(
    r"(?i)(?:(?:https?|ftp|wss?):[\\/]*|//)[^/?#\s<>\"'`]*@"
)
_EMBEDDED_WINDOWS_PATH = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])"
    r"(?:[a-z]:[\\/]+|\\\\(?:[.?][\\/]?)?|\\(?![\\/]))"
)
_EMBEDDED_POSIX_PATH = re.compile(r"(?:^|[\s<>\"'`(\[=,:;])/(?!/)[^\s]*")
_FILE_REFERENCE = re.compile(r"(?i)\bfile:[\\/]*")


class TopicState(str, Enum):
    """Explicit lifecycle for an inquiry container."""

    ACTIVE = "active"
    CLOSED = "closed"


class EvidenceLocatorKind(str, Enum):
    """The only v1 evidence source forms."""

    ENTRY = "entry"
    CHUNK = "chunk"
    RELATION = "relation"


class EvidenceAcquisitionMethod(str, Enum):
    """How a user deliberately selected one evidence item."""

    USER_SELECTED = "user_selected"
    BM25 = "bm25"
    RELATION = "relation"


class EvidenceState(str, Enum):
    """Recorded/effective source-binding condition."""

    CURRENT = "current"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class ClaimState(str, Enum):
    """Explicit epistemic status, never an automatic truth assertion."""

    DRAFT = "draft"
    SUPPORTED = "supported"
    CONTESTED = "contested"
    INSUFFICIENT = "insufficient"
    WITHDRAWN = "withdrawn"


class OutcomeState(str, Enum):
    """Review state of a revisioned research brief."""

    DRAFT = "draft"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    RETURNED = "returned"
    NEEDS_REVIEW = "needs_review"


class ClaimEvidencePolarity(str, Enum):
    """A claim/evidence edge is explicitly supporting or countering."""

    SUPPORT = "support"
    COUNTER = "counter"


def _require_identifier(value: Any, *, label: str) -> str:
    if type(value) is not str or _ID_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{label}格式无效")
    return value


def normalize_user_text(value: Any, *, label: str, maximum: int) -> str:
    """Normalize one user-authored scalar without accepting control payloads."""

    if type(value) is not str:
        raise TypeError(f"{label}必须是字符串")
    # A Topic/Claim is a concise authored statement, not an alternate source
    # body channel.  Folding whitespace also makes Markdown projection stable.
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label}不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{label}超过最大长度")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{label}包含控制字符")
    return normalized


def _contains_unsafe_outcome_reference(value: str) -> bool:
    """Return whether public Outcome text could disclose a private reference.

    Topic, Claim and suggestion text is persisted or returned verbatim.  It
    must therefore not become a side channel for local paths or credentials
    embedded in a URL.  This is intentionally stricter than citation display
    sanitization: a mutable Outcome request fails closed instead of accepting
    and later re-projecting a redacted-but-ambiguous authored assertion.
    """

    if is_local_reference(value):
        return True
    if (
        _FILE_REFERENCE.search(value)
        or _EMBEDDED_WINDOWS_PATH.search(value)
        or _EMBEDDED_POSIX_PATH.search(value)
    ):
        return True
    if _CREDENTIAL_URL_USERINFO.search(value):
        return True
    return any(
        url_contains_credentials(candidate)
        for candidate in _URL_REFERENCE.findall(value)
    )


def normalize_outcome_text(value: Any, *, label: str, maximum: int) -> str:
    """Normalize text that can cross the P1-B durable/public boundary.

    Unlike generic display text, P1-B authored text becomes SQLite state and a
    deterministic Markdown projection.  Reject unsafe path and credential
    references before either can be persisted or reflected by CLI/MCP.
    """

    normalized = normalize_user_text(value, label=label, maximum=maximum)
    if _contains_unsafe_outcome_reference(normalized):
        raise ValueError(f"{label}包含不允许的路径或凭据引用")
    return normalized


def normalize_limitation_codes(value: Iterable[Any] | None) -> tuple[str, ...]:
    """Return a bounded, deterministic set of stable machine-readable codes."""

    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise TypeError("limitation_codes必须是字符串序列")
    try:
        raw_values = tuple(value)
    except TypeError as exc:
        raise TypeError("limitation_codes必须是可迭代集合") from exc
    if len(raw_values) > 32:
        raise ValueError("limitation_codes数量超过上限")
    normalized: set[str] = set()
    for item in raw_values:
        if type(item) is not str or _LIMITATION_CODE.fullmatch(item) is None:
            raise ValueError("limitation_codes包含无效代码")
        normalized.add(item)
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class EvidenceLocator:
    """One canonical, source-body-free PKV evidence locator."""

    value: str
    kind: EvidenceLocatorKind
    knowledge_id: int | None = None
    chunk_id: int | None = None
    relation_id: int | None = None

    @classmethod
    def parse(cls, value: Any) -> EvidenceLocator:
        if type(value) is not str:
            raise TypeError("evidence locator必须是字符串")
        if match := _ENTRY_LOCATOR.fullmatch(value):
            return cls(
                value=value,
                kind=EvidenceLocatorKind.ENTRY,
                knowledge_id=int(match.group(1)),
            )
        if match := _CHUNK_LOCATOR.fullmatch(value):
            return cls(
                value=value,
                kind=EvidenceLocatorKind.CHUNK,
                knowledge_id=int(match.group(1)),
                chunk_id=int(match.group(2)),
            )
        if match := _RELATION_LOCATOR.fullmatch(value):
            return cls(
                value=value,
                kind=EvidenceLocatorKind.RELATION,
                relation_id=int(match.group(1)),
            )
        raise ValueError("evidence locator必须是受支持的PKV entry、chunk或relation定位符")

    def to_dict(self) -> dict[str, object]:
        return {"locator": self.value, "kind": self.kind.value}


@dataclass(frozen=True)
class TopicRecord:
    topic_id: str
    title: str
    question: str
    state: TopicState
    revision: int
    created_at: str
    updated_at: str

    # Internal v2 identity; v1 serialization deliberately remains unchanged.
    outcome_id: str | None = None
    input_revision: int | None = None
    input_digest: str | None = None
    render_contract_version: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.topic_id, label="topic_id")
        object.__setattr__(
            self,
            "title",
            normalize_outcome_text(
                self.title,
                label="topic title",
                maximum=_MAX_TITLE_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "question",
            normalize_outcome_text(
                self.question,
                label="topic question",
                maximum=_MAX_QUESTION_LENGTH,
            ),
        )
        if type(self.state) is not TopicState:
            raise TypeError("topic state无效")
        if type(self.revision) is not int or not 1 <= self.revision <= 2**63 - 1:
            raise ValueError("topic revision无效")
        if self.outcome_id is not None:
            _require_identifier(self.outcome_id, label="outcome_id")
        if self.input_revision is not None and (type(self.input_revision) is not int or not 1 <= self.input_revision <= 2**63 - 1):
            raise ValueError("topic input_revision无效")
        if self.input_digest is not None and (type(self.input_digest) is not str or _SHA256.fullmatch(self.input_digest) is None):
            raise ValueError("topic input_digest无效")

    def to_dict(self) -> dict[str, object]:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "question": self.question,
            "state": self.state.value,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    topic_id: str
    ordinal: int
    locator: EvidenceLocator
    source_revision_sha256: str
    acquisition_method: EvidenceAcquisitionMethod
    retrieval_status: str
    limitation_codes: tuple[str, ...]
    recorded_state: EvidenceState
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_identifier(self.evidence_id, label="evidence_id")
        _require_identifier(self.topic_id, label="topic_id")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("evidence ordinal无效")
        if type(self.locator) is not EvidenceLocator:
            raise TypeError("evidence locator无效")
        if _SHA256.fullmatch(self.source_revision_sha256) is None:
            raise ValueError("source_revision_sha256无效")
        if type(self.acquisition_method) is not EvidenceAcquisitionMethod:
            raise TypeError("evidence acquisition_method无效")
        if self.retrieval_status not in {"not_requested", "success", "degraded"}:
            raise ValueError("evidence retrieval_status无效")
        object.__setattr__(self, "limitation_codes", normalize_limitation_codes(self.limitation_codes))
        if type(self.recorded_state) is not EvidenceState:
            raise TypeError("evidence recorded_state无效")

    def with_recorded_state(self, state: EvidenceState) -> EvidenceRecord:
        return replace(self, recorded_state=state)

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "topic_id": self.topic_id,
            "ordinal": self.ordinal,
            **self.locator.to_dict(),
            "source_revision_sha256": self.source_revision_sha256,
            "acquisition_method": self.acquisition_method.value,
            "retrieval_status": self.retrieval_status,
            "limitation_codes": list(self.limitation_codes),
            "recorded_state": self.recorded_state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ClaimEvidenceLink:
    evidence_id: str
    polarity: ClaimEvidencePolarity

    def __post_init__(self) -> None:
        _require_identifier(self.evidence_id, label="evidence_id")
        if type(self.polarity) is not ClaimEvidencePolarity:
            raise TypeError("claim evidence polarity无效")

    def to_dict(self) -> dict[str, object]:
        return {"evidence_id": self.evidence_id, "polarity": self.polarity.value}


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    topic_id: str
    ordinal: int
    statement: str
    state: ClaimState
    limitation_codes: tuple[str, ...]
    revision: int
    evidence_links: tuple[ClaimEvidenceLink, ...]
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _require_identifier(self.claim_id, label="claim_id")
        _require_identifier(self.topic_id, label="topic_id")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("claim ordinal无效")
        object.__setattr__(
            self,
            "statement",
            normalize_outcome_text(
                self.statement,
                label="claim statement",
                maximum=_MAX_STATEMENT_LENGTH,
            ),
        )
        if type(self.state) is not ClaimState:
            raise TypeError("claim state无效")
        object.__setattr__(self, "limitation_codes", normalize_limitation_codes(self.limitation_codes))
        if type(self.revision) is not int or not 1 <= self.revision <= 2**63 - 1:
            raise ValueError("claim revision无效")
        links = tuple(self.evidence_links)
        if any(type(link) is not ClaimEvidenceLink for link in links):
            raise TypeError("claim evidence_links无效")
        if len({link.evidence_id for link in links}) != len(links):
            raise ValueError("claim evidence_links不能重复")
        object.__setattr__(self, "evidence_links", links)

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "topic_id": self.topic_id,
            "ordinal": self.ordinal,
            "statement": self.statement,
            "state": self.state.value,
            "limitation_codes": list(self.limitation_codes),
            "revision": self.revision,
            "evidence_links": [link.to_dict() for link in self.evidence_links],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ResearchBriefRecord:
    outcome_id: str
    topic_id: str
    state: OutcomeState
    revision: int
    markdown_relative_path: str
    content_sha256: str
    created_at: str
    updated_at: str
    effective_state: OutcomeState | None = None
    projection_state: str = "current"
    binding_state: str = "legacy_unverified"
    publication_id: str | None = None
    rendered_input_revision: int | None = None
    rendered_input_digest: str | None = None
    render_contract_version: str | None = None
    current_input_revision: int | None = None
    current_input_digest: str | None = None
    current_render_contract_version: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.outcome_id, label="outcome_id")
        _require_identifier(self.topic_id, label="topic_id")
        if type(self.state) is not OutcomeState:
            raise TypeError("outcome state无效")
        if self.effective_state is not None and type(self.effective_state) is not OutcomeState:
            raise TypeError("outcome effective_state无效")
        if type(self.revision) is not int or not 1 <= self.revision <= 2**63 - 1:
            raise ValueError("outcome revision无效")
        if (
            type(self.markdown_relative_path) is not str
            or not self.markdown_relative_path
            or self.markdown_relative_path.startswith("/")
            or "\\" in self.markdown_relative_path
            or ".." in self.markdown_relative_path.split("/")
        ):
            raise ValueError("outcome markdown_relative_path无效")
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError("outcome content_sha256无效")
        if (
            type(self.projection_state) is not str
            or self.projection_state not in _BRIEF_PROJECTION_STATES
        ):
            raise ValueError("outcome projection_state无效")
        if self.binding_state not in {"verified", "legacy_unverified"}:
            raise ValueError("outcome binding_state无效")
        binding = (self.publication_id, self.rendered_input_revision, self.rendered_input_digest, self.render_contract_version)
        if self.binding_state == "verified":
            if any(value is None for value in binding):
                raise ValueError("outcome verified binding缺失")
            if type(self.rendered_input_revision) is not int or not 1 <= self.rendered_input_revision <= 2**63 - 1:
                raise ValueError("outcome rendered_input_revision无效")
            for value in (self.publication_id, self.rendered_input_digest):
                if type(value) is not str or _SHA256.fullmatch(value) is None:
                    raise ValueError("outcome binding digest无效")
        elif any(value is not None for value in binding):
            raise ValueError("outcome legacy binding不能包含推定身份")

    @property
    def visible_state(self) -> OutcomeState:
        if self.projection_state != "current":
            return OutcomeState.NEEDS_REVIEW
        return self.effective_state or self.state

    def with_effective_state(self, state: OutcomeState) -> ResearchBriefRecord:
        return replace(self, effective_state=state)

    def with_projection_state(self, state: str) -> ResearchBriefRecord:
        """Overlay read-time Markdown projection integrity without a write."""

        if type(state) is not str or state not in _BRIEF_PROJECTION_STATES:
            raise ValueError("outcome projection_state无效")
        return replace(self, projection_state=state)

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome_id": self.outcome_id,
            "topic_id": self.topic_id,
            "outcome_type": "research_brief",
            "state": self.state.value,
            "effective_state": self.visible_state.value,
            "revision": self.revision,
            "markdown_relative_path": self.markdown_relative_path,
            "content_sha256": self.content_sha256,
            "projection_state": self.projection_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ReviewCommitResult:
    """Internal atomic review result; adapters own its explicit v2 projection."""

    brief: ResearchBriefRecord
    receipt_id: str
    idempotent: bool


@dataclass(frozen=True)
class OutcomeBundle:
    """One read-only, revalidated view of a Topic and its research brief."""

    topic: TopicRecord
    evidence: tuple[EvidenceRecord, ...]
    claims: tuple[ClaimRecord, ...]
    brief: ResearchBriefRecord | None

    def __post_init__(self) -> None:
        if type(self.topic) is not TopicRecord:
            raise TypeError("outcome bundle topic无效")
        evidence = tuple(self.evidence)
        claims = tuple(self.claims)
        if any(type(item) is not EvidenceRecord for item in evidence):
            raise TypeError("outcome bundle evidence无效")
        if any(type(item) is not ClaimRecord for item in claims):
            raise TypeError("outcome bundle claims无效")
        if self.brief is not None and type(self.brief) is not ResearchBriefRecord:
            raise TypeError("outcome bundle brief无效")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "claims", claims)

    @property
    def requires_review(self) -> bool:
        return any(
            item.recorded_state is not EvidenceState.CURRENT for item in self.evidence
        ) or (
            self.brief is not None
            and self.brief.visible_state is OutcomeState.NEEDS_REVIEW
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "topic": self.topic.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "claims": [item.to_dict() for item in self.claims],
            "brief": self.brief.to_dict() if self.brief is not None else None,
            "requires_review": self.requires_review,
        }


@dataclass(frozen=True)
class EvidenceSuggestion:
    """A strict-BM25 candidate that still requires explicit attachment."""

    knowledge_id: int
    locator: str
    title: str
    score: float

    def __post_init__(self) -> None:
        if type(self.knowledge_id) is not int or self.knowledge_id <= 0:
            raise ValueError("suggestion knowledge_id无效")
        parsed = EvidenceLocator.parse(self.locator)
        if parsed.kind is not EvidenceLocatorKind.ENTRY or parsed.knowledge_id != self.knowledge_id:
            raise ValueError("suggestion locator无效")
        object.__setattr__(
            self,
            "title",
            normalize_outcome_text(
                self.title,
                label="suggestion title",
                maximum=_MAX_TITLE_LENGTH,
            ),
        )
        if type(self.score) not in {int, float} or not 0.0 <= float(self.score) <= 1.0:
            raise ValueError("suggestion score无效")

    def to_dict(self) -> dict[str, object]:
        return {
            "knowledge_id": self.knowledge_id,
            "locator": self.locator,
            "title": self.title,
            "score": float(self.score),
        }


@dataclass(frozen=True)
class EvidenceSuggestionResult:
    """Provider-free BM25 suggestion result with stable degraded facts."""

    status: str
    suggestions: tuple[EvidenceSuggestion, ...]
    issues: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"success", "no_hits", "invalid", "error", "degraded"}:
            raise ValueError("suggestion status无效")
        suggestions = tuple(self.suggestions)
        if any(type(item) is not EvidenceSuggestion for item in suggestions):
            raise TypeError("suggestion items无效")
        object.__setattr__(self, "suggestions", suggestions)
        object.__setattr__(self, "issues", tuple(dict(item) for item in self.issues))

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": "bm25",
            "status": self.status,
            "suggestions": [item.to_dict() for item in self.suggestions],
            "issues": [dict(item) for item in self.issues],
        }


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
    "normalize_limitation_codes",
    "normalize_outcome_text",
    "normalize_user_text",
]
