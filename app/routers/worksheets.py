"""The worksheet as an outsider sees it, plus the live half both sides share.

Four endpoints — unlock, read, cells, rows — behind one shared token. They are
here rather than beside the staff routes in `application_profiles.py` because
everything on `/public/worksheets` is unauthenticated, and a prefix where that
is true of every route is a prefix you can audit by reading the top of it. The
staff worksheet endpoints keep their own home; the two paths meet in
`services/sheets.py`, which is the only place the figures are actually written.

**The exception, and why it is here.** `staff_router` at the bottom carries two
signed-in routes: the desk's own event stream and its cursor. They live beside
the guest stream rather than with the other staff routes because a live grid is
one feature with two doors, and the property that matters — *the audience is
computed from what the caller proved, never from what the request asked for* —
is only checkable if both doors are on the same page. Everything under
`/public/worksheets` is still unauthenticated; everything under `staff_router`
still resolves a user first. Nothing on either side reads a sheet list off the
request body.

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

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_db
from app.deps import resolve_user_from_headers
from app.enums import Role
from app.services import worksheet_links, worksheet_presence
from app.services.communication_events import HEARTBEAT_SECONDS
from app.services.communication_events import broker as communication_event_broker

log = logging.getLogger(__name__)

router = APIRouter(prefix="/public/worksheets", tags=["worksheets-public"])

#: The signed-in half of the live grid. Same prefix as the other staff
#: worksheet routes, registered separately so this module's two audiences stay
#: visibly apart. See the module docstring for why it is here at all.
staff_router = APIRouter(prefix="/application-profiles", tags=["worksheets-live"])

#: The unlock session travels in a header, never in the URL. Caddy logs every
#: request line to stdout, so a credential in a query string is a credential in
#: the access log, in browser history and in any Referer the page leaks.
SESSION_HEADER = "X-Worksheet-Session"

#: The tab, and what the person in it asked to be called. Headers rather than
#: query parameters for the same reason as the session: the request line is
#: logged, and a guest's name is the one piece of a worksheet URL that is about
#: a person. `EventSource` cannot send headers, which is why the browser opens
#: this stream with `fetch` + `ReadableStream`.
PARTICIPANT_HEADER = "X-Worksheet-Participant"
NAME_HEADER = "X-Worksheet-Name"

#: Who may not work on a financial statement. Copied from
#: `application_profiles._STATEMENT_DENIED_ROLES` rather than imported: that
#: module imports most of the app, and a router importing another router to
#: reach one frozenset is how an import cycle starts. The list is short and a
#: test pins that the two agree.
_STATEMENT_DENIED_ROLES = {Role.CLIENT, Role.DEALER, Role.VENDOR, Role.LENDER}

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


class LiveBody(BaseModel):
    """The two identifiers every live write carries, and what they are not.

    Neither one is a credential and neither one widens anything: the tab is
    told apart from other tabs so its own echo can be ignored and its name can
    be drawn on the cell, and that is the whole job. Access still comes from
    the token, and the sheets still come from the link row.
    """

    #: Which tab sent this, matched against the presence registry to name the
    #: cell. An id nobody is streaming under simply produces an unlabelled
    #: broadcast.
    participant_id: str | None = Field(default=None, max_length=64)
    #: Echoed back on the broadcast so the sender can ignore its own write.
    #: Without it the cell you are typing in is overwritten by your own event
    #: coming back around.
    client_id: str | None = Field(default=None, max_length=64)


class CellsBody(LiveBody):
    edits: list[CellEdit] = Field(default_factory=list, max_length=500)
    #: What the client believed the workbook clock read. A far-stale batch is
    #: refused by `sheets.apply_cell_edits` with one 409 rather than silently
    #: overwriting somebody who is still typing.
    base_rev: dict[str, Any] | None = None
    #: What the guest called themselves. Attribution in the audit row only —
    #: it is stored in a field named `claimed_name` for exactly that reason.
    name: str | None = Field(default=None, max_length=120)


class RowsBody(LiveBody):
    sheet: str = Field(min_length=1, max_length=24)
    op: str = Field(min_length=1, max_length=16)
    row_id: str | None = Field(default=None, max_length=64)
    after: str | None = Field(default=None, max_length=64)
    block: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    #: Lines on screen for that list. See `WorksheetRowOp.visible`.
    visible: int | None = Field(default=None, ge=0, le=10_000)


class CursorBody(BaseModel):
    """Where somebody is, and nothing about what they are typing.

    `address` is whitelisted down to position keys in
    `worksheet_presence.clean_address` before it goes anywhere near the wire.
    A cursor stream that carried values would be keystroke-level surveillance
    of a person entering their net worth, which is the one place the
    signals-not-content instinct is exactly right.
    """

    participant_id: str = Field(min_length=1, max_length=64)
    sheet: str = Field(min_length=1, max_length=24)
    address: dict[str, Any] | None = None
    editing: bool = False


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
        participant_id=payload.participant_id,
        client_id=payload.client_id,
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
    # Queued, not rendered. An outsider working through a shared link types the
    # same way the desk does, so the same 120-second settle applies: the PDF is
    # filed once the typing stops, on the finished figure.
    await sheets.refresh_touched_pdfs(db, access.profile, kinds, actor_name=payload.name)
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
        participant_id=payload.participant_id,
        client_id=payload.client_id,
        visible=payload.visible,
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
    # Queued, not rendered: see `write_worksheet_cells` above.
    await sheets.refresh_touched_pdfs(db, access.profile, [kind], actor_name=payload.name)
    return result


# ---------------------------------------------------------------------------
# live: one stream shape, two doors
# ---------------------------------------------------------------------------


def _frame(event: dict[str, Any]) -> str:
    """One SSE frame, in the shape `communications.py` already emits, so the
    browser's parser and the reconnect cursor need no second case."""
    event_type = str(event.get("type") or "sync.required")
    event_id = str(event.get("id") or "")
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def _live_stream(
    request: Request,
    worksheet_id: Any,
    participant: worksheet_presence.Participant,
) -> AsyncIterator[str]:
    """The stream both doors return, once the caller has been resolved.

    The order is deliberate. The subscription is opened *first*, so an event
    published a millisecond later is not lost between registering and
    listening. Then the join is announced — the joiner hears their own join
    back and ignores it by `participant_id`, which is cheaper than the race the
    other order creates. The snapshot is written straight into this one stream
    rather than dispatched: it is nobody else's business who just arrived a
    second time.

    **The audiences are the participant's own**, set when they were registered
    from the link row or the staff user's access. Nothing here reads a sheet
    list from the request, which is what makes broadcasting cell values safe:
    the audience key and the read scope are the same set.
    """
    audiences = [
        worksheet_presence.sheet_audience(worksheet_id, kind) for kind in participant.sheets
    ]

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        try:
            async with communication_event_broker.subscribe(audiences) as queue:
                worksheet_presence.announce_join(worksheet_id, participant)
                yield _frame(worksheet_presence.state_event(worksheet_id, participant))
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    except TimeoutError:
                        # The same 25-second comment frame the message stream
                        # uses. It keeps proxies from reaping an idle stream and
                        # is how a client learns the connection is still real.
                        yield ": heartbeat\n\n"
                        continue
                    yield _frame(event)
        finally:
            # The connection *is* the presence, so this is the only place a
            # participant leaves — no endpoint to forget to call, nothing to
            # drift when a tab crashes. The sweeper covers the one case this
            # misses: a half-open socket where the generator is never closed.
            gone_now = worksheet_presence.forget(worksheet_id, participant.participant_id)
            if gone_now is not None:
                worksheet_presence.announce_leave(worksheet_id, gone_now)

    return stream()


