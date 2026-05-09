"""Source-of-truth resolver for conflicting facts.

When two sources disagree on a fact (e.g. purchase price), the
higher-trust source wins. The AI never silently overwrites a
higher-trust value with a lower-trust one — it flags a conflict so
the caller can surface a contradiction action card.

Order (highest → lowest):

  underwriter_approved   The funding team has manually accepted this
                         value. Final say.
  verified_document      Pulled from a document that was scanned and
                         confirmed by analysis (or human review).
  borrower_confirmed     The borrower confirmed in chat (e.g. they
                         answered "yes that's right" to an AI prompt).
  agent_entered          The agent typed it in directly via the UI.
  ai_extracted_chat      The AI extracted it from conversational text
                         without explicit confirmation. Lowest trust.

Pure-Python — no DB I/O. Caller passes in the candidate set and gets
back a `FactResolution` describing the winner + any conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SOURCE_PRIORITY: dict[str, int] = {
    "underwriter_approved": 5,
    "verified_document": 4,
    "borrower_confirmed": 3,
    "agent_entered": 2,
    "ai_extracted_chat": 1,
}


@dataclass(frozen=True)
class FactCandidate:
    """One claimed value for a single field, with provenance."""
    field: str
    value: Any
    source: str
    """One of SOURCE_PRIORITY keys. Unknown sources score 0."""
    evidence_id: str | None = None
    """document_id or message_id backing this candidate, when relevant."""


@dataclass
class FactResolution:
    """Result of resolving a conflict for a single field."""
    field: str
    winning_value: Any
    winning_source: str
    winning_evidence_id: str | None
    has_conflict: bool
    conflicting: list[FactCandidate] = field(default_factory=list)
    """Lower-trust candidates that disagreed with the winner. Empty
    when there was no conflict (single candidate, or all candidates
    agreed)."""


def _score(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 0)


def resolve_fact_conflict(candidates: list[FactCandidate]) -> FactResolution | None:
    """Pick the highest-trust candidate. Surface lower-trust candidates
    that disagree with the winner as `conflicting` for the caller to
    render as contradiction action cards.

    Returns None if `candidates` is empty (caller should treat as
    "no fact known" — neither set nor conflicted).

    All candidates MUST share the same `field`. Caller is responsible
    for grouping by field before calling.

    "Agree" is determined by simple equality after light normalization
    (case-insensitive string compare, numeric tolerance). Anything
    else is treated as conflict."""
    if not candidates:
        return None
    fld = candidates[0].field
    if any(c.field != fld for c in candidates):
        raise ValueError(
            f"resolve_fact_conflict received candidates spanning multiple "
            f"fields ({sorted({c.field for c in candidates})}). Group by "
            f"field before calling."
        )

    if len(candidates) == 1:
        c = candidates[0]
        return FactResolution(
            field=fld,
            winning_value=c.value,
            winning_source=c.source,
            winning_evidence_id=c.evidence_id,
            has_conflict=False,
            conflicting=[],
        )

    # Sort highest-trust first; tie-broken by stable order. Anything
    # below the winner that DISAGREES is a conflict.
    ordered = sorted(candidates, key=lambda c: _score(c.source), reverse=True)
    winner = ordered[0]
    losers = [c for c in ordered[1:] if not _values_agree(c.value, winner.value)]
    return FactResolution(
        field=fld,
        winning_value=winner.value,
        winning_source=winner.source,
        winning_evidence_id=winner.evidence_id,
        has_conflict=bool(losers),
        conflicting=losers,
    )


def merge_into_existing(
    existing: FactCandidate | None,
    incoming: FactCandidate,
) -> FactResolution:
    """Convenience for the common case: an extracted-from-doc fact
    arrives and the caller wants to know whether to overwrite the
    chat-captured value or surface a conflict.

    Returns a FactResolution. If `existing` is None, the incoming
    value wins outright. If both exist and disagree, the higher-trust
    one wins; the lower-trust one shows up in `conflicting`."""
    if existing is None:
        return FactResolution(
            field=incoming.field,
            winning_value=incoming.value,
            winning_source=incoming.source,
            winning_evidence_id=incoming.evidence_id,
            has_conflict=False,
            conflicting=[],
        )
    res = resolve_fact_conflict([existing, incoming])
    assert res is not None
    return res


def _values_agree(a: Any, b: Any) -> bool:
    """Light equality with case-insensitive string compare + numeric
    tolerance for floats. None compares equal to None only."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        # Within 1% tolerance — guards against $875000 vs $875,000.50
        # being flagged as a conflict.
        if a == b:
            return True
        if max(abs(a), abs(b)) == 0:
            return True
        return abs(a - b) / max(abs(a), abs(b)) <= 0.01
    return a == b
