from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.models.financial_form_version import FinancialFormDraft
from app.routers import worksheets as routes
from app.services import client_financial_forms, financial_workspace, sheets, worksheet_links


def _snapshot(kind="debt_schedule", *, family_id=None, body=None, revision=3):
    return {"kind": kind, "family_id": str(family_id or uuid4()),
        "body": body if body is not None else financial_workspace.empty_body(kind),
        "draft": {"revision": revision, "base_version_id": None, "dirty": True},
        "current_version_id": None, "versions": []}


def _debt(**changes):
    return {"id": str(uuid4()), "lender": "Example lender", "debt_type": "term_loan",
        "original_amount": "100", "balance": "75", "monthly_payment": "10", "rate": "12",
        "origin": "ai_draft", "status": "active", "count_in_dscr": True,
        "evidence_links": [{"file_id": str(uuid4()), "role": "agreement"}], **changes}


def _access(*, can_edit=True, scope=("debt_schedule",)):
    return worksheet_links.WorksheetAccess(
        link=SimpleNamespace(id=uuid4(), invitee_email="client@example.test", completed_at=None),
        profile=SimpleNamespace(id=uuid4()), worksheet=SimpleNamespace(id=uuid4(), revision=7),
        sheets=scope, can_edit=can_edit)


def _request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.4"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sheets.KINDS)
async def test_scoped_worksheet_reads_use_managed_drafts_for_every_form(kind):
    snapshot = _snapshot(kind)
    family_id = uuid4()
    db, profile, worksheet = SimpleNamespace(info={}), SimpleNamespace(id=uuid4()), SimpleNamespace(revision=9)
    draft = SimpleNamespace(family_id=uuid4())
    ensure = AsyncMock(return_value=snapshot)
    with patch.object(client_financial_forms, "ensure_snapshot", ensure), patch.object(
        financial_workspace, "resolve_family", AsyncMock(return_value=(None, draft))):
        loaded = await sheets._load(db, profile, kind, origin="client_form", prefill={}, worksheet=worksheet, family_id=family_id)
    assert loaded.workspace == snapshot and loaded.draft is draft and loaded.rev == 9
    assert ensure.await_args.kwargs["family_id"] == family_id


@pytest.mark.asyncio
async def test_imported_cells_are_editable_without_changing_source_or_published_figures():
    active, excluded = _debt(), _debt(status="dismissed")
    snapshot = _snapshot(body={"debts": [active, excluded]})
    draft = SimpleNamespace(family_id=uuid4(), body=snapshot["body"])
    profile, db = SimpleNamespace(id=uuid4()), SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))
    loaded = sheets._Loaded(client_financial_forms.client_body("debt_schedule", snapshot["body"]), None, status="draft", completed=False, rev=0)
    loaded.draft, loaded.workspace = draft, snapshot
    assert loaded.body["debts"][0]["editable"] is True
    assert len(loaded.body["debts"]) == 1
    incoming = {"debts": [{**active, "balance": "55", "monthly_payment": "8", "origin": "admin",
        "status": "duplicate", "count_in_dscr": False, "evidence_links": []}, {"id": str(uuid4()), "lender": ""}]}
    stage = AsyncMock(return_value=draft)
    publish = AsyncMock()
    with patch.object(financial_workspace, "stage_draft", stage), patch.object(financial_workspace, "load_workspace", AsyncMock(return_value=snapshot)), patch.object(financial_workspace, "publish_version", publish):
        await sheets._save_kind(db, profile, "debt_schedule", loaded, incoming, origin="client_form", actor_user_id=None)
    merged = stage.await_args.args[4]
    changed = merged["debts"][0]
    assert changed["id"] == active["id"] and changed["balance"] == "55" and changed["monthly_payment"] == "8"
    assert changed["origin"] == "ai_draft" and changed["status"] == "active" and changed["count_in_dscr"] is True
    assert changed["evidence_links"] == active["evidence_links"]
    assert changed["client_reviewed"] is True
    assert len(merged["debts"]) == 2 and merged["debts"][1] == excluded
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_imported_row_survives_clearing_and_retyping_its_lender():
    active, excluded = _debt(), _debt(status="dismissed")
    snapshot = _snapshot(body={"debts": [active, excluded]})
    draft = FinancialFormDraft(family_id=UUID(snapshot["family_id"]), body=snapshot["body"], revision=3)
    loaded = sheets._Loaded(client_financial_forms.client_body("debt_schedule", snapshot["body"]), None, status="draft", completed=False, rev=0)
    loaded.draft, loaded.workspace = draft, snapshot
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))

    async def stage(_db, _profile, _family, _draft, body, **_kwargs):
        draft.body = body
        return draft

    with patch.object(financial_workspace, "stage_draft", stage), patch.object(financial_workspace, "load_workspace", AsyncMock(return_value=snapshot)):
        await sheets._save_kind(db, SimpleNamespace(), "debt_schedule", loaded,
            {"debts": [{**active, "lender": ""}]}, origin="client_form", actor_user_id=None)
        cleared = draft.body["debts"][0]
        assert cleared["id"] == active["id"] and cleared["lender"] == "" and cleared["status"] == "active"
        assert cleared["balance"] == "75" and cleared["monthly_payment"] == "10"
        assert draft.body["debts"][1] == excluded
        await sheets._save_kind(db, SimpleNamespace(), "debt_schedule", loaded,
            {"debts": [{**cleared, "lender": "Corrected lender"}]}, origin="client_form", actor_user_id=None)
        assert len(draft.body["debts"]) == 2
        assert draft.body["debts"][0]["id"] == active["id"]
        assert draft.body["debts"][0]["lender"] == "Corrected lender" and draft.body["debts"][0]["status"] == "active"
        assert draft.body["debts"][1]["status"] == "dismissed"
        # Only an explicit removal dismisses the existing active obligation.
        await sheets._save_kind(db, SimpleNamespace(), "debt_schedule", loaded,
            {"debts": []}, origin="client_form", actor_user_id=None)
        assert draft.body["debts"][0]["status"] == "dismissed"


