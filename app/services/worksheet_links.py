"""The no-login door onto a worksheet: what a token opens, and what it does not.

The owner's ask was *"this link should be able to be shared without login,
example the accountant"*, with per-link scopes — edit or view, and which of the
four sheets it opens. Everything in this module exists to make the second half
of that sentence true, because a scope the server does not enforce is a promise
the sharing dialog makes and the API breaks.

Three rules run through it:

**The scope is read off the row, never off the request.** `access.sheets` comes
from `financial_form_link_sheets`, and every read filters to it and every write
is refused outside it. A client that could name its own sheets could name the
personal financial statement.

**Independent tokens.** The forms packet derives four child tokens as
`f"{base}.{kind}"`, which means any child yields the base by `rsplit(".", 1)`
and the base yields all four children by concatenation — so a packet link
advertised as "P&L only" would be a lie. A worksheet token is its own
`secrets.token_urlsafe(32)` and this module refuses any token containing a dot
outright, so the derivation cannot creep back in by someone reusing the shape.

**Every miss reads the same.** Unknown, revoked, expired, wrong kind, no sheets,
a sheet outside the link's scope — one identical 404. The two exceptions are the
ones the production package already makes and the page genuinely needs: 401
`pin_required` so it knows to show the field, and 429 `pin_locked`. A view-only
holder writing gets 403, not 404: they legitimately hold the link, and a 404
would tell them it is broken.

The PIN mechanics are `production_packages.resolve_public_share` — PBKDF2 off
the event loop, attempts and lockout persisted on the row, a 12-hour HMAC
session bound to `link.id` + `pin_set_at`. They are copied rather than imported
because that function resolves a *package*; the parts worth sharing (the miss
throttle) are in `link_throttle`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.application_profile import ApplicationProfile
from app.models.financial_form_link import FinancialFormLink
from app.services import link_throttle

#: The link kind this module opens, and only this one. A `pfs` or `p_and_l`
#: form link resolved here would come with no sheet scope rows and no
#: worksheet, so it is refused with the same 404 as a token that never existed.
LINK_KIND = "worksheet"

#: The workbook's four sheets in tab order. Duplicated from `sheet_layout.KINDS`
#: rather than imported at module scope so this module stays importable without
#: the grid service; `_ordered` checks against it.
SHEET_KINDS: tuple[str, ...] = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")

_PIN_MAX_ATTEMPTS = 5
_PIN_LOCKOUT = timedelta(minutes=15)
_LINK_SESSION_TTL = timedelta(hours=12)

#: What a guest's writes are attributed to in the statement bodies. The same
#: origin the borrower's own form uses, so the debt schedule's "write only what
#: your own origin owns" rule keeps a guest off the desk's rows.
GUEST_ORIGIN = "client_form"


def _now() -> datetime:
    return datetime.now(UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def gone() -> HTTPException:
    """The one uniform miss. Every caller raises this exact object for every
    reason a token does not open a worksheet, so a prober times one code path
    and learns nothing from the difference between 'never existed' and
    'revoked yesterday'."""
    return HTTPException(status.HTTP_404_NOT_FOUND, "This link is no longer available")


@dataclass
class WorksheetAccess:
    """What one token proved: which worksheet, on whose file, over which sheets."""

    link: FinancialFormLink
    profile: ApplicationProfile
    worksheet: Any  # FinancialWorksheet — imported lazily, see `_worksheet_for`
    sheets: tuple[str, ...] = ()
    can_edit: bool = False
    #: Attribution only. Never access: a name typed into a prompt on a page with
    #: no login proves nothing, and it is stored in its own field for that reason.
    claimed_name: str | None = None
    via: str = "share_link"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return GUEST_ORIGIN

    def opens(self, kind: str) -> bool:
        return kind in self.sheets


# ---------------------------------------------------------------------------
# the signed session minted on a PIN unlock
# ---------------------------------------------------------------------------


def _link_secret() -> bytes:
    """Derived from the app's own secret, like the production link and the
    pre-call links: nothing extra to provision, and rotating that secret ends
    every open worksheet session at once."""
    s = get_settings()
    raw = s.clerk_secret_key or getattr(s, "provider_secrets_encryption_key", "") or "qc-worksheet"
    return hashlib.sha256(f"worksheet-link-session:{raw}".encode()).digest()


def _link_session(link: FinancialFormLink) -> tuple[str, datetime]:
    """A 12-hour credential bound to the link *and* to when its PIN was set.

    Binding to `pin_set_at` is what makes rotating the PIN end every open tab:
    there is no session table to revoke, because the thing the signature
    covers changed.
    """
    expires = _now() + _LINK_SESSION_TTL
    payload = {
        "lid": str(link.id),
        "pst": link.pin_set_at.isoformat() if link.pin_set_at else "",
        "exp": int(expires.timestamp()),
    }
    body = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    sig = hmac.new(_link_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}", expires


def _link_session_valid(session: str | None, link: FinancialFormLink) -> bool:
    try:
        body, sig = (session or "").split(".", 1)
    except ValueError:
        return False
    expected = hmac.new(_link_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(
            base64.urlsafe_b64decode((body + "=" * (-len(body) % 4)).encode()).decode()
        )
    except (ValueError, UnicodeDecodeError):
        return False
    pst = link.pin_set_at.isoformat() if link.pin_set_at else ""
    return (
        payload.get("lid") == str(link.id)
        and payload.get("pst") == pst
        and int(payload.get("exp", 0) or 0) > int(_now().timestamp())
    )


# ---------------------------------------------------------------------------
# resolving
# ---------------------------------------------------------------------------


def _ordered(kinds: Any) -> tuple[str, ...]:
    """The link's sheets in workbook order, with anything unrecognised dropped."""
    wanted = {str(k) for k in (kinds or [])}
    return tuple(kind for kind in SHEET_KINDS if kind in wanted)


