from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.enums import Role
from app.routers import dealer_ai_intake as router


def _candidate(*, role: Role = Role.SUPER_ADMIN, relation: str = "Super Admin"):
    user_id = uuid4()
    return router.IntakeNotificationUserRead(
        user_id=user_id,
        name="Morgan Desk",
        email="morgan@example.com",
        role=role.value,
        relations=[relation],
    )


def _intake(*, state=None):
    return SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        intake_state=state,
    )


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


def _locking_db(intake):
    return SimpleNamespace(
        execute=AsyncMock(return_value=_ScalarResult(intake)),
        refresh=AsyncMock(),
        commit=AsyncMock(),
    )


def test_default_routing_preserves_legacy_super_admin_delivery() -> None:
    admin = _candidate()
    routing = router._notification_routing_read(_intake(), [admin])
    rules = {event.key: event.rule for event in routing.events}

    assert routing.uses_default is True
    assert rules["intake_started"].enabled is True
    assert rules["intake_started"].to_user_ids == [admin.user_id]
    assert rules["review_approved"].to_user_ids == [admin.user_id]
    assert rules["review_denied"].to_user_ids == [admin.user_id]
    assert rules["file_uploaded"].enabled is False


def test_stale_or_deactivated_recipient_is_hidden_and_route_turns_off() -> None:
    stale_id = uuid4()
    state = {
        router.INTAKE_NOTIFICATION_ROUTING_STATE_KEY: {
            "version": router.INTAKE_NOTIFICATION_ROUTING_VERSION,
            "events": {
                key: {
                    "enabled": key == "intake_started",
                    "email_enabled": True,
                    "in_app_enabled": False,
                    "to_user_ids": [str(stale_id)] if key == "intake_started" else [],
                    "cc_user_ids": [],
                }
                for key in router.INTAKE_NOTIFICATION_EVENT_DEFINITIONS
            },
        }
    }

    routing = router._notification_routing_read(_intake(state=state), [])
    started = next(event.rule for event in routing.events if event.key == "intake_started")

    assert routing.uses_default is False
    assert started.to_user_ids == []
    assert started.cc_user_ids == []
    assert started.enabled is False


def test_enabled_rule_requires_a_channel_and_cc_requires_email() -> None:
    user_id = uuid4()
    with pytest.raises(ValidationError, match="requires Email or In-app"):
        router.IntakeNotificationRule(
            enabled=True,
            email_enabled=False,
            in_app_enabled=False,
            to_user_ids=[user_id],
        )
    with pytest.raises(ValidationError, match="CC recipients require email"):
        router.IntakeNotificationRule(
            enabled=True,
            email_enabled=False,
            in_app_enabled=True,
            to_user_ids=[user_id],
            cc_user_ids=[uuid4()],
        )
    with pytest.raises(ValidationError, match="both a primary recipient and CC"):
        router.IntakeNotificationRule(
            enabled=True,
            email_enabled=True,
            in_app_enabled=True,
            to_user_ids=[user_id],
            cc_user_ids=[user_id],
        )


async def test_dispatch_honors_named_to_cc_and_in_app_channels() -> None:
    lead = _candidate(role=Role.FIELD_REP, relation="Originating agent")
    broker = _candidate(role=Role.BROKER, relation="Broker / dealer partner")
    events = {
        key: {
            "enabled": key == "intake_started",
            "email_enabled": key == "intake_started",
            "in_app_enabled": key == "intake_started",
            "to_user_ids": [str(lead.user_id)] if key == "intake_started" else [],
            "cc_user_ids": [str(broker.user_id)] if key == "intake_started" else [],
        }
        for key in router.INTAKE_NOTIFICATION_EVENT_DEFINITIONS
    }
    intake = _intake(
        state={
            router.INTAKE_NOTIFICATION_ROUTING_STATE_KEY: {
                "version": router.INTAKE_NOTIFICATION_ROUTING_VERSION,
                "events": events,
            }
        }
    )
    db = SimpleNamespace()
    notification_rows = [
        SimpleNamespace(id=uuid4(), recipient_user_id=lead.user_id),
        SimpleNamespace(id=uuid4(), recipient_user_id=broker.user_id),
    ]
    email_result = SimpleNamespace(ok=True, detail="sent", message_id="ses-123")

    with (
        patch.object(
            router,
            "_intake_notification_candidates",
            AsyncMock(return_value=[lead, broker]),
        ),
        patch.object(
            router.notifications,
            "notify_users",
            AsyncMock(return_value=notification_rows),
        ) as notify,
        patch.object(router, "send_raw_email", return_value=email_result) as send,
    ):
        records = await router._dispatch_intake_notification(
            db,
            intake,
            event_type="intake_started",
            subject="Intake started",
            body_text="Intake started\nDetails",
            body_html="<p>Intake started</p>",
        )

    assert notify.await_args.kwargs["recipient_ids"] == {lead.user_id}
    assert notify.await_args.kwargs["email"] is False
    assert notify.await_args.kwargs["push"] is False
    assert send.call_args.kwargs["to_emails"] == [lead.email]
    assert send.call_args.kwargs["cc_emails"] == [broker.email]
    assert {(row["recipient"], row["recipient_type"]) for row in records} == {
        (lead.email, "to"),
        (broker.email, "cc"),
    }
    assert all(row["email_ok"] is True for row in records)


