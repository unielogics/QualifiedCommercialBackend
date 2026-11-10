"""Explicit NOI reviews do not manufacture P&L history or qualification facts."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers.financial_metrics import FinancialNoiReview, review_noi
from app.services import financial_metric_review, financial_noi_review
from app.tests.test_financial_metric_review import ACTOR, build, pnl, tax


def save(cockpit, state=None, **changes):
    payload = {"mode": "manual", "annual_value": 42000, "year": 2025, "currency": "USD",
               "reason": "Reviewed annual operating earnings and its supporting records",
               "expected_revision": (state or {}).get("revision", 0),
               "source_stamp": cockpit["noi"]["review"]["source_stamp"], "idempotency_key": "noi-review-test"}
    payload.update(changes)
    return financial_noi_review.change(state, cockpit["noi"]["review"], actor=ACTOR, **payload)


def business_tax(**facts):
    return tax(**{"tax_form": "1120S", "currency": "USD", "basis": "accrual", **facts})


def test_empty_file_can_save_manual_annual_noi_without_creating_a_source_statement():
    first = build([])
    assert first["noi"]["review"]["sources"] == [] and first["noi"]["headline"] is None
    state, changed = save(first)
    current = build([], state=state)
    headline = current["noi"]["headline"]
    assert changed and headline["value"] == headline["annualized_value"] == 42000
    assert headline["kind"] == "staff_reviewed_noi" and headline["staff_reviewed"]
    assert headline["year"] == "2025" and headline["currency"] == "USD"
    assert headline["period_start"] is None and headline["period_end"] is None
    assert headline["source_refs"] == [] and headline["source_value"] is None
    assert current["noi"]["years"] == [] and current["noi"]["source_headline"] is None
    assert current["noi"]["reason"] is None and not current["noi"]["review"]["stale"]
    for key in ("headline", "candidates", "potential"):
        assert current["ebitda"].get(key) == first["ebitda"].get(key)
    assert current["dscr"]["headline"] == first["dscr"]["headline"]
    assert current["noi"]["scenario"]["current"] == 42000
    assert current["noi"]["scenario"]["after_debt_service_current"] == 30000


def test_tax_calculation_is_provisional_then_staff_reviewed_without_da_addback():
    source = business_tax(ordinary_business_income=-54369, interest=6552, depreciation=99000)
    original = deepcopy(source)
    first = build([source])
    candidate = first["noi"]["review"]["sources"][0]
    assert candidate["eligible"] and candidate["year"] == 2025
    assert candidate["proposed_annual_value"] == -47817
    assert first["noi"]["headline"]["value"] == -47817
    assert first["noi"]["status"] == "provisional" and first["noi"]["review_needed"]
    state, _ = save(first, mode="source", selected_file_id="tax", annual_value=-47817)
    current = build([source], state=state)
    headline = current["noi"]["headline"]
    assert headline["kind"] == "tax_derived_noi" and headline["tax_derived_estimate"]
    assert "Tax-derived" in headline["label"] and headline["source_value"] == -47817
    assert headline["value"] == -47817 and current["noi"]["scenario"]["current"] == -47817
    assert current["noi"]["status"] == "reviewed" and not current["noi"]["review_needed"]
    assert current["noi"]["years"] == [] and source == original
    assert current["ebitda"]["headline"] == first["ebitda"]["headline"]
    assert current["dscr"]["headline"] == first["dscr"]["headline"]
    assert "non-operating" in " ".join(candidate["notes"])


def test_missing_tax_interest_remains_unknown_but_supported_reviewed_amount_can_be_entered():
    source = business_tax(interest=None)
    first = build([source])
    assert first["noi"]["review"]["sources"][0]["proposed_annual_value"] is None
    state, _ = save(first, mode="source", selected_file_id="tax", annual_value=100500)
    assert build([source], state=state)["noi"]["headline"]["value"] == 100500


@pytest.mark.parametrize("amount", [0, -10000, 1234.567])
def test_review_supports_zero_loss_and_cent_rounding(amount):
    state, _ = save(build([]), annual_value=amount)
    assert build([], state=state)["noi"]["headline"]["value"] == round(amount, 2)


def test_explicit_prior_year_tax_review_keeps_latest_source_pl_history_and_trend_independent():
    sources = [business_tax(), pnl(operating_income=40000)]
    first = build(sources)
    assert first["noi"]["source_headline"]["value"] == 40000
    state, _ = save(first, mode="source", selected_file_id="tax", annual_value=105000)
    current = build(sources, state=state)
    assert current["noi"]["headline"]["value"] == 105000
    assert current["noi"]["headline"]["year"] == "2025"
    assert current["noi"]["source_headline"] == first["noi"]["source_headline"]
    assert current["noi"]["candidates"] == first["noi"]["candidates"]
    assert current["noi"]["trend"] == first["noi"]["trend"]


def test_source_change_stales_review_without_silently_falling_back_to_original_pl():
    sources = [business_tax(), pnl(operating_income=40000)]
    state, _ = save(build(sources), mode="source", selected_file_id="tax")
    replacement = deepcopy(sources)
    replacement[0]["analysis_id"] = "replacement-analysis"
    current = build(replacement, state=state)
    assert current["noi"]["review"]["stale"]
    assert current["noi"]["headline"] is None
    assert current["noi"]["scenario"]["current"] is None
    assert current["noi"]["source_headline"]["value"] == 40000
    assert "Review and save" in current["noi"]["reason"]


def test_source_does_not_need_document_acceptance_to_record_a_staff_estimate():
    source = business_tax()
    source["accepted"] = False
    first = build([source])
    assert first["noi"]["review"]["sources"][0]["eligible"]
    state, _ = save(first, mode="source", selected_file_id="tax")
    result = build([source], state=state)
    assert result["noi"]["headline"]["staff_reviewed"]
    assert not result["noi"]["headline"]["source_accepted"]
    assert not source["accepted"]
    assert "does not accept" in " ".join(result["noi"]["headline"]["notes"])


def test_same_amount_with_new_provenance_stales_operating_plan_and_report_authority():
    from app.services.earnings_improvement import context_stamp
    from app.services.financial_calculation_policy import financial_authority

    first_state, _ = save(build([]))
    first = build([], state=first_state)
    second_state, _ = save(first, first_state, reason="Reconciled the same amount to a different annual basis",
                           idempotency_key="noi-review-second")
    second = build([], state=second_state)
    assert first["noi"]["headline"]["value"] == second["noi"]["headline"]["value"]
    assert context_stamp(first) != context_stamp(second)
    assert financial_authority(first)["signature"] != financial_authority(second)["signature"]


def test_explicit_reviewed_noi_reaches_operating_projection_without_replacing_earnings_or_coverage():
    from app.services.earnings_improvement import evaluate
    from app.tests.test_earnings_improvement import NOW, body, context

    state, _ = save(build([]))
    reviewed = build([], state=state)["noi"]
    cockpit = context()
    before = evaluate(body(), cockpit, now=NOW)
    cockpit["noi"] = reviewed
    after = evaluate(body(), cockpit, now=NOW)
    assert after["baseline"]["noi"] == 42000
    assert after["baseline"]["noi_staff_reviewed"]
    assert after["projection"]["proposed"]["noi"] == 42000 + after["totals"]["proposed"]["annual_impact"]
    assert after["baseline"]["earnings"] == before["baseline"]["earnings"]
    assert after["projection"]["proposed"]["dscr_current"] == before["projection"]["proposed"]["dscr_current"]
    assert any("explicit annual staff review" in note for note in after["risk_flags"])


def test_restoring_automatic_mode_restores_source_noi_and_preserves_review_history():
    sources = [pnl(operating_income=40000)]
    state, _ = save(build(sources))
    current = build(sources, state=state)
    restored, _ = save(current, state, mode="automatic", annual_value=None, year=None, currency=None,
                       idempotency_key="noi-restore-test")
    result = build(sources, state=restored)
    assert result["noi"]["headline"]["value"] == 40000
    assert not result["noi"]["headline"]["staff_adjusted"]
    assert len(result["noi"]["review"]["history"]) == 2


def test_replay_returns_original_receipt_even_if_revision_and_evidence_are_now_stale():
    first = build([])
    state, _ = save(first)
    replay, changed = save(first, state, expected_revision=0)
    assert replay == state and not changed
    with pytest.raises(HTTPException) as conflict:
        save(first, state, annual_value=9, expected_revision=0)
    assert conflict.value.status_code == 409


@pytest.mark.parametrize("changes", [
    {"annual_value": None}, {"annual_value": float("nan")}, {"annual_value": True},
    {"year": None}, {"year": True}, {"year": 1800}, {"currency": "XXX"},
    {"currency": None}, {"reason": "   "},
    {"period_start": "2025-01-01"},
    {"period_start": "2024-01-01", "period_end": "2025-12-31"},
    {"period_start": "2025-12-31", "period_end": "2025-01-01"},
    {"period_start": "2024-01-01", "period_end": "2024-12-31"},
    {"mode": "source"}, {"selected_file_id": "not-this-file"},
])
def test_invalid_review_does_not_change_state(changes):
    before = {"revision": 0, "events": []}
    with pytest.raises(HTTPException) as error:
        save(build([]), before, **changes)
    assert error.value.status_code == 422
    assert before == {"revision": 0, "events": []}


@pytest.mark.parametrize("mode", ["source", "manual"])
@pytest.mark.parametrize("changes", [{"year": 2024}, {"currency": "EUR"}])
def test_source_review_requires_matching_source_year_and_known_currency(changes, mode):
    with pytest.raises(HTTPException) as error:
        save(build([business_tax()]), mode=mode, selected_file_id="tax", **changes)
    assert error.value.status_code == 422


def test_legacy_tax_extraction_discloses_missing_quotation_verification_without_blocking_review():
    source = build([business_tax()])["noi"]["review"]["sources"][0]
    assert source["eligible"]
    assert "legacy_analysis_requires_explicit_reanalysis" in json.dumps(source["citation_status"])
    assert source["source_refs"][0]["citations"] == []
    assert any("No current server-validated source quotations" in note for note in source["notes"])


@pytest.mark.parametrize("facts", [{"entity_name": "Another Business LLC"}, {"tax_form": "1040"}])
def test_wrong_business_or_personal_return_is_never_an_eligible_business_noi_source(facts):
    first = build([business_tax(**facts)])
    assert not first["noi"]["review"]["sources"][0]["eligible"]
    with pytest.raises(HTTPException) as error:
        save(first, mode="source", selected_file_id="tax")
    assert error.value.status_code == 422


@pytest.mark.parametrize("changed", [{"expected_revision": 1}, {"source_stamp": "f" * 64}])
def test_revision_and_source_guards_reject_conflicting_save(changed):
    with pytest.raises(HTTPException) as error:
        save(build([]), **changed)
    assert error.value.status_code == 409


def test_external_read_removes_internal_noi_receipt_and_source_inventory():
    state, _ = save(build([]), reason="PRIVATE_REVIEW_NOTE")
    cockpit = build([], state=state)
    public = financial_metric_review.redact_for_external(cockpit, SimpleNamespace(role=Role.LENDER))
    assert public["noi"]["headline"]["value"] == 42000
    assert "review" not in public["noi"] and "review_receipt" not in public["noi"]["headline"]
    serialized = json.dumps(public)
    assert "PRIVATE_REVIEW_NOTE" not in serialized and "noi-review-test" not in serialized
    assert cockpit["noi"]["review"]["current"]["reason"] == "PRIVATE_REVIEW_NOTE"


@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.REGIONAL_MANAGER, Role.FIELD_REP, Role.LENDER])
@pytest.mark.asyncio
async def test_noi_route_denies_non_underwriting_roles_before_loading_profile(role):
    db = SimpleNamespace(commit=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await review_noi(uuid4(), FinancialNoiReview(mode="manual", annual_value=1, year=2025,
            currency="USD", reason="Source reviewed", expected_revision=0, source_stamp="a" * 64,
            idempotency_key="review-test"), SimpleNamespace(role=role), db)
    assert error.value.status_code == 403
    db.commit.assert_not_awaited()


def test_route_schema_rejects_boolean_amount_year_and_unknown_fields():
    valid = {"mode": "manual", "annual_value": 1, "year": 2025, "currency": "USD", "reason": "Reviewed",
             "expected_revision": 0, "source_stamp": "a" * 64, "idempotency_key": "test-review"}
    for change in ({"annual_value": True}, {"year": True}, {"year": 2025.5}, {"verified": True}):
        with pytest.raises(ValidationError):
            FinancialNoiReview(**(valid | change))


@pytest.mark.asyncio
async def test_route_uses_scoped_profile_and_commits_only_successful_review(monkeypatch):
    from app.routers import financial_metrics

    profile, user = SimpleNamespace(id=uuid4()), SimpleNamespace(role=Role.LOAN_EXEC)
    payload = FinancialNoiReview(mode="manual", annual_value=1, year=2025, currency="USD", reason="Reviewed",
        expected_revision=0, source_stamp="a" * 64, idempotency_key="test-review")
    loader, saver = AsyncMock(return_value=profile), AsyncMock(return_value={"saved": True})
    monkeypatch.setattr(financial_metrics.profiles, "load_profile", loader)
    monkeypatch.setattr(financial_metric_review, "save_noi_review", saver)
    db = SimpleNamespace(commit=AsyncMock())
    assert await review_noi(profile.id, payload, user, db) == {"saved": True}
    loader.assert_awaited_once_with(db, profile.id, user)
    saver.assert_awaited_once_with(db, profile, user, **payload.model_dump())
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistence_checks_refreshed_revision_after_acquiring_financial_lock(monkeypatch):
    from app.services import financial_cockpit, financial_workspace

    original = SimpleNamespace(id=uuid4(), financial_metric_review={"revision": 0, "events": []})
    current = SimpleNamespace(id=original.id, financial_metric_review={"revision": 1, "events": []})
    lock = AsyncMock()
    monkeypatch.setattr(financial_workspace, "lock_financial_profile", lock)
    monkeypatch.setattr(financial_cockpit, "load_cockpit", AsyncMock(return_value=build([], state=current.financial_metric_review)))
    result = SimpleNamespace(scalar_one=lambda: current)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    with pytest.raises(HTTPException) as error:
        await financial_metric_review.save_noi_review(db, original, ACTOR, mode="manual", annual_value=1,
            year=2025, currency="USD", reason="Reviewed", expected_revision=0,
            source_stamp=build([])["noi"]["review"]["source_stamp"], idempotency_key="test-review")
    assert error.value.status_code == 409
    lock.assert_awaited_once_with(db, original)
    assert original.financial_metric_review == {"revision": 0, "events": []}