async def _sheet_scope(db: AsyncSession, link: FinancialFormLink) -> tuple[str, ...]:
    from app.models.financial_worksheet import FinancialFormLinkSheet

    rows = await db.execute(
        select(FinancialFormLinkSheet.sheet_kind).where(
            FinancialFormLinkSheet.link_id == link.id
        )
    )
    return _ordered(rows.scalars().all())


async def _worksheet_for(db: AsyncSession, link: FinancialFormLink):
    from app.models.financial_worksheet import FinancialWorksheet

    if getattr(link, "worksheet_id", None) is None:
        return None
    return await db.get(FinancialWorksheet, link.worksheet_id)


async def resolve(
    db: AsyncSession,
    token: str,
    *,
    pin: str | None = None,
    session: str | None = None,
    client_ip: str | None = None,
) -> tuple[WorksheetAccess, str | None, datetime | None]:
    """A shared worksheet link: the token, plus a live session or the PIN.

    Returns `(access, session, session_expires)`; the session is minted only on
    a PIN unlock, so a GET carrying a valid session gets `None` back and has
    nothing to store.

    The order matters. The address throttle runs before anything is read, so a
    prober is stopped before the database is touched. The dot guard runs before
    the lookup, so `{base}.{kind}` cannot be walked even by accident. The PIN
    is checked last, once the link is known to be real, and a wrong one counts
    on the row where a restart cannot forget it.
    """
    from app.dealer_os.services.client_room import verify_passcode

    throttle_key = link_throttle.miss_key(token or "", client_ip, prefix="worksheet")
    if link_throttle.locked(throttle_key):
        raise link_throttle.too_many()

    # The packet's `{base}.{kind}` derivation is why per-sheet scope was
    # impossible before: any child token yielded the base and the base yielded
    # all four children. A worksheet token is one opaque secret with no
    # structure to walk, so a dot in it is either that scheme coming back or
    # somebody trying it. Refused before the hash is even computed.
    if "." in (token or "") or not (token or "").strip():
        link_throttle.note_miss(throttle_key)
        raise gone()

    link = (
        await db.execute(
            select(FinancialFormLink).where(FinancialFormLink.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()
    if link is None or link.kind != LINK_KIND or not link.is_open:
        link_throttle.note_miss(throttle_key)
        raise gone()

    sheets = await _sheet_scope(db, link)
    worksheet = await _worksheet_for(db, link)
    profile = await db.get(ApplicationProfile, link.profile_id)
    # No sheets means the link opens nothing, which is indistinguishable from a
    # link that does not exist — and is what it should look like.
    if not sheets or worksheet is None or profile is None:
        raise gone()

    new_session: str | None = None
    expires: datetime | None = None
    if getattr(link, "pin_hash", None):
        if session and _link_session_valid(session, link):
            pass
        elif pin is not None:
            now = _now()
            if link.pin_locked_until is not None and link.pin_locked_until > now:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": "pin_locked",
                        "message": "Too many wrong PINs. Try again in a few minutes.",
                    },
                )
            ok = await asyncio.to_thread(verify_passcode, str(pin).strip(), link.pin_hash)
            if not ok:
                link.pin_attempts = (link.pin_attempts or 0) + 1
                if link.pin_attempts >= _PIN_MAX_ATTEMPTS:
                    link.pin_locked_until = now + _PIN_LOCKOUT
                    link.pin_attempts = 0
                # **Committed, not flushed.** `get_db` rolls the session back on
                # any exception, and this function is about to raise one — a
                # flushed counter would be discarded on the way out and the
                # lockout would never arrive, however many wrong PINs were
                # tried. The only pending change here is the counter, and it is
                # the one thing that must outlive the failed request.
                # (`expire_on_commit=False`, so `link` stays usable.)
                await db.commit()
                link_throttle.note_miss(throttle_key)
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    detail={"code": "pin_invalid", "message": "That PIN is not right."},
                )
            link.pin_attempts = 0
            link.pin_locked_until = None
            new_session, expires = _link_session(link)
        else:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail={"code": "pin_required", "label": link.label},
            )

    link.last_used_at = _now()
    link.use_count = (getattr(link, "use_count", 0) or 0) + 1
    await db.flush()

    access = WorksheetAccess(
        link=link,
        profile=profile,
        worksheet=worksheet,
        sheets=sheets,
        can_edit=(getattr(link, "permission", "edit") or "edit") == "edit",
        claimed_name=link.label,
        via="share_link",
    )
    return access, new_session, expires


