from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers.financial_metrics import FinancialMetricCorrection, correct_metrics
from app.services import financial_cockpit as cockpit
from app.services import financial_metric_review as review

ACTOR = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC, name="Staff reviewer")
ENTITY = "Example LLC"


def file(kind, facts, *, key="source", analysis="analysis-1"):
    return {"file_id": key, "file_name": f"{key}.pdf", "analysis_id": analysis, "content_hash": "content-1",
            "accepted": True, "classification": kind, "key_facts": facts}


def pnl(**extra):
    return file("current_p_and_l", {"business_name": ENTITY, "currency": "USD", "basis": "accrual",
                                   "period_start": "2026-01-01", "period_end": "2026-06-30",
                                   "gross_revenue": 200000, "net_income": 20000, "interest": 1000,
                                   "income_taxes": 2000, "depreciation_and_amortization": 3000, **extra})


def tax(**extra):
    return file("tax_return", {"entity_name": ENTITY, "tax_year": "2025", "ordinary_business_income": 100000,
                              "interest": 5000, "depreciation": 10000, **extra}, key="tax")


def build(analyses=None, state=None, **extra):
    return cockpit.build_cockpit(analyses if analyses is not None else [pnl()],
                                [{"id": "debt", "lender": "Lender", "monthly_payment": 1000, "balance": 10000, "origin": "admin"}],
                                business_entity=ENTITY, metric_review_state=state, **extra)


def target(c, key="profit_and_loss.net_income"):
    return next(i for i in c["metric_review"]["inputs"] if i["key"] == key)


def correction(c, key="profit_and_loss.net_income", value=30000, state=None, **extra):
    return review.change(state, c["metric_review"], input_key=key, action="correct", value=value,
                         reason="Checked the source statement", expected_revision=(state or {}).get("revision", 0),
                         source_stamp=target(c, key)["source_stamp"], idempotency_key="correction-1", actor=ACTOR, **extra)[0]


def test_complete_pl_reconciles_once_and_dscr_keeps_tax_precedence():
    c = build([tax(), pnl()])
    result = c["ebitda"]["headline"]
    assert result["kind"] == "computed_ebitda" and result["value"] == 26000
    assert result["annualized_value"] == 52000 and result["entity_comparable"]
    assert c["dscr"]["headline"]["key"] == "tax_return"
    assert c["dscr"]["headline"]["numerator"] == 115000
    assert "dscr.profit_and_loss" in target(c)["used_by"]
    assert not target(c)["used_by_headline"]
    assert target(c)["source_refs"][0]["analysis_id"] == "analysis-1"


@pytest.mark.parametrize("currency,status,numerator_currency", [("USD", "comparable", "USD"), ("EUR", "mismatch", "EUR"), (None, "unknown", None), ("XXX", "unknown", None)])
def test_dscr_blocks_known_currency_mismatch_and_retains_currency_provenance(currency, status, numerator_currency):
    c = build([pnl(currency=currency)])
    candidate = c["dscr"]["candidates"][0]
    calculation = next(row for row in c["metric_review"]["calculations"] if row["key"] == "dscr.profit_and_loss")
    if status == "mismatch":
        assert c["dscr"]["headline"] is None and c["dscr"]["pro_forma_headline"] is None
        assert candidate["source_numerator"] == 52000
        assert candidate["value"] is None and candidate["numerator"] is None
        assert candidate["pro_forma_value"] is None and candidate["status"] == "unavailable"
    else:
        assert c["dscr"]["headline"] is candidate
        assert candidate["value"] == 4.33 and candidate["numerator"] == 52000
    for result in (candidate, calculation):
        assert result["currency_status"] == status and result["numerator_currency"] == numerator_currency
        assert result["denominator_currency"] == "USD"
        assert result["currency_comparable"] == (status == "comparable")
        if status != "comparable":
            assert "without currency conversion" in result["currency_note"]
    assert c["ebitda"]["headline"]["provisional"] == (status == "unknown")
    assert target(c, "debt.annual")["source_refs"][0]["currency"] == "USD"


