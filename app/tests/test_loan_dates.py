from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.services.loan_dates import (
    add_business_days,
    client_terms_dates,
    dates_from_plan,
    listing_dates,
    loan_dates,
    preserve_plan_dates,
)


@pytest.mark.parametrize(("funding", "months", "maturity"), [
    ("2024-01-31", 1, "2024-02-29"), ("2025-01-31", 1, "2025-02-28"),
    ("2024-02-29", 12, "2025-02-28"), ("2026-12-31", 2, "2027-02-28"),
    ("2026-11-11", 24, "2028-11-11"),
])
def test_calendar_maturity_tracks_funding_and_term(funding, months, maturity):
    result = loan_dates(funding, months)
    assert result["maturity_date"] == maturity
    assert result["maturity_date_mode"] == "automatic"


def test_manual_override_survives_term_changes_until_automatic_is_restored():
    assert loan_dates("2026-01-31", 12, "2028-06-01", "manual")["maturity_date"] == "2028-06-01"
    assert loan_dates("2026-01-31", 24, "2028-06-01", "manual")["maturity_date"] == "2028-06-01"
    assert loan_dates("2026-01-31", 24, "2028-06-01", "automatic")["maturity_date"] == "2028-01-31"


@pytest.mark.parametrize("values", [("2026-01-01", 12, "2025-12-31", "manual"), ("2026-01-01", 12, None, "manual"), ("2026-01-01", 0, None, None), ("9999-12-31", 12, None, None)])
def test_invalid_or_unrepresentable_dates_cannot_be_saved(values):
    with pytest.raises(ValueError):
        loan_dates(*values)


def test_missing_funding_does_not_invent_a_date_or_drift_with_today():
    assert loan_dates(None, 24)["maturity_date"] is None
    row = SimpleNamespace(term_months=12, deal_budget_calculation={}, created_at=datetime(2026, 1, 2, 17, tzinfo=UTC), issued_at=None, closing_estimate_days=1)
    assert client_terms_dates(row)["expected_funding_date"] == "2026-01-05"
    assert client_terms_dates(row)["maturity_date"] == "2027-01-05"
    assert not client_terms_dates(row)["dates_recorded"]


def test_old_plan_clients_preserve_dates_and_automatic_maturity_recalculates():
    current = {"new_loan_expected_funding_date": "2026-01-31", "new_loan_maturity_date": "2027-01-31", "new_loan_maturity_mode": "automatic"}
    saved = preserve_plan_dates({"new_loan_term_months": 24}, current, {"new_loan_term_months"})
    assert saved["new_loan_maturity_date"] == "2028-01-31"
    cleared = preserve_plan_dates({"new_loan_term_months": 24, "new_loan_expected_funding_date": None, "new_loan_maturity_date": None, "new_loan_maturity_mode": "automatic"}, current, set(current))
    assert dates_from_plan(cleared)["maturity_date"] is None


def test_business_day_estimate_skips_weekend_and_recorded_snapshot_wins():
    assert add_business_days(date(2026, 1, 2), 1) == date(2026, 1, 5)
    row = SimpleNamespace(term_months=12, deal_budget_calculation={"loan_dates": loan_dates("2026-02-03", 12)}, created_at=None)
    assert client_terms_dates(row)["maturity_date"] == "2027-02-03"


def test_malformed_old_listing_metadata_does_not_break_listing_read():
    row = SimpleNamespace(source_snapshot={"loan_dates": {"expected_funding_date": "broken"}}, term_months=24, maturity_date=date(2028, 1, 1))
    assert listing_dates(row)["maturity_date"] == "2028-01-01"
    assert "Review" in listing_dates(row)["date_note"]
