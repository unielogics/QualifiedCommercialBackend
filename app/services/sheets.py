"""Reading and writing the four financial forms as one live worksheet.

The owner asked for "a google sheet type of interface" over the profit and
loss statement, the balance sheet, the business debt schedule and the personal
financial statement — one workbook the desk works in and can share, without a
login, with an outsider (their example was the accountant).

**Nothing here is a new store of figures.** Every cell resolves to a place the
number already lives: `business_financial_statements.body`,
`financial_statements.body`, or a `dos_debts` row. Reads go through the same
per-kind body readers the existing forms use, writes go through the same save
functions, and the shape of a sheet comes from `sheet_layout`, which the
downloadable workbook renders from too. A worksheet that kept its own copy of
the numbers would be a second truth, and the reason to type into the grid at
all is that underwriting reads exactly what the accountant typed.

What this module adds is the two things a live grid needs and four separate
documents cannot have:

- **A clock.** `financial_worksheets.revision` is bumped once per accepted
  batch, under `SELECT … FOR UPDATE` on that row. The lock serialises writers,
  which is what makes read-modify-write on a JSONB body safe when a save is a
  keystroke; the counter orders events. Each statement-backed sheet stores the
  clock reading of its last change in `sheet_rev`; the debt schedule, whose
  rows belong to the file rather than to this worksheet, reads the clock.
- **A save that is a cell, not a form.** `apply_cell_edits` unflattens the
  handful of keys that changed onto the loaded body and hands the whole body
  to the existing save function, so every rule those functions enforce still
  holds — the draft→submitted latch, blank staying null, and above all
  `save_debt_rows`' law that a save writes only the rows its own origin owns.

- **A write that announces itself.** Once a batch is accepted, every cell it
  changed is published to `sheet:{worksheet}:{kind}` — the audience for that
  one sheet, built from the kind that was written rather than from anything the
  request named — carrying the value as *stored*, so the other grids paint what
  is on the file and not what somebody typed. It goes out on `pg_notify` inside
  this transaction: a save that rolls back on the way out announces nothing.
  See `services/worksheet_presence.py` for both lanes.

**Staleness is not a conflict.** A whole-body `version` check is right for a
form somebody fills in and saves once; on a live sheet two people editing
different cells would collide on it constantly and there would be nothing
either could do. So a stale `base_rev` is accepted and the write lands
(last-write-wins per cell, which is visible because the other person's cursor
is sitting in the cell). One exception: a client more than `STALE_LIMIT`
batches behind is not behind, it is disconnected, and gets a single 409 telling
it to reload.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.financial_worksheet import FinancialWorksheet
from app.services import business_statement_schema as bss
from app.services import business_statements, financial_statements, pfs_schema, sheet_layout

KINDS: tuple[str, ...] = sheet_layout.KINDS

#: How far behind a client may be before it is told to reload rather than
#: patched. Two people typing produce a few revisions a second; two hundred is
#: minutes of divergence, which is a dropped connection, not a race.
STALE_LIMIT = 200

#: Blank lines offered past the end of each list, so there is always somewhere
#: to type. They are addressed by ordinal — `r{n}` where n is the position
#: *after* the stored rows — which is exactly how `sheet_layout._set` appends,
#: so typing into one creates the row and stored rows keep their own ids.
BLANK_DEBT_ROWS = 3
BLANK_SCHEDULE_ROWS = 2

#: Where `sheet_rev` lives, per sheet. Addressed as plain tables rather than
#: through the ORM: the column is bookkeeping for this module, and no other
#: reader of a statement should have to know it exists.
_REV_TABLE = {
    "p_and_l": sa.table(
        "business_financial_statements", sa.column("id"), sa.column("sheet_rev")
    ),
    "balance_sheet": sa.table(
        "business_financial_statements", sa.column("id"), sa.column("sheet_rev")
    ),
    "pfs": sa.table("financial_statements", sa.column("id"), sa.column("sheet_rev")),
}


class _Loaded:
    """One sheet as it currently stands: the body a save would patch, the row
    that holds it, and how it is reported."""

    __slots__ = ("body", "statement", "status", "completed", "rev")

    def __init__(
        self,
        body: dict[str, Any],
        statement: Any,
        *,
        status: str,
        completed: bool,
        rev: int,
    ) -> None:
        self.body = body
        self.statement = statement
        self.status = status
        self.completed = completed
        self.rev = rev



log = logging.getLogger(__name__)

def _json(value: Any) -> Any:
    """A computed figure as JSON. Decimal is the only surprise — every total in
    the three schemas is one, and `float` on the wire is what the grid formats."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _computed(kind: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: _json(value) for key, value in sheet_layout.totals(kind, body).items()}