def test_comparable_pl_fallback_leads_when_tax_currency_is_ineligible():
    c = build([tax(currency="EUR"), pnl(currency="USD")])
    assert c["ebitda"]["headline"]["currency"] == "USD"
    assert c["dscr"]["headline"]["key"] == "profit_and_loss"
    assert c["dscr"]["headline"]["value"] == 4.33
    tax_candidate = next(row for row in c["dscr"]["candidates"] if row["key"] == "tax_return")
    assert tax_candidate["source_numerator"] == 115000
    assert tax_candidate["value"] is None and tax_candidate["numerator"] is None
    assert tax_candidate["currency_status"] == "mismatch"
    assert tax_candidate["source_currencies"] == ["EUR"]
    assert "EUR" in tax_candidate["note"]
    selected = [row["key"] for row in c["metric_review"]["calculations"] if row.get("selected")]
    assert selected == ["dscr.profit_and_loss"]


def test_unknown_accounting_basis_keeps_complete_operand_ebitda_provisional():
    c = build([pnl(basis=None)])
    result = c["ebitda"]["headline"]
    assert result["value"] == 26000 and result["annualized_value"] == 52000
    assert result["complete"] and result["provisional"]
    assert any("accounting basis is unconfirmed" in note for note in result["notes"])


def test_mixed_bank_source_currencies_are_explicit_without_fx_or_ebitda():
    analyses = [file("bank_statement", {"currency": currency, "bank": "Bank", "account_last4": "1234",
                                       "statement_period": f"2026-0{month}", "total_deposits_and_credits": 10000,
                                       "total_withdrawals_and_debits": 8000}, key=f"bank-{month}") for month, currency in ((1, "EUR"), (2, "USD"), (3, "USD"))]
    c = build(analyses)
    assert c["ebitda"]["headline"] is None
    assert c["dscr"]["headline"] is None
    candidate = c["dscr"]["candidates"][0]
    assert candidate["source_numerator"] == 36000
    assert candidate["numerator"] is None and candidate["value"] is None
    assert candidate["source_currencies"] == ["EUR", "USD"]
    assert candidate["numerator_currency"] is None and candidate["currency_status"] == "mismatch"


def test_effective_operand_changes_dscr_plan_and_net_profit_but_not_raw_analysis():
    analyses = [pnl()]
    before = deepcopy(analyses)
    c = build(analyses)
    state = correction(c)
    result = build(analyses, state)
    assert analyses == before
    assert result["ebitda"]["headline"]["value"] == 36000
    assert result["dscr"]["headline"]["value"] == 6
    assert result["plan"]["evaluation"]["headline_before"] == 6
    assert result["net_profit"]["conclusion"]["value"] == 60000
    assert result["debt"]["service"]["annual"] == 12000


def test_unknowns_and_explicit_zero_are_distinct_and_tax_is_only_estimate():
    c = build([tax(interest=None, depreciation=None)])
    assert c["ebitda"]["headline"]["kind"] == "tax_estimate"
    assert c["ebitda"]["headline"]["provisional"] and not c["ebitda"]["headline"]["complete"]
    assert len(c["ebitda"]["headline"]["missing_inputs"]) == 2
    key = next(i["key"] for i in c["metric_review"]["inputs"] if i["key"].endswith(".interest"))
    state = correction(c, key, 0)
    updated = build([tax(interest=None, depreciation=None)], state)
    assert target(updated, key)["value"] == 0 and not target(updated, key)["missing"]


def test_incomplete_pl_does_not_invent_ebitda_but_known_zero_is_valid():
    c = build([pnl(interest=None)])
    assert c["ebitda"]["headline"] is None
    assert build([pnl(net_income=0, interest=0, income_taxes=0, depreciation_and_amortization=0)])["ebitda"]["headline"]["value"] == 0


