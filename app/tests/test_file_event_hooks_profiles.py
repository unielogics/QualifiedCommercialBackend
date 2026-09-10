"""File-timeline hooks on the profile, offer, pipeline and loan routers.

Source pins: every hook site calls `file_events.emit(` with the tier it was
given, and before the site's commit. Behaviour, without a database: the
underwriting write emits a client status line only when the lifecycle actually
moved and a desk note line only when the reviewer notes changed (never
carrying the note); the client's answer to the processing offer carries no
actor and no reason; the offer send, the pipeline move and the loan PATCH each
emit once, and a move that changes nothing emits nothing.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.enums import LoanStage, Role
from app.routers import application_profiles as profiles_router
from app.routers import loans as loans_router
from app.routers import merchant_offers as offers_router
from app.routers import operator_files as files_router
from app.schemas.application_profile import ApplicationRoomMerchantOfferRespond
from app.schemas.loan import LoanUpdate
from app.schemas.operator_file import PipelineMoveRequest
from app.services import file_events
from app.services import merchant_processing as mp


def _run(coro):
    return asyncio.run(coro)


def _user(role=Role.LOAN_EXEC, name="Desk"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, name=name, email=f"{name.lower()}@example.com")


def _calls(emit: AsyncMock) -> list[dict]:
    return [call.kwargs for call in emit.await_args_list]


def _said(calls: list[dict]) -> str:
    """Everything a timeline row would carry: title, body, label and meta —
    not the ORM objects the hook was handed, which naturally hold the note."""
    return " | ".join(f"{c['title']} {c.get('body')} {c.get('actor_label')} {c.get('meta')}" for c in calls)


def _emit_blocks(src: str) -> list[str]:
    """Each `file_events.emit(...)` call in a handler's source, to its closing paren."""
    blocks = []
    start = src.find("file_events.emit(")
    while start != -1:
        depth, i = 0, src.index("(", start)
        while True:
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            if depth == 0:
                break
            i += 1
        blocks.append(src[start : i + 1])
        start = src.find("file_events.emit(", i)
    return blocks


# ── source pins ─────────────────────────────────────────────────────────────

CLIENT = "file_events.VISIBILITY_CLIENT"
DESK = "file_events.VISIBILITY_DESK"

SITES = [
    (profiles_router.create_application_room_request, "document.requested", [CLIENT]),
    (profiles_router.request_financial_form, "document.requested", [CLIENT]),
    (profiles_router.save_debt_schedule, "document.received", [CLIENT]),
    (profiles_router.submit_public_financial_form, "document.received", [CLIENT]),
    (profiles_router.submit_financial_statement, "document.received", [CLIENT]),
    (profiles_router.apply_underwriting_changes, "status.changed", [CLIENT, DESK]),
    (profiles_router.public_application_room_merchant_offer_respond, "offer.answered", [CLIENT]),
    (offers_router.send_merchant_offer, "offer.sent", [CLIENT]),
    (files_router.move_operator_file_pipeline, "status.changed", [CLIENT]),
    (loans_router.update_loan, "status.changed", [CLIENT]),
]


def test_every_site_calls_emit_with_its_tier_before_the_commit():
    for handler, kind, tiers in SITES:
        src = inspect.getsource(handler)
        assert "file_events.emit(" in src, handler.__name__
        assert f'kind="{kind}"' in src, (handler.__name__, kind)
        for tier in tiers:
            assert tier in src, (handler.__name__, tier)
        boundary = "await db.commit()" if "await db.commit()" in src else "await db.flush()"
        assert src.index("file_events.emit(") < src.index(boundary), handler.__name__
    # The public form submit has two branches, each with its own commit.
    assert inspect.getsource(profiles_router.submit_public_financial_form).count("file_events.emit(") == 2
    # The underwriting write emits the desk note as note.added, and no hook here carries a body.
    underwriting = inspect.getsource(profiles_router.apply_underwriting_changes)
    assert 'kind="note.added"' in underwriting and 'title="Reviewer notes updated"' in underwriting
    for handler, _kind, _tiers in SITES:
        blocks = _emit_blocks(inspect.getsource(handler))
        assert blocks, handler.__name__
        for block in blocks:
            for leak in ("body=", "underwriting_notes", "reviewer_notes", "client_response_reason", "payload.reason", "payload.instructions"):
                assert leak not in block, (handler.__name__, leak, block)


