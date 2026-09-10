"""The four-form routes without a database: how the two business statements
are reported on the panel, how a link of either kind dispatches on the public
endpoints, and the packet — one link, four forms, one close.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import application_profiles as router
from app.schemas.application_profile import FinancialFormsRead, FinancialStatementWrite
from app.services import application_profiles as profiles
from app.services import business_statement_schema as bss
from app.services import business_statements, file_events, financial_statements, pfs_schema
from app.services import public_underwriting_packet_pdf as packet_pdf


def _run(coro):
    return asyncio.run(coro)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role=Role.LOAN_EXEC, name="Jane Desk", email="jane@example.com")


def _profile(bucket=True):
    return SimpleNamespace(id=uuid.uuid4(), primary_bucket_id=uuid.uuid4() if bucket else None, dealer_id=None, intake_id=None)


def _db(rows=None, scalar=None, get=None):
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: list(rows or []), first=lambda: (rows or [None])[0]),
        scalar_one_or_none=lambda: scalar,
    )
    return SimpleNamespace(
        execute=AsyncMock(return_value=result),
        add=MagicMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
        get=AsyncMock(return_value=get),
    )


def _slot(name="Year-to-date P&L and balance sheet", status="uploaded"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, category="Financials", status=status)


def _pl_body(**header):
    body = bss.pl_empty_body()
    body["header"].update(period_start="2026-01-01", period_end="2026-06-30", **header)
    body["sections"]["revenue"]["gross_revenue"] = "100000"
    body["sections"]["operating_expenses"]["interest"] = "1000"
    return body


def _statement(kind="p_and_l", status="submitted", body=None):
    return SimpleNamespace(
        id=uuid.uuid4(), kind=kind, status=status, body=body or _pl_body(),
        updated_at=datetime.now(UTC), submitted_by_user_id=None,
    )


# ── status assembly ─────────────────────────────────────────────────────────


def _status(kind, *, slot, statement, slot_analyses=([], False), bucket_analyses=(), extracted=None, profile=None):
    name = "extract_profit_and_loss" if kind == "p_and_l" else "extract_balance_sheet"
    with (
        patch.object(business_statements, "slot_for_kind", AsyncMock(return_value=slot)),
        patch.object(business_statements, "latest_for_profile", AsyncMock(return_value=statement)),
        patch.object(router, "_slot_analyses", AsyncMock(return_value=slot_analyses)) as slot_reads,
        patch.object(router, "_bucket_analyses", AsyncMock(return_value=list(bucket_analyses))) as bucket_reads,
        patch.object(packet_pdf, name, MagicMock(return_value=extracted), create=True) as extractor,
    ):
        status = _run(router._business_statement_status(_db(), profile or _profile(), kind))
    return status, slot_reads, bucket_reads, extractor


def test_a_submitted_statement_reports_filled_with_the_forms_own_figures():
    status, _, _, extractor = _status("p_and_l", slot=_slot(), statement=_statement())
    assert status.source == "filled" and status.satisfied and status.requested
    assert status.figures_from == "form"
    assert status.period_label == "Jan–Jun 2026"
    assert status.net_income == 99000.0
    assert status.ebitda == 100000.0
    assert status.statement_id is not None
    assert status.filled_by_staff is False
    extractor.assert_not_called()


def test_a_recognised_upload_on_the_slot_reports_uploaded_with_the_documents_figures():
    analyses = [{"classification": "current_p_and_l", "key_facts": {"net_income": 5}}]
    status, slot_reads, bucket_reads, extractor = _status(
        "p_and_l", slot=_slot(), statement=None, slot_analyses=(analyses, False),
        extracted={"period_start": "2026-01-01", "period_end": "2026-03-31", "net_income": 12000, "ebitda": 15000},
    )
    assert status.source == "uploaded" and status.satisfied and status.requested
    assert status.figures_from == "document"
    assert status.period_label == "Jan–Mar 2026"
    assert (status.net_income, status.ebitda) == (12000.0, 15000.0)
    extractor.assert_called_once_with(analyses)
    bucket_reads.assert_not_awaited()


def test_without_a_slot_the_whole_bucket_is_read():
    analyses = [{"classification": "balance_sheet", "key_facts": {}}]
    status, slot_reads, bucket_reads, extractor = _status(
        "balance_sheet", slot=None, statement=None, bucket_analyses=analyses,
        extracted={"as_of_date": "2026-06-30", "total_assets": 1000, "total_liabilities": 400, "total_equity": 600},
    )
    assert status.source == "uploaded" and status.satisfied
    assert status.requested is False
    assert status.period_label == "as of 2026-06-30"
    assert (status.total_assets, status.total_liabilities, status.total_equity) == (1000.0, 400.0, 600.0)
    assert status.balances is True
    extractor.assert_called_once_with(analyses)
    slot_reads.assert_not_awaited()


def test_a_p_and_l_on_the_shared_slot_leaves_the_balance_sheet_unsatisfied():
    """Never `slot.status == "uploaded"`: Main Street asks for both on one row."""
    analyses = [{"classification": "current_p_and_l", "key_facts": {"gross_revenue": 1}}]
    status, _, bucket_reads, extractor = _status(
        "balance_sheet", slot=_slot(status="uploaded"), statement=None,
        slot_analyses=(analyses, False), extracted=None,
    )
    assert status.source == "none"
    assert status.satisfied is False
    assert status.requested is True
    extractor.assert_called_once_with(analyses)
    bucket_reads.assert_not_awaited()


def test_a_document_still_being_read_is_pending_not_missing():
    status, *_ = _status("p_and_l", slot=_slot(), statement=None, slot_analyses=([], True))
    assert status.source == "none" and status.analysis_pending is True


def test_a_draft_is_not_filled():
    status, *_ = _status("p_and_l", slot=None, statement=_statement(status="draft"), profile=_profile(bucket=False))
    assert status.source == "none" and status.satisfied is False
    assert status.statement_id is not None


def test_a_typed_balance_sheet_reports_whether_it_balances():
    body = bss.bs_empty_body()
    body["header"]["as_of_date"] = "2026-06-30"
    body["sections"]["current_assets"]["cash_in_bank"] = "1000"
    body["sections"]["equity"]["owner_capital"] = "5"
    status, *_ = _status("balance_sheet", slot=_slot(), statement=_statement("balance_sheet", body=body))
    assert status.source == "filled" and status.balances is False
    assert status.total_equity == 5.0


def test_the_extractors_are_imported_lazily_and_their_absence_means_none():
    src = inspect.getsource(router._recognised_business_statement)
    assert "from app.services.public_underwriting_packet_pdf import" in src
    with patch.dict("sys.modules", {"app.services.public_underwriting_packet_pdf": None}):
        assert router._recognised_business_statement("p_and_l", []) is None


def test_the_status_read_lists_four_forms_and_the_packets():
    profile = _profile()
    user = _user()
    entry = lambda kind: router.FinancialFormStatus(kind=kind, label=kind)  # noqa: E731
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(router, "_requested_slot", AsyncMock(return_value=None)),
        patch.object(financial_statements, "latest_for_profile", AsyncMock(return_value=None)),
        patch.object(router, "_business_statement_status", AsyncMock(side_effect=lambda db, p, k: entry(k))),
        patch.object(router, "_packets_for_profile", AsyncMock(return_value=[router.FinancialFormPacket(packet_id=uuid.uuid4())])),
    ):
        read = _run(router.financial_forms_status(profile.id, user, _db(rows=[])))
    assert isinstance(read, FinancialFormsRead)
    assert [f.kind for f in read.forms] == ["pfs", "debt_schedule", "p_and_l", "balance_sheet"]
    assert len(read.packets) == 1


def test_requested_slot_dispatches_the_two_kinds_to_slot_for_kind():
    slot = _slot()
    with patch.object(business_statements, "slot_for_kind", AsyncMock(return_value=slot)) as finder:
        assert _run(router._requested_slot(_db(), _profile(), "balance_sheet")) is slot
    finder.assert_awaited_once()
    assert router._FORM_LABEL["p_and_l"] == "Profit and loss statement"
    assert router._FORM_SLOT_CATEGORY["balance_sheet"] == "Financials"
    assert "packet" not in router._FORM_SLOT_CATEGORY and "packet" not in router._FORM_LABEL


def test_the_schema_route_serves_describe_and_404s_anything_else():
    served = _run(router.business_statement_form_schema("balance_sheet", _user()))
    assert served == bss.describe("balance_sheet")
    with pytest.raises(HTTPException) as caught:
        _run(router.business_statement_form_schema("pfs", _user()))
    assert caught.value.status_code == 404


# ── the desk's own routes ───────────────────────────────────────────────────


def test_the_body_route_returns_the_seeded_body_with_status_and_id():
    profile = _profile()
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(financial_statements, "form_prefill", AsyncMock(return_value={"business_name": "Acme"})),
        patch.object(business_statements, "latest_for_profile", AsyncMock(return_value=None)),
    ):
        body = _run(router.read_business_statement_body(profile.id, "p_and_l", _user(), _db()))
    assert body["header"]["business_name"] == "Acme"
    assert body["status"] is None and body["statement_id"] is None
    assert body["schema_version"] == "qc_pl.v1"


def test_the_put_route_saves_a_draft_without_touching_the_checklist():
    profile, user = _profile(), _user()
    saved = _statement(status="draft")
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(business_statements, "save", AsyncMock(return_value=saved)) as save,
        patch.object(business_statements, "ensure_slot", AsyncMock()) as ensure,
        patch.object(business_statements, "file_pdf", AsyncMock()) as filed,
        patch.object(profiles, "log_profile_action", AsyncMock()),
    ):
        result = _run(router.save_business_statement(profile.id, "p_and_l", router.FinancialFormSave(body=_pl_body()), user, _db()))
    assert result == {"statement_id": saved.id, "status": "draft", "submitted": False}
    assert save.await_args.kwargs["status"] == "draft"
    assert save.await_args.kwargs["actor_user_id"] == user.id
    ensure.assert_not_awaited()
    filed.assert_not_awaited()


def test_the_put_route_with_submit_ensures_the_slot_and_files():
    profile, user = _profile(), _user()
    saved = _statement(status="submitted")
    slot = _slot()
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(business_statements, "save", AsyncMock(return_value=saved)) as save,
        patch.object(business_statements, "ensure_slot", AsyncMock(return_value=slot)) as ensure,
        patch.object(business_statements, "file_pdf", AsyncMock()) as filed,
        patch.object(profiles, "log_profile_action", AsyncMock()),
    ):
        result = _run(router.save_business_statement(profile.id, "balance_sheet", router.FinancialFormSave(body=bss.bs_empty_body(), submit=True), user, _db()))
    assert result["submitted"] is True and result["status"] == "submitted"
    ensure.assert_awaited_once()
    assert ensure.await_args.args[1:] == (profile, "balance_sheet")
    assert ensure.await_args.kwargs == {"required": False}
    assert save.await_args_list[-1].kwargs["status"] == "submitted"
    kwargs = filed.await_args.kwargs
    assert kwargs["slot"] is slot and kwargs["actor_user_id"] == user.id and kwargs["actor"] is user


def test_the_pdf_and_request_routes_take_the_two_kinds_and_still_refuse_packet():
    src = inspect.getsource(router.financial_form_pdf)
    assert "business_statement_schema.KINDS" in src and "render_balance_sheet_pdf" in src
    src = inspect.getsource(router.request_financial_form)
    assert "business_statements.ensure_slot(db, profile, kind, required=True)" in src
    for handler in (router.financial_form_pdf, router.request_financial_form):
        with pytest.raises(HTTPException) as caught:
            _run(handler(uuid.uuid4(), "packet", _user(), _db()))
        assert caught.value.status_code == 404


# ── the public endpoints, by kind ───────────────────────────────────────────


def _link(kind, **overrides):
    base = dict(id=uuid.uuid4(), kind=kind, profile_id=uuid.uuid4(), statement_id=None, invitee_email=None, completed_at=None, last_used_at=None, revoked_at=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_public_get_dispatches_a_business_statement_link_to_its_schema_and_seeded_body():
    link = _link("balance_sheet")
    profile = _profile()
    with (
        patch.object(financial_statements, "link_for_token", AsyncMock(return_value=link)),
        patch.object(financial_statements, "form_prefill", AsyncMock(return_value={"business_name": "Acme", "owner_count": 1})),
        patch.object(business_statements, "latest_for_profile", AsyncMock(return_value=None)),
    ):
        out = _run(router.public_financial_form("tok", _db(get=profile)))
    assert out["kind"] == "balance_sheet"
    assert out["schema"]["kind"] == "balance_sheet"
    assert out["body"]["header"]["business_name"] == "Acme"
    assert out["completed"] is False
    assert out["business_name"] == "Acme"
    assert set(out) == {"kind", "schema", "body", "completed", "business_name", "prefill"}
    assert link.last_used_at is not None


def test_public_get_still_serves_the_pfs_and_debt_schedule_as_before():
    with (
        patch.object(financial_statements, "link_for_token", AsyncMock(return_value=_link("pfs"))),
        patch.object(financial_statements, "form_prefill", AsyncMock(return_value={})),
    ):
        out = _run(router.public_financial_form("tok", _db(get=_profile())))
    assert out["kind"] == "pfs" and out["schema"] == pfs_schema.describe()


def test_public_draft_saves_a_draft_of_the_kind_and_ignores_owners():
    link = _link("p_and_l")
    payload = FinancialStatementWrite(body=_pl_body(), owners=[])
    with (
        patch.object(financial_statements, "link_for_token", AsyncMock(return_value=link)),
        patch.object(business_statements, "save", AsyncMock(return_value=_statement(status="draft"))) as save,
    ):
        out = _run(router.save_public_financial_form_draft("tok", payload, _db(get=_profile())))
    assert out == {"saved": True}
    kwargs = save.await_args.kwargs
    assert kwargs["kind"] == "p_and_l" and kwargs["status"] == "draft"


def test_public_submit_files_as_the_borrower_and_leaves_no_staff_stamp():
    link = _link("p_and_l", invitee_email="cpa@example.com")
    profile = _profile()
    db = _db(get=profile, scalar=None)
    slot = _slot()
    with (
        patch.object(financial_statements, "link_for_token", AsyncMock(return_value=link)),
        patch.object(business_statements, "ensure_slot", AsyncMock(return_value=slot)) as ensure,
        patch.object(business_statements, "file_pdf", AsyncMock()) as filed,
    ):
        out = _run(router.submit_public_financial_form("tok", FinancialStatementWrite(body=_pl_body(prepared_by="Pat CPA")), db))
    assert out == {"completed": True}
    assert link.completed_at is not None
    ensure.assert_awaited_once()
    assert ensure.await_args.kwargs == {"required": False}
    kwargs = filed.await_args.kwargs
    statement = filed.await_args.args[2]
    assert statement.status == "submitted"
    assert statement.submitted_by_user_id is None   # the real save ran; nobody on staff
    assert kwargs["actor_user_id"] is None and kwargs["actor"] is None
    assert kwargs["actor_name"] == "Pat CPA"
    assert kwargs["actor_email"] == "cpa@example.com"
    db.commit.assert_awaited()


def test_the_public_submit_keeps_exactly_two_emit_sites_and_the_service_owns_the_third():
    assert inspect.getsource(router.submit_public_financial_form).count("file_events.emit(") == 2
    assert 'kind="document.received"' in inspect.getsource(business_statements.file_pdf)


# ── the packet ──────────────────────────────────────────────────────────────


def _mint(profile=None, user=None, *, pfs=None, requested_slot=None):
    profile = profile or _profile()
    user = user or _user()
    db = _db(scalar=None)
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(router, "_requested_slot", AsyncMock(return_value=requested_slot)),
        patch.object(business_statements, "ensure_slot", AsyncMock(return_value=_slot())) as ensure,
        patch.object(financial_statements, "latest_for_profile", AsyncMock(return_value=pfs)),
        patch.object(financial_statements, "save_statement", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="draft"))) as create_pfs,
        patch.object(file_events, "emit", AsyncMock()) as emit,
        patch.object(profiles, "log_profile_action", AsyncMock()) as logged,
    ):
        result = _run(router.mint_financial_form_link(profile.id, "packet", user, db))
    links = [call.args[0] for call in db.add.call_args_list if getattr(call.args[0], "token_hash", None)]
    slots = [call.args[0] for call in db.add.call_args_list if not getattr(call.args[0], "token_hash", None)]
    return result, links, slots, ensure, create_pfs, emit, logged, db


def test_the_packet_is_four_children_with_derived_tokens_sharing_one_id():
    result, links, slots, ensure, create_pfs, emit, logged, db = _mint()
    base = result["url"].rsplit("/forms/packet/", 1)[1]
    assert "." not in base
    assert result["packet_id"] and result["expires_at"] is not None
    assert {link.kind for link in links} == {"pfs", "debt_schedule", "p_and_l", "balance_sheet"}
    assert len({link.packet_id for link in links}) == 1 and links[0].packet_id == result["packet_id"]
    for link in links:
        # Each child resolves through link_for_token unchanged: same hash rule.
        assert link.token_hash == financial_statements.hash_token(f"{base}.{link.kind}")
        assert link.expires_at is not None and link.revoked_at is None
        assert link.created_by is not None
    pfs = next(link for link in links if link.kind == "pfs")
    assert pfs.statement_id == create_pfs.return_value.id   # attached, as a pfs link is
    assert pfs.completed_at is None
    assert all(link.statement_id is None for link in links if link.kind != "pfs")
    db.commit.assert_awaited_once()


def test_the_packet_ensures_all_four_slots_silently_and_emits_one_event():
    result, links, slots, ensure, create_pfs, emit, logged, db = _mint()
    assert [call.args[2] for call in ensure.await_args_list] == ["p_and_l", "balance_sheet"]
    assert all(call.kwargs == {"required": False} for call in ensure.await_args_list)
    assert sorted(slot.category for slot in slots) == ["Debts", "Personal Financials"]
    assert all(slot.required is False for slot in slots)
    emit.assert_awaited_once()
    event = emit.await_args.kwargs
    assert event["kind"] == "forms.packet_sent"
    assert event["title"] == "Financial forms packet was sent"
    assert event["visibility"] == file_events.VISIBILITY_CLIENT
    assert event["target_id"] == result["packet_id"]
    assert logged.await_args.args[3] == "financial_form.packet_link_minted"


def test_existing_slots_are_not_recreated():
    _, _, slots, ensure, *_ = _mint(requested_slot=_slot("Personal financial statement"))
    assert slots == []
    assert ensure.await_count == 2   # the two new kinds still go through ensure_slot, idempotently


def test_an_already_submitted_pfs_is_marked_done_on_its_child():
    submitted = SimpleNamespace(id=uuid.uuid4(), status="submitted")
    _, links, _, _, create_pfs, *_ = _mint(pfs=submitted)
    pfs = next(link for link in links if link.kind == "pfs")
    assert pfs.statement_id == submitted.id
    assert pfs.completed_at is not None
    create_pfs.assert_not_awaited()


def test_the_packet_409s_without_a_room_and_never_reaches_the_category_guard():
    with patch.object(profiles, "load_profile", AsyncMock(return_value=_profile(bucket=False))):
        with pytest.raises(HTTPException) as caught:
            _run(router.mint_financial_form_link(uuid.uuid4(), "packet", _user(), _db()))
    assert caught.value.status_code == 409
    src = inspect.getsource(router.mint_financial_form_link)
    assert src.index('if kind == "packet"') < src.index("if kind not in _FORM_SLOT_CATEGORY")


def test_a_single_link_of_a_new_kind_mints_without_touching_the_checklist():
    profile, user = _profile(), _user()
    link = SimpleNamespace(id=uuid.uuid4(), expires_at=datetime.now(UTC) + timedelta(days=30))
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(financial_statements, "mint_link", AsyncMock(return_value=(link, "tok"))) as mint,
        patch.object(business_statements, "ensure_slot", AsyncMock()) as ensure,
        patch.object(profiles, "log_profile_action", AsyncMock()),
    ):
        result = _run(router.mint_financial_form_link(profile.id, "balance_sheet", user, _db()))
    assert result["url"].endswith("/forms/balance_sheet/tok")
    assert mint.await_args.kwargs["kind"] == "balance_sheet" and mint.await_args.kwargs["statement_id"] is None
    ensure.assert_not_awaited()


def test_a_pfs_link_minted_the_old_way_still_draws_its_own_dotless_token():
    db = _db()
    link, token = _run(financial_statements.mint_link(db, _profile(), kind="pfs"))
    assert "." not in token and len(token) > 30
    assert link.token_hash == financial_statements.hash_token(token)
    assert link.packet_id is None
    given, token = _run(financial_statements.mint_link(db, _profile(), kind="p_and_l", token="base.p_and_l"))
    assert token == "base.p_and_l" and given.token_hash == financial_statements.hash_token("base.p_and_l")


def test_revoke_closes_all_four_children_at_once():
    profile = _profile()
    packet_id = uuid.uuid4()
    children = [
        SimpleNamespace(kind=k, packet_id=packet_id, revoked_at=None, expires_at=datetime.now(UTC) + timedelta(days=1))
        for k in router.PACKET_KINDS
    ]
    db = _db(rows=children)
    with (
        patch.object(profiles, "load_profile", AsyncMock(return_value=profile)),
        patch.object(profiles, "log_profile_action", AsyncMock()),
    ):
        out = _run(router.revoke_financial_form_packet(profile.id, packet_id, _user(), db))
    assert out == {"revoked": True}
    assert all(child.revoked_at is not None for child in children)
    from app.models.financial_form_link import FinancialFormLink

    closed = FinancialFormLink(kind="pfs", token_hash="x", expires_at=children[0].expires_at, revoked_at=children[0].revoked_at)
    assert closed.is_open is False
    with patch.object(profiles, "load_profile", AsyncMock(return_value=profile)):
        with pytest.raises(HTTPException) as caught:
            _run(router.revoke_financial_form_packet(profile.id, uuid.uuid4(), _user(), _db(rows=[])))
    assert caught.value.status_code == 404


def test_packets_are_grouped_from_their_children():
    profile = _profile()
    a, b = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(kind="pfs", packet_id=a, created_at=now, expires_at=now, completed_at=now, revoked_at=None),
        SimpleNamespace(kind="p_and_l", packet_id=a, created_at=now, expires_at=now, completed_at=None, revoked_at=None),
        SimpleNamespace(kind="pfs", packet_id=b, created_at=now - timedelta(days=1), expires_at=now, completed_at=None, revoked_at=now),
        SimpleNamespace(kind="p_and_l", packet_id=b, created_at=now - timedelta(days=1), expires_at=now, completed_at=None, revoked_at=now),
    ]
    packets = _run(router._packets_for_profile(_db(rows=rows), profile))
    assert [p.packet_id for p in packets] == [a, b]   # newest first
    assert packets[0].completed_kinds == ["pfs"] and packets[0].revoked is False
    assert packets[1].completed_kinds == [] and packets[1].revoked is True
