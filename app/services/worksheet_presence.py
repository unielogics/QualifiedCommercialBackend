"""Who is in the worksheet right now, and what the wire says while they type.

Two lanes, and the split is the whole design:

**Lane A — the figures.** A cell that changed is a fact about the file. It
rides `publish_communication_event`'s carrier, Postgres `LISTEN/NOTIFY`, which
fires inside the writer's own transaction — so a save that rolled back never
announces itself, and a second worker would still hear it. `cell_event` and
`row_event` below build those payloads; `publish_sheet_event` sends them, and
`services/sheets.py` calls it after a write has been accepted, never before.

**Lane B — the people.** Presence and cursors have no row, nothing to commit
and no worth after a restart, so they go through `publish_ephemeral`, which is
in-process. That is correct while the API runs `--workers 1` and it is the
first thing that breaks at two: two people on different workers would simply
stop seeing each other, with no error anywhere. Every call site here says so.

**The audience invariant.** Every payload this module builds is addressed to
`sheet_audience(worksheet_id, kind)` — one key per *sheet*, never one per
worksheet — and the kinds are always the ones the caller proved: the sheet
rows of the resolved link, or a staff user's access to the file. **No audience
is ever computed from anything in the request body.** A link that opens the
P&L and not the personal financial statement must not receive PFS cell values,
and the audience key is the only thing standing between the two. Cell values
are broadcast precisely because the audience equals the read scope; break that
equality and the broadcast becomes a leak.

**The connection is the presence.** A participant is registered when their
event stream opens and dropped in the generator's `finally`. There are no
join/leave endpoints, because a join endpoint is a state machine that drifts
from reality the first time a tab crashes. The one gap that leaves is a
half-open TCP connection — a laptop lid closed on wifi holds the socket for
minutes — so `sweep_once` drops anyone unheard from for `STALE_AFTER` and
announces their leave. It runs as a plain asyncio task, deliberately not an
APScheduler job: APScheduler is the reason `--workers 1` exists and this must
not be added to that pile.

**A claimed name is not an identity.** A guest types what to call themselves on
a page with no login. It is clamped, stripped of control characters, kept only
in the dictionary below — never in a table — and where it reaches the audit
trail it lands in a field named `claimed_name`, beside the proved facts, never
merged into an actor field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import sheet_layout
from app.services.communication_events import (
    CHANNEL,
    broker,
    publish_ephemeral,
    sheet_audience,
)

log = logging.getLogger(__name__)

#: Eight colours, named because the name is what disambiguates two anonymous
#: guests: a bare "Guest" twice over tells you nothing, "Guest (amber)" and
#: "Guest (teal)" tell you which cursor is whose.
COLOURS: tuple[str, ...] = (
    "blue",
    "amber",
    "teal",
    "violet",
    "coral",
    "green",
    "indigo",
    "rose",
)

#: A worksheet is four financial forms, not a webinar. Twenty is far past any
#: real desk-plus-accountant session and well inside what one in-process fan-out
#: can carry; past it the 21st connection is refused with a sentence that says
#: what happened rather than a broken stream.
MAX_PARTICIPANTS = 20

#: Unheard-from for this long and you are gone. `last_seen_at` is bumped by the
#: stream opening, by a cursor POST and by an accepted write, so a person who is
#: actually there refreshes it several times a minute.
STALE_AFTER = timedelta(seconds=90)
SWEEP_SECONDS = 30

#: What a broadcast cell value is clipped to. In the spirit of the production
#: package's `_clip`: it keeps every event comfortably inside `pg_notify`'s
#: 8000-byte payload cap, and `truncated` tells the receiver to re-read that one
#: cell rather than paint half a value.
VALUE_LIMIT = 500

#: A claimed name is a label, not prose.
NAME_LIMIT = 40

#: The only keys a cursor address may carry. A cursor is a position; anything
#: else on that wire — above all a half-typed number — would make the cursor
#: stream keystroke-level surveillance of somebody entering their net worth.
CURSOR_ADDRESS_KEYS = (
    "section_key",
    "row_key",
    "row_id",
    "column_key",
    "schedule_key",
    "key",
    "r",
    "c",
)
_CURSOR_VALUE_LIMIT = 80

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


@dataclass
class Participant:
    """One open event stream. Not one person and not one session — one tab."""

    participant_id: str
    display_name: str
    kind: str  # "staff" | "guest"
    colour: int
    sheets: frozenset[str]
    #: Proved. A staff user's id, or the link row a guest resolved.
    user_id: uuid.UUID | None = None
    link_id: uuid.UUID | None = None
    #: Self-declared, guests only. Held here and nowhere else.
    claimed_name: str | None = None
    joined_at: datetime = field(default_factory=_now)
    last_seen_at: datetime = field(default_factory=_now)
    cursor: dict[str, Any] | None = None

    @property
    def colour_name(self) -> str:
        return COLOURS[self.colour % len(COLOURS)]

    def public(self) -> dict[str, Any]:
        """What the other browsers are told. No user id, no link id: the
        presence strip needs a name, a colour and which sheets to draw on."""
        return {
            "participant_id": self.participant_id,
            "name": self.display_name,
            "kind": self.kind,
            "colour": self.colour,
            "colour_name": self.colour_name,
            "sheets": sorted(self.sheets),
            "joined_at": self.joined_at.isoformat(),
            "cursor": self.cursor,
        }

    def by(self) -> dict[str, Any]:
        """The `by` block on a cell event: who typed it, as the grid labels it."""
        return {
            "participant_id": self.participant_id,
            "name": self.display_name,
            "colour": self.colour,
        }


_rooms: dict[uuid.UUID, dict[str, Participant]] = {}


def room(worksheet_id: uuid.UUID | str) -> dict[str, Participant]:
    return _rooms.get(_key(worksheet_id), {})


def _key(worksheet_id: uuid.UUID | str) -> uuid.UUID:
    if isinstance(worksheet_id, uuid.UUID):
        return worksheet_id
    return uuid.UUID(str(worksheet_id))


def clear() -> None:
    """Tests share a process, and so does a reload. Drops every room."""
    _rooms.clear()


def clean_name(raw: Any, *, limit: int = NAME_LIMIT) -> str | None:
    """A self-declared name, made safe to store and render.

    Control characters out (a newline in a presence strip is a broken layout at
    best), unicode normalised, clamped. React escapes on render; this is for
    everything that is not React — the audit row, a log line, a tooltip built
    by concatenation.
    """
    if raw is None:
        return None
    value = unicodedata.normalize("NFC", str(raw))
    value = _CONTROL.sub(" ", value).strip()
    value = re.sub(r"\s+", " ", value)
    if not value:
        return None
    return value[:limit].strip() or None


def colour_for(worksheet_id: uuid.UUID | str, key: str) -> int:
    """Deterministic first, deduplicated second.

    `crc32(key) % 8` means a person keeps their colour across a reconnect, a
    reload and a redeploy — the thing round-robin gets wrong is that somebody
    *else* leaving changes your colour, which is disorienting in exactly the
    moment you are looking at the strip to see who left. Only on a collision
    inside one worksheet does the next free index win.
    """
    wanted = zlib.crc32(str(key).encode("utf-8")) % len(COLOURS)
    taken = {p.colour for p in room(worksheet_id).values()}
    if wanted not in taken:
        return wanted
    for step in range(1, len(COLOURS)):
        candidate = (wanted + step) % len(COLOURS)
        if candidate not in taken:
            return candidate
    return wanted


def colour_key(*, user_id: Any = None, link_id: Any = None, participant_id: str = "") -> str:
    """Staff are the same person in every tab; a guest is a browser.

    A staff colour keys off the user id, so the underwriter is the same blue on
    their laptop and their second screen. A guest has no identity to key off —
    two people can hold the same link — so it is the link plus the tab.
    """
    if user_id:
        return str(user_id)
    return f"{link_id}:{participant_id}"


def full(worksheet_id: uuid.UUID | str) -> bool:
    return len(room(worksheet_id)) >= MAX_PARTICIPANTS


def too_many() -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "worksheet_full",
            "message": (
                f"This worksheet already has {MAX_PARTICIPANTS} people in it. "
                "Ask one of them to close their tab and try again."
            ),
        },
    )


def register(
    worksheet_id: uuid.UUID | str,
    *,
    participant_id: str,
    kind: str,
    sheets: Any,
    display_name: str | None = None,
    claimed_name: str | None = None,
    user_id: uuid.UUID | None = None,
    link_id: uuid.UUID | None = None,
) -> Participant:
    """Add a participant, or refresh the one already under that id.

    Called from the route *before* the streaming response is returned, so the
    cap can be answered with a status code rather than a stream that dies for
    no stated reason. If the browser then goes away before the generator runs,
    the sweeper collects the record — which is why the sweeper exists.
    """
    key = _key(worksheet_id)
    people = _rooms.setdefault(key, {})
    existing = people.get(participant_id)
    if existing is None and len(people) >= MAX_PARTICIPANTS:
        raise too_many()

    claimed = clean_name(claimed_name)
    if existing is not None:
        # A reconnect keeps its colour and its joined_at: it is the same tab.
        existing.sheets = frozenset(str(s) for s in (sheets or ()))
        existing.claimed_name = claimed or existing.claimed_name
        existing.display_name = display_name or existing.display_name
        existing.last_seen_at = _now()
        return existing

    colour = colour_for(
        key, colour_key(user_id=user_id, link_id=link_id, participant_id=participant_id)
    )
    name = clean_name(display_name) or claimed
    participant = Participant(
        participant_id=participant_id,
        display_name=name or f"Guest ({COLOURS[colour]})",
        kind=kind,
        colour=colour,
        sheets=frozenset(str(s) for s in (sheets or ())),
        user_id=user_id,
        link_id=link_id,
        claimed_name=claimed,
    )
    people[participant.participant_id] = participant
    return participant


def get(worksheet_id: uuid.UUID | str, participant_id: str) -> Participant | None:
    return room(worksheet_id).get(participant_id)


def by_user(worksheet_id: uuid.UUID | str, user_id: Any) -> Participant | None:
    """The staff participant for a user, if they have a stream open.

    Used to name a write: the staff write endpoint knows who the actor is but
    not which tab they are in, and a cell event with no `by` block draws no
    label on the cell.
    """
    if not user_id:
        return None
    for participant in room(worksheet_id).values():
        if participant.user_id and str(participant.user_id) == str(user_id):
            return participant
    return None


def touch(worksheet_id: uuid.UUID | str, participant_id: str | None) -> Participant | None:
    participant = get(worksheet_id, participant_id or "")
    if participant is not None:
        participant.last_seen_at = _now()
    return participant


def forget(worksheet_id: uuid.UUID | str, participant_id: str) -> Participant | None:
    key = _key(worksheet_id)
    people = _rooms.get(key)
    if not people:
        return None
    participant = people.pop(participant_id, None)
    if not people:
        _rooms.pop(key, None)
    return participant


def snapshot(worksheet_id: uuid.UUID | str) -> list[dict[str, Any]]:
    return [p.public() for p in sorted(room(worksheet_id).values(), key=lambda p: p.joined_at)]


# ---------------------------------------------------------------------------
# lane B — presence and cursors, in process
# ---------------------------------------------------------------------------


def _envelope(event_type: str, audiences: Any, data: dict[str, Any]) -> dict[str, Any]:
    """The same envelope `_event_payload` produces, plus `data`.

    Kept identical on purpose: `broker.dispatch` routes on `audiences` and the
    SSE framing reads `id` and `type`, so nothing downstream needs to know a
    worksheet event from a message-inbox one.
    """
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "audiences": sorted({str(a) for a in audiences}),
        "occurred_at": _now().isoformat(),
        "data": data,
    }


def _audiences(worksheet_id: uuid.UUID | str, kinds: Any) -> list[str]:
    """Server-side, always. The kinds handed in here are the ones the caller
    proved — a link's sheet rows or a staff user's access — never a list that
    arrived in a request body."""
    return [sheet_audience(worksheet_id, str(kind)) for kind in (kinds or ())]


def presence_event(
    event_type: str, worksheet_id: uuid.UUID | str, participant: Participant
) -> dict[str, Any]:
    """A join or a leave, addressed to the sheets that participant is on.

    There is no worksheet-wide audience by construction, so "who is here" is
    scoped the same way the figures are: you see the people who share at least
    one sheet with you. A guest holding a P&L-only link is not announced to a
    stream that only opens the personal financial statement, which is the same
    answer the read scope gives.
    """
    return _envelope(
        event_type,
        _audiences(worksheet_id, participant.sheets),
        {
            "worksheet_id": str(worksheet_id),
            "participant": participant.public(),
        },
    )


def state_event(worksheet_id: uuid.UUID | str, participant: Participant) -> dict[str, Any]:
    """The snapshot unicast to a joiner. Not dispatched: it is written straight
    into that one stream, so it carries no audiences to route on."""
    return _envelope(
        "presence.state",
        _audiences(worksheet_id, participant.sheets),
        {
            "worksheet_id": str(worksheet_id),
            "you": participant.public(),
            "participants": [
                p
                for p in snapshot(worksheet_id)
                if set(p["sheets"]) & set(participant.sheets)
            ],
        },
    )


def announce_join(worksheet_id: uuid.UUID | str, participant: Participant) -> None:
    # Lane B: in-process only. At two workers the other worker's tabs never
    # hear this. See the module docstring.
    publish_ephemeral(presence_event("presence.join", worksheet_id, participant))


def announce_leave(worksheet_id: uuid.UUID | str, participant: Participant) -> None:
    publish_ephemeral(presence_event("presence.leave", worksheet_id, participant))


def clean_address(raw: Any) -> dict[str, Any]:
    """A cursor position, and nothing that is not one.

    Whitelisted rather than filtered: a denylist over a client-supplied dict is
    one unexpected key away from putting the number somebody is typing on the
    wire. Anything not in `CURSOR_ADDRESS_KEYS` — `value` above all — is
    dropped without comment.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in CURSOR_ADDRESS_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            out[key] = value
            continue
        cleaned = _CONTROL.sub("", str(value))[:_CURSOR_VALUE_LIMIT]
        if cleaned:
            out[key] = cleaned
    return out


