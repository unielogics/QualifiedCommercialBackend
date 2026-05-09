"""Plan builder tests — pure-Python helpers.

Locks down the pieces of `app/services/ai/plan_builder.py` that don't
need a DB:

  - `_pick_next_best`: highest-leverage pick + correct ordering.
  - `_compute_readiness_score`: ratio of satisfied required items.
  - `_build_resolver_context`: assembles the right context dict from
    a fake Client + realtor_profile.
  - `_serialize_requirement`: stable JSONB shape.

Full end-to-end (resolver + DB upsert) is exercised via the resolver
tests + the Phase 4 AI-router tests; here we just lock down the
deterministic helpers so the contract for callers stays stable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.services.ai.plan_builder import (
    _build_resolver_context,
    _compute_readiness_score,
    _pick_next_best,
    _serialize_requirement,
)
from app.services.ai.requirement_resolver import ResolvedRequirement


# ── Fixtures ────────────────────────────────────────────────────────


@dataclass
class FakeClient:
    id: uuid.UUID = uuid.uuid4()
    name: str = "Marcus Holloway"
    client_type: str | None = "buyer"
    realtor_profile: dict | None = None


def _resolved(
    *,
    key: str,
    label: str = "X",
    level: str = "required",
    blocks: str | None = None,
    order: int = 0,
) -> ResolvedRequirement:
    return ResolvedRequirement(
        requirement_key=key,
        label=label,
        category="fact",
        required_level=level,
        blocks_stage=blocks,
        visibility=["agent"],
        can_agent_override=False,
        can_underwriter_waive=True,
        verification_required=False,
        expiration_days=None,
        ai_request_message_template=None,
        display_order=order,
        source="platform",
        playbook_id=uuid.uuid4(),
        playbook_version=1,
        playbook_name="X",
    )


def _serialized(*, key: str, level: str, status: str, blocks: str | None = None, order: int = 0, template: str | None = None) -> dict:
    r = _resolved(key=key, label=key, level=level, blocks=blocks, order=order)
    if template is not None:
        r = ResolvedRequirement(**{**r.__dict__, "ai_request_message_template": template})
    return _serialize_requirement(r, status=status, source="platform", evidence_id=None)


# ── _build_resolver_context ────────────────────────────────────────


def test_resolver_context_pulls_buyer_facts() -> None:
    profile = {
        "client_type": "buyer",
        "buyer_profile": {
            "target_property_type": "multifamily",
            "financing_needed": True,
            "under_contract": False,
        },
        "known_facts": [],
    }
    ctx = _build_resolver_context(FakeClient(realtor_profile=profile), profile)
    assert ctx["client_type"] == "buyer"
    assert ctx["target_property_type"] == "multifamily"
    assert ctx["financing_needed"] is True
    assert ctx["under_contract"] is False


def test_resolver_context_borrower_type_from_known_facts() -> None:
    """When the realtor AI captured the borrower as an entity, the
    resolver context flips borrower_type so entity-gated lending
    requirements (operating_agreement) apply."""
    profile = {
        "client_type": "buyer",
        "buyer_profile": {},
        "known_facts": [
            {"field": "borrower_entity_type", "value": "LLC", "source": "agent"},
        ],
    }
    ctx = _build_resolver_context(FakeClient(realtor_profile=profile), profile)
    assert ctx["borrower_type"] == "entity"


def test_resolver_context_individual_borrower_when_personal() -> None:
    profile = {
        "client_type": "buyer",
        "buyer_profile": {},
        "known_facts": [
            {"field": "borrower_entity_type", "value": "individual", "source": "agent"},
        ],
    }
    ctx = _build_resolver_context(FakeClient(realtor_profile=profile), profile)
    assert ctx["borrower_type"] == "individual"


def test_resolver_context_handles_empty_profile() -> None:
    """No buyer_profile + no known_facts should yield a minimal but
    non-broken context."""
    profile = {}
    ctx = _build_resolver_context(FakeClient(client_type="buyer", realtor_profile=None), profile)
    assert ctx["client_type"] == "buyer"
    assert "borrower_type" not in ctx


# ── _pick_next_best ─────────────────────────────────────────────────


def test_pick_next_best_returns_none_for_empty_list() -> None:
    q, a = _pick_next_best([], {})
    assert q is None
    assert a is None


def test_pick_next_best_returns_none_when_all_satisfied() -> None:
    items = [_serialized(key="x", level="required", status="verified")]
    q, a = _pick_next_best(items, {})
    assert q is None
    assert a is None


def test_pick_next_best_prefers_required_over_recommended() -> None:
    items = [
        _serialized(key="optional_thing", level="optional", status="missing", order=0),
        _serialized(key="critical_thing", level="required", status="missing", order=1),
        _serialized(key="nice_to_have", level="recommended", status="missing", order=2),
    ]
    q, a = _pick_next_best(items, {})
    assert a is not None
    assert a["requirement_key"] == "critical_thing"


def test_pick_next_best_prefers_earlier_blocking_stage() -> None:
    """An item that blocks 'prequalification' beats one that blocks
    'closing' — fixing earlier stages unlocks more downstream work."""
    items = [
        _serialized(key="closing_thing", level="required", status="missing", blocks="closing", order=0),
        _serialized(key="prequal_thing", level="required", status="missing", blocks="prequalification", order=1),
    ]
    q, a = _pick_next_best(items, {})
    assert a is not None
    assert a["requirement_key"] == "prequal_thing"


def test_pick_next_best_uses_requirement_template_when_present() -> None:
    items = [_serialized(
        key="x", level="required", status="missing",
        template="Custom AI prompt for X — {{client_name}} please confirm.",
    )]
    q, a = _pick_next_best(items, {})
    assert q == "Custom AI prompt for X — {{client_name}} please confirm."


def test_pick_next_best_falls_back_to_generic_question() -> None:
    items = [_serialized(key="x", level="required", status="missing")]
    q, a = _pick_next_best(items, {})
    assert q is not None
    assert "x" in q.lower()


def test_pick_next_best_skips_already_provided_unverified() -> None:
    """provided_unverified is technically still open but ranks below
    `asked` and `missing` — the AI doesn't re-ask, it verifies."""
    items = [
        _serialized(key="provided", level="required", status="provided_unverified", order=0),
        _serialized(key="missing", level="required", status="missing", order=1),
    ]
    q, a = _pick_next_best(items, {})
    assert a is not None
    assert a["requirement_key"] == "missing"


