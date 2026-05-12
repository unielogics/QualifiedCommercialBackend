"""Unit tests for app/services/activity_log.py — diff helpers,
visibility registry, and payload audience filtering. No DB."""

from __future__ import annotations

from app.services.activity_log import (
    CLIENT_VISIBLE,
    INTERNAL_ONLY,
    LOAN_DIFF_FIELDS,
    OPERATOR_VISIBLE,
    diff_changes,
    field_label,
    filter_payload_for_audience,
    format_field_change,
    format_field_value,
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
    """The summary string is human-readable: column names become
    labels, values are formatted with the right unit (% / $)."""
    changes = [
        {"field": "base_rate", "before": 7.5, "after": 7.8},
        {"field": "amount", "before": 500_000, "after": 520_000},
        {"field": "ltv", "before": 0.75, "after": 0.72},
    ]
    summary = summarize_diff(changes)
    # Labels, not column names
    assert "Base rate" in summary
    assert "base_rate" not in summary
    assert "Loan amount" in summary
    assert "LTV" in summary
    # Formatted values
    assert "7.50%" in summary
    assert "7.80%" in summary
    assert "$500,000" in summary
    assert "$520,000" in summary
    assert "75.00%" in summary  # LTV stored as fraction
    assert "72.00%" in summary


def test_summarize_diff_caps_with_overflow_note():
    changes = [{"field": f"f{i}", "before": 1, "after": 2} for i in range(5)]
    summary = summarize_diff(changes)
    assert "and 2 more" in summary


# ── humanization helpers ───────────────────────────────────────────


def test_field_label_known_fields():
    assert field_label("base_rate") == "Base rate"
    assert field_label("fico_override") == "FICO override"
    assert field_label("monthly_hoa") == "Monthly HOA"
    assert field_label("ltv") == "LTV"


def test_field_label_unknown_falls_back_to_titlecase():
    assert field_label("some_brand_new_field") == "Some brand new field"


def test_format_field_value_money():
    assert format_field_value("amount", 500_000) == "$500,000"
    assert format_field_value("annual_taxes", 4500.0) == "$4,500"
    assert format_field_value("amount", None) == "—"


def test_format_field_value_percent_rate_vs_fraction():
    """base_rate is stored in percent units (7.5 → 7.50%) but LTV /
    vacancy / origination are stored as 0–1 fractions (0.75 → 75.00%).
    Must not confuse the two."""
    assert format_field_value("base_rate", 7.5) == "7.50%"
    assert format_field_value("final_rate", 8.25) == "8.25%"
    assert format_field_value("ltv", 0.75) == "75.00%"
    assert format_field_value("vacancy_pct", 0.05) == "5.00%"
    assert format_field_value("origination_pct", 0.015) == "1.50%"
    assert format_field_value("construction_holdback_pct", 0.20) == "20.00%"


def test_format_field_value_term_months_to_years():
    """360 months → 30 years (clean divides). 7 months stays as months."""
    assert format_field_value("term_months", 360) == "30 years"
    assert format_field_value("term_months", 240) == "20 years"
    assert format_field_value("term_months", 12) == "1 year"
    assert format_field_value("term_months", 7) == "7 months"
    assert format_field_value("seasoning_months", 6) == "6 months"


def test_format_field_value_enums_titlecase():
    assert format_field_value("amortization_style", "fully_amortizing") == "Fully Amortizing"
    assert format_field_value("entity_type", "llc") == "Llc"  # accepted — exact-case preserved by .title() rules
    assert format_field_value("stage", "lender_connected") == "Lender Connected"


def test_format_field_value_points_and_integers():
    assert format_field_value("discount_points", 1.25) == "1.250 pts"
    assert format_field_value("fico_override", 720) == "720"
    assert format_field_value("property_count", 5) == "5"


def test_format_field_value_dscr_keeps_two_decimals():
    assert format_field_value("dscr", 1.3) == "1.30"
    assert format_field_value("dscr", 1.275) == "1.27" or format_field_value("dscr", 1.275) == "1.28"


def test_format_field_change_full_line():
    """The single-line diff we render in the activity summary + AI
    prompt: 'Base rate: 7.50% → 7.80%'."""
    s = format_field_change({"field": "base_rate", "before": 7.5, "after": 7.8})
    assert s == "Base rate: 7.50% → 7.80%"


def test_format_field_change_handles_missing_values():
    """First-time-set or removed fields render with em-dash on the
    empty side."""
    s = format_field_change({"field": "fico_override", "before": None, "after": 715})
    assert s == "FICO override: — → 715"
    s2 = format_field_change({"field": "fico_override", "before": 700, "after": None})
    assert s2 == "FICO override: 700 → —"


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
