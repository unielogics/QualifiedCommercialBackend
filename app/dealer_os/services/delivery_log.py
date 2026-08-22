"""What was asked of the applicant, and what came back.

Step 2 of the application shows a table: request, channel, recipient, status,
timestamp. Every one of those facts is already recorded — `log_action` writes a
row whenever a link is sent, opened or completed — but as loose audit entries
with different action names and different payload shapes. Reading them requires
knowing which of five action strings mean "we asked for something".

So this is a projection, not new storage. Nothing here writes; it reads the
trail and gives it one shape. Two consequences worth keeping in mind: the log
can only ever be as good as what `log_action` recorded, and adding a new kind of
client request means adding it to `_REQUESTS` here or it will simply not appear.

**Status is derived, not stored.** A request is Sent when it went out, Opened
when the client loaded the page, and Completed when the thing was actually
done. Those three facts arrive as separate events at separate times, so the row
a rep reads is the newest state of a request rather than one event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerAuditLog

__all__ = ["DeliveryRow", "build"]

# action -> (label, kind). Only these appear in the delivery log; everything
# else in the audit trail is desk activity, not something asked of the client.
_REQUESTS: dict[str, tuple[str, str]] = {
    "client_request.bank_connect": ("Bank connection", "bank"),
    "client_request.bank_upload": ("Statement upload", "bank"),
    "client_request.document": ("Document request", "document"),
    "client_request.signature": ("Signature request", "signature"),
    "owner.credit_invite": ("Credit authorization", "credit"),
}

# Events that advance a request already in the log, rather than starting one.
_COMPLETIONS: dict[str, tuple[str, str]] = {
    "owner.soft_pull": ("credit", "Completed"),
    "plaid.connect": ("bank", "Completed"),
    "plaid.connect.client": ("bank", "Completed"),
}


@dataclass
class DeliveryRow:
    kind: str
    request: str
    channel: str
    recipient: str
    status: str
    """Sent · Opened · Completed · Failed."""
    at: datetime
    detail: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)


def _channel_of(after: dict[str, Any] | None) -> str:
    """Read the channel back out of what the delivery seam recorded.

    `deliver_link` reports email and sms independently because "the text failed
    but the email went" is a different situation from "nothing went". That
    honesty has to survive into the table, so both are named when both fired.
    """
    a = after or {}
    if a.get("email") and a.get("sms"):
        return "Email + SMS"
    if a.get("email"):
        return "Email"
    if a.get("sms"):
        return "SMS"
    ch = a.get("channel")
    if isinstance(ch, str) and ch not in ("none", ""):
        return ch.upper() if ch == "sms" else ch.capitalize()
    return "Not sent"


def _status_of(after: dict[str, Any] | None) -> str:
    a = after or {}
    if a.get("delivered") is False and not (a.get("email") or a.get("sms")):
        return "Failed"
    return "Sent"


async def build(db: AsyncSession, dealer_id, limit: int = 100) -> list[DeliveryRow]:
    """Newest first, one row per request, with its own history attached."""
    rows = (
        (
            await db.execute(
                select(DealerAuditLog)
                .where(DealerAuditLog.dealer_id == dealer_id)
                .order_by(DealerAuditLog.created_at.asc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )

    out: list[DeliveryRow] = []
    # Newest open request per kind, so a completion attaches to the send it
    # actually followed rather than to the first one ever made.
    latest: dict[str, DeliveryRow] = {}

    for r in rows:
        if r.action in _REQUESTS:
            label, kind = _REQUESTS[r.action]
            after = r.after if isinstance(r.after, dict) else {}
            row = DeliveryRow(
                kind=kind,
                request=label,
                channel=_channel_of(after),
                recipient=str(after.get("recipient") or "") or "—",
                status=_status_of(after),
                at=r.created_at,
                detail=str(after.get("purpose") or ""),
                history=[{"at": r.created_at.isoformat(), "event": "Sent", "by": r.actor_name}],
            )
            out.append(row)
            latest[kind] = row
            continue

        if r.action in _COMPLETIONS:
            kind, status = _COMPLETIONS[r.action]
            target = latest.get(kind)
            if target is None:
                # Completed without a recorded send: the client connected from
                # a link issued before this log existed, or the desk did it by
                # hand. Show it rather than dropping it.
                target = DeliveryRow(
                    kind=kind,
                    request=_REQUESTS.get(
                        f"client_request.{kind}", ("Bank connection", kind)
                    )[0]
                    if kind == "bank"
                    else "Credit authorization",
                    channel="—",
                    recipient="—",
                    status=status,
                    at=r.created_at,
                    detail="No send recorded",
                )
                out.append(target)
                latest[kind] = target
            else:
                target.status = status
                target.at = r.created_at
            target.history.append(
                {"at": r.created_at.isoformat(), "event": status, "by": r.actor_name}
            )

    out.sort(key=lambda x: x.at, reverse=True)
    return out[:limit]