async def test_delivery_claim_is_permanent_and_prevents_duplicate_send() -> None:
    intake = _intake(state={})
    db = _locking_db(intake)
    deliveries = [{"recipient": "morgan@example.com", "email_ok": True}]

    with patch.object(
        router,
        "_dispatch_intake_notification",
        AsyncMock(return_value=deliveries),
    ) as dispatch:
        first = await router._record_intake_notification_once(
            db,
            intake,
            event_type="file_uploaded",
            event_key="file_uploaded:one",
            subject="New file",
            body_text="New file",
            body_html="<p>New file</p>",
            request=None,
        )
        second = await router._record_intake_notification_once(
            db,
            intake,
            event_type="file_uploaded",
            event_key="file_uploaded:one",
            subject="New file",
            body_text="New file",
            body_html="<p>New file</p>",
            request=None,
        )

    assert first == second
    dispatch.assert_awaited_once()
    assert db.execute.await_count == 3  # claim, completion, replay lookup
    assert db.refresh.await_count == 3
    assert db.commit.await_count == 2
    assert "file_uploaded:one" in intake.intake_state[
        router.INTAKE_NOTIFICATION_AUDIT_STATE_KEY
    ]


async def test_completion_reloads_state_and_preserves_concurrent_routing_edit() -> None:
    intake = _intake(state={})
    db = _locking_db(intake)

    async def dispatch(*_args, **_kwargs):
        # Simulate a routing edit committed while SES is in flight.  The
        # completion path must merge with, rather than overwrite, this state.
        state = dict(intake.intake_state)
        state[router.INTAKE_NOTIFICATION_ROUTING_STATE_KEY] = {
            "version": router.INTAKE_NOTIFICATION_ROUTING_VERSION,
            "updated_by_user_id": str(uuid4()),
            "events": {},
        }
        intake.intake_state = state
        return [{"status": "sent"}]

    with patch.object(router, "_dispatch_intake_notification", dispatch):
        await router._record_intake_notification_once(
            db,
            intake,
            event_type="review_approved",
            event_key="review_approved",
            subject="Review complete",
            body_text="Review complete",
            body_html="<p>Review complete</p>",
            request=None,
        )

    assert router.INTAKE_NOTIFICATION_ROUTING_STATE_KEY in intake.intake_state
    assert intake.intake_state[router.INTAKE_NOTIFICATION_AUDIT_STATE_KEY][
        "review_approved"
    ]["status"] == "completed"
    # One row lock/refresh protects the claim and another protects completion.
    assert db.execute.await_count == 2
    assert db.refresh.await_count == 2


async def test_update_rejects_recipient_no_longer_involved_under_lock() -> None:
    intake = _intake(state={})
    db = _locking_db(intake)
    admin = _candidate()
    outsider_id = uuid4()
    payload = router.IntakeNotificationRoutingUpdate(
        events={
            key: router.IntakeNotificationRule(
                enabled=key == "intake_started",
                email_enabled=True,
                in_app_enabled=False,
                to_user_ids=[outsider_id] if key == "intake_started" else [],
                cc_user_ids=[],
            )
            for key in router.INTAKE_NOTIFICATION_EVENT_DEFINITIONS
        }
    )
    user = SimpleNamespace(id=admin.user_id, role=Role.SUPER_ADMIN)

    with (
        patch.object(router, "_load_admin_dealer_lead", AsyncMock(return_value=intake)),
        patch.object(router, "_intake_notification_candidates", AsyncMock(return_value=[admin])),
    ):
        with pytest.raises(HTTPException) as caught:
            await router.update_intake_notification_routing(
                intake.id,
                payload,
                SimpleNamespace(),
                user,
                db,
            )

    assert caught.value.status_code == 422
    assert "no longer involved" in caught.value.detail
    db.execute.assert_awaited_once()
    db.refresh.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_notification_claim_ledger_is_not_trimmed() -> None:
    source = inspect.getsource(router._record_intake_notification_once)
    assert "len(audit)" not in source
    assert "with_for_update()" in source
    assert source.count('attribute_names=["intake_state"]') == 2


def test_upload_completion_wires_the_file_notification() -> None:
    source = inspect.getsource(router._complete_upload)
    assert "_record_file_uploaded_notification(" in source
    assert source.index("await db.commit()") < source.index(
        "_record_file_uploaded_notification("
    )