def move_cursor(
    worksheet_id: uuid.UUID | str,
    participant: Participant,
    *,
    sheet_kind: str,
    address: Any,
    editing: bool = False,
) -> dict[str, Any]:
    """Publish where somebody is looking. Never what they are typing.

    Addressed to the one sheet the cursor is on, and only if the participant's
    own scope includes it — a cursor is a pointer at a cell, and a pointer at a
    cell a stream may not read is still a fact about that sheet.
    """
    kind = str(sheet_kind or "")
    if kind not in participant.sheets:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This link is no longer available")
    cursor = {
        "sheet_kind": kind,
        "address": clean_address(address),
        "editing": bool(editing),
    }
    participant.cursor = cursor
    participant.last_seen_at = _now()
    event = _envelope(
        "presence.cursor",
        _audiences(worksheet_id, [kind]),
        {
            "worksheet_id": str(worksheet_id),
            "participant_id": participant.participant_id,
            "name": participant.display_name,
            "colour": participant.colour,
            **cursor,
        },
    )
    publish_ephemeral(event)
    return event


# ---------------------------------------------------------------------------
# the sweeper
# ---------------------------------------------------------------------------


def sweep_once(*, now: datetime | None = None) -> list[Participant]:
    """Drop everyone unheard from for `STALE_AFTER`, announcing each leave.

    This is the half-open connection case: the socket is still nominally open,
    `request.is_disconnected()` has not noticed, and the generator's `finally`
    will not run for minutes. Without this the presence strip slowly fills with
    people who went home.
    """
    cutoff = (now or _now()) - STALE_AFTER
    dropped: list[Participant] = []
    for worksheet_id, people in list(_rooms.items()):
        for participant_id, participant in list(people.items()):
            if participant.last_seen_at > cutoff:
                continue
            people.pop(participant_id, None)
            dropped.append(participant)
            announce_leave(worksheet_id, participant)
        if not people:
            _rooms.pop(worksheet_id, None)
    return dropped