def test_explicit_unknown_correction_removes_an_invented_operand_without_turning_it_zero():
    c = build()
    state = correction(c, value=None, mark_unknown=True)
    updated = build(state=state)
    assert target(updated)["value"] is None and target(updated)["missing"]
    assert target(updated)["correction"]["mark_unknown"]
    assert updated["ebitda"]["headline"] is None and not updated["dscr"]["candidates"]


def test_explicit_unknown_reporting_month_count_disables_annualization():
    c = build()
    state = correction(c, "profit_and_loss.months_covered", None, mark_unknown=True)
    updated = build(state=state)
    assert updated["ebitda"]["headline"]["value"] == 26000
    assert updated["ebitda"]["headline"]["annualized_value"] is None


def test_reported_ebitda_conflict_is_visible_and_not_double_counted():
    c = build([pnl(ebitda=50000)])
    result = c["ebitda"]["headline"]
    assert result["value"] == 50000 and result["computed_value"] == 26000
    assert any("differs" in note for note in result["notes"])
    updated = build([pnl(ebitda=50000)], correction(c))
    assert updated["dscr"]["headline"]["numerator"] == 100000
    assert updated["ebitda"]["headline"]["computed_value"] == 36000
    assert "profit_and_loss.reconciliation" in target(updated)["used_by"]


@pytest.mark.parametrize("printed", ["$100,000", "100,000", "(100,000)"])
def test_reported_numeric_format_keeps_existing_extractor_dscr_basis(printed):
    c = build([pnl(ebitda=printed, interest=None)])
    expected = -100000 if printed.startswith("(") else 100000
    assert c["ebitda"]["headline"]["value"] == expected
    assert c["dscr"]["headline"]["numerator"] == expected * 2


@pytest.mark.parametrize("end", ["2026-04-30", None])
def test_short_or_unknown_period_never_annualizes_ebitda(end):
    c = build([pnl(period_end=end)])
    result = c["ebitda"]["headline"]
    assert result["value"] == 26000 and result["annualized_value"] is None
    assert not c["dscr"]["candidates"]


def test_period_correction_is_explicit_audited_and_retains_original_dates():
    source = pnl(period_end="2026-04-30")
    c = build([source])
    state = correction(c, "profit_and_loss.months_covered", 6)
    updated = build([source], state)
    assert updated["ebitda"]["headline"]["annualized_value"] == 52000
    assert updated["profit_and_loss"]["period_end"] == "2026-04-30"
    assert any("corrected" in note for note in updated["ebitda"]["headline"]["notes"])


def test_signed_losses_tax_benefits_and_cents_are_preserved():
    c = build([pnl(net_income=-100, income_taxes=-10)])
    state = correction(c, value=-200.125)
    result = build([pnl(net_income=-100, income_taxes=-10)], state)
    assert target(result)["value"] == -200.12
    assert result["ebitda"]["headline"]["value"] == 3789.88


@pytest.mark.parametrize("kind", ["analysis", "hash", "period", "entity"])
def test_changed_source_context_stales_correction_without_applying(kind):
    c = build()
    state = correction(c)
    source = pnl()
    if kind == "analysis":
        source["analysis_id"] = "analysis-2"
    elif kind == "hash":
        source["content_hash"] = "replacement"
    elif kind == "period":
        source["key_facts"]["period_end"] = "2026-07-31"
    elif kind == "entity":
        source["key_facts"]["business_name"] = "Different LLC"
    updated = build([source], state)
    assert target(updated)["value"] == 20000
    assert len(updated["metric_review"]["stale_corrections"]) == 1


def test_earnings_correction_survives_debt_version_and_supporting_evidence_changes():
    c = build()
    state = correction(c)
    version = {"body": {"debts": [{"id": "debt", "lender": "Lender", "balance": 5000, "monthly_payment": 500}]}, "version_id": "new-version"}
    updated = build([pnl()], state, accepted_schedule=version)
    assert target(updated)["value"] == 30000
    assert not updated["metric_review"]["stale_corrections"]
    assert updated["dscr"]["headline"]["value"] == 12
    assert updated["metric_review"]["source_stamp"] != c["metric_review"]["source_stamp"]


