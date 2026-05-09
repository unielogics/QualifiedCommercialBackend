"""Source-of-truth priority tests.

Pure-Python — no DB. Locks down:

  - Highest-trust source wins on conflict.
  - Single-candidate case: no conflict, value passes through.
  - Numeric tolerance: $875,000 vs $875,000.50 is NOT a conflict.
  - String case-insensitivity: "DSCR" vs "dscr" is NOT a conflict.
  - Lower-trust candidates that AGREE with the winner don't show up
    in `conflicting`.
  - merge_into_existing convenience wrapper.
"""

from __future__ import annotations

import pytest

from app.services.ai.fact_priority import (
    FactCandidate,
    SOURCE_PRIORITY,
    merge_into_existing,
    resolve_fact_conflict,
)


def test_single_candidate_returns_no_conflict() -> None:
    res = resolve_fact_conflict([FactCandidate(field="purchase_price", value=875000, source="agent_entered")])
    assert res is not None
    assert res.has_conflict is False
    assert res.winning_value == 875000
    assert res.winning_source == "agent_entered"
    assert res.conflicting == []


def test_empty_candidates_returns_none() -> None:
    assert resolve_fact_conflict([]) is None


def test_higher_trust_source_wins() -> None:
    chat = FactCandidate(field="purchase_price", value=900000, source="ai_extracted_chat")
    doc = FactCandidate(field="purchase_price", value=875000, source="verified_document", evidence_id="doc_1")
    res = resolve_fact_conflict([chat, doc])
    assert res is not None
    assert res.winning_value == 875000
    assert res.winning_source == "verified_document"
    assert res.winning_evidence_id == "doc_1"
    assert res.has_conflict is True
    assert len(res.conflicting) == 1
    assert res.conflicting[0].source == "ai_extracted_chat"


def test_underwriter_beats_verified_document() -> None:
    doc = FactCandidate(field="ltv", value=0.75, source="verified_document")
    uw = FactCandidate(field="ltv", value=0.70, source="underwriter_approved")
    res = resolve_fact_conflict([doc, uw])
    assert res is not None
    assert res.winning_source == "underwriter_approved"
    assert res.has_conflict is True


def test_agreeing_candidates_do_not_create_conflict() -> None:
    """A lower-trust source that agrees with the winner is filtered
    out of `conflicting`."""
    a = FactCandidate(field="purchase_price", value=875000, source="ai_extracted_chat")
    b = FactCandidate(field="purchase_price", value=875000, source="agent_entered")
    res = resolve_fact_conflict([a, b])
    assert res is not None
    assert res.has_conflict is False
    assert res.conflicting == []


def test_numeric_tolerance_not_a_conflict() -> None:
    """$875,000 and $875,000.50 (rounding artifact) shouldn't trip
    contradiction detection."""
    a = FactCandidate(field="purchase_price", value=875000, source="ai_extracted_chat")
    b = FactCandidate(field="purchase_price", value=875000.50, source="verified_document")
    res = resolve_fact_conflict([a, b])
    assert res is not None
    assert res.has_conflict is False


def test_string_case_insensitive_not_a_conflict() -> None:
    a = FactCandidate(field="loan_type", value="DSCR", source="agent_entered")
    b = FactCandidate(field="loan_type", value="dscr", source="ai_extracted_chat")
    res = resolve_fact_conflict([a, b])
    assert res is not None
    assert res.has_conflict is False


def test_mixed_field_raises() -> None:
    """Caller must group candidates by field — mixing fields is an
    API violation, not a soft failure."""
    a = FactCandidate(field="purchase_price", value=1, source="agent_entered")
    b = FactCandidate(field="ltv", value=2, source="agent_entered")
    with pytest.raises(ValueError):
        resolve_fact_conflict([a, b])


def test_merge_into_existing_with_no_existing_returns_incoming() -> None:
    incoming = FactCandidate(field="ltv", value=0.75, source="ai_extracted_chat")
    res = merge_into_existing(None, incoming)
    assert res.winning_value == 0.75
    assert res.has_conflict is False


def test_merge_into_existing_lower_trust_loses() -> None:
    """Lower-trust incoming value cannot overwrite higher-trust
    existing — the existing wins, incoming is in `conflicting`."""
    existing = FactCandidate(field="ltv", value=0.70, source="verified_document")
    incoming = FactCandidate(field="ltv", value=0.75, source="ai_extracted_chat")
    res = merge_into_existing(existing, incoming)
    assert res.winning_source == "verified_document"
    assert res.winning_value == 0.70
    assert res.has_conflict is True
    assert res.conflicting[0].source == "ai_extracted_chat"


def test_priority_order_matches_documented_hierarchy() -> None:
    """If this constant changes, the AI's silent-overwrite guarantee
    changes — lock it down."""
    assert SOURCE_PRIORITY["underwriter_approved"] > SOURCE_PRIORITY["verified_document"]
    assert SOURCE_PRIORITY["verified_document"] > SOURCE_PRIORITY["borrower_confirmed"]
    assert SOURCE_PRIORITY["borrower_confirmed"] > SOURCE_PRIORITY["agent_entered"]
    assert SOURCE_PRIORITY["agent_entered"] > SOURCE_PRIORITY["ai_extracted_chat"]


def test_unknown_source_scores_zero_and_loses() -> None:
    weird = FactCandidate(field="ltv", value=99, source="some_unknown_source")
    real = FactCandidate(field="ltv", value=0.7, source="ai_extracted_chat")
    res = resolve_fact_conflict([weird, real])
    assert res is not None
    assert res.winning_source == "ai_extracted_chat"
    assert res.winning_value == 0.7
    assert res.has_conflict is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
