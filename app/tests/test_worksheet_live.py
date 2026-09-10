"""The live half: two doors onto one stream, and the line neither may cross.

The property everything here exists to protect is one sentence: **the audience
is computed from what the caller proved, never from what the request asked
for.** Cell values are broadcast — that is the feature, and it is only safe
because the audience key `sheet:{worksheet}:{kind}` is by construction the same
set as the read scope. A single line that took the sheet list from a request
body would turn every broadcast into a leak, so it is pinned from the outside:
a stream opened by a link that does not include the personal financial
statement never receives a PFS frame, whatever the request says.

The rest is the behaviour a live grid is judged on. The connection *is* the
presence, so closing a stream announces a leave with no endpoint to call. A
view-only holder watches the sheet change under them and is refused every
write. A cursor carries a position and never a value.

`sheets.*` is patched where the figures would be written: this file is about
the envelope and the wire, and both must be provable without a database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import worksheets as routes
from app.services import link_throttle
from app.services import worksheet_links as wl
from app.services import worksheet_presence as presence

ALL_SHEETS = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")
PIN = "482913"


@pytest.fixture(autouse=True)
def _fresh():
    link_throttle.clear()
    presence.clear()
    yield
    link_throttle.clear()
    presence.clear()


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


def _link(**over):
    from app.dealer_os.services.client_room import _hash_passcode

    base = dict(
        id=uuid.uuid4(),
        profile_id=uuid.uuid4(),
        worksheet_id=uuid.uuid4(),
        kind="worksheet",
        packet_id=None,
        statement_id=None,
        token_hash=wl.hash_token("tok"),
        label="For the accountant",
        invitee_email=None,
        permission="edit",
        created_by=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(days=7),
        revoked_at=None,
        last_used_at=None,
        completed_at=None,
        use_count=0,
        pin_hash=_hash_passcode(PIN),
        pin_set_at=datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        pin_attempts=0,
        pin_locked_until=None,
    )
    base.update(over)
    link = SimpleNamespace(**base)
    link.is_open = link.revoked_at is None and (
        link.expires_at is None or link.expires_at > datetime.now(UTC)
    )
    return link


def _db(link, *, sheets=ALL_SHEETS, worksheet=None):
    profile = SimpleNamespace(id=getattr(link, "profile_id", uuid.uuid4()))
    worksheet = worksheet or SimpleNamespace(
        id=getattr(link, "worksheet_id", uuid.uuid4()),
        profile_id=profile.id,
        revision=17,
    )
    calls = {"n": 0}

    async def execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(scalar_one_or_none=lambda: link)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(sheets)))

    async def get(model, _key, **_kw):
        return {"ApplicationProfile": profile, "FinancialWorksheet": worksheet}.get(model.__name__)

    return SimpleNamespace(
        execute=execute,
        get=get,
        flush=AsyncMock(),
        commit=AsyncMock(),
        add=lambda _row: None,
        profile=profile,
        worksheet=worksheet,
    )


def _request(*, alive=200):
    """A request that reports itself connected `alive` times, then hangs up."""
    left = {"n": alive}

    async def is_disconnected():
        left["n"] -= 1
        return left["n"] < 0

    return SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.4", "user-agent": "Firefox/141"},
        client=SimpleNamespace(host="203.0.113.4"),
        is_disconnected=is_disconnected,
    )


def _guest_session(link, *, sheets=ALL_SHEETS, worksheet=None):
    """The stream route opens its own session rather than taking `get_db`: a
    request-scoped one would be held for the life of the stream. Patch the
    factory the way the staff side is patched, so the test exercises that."""
    db = _db(link, sheets=sheets, worksheet=worksheet)
    db.rollback = AsyncMock()

    class _Session:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_exc):
            return False

    return patch.object(routes, "SessionLocal", lambda: _Session())


async def _unlock(db):
    _access, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")
    return session


def _frames(raw: list[str]) -> list[dict]:
    """The `data:` payloads of the frames that carry one."""
    out = []
    for chunk in raw:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: ") :]))
    return out


async def _pump(response, *, publish=None, expect=0):
    """Drive a stream: opening frames, then whatever `publish` puts on the bus."""
    body = response.body_iterator
    raw = [await anext(body), await anext(body)]  # retry, presence.state
    if publish is not None:
        publish()
    for _ in range(expect):
        raw.append(await asyncio.wait_for(anext(body), timeout=2))
    await body.aclose()
    return raw


# ---------------------------------------------------------------------------
# the invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_never_receives_a_sheet_the_link_does_not_open():
    """The whole design in one test. The desk unticked the personal financial
    statement; the accountant's stream carries the P&L cell and does not carry
    the net-worth one, because the two are on different audience keys."""
    link = _link()
    kinds = ("p_and_l", "balance_sheet", "debt_schedule")
    db = _db(link, sheets=kinds)
    session = await _unlock(db)
    ws = db.worksheet.id

    with _guest_session(link, sheets=kinds, worksheet=db.worksheet):
        response = await routes.stream_worksheet_events(
            "tok", _request(), session, participant="tab-1", name="Marisol"
        )

    def publish():
        presence.broker.dispatch(
            presence.cell_event(ws, sheet_kind="p_and_l", key="supplies", value="40", revision=18)
        )
        presence.broker.dispatch(
            presence.cell_event(
                ws, sheet_kind="pfs", key="cash_on_hand", value="912000", revision=19
            )
        )

    raw = await _pump(response, publish=publish, expect=2)
    events = _frames(raw)
    # The joiner hears its own join back: the subscription is opened before the
    # announcement so nothing published in between is lost, and a self-echo is
    # cheaper to ignore by `participant_id` than that race is to fix.
    assert [e["type"] for e in events] == ["presence.state", "presence.join", "cell.changed"]
    assert events[-1]["data"]["sheet_kind"] == "p_and_l"
    # Not "the PFS event arrived and was filtered on the way out" — it was
    # never routed to this queue at all.
    assert "912000" not in "".join(raw)
    assert "pfs" not in "".join(raw)


@pytest.mark.asyncio
async def test_the_audience_comes_off_the_link_row_and_the_request_cannot_widen_it():
    link = _link()
    kinds = ("p_and_l",)
    db = _db(link, sheets=kinds)
    session = await _unlock(db)

    seen = {}
    real_register = presence.register

    def spy(worksheet_id, **kw):
        seen.update(kw)
        return real_register(worksheet_id, **kw)

    with patch.object(presence, "register", spy):
        with _guest_session(link, sheets=kinds, worksheet=db.worksheet):
            response = await routes.stream_worksheet_events(
                "tok", _request(), session, participant="tab-1", name="Marisol"
            )
        await _pump(response)

    assert tuple(seen["sheets"]) == kinds

    # And there is no way to ask for more: the route takes a token, a session,
    # a tab and a name. Nothing that names a sheet. This assertion is the guard
    # against somebody adding one later.
    import inspect

    params = set(inspect.signature(routes.stream_worksheet_events).parameters)
    assert params == {"token", "request", "session", "participant", "name"}
    assert not {p for p in params if "sheet" in p or "audience" in p}


@pytest.mark.asyncio
async def test_a_guest_subscribes_to_sheet_keys_only():
    """No user identity, so no `user:` audience — there is nothing to give
    them and nothing to fall back on."""
    link = _link()
    db = _db(link)
    session = await _unlock(db)
    subscribed: list[list[str]] = []
    real = presence.broker.subscribe

    def spy(audiences):
        keys = list(audiences)
        subscribed.append(keys)
        return real(keys)

    with patch.object(presence.broker, "subscribe", spy):
        with _guest_session(link, worksheet=db.worksheet):
            response = await routes.stream_worksheet_events(
                "tok", _request(), session, participant="tab-1"
            )
        await _pump(response)

    (keys,) = subscribed
    assert keys and all(key.startswith(f"sheet:{db.worksheet.id}:") for key in keys)
    assert not any(key.startswith("user:") for key in keys)


# ---------------------------------------------------------------------------
# presence through the door
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closing_the_stream_announces_the_leave_with_no_endpoint_to_call():
    link = _link()
    db = _db(link)
    session = await _unlock(db)
    ws = db.worksheet.id

    with _guest_session(link, worksheet=db.worksheet):
        response = await routes.stream_worksheet_events(
            "tok", _request(), session, participant="tab-1", name="Marisol"
        )
    assert "tab-1" in presence.room(ws)

    seen: list[dict] = []
    original = presence.broker.dispatch
    presence.broker.dispatch = seen.append  # type: ignore[assignment]
    try:
        await _pump(response)
    finally:
        presence.broker.dispatch = original  # type: ignore[assignment]

    assert presence.room(ws) == {}
    assert [e["type"] for e in seen] == ["presence.join", "presence.leave"]
    assert seen[-1]["data"]["participant"]["name"] == "Marisol"


@pytest.mark.asyncio
async def test_a_hung_up_connection_ends_the_stream():
    link = _link()
    db = _db(link)
    session = await _unlock(db)
    with _guest_session(link, worksheet=db.worksheet):
        response = await routes.stream_worksheet_events(
            "tok", _request(alive=0), session, participant="tab-1"
        )
    body = response.body_iterator
    frames = [chunk async for chunk in body]
    assert frames[0] == "retry: 3000\n\n"
    assert presence.room(db.worksheet.id) == {}


@pytest.mark.asyncio
async def test_the_twenty_first_stream_is_refused_before_the_response_is_returned():
    """Refused with a status code rather than a stream that dies unexplained —
    which is only possible because the cap is checked in the route, before the
    StreamingResponse exists."""
    link = _link()
    db = _db(link)
    session = await _unlock(db)
    for n in range(presence.MAX_PARTICIPANTS):
        presence.register(
            db.worksheet.id, participant_id=f"other-{n}", kind="guest",
            sheets=ALL_SHEETS, link_id=link.id,
        )
    with _guest_session(link, worksheet=db.worksheet), pytest.raises(HTTPException) as err:
        await routes.stream_worksheet_events(
            "tok", _request(), session, participant="tab-late"
        )
    assert err.value.status_code == 429 and err.value.detail["code"] == "worksheet_full"


@pytest.mark.asyncio
async def test_a_view_only_participant_may_stream_but_never_write():
    """The accountant looking over your shoulder. Watching is the feature; the
    403 is on the write, not on the window."""
    link = _link(permission="view")
    db = _db(link)
    session = await _unlock(db)

    with _guest_session(link, worksheet=db.worksheet):
        response = await routes.stream_worksheet_events(
            "tok", _request(), session, participant="tab-1"
        )
    raw = await _pump(response)
    assert _frames(raw)[0]["type"] == "presence.state"

    from app.services import sheets

    apply = AsyncMock(return_value={"rev": {}, "computed": {}, "resync": []})
    with patch.object(sheets, "apply_cell_edits", apply, create=True):
        body = routes.CellsBody(
            edits=[{"sheet": "p_and_l", "key": "supplies", "value": "40"}], participant_id="tab-1"
        )
        with pytest.raises(HTTPException) as err:
            await routes.write_worksheet_cells(
                "tok", body, _request(), _db(link, worksheet=db.worksheet), session
            )
    assert err.value.status_code == 403 and apply.await_count == 0


# ---------------------------------------------------------------------------
# cursors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cursor_publishes_a_position_and_bumps_the_clock():
    link = _link()
    db = _db(link)
    session = await _unlock(db)
    person = presence.register(
        db.worksheet.id, participant_id="tab-1", kind="guest",
        sheets=ALL_SHEETS, link_id=link.id, display_name="Marisol",
    )
    person.last_seen_at = datetime.now(UTC) - timedelta(seconds=60)

    seen: list[dict] = []
    original = presence.broker.dispatch
    presence.broker.dispatch = seen.append  # type: ignore[assignment]
    try:
        body = routes.CursorBody(
            participant_id="tab-1",
            sheet="debt_schedule",
            address={"row_id": "r1", "column_key": "balance", "value": "48000"},
            editing=True,
        )
        answer = await routes.move_worksheet_cursor(
            "tok", body, _request(), _db(link, worksheet=db.worksheet), session
        )
    finally:
        presence.broker.dispatch = original  # type: ignore[assignment]

    assert answer == {"ok": True}
    (event,) = seen
    assert event["type"] == "presence.cursor"
    assert event["audiences"] == [f"sheet:{db.worksheet.id}:debt_schedule"]
    assert "48000" not in str(event)
    assert person.last_seen_at > datetime.now(UTC) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_a_cursor_from_a_tab_that_is_not_streaming_is_refused():
    link = _link()
    db = _db(link)
    session = await _unlock(db)
    body = routes.CursorBody(participant_id="ghost", sheet="p_and_l", address={"key": "supplies"})
    with pytest.raises(HTTPException) as err:
        await routes.move_worksheet_cursor(
            "tok", body, _request(), _db(link, worksheet=db.worksheet), session
        )
    assert err.value.status_code == 409 and err.value.detail["code"] == "not_streaming"


@pytest.mark.asyncio
async def test_a_cursor_on_an_unopened_sheet_is_the_uniform_miss():
    link = _link()
    kinds = ("p_and_l",)
    db = _db(link, sheets=kinds)
    session = await _unlock(db)
    presence.register(
        db.worksheet.id, participant_id="tab-1", kind="guest", sheets=kinds, link_id=link.id
    )
    body = routes.CursorBody(participant_id="tab-1", sheet="pfs", address={"key": "cash_on_hand"})
    with pytest.raises(HTTPException) as err:
        await routes.move_worksheet_cursor(
            "tok", body, _request(), _db(link, sheets=kinds, worksheet=db.worksheet), session
        )
    assert err.value.status_code == 404 and err.value.detail == wl.gone().detail


# ---------------------------------------------------------------------------
# the desk's door
# ---------------------------------------------------------------------------


def _staff_patches(user, worksheet):
    from app.services import application_profiles as profiles

    auth_db = SimpleNamespace(
        get=AsyncMock(return_value=worksheet), commit=AsyncMock(), rollback=AsyncMock()
    )

    class _Session:
        async def __aenter__(self):
            return auth_db

        async def __aexit__(self, *_exc):
            return False

    return (
        patch.object(routes, "SessionLocal", lambda: _Session()),
        patch.object(routes, "resolve_user_from_headers", AsyncMock(return_value=user)),
        # The real one answers about the id it was asked for, which is what
        # makes the "worksheet belongs to another file" check meaningful.
        patch.object(
            profiles,
            "load_profile",
            AsyncMock(side_effect=lambda _db, profile_id, _user: SimpleNamespace(id=profile_id)),
        ),
    )


@pytest.mark.asyncio
async def test_the_desk_streams_all_four_sheets_and_is_named_from_its_account():
    user = SimpleNamespace(id=uuid.uuid4(), role=Role.LOAN_EXEC, name="Jane Desk", email="j@x.com")
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=3)
    a, b, c = _staff_patches(user, worksheet)
    with a, b, c:
        response = await routes.stream_staff_worksheet_events(
            worksheet.profile_id, worksheet.id, _request(), participant="desk-1"
        )
        raw = await _pump(response)

    person = presence.room(worksheet.id).get("desk-1")
    assert person is None  # the stream closed, so they left
    state = _frames(raw)[0]
    assert state["type"] == "presence.state"
    assert state["data"]["you"]["name"] == "Jane Desk"
    assert set(state["data"]["you"]["sheets"]) == set(ALL_SHEETS)
    # A signed-in user is already named; no display name is taken from the
    # request, so nothing self-declared sits beside a proved one.
    import inspect

    assert "name" not in inspect.signature(routes.stream_staff_worksheet_events).parameters


@pytest.mark.asyncio
async def test_a_role_that_may_not_touch_a_statement_may_not_watch_one_either():
    user = SimpleNamespace(id=uuid.uuid4(), role=Role.CLIENT, name="Borrower", email="b@x.com")
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=3)
    a, b, c = _staff_patches(user, worksheet)
    with a, b, c:
        with pytest.raises(HTTPException) as err:
            await routes.stream_staff_worksheet_events(
                worksheet.profile_id, worksheet.id, _request(), participant="desk-1"
            )
    assert err.value.status_code == 403
    assert presence.room(worksheet.id) == {}


@pytest.mark.asyncio
async def test_a_worksheet_on_another_file_is_not_found():
    user = SimpleNamespace(id=uuid.uuid4(), role=Role.LOAN_EXEC, name="Jane", email="j@x.com")
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=3)
    a, b, c = _staff_patches(user, worksheet)
    with a, b, c:
        with pytest.raises(HTTPException) as err:
            await routes.stream_staff_worksheet_events(
                uuid.uuid4(), worksheet.id, _request(), participant="desk-1"
            )
    assert err.value.status_code == 404


def test_the_denied_roles_are_the_same_list_the_statement_routes_use():
    """Copied rather than imported, so a test has to hold the two together."""
    from app.routers import application_profiles as staff_routes

    assert routes._STATEMENT_DENIED_ROLES == staff_routes._STATEMENT_DENIED_ROLES


# ---------------------------------------------------------------------------
# publishing a write
# ---------------------------------------------------------------------------


def _write_db():
    """A session with no bind: `publish_sheet_event` falls back to an in-process
    dispatch, the same way `publish_communication_event` does off Postgres."""
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None, scalar=lambda: 0)),
        flush=AsyncMock(),
        commit=AsyncMock(),
        add=lambda _row: None,
        get=AsyncMock(return_value=None),
    )


async def _apply(
    edits, *, worksheet, participant_id=None, client_id=None, actor_user_id=None, mangle=None
):
    from app.services import business_statement_schema as bss
    from app.services import business_statements, financial_statements, sheets

    statement = SimpleNamespace(id=uuid.uuid4(), kind="p_and_l", status="draft", body=bss.pl_empty_body())

    async def _body_for_profile(_db, _profile, kind, _prefill=None):
        return dict(statement.body), statement

    async def _save(_db, _profile, *, kind, body, status, actor_user_id, statement):
        statement.body = mangle(body) if mangle else body
        return statement

    profile = SimpleNamespace(id=worksheet.profile_id, primary_bucket_id=None)
    seen: list[dict] = []
    original = presence.broker.dispatch
    presence.broker.dispatch = seen.append  # type: ignore[assignment]
    try:
        with (
            patch.object(business_statements, "body_for_profile", AsyncMock(side_effect=_body_for_profile)),
            patch.object(business_statements, "save", AsyncMock(side_effect=_save)),
            patch.object(financial_statements, "form_prefill", AsyncMock(return_value={})),
            patch.object(sheets, "_lock", AsyncMock(return_value=worksheet)),
            patch.object(sheets, "_stamp_sheet_rev", AsyncMock(return_value=None)),
        ):
            result = await sheets.apply_cell_edits(
                _write_db(),
                profile,
                edits,
                origin="admin",
                actor_user_id=actor_user_id,
                worksheet=worksheet,
                participant_id=participant_id,
                client_id=client_id,
            )
    finally:
        presence.broker.dispatch = original  # type: ignore[assignment]
    return result, seen


@pytest.mark.asyncio
async def test_an_accepted_write_announces_the_cell_it_stored():
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=17)
    person = presence.register(
        worksheet.id, participant_id="tab-1", kind="guest",
        sheets=ALL_SHEETS, link_id=uuid.uuid4(), display_name="Marisol",
    )
    result, seen = await _apply(
        [{"sheet": "p_and_l", "key": "supplies", "value": "40"}],
        worksheet=worksheet,
        participant_id="tab-1",
        client_id="c_71",
    )

    assert result["rev"]["p_and_l"] == 18
    (event,) = [e for e in seen if e["type"] == "cell.changed"]
    assert event["audiences"] == [f"sheet:{worksheet.id}:p_and_l"]
    data = event["data"]
    assert data["address"] == {
        "key": "supplies",
        "section_key": "operating_expenses",
        "row_key": "supplies",
    }
    assert data["value"] == "40" and data["revision"] == 18
    # The sender's own echo is labelled so it can be ignored; without this the
    # cell you are typing in is stomped by your own broadcast.
    assert data["origin_client_id"] == "c_71"
    assert data["by"] == person.by()
    assert data["by"]["name"] == "Marisol"


@pytest.mark.asyncio
async def test_a_write_from_nobody_streaming_still_lands_just_unlabelled():
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=1)
    _result, seen = await _apply(
        [{"sheet": "p_and_l", "key": "supplies", "value": "40"}], worksheet=worksheet
    )
    (event,) = [e for e in seen if e["type"] == "cell.changed"]
    assert event["data"]["by"] is None
    assert event["data"]["origin_client_id"] is None


@pytest.mark.asyncio
async def test_a_writing_tab_is_kept_alive_by_its_own_write():
    """`last_seen_at` is bumped by an accepted write, so somebody typing
    steadily is never swept out from under themselves."""
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=1)
    person = presence.register(
        worksheet.id, participant_id="tab-1", kind="guest", sheets=ALL_SHEETS,
        link_id=uuid.uuid4(), display_name="Marisol",
    )
    person.last_seen_at = datetime.now(UTC) - presence.STALE_AFTER - timedelta(seconds=10)
    await _apply(
        [{"sheet": "p_and_l", "key": "supplies", "value": "40"}],
        worksheet=worksheet,
        participant_id="tab-1",
    )
    assert presence.sweep_once() == []


@pytest.mark.asyncio
async def test_the_broadcast_carries_what_was_stored_not_what_was_typed():
    """A figure the save normalised is announced as stored. Broadcasting the
    typed text would paint the other grids with a value the file does not
    hold."""
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=1)

    def normalised(body):
        body["sections"]["operating_expenses"]["supplies"] = "40.00"
        return body

    result, seen = await _apply(
        [{"sheet": "p_and_l", "key": "supplies", "value": "40"}],
        worksheet=worksheet,
        mangle=normalised,
    )
    (event,) = [e for e in seen if e["type"] == "cell.changed"]
    assert event["data"]["value"] == "40.00"
    # And the sender is told to take the server's answer for that sheet.
    assert result["resync"] == ["p_and_l"]


@pytest.mark.asyncio
async def test_nothing_is_announced_when_the_batch_is_empty():
    worksheet = SimpleNamespace(id=uuid.uuid4(), profile_id=uuid.uuid4(), revision=5)
    result, seen = await _apply([], worksheet=worksheet)
    assert result["rev"]["debt_schedule"] == 5
    assert [e for e in seen if e["type"] == "cell.changed"] == []
