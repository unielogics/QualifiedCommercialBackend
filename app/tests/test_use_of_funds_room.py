from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import application_profiles as rooms
from app.schemas.use_of_funds import UseOfFundsPatch, UseOfFundsRoomAccess, UseOfFundsRoomPatch
from app.services import use_of_funds


def saved_profile(**values):
    return SimpleNamespace(**{
        "id": uuid4(), "primary_bucket_id": uuid4(), "dealer_id": None,
        "use_of_funds": [], "use_of_funds_revision": 0,
        "use_of_funds_updated_at": None, "use_of_funds_updated_by_user_id": None,
        **values,
    })


def room_link(profile, **values):
    return SimpleNamespace(**{
        "id": uuid4(), "bucket_id": profile.primary_bucket_id, "status": "active",
        "expires_at": None, "passcode_hash": "hashed", **values,
    })


def rows():
    return [
        {"id": "equipment", "category": "equipment", "label": "Equipment", "amount": "510.00"},
        {"id": "working", "category": "working_capital", "label": "Operating costs", "amount": "490.00"},
    ]


def request():
    return SimpleNamespace(client=SimpleNamespace(host="198.51.100.8"), headers={})


@pytest.mark.parametrize("extra", [
    {"profile_id": str(uuid4())}, {"updated_by_user_id": str(uuid4())},
    {"requested_amount": 1000}, {"real_estate_equipment_pct": 100},
])
def test_public_budget_cannot_choose_another_file_actor_or_routing_facts(extra):
    with pytest.raises(ValidationError):
        UseOfFundsRoomPatch(passcode="123456", items=rows(), expected_revision=0, **extra)
    with pytest.raises(ValidationError):
        UseOfFundsRoomAccess(passcode="123456", **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("dealer", [False, True])
async def test_public_budget_read_is_token_bound_and_redacts_staff_identity(dealer):
    profile = saved_profile(dealer_id=uuid4() if dealer else None, use_of_funds_updated_by_user_id=uuid4())
    link = room_link(profile)
    budget = use_of_funds.summarize(profile, Decimal(1000), "intake.requested_loan_amount")
    db = SimpleNamespace()
    payload = UseOfFundsRoomAccess(passcode="123456")
    req = request()
    with (
        patch.object(rooms, "_public_application_room", AsyncMock(return_value=(link, profile))) as authorize,
        patch.object(use_of_funds, "read_budget", AsyncMock(return_value=budget)) as read,
    ):
        result = await rooms.public_application_room_use_of_funds("bound-token", payload, req, db)
    authorize.assert_awaited_once_with(db, "bound-token", "123456", req, allow_dealer=True)
    read.assert_awaited_once_with(db, profile)
    assert result.can_edit is True
    assert result.updated_by_user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_unauthorized_public_reads_and_writes_do_not_touch_budget(status):
    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch.object(rooms, "_public_application_room", AsyncMock(side_effect=HTTPException(status, "Invalid room"))),
        patch.object(use_of_funds, "read_budget", AsyncMock()) as read,
        patch.object(use_of_funds, "update_client_budget", AsyncMock()) as update,
    ):
        with pytest.raises(HTTPException) as error:
            await rooms.public_application_room_use_of_funds("bad", UseOfFundsRoomAccess(passcode="123456"), request(), db)
        assert error.value.status_code == status
        with pytest.raises(HTTPException) as error:
            await rooms.public_application_room_update_use_of_funds("bad", UseOfFundsRoomPatch(passcode="123456", items=rows(), expected_revision=0), request(), db)
        assert error.value.status_code == status
    read.assert_not_awaited()
    update.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_save_removes_pin_before_persistence_and_commits_once():
    profile = saved_profile()
    link = room_link(profile)
    db = SimpleNamespace(commit=AsyncMock())
    budget = use_of_funds.summarize(profile, Decimal(1000), "loan.amount")
    payload = UseOfFundsRoomPatch(passcode="123456", items=rows(), expected_revision=0)
    with (
        patch.object(rooms, "_public_application_room", AsyncMock(return_value=(link, profile))),
        patch.object(use_of_funds, "update_client_budget", AsyncMock(return_value=budget)) as update,
    ):
        await rooms.public_application_room_update_use_of_funds("bound", payload, request(), db)
    passed_payload = update.await_args.args[2]
    assert isinstance(passed_payload, UseOfFundsPatch)
    assert "passcode" not in passed_payload.model_dump()
    assert update.await_args.kwargs == {"room_link": link}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"status": "revoked"}, {"bucket_id": uuid4()},
    {"expires_at": datetime.now(UTC) - timedelta(minutes=1)},
])
async def test_client_write_rechecks_room_binding_status_and_expiry(changes):
    profile = saved_profile()
    db = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await use_of_funds.update_client_budget(
            db, profile, UseOfFundsPatch(items=rows(), expected_revision=0),
            room_link=room_link(profile, **changes),
        )
    assert error.value.status_code == 404
    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_save_uses_staff_lock_revision_and_safe_audit_then_source_change_recomputes():
    profile = saved_profile()
    link = room_link(profile)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock())
    payload = UseOfFundsPatch(items=rows(), expected_revision=0)
    with (
        patch.object(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal(1000), "loan.amount", None, None))),
        patch("app.services.application_profiles.log_profile_action", AsyncMock()) as audit,
    ):
        result = await use_of_funds.update_client_budget(db, profile, payload, room_link=link)
        assert result.revision == 1
        assert result.complete and result.real_estate_equipment_pct == 51
        with pytest.raises(HTTPException) as error:
            await use_of_funds.update_client_budget(db, profile, payload, room_link=link)
        assert error.value.status_code == 409
    query = db.execute.await_args.args[0]
    assert query._for_update_arg is not None
    assert query.get_execution_options()["populate_existing"] is True
    assert profile.use_of_funds_updated_by_user_id is None
    assert audit.await_args.args[2] is None
    assert audit.await_args.args[3] == "use_of_funds.update.application_room"
    assert audit.await_args.kwargs["target_id"] == link.id
    assert audit.await_args.kwargs["metadata"]["actor_source"] == "secure_room_client"
    assert "123456" not in str(audit.await_args)
    changed = use_of_funds.summarize(profile, Decimal(2000), "loan.amount")
    assert not changed.complete
    assert changed.real_estate_equipment_pct is None


