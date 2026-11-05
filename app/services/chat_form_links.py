"""Form links handed out by the client-facing AI chat.

The owner's ask was that the chat "be able to give them the links or show them
buttons that take them to the PFS etc." The buttons already existed; what they
opened was the document room with a query parameter nothing read. This module
is what a "Complete now" or "Open the worksheet" button now mints: the same
links the desk mints by hand from the file page, attributed to the chat.

Two rules run through it, both mirrored from the router functions
`mint_financial_form_link` and `mint_worksheet_link` in
`app/routers/application_profiles.py` rather than reinvented:

**A kind link is minted exactly the way the desk mints one.** A PFS link gets
its empty statement first, so the borrower resumes a draft instead of starting
a second sheet; the debt schedule writes straight to the file and needs no
placeholder. A worksheet link draws its own token (never derived from a packet
base), is joined to the worksheet by `worksheet_id`, records which sheets it
opens as rows, and carries a PIN whenever `worksheet_links.pin_required_for`
says so. Nobody is signed in when the chat mints, so `created_by` is None and
the borrower's own address goes on `invitee_email`; the label says where the
link came from.

**Nothing secret outlives the response.** `mint_link` keeps only the token's
hash ("a lost link is reminted, not looked up") and `set_pin` returns the PIN
"once, in the clear, to be shown once". The chat honours both: the URL, which
embeds the raw token, and the PIN go back to the borrower in the execute
response and are written nowhere — not on the action row, whose `result` is
re-served to the room on every load, and not in the audit, which records that
a PIN was set and never what it is.

**At most one live chat-minted link per target.** Because a click cannot look
an old link up, every click mints, and a borrower who presses the same button
twice would otherwise hold two working credentials to the same form.
`retire_chat_links` closes the earlier ones first: every open link on the same
file that the chat minted for the same target — the same `kind` for a form
page, the same sheet set (read from the link's own scope rows) for a worksheet
— is revoked before the new one is drawn. Links the desk minted by hand are a
different label and a different author and are never touched.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketAIChatAction
from app.models.financial_form_link import FinancialFormLink
from app.models.financial_worksheet import FinancialFormLinkSheet
from app.services import application_profiles as profiles
from app.services import financial_statements, pfs_schema, worksheet_links
from app.services import sheets as sheets_service

#: What the desk sees on a link the chat minted, in the same `label` column the
#: sharing dialog fills in by hand.
CHAT_LINK_LABEL = "From the secure room chat"

#: The audit action. Distinct from the desk's `financial_form.link_minted` so
#: "which links did the AI hand out?" is a filter, not a guess.
AUDIT_ACTION = "financial_form.link_minted_by_chat"

#: Every link the chat mints is writable: the borrower is being asked to fill
#: the form in, and a view link would be a button that opens a page they
#: cannot type into.
WORKSHEET_PERMISSION = "edit"

#: The single-form kinds the chat can mint on their own. The P&L and balance
#: sheet are asked for as one requirement and opened as one worksheet link.
FORM_KINDS: tuple[str, ...] = ("pfs", "debt_schedule", "p_and_l", "balance_sheet")

_FORM_LABEL = {
    "pfs": "personal financial statement",
    "debt_schedule": "business debt schedule",
    "p_and_l": "profit and loss statement",
    "balance_sheet": "balance sheet",
}

#: Which sheets each actionable requirement opens when the borrower chooses to
#: complete it now. The PFS and the debt schedule have form pages of their own;
#: the P&L and balance sheet live only in the worksheet.
COMPLETE_NOW_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "owner_personal_financial_statement": ("pfs", ()),
    "business_debt_schedule": ("debt_schedule", ()),
    "ytd_p_and_l_balance_sheet": ("worksheet", ("p_and_l", "balance_sheet")),
}

def _base_url() -> str:
    return get_settings().frontend_app_url.rstrip("/")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def ordered_sheets(kinds: Iterable[str]) -> tuple[str, ...]:
    """The requested sheets in workbook order, anything unknown dropped."""
    wanted = {str(kind) for kind in (kinds or [])}
    return tuple(kind for kind in sheets_service.KINDS if kind in wanted)


def target_for(action_type: str, requirement_key: str | None) -> tuple[str, tuple[str, ...]] | None:
    """What a chat action opens: `(kind, sheets)`.

    `("pfs", ())` or `("debt_schedule", ())` for a form page, `("worksheet",
    (...))` for a worksheet link scoped to those sheets. None for an action
    that opens no form (upload, download, email). Two actions with equal
    targets open the same form, which is why minting for one retires the
    chat's earlier link for the other.
    """
    if action_type == "open_worksheet":
        return ("worksheet", tuple(sheets_service.KINDS))
    if action_type == "complete_now":
        return COMPLETE_NOW_TARGETS.get(requirement_key or "")
    return None


# ---------------------------------------------------------------------------
# minting
# ---------------------------------------------------------------------------


async def mint_form_link(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    kind: str,
    recipient: str | None,
    retired_previous: int = 0,
) -> dict:
    """One link onto one form page, minted the way the desk mints it.

    Returns `{form_url, access_code, link_id, expires_at}`. A form page carries
    no PIN, so `access_code` is always None here; the key is present so every
    minted link has the same shape to its caller. `retired_previous` is how
    many earlier chat links `link_for_action` closed before this mint; it goes
    on the audit row so "why was the borrower's link revoked?" has an answer.
    """
    if kind not in FORM_KINDS:
        raise ValueError(f"Unknown form kind: {kind}")

    statement_id: uuid.UUID | None = None
    if kind == "pfs":
        # Create the statement the link will fill, as the router does, so the
        # borrower lands on a draft they can resume rather than a blank that
        # becomes a second sheet. Nobody is signed in: no actor.
        statement = await financial_statements.latest_for_profile(db, profile.id)
        if statement is None:
            statement = await financial_statements.save_statement(
                db, profile, body=pfs_schema.empty_body(), actor_user_id=None
            )
        statement_id = statement.id

    link, token = await financial_statements.mint_link(
        db,
        profile,
        kind=kind,
        statement_id=statement_id,
        label=CHAT_LINK_LABEL,
        invitee_email=recipient,
        created_by=None,
    )
    await profiles.log_profile_action(
        db,
        profile,
        None,
        AUDIT_ACTION,
        f"The secure room chat created a share link for the {_FORM_LABEL[kind]}",
        target_type="financial_form",
        target_id=link.id,
        # The token is deliberately absent: the audit row names the link, never
        # the credential that opens it.
        metadata={
            "link_id": str(link.id),
            "kind": kind,
            "recipient": recipient,
            "retired_previous": retired_previous,
        },
    )
    return {
        "form_url": f"{_base_url()}/forms/{kind}/{token}",
        "access_code": None,
        "link_id": str(link.id),
        "expires_at": _iso(link.expires_at),
    }


async def mint_worksheet_link(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    sheets: Iterable[str],
    recipient: str | None,
    retired_previous: int = 0,
) -> dict:
    """An edit link onto some of the worksheet, minted the way the desk mints it.

    Returns `{form_url, access_code, link_id, expires_at}`. `access_code` is
    the PIN when `worksheet_links.pin_required_for` demands one — which, for an
    edit link, is always — returned here once and never recoverable afterwards.
    The caller shows it and forgets it; a borrower who loses it clicks again
    and gets a new link with a new PIN, the old one retired.
    """
    wanted = ordered_sheets(sheets)
    if not wanted:
        raise ValueError("A worksheet link has to open at least one sheet")

    worksheet = await sheets_service.ensure_worksheet(db, profile, created_by=None)
    link, token = await financial_statements.mint_link(
        db,
        profile,
        kind="worksheet",
        label=CHAT_LINK_LABEL,
        invitee_email=recipient,
        created_by=None,
        ttl_days=financial_statements.DEFAULT_LINK_TTL_DAYS,
        # Drawn on its own, never derived: see the router's docstring on why a
        # packet-style `{base}.{kind}` token would make per-sheet scope a lie.
        token=secrets.token_urlsafe(32),
    )
    link.worksheet_id = worksheet.id
    link.packet_id = worksheet.packet_id
    link.permission = WORKSHEET_PERMISSION
    for kind in wanted:
        db.add(FinancialFormLinkSheet(link_id=link.id, sheet_kind=kind))

    pin: str | None = None
    if worksheet_links.pin_required_for(WORKSHEET_PERMISSION, wanted):
        pin = await worksheet_links.set_pin(link)
    await db.flush()

    await profiles.log_profile_action(
        db,
        profile,
        None,
        AUDIT_ACTION,
        f"The secure room chat shared {len(wanted)} worksheet sheet(s) as an "
        f"{WORKSHEET_PERMISSION} link" + (" with a PIN" if pin else ""),
        target_type="financial_worksheet",
        target_id=worksheet.id,
        # The PIN is deliberately absent: the audit row says one was set, never
        # what it is.
        metadata={
            "link_id": str(link.id),
            "sheets": list(wanted),
            "permission": WORKSHEET_PERMISSION,
            "pin_set": bool(pin),
            "recipient": recipient,
            "retired_previous": retired_previous,
        },
    )
    return {
        "form_url": f"{_base_url()}/forms/worksheet/{token}",
        "access_code": pin,
        "link_id": str(link.id),
        "expires_at": _iso(link.expires_at),
    }


# ---------------------------------------------------------------------------
# retiring
# ---------------------------------------------------------------------------


async def _link_sheets(db: AsyncSession, link: FinancialFormLink) -> tuple[str, ...]:
    rows = await db.execute(
        select(FinancialFormLinkSheet.sheet_kind).where(FinancialFormLinkSheet.link_id == link.id)
    )
    return ordered_sheets(rows.scalars().all())


async def retire_chat_links(
    db: AsyncSession,
    *,
    profile_id: uuid.UUID,
    target: tuple[str, tuple[str, ...]],
) -> int:
    """Revoke every open link the chat minted on this file for this target.

    A chat-minted link is one carrying `CHAT_LINK_LABEL` with no `created_by`
    — the mint sets exactly that pair, and a link the desk minted by hand has
    an author, so neither marker alone is trusted. Open means not revoked and
    not expired. The target is the link's `kind` for a form page; for a
    worksheet it is the sheet set, read off the link's own scope rows rather
    than inferred, so a two-sheet link and a four-sheet link on the same file
    retire independently. Returns how many were closed.
    """
    kind, wanted_sheets = target
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            select(FinancialFormLink).where(
                FinancialFormLink.profile_id == profile_id,
                FinancialFormLink.kind == kind,
                FinancialFormLink.label == CHAT_LINK_LABEL,
                FinancialFormLink.created_by.is_(None),
                FinancialFormLink.revoked_at.is_(None),
                or_(FinancialFormLink.expires_at.is_(None), FinancialFormLink.expires_at > now),
            )
        )
    ).scalars().all()
    retired = 0
    for link in rows:
        if not link.is_open:
            continue
        if kind == "worksheet" and await _link_sheets(db, link) != wanted_sheets:
            continue
        link.revoked_at = now
        retired += 1
    if retired:
        await db.flush()
    return retired


async def link_for_action(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    action: BucketAIChatAction,
) -> dict:
    """The link a clicked action opens: the chat's earlier link for the same
    target retired, a new one minted.

    Returns `{form_url, access_code, link_id, expires_at}`, the first two of
    which exist only in this return value. The caller hands them to the
    borrower and persists neither.
    """
    target = target_for(action.action_type, action.requirement_key)
    if target is None:
        raise ValueError(f"{action.action_type} opens no form")
    retired = await retire_chat_links(db, profile_id=profile.id, target=target)
    kind, wanted_sheets = target
    if kind == "worksheet":
        return await mint_worksheet_link(
            db,
            profile,
            sheets=wanted_sheets,
            recipient=action.recipient_email,
            retired_previous=retired,
        )
    return await mint_form_link(
        db, profile, kind=kind, recipient=action.recipient_email, retired_previous=retired
    )