def test_known_wrong_business_tax_is_excluded_before_earnings_and_correction_inputs():
    c = build([tax(entity_name="Other Inc"), pnl()])
    assert c["ebitda"]["headline"]["key"] == "profit_and_loss"
    assert c["dscr"]["headline"]["key"] == "profit_and_loss"
    assert not any(row["key"] == "dscr.tax_return" for row in c["metric_review"]["calculations"])
    wrong_inputs = [i for i in c["metric_review"]["inputs"] if i["key"].startswith("tax_return:")]
    assert wrong_inputs == []
    assert c["metric_review"]["excluded_income_sources"] == [{"file_id": "tax", "file_name": "tax.pdf", "reason": "Business identity does not match this file; review the association in Evidence."}]


def test_idempotent_replay_before_revision_and_source_checks_and_restore():
    c = build()
    state = correction(c)
    duplicate = correction(c, state=None)
    assert duplicate["revision"] == 1
    event = state["events"][0]
    replay, changed = review.change(state, {"inputs": []}, input_key=event["input_key"], action="correct", value=30000,
                                   reason=event["reason"], expected_revision=0, source_stamp=event["source_stamp"], idempotency_key="correction-1", actor=ACTOR)
    assert not changed and replay == state
    current = build(state=state)
    restored, _ = review.change(state, current["metric_review"], input_key=event["input_key"], action="restore", value=None,
                               reason="Restore source reading", expected_revision=1, source_stamp=event["source_stamp"], idempotency_key="restore-1", actor=ACTOR)
    assert build(state=restored)["ebitda"]["headline"]["value"] == 26000
    assert len(restored["events"]) == 2 and restored["events"][1]["actor_id"] == str(ACTOR.id)
    assert restored["events"][1]["effective_value"] == 20000


@pytest.mark.parametrize("problem", ["revision", "source", "reason", "null", "nan", "debt", "role", "months"])
def test_invalid_or_unscoped_corrections_rejected(problem):
    c = build()
    payload = dict(input_key="profit_and_loss.net_income", action="correct", value=0, reason="Checked source",
                   expected_revision=0, source_stamp=target(c)["source_stamp"], idempotency_key="request-1", actor=ACTOR)
    if problem == "revision":
        payload["expected_revision"] = 1
    elif problem == "source":
        payload["source_stamp"] = "wrong"
    elif problem == "reason":
        payload["reason"] = " "
    elif problem in {"null", "nan"}:
        payload["value"] = None if problem == "null" else float("nan")
    elif problem == "debt":
        payload["input_key"] = "debt.monthly"
    elif problem == "role":
        payload["actor"] = SimpleNamespace(role=Role.BROKER)
    elif problem == "months":
        payload.update(input_key="profit_and_loss.months_covered", value=4.5, source_stamp=target(c, "profit_and_loss.months_covered")["source_stamp"])
    with pytest.raises(HTTPException):
        review.change({}, c["metric_review"], **payload)


def test_bank_correction_only_affects_cash_proxy_dscr_not_statement_figures():
    analyses = [file("bank_statement", {"bank": "Bank", "account_last4": "1234", "statement_period": f"2026-0{month}",
                                       "total_deposits_and_credits": 10000, "total_withdrawals_and_debits": 8000}, key=f"bank-{month}") for month in (1, 2, 3)]
    c = build(analyses)
    state = correction(c, "bank.avg_net", 3000)
    updated = build(analyses, state)
    assert updated["ebitda"]["headline"] is None
    assert updated["dscr"]["headline"]["numerator"] == 48000
    assert updated["bank"]["summary"]["avg_net"] == 2000
    assert updated["net_profit"]["conclusion"]["value"] == 24000