@pytest.mark.asyncio
async def test_client_partial_budget_is_saved_but_overallocation_is_rejected():
    profile = saved_profile()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one=lambda: profile)), flush=AsyncMock())
    with (
        patch.object(use_of_funds, "source_funding_data", AsyncMock(return_value=(Decimal(1000), "loan.amount", None, None))),
        patch("app.services.application_profiles.log_profile_action", AsyncMock()),
    ):
        result = await use_of_funds.update_client_budget(db, profile, UseOfFundsPatch(items=rows()[:1], expected_revision=0), room_link=room_link(profile))
        assert not result.complete and result.real_estate_equipment_pct is None
        assert result.total == 510
        with pytest.raises(HTTPException) as error:
            await use_of_funds.update_client_budget(db, profile, UseOfFundsPatch(items=[{**rows()[0], "amount": "1000.01"}], expected_revision=1), room_link=room_link(profile))
        assert error.value.status_code == 422
    assert profile.use_of_funds_revision == 1


@pytest.mark.asyncio
async def test_room_expiry_rejected_before_pin_and_dealer_opt_in_is_local():
    profile = saved_profile(dealer_id=uuid4())
    link = room_link(profile, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: link)))
    with patch.object(rooms, "_verify_passcode", return_value=True) as verify:
        with pytest.raises(HTTPException) as error:
            await rooms._public_application_room(db, "token", "123456", request(), allow_dealer=True)
        assert error.value.status_code == 404
        verify.assert_not_called()
    link.expires_at = None
    with patch.object(rooms, "_verify_passcode", return_value=True):
        db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda: link), SimpleNamespace(scalar_one_or_none=lambda: profile)]
        with pytest.raises(HTTPException) as error:
            await rooms._public_application_room(db, "token", "123456", request())
        assert error.value.status_code == 404
        db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda: link), SimpleNamespace(scalar_one_or_none=lambda: profile)]
        assert await rooms._public_application_room(db, "token", "123456", request(), allow_dealer=True) == (link, profile)


@pytest.mark.asyncio
async def test_invalid_pin_stops_before_loading_any_funding_profile():
    profile = saved_profile()
    link = room_link(profile)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: link)))
    with patch.object(rooms, "_verify_passcode", return_value=False) as verify:
        with pytest.raises(HTTPException) as error:
            await rooms._public_application_room(db, "token", "000000", request(), allow_dealer=True)
    assert error.value.status_code == 403
    assert db.execute.await_count == 1
    assert verify.call_args.args == ("000000", "hashed")
    assert verify.call_args.kwargs["attempt_scope"]