# ── _compute_readiness_score ────────────────────────────────────────


def test_readiness_score_zero_when_no_required_items() -> None:
    items = [_serialized(key="x", level="optional", status="verified")]
    assert _compute_readiness_score(items) == 0


def test_readiness_score_full_when_all_required_satisfied() -> None:
    items = [
        _serialized(key="a", level="required", status="verified"),
        _serialized(key="b", level="required", status="uploaded"),
    ]
    assert _compute_readiness_score(items) == 100


def test_readiness_score_half_when_half_satisfied() -> None:
    items = [
        _serialized(key="a", level="required", status="verified"),
        _serialized(key="b", level="required", status="missing"),
    ]
    assert _compute_readiness_score(items) == 50


def test_readiness_score_ignores_recommended_items() -> None:
    """Recommended items don't count against the score — they're
    nice-to-haves the agent gets credit for separately."""
    items = [
        _serialized(key="a", level="required", status="verified"),
        _serialized(key="b", level="recommended", status="missing"),
    ]
    assert _compute_readiness_score(items) == 100


# ── _serialize_requirement ──────────────────────────────────────────


def test_serialize_requirement_round_trips_metadata() -> None:
    r = _resolved(key="bank_statements", label="Bank statements", level="required", blocks="underwriting")
    out = _serialize_requirement(r, status="missing", source="funding_required", evidence_id=None)
    assert out["requirement_key"] == "bank_statements"
    assert out["label"] == "Bank statements"
    assert out["required_level"] == "required"
    assert out["blocks_stage"] == "underwriting"
    assert out["status"] == "missing"
    assert out["source"] == "funding_required"
    # playbook provenance is preserved so the UI can show "from
    # funding playbook v1".
    assert "playbook_id" in out
    assert "playbook_version" in out


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
