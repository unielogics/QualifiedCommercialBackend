from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import LoanStage, Role
from app.routers import operator_files as files_router
from app.schemas.operator_file import PipelineMoveRequest
from app.services import file_events


def _run(coro):
    return asyncio.run(coro)


def _user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        role=Role.LOAN_EXEC,
        name="Desk",
        email="desk@example.com",
    )


def _profile(**overrides):
    values = {
        "id": uuid.uuid4(),
        "loan_id": uuid.uuid4(),
        "intake_id": None,
        "dealer_id": None,
        "underwriting_status": "in_underwriting",
        "underwriting_approved_amount": None,
        "underwriting_term_sheet_amount": None,
        "underwriting_approved_dscr": None,
        "underwriting_notes": None,
        "underwriting_updated_by_user_id": None,
        "underwriting_updated_at": None,
        "underwriting_close_outcome": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_sync_pipeline_loan_stage_accepts_database_string_and_audits_values():
    profile = _profile()
    loan = SimpleNamespace(id=profile.loan_id, stage="collecting_docs")
    db = SimpleNamespace(get=AsyncMock(return_value=loan))
    user = _user()

    with (
        patch.object(files_router, "log_activity", AsyncMock()) as log_activity,
        patch.object(files_router, "mark_loan_dirty", AsyncMock()) as mark_dirty,
    ):
        result = _run(
            files_router._sync_pipeline_loan_stage(
                db,
                profile=profile,
                target_status="approved",
                user=user,
                note="Approved by the desk",
            )
        )

    assert result is loan
    assert loan.stage == LoanStage.CLOSING
    assert log_activity.await_args.kwargs["summary"].endswith("collecting_docs -> closing")
    assert log_activity.await_args.kwargs["payload"] == {
        "profile_id": str(profile.id),
        "underwriting_status": "approved",
        "from": "collecting_docs",
        "to": "closing",
        "note": "Approved by the desk",
    }
    mark_dirty.assert_awaited_once_with(db, loan.id)


def test_sync_pipeline_loan_stage_treats_matching_database_string_as_no_op():
    profile = _profile()
    loan = SimpleNamespace(id=profile.loan_id, stage="closing")
    db = SimpleNamespace(get=AsyncMock(return_value=loan))

    with (
        patch.object(files_router, "log_activity", AsyncMock()) as log_activity,
        patch.object(files_router, "mark_loan_dirty", AsyncMock()) as mark_dirty,
    ):
        result = _run(
            files_router._sync_pipeline_loan_stage(
                db,
                profile=profile,
                target_status="approved",
                user=_user(),
                note=None,
            )
        )

    assert result is loan
    log_activity.assert_not_awaited()
    mark_dirty.assert_not_awaited()


def _move_to_approved(profile, payload):
    user = _user()
    loan = SimpleNamespace(id=profile.loan_id, stage="closing")
    db = SimpleNamespace(
        commit=AsyncMock(),
        refresh=AsyncMock(),
        get=AsyncMock(return_value=None),
    )
    with (
        patch.object(files_router.profiles, "resolve_profile", AsyncMock(return_value=profile)),
        patch.object(files_router, "_sync_pipeline_loan_stage", AsyncMock(return_value=loan)),
        patch.object(files_router.profiles, "log_profile_action", AsyncMock()) as log_action,
        patch.object(file_events, "emit", AsyncMock()) as emit,
    ):
        result = _run(
            files_router.move_operator_file_pipeline(
                "loan",
                profile.loan_id,
                payload,
                SimpleNamespace(),
                user,
                db,
            )
        )
    return result, db, log_action, emit


def test_approval_persists_details_and_keeps_note_out_of_client_timeline():
    profile = _profile()
    result, db, log_action, emit = _move_to_approved(
        profile,
        PipelineMoveRequest(
            target_status="approved",
            approved_amount=275_000,
            approved_dscr=1.27,
            note="  Approved for the equipment tranche.  ",
        ),
    )

    assert profile.underwriting_status == "approved"
    assert profile.underwriting_approved_amount == 275_000
    assert profile.underwriting_approved_dscr == 1.27
    assert profile.underwriting_notes == "Approved for the equipment tranche."
    assert result.loan_stage == "closing"
    metadata = log_action.await_args.kwargs["metadata"]
    assert metadata["approved_amount"] == 275_000
    assert metadata["approved_dscr"] == 1.27
    assert metadata["note"] == "Approved for the equipment tranche."
    client_event = emit.await_args.kwargs
    assert client_event["visibility"] == file_events.VISIBILITY_CLIENT
    assert client_event["meta"] == {"from": "in_underwriting", "to": "approved"}
    assert client_event.get("body") is None
    assert "equipment tranche" not in str(client_event["meta"])
    db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    ("existing_amount", "term_sheet_amount", "expected"),
    [
        (Decimal("180000.00"), Decimal("190000.00"), 180_000.0),
        (None, Decimal("190000.00"), 190_000.0),
        (Decimal("0"), Decimal("190000.00"), 190_000.0),
    ],
)
def test_approval_amount_falls_back_to_existing_then_term_sheet(
    existing_amount, term_sheet_amount, expected
):
    profile = _profile(
        underwriting_approved_amount=existing_amount,
        underwriting_term_sheet_amount=term_sheet_amount,
    )

    _result, _db, log_action, _emit = _move_to_approved(
        profile,
        PipelineMoveRequest(target_status="approved"),
    )

    assert profile.underwriting_approved_amount == expected
    assert log_action.await_args.kwargs["metadata"]["approved_amount"] == expected