@pytest.mark.asyncio
async def test_new_balance_first_row_survives_without_creating_blank_padding_obligations():
    snapshot = _snapshot(body={"debts": []})
    draft = FinancialFormDraft(family_id=UUID(snapshot["family_id"]), body=snapshot["body"], revision=3)
    loaded = sheets._Loaded(snapshot["body"], None, status="draft", completed=False, rev=0)
    loaded.draft, loaded.workspace = draft, snapshot
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))
    row_id = str(uuid4())

    async def stage(_db, _profile, _family, _draft, body, **_kwargs):
        draft.body = body
        return draft

    with patch.object(financial_workspace, "stage_draft", stage), patch.object(financial_workspace, "load_workspace", AsyncMock(return_value=snapshot)):
        await sheets._save_kind(db, SimpleNamespace(), "debt_schedule", loaded,
            {"debts": [{"id": row_id, "balance": "50"}, {"id": str(uuid4()), "lender": " "}]}, origin="client_form", actor_user_id=None)
        assert len(draft.body["debts"]) == 1 and draft.body["debts"][0]["id"] == row_id
        assert draft.body["debts"][0]["balance"] == "50" and draft.body["debts"][0]["status"] == "active"
        await sheets._save_kind(db, SimpleNamespace(), "debt_schedule", loaded,
            {"debts": [{**draft.body["debts"][0], "lender": "New lender"}]}, origin="client_form", actor_user_id=None)
        assert len(draft.body["debts"]) == 1
        assert draft.body["debts"][0]["id"] == row_id and draft.body["debts"][0]["lender"] == "New lender"


@pytest.mark.asyncio
async def test_link_sheet_binding_does_not_follow_a_newer_owner_of_same_kind():
    first, newer = uuid4(), uuid4()
    row = SimpleNamespace(sheet_kind="pfs", financial_family_id=None)
    access = _access(scope=("pfs",))
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))), flush=AsyncMock())
    latest = [first]

    async def ensure(_db, _profile, kind, *, family_id=None, statement_id=None):
        return _snapshot(kind, family_id=family_id or latest[0])

    with patch.object(financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(client_financial_forms, "ensure_snapshot", ensure):
        assert await worksheet_links.bound_families(db, access) == {"pfs": first}
        latest[0] = newer
        assert await worksheet_links.bound_families(db, access) == {"pfs": first}
    assert row.financial_family_id == first


@pytest.mark.asyncio
async def test_packet_grid_adopts_the_same_owner_family_as_its_form_child():
    selected, unrelated_newer = uuid4(), uuid4()
    row = SimpleNamespace(sheet_kind="pfs", financial_family_id=None)
    child = SimpleNamespace(financial_family_id=selected, statement_id=uuid4())
    access = _access(scope=("pfs",))
    access.link.packet_id = uuid4()
    db = SimpleNamespace(info={}, execute=AsyncMock(side_effect=[
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row])),
        SimpleNamespace(scalar_one_or_none=lambda: child),
    ]), flush=AsyncMock())
    ensure = AsyncMock(return_value=_snapshot("pfs", family_id=selected))
    with patch.object(financial_workspace, "lock_financial_profile", AsyncMock()), patch.object(client_financial_forms, "ensure_snapshot", ensure):
        result = await worksheet_links.bound_families(db, access)
    assert result == {"pfs": selected} and row.financial_family_id != unrelated_newer
    assert ensure.await_args.kwargs["family_id"] == selected