def test_the_pipeline_move_does_not_reach_the_underwriting_write_so_it_hooks_itself():
    src = inspect.getsource(files_router.move_operator_file_pipeline)
    assert "await apply_underwriting_changes(" not in src
    assert "underwriting_status_title(" in src and "before_status" in src


def test_lifecycle_titles_are_plain_words_for_every_status():
    for status_value in ("submitted", "collecting_docs", "in_underwriting", "term_sheet_provided", "approved", "closed_won", "closed_lost", "denied"):
        title = profiles_router.underwriting_status_title(status_value)
        assert title and "_" not in title and len(title) <= 200, status_value
    assert profiles_router.underwriting_status_title("in_underwriting") == "Your file moved to underwriting"
    assert profiles_router.underwriting_status_title("term_sheet_provided") == "Term sheet provided"
    assert profiles_router.underwriting_status_title("something_new") == "Your file moved to something new"
    for stage in LoanStage:
        assert "_" not in loans_router._stage_event_title(stage), stage
    assert loans_router._stage_event_title("collecting_docs") == loans_router._stage_event_title(LoanStage.COLLECTING_DOCS)


# ── the underwriting write ──────────────────────────────────────────────────


def _profile(**kw):
    base = dict(
        id=uuid.uuid4(), loan_id=None, intake_id=None, dealer_id=None, client_id=None, primary_bucket_id=None,
        underwriting_status="submitted", underwriting_notes=None, underwriting_close_outcome=None,
        underwriting_approved_amount=None, underwriting_term_sheet_amount=None, underwriting_current_dscr=None,
        underwriting_target_dscr=None, underwriting_approved_dscr=None, underwriting_updated_by_user_id=None,
        underwriting_updated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _underwrite(profile, changes):
    user = _user()
    db = SimpleNamespace(flush=AsyncMock())
    with patch.object(profiles_router, "_sync_profile_loan_stage", AsyncMock(return_value=None)), \
         patch.object(profiles_router.profiles, "log_profile_action", AsyncMock()), \
         patch.object(file_events, "emit", AsyncMock()) as emit:
        _run(profiles_router.apply_underwriting_changes(db, profile, user, changes))
    calls = _calls(emit)
    for call in calls:
        assert call["actor"] is user and call["profile"] is profile
    return calls


def test_the_underwriting_write_emits_a_client_status_line_only_when_the_lifecycle_moved():
    calls = _underwrite(_profile(), {"underwriting_status": "in_underwriting"})
    assert [(c["kind"], c["visibility"], c["title"]) for c in calls] == [("status.changed", "client", "Your file moved to underwriting")]
    assert calls[0]["meta"] == {"from": "submitted", "to": "in_underwriting"}
    assert _underwrite(_profile(underwriting_status="in_underwriting"), {"underwriting_status": "in_underwriting"}) == []
    assert _underwrite(_profile(), {"approved_amount": 250000.0, "term_sheet_amount": 250000.0}) == []
    assert _underwrite(_profile(), {"underwriting_status": "term_sheet_provided"})[0]["title"] == "Term sheet provided"
    assert _underwrite(_profile(), {"underwriting_status": "denied"})[0]["title"] == "Denied"


def test_reviewer_notes_emit_a_desk_line_that_never_carries_the_note():
    note = "Borrower's cousin vouched for the lease; verify before term sheet."
    calls = _underwrite(_profile(), {"reviewer_notes": note})
    assert [(c["kind"], c["visibility"], c["title"]) for c in calls] == [("note.added", "desk", "Reviewer notes updated")]
    assert "cousin" not in _said(calls) and calls[0].get("body") is None
    assert _underwrite(_profile(underwriting_notes=note), {"reviewer_notes": note}) == []
    both = _underwrite(_profile(), {"underwriting_status": "approved", "reviewer_notes": note})
    assert [c["kind"] for c in both] == ["status.changed", "note.added"]
    assert both[0]["title"] == "Approved" and "cousin" not in _said(both)


# ── the client's answer to the processing offer ─────────────────────────────


def test_the_clients_answer_emits_one_client_line_with_no_actor_and_no_reason():
    profile = SimpleNamespace(id=uuid.uuid4(), intake_id=None, client_id=None, dealer_id=None)
    offer = SimpleNamespace(id=uuid.uuid4(), status=mp.STATUS_SENT, terms_version=1, partner_email_status="sent", partner_email_error=None)
    payload = ApplicationRoomMerchantOfferRespond(passcode="123456", response="declined", responder_name="Ana Lopez", reason="Too expensive this quarter", terms_version=1)
    order: list[str] = []
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: order.append("commit")), get=AsyncMock(return_value=None))
    with patch.object(profiles_router, "_public_application_room", AsyncMock(return_value=(SimpleNamespace(), profile))), \
         patch.object(profiles_router, "_room_visible_offer", AsyncMock(return_value=offer)), \
         patch.object(profiles_router, "_room_offer_partner_name", AsyncMock(return_value="Acme Pay")), \
         patch.object(profiles_router, "_client_ip", lambda request: "203.0.113.9"), \
         patch.object(mp, "sync_intake_state", AsyncMock()), \
         patch.object(mp, "notify_partner", AsyncMock()), \
         patch.object(mp, "client_view", lambda offer, partner_name=None: {"status": offer.status}), \
         patch.object(profiles_router.profiles, "log_profile_action", AsyncMock()), \
         patch.object(file_events, "emit", AsyncMock(side_effect=lambda *a, **k: order.append("emit"))) as emit:
        out = _run(profiles_router.public_application_room_merchant_offer_respond("tok", payload, SimpleNamespace(headers={}), db))
    calls = _calls(emit)
    assert out == {"status": mp.STATUS_DECLINED} and offer.client_response_reason == "Too expensive this quarter"
    assert [(c["kind"], c["visibility"], c["title"], c["actor"], c["target_id"]) for c in calls] == [("offer.answered", "client", "Processing offer declined", None, offer.id)]
    assert "expensive" not in _said(calls) and calls[0].get("body") is None
    assert order[:2] == ["emit", "commit"]


