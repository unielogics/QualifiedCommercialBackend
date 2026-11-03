"""Listing a file from the file page: the draft carries program readiness and
the cockpit's figures, an AI lead resolves its profile first, and a file that
is already listed is pointed at its listing instead of a duplicate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.capital_partners.services import prefill
from app.services import application_programs as programs
from app.services import financial_cockpit


def test_the_summary_sentence_states_months_and_average_deposits_or_nothing() -> None:
    assert prefill.file_summary_sentence({"bank_months": 6, "avg_monthly_deposits": 57536.4}) == "6 months of bank statements reviewed; average monthly deposits $57,536."
    assert prefill.file_summary_sentence({"bank_months": 0, "avg_monthly_deposits": None}) is None
    assert prefill.file_summary_sentence({}) is None


@pytest.mark.asyncio
async def test_the_readiness_snapshot_carries_programs_coverage_and_the_cockpit_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SimpleNamespace(id=uuid.uuid4())
    selection = SimpleNamespace(program_key="mca_refinance", program_name="MCA Refinance", source="ai_auto")
    candidate = SimpleNamespace(
        program_key="mca_refinance",
        eligible=True,
        recommendation_status=SimpleNamespace(value="recommended"),
        rule_state="pass",
        requires_review=True,
        reasons=["Revenue estimated from deposits", "Years in business recorded by the desk", "r3", "r4", "r5"],
    )

    async def selections(_db, _profile_id):
        return [selection]

    async def candidates(_db, _profile):
        return [candidate]

    async def cockpit(_db, _profile):
        return {
            "generated_at": "2026-11-03T20:00:00+00:00",
            "coverage": {"bank_months": 6, "bank_months_accepted": 6, "bank_months_required": 6, "tax_years": [], "debt_rows": 0, "has_debt_schedule_document": True, "files_analyzed": 8, "files_accepted": 7},
            "bank": {"summary": {"avg_deposits": 50000.0, "annualized_deposits": 600000.0, "avg_net": 5000.0, "average_balance": 15500.0, "nsf_total": 0, "deposit_trend_pct": 4.2}},
            "dscr": {"headline": {"key": "bank_cash_flow", "value": 1.47, "status": "healthy"}, "pro_forma_headline": None, "annual_debt_service": 127600.0},
            "net_profit": {"conclusion": {"key": "bank_cash_flow", "value": 60000.0, "period": "Apr 2026–Sep 2026"}},
            "tax": {"years": []},
        }

    class _Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(stage=1, status="draft", version=2)

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    monkeypatch.setattr(programs, "active_selections", selections)
    monkeypatch.setattr(programs, "published_candidates", candidates)
    monkeypatch.setattr(financial_cockpit, "load_cockpit", cockpit)

    snapshot = await prefill.readiness_snapshot(_Db(), profile)
    readiness = snapshot["readiness"]
    assert readiness["programs"] == [
        {
            "key": "mca_refinance",
            "name": "MCA Refinance",
            "source": "ai_auto",
            "eligible": True,
            "recommendation_status": "recommended",
            "rule_state": "pass",
            "requires_review": True,
            "reasons": ["Revenue estimated from deposits", "Years in business recorded by the desk", "r3", "r4"],
        }
    ]
    assert readiness["coverage_complete"] is True and readiness["has_debt_schedule"] is True
    assert (readiness["files_analyzed"], readiness["files_accepted"]) == (8, 7)
    financials = snapshot["financials"]
    assert (financials["bank_months"], financials["avg_monthly_deposits"], financials["dscr"], financials["dscr_basis"]) == (6, 50000.0, 1.47, "bank_cash_flow")
    assert (financials["net_profit"], financials["net_profit_basis"]) == (60000.0, "bank_cash_flow")
    assert snapshot["production_package"] == {"stage": 1, "status": "draft", "version": 2}


@pytest.mark.asyncio
async def test_an_unknown_intake_reads_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import application_profiles as profiles

    async def failing(_db, _kind, _id, _user):
        raise RuntimeError("no such intake")

    monkeypatch.setattr(profiles, "resolve_profile", failing)
    with pytest.raises(LookupError):
        await prefill.resolve_intake_profile(SimpleNamespace(), SimpleNamespace(), uuid.uuid4())


def test_products_apply_as_plain_json_with_one_primary() -> None:
    from decimal import Decimal

    from app.capital_partners import schemas as S
    from app.capital_partners.models import CpListing
    from app.capital_partners.router_admin import _apply

    listing = CpListing()
    _apply(
        listing,
        S.ListingIn(
            products=[
                S.ListingProductIn(program_key="mca_refinance", label="MCA Refinance", amount=Decimal("70000"), rate_pct=Decimal("16"), term_months=120, monthly_payment=Decimal("1172.26"), dscr=Decimal("1.31")),
                S.ListingProductIn(program_key="rbf", label="Revenue-based", amount=Decimal("50000"), factor_rate=Decimal("1.3"), term_months=12),
            ]
        ),
    )
    assert listing.products[0]["primary"] is True and listing.products[1]["primary"] is False
    assert listing.products[0]["amount"] == 70000.0 and listing.products[0]["rate_pct"] == 16.0
    assert listing.products[1]["factor_rate"] == 1.3 and listing.products[1]["payment_frequency"] == "monthly"
    assert all(not isinstance(v, Decimal) for p in listing.products for v in p.values())


@pytest.mark.asyncio
async def test_suggested_products_mark_the_first_as_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import offer_suggestions

    async def fake(_db, _profile, *, program_keys=None):
        return {"products": [{"program_key": "a", "label": "A", "amount": 70000.0, "rate_pct": 16.0, "term_months": 120, "monthly_payment": 1172.26, "dscr": 1.31, "reading": "A: fine."}, {"program_key": "b", "label": "B", "amount": None, "reason": "no room"}]}

    monkeypatch.setattr(offer_suggestions, "suggest_offers", fake)
    products = await prefill.suggested_products(SimpleNamespace(), SimpleNamespace(id=uuid.uuid4()))
    assert [p["primary"] for p in products] == [True, False]
    assert products[0]["notes"] == "A: fine." and products[1]["amount"] is None