def require_edit(access: WorksheetAccess) -> None:
    """A view-only holder writing gets 403, not the uniform 404.

    They hold a link we issued and it opened a moment ago; telling them it does
    not exist would send them to whoever shared it convinced it is broken. The
    check is here rather than in a disabled input because a disabled input is a
    courtesy and this is the boundary.
    """
    if not access.can_edit:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This link opens the worksheet for reading only"
        )


def require_sheets(access: WorksheetAccess, kinds: Any) -> tuple[str, ...]:
    """Every kind touched must be one the link opens, or the uniform 404.

    A sheet outside the scope is 404 rather than 403 on purpose: 403 would
    confirm the sheet exists on this file, and the whole point of unticking the
    personal financial statement is that the holder learns nothing about it.
    """
    wanted = _ordered(kinds)
    if not wanted or any(kind not in access.sheets for kind in wanted):
        raise gone()
    return wanted


def guard_write_rate(access: WorksheetAccess) -> None:
    """One leaked link must not be able to rewrite the sheet in a loop.

    Also a fan-out bound: every accepted write is broadcast to every other
    participant, so an unbounded writer is an unbounded broadcaster.
    """
    key = f"worksheet-write:{access.link.id}"
    if link_throttle.write_capped(key):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Slow down — too many changes at once."
        )
    link_throttle.note_write(key)


# ---------------------------------------------------------------------------
# minting side: what the sharing dialog has to do about the PIN
# ---------------------------------------------------------------------------