@pytest.mark.asyncio
async def test_router_requires_underwriting_before_loading_profile_or_writing():
    payload = FinancialMetricCorrection(input_key="x", action="correct", value=0, reason="Reason", expected_revision=0, source_stamp="x" * 64, idempotency_key="request-1")
    with patch("app.routers.financial_metrics.profiles.load_profile", AsyncMock()) as load:
        with pytest.raises(HTTPException) as caught:
            await correct_metrics(uuid4(), payload, SimpleNamespace(role=Role.BROKER), AsyncMock())
    assert caught.value.status_code == 403 and not load.called


def test_transport_rejects_nonfinite_and_unexpected_data():
    with pytest.raises(ValidationError):
        FinancialMetricCorrection(input_key="x", action="correct", value=float("inf"), reason="Reason", expected_revision=0, source_stamp="x" * 64, idempotency_key="request-1")


def test_tax_short_period_is_not_presented_as_annualized_new_ebitda():
    c = build([tax(period_start="2025-07-01", period_end="2025-12-31")])
    result = c["ebitda"]["headline"]
    assert result["months_covered"] == 6 and result["value"] == 115000
    assert result["annualized_value"] is None and result["annualization_factor"] is None
    assert c["dscr"]["headline"] is None


def test_operand_names_the_actual_tax_alias_and_no_invented_page():
    c = build([tax(ordinary_business_income=None, ordinary_business_income_loss_current=100000,
                   interest=None, interest_expense=5000)])
    tax_inputs = [i for i in c["metric_review"]["inputs"] if i["key"].startswith("tax_return:")]
    income = next(i for i in tax_inputs if i["key"].endswith(".income"))
    interest = next(i for i in tax_inputs if i["key"].endswith(".interest"))
    assert income["source_refs"][0]["fact_key"] == "ordinary_business_income_loss_current"
    assert interest["source_refs"][0]["fact_key"] == "interest_expense"
    assert not income["source_refs"][0]["pages"]


def test_verified_field_citation_retains_its_page_and_quote():
    from app.services.financial_fact_citations import build_financial_citation_manifest

    source = pnl()
    source["content_hash"] = "a" * 64
    analysis = SimpleNamespace(id=source["analysis_id"], bucket_file_id=source["file_id"], content_hash=source["content_hash"],
                               status="completed", analysis={"key_facts": source["key_facts"],
                               "fact_citations": {"net_income": [{"page": 2, "line_label": "Net income", "quote": "Net income 20000", "value": "20000", "measure": "net_income"}]}})
    source["financial_fact_citations"] = build_financial_citation_manifest(analysis, source_pages={2: "Net income 20000"}, source_kind="pdf_searchable", source_hash=analysis.content_hash)
    c = build([source])
    ref = target(c)["source_refs"][0]
    assert ref["pages"] == [2] and ref["citations"][0]["quote"] == "Net income 20000"


def test_internal_audit_is_not_exposed_to_external_numeric_readers():
    c = build()
    updated = build(state=correction(c))
    safe = review.redact_for_external(updated, SimpleNamespace(role=Role.CLIENT))
    assert not safe["metric_review"]["history"]
    assert target(safe)["correction"] == {"value": 30000, "at": target(updated)["correction"]["at"]}
    assert not target(safe)["editable"] and updated["metric_review"]["history"]


@pytest.mark.asyncio
async def test_waiting_writer_refreshes_cached_profile_before_revision_check():
    stale_profile = SimpleNamespace(id=uuid4(), financial_metric_review={})
    committed = SimpleNamespace(id=stale_profile.id, financial_metric_review={"revision": 1, "events": []})
    c = build()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: committed)
    with patch("app.services.financial_workspace.lock_financial_profile", AsyncMock()), patch.object(cockpit, "load_cockpit", AsyncMock(return_value=c)):
        with pytest.raises(HTTPException) as caught:
            await review.correct(db, stale_profile, ACTOR, input_key="profit_and_loss.net_income", action="correct", value=0,
                                 reason="Checked", expected_revision=0, source_stamp=target(c)["source_stamp"], idempotency_key="request-1")
    assert caught.value.status_code == 409
    assert db.execute.call_args.args[0].get_execution_options()["populate_existing"] is True