# ── sending the offer ───────────────────────────────────────────────────────


def test_sending_the_offer_emits_one_client_line_naming_the_saving():
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=None, intake_id=uuid.uuid4())
    user = _user()
    offer = SimpleNamespace(id=uuid.uuid4(), status=mp.STATUS_EXTRACTED, estimated_annual_savings=Decimal("4200.00"), lender_id=uuid.uuid4(), sent_at=None, sent_by_user_id=None)
    order: list[str] = []
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: order.append("commit")))
    with patch.object(offers_router, "_profile", AsyncMock(return_value=profile)), \
         patch.object(offers_router, "_open_offer", AsyncMock(return_value=offer)), \
         patch.object(offers_router, "_panel", AsyncMock(return_value="panel")), \
         patch.object(mp, "sync_intake_state", AsyncMock()), \
         patch.object(offers_router.profiles, "log_profile_action", AsyncMock()), \
         patch.object(file_events, "emit", AsyncMock(side_effect=lambda *a, **k: order.append("emit"))) as emit:
        assert _run(offers_router.send_merchant_offer(profile.id, offers_router.MerchantOfferSend(), user, db)) == "panel"
    calls = _calls(emit)
    assert offer.status == mp.STATUS_SENT
    assert [(c["kind"], c["visibility"], c["actor"], c["target_id"]) for c in calls] == [("offer.sent", "client", user, offer.id)]
    assert calls[0]["title"] == "Processing offer sent: estimated annual savings $4,200.00"
    assert order == ["emit", "commit"]


