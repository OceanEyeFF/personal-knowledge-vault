"""Canonical, body-free identities shared by Outcome writes and migration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.outcomes.models import (
    ClaimRecord, EvidenceRecord, TopicRecord, EvidenceLocator,
    normalize_limitation_codes, normalize_outcome_text, _require_identifier,
)

RENDER_CONTRACT_VERSION = "research-brief.v2"
MAX_REVISION = 2**63 - 1
_SHA = re.compile(r"^[0-9a-f]{64}$")


def require_revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise ValueError("Outcome revision is outside the supported integer range")
    return value


def _sha(value: Any) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError("Outcome SHA-256 is invalid")
    return value


def _fields(value: Mapping, names: tuple[str, ...]) -> dict:
    if not isinstance(value, Mapping) or set(value) != set(names):
        raise ValueError("Outcome identity fields do not match the contract")
    return dict(value)


def _choice(value: Any, allowed: set[str]) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError("Outcome identity enum is invalid")
    return value


def _codes(value: Any) -> list[str]:
    if type(value) is not list:
        raise ValueError("Outcome limitation codes must be a JSON array")
    return list(normalize_limitation_codes(value))


def canonical_identity(domain: str, payload: Mapping) -> str:
    """Hash only canonical JSON scalars; floats and implicit coercions fail closed."""
    def validate(value: Any) -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                validate(item)
            return
        raise ValueError("Outcome identity contains a noncanonical JSON value")
    validate(payload)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(domain.encode("utf-8") + b"\x00" + encoded).hexdigest()


def outcome_input_payload(
    *, topic: TopicRecord | Mapping, evidence: Iterable[EvidenceRecord | Mapping],
    claims: Iterable[ClaimRecord | Mapping], input_revision: int | None = None,
    render_contract_version: str | None = None,
) -> dict:
    if isinstance(topic, TopicRecord):
        input_revision = topic.input_revision if input_revision is None else input_revision
        render_contract_version = topic.render_contract_version if render_contract_version is None else render_contract_version
        topic = {"topic_id": topic.topic_id, "outcome_id": topic.outcome_id, "title": topic.title,
                 "question": topic.question, "state": topic.state.value, "revision": topic.revision}
    projected_topic = _fields(topic, ("topic_id", "outcome_id", "title", "question", "state", "revision"))
    for name in ("topic_id", "outcome_id"):
        _require_identifier(projected_topic[name], label=name)
    projected_topic["title"] = normalize_outcome_text(projected_topic["title"], label="topic title", maximum=240)
    projected_topic["question"] = normalize_outcome_text(projected_topic["question"], label="topic question", maximum=4000)
    _choice(projected_topic["state"], {"active", "closed"})
    require_revision(projected_topic["revision"])
    require_revision(input_revision)
    if type(render_contract_version) is not str or not render_contract_version or len(render_contract_version) > 96:
        raise ValueError("Outcome render contract version is invalid")
    projected_evidence = []
    for item in evidence:
        if isinstance(item, EvidenceRecord):
            item = {"evidence_id": item.evidence_id, "ordinal": item.ordinal, "locator": item.locator.value,
                    "locator_kind": item.locator.kind.value, "source_revision_sha256": item.source_revision_sha256,
                    "acquisition_method": item.acquisition_method.value, "retrieval_status": item.retrieval_status,
                    "recorded_state": item.recorded_state.value, "limitation_codes": list(item.limitation_codes)}
        item = _fields(item, ("evidence_id", "ordinal", "locator", "locator_kind", "source_revision_sha256", "acquisition_method", "retrieval_status", "recorded_state", "limitation_codes"))
        _require_identifier(item["evidence_id"], label="evidence_id")
        require_revision(item["ordinal"])
        if EvidenceLocator.parse(item["locator"]).kind.value != item["locator_kind"]:
            raise ValueError("Outcome locator kind does not match")
        _sha(item["source_revision_sha256"])
        _choice(item["acquisition_method"], {"user_selected", "bm25", "relation"})
        _choice(item["retrieval_status"], {"not_requested", "success", "degraded"})
        _choice(item["recorded_state"], {"current", "stale", "unavailable"})
        item["limitation_codes"] = _codes(item["limitation_codes"])
        projected_evidence.append(item)
    projected_claims = []
    for item in claims:
        if isinstance(item, ClaimRecord):
            item = {"claim_id": item.claim_id, "ordinal": item.ordinal, "statement": item.statement,
                    "state": item.state.value, "revision": item.revision, "limitation_codes": list(item.limitation_codes),
                    "evidence_links": [link.to_dict() for link in item.evidence_links]}
        item = _fields(item, ("claim_id", "ordinal", "statement", "state", "revision", "limitation_codes", "evidence_links"))
        _require_identifier(item["claim_id"], label="claim_id")
        require_revision(item["ordinal"])
        require_revision(item["revision"])
        item["statement"] = normalize_outcome_text(item["statement"], label="claim statement", maximum=8000)
        _choice(item["state"], {"draft", "supported", "contested", "insufficient", "withdrawn"})
        item["limitation_codes"] = _codes(item["limitation_codes"])
        if type(item["evidence_links"]) is not list:
            raise ValueError("Outcome evidence links must be a JSON array")
        links = []
        for link in item["evidence_links"]:
            link = _fields(link, ("evidence_id", "polarity"))
            _require_identifier(link["evidence_id"], label="evidence_id")
            _choice(link["polarity"], {"support", "counter"})
            links.append(link)
        if len({link["evidence_id"] for link in links}) != len(links):
            raise ValueError("Outcome links are duplicated")
        item["evidence_links"] = sorted(links, key=lambda link: link["evidence_id"])
        projected_claims.append(item)
    for rows, key in ((projected_evidence, "evidence_id"), (projected_claims, "claim_id")):
        if len({row[key] for row in rows}) != len(rows):
            raise ValueError("Outcome inputs are duplicated")
        rows.sort(key=lambda row: (row["ordinal"], row[key]))
    evidence_ids = {row["evidence_id"] for row in projected_evidence}
    if any(link["evidence_id"] not in evidence_ids for row in projected_claims for link in row["evidence_links"]):
        raise ValueError("Outcome link target is absent")
    return {"input_revision": input_revision, "render_contract_version": render_contract_version,
            "topic": projected_topic, "evidence": projected_evidence, "claims": projected_claims}


def outcome_input_digest(**kwargs: Any) -> str:
    return canonical_identity("pkv.outcome.input.v1", outcome_input_payload(**kwargs))


def outcome_publication_id(*, outcome_id: str, candidate_revision: int, input_digest: str) -> str:
    _require_identifier(outcome_id, label="outcome_id")
    return canonical_identity("pkv.outcome.publication.v1", {"outcome_id": outcome_id,
        "candidate_revision": require_revision(candidate_revision), "input_digest": _sha(input_digest)})


def outcome_review_receipt_id(*, outcome_id: str, expected_revision: int, content_sha256: str,
                              expected_input_revision: int, action: str, reason_code: str | None) -> str:
    _require_identifier(outcome_id, label="outcome_id")
    _choice(action, {"approve", "return"})
    if action == "approve" and reason_code is not None:
        raise ValueError("Approval cannot carry a return reason")
    if action == "return":
        _choice(reason_code, {"needs_revision", "insufficient_evidence", "other"})
    return canonical_identity("pkv.outcome.review.v1", {"outcome_id": outcome_id,
        "expected_revision": require_revision(expected_revision), "content_sha256": _sha(content_sha256),
        "expected_input_revision": require_revision(expected_input_revision), "action": action, "reason_code": reason_code})
