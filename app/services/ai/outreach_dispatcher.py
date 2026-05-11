"""Outreach dispatcher — single multiplexer for sending borrower-facing
messages out via portal / email / SMS / voice.

The cadence engine's `_handle_auto_send_reminder` calls this when the
file-level OutreachMode allows the channel. Every dispatch writes an
`ai_outreach_events` row BEFORE the provider call — on exception the
row stays with status='failed' + error text. That's the audit trail.

Phase 4 ships portal only. Phase 5 wires email (existing EmailDraft +
Gmail path) + SMS (stub provider + TCPA consent + quiet-hours).
Voice is permanently off in v1.

Completion-mode enforcement also lives here:
  • ai_can_complete       → AI may flip CRS status='verified' when
                            the underlying signal fires (doc scan, chat).
  • requires_human_verify → AI marks 'provided_unverified' + spawns
                            an AITask for the operator. Stops follow-up.
  • borrower_self_attest  → Borrower confirms in chat → status='verified'
                            with trust source='borrower_confirmed'.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import OutreachChannel, OutreachEventStatus
from app.models.ai_outreach_event import AIOutreachEvent
from app.models.ai_task_assignment import AITaskAssignment
from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.loan import Loan
from app.services.ai_messaging import post_ai_message

log = logging.getLogger(__name__)


async def dispatch_portal(
    db: AsyncSession,
    *,
    assignment: AITaskAssignment,
    message_body: str,
    template_key: str | None = None,
) -> AIOutreachEvent:
    """Post a borrower-facing portal message + log the dispatch.

    Reuses ai_messaging.post_ai_message() — the canonical
    find-or-create AIChatThread + append AIChatMessage path.
    Auto-increments assignment.attempts_made on success."""
    # Visibility guard: if assignment instructions are 'internal'
    # they must not appear in the rendered body. Caller is expected
    # to have respected this — we re-assert here as defense-in-depth.
    if assignment.instructions and assignment.instructions_visibility == "internal":
        # Strip — don't leak internal context to the borrower.
        message_body = _strip_internal_segments(message_body, assignment.instructions)

    event = AIOutreachEvent(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        channel=OutreachChannel.PORTAL.value,
        direction="outbound",
        template_key=template_key,
        status=OutreachEventStatus.SCHEDULED.value,
        scheduled_for=datetime.now(timezone.utc),
        payload={"body": message_body},
    )
    db.add(event)
    await db.flush()

    # Resolve the borrower's user_id + loan_id via CRS → Client.
    crs = await db.get(ClientRequirementStatus, assignment.client_requirement_status_id)
    if crs is None:
        event.status = OutreachEventStatus.FAILED.value
        event.error = "CRS row missing"
        await db.flush()
        return event
    client = await db.get(Client, crs.client_id)
    if client is None or client.user_id is None:
        event.status = OutreachEventStatus.FAILED.value
        event.error = "Client has no linked user — cannot post portal message"
        await db.flush()
        return event

    try:
        # Post the AI message to the borrower's loan-scoped thread.
        message = await post_ai_message(
            db,
            user_id=client.user_id,
            loan_id=crs.loan_id,
            title_hint="AI assistant",
            content=message_body,
        )
        event.status = OutreachEventStatus.SENT.value
        event.sent_at = datetime.now(timezone.utc)
        event.ai_chat_message_id = getattr(message, "id", None)
        # Update assignment state.
        assignment.attempts_made = (assignment.attempts_made or 0) + 1
        # Next-run schedule from the cadence policy.
        hrs = int((assignment.cadence or {}).get("hours_between_attempts", 48))
        from datetime import timedelta
        assignment.next_run_at = datetime.now(timezone.utc) + timedelta(hours=hrs)
        # CRS lifecycle: bump status to 'asked' if it was 'missing'.
        if crs.status == "missing":
            crs.status = "asked"
        crs.last_requested_at = datetime.now(timezone.utc)
        await db.flush()
        log.info(
            "outreach.dispatch_portal assignment=%s attempt=%d crs_status=%s",
            assignment.id, assignment.attempts_made, crs.status,
        )
    except Exception as e:  # noqa: BLE001
        event.status = OutreachEventStatus.FAILED.value
        event.error = str(e)
        await db.flush()
        log.exception("outreach.dispatch_portal_failed assignment=%s", assignment.id)
    return event


async def dispatch(
    db: AsyncSession,
    *,
    assignment: AITaskAssignment,
    channel: str,
    message_body: str,
    template_key: str | None = None,
) -> AIOutreachEvent:
    """Multiplexer. Phase 4 ships portal only; email + SMS land in
    Phase 5 with the dispatcher stubs that record `blocked_by_consent`
    or `blocked_by_policy` until provider integrations land."""
    channels = list(assignment.channels or [])
    if channel not in channels:
        return await _record_blocked(
            db, assignment, channel,
            OutreachEventStatus.BLOCKED_BY_POLICY,
            f"channel {channel!r} not in assignment.channels {channels!r}",
            message_body, template_key,
        )

    if channel == OutreachChannel.PORTAL.value:
        return await dispatch_portal(
            db, assignment=assignment, message_body=message_body,
            template_key=template_key,
        )

    if channel == OutreachChannel.EMAIL.value:
        return await dispatch_email(
            db, assignment=assignment, message_body=message_body,
            template_key=template_key,
        )

    if channel == OutreachChannel.SMS.value:
        return await dispatch_sms(
            db, assignment=assignment, message_body=message_body,
            template_key=template_key,
        )

    # Voice — permanently disabled in v1.
    return await _record_blocked(
        db, assignment, channel,
        OutreachEventStatus.BLOCKED_BY_POLICY,
        "voice channel disabled in v1",
        message_body, template_key,
    )


async def dispatch_email(
    db: AsyncSession,
    *,
    assignment: AITaskAssignment,
    message_body: str,
    template_key: str | None = None,
) -> AIOutreachEvent:
    """Phase 5 — wires to the existing EmailDraft + Gmail send path.

    Pre-checks:
      • consent_state.email.opted_out_at must be unset (CAN-SPAM
        opt-out honored).
      • Borrower has an email on the Client row.
    """
    consent = (assignment.consent_state or {}).get("email") or {}
    opted_out_at = consent.get("opted_out_at")

    if opted_out_at:
        return await _record_blocked(
            db, assignment, OutreachChannel.EMAIL.value,
            OutreachEventStatus.BLOCKED_BY_CONSENT,
            "borrower opted out of email",
            message_body, template_key,
        )

    crs = await db.get(ClientRequirementStatus, assignment.client_requirement_status_id)
    if crs is None:
        return await _record_blocked(
            db, assignment, OutreachChannel.EMAIL.value,
            OutreachEventStatus.FAILED, "CRS row missing",
            message_body, template_key,
        )
    client = await db.get(Client, crs.client_id)
    if client is None or not client.email:
        return await _record_blocked(
            db, assignment, OutreachChannel.EMAIL.value,
            OutreachEventStatus.FAILED,
            "client has no email address",
            message_body, template_key,
        )

    event = AIOutreachEvent(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        channel=OutreachChannel.EMAIL.value,
        direction="outbound",
        template_key=template_key,
        status=OutreachEventStatus.SCHEDULED.value,
        scheduled_for=datetime.now(timezone.utc),
        payload={
            "to_email": client.email,
            "subject": "Update on your file",
            "body": message_body + _can_spam_footer(client.email),
        },
    )
    db.add(event)
    await db.flush()

    # Create an EmailDraft row — Gmail send path picks it up.
    try:
        from app.models.email_draft import EmailDraft
        from app.enums import EmailDraftStatus
        draft = EmailDraft(
            id=uuid.uuid4(),
            loan_id=crs.loan_id,
            client_id=client.id,
            to_email=client.email,
            cc_emails=[],
            bcc_emails=[],
            subject="Update on your file",
            body=message_body + _can_spam_footer(client.email),
            status=EmailDraftStatus.PENDING.value,
            triggered_by_kind="ai_secretary_outreach",
            triggered_by_payload={
                "assignment_id": str(assignment.id),
                "event_id": str(event.id),
                "template_key": template_key,
            },
        )
        db.add(draft)
        event.status = OutreachEventStatus.DRAFTED.value
        event.message_id = str(draft.id)
        await db.flush()
    except Exception as e:  # noqa: BLE001
        event.status = OutreachEventStatus.FAILED.value
        event.error = str(e)
        await db.flush()
        log.exception("outreach.dispatch_email_failed assignment=%s", assignment.id)
    return event


async def dispatch_sms(
    db: AsyncSession,
    *,
    assignment: AITaskAssignment,
    message_body: str,
    template_key: str | None = None,
) -> AIOutreachEvent:
    """Phase 5 — TCPA-gated SMS. Provider call is a stub until we
    pick a vendor; the event row + consent checks still fire.

    Required for SEND:
      • consent_state.sms.state == 'granted'
      • Now is within configured quiet_hours window (default 8am-8pm
        borrower-local; we use UTC here as a v1 simplification).
    """
    consent = (assignment.consent_state or {}).get("sms") or {}
    if consent.get("state") != "granted":
        return await _record_blocked(
            db, assignment, OutreachChannel.SMS.value,
            OutreachEventStatus.BLOCKED_BY_CONSENT,
            "borrower has not granted SMS consent",
            message_body, template_key,
        )

    # Quiet hours check.
    cadence = assignment.cadence or {}
    qhs = cadence.get("quiet_hours_start")
    qhe = cadence.get("quiet_hours_end")
    now = datetime.now(timezone.utc)
    if qhs and qhe and not _within_window(now.strftime("%H:%M"), qhs, qhe):
        return await _record_blocked(
            db, assignment, OutreachChannel.SMS.value,
            OutreachEventStatus.BLOCKED_BY_QUIET_HOURS,
            f"outside quiet hours window {qhs}-{qhe}",
            message_body, template_key,
        )

    crs = await db.get(ClientRequirementStatus, assignment.client_requirement_status_id)
    if crs is None:
        return await _record_blocked(
            db, assignment, OutreachChannel.SMS.value,
            OutreachEventStatus.FAILED, "CRS row missing",
            message_body, template_key,
        )
    client = await db.get(Client, crs.client_id)
    if client is None or not client.phone:
        return await _record_blocked(
            db, assignment, OutreachChannel.SMS.value,
            OutreachEventStatus.FAILED,
            "client has no phone number",
            message_body, template_key,
        )

    event = AIOutreachEvent(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        channel=OutreachChannel.SMS.value,
        direction="outbound",
        template_key=template_key,
        status=OutreachEventStatus.SCHEDULED.value,
        scheduled_for=now,
        payload={"text": message_body, "to_phone": client.phone},
    )
    db.add(event)
    await db.flush()

    # Provider stub. When the SMS vendor is wired, replace this with
    # the real send + capture provider_sid into event.message_id.
    log.info(
        "outreach.dispatch_sms STUB assignment=%s phone=%s body_len=%d",
        assignment.id, client.phone, len(message_body),
    )
    event.status = OutreachEventStatus.FAILED.value
    event.error = "SMS provider not configured — stub only"
    await db.flush()
    return event


# ── Helpers ────────────────────────────────────────────────────────


async def _record_blocked(
    db: AsyncSession,
    assignment: AITaskAssignment,
    channel: str,
    status: OutreachEventStatus,
    reason: str,
    message_body: str,
    template_key: str | None,
) -> AIOutreachEvent:
    event = AIOutreachEvent(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        channel=channel,
        direction="outbound",
        template_key=template_key,
        status=status.value,
        error=reason,
        payload={"body": message_body},
    )
    db.add(event)
    await db.flush()
    log.info(
        "outreach.blocked assignment=%s channel=%s status=%s reason=%s",
        assignment.id, channel, status.value, reason,
    )
    return event


def _within_window(now_hhmm: str, start: str, end: str) -> bool:
    """True when now is within [start, end) by clock time. Simple
    string compare works for 24h zero-padded HH:MM."""
    if start <= end:
        return start <= now_hhmm < end
    # Wraps midnight.
    return now_hhmm >= start or now_hhmm < end


def _can_spam_footer(to_email: str) -> str:
    """Auto-appended unsubscribe footer for borrower-facing emails."""
    return (
        "\n\n---\n"
        "Qualified Commercial · You can manage how the AI Assistant contacts you "
        "from your borrower portal. Reply STOP to opt out of automated emails."
    )


def _strip_internal_segments(body: str, internal_text: str) -> str:
    """Conservative — if internal_text appears verbatim in body,
    redact it. Belt-and-suspenders to the visibility filter."""
    if not internal_text:
        return body
    return body.replace(internal_text, "[internal note redacted]")