@pytest.mark.asyncio
@pytest.mark.parametrize("same_family", [True, False])
async def test_packet_completion_only_marks_the_published_family(same_family):
    family_id = uuid4()
    child = SimpleNamespace(financial_family_id=family_id if same_family else uuid4(), completed_at=None)
    access = _access()
    access.link.packet_id = uuid4()
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: child)))
    await worksheet_links.complete_bound_sheet(db, access, "debt_schedule", family_id)
    assert (child.completed_at is not None) is same_family


@pytest.mark.asyncio
async def test_cell_flush_returns_the_latest_financial_draft_revision_without_publishing():
    active = _debt()
    snapshot = _snapshot(body={"debts": [active]})
    draft = FinancialFormDraft(family_id=UUID(snapshot["family_id"]), body=snapshot["body"], revision=3)
    loaded = sheets._Loaded(snapshot["body"], None, status="draft", completed=False, rev=1)
    loaded.draft, loaded.workspace = draft, snapshot
    worksheet = SimpleNamespace(id=uuid4(), revision=1)
    db = SimpleNamespace(info={}, get=AsyncMock(return_value=SimpleNamespace()), flush=AsyncMock())
    order = []

    async def lock_profile(_db, _profile):
        order.append("profile")

    async def lock_sheet(_db, _worksheet):
        order.append("worksheet")
        return worksheet

    async def stage(_db, _profile, _family, _draft, body, **_kwargs):
        draft.body = body
        return draft

    refreshed = {**snapshot, "draft": {**snapshot["draft"], "revision": 4}}
    with (
        patch.object(financial_workspace, "lock_financial_profile", lock_profile),
        patch.object(sheets, "_lock", lock_sheet),
        patch.object(sheets, "_load", AsyncMock(return_value=loaded)),
        patch.object(sheets.financial_statements, "form_prefill", AsyncMock(return_value={})),
        patch.object(financial_workspace, "stage_draft", stage),
        patch.object(financial_workspace, "load_workspace", AsyncMock(return_value=refreshed)),
        patch.object(financial_workspace, "publish_version", AsyncMock()) as publish,
        patch.object(sheets, "_announce", AsyncMock()) as announce,
    ):
        result = await sheets.apply_cell_edits(db, SimpleNamespace(id=uuid4()),
            [{"sheet": "debt_schedule", "key": f"{active['id']}.balance", "value": "50"}],
            origin="client_form", worksheet=worksheet, family_ids={"debt_schedule": draft.family_id})
    assert order == ["profile", "worksheet"]
    assert result["workspaces"]["debt_schedule"]["revision"] == 4
    assert draft.body["debts"][0]["balance"] == "50"
    event = announce.await_args.args[2][0]
    assert event["data"]["family_id"] == snapshot["family_id"] and event["data"]["value"] == "50"
    publish.assert_not_awaited()


def test_live_financial_values_from_another_owner_family_never_reach_the_client():
    selected = uuid4()
    event = {"type": "cell.changed", "data": {"sheet_kind": "pfs", "family_id": str(uuid4()), "value": "PRIVATE-NET-WORTH"}}
    assert routes._scoped_family_event(event, {"pfs": selected}) is None
    # Staff still sees its authorized profile-wide event stream.
    assert routes._scoped_family_event(event, None) == event


def test_untagged_live_values_trigger_a_bound_refresh_without_carrying_figures():
    event = {"type": "cell.changed", "data": {"worksheet_id": "sheet", "sheet_kind": "pfs", "value": "PRIVATE-NET-WORTH", "address": {"key": "cash"}}}
    safe = routes._scoped_family_event(event, {"pfs": uuid4()})
    assert safe["type"] == "sync.required"
    assert "PRIVATE-NET-WORTH" not in str(safe) and "address" not in safe["data"]
    assert safe["data"]["sheet_kind"] == "pfs"


def test_bound_live_financial_values_still_update_the_client_in_place():
    selected = uuid4()
    event = {"type": "cell.changed", "data": {"sheet_kind": "pfs", "family_id": str(selected), "value": "100"}}
    assert routes._scoped_family_event(event, {"pfs": selected}) == event


