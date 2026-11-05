from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Response

from app.dealer_os import router as dealer_router
from app.dealer_os.models import DealerDebt
from app.dealer_os.schemas import DebtCreate, DebtPatch, DscrComponentAction
from app.dealer_os.services import extract
from app.dealer_os.services import financial_debt_drafts as drafts


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.scalar


def row(**patches):
    return {"id": str(uuid4()), "lender": "Forward Financing", "debt_type": "loan", "origin": "ai_draft",
            "status": "active", "monthly_payment": "900", "balance": "12000",
            "vendor_key": "FORWARD FINANCING", "count_in_dscr": True, "evidence_links": [], **patches}


def context(*rows):
    return drafts.ManagedDebtDraft(SimpleNamespace(id=uuid4()), {
        "family_id": str(uuid4()), "kind": "debt_schedule", "body": {"debts": list(rows)},
        "draft": {"revision": 4, "dirty": False},
    })


async def accept_draft(_db, _profile, **kwargs):
    return {"family_id": str(kwargs["family_id"]), "kind": "debt_schedule", "body": deepcopy(kwargs["body"]),
            "draft": {"revision": kwargs["expected_revision"] + 1, "dirty": True}}


@pytest.mark.asyncio
async def test_dealer_patch_stages_accepted_data_without_touching_published_debt():
    original = row(origin="client_form")
    managed = context(original)
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock(), commit=AsyncMock(), refresh=AsyncMock())
    dealer, actor = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    with patch.object(dealer_router, "require_team_or_rep"), patch.object(
        dealer_router, "resolve_dealer_scope", AsyncMock(return_value=dealer)
    ), patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock(side_effect=accept_draft)
    ), patch.object(dealer_router, "log_action", AsyncMock()):
        result = await dealer_router.patch_debt(dealer.id, UUID(original["id"]), DebtPatch(balance=10000), actor, db)
    assert result.balance == 10000 and result.origin == "admin" and result.draft_pending
    assert result.draft_revision == 5
    assert original["balance"] == "12000" and original["origin"] == "client_form"
    db.execute.assert_not_awaited()
    db.add.assert_not_called()
    db.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_dealer_create_and_delete_do_not_publish_and_delete_keeps_legacy_204():
    managed, dealer, actor = context(), SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), commit=AsyncMock())
    with patch.object(dealer_router, "require_team_or_rep"), patch.object(
        dealer_router, "resolve_dealer_scope", AsyncMock(return_value=dealer)
    ), patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock(side_effect=accept_draft)
    ), patch.object(dealer_router, "log_action", AsyncMock()):
        created = await dealer_router.create_debt(dealer.id, DebtCreate(lender="Test Lender", payment_amount=100, payment_frequency="weekly"), actor, db)
        response = Response(status_code=204)
        result = await dealer_router.delete_debt(dealer.id, created.id, actor, db, response)
    assert result is None and response.headers["X-Financial-Draft-Pending"] == "true"
    assert managed.snapshot["body"]["debts"][0]["status"] == "dismissed"
    assert created.draft_pending and created.monthly_payment > 100
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_dscr_toggle_stays_pending_and_reports_published_effective_amount():
    original = row()
    managed, dealer, actor = context(original), SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    published = SimpleNamespace(id=UUID(original["id"]), monthly_payment=900, status="active", count_in_dscr=True)
    db = SimpleNamespace(execute=AsyncMock(return_value=Result(scalar=published)), commit=AsyncMock())
    with patch.object(dealer_router, "require_team"), patch.object(
        dealer_router, "_load_visible_dealer", AsyncMock(return_value=dealer)
    ), patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock(side_effect=accept_draft)
    ), patch.object(dealer_router, "log_action", AsyncMock()), patch.object(dealer_router, "recompute_snapshot", AsyncMock()) as recompute:
        result = await dealer_router.dscr_component_action(dealer.id,
            DscrComponentAction(action="toggle", debt_id=published.id, count_in_dscr=False), actor, db)
    assert not result.count_in_dscr and result.draft_pending
    assert result.effective_monthly == 900
    assert published.count_in_dscr is True
    recompute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["dismissed", "duplicate"])