# ── the pipeline move ───────────────────────────────────────────────────────


def _move(profile, target_status):
    user = _user()
    order: list[str] = []
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: order.append("commit")), refresh=AsyncMock(), get=AsyncMock(return_value=None))
    with patch.object(files_router.profiles, "resolve_profile", AsyncMock(return_value=profile)), \
         patch.object(files_router, "_sync_pipeline_loan_stage", AsyncMock(return_value=None)), \
         patch.object(files_router.profiles, "log_profile_action", AsyncMock()), \
         patch.object(file_events, "emit", AsyncMock(side_effect=lambda *a, **k: order.append("emit"))) as emit:
        result = _run(files_router.move_operator_file_pipeline("loan", uuid.uuid4(), PipelineMoveRequest(target_status=target_status), SimpleNamespace(), user, db))
    return result, _calls(emit), order, user


def test_the_pipeline_move_emits_one_client_status_line_and_a_no_op_move_emits_nothing():
    profile = SimpleNamespace(id=uuid.uuid4(), loan_id=None, intake_id=None, dealer_id=None, underwriting_status="submitted", underwriting_updated_by_user_id=None, underwriting_updated_at=None, underwriting_close_outcome=None)
    result, calls, order, user = _move(profile, "collecting_docs")
    assert result.underwriting_status == "collecting_docs" and profile.underwriting_status == "collecting_docs"
    assert [(c["kind"], c["visibility"], c["title"], c["actor"], c["profile"]) for c in calls] == [("status.changed", "client", "We are collecting your documents", user, profile)]
    assert calls[0]["meta"] == {"from": "submitted", "to": "collecting_docs"}
    assert order == ["emit", "commit"]
    _result, calls, order, _user_ = _move(profile, "collecting_docs")
    assert calls == [] and order == ["commit"]


# ── the loan PATCH ──────────────────────────────────────────────────────────


def test_a_loan_stage_patch_emits_one_client_line_and_other_patches_do_not():
    loan = SimpleNamespace(id=uuid.uuid4(), deal_id="QC-1001", stage=LoanStage.PREQUALIFIED, client_id=None, address="1 Main St")
    user = _user()
    order: list[str] = []
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: loan)),
        flush=AsyncMock(side_effect=lambda: order.append("flush")),
        refresh=AsyncMock(),
    )

    def run(payload):
        order.clear()
        with patch.object(loans_router, "_scope_query", lambda user, stmt: stmt), \
             patch("app.services.activity_log.log_loan_diff", AsyncMock()), \
             patch.object(loans_router, "mark_loan_dirty", AsyncMock()), \
             patch.object(loans_router, "LoanRead", SimpleNamespace(model_validate=lambda x: x)), \
             patch.object(file_events, "emit", AsyncMock(side_effect=lambda *a, **k: order.append("emit"))) as emit:
            _run(loans_router.update_loan(loan.id, payload, user, db))
        return _calls(emit)

    calls = run(LoanUpdate(stage=LoanStage.COLLECTING_DOCS))
    assert loan.stage == LoanStage.COLLECTING_DOCS
    assert [(c["kind"], c["visibility"], c["loan_id"], c["actor"], c["title"]) for c in calls] == [("status.changed", "client", loan.id, user, "We are collecting your documents")]
    assert calls[0]["meta"] == {"from": "prequalified", "to": "collecting_docs"}
    assert order == ["emit", "flush"]
    assert run(LoanUpdate(stage=LoanStage.COLLECTING_DOCS)) == []
    assert run(LoanUpdate(address="2 Main St")) == []