def _kind_or_400(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown sheet")
    return kind


def _row_keys(rows: Sequence[Any], blanks: int) -> list[str]:
    """The stored rows by identity, then `blanks` empty lines addressed by the
    position each would occupy."""
    keys = [
        sheet_layout._list_row_key(row, index)
        for index, row in enumerate(rows or [], start=1)
    ]
    return keys + [f"r{n}" for n in range(len(keys) + 1, len(keys) + 1 + blanks)]


def _layout_for(kind: str, body: Mapping[str, Any] | None) -> sheet_layout.Sheet:
    """The sheet's shape for the grid: the body's own rows, plus blank lines."""
    body = body or {}
    if kind == "debt_schedule":
        return sheet_layout.layout(
            kind, debt_rows=_row_keys(body.get("debts") or [], BLANK_DEBT_ROWS)
        )
    if kind == "pfs":
        schedules = pfs_schema.normalize_schedule_rows(dict(body))
        return sheet_layout.layout(
            kind,
            schedule_rows={
                spec.key: _row_keys(schedules.get(spec.key) or [], BLANK_SCHEDULE_ROWS)
                for spec in pfs_schema.SCHEDULES
            },
        )
    return sheet_layout.layout(kind)


def _cell_payload(cell: sheet_layout.Cell) -> dict[str, Any]:
    out: dict[str, Any] = {"c": cell.c, "type": cell.type, "format": cell.format}
    if cell.key is not None:
        out["key"] = cell.key
    if cell.xlsx_name is not None:
        out["xlsx_name"] = cell.xlsx_name
    if cell.editable:
        out["editable"] = True
    if cell.label is not None:
        out["label"] = cell.label
    if cell.options:
        out["options"] = list(cell.options)
    if cell.compute is not None:
        out["compute"] = cell.compute
    if cell.source is not None:
        out["source"] = cell.source
    if cell.hint is not None:
        out["hint"] = cell.hint
    if cell.flags:
        out["flags"] = list(cell.flags)
    if cell.emphasis:
        out["emphasis"] = True
    if cell.colspan != 1:
        out["colspan"] = cell.colspan
    return out


def _row_payload(row: sheet_layout.Row) -> dict[str, Any]:
    out: dict[str, Any] = {"r": row.r, "kind": row.kind}
    if row.label is not None:
        out["label"] = row.label
    if row.block is not None:
        out["block"] = row.block
    if row.row_key is not None:
        out["row_key"] = row.row_key
    if row.ordinal is not None:
        out["ordinal"] = row.ordinal
    out["cells"] = [_cell_payload(cell) for cell in row.cells]
    return out


def _sheet_shape(sheet: sheet_layout.Sheet) -> dict[str, Any]:
    return {
        "columns": [
            {"c": column.c, "label": column.label, "width": column.width, "align": column.align}
            for column in sheet.columns
        ],
        "freeze": {"rows": sheet.freeze[0], "cols": sheet.freeze[1]},
        "rows": [_row_payload(row) for row in sheet.rows],
    }


def _row_meta(kind: str, body: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Which debt rows this caller may actually write, by row key.

    The schedule is one list shown to everybody and writable in parts: a save
    touches only the rows its own origin owns, so a grid that let somebody type
    into a borrower's row would be showing an edit that will bounce.
    `debt_body_for_profile` already decides this per row; it is carried through
    rather than recomputed. Every other sheet is a single document and has
    nothing per row to say.
    """
    if kind != "debt_schedule":
        return {}
    return {
        str(row["id"]): {
            "editable": bool(row.get("editable", True)),
            "owner": row.get("owner"),
        }
        for row in (body or {}).get("debts") or []
        if isinstance(row, dict) and row.get("id")
    }


def _schema_for(kind: str) -> dict[str, Any]:
    """The field description the browser already renders the stacked forms
    from, shipped beside the grid so one totals engine serves both."""
    if kind in bss.KINDS:
        return bss.describe(kind)
    if kind == "pfs":
        return pfs_schema.describe()
    return {"kind": "debt_schedule", "columns": list(financial_statements.DEBT_COLUMNS)}


# ---------------------------------------------------------------------------
# The worksheet row
# ---------------------------------------------------------------------------


async def worksheet_for_profile(
    db: AsyncSession, profile: ApplicationProfile
) -> FinancialWorksheet | None:
    """The file's worksheet. One per file: the four forms are one workbook, and
    a second clock over the same figures would order nothing."""
    return (
        await db.execute(
            select(FinancialWorksheet)
            .where(FinancialWorksheet.profile_id == profile.id)
            .order_by(FinancialWorksheet.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def ensure_worksheet(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    created_by: uuid.UUID | None = None,
    packet_id: uuid.UUID | None = None,
) -> FinancialWorksheet:
    worksheet = await worksheet_for_profile(db, profile)
    if worksheet is not None:
        return worksheet
    worksheet = FinancialWorksheet(
        profile_id=profile.id, created_by=created_by, packet_id=packet_id, revision=0
    )
    db.add(worksheet)
    await db.flush()
    return worksheet


async def _lock(db: AsyncSession, worksheet: FinancialWorksheet) -> FinancialWorksheet:
    """Take the worksheet's row lock. Every write to any of the four sheets
    goes through here, so two writers serialise instead of losing one another's
    read-modify-write on the same JSONB body."""
    locked = (
        await db.execute(
            select(FinancialWorksheet)
            .where(FinancialWorksheet.id == worksheet.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    return locked or worksheet


async def _read_sheet_rev(db: AsyncSession, kind: str, statement_id: Any) -> int:
    table = _REV_TABLE.get(kind)
    if table is None or statement_id is None:
        return 0
    found = (
        await db.execute(select(table.c.sheet_rev).where(table.c.id == statement_id))
    ).scalar()
    return int(found or 0)


async def _stamp_sheet_rev(db: AsyncSession, kind: str, statement_id: Any, rev: int) -> None:
    table = _REV_TABLE.get(kind)
    if table is None or statement_id is None:
        return
    await db.execute(sa.update(table).where(table.c.id == statement_id).values(sheet_rev=rev))


# ---------------------------------------------------------------------------
# Loading a sheet
# ---------------------------------------------------------------------------


async def _load(
    db: AsyncSession,
    profile: ApplicationProfile,
    kind: str,
    *,
    origin: str,
    prefill: Mapping[str, Any],
    worksheet: FinancialWorksheet,
) -> _Loaded:
    """One sheet's current body, through the reader that already owns it."""
    if kind in bss.KINDS:
        body, statement = await business_statements.body_for_profile(
            db, profile, kind, dict(prefill)
        )
        state = statement.status if statement else "draft"
        return _Loaded(
            dict(body),
            statement,
            status=state,
            completed=state == "submitted",
            rev=await _read_sheet_rev(db, kind, statement.id if statement else None),
        )

    if kind == "pfs":
        statement = await financial_statements.latest_for_profile(db, profile.id)
        body = dict((statement.body if statement else None) or pfs_schema.empty_body())
        body = financial_statements.seed_pfs_applicant(body, dict(prefill))
        # The 413's "as of" line lives in the body now; a statement stored
        # before it did still has the date in its own column, so seed from
        # there rather than showing the sheet undated.
        if not str(body.get("as_of") or "").strip() and getattr(statement, "statement_date", None):
            body["as_of"] = statement.statement_date.isoformat()
        state = statement.status if statement else "draft"
        return _Loaded(
            body,
            statement,
            status=state,
            completed=state == "submitted",
            rev=await _read_sheet_rev(db, kind, statement.id if statement else None),
        )

    body = await financial_statements.debt_body_for_profile(db, profile, origin=origin)
    if not str(body.get("business_name") or "").strip() and prefill.get("business_name"):
        body = {**body, "business_name": prefill["business_name"]}
    # The schedule is a live list on the file, not a document with a submit
    # latch, so it has no submitted state to report — and no statement row to
    # carry a rev, which is why it reads the workbook clock.
    return _Loaded(dict(body), None, status="draft", completed=False, rev=worksheet.revision)


def _scoped(kinds: Iterable[str] | None) -> list[str]:
    """The kinds asked for, in workbook order, with anything unknown dropped.

    Scope is decided by the caller from the link or the user — never from
    client input — and this only orders it. **`None` means all four; an empty
    selection means none.** Collapsing the two would turn a link whose stored
    scope rows had been deleted into a link that opens everything, which is
    the failure this whole mechanism exists to prevent.
    """
    wanted = set(KINDS if kinds is None else kinds)
    return [kind for kind in KINDS if kind in wanted]


async def read_sheets(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    kinds: Iterable[str] | None = None,
    origin: str = "admin",
    can_edit: bool = True,
    worksheet: FinancialWorksheet | None = None,
) -> dict[str, Any]:
    """The whole workbook in one answer: shape, values and computed figures.

    One call rather than four, because a tab bar that cannot see what the other
    three hold cannot tell you which of them is still empty — and because scope
    must be applied once, on the server. A sheet outside `kinds` is not
    returned at all: a link that omits the personal financial statement never
    puts its figures on the wire.
    """
    scope = _scoped(kinds)
    worksheet = worksheet or await ensure_worksheet(db, profile)
    prefill = await financial_statements.form_prefill(db, profile)

    sheets: list[dict[str, Any]] = []
    for kind in scope:
        loaded = await _load(
            db, profile, kind, origin=origin, prefill=prefill, worksheet=worksheet
        )
        shape = _layout_for(kind, loaded.body)
        sheets.append(
            {
                "kind": kind,
                "title": shape.title,
                "schema_version": shape.schema_version,
                "status": loaded.status,
                "completed": loaded.completed,
                "rev": loaded.rev,
                **_sheet_shape(shape),
                "schema": _schema_for(kind),
                "values": sheet_layout.flatten(kind, loaded.body),
                "row_meta": _row_meta(kind, loaded.body),
                "computed": _computed(kind, loaded.body),
            }
        )

    return {
        "layout_version": sheet_layout.LAYOUT_VERSION,
        "worksheet_id": worksheet.id,
        "revision": worksheet.revision,
        "scope": {
            "can_edit": bool(can_edit),
            "sheets": scope,
            # Which tab opens first. The first sheet in scope, so a link that
            # only opens the debt schedule does not land on an empty P&L.
            "open_at": scope[0] if scope else None,
        },
        "business_name": prefill.get("business_name"),
        "prefill": prefill,
        "sheets": sheets,
    }


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _by(
    worksheet: FinancialWorksheet | None,
    *,
    participant_id: str | None,
    actor_user_id: uuid.UUID | None,
) -> dict[str, Any] | None:
    """Who to put on the cell label, if they have a stream open.

    A guest names their own tab; a staff writer is looked up by user id,
    because the write endpoint knows who the actor is but not which tab they
    are in. Nobody streaming means no label — the value still lands, it just
    arrives unattributed, which is the right answer for a write that came from
    a script or a second window that is not watching.
    """
    if worksheet is None or getattr(worksheet, "id", None) is None:
        return None
    from app.services import worksheet_presence

    participant = None
    if participant_id:
        participant = worksheet_presence.touch(worksheet.id, participant_id)
    if participant is None:
        participant = worksheet_presence.by_user(worksheet.id, actor_user_id)
    return participant.by() if participant is not None else None


async def _announce(
    db: AsyncSession, worksheet: FinancialWorksheet | None, events: Sequence[dict[str, Any]]
) -> None:
    """Broadcast what was just written — **after** it was accepted, never before.

    The carrier is `pg_notify` on this session, so these ride the same
    transaction as the write itself: a save that rolls back on the way out
    announces nothing, and every other browser is spared a value that does not
    exist. The audience on each event is `sheet:{worksheet}:{kind}` for the one
    sheet the cell is on, built server-side in `worksheet_presence` from the
    kind that was actually written — never from anything the request named.
    """
    if worksheet is None or getattr(worksheet, "id", None) is None or not events:
        return
    from app.services import worksheet_presence

    for event in events:
        await worksheet_presence.publish_sheet_event(db, event)


def _group(edits: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """`[{sheet, key, value}]` as `{kind: {key: value}}`, later edits winning.

    Last write wins per cell, and within one batch that means the last one
    sent. Two edits to *different* cells both land, which is the whole reason
    the grain is a cell and not a body.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for edit in edits or []:
        kind = _kind_or_400(str((edit.get("sheet") if isinstance(edit, Mapping) else None) or ""))
        key = str(edit.get("key") or "").strip()
        if not key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "An edit needs a cell key")
        grouped.setdefault(kind, {})[key] = edit.get("value")
    return grouped


def _check_stale(worksheet: FinancialWorksheet, kinds: Iterable[str], base_rev: Mapping[str, Any] | None) -> None:
    """One 409, and only for a client that has stopped listening.

    A stale `base_rev` is normal — it is what "somebody else typed while you
    were typing" looks like — and refusing it would 409 on keystrokes. Being
    hundreds of revisions behind is a different fact: the connection dropped,
    the client's picture of the sheet is not a slightly old one, and patching
    it would show figures nobody entered.
    """
    for kind in kinds:
        raw = (base_rev or {}).get(kind)
        if raw is None:
            continue
        try:
            base = int(raw)
        except (TypeError, ValueError):
            continue
        if worksheet.revision - base > STALE_LIMIT:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "stale_worksheet", "current_revision": worksheet.revision},
            )


async def _save_kind(
    db: AsyncSession,
    profile: ApplicationProfile,
    kind: str,
    loaded: _Loaded,
    body: dict[str, Any],
    *,
    origin: str,
    actor_user_id: uuid.UUID | None,
) -> Any:
    """Hand the patched body to the function that already owns saving it.

    Nothing is reimplemented here on purpose. `business_statements.save` keeps
    a submitted statement submitted and rewrites the derived columns;
    `save_statement` does the same for the 413; `save_debt_rows` writes only
    the rows this origin owns, updating them in place and leaving another
    source's rows matched but untouched. A grid that wrote rows itself would
    lose all three rules on its first keystroke.
    """
    if kind in bss.KINDS:
        return await business_statements.save(
            db,
            profile,
            kind=kind,
            body=body,
            status="draft",
            actor_user_id=actor_user_id,
            statement=loaded.statement,
        )
    if kind == "pfs":
        return await financial_statements.save_statement(
            db,
            profile,
            body=body,
            status=loaded.status,
            actor_user_id=actor_user_id,
            statement=loaded.statement,
        )
    await financial_statements.save_debt_rows(
        db, profile, financial_statements.debt_rows_from_body(body), origin=origin
    )
    return None


async def _stored_body(
    db: AsyncSession,
    profile: ApplicationProfile,
    kind: str,
    saved: Any,
    body: dict[str, Any],
    *,
    origin: str,
) -> dict[str, Any]:
    """What is actually on the file now. For the two statements and the 413 the
    body was stored verbatim; the debt schedule is re-read, because its save
    normalises figures, drops a line nobody filled in, and refuses a row this
    origin does not own — all of which the grid needs to be told about."""
    if kind == "debt_schedule":
        return await financial_statements.debt_body_for_profile(db, profile, origin=origin)
    return dict(getattr(saved, "body", None) or body)


async def refresh_touched_pdfs(
    db: AsyncSession,
    profile: ApplicationProfile,
    kinds: Sequence[str],
    *,
    actor_name: str | None = None,
    actor_email: str | None = None,
) -> None:
    """Put today's figures behind each form's PDF, after the save is committed.

    The AI reads a form through the document filed for it — `classification`
    and `key_facts` on the analysis row, never the statement table — so a
    worksheet that only wrote the database would leave the extractors, the
    intelligence cards and the lender packet reading whatever the borrower had
    when they last pressed Send. Refreshing here keeps them current.

    **After the commit, never inside it.** Rendering a PDF is WeasyPrint and an
    S3 put; neither belongs in the transaction that holds the cell the person
    just typed. It is also allowed to fail: a document that could not be
    re-rendered is a stale document, which is a far smaller problem than a save
    that came back as an error because a renderer did.
    """
    from app.services import drafted_forms

    for kind in dict.fromkeys(kinds):
        if kind not in KINDS:
            continue
        try:
            await drafted_forms.refresh_saved_form(
                db, profile, kind, actor_name=actor_name, actor_email=actor_email
            )
        except Exception:  # noqa: BLE001 - a stale PDF must never fail a save
            log.exception("sheets: could not refresh the %s document", kind)
            await db.rollback()


async def apply_cell_edits(
    db: AsyncSession,
    profile: ApplicationProfile,
    edits: Sequence[Mapping[str, Any]],
    *,
    base_rev: Mapping[str, Any] | None = None,
    origin: str = "admin",
    actor_user_id: uuid.UUID | None = None,
    worksheet: FinancialWorksheet | None = None,
    participant_id: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Write a handful of cells and say where the workbook now stands.

    Returns `{rev, computed, resync}`. `resync` names the sheets whose stored
    value is not what was sent — a figure normalised, a blank line dropped, a
    row another source owns — so the grid takes the server's answer for those
    rather than leaving the typed text on screen as though it had been kept.
    """
    grouped = _group(edits)
    worksheet = worksheet or await ensure_worksheet(db, profile, created_by=actor_user_id)
    worksheet = await _lock(db, worksheet)
    _check_stale(worksheet, grouped, base_rev)

    if not grouped:
        return {"rev": {"debt_schedule": worksheet.revision}, "computed": {}, "resync": []}

    prefill = await financial_statements.form_prefill(db, profile)
    new_rev = int(worksheet.revision) + 1
    revs: dict[str, int] = {}
    computed: dict[str, dict[str, Any]] = {}
    resync: list[str] = []
    by = _by(worksheet, participant_id=participant_id, actor_user_id=actor_user_id)
    events: list[dict[str, Any]] = []
    from app.services import worksheet_presence

    for kind, values in grouped.items():
        loaded = await _load(
            db, profile, kind, origin=origin, prefill=prefill, worksheet=worksheet
        )
        try:
            body = sheet_layout.unflatten(kind, values, loaded.body)
        except KeyError as exc:
            # A key the layout does not know. Refused rather than stored
            # somewhere nobody reads it back from.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Unknown cell {exc.args[0]!r} on {kind}"
            ) from exc
        saved = await _save_kind(
            db, profile, kind, loaded, body, origin=origin, actor_user_id=actor_user_id
        )
        stored = await _stored_body(db, profile, kind, saved, body, origin=origin)
        computed[kind] = _computed(kind, stored)
        flat = sheet_layout.flatten(kind, stored)
        if any(flat.get(key, "") != sheet_layout._as_text(value) for key, value in values.items()):
            resync.append(kind)
        statement_id = getattr(saved, "id", None) or getattr(loaded.statement, "id", None)
        await _stamp_sheet_rev(db, kind, statement_id, new_rev)
        revs[kind] = new_rev

        for key, value in values.items():
            # What is stored, not what was typed: a figure the save normalised
            # or refused would otherwise be broadcast as though it had been
            # kept. An appended row is addressed by its ordinal key until it
            # exists, so the typed text is the fallback for exactly that case.
            events.append(
                worksheet_presence.cell_event(
                    worksheet.id,
                    sheet_kind=kind,
                    key=key,
                    value=flat.get(key, sheet_layout._as_text(value)),
                    revision=new_rev,
                    by=by,
                    origin_client_id=client_id,
                )
            )

    worksheet.revision = new_rev
    await db.flush()
    await _announce(db, worksheet, events)
    # The debt schedule reads the clock rather than a stored rev, so it is
    # always reported: otherwise a client that only ever edits the P&L would
    # drift behind on it until it tripped the stale limit for no reason.
    revs["debt_schedule"] = new_rev
    return {"rev": revs, "computed": computed, "resync": resync}


async def apply_row_op(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    kind: str,
    op: str,
    row_id: str | None = None,
    after: str | None = None,
    block: str | None = None,
    origin: str = "admin",
    actor_user_id: uuid.UUID | None = None,
    worksheet: FinancialWorksheet | None = None,
    participant_id: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Add or remove a line on one of the two list-shaped sheets.

    **An added debt row is not written to the file.** It comes back as an
    addressable blank line and becomes a real `dos_debts` row the moment
    somebody types into it. Persisting it empty would mean inventing an
    obligation — and `count_in_dscr` defaults to true, so a phantom row lands
    in the debt-service denominator and understates coverage. A blank line on
    the personal financial statement carries no such weight and is stored.
    """
    _kind_or_400(kind)
    if kind not in ("debt_schedule", "pfs"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sheet has a fixed row list")
    if op not in ("insert", "delete"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown row operation")

    worksheet = worksheet or await ensure_worksheet(db, profile, created_by=actor_user_id)
    worksheet = await _lock(db, worksheet)
    prefill = await financial_statements.form_prefill(db, profile)
    loaded = await _load(db, profile, kind, origin=origin, prefill=prefill, worksheet=worksheet)
    from app.services import worksheet_presence

    by = _by(worksheet, participant_id=participant_id, actor_user_id=actor_user_id)

    def _row_announcement(row_id: str | None, revision: int) -> dict[str, Any]:
        return worksheet_presence.row_event(
            worksheet.id,
            event_type="row.inserted" if op == "insert" else "row.deleted",
            sheet_kind=kind,
            row_id=row_id,
            block=block,
            after=after,
            revision=revision,
            by=by,
            origin_client_id=client_id,
        )

    if kind == "pfs":
        spec = pfs_schema.SCHEDULES_BY_KEY.get(str(block or ""))
        if spec is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown schedule")
        body = dict(loaded.body)
        schedules = dict(pfs_schema.normalize_schedule_rows(body))
        rows = list(schedules.get(spec.key) or [])
        was = [str((row or {}).get("id") or "") for row in rows]
        rows = _apply_rows(rows, op=op, row_id=row_id, after=after)
        # Which line this was about, for the broadcast: on a delete it is the
        # one asked for, on an insert it is the id `_apply_rows` minted, and
        # the only honest way to learn that is to diff.
        touched = row_id or next(
            (str((row or {}).get("id") or "") for row in rows
             if str((row or {}).get("id") or "") not in was),
            None,
        )
        schedules[spec.key] = rows
        body["schedules"] = schedules
        saved = await financial_statements.save_statement(
            db,
            profile,
            body=body,
            status=loaded.status,
            actor_user_id=actor_user_id,
            statement=loaded.statement,
        )
        worksheet.revision = int(worksheet.revision) + 1
        await _stamp_sheet_rev(db, kind, getattr(saved, "id", None), worksheet.revision)
        await db.flush()
        await _announce(db, worksheet, [_row_announcement(touched, worksheet.revision)])
        stored = dict(getattr(saved, "body", None) or body)
        return {
            "rows": _row_payloads(kind, stored),
            "row_meta": _row_meta(kind, stored),
            "rev": {kind: worksheet.revision, "debt_schedule": worksheet.revision},
        }

    rows = list(loaded.body.get("debts") or [])
    if op == "insert":
        fresh = {"id": str(uuid.uuid4()), "editable": True, "owner": origin}
        rows = _apply_rows(rows, op=op, row_id=fresh["id"], after=after, blank=fresh)
        # Not saved: an empty line is not an obligation. It persists on the
        # first keystroke, through the same `save_debt_rows` every other edit
        # takes, which will insert it under this origin.
        body = {**loaded.body, "debts": rows}
        # Announced anyway, and at the unchanged revision: the other grids draw
        # the same blank line under the same id, so the first keystroke lands
        # in a row everybody already has rather than appearing out of nowhere.
        await _announce(db, worksheet, [_row_announcement(fresh["id"], worksheet.revision)])
        return {
            "rows": _row_payloads(kind, body),
            "row_meta": _row_meta(kind, body),
            "rev": {kind: worksheet.revision},
        }

    rows = _apply_rows(rows, op=op, row_id=row_id, after=after)
    await financial_statements.save_debt_rows(
        db,
        profile,
        financial_statements.debt_rows_from_body({"debts": rows}),
        origin=origin,
    )
    worksheet.revision = int(worksheet.revision) + 1
    await db.flush()
    await _announce(db, worksheet, [_row_announcement(row_id, worksheet.revision)])
    stored = await financial_statements.debt_body_for_profile(db, profile, origin=origin)
    return {
        "rows": _row_payloads(kind, stored),
        "row_meta": _row_meta(kind, stored),
        "rev": {kind: worksheet.revision},
    }


def _apply_rows(
    rows: list[Any],
    *,
    op: str,
    row_id: str | None,
    after: str | None,
    blank: dict[str, Any] | None = None,
) -> list[Any]:
    if op == "delete":
        if not row_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Which row?")
        found = [row for row in rows if str((row or {}).get("id") or "") != str(row_id)]
        if len(found) == len(rows):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Row not found")
        return found
    new = dict(blank or {"id": str(uuid.uuid4())})
    if not after:
        return [*rows, new]
    at = next(
        (index for index, row in enumerate(rows) if str((row or {}).get("id") or "") == str(after)),
        len(rows) - 1,
    )
    return [*rows[: at + 1], new, *rows[at + 1 :]]


def _row_payloads(kind: str, body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The sheet's row list after a row operation, in the same shape the read
    returns, so the grid replaces rows rather than reconciling two shapes."""
    shape = _layout_for(kind, body)
    values = sheet_layout.flatten(kind, body)
    return [
        {**_row_payload(row), "values": {
            cell.key: values.get(cell.key, "") for cell in row.cells if cell.key
        }}
        for row in shape.rows
    ]


__all__ = [
    "BLANK_DEBT_ROWS",
    "BLANK_SCHEDULE_ROWS",
    "KINDS",
    "STALE_LIMIT",
    "apply_cell_edits",
    "apply_row_op",
    "ensure_worksheet",
    "read_sheets",
    "worksheet_for_profile",
]
