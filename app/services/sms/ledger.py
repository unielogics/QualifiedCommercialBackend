"""Writing the SMS ledger.

`record` never raises to its caller: a ledger write must not break a send that
already happened, and a send must not be reported failed because bookkeeping
hiccuped. Failures log loudly instead — a quiet ledger is a lying ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sms_message import SmsMessage

log = logging.getLogger(__name__)

__all__ = ["record", "mark_delivery", "client_for_phone"]


async def record(
    db: AsyncSession,
    *,
    direction: str,
    phone_e164: str,
    status: str,
    body: str | None = None,
    provider: str = "",
    provider_message_id: str = "",
    detail: str = "",
    context: str = "",
    client_id=None,
) -> SmsMessage | None:
    try:
        row = SmsMessage(
            direction=direction,
            phone_e164=phone_e164,
            body=body,
            provider=provider,
            provider_message_id=provider_message_id[:64],
            status=status,
            detail=detail[:300],
            context=context[:32],
            client_id=client_id,
        )
        db.add(row)
        await db.flush()
        return row
    except Exception:  # noqa: BLE001
        log.exception(
            "sms ledger write failed dir=%s phone=%s status=%s — send outcome unaffected",
            direction, phone_e164, status,
        )
        return None


async def mark_delivery(
    db: AsyncSession, *, provider_message_id: str, status: str
) -> bool:
    """Advance an outbound row on a carrier state event (sent/delivered).

    Matched on the provider's id because that is the only name both sides
    share. Delivery events can arrive out of order; "delivered" never regresses
    to "sent".
    """
    if not provider_message_id:
        return False
    row = (
        await db.execute(
            select(SmsMessage).where(
                SmsMessage.provider_message_id == provider_message_id,
                SmsMessage.direction == "outbound",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        log.info("sms ledger: delivery event for unknown id %s", provider_message_id)
        return False
    if status == "delivered":
        row.status = "delivered"
        row.delivered_at = datetime.now(timezone.utc)
    elif status == "sent" and row.status not in ("delivered",):
        row.status = "sent"
    await db.flush()
    return True


async def client_for_phone(db: AsyncSession, phone_e164: str):
    """Best-effort match of a number to a client, newest first.

    Client phones are stored as entered, so match on the digits. A miss is
    fine — the ledger keeps unknown-sender rows too.
    """
    from sqlalchemy import func

    from app.models.client import Client

    digits = "".join(ch for ch in phone_e164 if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not digits:
        return None
    try:
        return (
            await db.execute(
                select(Client)
                .where(func.regexp_replace(Client.phone, r"\D", "", "g").like(f"%{digits}"))
                .order_by(Client.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        log.exception("sms ledger: client lookup failed for %s", phone_e164)
        return None