async def test_dscr_add_vendor_cannot_resurrect_a_reconciled_row(status):
    original = row(status=status)
    managed, dealer = context(original), SimpleNamespace(id=uuid4())
    with patch.object(dealer_router, "require_team"), patch.object(
        dealer_router, "_load_visible_dealer", AsyncMock(return_value=dealer)
    ), patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock()
    ) as save:
        with pytest.raises(HTTPException) as failure:
            await dealer_router.dscr_component_action(dealer.id,
                DscrComponentAction(action="add_vendor", vendor_key=original["vendor_key"]), SimpleNamespace(id=uuid4()), AsyncMock())
    assert failure.value.status_code == 409
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_extraction_stages_contract_and_never_updates_period_debt_service():
    original = row()
    managed, notes, document_id = context(original), [], uuid4()
    db = SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock())
    with patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock(side_effect=accept_draft)
    ):
        assert await extract._route_debt_schedule(db, uuid4(), [{"lender": original["lender"], "monthly_payment": 1200}], notes) == 0
        assert await extract._route_debt_rows(db, uuid4(), document_id,
            [{"lender": original["lender"], "monthly_payment": 1200}], notes) == 1
    assert original["monthly_payment"] == "900"
    assert managed.snapshot["body"]["debts"][0]["monthly_payment"] == 1200
    assert managed.snapshot["body"]["debts"][0]["document_id"] == str(document_id)
    assert any("awaits review" in note for note in notes)
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_draft_extraction_preserves_human_rows_tombstones_and_distinct_contracts():
    admin = row(lender="Desk Lender", origin="admin", vendor_key="DESK LENDER")
    retired = row(lender="Retired Lender", status="dismissed", vendor_key="RETIRED LENDER")
    first, second = row(lender="Shared Lender", vendor_key="SHARED LENDER"), row(lender="Shared Lender", vendor_key="SHARED LENDER")
    managed = context(admin, retired, first, second)
    with patch.object(drafts.financial_workspace, "save_draft", AsyncMock()) as save:
        result = await drafts.stage_proposals(AsyncMock(), managed,
            [{"lender": item["lender"], "monthly_payment": 1} for item in (admin, retired, first)], fields=("monthly_payment",))
    assert result["created"] == result["updated"] == 0
    assert result["skipped_admin"] == 3
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_legacy_extraction_still_updates_only_eligible_rows():
    published = DealerDebt(id=uuid4(), dealer_id=uuid4(), lender="Forward Financing", monthly_payment=900,
                           origin="ai_draft", status="active", vendor_key="FORWARD FINANCING")
    db = SimpleNamespace(execute=AsyncMock(return_value=Result([published])), flush=AsyncMock(), add=Mock())
    notes = []
    assert await extract._route_debt_rows(db, published.dealer_id, None,
        [{"lender": published.lender, "monthly_payment": 1200}], notes) == 1
    assert published.monthly_payment == 1200
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_managed_context_queries_only_the_requested_dealer_profile():
    dealer_id, owner = uuid4(), SimpleNamespace(id=uuid4())
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Result(scalar=owner)))
    with patch.object(drafts.financial_workspace_legacy, "managed_snapshot", AsyncMock(return_value={"family_id": str(uuid4())})) as load:
        result = await drafts.managed_context(db, dealer_id)
    query = db.execute.await_args.args[0]
    assert list(query.compile().params.values()) == [dealer_id]
    assert result.profile is owner
    load.assert_awaited_once_with(db, owner, "debt_schedule")