class PresenceSweeper:
    """A plain asyncio task, started in the lifespan beside `broker.start()`.

    Deliberately not an APScheduler job. APScheduler is in-process and is the
    stated reason the API runs `--workers 1`; adding presence housekeeping to
    it would put a thirty-second tick on the pile of things that have to be
    solved before the second worker can exist. This task is worker-local by
    nature — it sweeps a dictionary that is worker-local by nature.
    """

    def __init__(self, interval: float = SWEEP_SECONDS) -> None:
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="worksheet-presence-sweeper")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                return
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            try:
                sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # A sweeper that dies takes presence accuracy with it and says
                # nothing. Log and keep ticking.
                log.exception("worksheet presence sweep failed")


sweeper = PresenceSweeper()


# ---------------------------------------------------------------------------
# lane A — the figures, on the transaction
# ---------------------------------------------------------------------------


def clip(value: Any) -> tuple[str | None, bool]:
    """The broadcast value, and whether the receiver has to go and read it."""
    if value is None:
        return None, False
    text_value = str(value)
    if len(text_value) <= VALUE_LIMIT:
        return text_value, False
    return text_value[:VALUE_LIMIT], True


def cell_address(kind: str, key: str) -> dict[str, Any]:
    """The wire address for a cell key, in the shape the grid addresses cells.

    Derived from the layout's own path rather than by splitting the key, so the
    two never disagree. The key travels alongside it and is what a client
    should write back with: an appended row's *address* is nominal until the
    row exists, and addressing an edit by address rather than by key is how a
    value lands in the wrong row.
    """
    try:
        path = sheet_layout._path_for_key(kind, key)
    except (KeyError, AttributeError):
        return {"key": key}
    if len(path) == 3 and path[0] == "sections":
        return {"key": key, "section_key": path[1], "row_key": path[2]}
    if len(path) == 3 and path[0] == "debts":
        return {"key": key, "row_id": path[1], "column_key": path[2]}
    if len(path) == 4 and path[0] == "schedules":
        return {
            "key": key,
            "schedule_key": path[1],
            "row_id": path[2],
            "column_key": str(key).rsplit(".", 1)[-1],
        }
    return {"key": key, "row_key": path[-1]}


