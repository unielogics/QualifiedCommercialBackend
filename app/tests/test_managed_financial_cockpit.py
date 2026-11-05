from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.routers import application_profiles as routes
from app.schemas.debt_schedule_review import DebtReviewLineWrite
from app.schemas.financial_cockpit import DebtRowPatch
from app.services import debt_schedule_review as review
from app.services import financial_cockpit as cockpit
from app.services import financial_workspace as workspace
from app.services import mca_advances
from app.tests.test_accepted_debt_precedence import obligation
from app.tests.test_financial_cockpit import (
    ONDECK,
    _file,
    _recurring_month,
    _statement_with_recurring,
)


def snapshot(*rows):
    return {"family_id": str(uuid4()), "kind": "debt_schedule", "body": {"debts": deepcopy(list(rows))},
            "draft": {"revision": 4, "dirty": False, "base_version_id": str(uuid4())}}


async def save_draft(_db, _profile, **kwargs):
    return {"family_id": str(kwargs["family_id"]), "kind": "debt_schedule", "body": deepcopy(kwargs["body"]),
            "draft": {"revision": kwargs["expected_revision"] + 1, "dirty": True, "base_version_id": str(uuid4())}}


def accepted(body):
    return {"body": body, "version_id": str(uuid4()), "actor_name": "Desk", "saved_at": "2026-11-05T12:00:00+00:00"}


def observed_files():
    names = [f"Funding Company {letter}" for letter in "ABCDEFGHIJKLMNOPQRST"]
    payees = [{**ONDECK, "payee": name} for name in names]
    return [_statement_with_recurring("bank", [_recurring_month(f"2026-{month:02d}-01 to 2026-{month:02d}-28", payees)
        for month in (5, 6, 7)])]


@pytest.mark.parametrize("no_debt", [False, True])
def test_actual_financial_cockpit_and_plan_take_only_the_saved_schedule_even_with_many_observed_lenders(no_debt):
    rows = [] if no_debt else [obligation(monthly_payment=str(value), vendor_key="ONE SHARED PAYEE") for value in (100, 200, 300, 400, 500)]
    original = {"debts": rows, "no_debt": no_debt}
    files = observed_files() + [_file("old schedule", "debt_schedule", {"debts": [{"lender": "Old extra", "monthly_payment": 99999, "current_balance": 500000}]})]
    stale_projection = [{"id": str(uuid4()), "lender": "Outside projected loan", "monthly_payment": 88888, "category": "mca"}]
    result = cockpit.build_cockpit(files, stale_projection, accepted_schedule=accepted(original))
    service = result["debt"]["service"]
    assert service["source"] == "accepted_debt_schedule" and service["schedule_authoritative"]
    assert service["monthly"] == (0 if no_debt else 1500)
    assert service["annual"] == (0 if no_debt else 18000)
    assert len(result["debt"]["rows"]) == len(result["debt"]["obligations"]) == len(rows)
    assert all(row["in_debt_service"] for row in result["debt"]["obligations"])
    assert not service["declaration_conflict"]
    assert service["observed_monthly"] > 1500
    assert result["dscr"]["annual_debt_service"] == service["annual"]
    if no_debt:
        assert result["debt"]["schedule"]["status"] == "voided"
        assert result["dscr"]["headline"] is None
        assert result["dscr"]["no_debt"] and "completed saved schedule" in result["dscr"]["reason"]


@pytest.mark.asyncio
async def test_legacy_review_updates_shared_draft_without_mutating_published_rows_or_refreshing_pdf():
    original = obligation(origin="client_form")
    draft = snapshot(original)
    db = SimpleNamespace(add=Mock(), delete=AsyncMock(), execute=AsyncMock(), flush=AsyncMock())
    line = DebtReviewLineWrite(key="row:" + original["id"], row_id=UUID(original["id"]), lender=original["lender"],
        monthly_payment=250, balance=8000, notes="Reviewed by the desk")
    with patch.object(review, "workspace_snapshot", AsyncMock(return_value=draft)), patch.object(
        review, "schedule_documents", AsyncMock(return_value=[])
    ), patch.object(workspace, "save_draft", AsyncMock(side_effect=save_draft)):
        saved = await review.apply_review(db, SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()), [line])
    assert saved["kept"] == saved["updated"] == 1
    row = draft["body"]["debts"][0]
    assert row["id"] == original["id"] and row["monthly_payment"] == 250 and row["origin"] == "admin"
    assert original["monthly_payment"] == "100" and original["origin"] == "client_form"
    assert draft["draft"]["revision"] == 5
    db.add.assert_not_called()
    db.delete.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_review_deletion_preserves_stable_identity_and_cascades_duplicate_aliases_in_draft():
    original = obligation()
    alias = obligation(status="duplicate", duplicate_of_id=original["id"])
    draft = snapshot(original, alias)
    line = DebtReviewLineWrite(key="row:" + original["id"], row_id=UUID(original["id"]), lender=original["lender"], keep=False, remove_reason="paid_off")
    with patch.object(review, "schedule_documents", AsyncMock(return_value=[])), patch.object(
        workspace, "save_draft", AsyncMock(side_effect=save_draft)
    ):
        await review.apply_review(SimpleNamespace(), SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()), [line], workspace_draft=draft)
    assert {row["id"] for row in draft["body"]["debts"]} == {original["id"], alias["id"]}
    assert all(row["status"] == "dismissed" for row in draft["body"]["debts"])
    assert alias["status"] == "duplicate" and original["status"] == "active"