@pytest.mark.asyncio
async def test_source_removal_stages_retirement_and_keeps_published_source_ids():
    document_id = uuid4()
    source = row(document_id=str(document_id))
    desk = row(lender="Desk Lender", origin="admin", document_id=str(document_id))
    managed = context(source, desk)
    db = SimpleNamespace(execute=AsyncMock(), add=Mock())
    with patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "save_draft", AsyncMock(side_effect=accept_draft)
    ):
        assert await drafts.retire_document_rows(db, uuid4(), [document_id], actor=SimpleNamespace(id=uuid4()))
    staged = managed.snapshot["body"]["debts"]
    assert staged[0]["status"] == "dismissed" and staged[1]["status"] == "active"
    assert all(item["document_id"] == str(document_id) for item in staged)
    assert source["status"] == "active" and "source_document_removed" not in source.get("evidence", {})
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_removal_retains_version_originals_and_pdfs():
    source_id, pdf_id, unrelated_id = uuid4(), uuid4(), uuid4()
    version = SimpleNamespace(source_file_id=source_id, pdf_file_id=pdf_id)
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Result([version])))
    assert await drafts.version_file_ids(db, [source_id, pdf_id, unrelated_id]) == {source_id, pdf_id}


@pytest.mark.asyncio
async def test_managed_pl_import_never_writes_period_revenue_or_income():
    db, notes = SimpleNamespace(execute=AsyncMock(), add=Mock()), []
    with patch.object(drafts, "managed_context", AsyncMock(return_value=context())):
        assert await extract._route_pl_months(db, uuid4(), [{"month": "2026-10", "revenue": 30000, "net_income": 6000}], notes) == 0
    db.execute.assert_not_awaited()
    db.add.assert_not_called()
    assert "published period revenue and net income are unchanged" in notes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["p_and_l", "balance_sheet", "pfs"])
async def test_other_managed_financial_sources_register_reviewable_pdf_mappings(kind):
    managed = context()
    managed.profile.primary_bucket_id = uuid4()
    file = SimpleNamespace(id=uuid4(), bucket_id=managed.profile.primary_bucket_id, deleted_at=None,
                           content_type="application/pdf")
    doc = SimpleNamespace(dealer_id=uuid4(), bucket_file_id=file.id)
    analysis = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=file), execute=AsyncMock(return_value=Result(scalar=analysis)))
    notes = []
    with patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "propose_import", AsyncMock()
    ) as propose:
        assert await drafts.register_managed_form_source(db, doc, kind, notes)
    propose.assert_awaited_once_with(db, managed.profile, kind=kind, file_id=file.id, analysis=analysis, extract_full=False)
    assert "mapping was staged for review" in notes[0]


@pytest.mark.asyncio
async def test_managed_financial_mapping_rejects_source_from_another_bucket():
    managed = context()
    managed.profile.primary_bucket_id = uuid4()
    foreign = SimpleNamespace(id=uuid4(), bucket_id=uuid4(), content_type="application/pdf", deleted_at=None)
    doc = SimpleNamespace(dealer_id=uuid4(), bucket_file_id=foreign.id)
    db = SimpleNamespace(get=AsyncMock(return_value=foreign), execute=AsyncMock())
    with patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "propose_import", AsyncMock()
    ) as propose:
        assert await drafts.register_managed_form_source(db, doc, "pfs", [])
    propose.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_row_edit_cannot_target_a_different_profiles_row():
    managed = context(row())
    with patch.object(drafts.financial_workspace, "save_draft", AsyncMock()) as save:
        with pytest.raises(HTTPException) as error:
            await drafts.edit_row(AsyncMock(), managed, op="update", row_id=uuid4(), fields={"balance": 1})
    assert error.value.status_code == 404
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_generated_financial_pdf_is_not_imported_back_into_its_own_draft():
    managed = context()
    managed.profile.primary_bucket_id = uuid4()
    file = SimpleNamespace(id=uuid4(), bucket_id=managed.profile.primary_bucket_id, deleted_at=None,
                           content_type="application/pdf", content_hash="current-hash")
    db = SimpleNamespace(get=AsyncMock(return_value=file), execute=AsyncMock(return_value=Result(
        scalar=SimpleNamespace(provider="drafted_form", analysis={"financial_version": {"version_number": 2}})
    )))
    with patch.object(drafts, "managed_context", AsyncMock(return_value=managed)), patch.object(
        drafts.financial_workspace, "propose_import", AsyncMock()
    ) as propose:
        assert await drafts.register_managed_form_source(db, SimpleNamespace(dealer_id=uuid4(), bucket_file_id=file.id), "pfs", [])
    assert "current-hash" in db.execute.await_args.args[0].compile().params.values()
    propose.assert_not_awaited()