def test_explicit_null_approved_dscr_clears_a_stale_value():
    profile = _profile(
        underwriting_approved_amount=Decimal("180000.00"),
        underwriting_approved_dscr=Decimal("1.35"),
    )

    _result, _db, log_action, _emit = _move_to_approved(
        profile,
        PipelineMoveRequest(target_status="approved", approved_dscr=None),
    )

    assert profile.underwriting_approved_dscr is None
    assert log_action.await_args.kwargs["metadata"]["approved_dscr"] is None


def test_approval_without_a_positive_effective_amount_is_rejected_before_writes():
    profile = _profile(
        loan_id=None,
        intake_id=uuid.uuid4(),
        underwriting_approved_amount=Decimal("0"),
        underwriting_term_sheet_amount=None,
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), get=AsyncMock())

    with (
        patch.object(files_router.profiles, "resolve_profile", AsyncMock(return_value=profile)),
        patch.object(files_router, "promote_intake_to_funding", AsyncMock()) as promote,
        patch.object(files_router, "_sync_pipeline_loan_stage", AsyncMock()) as sync_stage,
        patch.object(files_router.profiles, "log_profile_action", AsyncMock()) as log_action,
        patch.object(file_events, "emit", AsyncMock()) as emit,
        pytest.raises(HTTPException) as exc_info,
    ):
        _run(
            files_router.move_operator_file_pipeline(
                "intake",
                profile.intake_id,
                PipelineMoveRequest(target_status="approved", note="should not persist"),
                SimpleNamespace(),
                _user(),
                db,
            )
        )

    assert exc_info.value.status_code == 422
    assert profile.underwriting_status == "in_underwriting"
    assert profile.underwriting_notes is None
    promote.assert_not_awaited()
    sync_stage.assert_not_awaited()
    log_action.assert_not_awaited()
    emit.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_pipeline_move_approval_fields_validate_the_api_contract():
    valid = PipelineMoveRequest(
        target_status="approved",
        approved_amount=1,
        approved_dscr=0,
    )
    assert valid.approved_amount == 1
    assert valid.approved_dscr == 0

    for invalid in (
        {"approved_amount": 0},
        {"approved_amount": -1},
        {"approved_amount": float("inf")},
        {"approved_dscr": -0.01},
        {"approved_dscr": float("nan")},
    ):
        with pytest.raises(ValidationError):
            PipelineMoveRequest(target_status="approved", **invalid)
