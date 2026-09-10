"""The presence registry: who is here, what colour they are, and when they go.

Presence is the part of a live grid people notice when it is wrong. A colour
that changes when somebody *else* leaves, a strip that slowly fills with people
who went home, a guest whose typed name reaches a field meant for proved facts
— each of those is a small thing that makes the feature feel untrustworthy, and
each is pinned here.

Nothing in this module talks to a database. The registry is one in-process
dictionary, correct while the API runs `--workers 1` and honestly documented as
the first thing that breaks at two.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.services import worksheet_presence as presence
from app.services.communication_events import broker

ALL_SHEETS = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")


@pytest.fixture(autouse=True)
def _fresh_registry():
    presence.clear()
    yield
    presence.clear()


def _guest(ws, n=1, sheets=ALL_SHEETS, link_id=None, name=None):
    return presence.register(
        ws,
        participant_id=f"p_{n}",
        kind="guest",
        sheets=sheets,
        display_name=name,
        claimed_name=name,
        link_id=link_id or uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# colour
# ---------------------------------------------------------------------------


def test_colour_is_stable_across_a_reconnect_and_across_a_restart():
    """The same tab comes back the same colour, and so does the same staff user
    in a second window. `crc32(key) % 8` means the answer survives a redeploy,
    which round-robin would not."""
    ws = uuid.uuid4()
    link = uuid.uuid4()
    first = _guest(ws, 1, link_id=link)
    colour = first.colour

    presence.forget(ws, first.participant_id)
    assert presence.room(ws) == {}
    again = _guest(ws, 1, link_id=link)
    assert again.colour == colour

    # And it does not depend on who else is in the room: the whole point of
    # deterministic-then-dedup is that somebody else leaving cannot repaint you.
    other = _guest(ws, 2, link_id=link)
    presence.forget(ws, other.participant_id)
    presence.clear()
    fresh = _guest(ws, 1, link_id=link)
    assert fresh.colour == colour


def test_a_collision_takes_the_next_free_colour_and_nobody_shares_one():
    ws = uuid.uuid4()
    link = uuid.uuid4()
    # Force the collision: two keys that hash to the same slot are hard to find
    # on purpose, so drive it through the full palette and check the property.
    people = [_guest(ws, n, link_id=link) for n in range(len(presence.COLOURS))]
    colours = [p.colour for p in people]
    assert sorted(colours) == list(range(len(presence.COLOURS)))
    assert len(set(colours)) == len(colours)


def test_a_staff_colour_keys_off_the_user_not_the_tab():
    """Two windows, one person, one colour — the strip should not show the
    underwriter twice in two colours."""
    user_id = uuid.uuid4()
    ws = uuid.uuid4()
    assert presence.colour_key(user_id=user_id, participant_id="a") == str(user_id)
    assert presence.colour_key(user_id=user_id, participant_id="b") == str(user_id)
    one = presence.register(
        ws, participant_id="tab-a", kind="staff", sheets=ALL_SHEETS,
        display_name="Jane Desk", user_id=user_id,
    )
    presence.forget(ws, "tab-a")
    two = presence.register(
        ws, participant_id="tab-b", kind="staff", sheets=ALL_SHEETS,
        display_name="Jane Desk", user_id=user_id,
    )
    assert one.colour == two.colour


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------


def test_a_claimed_name_is_clamped_stripped_and_kept_out_of_the_proved_fields():
    ws = uuid.uuid4()
    person = _guest(ws, 1, name="  Mari\nsol\t(book\x00keeper)   " + "x" * 80)
    assert len(person.display_name) <= presence.NAME_LIMIT
    assert "\n" not in person.display_name and "\x00" not in person.display_name
    # It is a claim, and it lives in the field that says so. Nothing merged it
    # into a proved one.
    assert person.claimed_name == person.display_name
    assert person.user_id is None
    assert "claimed_name" not in person.public()
    assert "user_id" not in person.public() and "link_id" not in person.public()


def test_a_skipped_name_becomes_the_colour_so_two_guests_are_told_apart():
    ws = uuid.uuid4()
    link = uuid.uuid4()
    one = _guest(ws, 1, link_id=link)
    two = _guest(ws, 2, link_id=link)
    assert one.display_name.startswith("Guest (") and two.display_name.startswith("Guest (")
    assert one.display_name != two.display_name
    assert one.display_name == f"Guest ({one.colour_name})"


def test_clean_name_answers_none_for_nothing_worth_showing():
    assert presence.clean_name(None) is None
    assert presence.clean_name("   ") is None
    assert presence.clean_name("\x00\x1f") is None
    assert presence.clean_name("Dana  R.") == "Dana R."


# ---------------------------------------------------------------------------
# the cap
# ---------------------------------------------------------------------------


def test_the_twenty_first_connection_is_refused_and_says_why():
    ws = uuid.uuid4()
    link = uuid.uuid4()
    for n in range(presence.MAX_PARTICIPANTS):
        _guest(ws, n, link_id=link)
    assert len(presence.room(ws)) == presence.MAX_PARTICIPANTS

    with pytest.raises(HTTPException) as err:
        _guest(ws, 999, link_id=link)
    assert err.value.status_code == 429
    assert err.value.detail["code"] == "worksheet_full"
    assert str(presence.MAX_PARTICIPANTS) in err.value.detail["message"]

    # A tab already in the room reconnecting is not a new participant, so the
    # cap does not lock the twentieth person out of their own reload.
    again = _guest(ws, 0, link_id=link)
    assert again.participant_id == "p_0"
    assert len(presence.room(ws)) == presence.MAX_PARTICIPANTS

    # And the cap is per worksheet, not global.
    other = uuid.uuid4()
    assert _guest(other, 1, link_id=link) is not None


# ---------------------------------------------------------------------------
# the sweeper
# ---------------------------------------------------------------------------


def test_the_sweeper_drops_a_stalled_participant_and_announces_the_leave():
    """The half-open connection: the socket is nominally still there, the
    generator's `finally` has not run, and without this the strip keeps showing
    somebody who closed their laptop twenty minutes ago."""
    ws = uuid.uuid4()
    here = _guest(ws, 1, name="Still here")
    stalled = _guest(ws, 2, name="Lid closed")
    stalled.last_seen_at = datetime.now(UTC) - presence.STALE_AFTER - timedelta(seconds=1)

    seen: list[dict] = []
    original = broker.dispatch
    broker.dispatch = seen.append  # type: ignore[assignment]
    try:
        dropped = presence.sweep_once()
    finally:
        broker.dispatch = original  # type: ignore[assignment]

    assert [p.participant_id for p in dropped] == [stalled.participant_id]
    assert set(presence.room(ws)) == {here.participant_id}
    (event,) = seen
    assert event["type"] == "presence.leave"
    assert event["data"]["participant"]["name"] == "Lid closed"


def test_a_touch_keeps_somebody_in_the_room():
    ws = uuid.uuid4()
    person = _guest(ws, 1)
    person.last_seen_at = datetime.now(UTC) - presence.STALE_AFTER - timedelta(seconds=5)
    presence.touch(ws, person.participant_id)
    assert presence.sweep_once() == []
    assert presence.touch(ws, "nobody") is None


@pytest.mark.asyncio
async def test_the_sweeper_task_starts_ticks_and_stops():
    """It is a plain asyncio task, started in the lifespan beside
    `broker.start()`. APScheduler is the reason this process runs `--workers 1`
    and presence housekeeping does not belong on that pile."""
    ws = uuid.uuid4()
    stalled = _guest(ws, 1)
    stalled.last_seen_at = datetime.now(UTC) - presence.STALE_AFTER - timedelta(seconds=1)

    task = presence.PresenceSweeper(interval=0.01)
    await task.start()
    await task.start()  # idempotent: a second lifespan call must not double it
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not presence.room(ws):
            break
    await task.stop()
    assert presence.room(ws) == {}
    assert task._task is None


def test_sweeping_an_empty_registry_is_a_no_op():
    assert presence.sweep_once() == []
    assert presence.room(uuid.uuid4()) == {}


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------


def test_a_broadcast_value_is_clipped_and_says_so():
    ws = uuid.uuid4()
    long_value = "9" * (presence.VALUE_LIMIT + 250)
    event = presence.cell_event(
        ws, sheet_kind="p_and_l", key="gross_revenue", value=long_value, revision=12
    )
    assert len(event["data"]["value"]) == presence.VALUE_LIMIT
    assert event["data"]["truncated"] is True

    short = presence.cell_event(
        ws, sheet_kind="p_and_l", key="gross_revenue", value="1250000", revision=12
    )
    assert short["data"]["value"] == "1250000" and short["data"]["truncated"] is False
    # Comfortably inside pg_notify's 8000-byte payload cap, which is the reason
    # the clip exists at all.
    import json

    assert len(json.dumps(event)) < 8000


def test_a_cell_event_addresses_the_cell_the_way_the_grid_does():
    ws = uuid.uuid4()
    row = str(uuid.uuid4())
    statement = presence.cell_event(
        ws, sheet_kind="p_and_l", key="supplies", value="40", revision=1
    )["data"]["address"]
    assert statement == {
        "key": "supplies",
        "section_key": "operating_expenses",
        "row_key": "supplies",
    }

    debt = presence.cell_event(
        ws, sheet_kind="debt_schedule", key=f"{row}.balance", value="10", revision=1
    )["data"]["address"]
    assert debt == {"key": f"{row}.balance", "row_id": row, "column_key": "balance"}

    schedule = presence.cell_event(
        ws,
        sheet_kind="pfs",
        key="notes_payable.r1.original_balance",
        value="10",
        revision=1,
    )["data"]["address"]
    assert schedule == {
        "key": "notes_payable.r1.original_balance",
        "schedule_key": "notes_payable",
        "row_id": "r1",
        "column_key": "original_balance",
    }

    # The key travels on every address, because an appended row's *address* is
    # nominal until the row exists and an edit addressed by anything but the
    # key can land in the wrong line.
    assert all(
        "key" in presence.cell_event(ws, sheet_kind=k, key=key, value="1", revision=1)["data"][
            "address"
        ]
        for k, key in (("p_and_l", "supplies"), ("debt_schedule", "r4.balance"))
    )


def test_a_cursor_carries_a_position_and_nothing_else():
    """The one place the signals-not-content rule genuinely applies: a cursor
    stream that carried values would be keystroke-level surveillance of
    somebody typing their net worth."""
    ws = uuid.uuid4()
    person = _guest(ws, 1, sheets=("pfs",))
    seen: list[dict] = []
    original = broker.dispatch
    broker.dispatch = seen.append  # type: ignore[assignment]
    try:
        presence.move_cursor(
            ws,
            person,
            sheet_kind="pfs",
            address={
                "schedule_key": "real_estate",
                "row_id": "r1",
                "column_key": "market_value",
                "value": "480000",
                "draft": "48000",
                "notes": "half typed",
            },
            editing=True,
        )
    finally:
        broker.dispatch = original  # type: ignore[assignment]

    (event,) = seen
    assert event["type"] == "presence.cursor"
    assert "480000" not in str(event)
    assert event["data"]["address"] == {
        "schedule_key": "real_estate",
        "row_id": "r1",
        "column_key": "market_value",
    }
    assert event["data"]["editing"] is True
    assert "value" not in event["data"] and "value" not in event["data"]["address"]


def test_a_cursor_on_a_sheet_the_participant_does_not_hold_is_the_uniform_miss():
    ws = uuid.uuid4()
    person = _guest(ws, 1, sheets=("p_and_l",))
    with pytest.raises(HTTPException) as err:
        presence.move_cursor(ws, person, sheet_kind="pfs", address={"key": "cash_on_hand"})
    assert err.value.status_code == 404
    assert err.value.detail == "This link is no longer available"


def test_clean_address_drops_anything_that_is_not_a_position():
    assert presence.clean_address({"value": "1", "amount": 2}) == {}
    assert presence.clean_address("not a dict") == {}
    assert presence.clean_address({"r": 4, "c": 2}) == {"r": 4, "c": 2}
    assert presence.clean_address({"key": "a" * 500})["key"] == "a" * 80
    assert presence.clean_address({"row_key": "gross\nrevenue"}) == {"row_key": "grossrevenue"}


# ---------------------------------------------------------------------------
# audiences
# ---------------------------------------------------------------------------


def test_every_event_is_addressed_per_sheet_never_per_worksheet():
    """One key per (worksheet, sheet). A worksheet-wide key would put the
    borrower's net worth on the accountant's wire the moment the desk unticked
    the personal financial statement."""
    ws = uuid.uuid4()
    person = _guest(ws, 1, sheets=("p_and_l", "balance_sheet"))
    join = presence.presence_event("presence.join", ws, person)
    assert join["audiences"] == sorted(
        [f"sheet:{ws}:p_and_l", f"sheet:{ws}:balance_sheet"]
    )
    cell = presence.cell_event(ws, sheet_kind="p_and_l", key="supplies", value="1", revision=2)
    assert cell["audiences"] == [f"sheet:{ws}:p_and_l"]
    assert all(str(ws) in a and a.startswith("sheet:") for a in cell["audiences"])


def test_a_snapshot_only_names_people_who_share_a_sheet_with_you():
    ws = uuid.uuid4()
    desk = presence.register(
        ws, participant_id="desk", kind="staff", sheets=ALL_SHEETS,
        display_name="Jane Desk", user_id=uuid.uuid4(),
    )
    accountant = _guest(ws, 1, sheets=("p_and_l",), name="Marisol")
    pfs_only = _guest(ws, 2, sheets=("pfs",), name="Owner")

    seen = presence.state_event(ws, accountant)["data"]["participants"]
    names = {p["name"] for p in seen}
    assert "Jane Desk" in names and "Marisol" in names
    assert "Owner" not in names
    assert presence.state_event(ws, desk)["data"]["you"]["participant_id"] == "desk"
    assert len(presence.state_event(ws, desk)["data"]["participants"]) == 3
    assert pfs_only.display_name == "Owner"
