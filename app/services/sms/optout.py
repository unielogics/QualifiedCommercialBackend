"""The gate that stands between every outbound text and a phone number.

Two records answer "may we text this number", and both have to be consulted:

  dos_sms_consent   the grant lifecycle for dealer files. `consent_for` is
                    already number-keyed with revocation-wins semantics.
  sms_opt_out       a plain suppression list, needing no prior grant.

The second exists because the first cannot answer for a number that never
granted anything. `sms_consent.revoke()` only marks rows that are granted and
un-revoked, so a STOP from a CRM client — who has no consent rows at all — used
to match nothing and leave no trace, and the number stayed textable.

Reading is deliberately cheap and fail-closed-ish: `is_opted_out` returns True
on a lookup it could not complete, because refusing to send a text that should
have gone is recoverable, and sending one to a person who said stop is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sms_opt_out import SmsOptOut

log = logging.getLogger(__name__)

__all__ = ["is_opted_out", "record_opt_out", "clear_opt_out", "OPT_OUT_KEYWORDS"]


#: What carriers require to be honoured as an opt-out. Compared after stripping
#: punctuation and casing, so "Stop." and " STOP " both count.
OPT_OUT_KEYWORDS = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout"}
)


def is_opt_out_keyword(message: str) -> bool:
    """Whether an inbound reply is an opt-out request."""
    return "".join(ch for ch in (message or "").lower() if ch.isalpha()) in OPT_OUT_KEYWORDS


async def is_opted_out(db: AsyncSession, phone_e164: str) -> bool:
    """Has this number asked not to be texted?

    Returns True if it cannot tell. A lookup failure is not permission to send.
    """
    if not phone_e164:
        return True
    try:
        row = (
            await db.execute(
                select(SmsOptOut).where(
                    SmsOptOut.phone_e164 == phone_e164,
                    SmsOptOut.cleared_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return row is not None
    except Exception:  # noqa: BLE001
        log.exception("sms opt-out lookup failed for %s — refusing the send", phone_e164)
        return True


async def record_opt_out(
    db: AsyncSession,
    *,
    phone_e164: str,
    reason: str = "STOP",
    source: str = "sms_reply",
    note: str | None = None,
) -> SmsOptOut:
    """Suppress a number. Idempotent, and revives a previously cleared row.

    Also revokes any dealer consent grants the number holds, so the two records
    cannot disagree about whether the person is reachable.
    """
    row = (
        await db.execute(select(SmsOptOut).where(SmsOptOut.phone_e164 == phone_e164))
    ).scalar_one_or_none()

    if row is None:
        row = SmsOptOut(
            phone_e164=phone_e164, reason=reason[:120], source=source[:32], note=note
        )
        db.add(row)
    else:
        row.cleared_at = None
        row.reason = reason[:120]
        row.source = source[:32]
        if note:
            row.note = note

    # Persist the suppression BEFORE touching anything else. This row is the
    # load-bearing record: if the rest of this function falls over, the number
    # must still come back suppressed.
    await db.flush()

    # Keep the grant lifecycle in step — a bare STOP revokes everything for the
    # number, which is what a person means by it.
    #
    # Inside a SAVEPOINT, and that is not defensive padding. A failed statement
    # aborts the whole enclosing transaction in Postgres, so catching the
    # exception is not enough: without the savepoint, a revoke that raises takes
    # the suppression row down with it and `record_opt_out` returns as though it
    # succeeded. Losing an opt-out while reporting success is the single worst
    # failure this module can have.
    try:
        async with db.begin_nested():
            from app.dealer_os.services import sms_consent as sms_consent_svc

            await sms_consent_svc.revoke(db, phone_e164=phone_e164, reason=reason)
    except Exception:  # noqa: BLE001
        log.exception(
            "opt-out recorded for %s but dealer consent revoke failed — "
            "suppression stands, grants may be stale",
            phone_e164,
        )
    log.info("sms opt-out recorded phone=%s source=%s", phone_e164, source)
    return row


async def clear_opt_out(
    db: AsyncSession, *, phone_e164: str, note: str | None = None
) -> bool:
    """Lift a suppression after a fresh, explicit opt-in.

    Does NOT re-grant consent — that is a separate, documented act recorded by
    `sms_consent.record_consent`. This only stops the suppression list from
    vetoing it.
    """
    row = (
        await db.execute(
            select(SmsOptOut).where(
                SmsOptOut.phone_e164 == phone_e164, SmsOptOut.cleared_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.cleared_at = datetime.now(timezone.utc)
    if note:
        row.note = note
    await db.flush()
    log.info("sms opt-out cleared phone=%s", phone_e164)
    return True