def cell_event(
    worksheet_id: uuid.UUID | str,
    *,
    sheet_kind: str,
    key: str,
    value: Any,
    revision: int,
    by: dict[str, Any] | None = None,
    origin_client_id: str | None = None,
) -> dict[str, Any]:
    """One accepted cell, addressed to that one sheet.

    `origin_client_id` is what lets the sender ignore its own echo. Without it
    the cell you are typing in is stomped by your own broadcast coming back
    around, which is the single most obvious way a live grid feels broken.
    """
    clipped, truncated = clip(value)
    return _envelope(
        "cell.changed",
        _audiences(worksheet_id, [sheet_kind]),
        {
            "worksheet_id": str(worksheet_id),
            "sheet_kind": str(sheet_kind),
            "address": cell_address(str(sheet_kind), str(key)),
            "value": clipped,
            "truncated": truncated,
            "revision": int(revision),
            "by": by,
            "origin_client_id": origin_client_id,
        },
    )


def row_event(
    worksheet_id: uuid.UUID | str,
    *,
    event_type: str,
    sheet_kind: str,
    row_id: str | None,
    block: str | None = None,
    after: str | None = None,
    revision: int,
    by: dict[str, Any] | None = None,
    origin_client_id: str | None = None,
) -> dict[str, Any]:
    return _envelope(
        event_type,
        _audiences(worksheet_id, [sheet_kind]),
        {
            "worksheet_id": str(worksheet_id),
            "sheet_kind": str(sheet_kind),
            "schedule_key": block,
            "row_id": str(row_id) if row_id else None,
            "after_row_id": str(after) if after else None,
            "revision": int(revision),
            "by": by,
            "origin_client_id": origin_client_id,
        },
    )


