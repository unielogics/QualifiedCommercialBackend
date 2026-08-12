"""Client/broker activity intelligence for super admins.

Two consumers over the same event source (BucketActivityLog):

1. `run_admin_activity_digest` — scheduler job (every 5 min). Emails the
   super admin(s) one coalesced digest of everything clients and brokers did
   since the last send: uploads, chat messages, form submissions, new
   intakes, bookings, deletion requests. The cursor advances only after a
   successful SES send, so while SES is unprovisioned nothing is lost —
   the moment it's configured, the backlog (capped by max_lookback_hours)
   flows in the next tick.

2. `client_activity_rows` — the what's-new feed / unseen-count reads used by
   the admin leads UI.

Configured via AppSettings.data["admin_notifications"]
(AdminNotificationSettings): enabled flag, explicit recipient list
(default: every super admin), lookback cap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.enums import Role
from app.models.admin_activity import AdminDigestState
from app.models.bucket import Bucket, BucketActivityLog
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User

log = logging.getLogger(__name__)

# Actor roles that represent the CLIENT side of the platform (borrowers in
# public rooms, authenticated clients, share/vendor recipients). Broker
# (dealer-partner) actions sometimes log under internal roles — e.g. their AI
# chat logs actor_role="admin" — so the query below ALSO matches on the actor
# user's account role.
CLIENT_ACTOR_ROLES = {
    "public_lead",
    "uploader",
    "client",
    "dealer_partner",
    "broker",
    "vendor",
    "shared_user",
    "public_share_recipient",
}

# Pure-noise actions that never belong in a "what happened" digest: begun-but-
# not-finished uploads, room opens, failed passcodes, plumbing events.
EXCLUDED_ACTION_SUFFIXES = ("_started", "_accessed", "_opened", "_requested_link")
EXCLUDED_ACTIONS = {
    # System-ish events that ride along with every upload/chat turn — the
    # human action is already in the digest, these would just double it.
    "dealer_ai_review_queued",
    "ai_action_proposed",
    "share_passcode_failed",
    "dealer_ai_login_code_sent",
    "dealer_ai_login_code_verified",
    "shared_file_download_requested",
    "dealer_ai_resume_email_sent",
}

ACTION_LABELS = {
    "dealer_ai_file_uploaded": "Uploaded a document",
    "file_uploaded": "Uploaded a document",
    "dealer_ai_zip_extracted": "Uploaded a ZIP (extracted)",
    "ai_chat_message_created": "Sent an AI chat message",
    "dealer_ai_intake_created": "Started a new dealer intake",
    "funding_review_intake_created": "Started a real-estate funding review",
    "dealer_ai_lead_created_by_broker": "Broker created a lead",
    "dealer_ai_drafted_form_submitted": "Filled out a financial form",
    "dealer_ai_call_booked": "Booked a call",
    "dealer_ai_lead_deletion_requested_by_broker": "Broker requested lead deletion",
    "dealer_ai_lead_deletion_request_cancelled_by_broker": "Broker cancelled a deletion request",
    "note_created": "Left a note",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action.replace("_", " ").capitalize())


def _activity_filters():
    """Shared WHERE fragment: client/broker-side, meaningful actions only."""
    conditions = [
        or_(
            BucketActivityLog.actor_role.in_(CLIENT_ACTOR_ROLES),
            User.role == Role.DEALER_PARTNER,
        ),
        BucketActivityLog.action.notin_(EXCLUDED_ACTIONS),
    ]
    for suffix in EXCLUDED_ACTION_SUFFIXES:
        conditions.append(BucketActivityLog.action.notlike(f"%{suffix}"))
    return conditions


async def client_activity_rows(
    db: AsyncSession,
    *,
    since: datetime,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Client/broker activity newest-first, joined to the owning lead."""
    rows = (
        await db.execute(
            select(BucketActivityLog, PublicUnderwritingIntake, Bucket)
            .join(Bucket, Bucket.id == BucketActivityLog.bucket_id)
            .outerjoin(User, User.id == BucketActivityLog.actor_user_id)
            .outerjoin(PublicUnderwritingIntake, PublicUnderwritingIntake.bucket_id == BucketActivityLog.bucket_id)
            .where(BucketActivityLog.created_at > since, *_activity_filters())
            .order_by(BucketActivityLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    items: list[dict[str, Any]] = []
    for event, intake, bucket in rows:
        items.append(
            {
                "event_id": str(event.id),
                "intake_id": str(intake.id) if intake else None,
                "bucket_id": str(event.bucket_id),
                "lead_name": (intake.business_name or intake.full_name) if intake else (bucket.client_name or bucket.name),
                "variant": intake.variant if intake else None,
                "action": event.action,
                "label": action_label(event.action),
                "actor_name": event.actor_name,
                "actor_role": event.actor_role,
                "detail": event.detail,
                "created_at": event.created_at,
            }
        )
    return items


async def unseen_counts_by_bucket(
    db: AsyncSession,
    *,
    seen_by_intake: dict[UUID, datetime],
    bucket_to_intake: dict[UUID, UUID],
    default_since: datetime,
) -> dict[UUID, int]:
    """Unseen client/broker events per bucket for one admin. A lead the admin
    never opened counts everything within the default window as unseen."""
    if not bucket_to_intake:
        return {}
    floor = min([default_since, *seen_by_intake.values()]) if seen_by_intake else default_since
    rows = (
        await db.execute(
            select(BucketActivityLog.bucket_id, BucketActivityLog.created_at)
            .outerjoin(User, User.id == BucketActivityLog.actor_user_id)
            .where(
                BucketActivityLog.bucket_id.in_(list(bucket_to_intake.keys())),
                BucketActivityLog.created_at > floor,
                *_activity_filters(),
            )
        )
    ).all()
    counts: dict[UUID, int] = {}
    for bucket_id, created_at in rows:
        intake_id = bucket_to_intake.get(bucket_id)
        threshold = seen_by_intake.get(intake_id, default_since) if intake_id else default_since
        if created_at > threshold:
            counts[bucket_id] = counts.get(bucket_id, 0) + 1
    return counts


# --------------------------------------------------------------------------
# Email digest
# --------------------------------------------------------------------------

def _digest_settings(raw: Any) -> dict[str, Any]:
    from app.schemas.settings import AdminNotificationSettings

    try:
        parsed = AdminNotificationSettings.model_validate(raw) if isinstance(raw, dict) else AdminNotificationSettings()
    except Exception:  # noqa: BLE001 — malformed settings must never kill the job
        parsed = AdminNotificationSettings()
    return parsed.model_dump()


def _format_digest(items: list[dict[str, Any]], *, public_url: str) -> tuple[str, str, str]:
    """(subject, body_text, body_html) — one section per lead, newest lead first."""
    by_lead: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        key = item["lead_name"] or "Unassigned room"
        if key not in by_lead:
            by_lead[key] = []
            order.append(key)
        by_lead[key].append(item)

    upload_count = sum(1 for item in items if "upload" in item["action"])
    message_count = sum(1 for item in items if "chat" in item["action"])
    pieces = []
    if upload_count:
        pieces.append(f"{upload_count} upload{'s' if upload_count != 1 else ''}")
    if message_count:
        pieces.append(f"{message_count} message{'s' if message_count != 1 else ''}")
    other = len(items) - upload_count - message_count
    if other:
        pieces.append(f"{other} other update{'s' if other != 1 else ''}")
    subject = f"QC activity: {', '.join(pieces)} across {len(order)} lead{'s' if len(order) != 1 else ''}"

    text_lines: list[str] = ["Client & broker activity on Qualified Commercial:", ""]
    html_sections: list[str] = []
    for lead_name in order:
        events = by_lead[lead_name]
        first = events[0]
        link = f"{public_url}/admin/ai-underwriter-leads?lead={first['intake_id']}" if first.get("intake_id") else f"{public_url}/admin/buckets"
        text_lines.append(f"— {lead_name} ({len(events)} update{'s' if len(events) != 1 else ''})")
        rows_html = []
        for event in sorted(events, key=lambda item: item["created_at"]):
            stamp = event["created_at"].astimezone(timezone.utc).strftime("%I:%M %p UTC")
            actor = event["actor_name"] or (event["actor_role"] or "client").replace("_", " ")
            detail = (event["detail"] or "").strip()
            detail_part = f" — {detail[:140]}" if detail else ""
            text_lines.append(f"    {stamp} · {actor}: {event['label']}{detail_part}")
            rows_html.append(
                f'<tr><td style="color:#64748b;white-space:nowrap;padding:4px 10px 4px 0;">{escape(stamp)}</td>'
                f'<td style="padding:4px 0;"><strong>{escape(actor)}</strong> — {escape(event["label"])}'
                f'{f"<span style=\"color:#475569\"> · {escape(detail[:140])}</span>" if detail else ""}</td></tr>'
            )
        text_lines.append(f"    Open: {link}")
        text_lines.append("")
        html_sections.append(
            f'<div style="border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;margin-bottom:12px;">'
            f'<div style="font-weight:700;color:#0f172a;margin-bottom:6px;">{escape(lead_name)}'
            f'<span style="color:#64748b;font-weight:400;"> — {len(events)} update{"s" if len(events) != 1 else ""}</span></div>'
            f'<table style="border-collapse:collapse;font-size:13px;color:#0f172a;">{"".join(rows_html)}</table>'
            f'<div style="margin-top:8px;"><a href="{escape(link)}" style="color:#0F766E;font-weight:700;">Open this lead →</a></div>'
            f"</div>"
        )

    body_text = "\n".join(text_lines)
    body_html = (
        '<div style="font-family:Helvetica,Arial,sans-serif;max-width:640px;">'
        '<div style="color:#0F766E;font-weight:800;letter-spacing:.14em;font-size:11px;">QUALIFIED COMMERCIAL</div>'
        '<h2 style="margin:4px 0 14px;color:#0f172a;">Client & broker activity</h2>'
        + "".join(html_sections)
        + '<p style="color:#94a3b8;font-size:11px;">You receive this because admin activity notifications are enabled. '
        "Configure in Lending AI settings.</p></div>"
    )
    return subject, body_text, body_html


async def run_admin_activity_digest() -> None:
    """Scheduler entry point — see module docstring."""
    from app.models.app_settings import AppSettings
    from app.routers.buckets import _public_url
    from app.services.email.ses_client import send_email, ses_configured

    async with SessionLocal() as db:
        settings_row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
        config = _digest_settings((settings_row.data or {}).get("admin_notifications") if settings_row else None)
        if not config.get("enabled", True):
            return

        state = (await db.execute(select(AdminDigestState).limit(1))).scalar_one_or_none()
        if state is None:
            state = AdminDigestState(last_event_at=None, last_sent_at=None)
            db.add(state)
            await db.flush()

        now = datetime.now(timezone.utc)
        lookback_floor = now - timedelta(hours=int(config.get("max_lookback_hours") or 24))
        since = max(state.last_event_at, lookback_floor) if state.last_event_at else lookback_floor

        items = await client_activity_rows(db, since=since, limit=200)
        if not items:
            return
        if not ses_configured():
            log.info("admin_activity_digest: %d event(s) pending, SES not configured — will retry", len(items))
            return

        recipients = [addr for addr in (config.get("recipients") or []) if addr and "@" in addr]
        if not recipients:
            recipients = [
                user.email
                for user in (
                    await db.execute(select(User).where(User.role == Role.SUPER_ADMIN, User.email.isnot(None)))
                ).scalars().all()
                if user.email
            ]
        if not recipients:
            log.warning("admin_activity_digest: no recipients resolved; skipping")
            return

        public_url = _public_url("").rstrip("/")
        subject, body_text, body_html = _format_digest(list(reversed(items)), public_url=public_url)

        delivered = False
        for recipient in recipients:
            result = send_email(to_email=recipient, subject=subject, body_text=body_text, body_html=body_html)
            if result.ok:
                delivered = True
            else:
                log.warning("admin_activity_digest: send to %s failed: %s", recipient, result.detail)

        if delivered:
            state.last_event_at = max(item["created_at"] for item in items)
            state.last_sent_at = now
            await db.commit()
        else:
            await db.rollback()
