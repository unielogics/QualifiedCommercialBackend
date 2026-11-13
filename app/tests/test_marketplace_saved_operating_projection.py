"""Saved future figures, accounting separation and publication privacy."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.capital_partners.services import listings, sync
from app.capital_partners.services import operating_projection as projection
from app.models.application_profile import ApplicationProfile
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormVersion,
)
from app.services import financial_workspace
from app.tests import test_capital_partners as capital_tests
from app.tests.test_marketplace_ppm_lifecycle import ppm_harness
from app.tests.test_refinance_reinvestment_independent import accounting_change, saved_loan


def version(**patch):
    return {"id": str(uuid4()), "version_number": 3, "state": "client_submitted", "saved_at": "2026-11-13T12:00:00Z", **patch}


def context():
    result = saved_loan()
    result["plan"]["evaluation"].update(debt_service_after_annual=78000, coverage_unavailable_reason=None)
    return result


def test_saved_conditional_plan_separates_noi_from_refinance_cash_and_coverage():
    raw, cockpit = accounting_change(), context()
    original = deepcopy((raw, cockpit))
    result = projection.project(raw, cockpit, version())
    assert (raw, cockpit) == original
    assert result["qualification"] == "proposed"
    assert result["historical_figures_unchanged"] is True
    assert result["current"]["noi_annual"] == 18000
    assert result["proposed_operating"]["noi_annual"] == 24000
    assert result["refinance_only"]["noi_delta_annual"] == 0
    assert result["proposed_operating"]["earnings_annual"] == 30000
    assert result["current"]["cash_after_debt_annual"] == -90000
    assert result["refinance_only"]["cash_after_debt_annual"] == -48000
    assert result["proposed_operating"]["cash_delta_monthly"] == 3500
    assert result["proposed_operating"]["dscr_with_refinance"] == .3077
    assert all(value is None for value in result["reviewed_operating"].values())


@pytest.mark.parametrize("mutation", ["currency", "source", "unknown"])
def test_unconfirmed_or_changed_sources_do_not_become_known_future_noi(mutation):
    raw, cockpit = accounting_change(), context()
    if mutation == "currency":
        raw["currency"] = "CAD"
    elif mutation == "source":
        raw["expenses"][0]["current_amount"] = 14000
    else:
        raw["adjustments"][0]["command"]["value"] = None
    result = projection.project(raw, cockpit, version())
    assert result["proposed_operating"]["noi_annual"] is None
    assert result["current"]["noi_annual"] == 18000
    assert result["current"]["noi_currency"] == "USD"


def test_public_adapter_is_field_allowlisted_including_nested_points_and_messages():
    value = projection.project(accounting_change(), context(), version())
    value["private_actor"] = value["missing"] = value["notes"] = ["private-payee.pdf secret-source-id"]
    value["current"]["review_reason"] = "private-review"
    value["saved_version"]["staff_actor_id"] = "private-staff"
    value["saved_terms"]["recipient"] = "private-fee"
    public = projection.public(value)
    assert "private" not in str(public)
    assert public["current"]["noi_annual"] == 18000


@pytest.mark.asyncio
async def test_saved_pointer_never_selects_unsaved_draft_or_forges_client_approval(monkeypatch):
    saved_id, draft_id, family_id = uuid4(), uuid4(), uuid4()
    record = SimpleNamespace(id=saved_id, body=accounting_change(), version_number=3,
        status="client_submitted", created_at=datetime.fromisoformat("2026-11-13T12:00:00+00:00"))
    family = SimpleNamespace(id=family_id, current_version_id=saved_id)
    monkeypatch.setattr(financial_workspace, "existing_family", AsyncMock(return_value=family))
    db = SimpleNamespace(get=AsyncMock(return_value=record))
    cockpit = context()
    cockpit["operating_plan"] = {"available": True, "draft_id": str(draft_id)}
    result = await projection.saved(db, SimpleNamespace(id=uuid4()), cockpit)
    db.get.assert_awaited_once_with(FinancialFormVersion, saved_id)
    assert result["saved_version"]["id"] == str(saved_id)
    assert result["qualification"] == "proposed"


def test_public_active_figures_and_generated_documents_do_not_follow_pending_candidate():
    public_doc, candidate_doc = uuid4(), uuid4()
    listing = SimpleNamespace(published_at=True, source_snapshot={
        "financials": {"net_profit": 999999, "operating_projection": {"qualification": "proposed"}},
        "active_financials": {"net_profit": -10000}, "active_listing": {"note_rate_pct": "12"},
        "active_generated_document_ids": [str(public_doc)]}, note_rate_pct=25,
        documents=[SimpleNamespace(id=ident, status="ready", kind="investor_presentation") for ident in (public_doc, candidate_doc)])
    public = listings.partner_financials(listing)
    assert public["net_profit"] == -10000
    assert public["financial_update_pending"] is True
    assert public["pending_operating_projection"]["qualification"] == "proposed"
    assert listings.member_view(listing).note_rate_pct == "12"
    assert [row.id for row in listings.ready_documents(listing)] == [public_doc]


def test_refresh_metadata_and_no_saved_plan_do_not_change_material_sections():
    # An absent optional feature keeps the old exact section shape.
    cockpit = context()
    from app.services.financial_cockpit import build_cockpit
    from app.tests.test_financial_metric_review import pnl

    cockpit = build_cockpit([pnl()], [], requested_amount=100000)
    before = sync.file_sections(cockpit, program_keys=[], package=None)
    assert "operating_projection" not in before
    cockpit["refresh_job"] = {"status": "running", "started_at": "2099-01-01"}
    assert sync.fingerprints(before) == sync.fingerprints(sync.file_sections(cockpit, program_keys=[], package=None))
    changed = sync.file_sections(cockpit, program_keys=[], package=None, operating_projection={"saved_version": {"id": "one"}})
    assert sync.fingerprints(changed)["operating_projection"] != sync.saved_source_fingerprint(None)


@pytest.mark.asyncio
async def test_real_saved_version_pointer_ignores_unsaved_edits_and_changes_only_after_new_version(monkeypatch):
    monkeypatch.setattr(capital_tests, "MODELS", [*capital_tests.MODELS, ApplicationProfile,
        FinancialFormFamily, FinancialFormDraft, FinancialFormVersion])
    async with ppm_harness() as h:
        profile = ApplicationProfile(vertical="main_street", funding_category="general", industry="Synthetic")
        h.db.add(profile)
        await h.db.flush()
        family = FinancialFormFamily(profile_id=profile.id, kind="operating_plan", identity_key="operating", label="Synthetic plan")
        h.db.add(family)
        await h.db.flush()
        first = FinancialFormVersion(family_id=family.id, version_number=1, schema_version="test", body=accounting_change(),
            body_sha256="a" * 64, source="form", status="published", created_at=datetime(2026, 11, 13, 12, tzinfo=UTC))
        h.db.add(first)
        await h.db.flush()
        family.current_version_id = first.id
        draft = FinancialFormDraft(family_id=family.id, revision=100, body=accounting_change(3000))
        h.db.add(draft)
        await h.db.commit()
        cockpit = context()
        cockpit["operating_plan"] = {"available": True}
        saved = await projection.saved(h.db, profile, cockpit)
        assert saved["proposed_operating"]["noi_annual"] == 24000
        draft.body = accounting_change(1000)
        draft.revision += 1
        await h.db.commit()
        assert await projection.saved(h.db, profile, cockpit) == saved
        second = FinancialFormVersion(family_id=family.id, version_number=2, schema_version="test", body=accounting_change(3000),
            body_sha256="b" * 64, source="form", status="client_submitted")
        h.db.add(second)
        await h.db.flush()
        family.current_version_id = second.id
        await h.db.commit()
        updated = await projection.saved(h.db, profile, cockpit)
        assert updated["saved_version"]["id"] == str(second.id)
        assert updated["proposed_operating"]["noi_annual"] == 21000
        assert updated["qualification"] == "proposed"


def test_read_timestamps_and_numeric_storage_do_not_create_a_pending_publication():
    active = {"net_profit": 12000, "generated_at": "2026-11-13T12:00:00Z"}
    listing = SimpleNamespace(published_at=True, source_snapshot={"active_financials": active,
        "financials": {**active, "generated_at": "2026-11-13T13:00:00Z"}})
    assert sync.publication_changed(listing) is False
    listing.source_snapshot["financials"]["net_profit"] += .01
    assert sync.publication_changed(listing) is True
