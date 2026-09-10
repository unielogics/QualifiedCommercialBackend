"""The worksheet as an outsider sees it. No login on any route in this file.

Four endpoints — unlock, read, cells, rows — behind one shared token. They are
here rather than beside the staff routes in `application_profiles.py` because
everything in this module is unauthenticated, and a file where that is true of
every route is a file you can audit by reading the top of it. The staff
worksheet endpoints keep their own home; the two paths meet in
`services/sheets.py`, which is the only place the figures are actually written.

What each route does before it does anything else:

1. `worksheet_links.resolve` — throttle, uniform 404, PIN, session.
2. `require_sheets` — every sheet named is one this link opens, or the same 404.
3. `require_edit` — a view-only link writes nothing, and hears 403 saying so.
4. `guard_write_rate` — one link cannot loop.
5. `record_edit` — every accepted write leaves a row with before and after.

Step 5 is the one that did not exist. The public form path saves a borrower's
balance sheet today with no audit trail whatsoever; a worksheet is meant to be
edited continuously by someone with no account, so it cannot inherit that.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services import worksheet_links

router = APIRouter(prefix="/public/worksheets", tags=["worksheets-public"])

#: The unlock session travels in a header, never in the URL. Caddy logs every
#: request line to stdout, so a credential in a query string is a credential in
#: the access log, in browser history and in any Referer the page leaks.
SESSION_HEADER = "X-Worksheet-Session"

#: Annotated rather than a Depends() default: a call in an argument default is
#: what B008 is about, and this module is new enough not to need the exemption
#: the older routers carry.
DbSession = Annotated[AsyncSession, Depends(get_db)]
LinkSession = Annotated[str | None, Header(alias=SESSION_HEADER)]


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class WorksheetUnlockBody(BaseModel):
    pin: str = Field(min_length=1, max_length=32)


class WorksheetUnlocked(BaseModel):
    #: Null when the link carries no PIN: there is nothing to present on later
    #: calls, and the page should not invent a header.
    session: str | None = None
    expires_at: Any = None


class CellEdit(BaseModel):
    sheet: str = Field(min_length=1, max_length=24)
    key: str = Field(min_length=1, max_length=160)
    #: Raw as typed. Normalising money is `sheet_layout`'s job, and doing it
    #: here as well would give two answers to what "(500)" means.
    value: Any = None


class CellsBody(BaseModel):
    edits: list[CellEdit] = Field(default_factory=list, max_length=500)
    #: What the client believed the workbook clock read. A far-stale batch is
    #: refused by `sheets.apply_cell_edits` with one 409 rather than silently
    #: overwriting somebody who is still typing.
    base_rev: dict[str, Any] | None = None
    #: What the guest called themselves. Attribution in the audit row only —
    #: it is stored in a field named `claimed_name` for exactly that reason.
    name: str | None = Field(default=None, max_length=120)


class RowsBody(BaseModel):
    sheet: str = Field(min_length=1, max_length=24)
    op: str = Field(min_length=1, max_length=16)
    row_id: str | None = Field(default=None, max_length=64)
    after: str | None = Field(default=None, max_length=64)
    block: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ip(request: Request) -> str | None:
    from app.request_context import client_ip

    return client_ip(request)


async def _snapshot(db: AsyncSession, access, kinds) -> dict[str, dict[str, Any]]:
    """The named sheets as stored right now, keyed by kind.

    Read once before a write and once after, because there is nowhere else a
    truthful `before` can come from — the request carries only what the client
    wants the cell to say. Scoped to the sheets the batch touches, which is
    normally one, so the cost is a couple of queries on a path that fires at
    blur/Enter rather than per keystroke. An audit row nobody can read a
    before-value out of would not be worth writing.
    """
    from app.services import sheets

    payload = await sheets.read_sheets(
        db, access.profile, kinds=list(kinds), origin=access.origin, worksheet=access.worksheet
    )
    return {
        str(sheet.get("kind")): sheet
        for sheet in (payload.get("sheets") or [])
        if isinstance(sheet, dict)
    }


def _values(snapshot: dict[str, dict[str, Any]], kind: str) -> dict[str, Any]:
    return dict((snapshot.get(kind) or {}).get("values") or {})


def _row_ids(rows: Any, block: str | None = None) -> list[str]:
    """The identities of a sheet's data rows, in order. Headers, section titles
    and total lines are not rows anyone inserted, so they are not recorded."""
    out: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("kind") != "data":
            continue
        if block and str(row.get("block") or "") != block:
            continue
        key = row.get("row_key")
        if key:
            out.append(str(key))
    return out


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.post("/{token}/unlock", response_model=WorksheetUnlocked)
async def unlock_worksheet(
    token: str, payload: WorksheetUnlockBody, request: Request, db: DbSession
) -> WorksheetUnlocked:
    """Trade the PIN for a 12-hour session. A wrong PIN counts on the link row,
    so five of them lock it whether or not this container restarts."""
    _access, session, expires = await worksheet_links.resolve(
        db, token, pin=payload.pin, client_ip=_ip(request)
    )
    await db.commit()
    return WorksheetUnlocked(session=session, expires_at=expires)


@router.get("/{token}")
async def read_worksheet(
    token: str, request: Request, db: DbSession, session: LinkSession = None
) -> dict[str, Any]:
    """The workbook, filtered to what this link opens.

    The filter is applied twice on purpose. `read_sheets` is told which kinds to
    load, and anything outside the scope is dropped from the payload again here.
    One of the two is redundant today; the redundant one is the cheap half of
    the pair that keeps a borrower's net worth off an accountant's wire.
    """
    access, _s, _e = await worksheet_links.resolve(
        db, token, session=session, client_ip=_ip(request)
    )
    from app.services import sheets

    payload = dict(
        await sheets.read_sheets(
            db,
            access.profile,
            kinds=list(access.sheets),
            origin=access.origin,
            worksheet=access.worksheet,
        )
    )
    payload["sheets"] = [
        sheet
        for sheet in (payload.get("sheets") or [])
        if isinstance(sheet, dict) and str(sheet.get("kind") or "") in access.sheets
    ]
    scope = dict(payload.get("scope") or {})
    scope["sheets"] = list(access.sheets)
    scope["can_edit"] = access.can_edit
    scope["open_at"] = access.sheets[0] if access.sheets else None
    payload["scope"] = scope
    # Alongside the scope rather than only inside it: the toolbar reads one
    # boolean, and a nested one is easy to forget to look at.
    payload["can_edit"] = access.can_edit
    payload["completed"] = access.link.completed_at is not None
    await db.commit()
    return payload


@router.post("/{token}/cells")
async def write_worksheet_cells(
    token: str, payload: CellsBody, request: Request, db: DbSession, session: LinkSession = None
) -> dict[str, Any]:
    from app.services import sheets

    access, _s, _e = await worksheet_links.resolve(
        db, token, session=session, client_ip=_ip(request)
    )
    worksheet_links.require_edit(access)
    edits = [edit.model_dump() for edit in payload.edits]
    if not edits:
        # A flush with nothing in it is a no-op, not a scope violation. Falling
        # through would ask `require_sheets` about an empty set and answer the
        # uniform 404, which would read to the page as a broken link.
        await db.commit()
        return {"rev": {}, "computed": {}, "resync": []}
    kinds = worksheet_links.require_sheets(access, {edit["sheet"] for edit in edits})
    worksheet_links.guard_write_rate(access)

    before = await _snapshot(db, access, kinds)
    result = await sheets.apply_cell_edits(
        db,
        access.profile,
        edits,
        base_rev=payload.base_rev,
        origin=access.origin,
        actor_user_id=None,
        worksheet=access.worksheet,
    )
    after = await _snapshot(db, access, kinds)
    revs = dict(result.get("rev") or {})
    for kind in kinds:
        touched = [edit["key"] for edit in edits if edit["sheet"] == kind]
        await worksheet_links.record_edit(
            db,
            access,
            sheet_kind=kind,
            revision=revs.get(kind) or getattr(access.worksheet, "revision", 0) or 0,
            changes=worksheet_links.diff_values(
                kind, _values(before, kind), _values(after, kind), touched
            ),
            request=request,
            claimed_name=payload.name,
        )
    await db.commit()
    return result


@router.post("/{token}/rows")
async def write_worksheet_rows(
    token: str, payload: RowsBody, request: Request, db: DbSession, session: LinkSession = None
) -> dict[str, Any]:
    """Add or remove a line on the debt schedule or one of the PFS schedules.

    The audit row records the row identities before and after rather than a
    cell diff: what changed is the shape of the sheet, and "these four rows
    became five, the new one is <id>" is the sentence somebody reading the log
    a month later needs.
    """
    from app.services import sheets

    access, _s, _e = await worksheet_links.resolve(
        db, token, session=session, client_ip=_ip(request)
    )
    worksheet_links.require_edit(access)
    (kind,) = worksheet_links.require_sheets(access, [payload.sheet])
    worksheet_links.guard_write_rate(access)

    before = await _snapshot(db, access, [kind])
    before_rows = _row_ids((before.get(kind) or {}).get("rows"), payload.block)
    result = await sheets.apply_row_op(
        db,
        access.profile,
        kind=kind,
        op=payload.op,
        row_id=payload.row_id,
        after=payload.after,
        block=payload.block,
        origin=access.origin,
        actor_user_id=None,
        worksheet=access.worksheet,
    )
    after_rows = _row_ids(result.get("rows"), payload.block)
    revs = dict(result.get("rev") or {})
    address = f"{kind}.rows" + (f".{payload.block}" if payload.block else "")
    await worksheet_links.record_edit(
        db,
        access,
        sheet_kind=kind,
        revision=revs.get(kind) or getattr(access.worksheet, "revision", 0) or 0,
        changes={
            address: {
                "before": worksheet_links.clip(before_rows),
                "after": worksheet_links.clip(after_rows),
                "op": payload.op,
                "row_id": payload.row_id,
            }
        },
        request=request,
        claimed_name=payload.name,
    )
    await db.commit()
    return result


__all__ = ["SESSION_HEADER", "router"]