@pytest.mark.asyncio
@pytest.mark.parametrize("can_edit,sheet,expected", [(False, "debt_schedule", 403), (True, "pfs", 404)])
async def test_publish_enforces_view_permission_and_sheet_scope_before_reading_data(can_edit, sheet, expected):
    access = _access(can_edit=can_edit)
    ensure = AsyncMock()
    with patch.object(worksheet_links, "resolve", AsyncMock(return_value=(access, None, None))), patch.object(client_financial_forms, "ensure_snapshot", ensure):
        with pytest.raises(HTTPException) as failure:
            await routes.save_worksheet_version("token", routes.WorksheetVersionBody(sheet=sheet, expected_revision=0, idempotency_key="save-123456"), _request(), SimpleNamespace(), "session")
    assert failure.value.status_code == expected
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_uses_bound_family_and_returns_only_safe_scoped_version_metadata():
    access = _access()
    family_id, version_id = uuid4(), uuid4()
    snapshot = _snapshot(family_id=family_id)
    version = {"id": str(version_id), "version_number": 2, "created_at": datetime.now(UTC).isoformat(),
        "status": "filed", "source": "client", "pdf_file_id": str(uuid4()), "source_file_id": str(uuid4()), "actor_name": "Internal staff"}
    save = AsyncMock(return_value={**snapshot, "saved_version": version})
    db = SimpleNamespace(commit=AsyncMock())
    resolve = AsyncMock(return_value=(access, None, None))
    with (
        patch.object(worksheet_links, "resolve", resolve),
        patch.object(worksheet_links, "bound_families", AsyncMock(return_value={"debt_schedule": family_id})),
        patch.object(worksheet_links, "guard_write_rate"),
        patch.object(worksheet_links, "record_edit", AsyncMock()),
        patch.object(client_financial_forms, "ensure_snapshot", AsyncMock(return_value=snapshot)),
        patch.object(client_financial_forms, "save_submission", save),
        patch.object(sheets, "read_sheets", AsyncMock(return_value={"sheets": [{"kind": "debt_schedule"}, {"kind": "pfs", "values": {"net_worth": "PRIVATE"}}]})),
    ):
        result = await routes.save_worksheet_version("token", routes.WorksheetVersionBody(sheet="debt_schedule", expected_revision=3, idempotency_key="save-123456"), _request(), db, "session")
    assert [row["kind"] for row in result["sheets"]] == ["debt_schedule"]
    assert save.await_args.kwargs["expected_revision"] == 3
    assert save.await_args.args[2]["family_id"] == str(family_id)
    assert resolve.await_args.kwargs["session"] == "session"
    assert set(result["saved_version"]) == {"id", "version_number", "created_at", "source", "status", "pdf_url"}
    assert result["saved_version"]["pdf_url"].endswith(f"/debt_schedule/{version_id}/pdf")
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_pdf_cannot_read_a_different_owner_family_even_with_same_kind():
    access, expected_family, other_family, version_id = _access(scope=("pfs",)), uuid4(), uuid4(), uuid4()
    pdf = AsyncMock()
    with patch.object(worksheet_links, "resolve", AsyncMock(return_value=(access, None, None))), patch.object(worksheet_links, "bound_families", AsyncMock(return_value={"pfs": expected_family})), patch.object(financial_workspace, "version_for_profile", AsyncMock(return_value=(SimpleNamespace(id=other_family, kind="pfs"), None))), patch.object(financial_workspace, "version_pdf", pdf):
        with pytest.raises(HTTPException) as failure:
            await routes.read_worksheet_version_pdf("token", "pfs", version_id, _request(), SimpleNamespace(), "session")
    assert failure.value.status_code == 404
    pdf.assert_not_awaited()


@pytest.mark.asyncio
async def test_pdf_keeps_pin_session_and_serves_immutable_scoped_bytes():
    access, family_id, version_id = _access(can_edit=False), uuid4(), uuid4()
    resolve = AsyncMock(return_value=(access, None, None))
    db = SimpleNamespace(commit=AsyncMock())
    with patch.object(worksheet_links, "resolve", resolve), patch.object(worksheet_links, "bound_families", AsyncMock(return_value={"debt_schedule": family_id})), patch.object(financial_workspace, "version_for_profile", AsyncMock(return_value=(SimpleNamespace(id=family_id, kind="debt_schedule"), None))), patch.object(financial_workspace, "version_pdf", AsyncMock(return_value=(b"%PDF-version2", "Saved Schedule-v2.pdf"))):
        result = await routes.read_worksheet_version_pdf("token", "debt_schedule", version_id, _request(), db, "pin-session")
    assert result.body == b"%PDF-version2" and result.headers["cache-control"] == "no-store"
    assert resolve.await_args.kwargs["session"] == "pin-session"
    db.commit.assert_awaited_once()
