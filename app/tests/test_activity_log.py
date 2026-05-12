"""Unit tests for app/services/activity_log.py — diff helpers,
visibility registry, and payload audience filtering. No DB."""

from __future__ import annotations

from app.services.activity_log import (
    CLIENT_VISIBLE,
    INTERNAL_ONLY,
    LOAN_DIFF_FIELDS,
    OPERATOR_VISIBLE,
    diff_changes,
    filter_payload_for_audience,
    is_visible_to,
    kind_visibility,
    loan_snapshot,
    summarize_diff,
)


# ── diff_changes ───────────────────────────────────────────────────


def test_diff_changes_detects_scalar_change():
    before = {"base_rate": 7.5, "amount": 500_000}
    after = {"base_rate": 7.8, "amount": 500_000}
    changes = diff_changes(before, after, fields=("base_rate", "amount"))
    assert len(changes) == 1
    assert changes[0]["field"] == "base_rate"
    assert changes[0]["before"] == 7.5
    assert changes[0]["after"] == 7.8


def test_diff_changes_returns_empty_when_nothing_changes():
    before = {"base_rate": 7.5}
    after = {"base_rate": 7.5}
    assert diff_changes(before, after, fields=("base_rate",)) == []


def test_diff_changes_handles_none_to_value():
    before = {"fico_override": None}
    after = {"fico_override": 715}
    changes = diff_changes(before, after, fields=("fico_override",))
    assert len(changes) == 1
    assert changes[0] == {"field": "fico_override", "before": None, "after": 715}


def test_diff_changes_decimal_to_float_equivalence():
    """Decimal('7.50') == 7.5 — must not show as a change."""
    from decimal import Decimal
    before = {"base_rate": Decimal("7.50")}
    after = {"base_rate": 7.5}
    assert diff_changes(before, after, fields=("base_rate",)) == []


def test_diff_changes_enum_unwrap():
    """StrEnum-like values compare by their .value attribute."""
    class _Fake:
        def __init__(self, v): self.value = v
    before = {"stage": _Fake("collecting_docs")}
    after = {"stage": _Fake("lender_connected")}
    changes = diff_changes(before, after, fields=("stage",))
    assert len(changes) == 1
    assert changes[0]["before"] == "collecting_docs"
    assert changes[0]["after"] == "lender_connected"


def test_diff_changes_restricts_to_field_subset():
    """When `fields` is given, untracked keys are ignored even if changed."""
    before = {"base_rate": 7.5, "updated_at": "old", "secret": 1}
    after = {"base_rate": 7.5, "updated_at": "new", "secret": 2}
    changes = diff_changes(before, after, fields=("base_rate",))
    assert changes == []


# ── summarize_diff ─────────────────────────────────────────────────


def test_summarize_diff_compact_for_three():
    changes = [
        {"field": "base_rate", "before": 7.5, "after": 7.8},
        {"field": "amount", "before": 500_000, "after": 520_000},
        {"field": "ltv", "before": 0.75, "after": 0.72},
    ]
    summary = summarize_diff(changes)
    assert "base_rate" in summary
    assert "7.5" in summary
    assert "7.8" in summary
    assert "amount" in summary
    assert "ltv" in summary


def test_summarize_diff_caps_with_overflow_note():
    changes = [{"field": f"f{i}", "before": 1, "after": 2} for i in range(5)]
    summary = summarize_diff(changes)
    assert "and 2 more" in summary


# ── visibility registry ────────────────────────────────────────────


def test_kind_visibility_known_kinds():
    assert kind_visibility("loan.stage_change") == CLIENT_VISIBLE
    assert kind_visibility("loan.criteria_changed") == OPERATOR_VISIBLE
    assert kind_visibility("loan.pricing_changed") == INTERNAL_ONLY
    assert kind_visibility("credit.fico_changed") == OPERATOR_VISIBLE


def test_kind_visibility_unknown_defaults_to_operator():
    """Safe default — never leak a new kind to client."""
    assert kind_visibility("totally.made_up_kind") == OPERATOR_VISIBLE


def test_is_visible_to_client():
    assert is_visible_to("loan.stage_change", "client") is True
    assert is_visible_to("loan.criteria_changed", "client") is False
    assert is_visible_to("loan.pricing_changed", "client") is False
    assert is_visible_to("ai_modify.correction_added", "client") is False


def test_is_visible_to_broker():
    assert is_visible_to("loan.stage_change", "broker") is True
    assert is_visible_to("loan.criteria_changed", "broker") is True
    assert is_visible_to("loan.pricing_changed", "broker") is False  # internal-only
    assert is_visible_to("ai_modify.correction_added", "broker") is False


def test_is_visible_to_super_admin_sees_everything():
    for kind in (
        "loan.stage_change",
        "loan.criteria_changed",
        "loan.pricing_changed",
        "ai_modify.correction_added",
        "ai.paused_by_super_admin",
    ):
        assert is_visible_to(kind, "super_admin") is True, kind


# ── filter_payload_for_audience ────────────────────────────────────


def test_filter_payload_strips_pricing_fields_for_client():
    payload = {
        "source": "operator_edit",
        "changes": [
            {"field": "base_rate", "before": 7.5, "after": 7.8},
            {"field": "amount", "before": 500_000, "after": 520_000},
            {"field": "discount_points", "before": 1.0, "after": 0.5},
            {"field": "term_months", "before": 360, "after": 240},
        ],
    }
    filtered = filter_payload_for_audience(payload, kind="loan.criteria_changed", audience="client")
    assert filtered is not None
    fields = [c["field"] for c in filtered["changes"]]
    assert "base_rate" not in fields
    assert "discount_points" not in fields
    assert "amount" in fields
    assert "term_months" in fields


def test_filter_payload_passthrough_for_internal():
    payload = {
        "changes": [
            {"field": "base_rate", "before": 7.5, "after": 7.8},
        ],
    }
    for audience in ("broker", "super_admin"):
        assert filter_payload_for_audience(payload, kind="loan.criteria_changed", audience=audience) is payload


def test_filter_payload_none_safe():
    assert filter_payload_for_audience(None, kind="loan.criteria_changed", audience="client") is None


# ── loan_snapshot ──────────────────────────────────────────────────


def test_loan_snapshot_covers_diff_fields():
    """The snapshot must include every field in LOAN_DIFF_FIELDS, so a
    later diff doesn't miss anything."""
    from types import SimpleNamespace
    loan = SimpleNamespace(**{f: i for i, f in enumerate(LOAN_DIFF_FIELDS)})
    snap = loan_snapshot(loan)
    for f in LOAN_DIFF_FIELDS:
        assert f in snap


def test_loan_snapshot_missing_fields_become_none():
    """Loans without all columns (legacy rows) snapshot to None for the
    missing fields instead of raising."""
    from types import SimpleNamespace
    loan = SimpleNamespace(base_rate=7.5)
    snap = loan_snapshot(loan)
    assert snap["base_rate"] == 7.5
    assert snap.get("discount_points") is None