def pin_required_for(permission: str, sheets: Any) -> bool:
    """Whether this link must carry a PIN.

    An **edit** link is a continuously writable credential with a month's life,
    designed to be forwarded — the strongest thing this system hands out, and
    the only one of its class with no second factor today: the production
    package share has one, the bucket room has one.

    A link that opens the **personal financial statement** carries a home
    address, a net worth and every personal liability. The `pfs_schema`
    docstring justifies collecting no SSN on the grounds that "the client-facing
    form is reachable through a link with no access code" — that sentence is a
    standing acknowledgement of the weak point, and the fix is to stop it being
    true for the sheet holding the net worth.

    A view-only link over the business sheets is a lower-stakes object, and
    friction there is what pushes people back to emailing spreadsheets, which is
    the actual status quo this replaces. So: not mandatory, and the dialog
    should still default it on.
    """
    return (permission or "edit") == "edit" or "pfs" in {str(k) for k in (sheets or [])}


async def set_pin(link: FinancialFormLink, pin: str | None = None) -> str:
    """Put a PIN on a link and return it once, in the clear, to be shown once.

    Hashing runs off the event loop — PBKDF2 at 240k iterations is deliberately
    expensive, and doing it inline would stall every other request on this
    single worker. Stamping `pin_set_at` is what ends any session minted under
    the previous PIN, so this doubles as rotation.
    """
    from app.dealer_os.services.client_room import (
        _generate_passcode,
        _hash_passcode,
        passcode_problem,
    )

    code = (pin or "").strip()
    if not code:
        code = _generate_passcode()
        while passcode_problem(code):
            code = _generate_passcode()
    elif passcode_problem(code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That PIN is too easy to guess")
    link.pin_hash = await asyncio.to_thread(_hash_passcode, code)
    link.pin_set_at = _now()
    link.pin_attempts = 0
    link.pin_locked_until = None
    return code


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def clip(value: Any, limit: int = 200) -> Any:
    """Values are the borrower's figures, not free text, but a paste can be
    anything. Clipped so one pathological cell cannot make the audit row the
    largest object in the table."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): clip(v, 80) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return [clip(v, 80) for v in value[:24]]
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def diff_values(
    kind: str, before: dict[str, Any], after: dict[str, Any], keys: Any
) -> dict[str, Any]:
    """The `{address: {before, after}}` shape, addresses flattened to strings.

    `"p_and_l.gross_revenue"` rather than a nested object, so somebody reading
    the log during an incident can grep for a figure without first learning the
    address union type. Only keys that actually moved are recorded: a batch that
    re-sent the same value is not a change, and a log full of them hides the one
    that was.
    """
    changes: dict[str, Any] = {}
    for key in keys:
        was, now = before.get(key), after.get(key)
        if was == now:
            continue
        changes[f"{kind}.{key}"] = {"before": clip(was), "after": clip(now)}
    return changes


async def record_edit(
    db: AsyncSession,
    access: WorksheetAccess,
    *,
    sheet_kind: str,
    revision: int,
    changes: dict[str, Any],
    request: Request | None = None,
    claimed_name: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Any:
    """One row per accepted write. Nothing else in this path leaves a trace.

    Returns the row so a caller can attach it to an event; returns None when
    there was nothing to record, so a no-op save does not manufacture history.
    """
    from app.models.financial_worksheet_edit import FinancialWorksheetEdit
    from app.request_context import client_ip

    if not changes:
        return None
    agent = request.headers.get("user-agent") if request is not None else None
    row = FinancialWorksheetEdit(
        worksheet_id=access.worksheet.id,
        revision=int(revision or 0),
        sheet_kind=sheet_kind,
        changes=changes,
        actor_user_id=actor_user_id,
        link_id=access.link.id if access.link is not None else None,
        claimed_name=(claimed_name or access.claimed_name or "").strip()[:120] or None,
        via=access.via,
        ip=client_ip(request) if request is not None else None,
        user_agent=(agent or "")[:400] or None,
    )
    db.add(row)
    return row