def _sse(body: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _participant_id(raw: str | None) -> str:
    """The tab's id, or one minted for it.

    Client-supplied and deliberately so: it is a key into an in-process
    dictionary, not a credential, and a browser that reconnects with the id it
    had keeps its colour and its row in the strip. It is clamped because it is
    also a dictionary key that ends up in JSON.
    """
    cleaned = worksheet_presence.clean_name(raw, limit=64)
    return cleaned or f"p_{secrets.token_urlsafe(8)}"


@router.get("/{token}/events")
async def stream_worksheet_events(
    token: str,
    request: Request,
    session: LinkSession = None,
    participant: Annotated[str | None, Header(alias=PARTICIPANT_HEADER)] = None,
    name: Annotated[str | None, Header(alias=NAME_HEADER)] = None,
) -> StreamingResponse:
    """A guest watching the sheets their link opens.

    A guest has no user identity, so the subscription is `sheet:` audiences and
    nothing else — there is no `user:` key to give them and no file-wide one to
    fall back on. A view-only link streams exactly like an edit link: watching
    somebody work is the accountant-over-your-shoulder case the owner asked
    for, and it is the writes that are refused, not the window.

    **No `DbSession` here, unlike every other route in this file.** A
    request-scoped session would be held open for as long as the browser stays
    on the page — hours — so this route opens its own, resolves the token,
    commits the link's use counter and closes it before the first frame. Same
    reasoning as `resolve_user_from_headers` on the staff side; a stream must
    not hold a connection out of the pool.
    """
    async with SessionLocal() as auth_db:
        try:
            access, _s, _e = await worksheet_links.resolve(
                auth_db, token, session=session, client_ip=_ip(request)
            )
            claimed = worksheet_presence.clean_name(name)
            person = worksheet_presence.register(
                access.worksheet.id,
                participant_id=_participant_id(participant),
                kind="guest",
                # From the link row. The one line in this file that would break
                # the whole design if it read a sheet list off the request.
                sheets=access.sheets,
                display_name=claimed,
                claimed_name=claimed,
                link_id=access.link.id,
            )
            worksheet_id = access.worksheet.id
            await auth_db.commit()
        except HTTPException:
            # An expired session or a revoked link on reconnect is ordinary.
            await auth_db.rollback()
            raise
        except Exception:
            await auth_db.rollback()
            log.exception("worksheet guest stream failed to open")
            raise
    return _sse(_live_stream(request, worksheet_id, person))


@router.post("/{token}/cursor")
async def move_worksheet_cursor(
    token: str, payload: CursorBody, request: Request, db: DbSession, session: LinkSession = None
) -> dict[str, Any]:
    """Say where this tab is. Ephemeral, in-process, and never a value.

    A view-only holder may move a cursor: they are allowed to be here and
    seeing where they are looking is the point of a shared sheet.
    """
    access, _s, _e = await worksheet_links.resolve(
        db, token, session=session, client_ip=_ip(request)
    )
    (kind,) = worksheet_links.require_sheets(access, [payload.sheet])
    person = worksheet_presence.get(access.worksheet.id, payload.participant_id)
    if person is None:
        # No stream, no cursor. The registry is the list of people who are
        # actually here, and a cursor from a tab that is not streaming would
        # put a name on a cell nobody could see leave.
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "not_streaming"})
    worksheet_presence.move_cursor(
        access.worksheet.id, person, sheet_kind=kind, address=payload.address, editing=payload.editing
    )
    await db.commit()
    return {"ok": True}