async def publish_sheet_event(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Lane A. `pg_notify` on the caller's own session, so the announcement is
    part of the transaction that made the change: a rollback takes the event
    with it, and a second worker would still receive it.

    Falls back to an in-process dispatch off Postgres (the test suite's SQLite
    session, and a session double that has no bind at all), the same way
    `publish_communication_event` does.
    """
    if not payload.get("audiences"):
        return
    get_bind = getattr(db, "get_bind", None)
    if get_bind is None:
        broker.dispatch(payload)
        return
    try:
        bind = get_bind()
        dialect = bind.dialect.name
    except Exception:  # noqa: BLE001
        broker.dispatch(payload)
        return
    if dialect != "postgresql":
        broker.dispatch(payload)
        return
    await db.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHANNEL, "payload": json.dumps(payload, separators=(",", ":"))},
    )


__all__ = [
    "COLOURS",
    "MAX_PARTICIPANTS",
    "STALE_AFTER",
    "VALUE_LIMIT",
    "Participant",
    "announce_join",
    "announce_leave",
    "by_user",
    "cell_address",
    "cell_event",
    "clean_address",
    "clean_name",
    "clear",
    "clip",
    "colour_for",
    "colour_key",
    "forget",
    "full",
    "get",
    "move_cursor",
    "presence_event",
    "publish_sheet_event",
    "register",
    "room",
    "row_event",
    "sheet_audience",
    "snapshot",
    "state_event",
    "sweep_once",
    "sweeper",
    "touch",
]