@pytest.mark.asyncio
async def test_legacy_review_refuses_ambiguous_same_lender_without_explicit_row_identity():
    draft = snapshot(obligation(), obligation())
    line = DebtReviewLineWrite(key="read-line", lender="Shared Lender", monthly_payment=500)
    with patch.object(review, "schedule_documents", AsyncMock(return_value=[])), patch.object(workspace, "save_draft", AsyncMock()) as save:
        with pytest.raises(HTTPException) as error:
            await review.apply_review(SimpleNamespace(), SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4()), [line], workspace_draft=draft)
    assert error.value.status_code == 409
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_file_action_publishes_numbered_version_not_legacy_pdf_overwrite():
    draft = snapshot(obligation())
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    db = SimpleNamespace()
    with patch.object(review, "workspace_snapshot", AsyncMock(return_value=draft)), patch.object(
        workspace, "publish_version", AsyncMock()
    ) as publish, patch.object(routes.drafted_forms, "refresh_form_pdf", AsyncMock()) as overwrite:
        filed, problem = await routes._file_schedule_from_rows(db, profile, SimpleNamespace(id=uuid4()))
    assert filed and problem is None
    assert publish.await_args.kwargs["family_id"] == UUID(draft["family_id"])
    assert publish.await_args.kwargs["expected_revision"] == 4 and publish.await_args.kwargs["submit"]
    overwrite.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_cannot_publish_incomplete_draft_or_infer_empty_no_debt():
    draft = snapshot(obligation(monthly_payment=""))
    with patch.object(review, "workspace_snapshot", AsyncMock(return_value=draft)), patch.object(workspace, "publish_version", AsyncMock()) as publish:
        filed, reason = await routes._file_schedule_from_rows(SimpleNamespace(), SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4()), None)
    assert not filed and "monthly" in reason.lower()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_verbal_advances_stage_only_their_owned_subset_and_keep_other_loans_and_tombstones():
    original = obligation(origin="admin", debt_type="mca", payment_amount="100", payment_frequency="daily", monthly_payment="2100")
    other = obligation(lender="Unrelated term loan", debt_type="loan", origin="client_form")
    retired = obligation(lender="Dismissed advance", debt_type="mca", origin="admin", status="dismissed")
    draft = snapshot(original, other, retired)
    db = SimpleNamespace(add=Mock(), delete=AsyncMock(), execute=AsyncMock(), flush=AsyncMock())
    with patch.object(review, "workspace_snapshot", AsyncMock(return_value=draft)), patch.object(workspace, "save_draft", AsyncMock(side_effect=save_draft)) as save:
        result = await mca_advances.save_advances(db, SimpleNamespace(id=uuid4()), [{"id": original["id"], "lender": original["lender"],
            "payment_amount": 200, "payment_frequency": "daily", "balance": 8000}], actor=SimpleNamespace(id=uuid4()))
    body = save.await_args.kwargs["body"]
    assert len(body["debts"]) == 3 and body["debts"][1:] == [other, retired]
    assert result[0].monthly_payment == 4200 and str(result[0].id) == original["id"] and result[0].draft_pending
    assert original["monthly_payment"] == "2100"
    db.add.assert_not_called()
    db.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_verbal_advance_removed_from_payload_is_dismissed_in_draft_not_deleted():
    original = obligation(origin="admin", debt_type="mca")
    draft = snapshot(original)
    with patch.object(review, "workspace_snapshot", AsyncMock(return_value=draft)), patch.object(workspace, "save_draft", AsyncMock(side_effect=save_draft)) as save:
        result = await mca_advances.save_advances(SimpleNamespace(), SimpleNamespace(id=uuid4()), [])
    assert result == []
    assert save.await_args.kwargs["body"]["debts"][0]["id"] == original["id"]
    assert save.await_args.kwargs["body"]["debts"][0]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_old_cockpit_row_edit_stages_revision_and_returns_saved_headline_until_explicit_publish():
    row = obligation()
    draft = snapshot(row)
    profile = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    result = save_draft
    async def stage(_db, _profile, _snapshot, body, **_kwargs):
        return await result(_db, _profile, family_id=UUID(draft["family_id"]), expected_revision=4, body=body)
    financial = cockpit.build_cockpit([], [], accepted_schedule=accepted({"debts": [row]}))
    financial["profile_id"] = str(profile.id)
    with patch.object(routes, "_require_underwriting_actor"), patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), patch.object(
        review, "workspace_snapshot", AsyncMock(return_value=draft)
    ), patch.object(routes, "stage_legacy_body", AsyncMock(side_effect=stage)) as save, patch.object(cockpit, "load_cockpit", AsyncMock(return_value=financial)):
        response = await routes.patch_debt_schedule_row(profile.id, UUID(row["id"]), DebtRowPatch(monthly_payment=250), SimpleNamespace(id=uuid4()), db)
    assert save.await_args.args[3]["debts"][0]["monthly_payment"] == 250
    assert response.debt["service"]["monthly"] == 100 and response.debt["draft_pending"]
    db.execute.assert_not_awaited()