async def _staff_worksheet(request: Request, profile_id: uuid.UUID, worksheet_id: uuid.UUID, **kw):
    """Resolve the desk side of a live route: user, file, worksheet.

    Authentication runs on a session this function opens and closes itself. A
    request-scoped one would stay open for the life of the stream, which is the
    mistake `resolve_user_from_headers` exists to make impossible to repeat.
    """
    from app.models.financial_worksheet import FinancialWorksheet
    from app.services import application_profiles as profiles

    async with SessionLocal() as auth_db:
        try:
            user = await resolve_user_from_headers(request, db=auth_db, **kw)
            if user.role in _STATEMENT_DENIED_ROLES:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "Only scoped staff may work on a financial statement",
                )
            profile = await profiles.load_profile(auth_db, profile_id, user)
            worksheet = await auth_db.get(FinancialWorksheet, worksheet_id)
            if worksheet is None or worksheet.profile_id != profile.id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Worksheet not found")
            await auth_db.commit()
            return user, worksheet
        except HTTPException as exc:
            await auth_db.rollback()
            log.warning("worksheet live route refused: status=%s", exc.status_code)
            raise
        except Exception:
            await auth_db.rollback()
            log.exception("worksheet live route auth failed")
            raise


def _staff_name(user: Any) -> str:
    return str(getattr(user, "name", None) or str(getattr(user, "email", "") or "").split("@")[0])


@staff_router.get("/{profile_id}/worksheets/{worksheet_id}/events")
async def stream_staff_worksheet_events(
    profile_id: uuid.UUID,
    worksheet_id: uuid.UUID,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
    participant: Annotated[str | None, Header(alias=PARTICIPANT_HEADER)] = None,
) -> StreamingResponse:
    """The desk's own stream. All four sheets, because the desk reads all four.

    There is no name to ask for and none is accepted: a signed-in user is
    already named, and taking a display name from the request would put a
    self-declared string next to proved ones.
    """
    from app.services import sheets

    user, worksheet = await _staff_worksheet(
        request, profile_id, worksheet_id, authorization=authorization, x_dev_user=x_dev_user
    )
    person = worksheet_presence.register(
        worksheet.id,
        participant_id=_participant_id(participant),
        kind="staff",
        sheets=sheets.KINDS,
        display_name=_staff_name(user),
        user_id=user.id,
    )
    return _sse(_live_stream(request, worksheet.id, person))


@staff_router.post("/{profile_id}/worksheets/{worksheet_id}/cursor")
async def move_staff_worksheet_cursor(
    profile_id: uuid.UUID,
    worksheet_id: uuid.UUID,
    payload: CursorBody,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_dev_user: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _user, worksheet = await _staff_worksheet(
        request, profile_id, worksheet_id, authorization=authorization, x_dev_user=x_dev_user
    )
    person = worksheet_presence.get(worksheet.id, payload.participant_id)
    if person is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "not_streaming"})
    worksheet_presence.move_cursor(
        worksheet.id, person, sheet_kind=payload.sheet, address=payload.address, editing=payload.editing
    )
    return {"ok": True}


__all__ = [
    "NAME_HEADER",
    "PARTICIPANT_HEADER",
    "SESSION_HEADER",
    "router",
    "staff_router",
]
