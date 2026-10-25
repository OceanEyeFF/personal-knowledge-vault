"""Deterministic Markdown projection for one traceable research brief."""

from __future__ import annotations

from src.outcomes.models import OutcomeBundle, OutcomeState


def _markdown_text(value: str) -> str:
    """Render authored scalar text without allowing it to become Markdown syntax."""

    escaped = value.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "(", ")", "#", ">", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _codes(codes: tuple[str, ...]) -> str:
    return ", ".join(f"`{code}`" for code in codes) if codes else "none"


def render_research_brief(
    bundle: OutcomeBundle,
    *,
    outcome_id: str,
    revision: int,
) -> str:
    """Return the complete immutable Markdown revision for a validated bundle.

    This renderer intentionally receives no Entry content, raw source URL, local
    path, retrieval highlight, or Provider result.  A locator and the captured
    source digest are enough to make the reasoning trail inspectable while
    retaining the storage/privacy boundary.
    """

    lines = [
        f"# Research Brief: {_markdown_text(bundle.topic.title)}",
        "",
        f"- Topic ID: `{bundle.topic.topic_id}`",
        f"- Topic revision: `{bundle.topic.revision}`",
        f"- Outcome ID: `{outcome_id}`",
        f"- Outcome revision: `{revision}`",
        f"- Review state: `{OutcomeState.AWAITING_REVIEW.value}`",
        "",
        "## Inquiry",
        "",
        _markdown_text(bundle.topic.question),
        "",
        "## Claims",
        "",
    ]
    evidence_by_id = {evidence.evidence_id: evidence for evidence in bundle.evidence}
    if not bundle.claims:
        lines.extend(["No claims have been recorded.", ""])
    for claim in bundle.claims:
        lines.extend(
            [
                f"### C{claim.ordinal}: `{claim.state.value}`",
                "",
                _markdown_text(claim.statement),
                "",
                f"- Limitations: {_codes(claim.limitation_codes)}",
                "- Evidence links:",
            ]
        )
        if not claim.evidence_links:
            lines.append("  - none")
        for link in claim.evidence_links:
            evidence = evidence_by_id.get(link.evidence_id)
            if evidence is None:
                # A detached source must never be silently rebound.  In normal
                # use ON DELETE CASCADE removes the link before this render, but
                # retaining the marker makes malformed legacy DB state visible.
                lines.append(
                    f"  - `{link.polarity.value}` · `{link.evidence_id}` · unavailable"
                )
                continue
            lines.append(
                "  - "
                f"`{link.polarity.value}` · E{evidence.ordinal} · "
                f"`{evidence.locator.value}` · state `{evidence.recorded_state.value}`"
            )
        lines.append("")

    lines.extend(["## Evidence Ledger", ""])
    if not bundle.evidence:
        lines.extend(["No evidence has been attached.", ""])
    for evidence in bundle.evidence:
        lines.extend(
            [
                f"### E{evidence.ordinal}",
                "",
                f"- Evidence ID: `{evidence.evidence_id}`",
                f"- Locator: `{evidence.locator.value}`",
                f"- Locator kind: `{evidence.locator.kind.value}`",
                f"- Captured source revision: `{evidence.source_revision_sha256}`",
                f"- Acquisition: `{evidence.acquisition_method.value}`",
                f"- Retrieval status: `{evidence.retrieval_status}`",
                f"- Binding state: `{evidence.recorded_state.value}`",
                f"- Limitations: {_codes(evidence.limitation_codes)}",
                "",
            ]
        )

    lines.extend(
        [
            "## Review",
            "",
            "This brief is a traceable research artifact, not an automatic truth assertion.",
            "It awaits explicit review. Any source drift or unavailable evidence requires a new review.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["render_research_brief"]
