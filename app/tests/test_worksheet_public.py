"""The no-login worksheet: what the token opens, what it refuses, what it records.

These are the properties the sharing dialog promises. A scope the server does
not enforce is a lie told in a checkbox, so each one is tested from the outside
— through `resolve` and the public routes — rather than by asserting that a
helper was called.

`sheets.*` is patched throughout: the grid service is the other half of this
feature and is being built alongside. What matters here is the envelope around
it, and the envelope must be provable on its own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.services import link_throttle
from app.services import worksheet_links as wl

ALL_SHEETS = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")
PIN = "482913"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """The miss counter is process-wide by design. Tests share a process."""
    link_throttle.clear()
    yield
    link_throttle.clear()


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
    # `is_open` is a property on the real model; the fake carries the same rule.
    link.is_open = (
        link.revoked_at is None
        and (link.expires_at is None or link.expires_at > datetime.now(UTC))
    )
    return link


def _db(link, *, sheets=ALL_SHEETS, profile=None, worksheet=None):
    """execute() answers the link lookup first, then the sheet-scope query;
    get() dispatches on the model. add() collects what was written."""
    profile = profile or SimpleNamespace(id=getattr(link, "profile_id", uuid.uuid4()))
    worksheet = worksheet or SimpleNamespace(
        id=getattr(link, "worksheet_id", uuid.uuid4()), revision=17
    )
    calls = {"n": 0}
    added: list = []

    async def execute(_stmt):
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(scalar_one_or_none=lambda: link)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(sheets)))

    async def get(model, key, **_kw):
        return {
            "ApplicationProfile": profile,
            "FinancialWorksheet": worksheet,
        }.get(model.__name__)

    db = SimpleNamespace(
        execute=execute,
        get=get,
        flush=AsyncMock(),
        commit=AsyncMock(),
        add=added.append,
        added=added,
        profile=profile,
        worksheet=worksheet,
    )
    return db


def _request(ip="203.0.113.4", agent="Mozilla/5.0 (Accountant)"):
    return SimpleNamespace(
        headers={"x-forwarded-for": ip, "user-agent": agent},
        client=SimpleNamespace(host=ip),
    )


def _sheet(kind, values):
    return {"kind": kind, "values": dict(values), "rows": [], "computed": {}}


def _read_payload(kinds):
    return {
        "layout_version": "qc_sheets.v1",
        "worksheet_id": uuid.uuid4(),
        "revision": 17,
        "scope": {"can_edit": True, "sheets": list(kinds), "open_at": kinds[0]},
        "sheets": [_sheet(kind, {f"{kind}_cell": "1"}) for kind in kinds],
    }


async def _open(db, **kw):
    return await wl.resolve(db, "tok", client_ip="203.0.113.4", pin=PIN, **kw)


# ---------------------------------------------------------------------------
# the uniform miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_miss_reads_the_same():
    """Unknown, expired, revoked, the wrong kind, a link that opens nothing —
    one body. Telling them apart tells a prober which tokens are real."""
    bodies = set()
    cases = [
        (None, ALL_SHEETS),
        (_link(revoked_at=datetime.now(UTC)), ALL_SHEETS),
        (_link(expires_at=datetime.now(UTC) - timedelta(minutes=1)), ALL_SHEETS),
        (_link(kind="pfs"), ALL_SHEETS),
        (_link(), ()),  # a link with no sheets opens nothing
    ]
    for n, (link, sheets) in enumerate(cases):
        link_throttle.clear()
        db = _db(link, sheets=sheets)
        with pytest.raises(HTTPException) as err:
            await wl.resolve(db, "tok", pin=PIN, client_ip=f"203.0.113.{n}")
        assert err.value.status_code == 404
        bodies.add(err.value.detail)
    assert len(bodies) == 1, bodies

    # And a worksheet whose row was deleted out from under the link is the same.
    link_throttle.clear()
    db = _db(_link(), worksheet=None)
    db.get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as err:
        await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.9")
    assert err.value.status_code == 404 and err.value.detail in bodies


@pytest.mark.asyncio
async def test_a_derived_child_token_does_not_resolve():
    """The packet mints `{base}.{kind}`, so any child yields the base and the
    base yields all four children. A worksheet token has no structure to walk,
    and the dot is refused before the database is touched."""
    link = _link()
    db = _db(link)
    for token in ("tok.pfs", "tok.p_and_l", ".", "tok."):
        link_throttle.clear()
        with pytest.raises(HTTPException) as err:
            await wl.resolve(db, token, pin=PIN, client_ip="203.0.113.4")
        assert err.value.status_code == 404
        assert err.value.detail == wl.gone().detail
    # It never reached the lookup: the link row was never read.
    assert link.use_count == 0 and link.last_used_at is None


@pytest.mark.asyncio
async def test_one_address_hammering_a_token_is_throttled_before_the_pin():
    db = _db(None)
    key = link_throttle.miss_key("tok", "198.51.100.1", prefix="worksheet")
    for _ in range(link_throttle.MISS_LIMIT):
        link_throttle.note_miss(key)
    with pytest.raises(HTTPException) as err:
        await wl.resolve(db, "tok", pin=PIN, client_ip="198.51.100.1")
    assert err.value.status_code == 429
    # A different address is unaffected: the key is the pair, not the token.
    db = _db(_link())
    access, _s, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="198.51.100.2")
    assert access.sheets == ALL_SHEETS


# ---------------------------------------------------------------------------
# the PIN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pin_locks_on_the_row_and_a_restart_does_not_forget():
    link = _link()
    # No credential at all: asked for the PIN, told the sharer's label, no more.
    with pytest.raises(HTTPException) as err:
        await wl.resolve(_db(link), "tok", client_ip="203.0.113.4")
    assert err.value.status_code == 401
    assert err.value.detail == {"code": "pin_required", "label": "For the accountant"}

    for n in range(1, 5):
        link_throttle.clear()
        with pytest.raises(HTTPException) as err:
            await wl.resolve(_db(link), "tok", pin="000000", client_ip=f"203.0.113.{n}")
        assert err.value.status_code == 401 and err.value.detail["code"] == "pin_invalid"
        assert link.pin_attempts == n and link.pin_locked_until is None

    link_throttle.clear()
    with pytest.raises(HTTPException):
        await wl.resolve(_db(link), "tok", pin="000000", client_ip="203.0.113.5")
    assert link.pin_locked_until is not None and link.pin_attempts == 0

    # The deploy that everybody worries about: process memory is wiped, the row
    # is not, and the right PIN is still refused.
    link_throttle.clear()
    with pytest.raises(HTTPException) as err:
        await wl.resolve(_db(link), "tok", pin=PIN, client_ip="203.0.113.6")
    assert err.value.status_code == 429 and err.value.detail["code"] == "pin_locked"

    link.pin_locked_until = datetime.now(UTC) - timedelta(seconds=1)
    access, session, expires = await wl.resolve(
        _db(link), "tok", pin=PIN, client_ip="203.0.113.7"
    )
    assert session and expires > datetime.now(UTC)
    assert link.pin_attempts == 0 and link.pin_locked_until is None
    assert link.use_count == 1 and link.last_used_at is not None
    assert access.can_edit and access.sheets == ALL_SHEETS


@pytest.mark.asyncio
async def test_rotating_the_pin_ends_a_live_session():
    link = _link()
    _access, session, _e = await wl.resolve(_db(link), "tok", pin=PIN, client_ip="203.0.113.4")
    again, none_session, _ = await wl.resolve(
        _db(link), "tok", session=session, client_ip="203.0.113.4"
    )
    assert again.link is link and none_session is None

    link.pin_set_at = datetime.now(UTC)  # the sharer rotated it
    with pytest.raises(HTTPException) as err:
        await wl.resolve(_db(link), "tok", session=session, client_ip="203.0.113.4")
    assert err.value.status_code == 401 and err.value.detail["code"] == "pin_required"

    # A forged signature is not a session either.
    body = session.split(".", 1)[0]
    with pytest.raises(HTTPException):
        await wl.resolve(_db(link), "tok", session=f"{body}.{'0' * 32}", client_ip="203.0.113.4")


@pytest.mark.asyncio
async def test_a_link_with_no_pin_opens_without_one():
    """View-only links may be shared without a second factor; the PIN is
    mandatory only where the mint path makes it so."""
    link = _link(pin_hash=None, pin_set_at=None, permission="view")
    access, session, _e = await wl.resolve(_db(link), "tok", client_ip="203.0.113.4")
    assert session is None and access.can_edit is False


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_view_only_link_reads_but_never_writes():
    from app.routers import worksheets as routes

    link = _link(permission="view")
    kinds = ("p_and_l", "balance_sheet")
    request = _request()

    from app.services import sheets

    with patch.object(
        sheets, "read_sheets", AsyncMock(return_value=_read_payload(kinds)), create=True
    ):
        db = _db(link, sheets=kinds)
        _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")
        payload = await routes.read_worksheet("tok", request, _db(link, sheets=kinds), session)
    assert payload["can_edit"] is False and payload["scope"]["can_edit"] is False
    assert [s["kind"] for s in payload["sheets"]] == list(kinds)

    apply = AsyncMock(return_value={"rev": {}, "computed": {}, "resync": []})
    with patch.object(sheets, "apply_cell_edits", apply, create=True):
        body = routes.CellsBody(edits=[{"sheet": "p_and_l", "key": "gross_revenue", "value": "5"}])
        with pytest.raises(HTTPException) as err:
            await routes.write_worksheet_cells(
                "tok", body, request, _db(link, sheets=kinds), session
            )
    # 403, not 404: they hold a link we issued and it opened a second ago.
    assert err.value.status_code == 403
    assert apply.await_count == 0


@pytest.mark.asyncio
async def test_a_link_without_the_pfs_never_sees_it_and_cannot_write_to_it():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link()
    kinds = ("p_and_l", "balance_sheet", "debt_schedule")
    request = _request()
    db = _db(link, sheets=kinds)
    _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")

    # Even if the grid service answered with the personal statement anyway, the
    # route drops it. Two filters, because this one is the whole promise.
    leaky = _read_payload(ALL_SHEETS)
    with patch.object(sheets, "read_sheets", AsyncMock(return_value=leaky), create=True):
        payload = await routes.read_worksheet("tok", request, _db(link, sheets=kinds), session)
    assert [s["kind"] for s in payload["sheets"]] == list(kinds)
    assert "pfs" not in payload["scope"]["sheets"]
    assert "pfs" not in str(payload["sheets"])

    apply = AsyncMock(return_value={"rev": {}, "computed": {}, "resync": []})
    with patch.object(sheets, "apply_cell_edits", apply, create=True):
        body = routes.CellsBody(edits=[{"sheet": "pfs", "key": "cash_on_hand", "value": "9"}])
        with pytest.raises(HTTPException) as err:
            await routes.write_worksheet_cells(
                "tok", body, request, _db(link, sheets=kinds), session
            )
    # 404, not 403: a 403 would confirm the file has a personal statement.
    assert err.value.status_code == 404 and err.value.detail == wl.gone().detail
    assert apply.await_count == 0

    # One in-scope cell does not carry an out-of-scope one in with it.
    with patch.object(sheets, "apply_cell_edits", apply, create=True):
        body = routes.CellsBody(
            edits=[
                {"sheet": "p_and_l", "key": "gross_revenue", "value": "5"},
                {"sheet": "pfs", "key": "cash_on_hand", "value": "9"},
            ]
        )
        with pytest.raises(HTTPException) as err:
            await routes.write_worksheet_cells(
                "tok", body, request, _db(link, sheets=kinds), session
            )
    assert err.value.status_code == 404 and apply.await_count == 0


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_accepted_write_leaves_a_row_with_before_and_after():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link()
    request = _request(ip="198.51.100.44", agent="Firefox/141")
    db = _db(link)
    _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")

    before = {
        "layout_version": "qc_sheets.v1",
        "scope": {},
        "sheets": [_sheet("p_and_l", {"gross_revenue": "1,000", "supplies": "40"})],
    }
    after = {
        "layout_version": "qc_sheets.v1",
        "scope": {},
        "sheets": [_sheet("p_and_l", {"gross_revenue": "1,250", "supplies": "40"})],
    }
    reads = AsyncMock(side_effect=[before, after])
    apply = AsyncMock(return_value={"rev": {"p_and_l": 18}, "computed": {}, "resync": []})
    write_db = _db(link)
    with (
        patch.object(sheets, "read_sheets", reads, create=True),
        patch.object(sheets, "apply_cell_edits", apply, create=True),
    ):
        body = routes.CellsBody(
            edits=[
                {"sheet": "p_and_l", "key": "gross_revenue", "value": "1250"},
                {"sheet": "p_and_l", "key": "supplies", "value": "40"},
            ],
            base_rev={"p_and_l": 17},
            name="Marisol (bookkeeper)",
        )
        result = await routes.write_worksheet_cells("tok", body, request, write_db, session)

    assert result["rev"] == {"p_and_l": 18}
    assert apply.await_args.kwargs["origin"] == "client_form"
    assert apply.await_args.kwargs["actor_user_id"] is None

    (row,) = write_db.added
    assert row.sheet_kind == "p_and_l" and row.revision == 18
    assert row.link_id == link.id and row.actor_user_id is None and row.via == "share_link"
    assert row.ip == "198.51.100.44" and row.user_agent == "Firefox/141"
    # Proved and claimed are different fields, and only what moved is recorded.
    assert row.claimed_name == "Marisol (bookkeeper)"
    assert row.changes == {
        "p_and_l.gross_revenue": {"before": "1,000", "after": "1,250"}
    }


@pytest.mark.asyncio
async def test_a_write_that_changes_nothing_writes_no_history():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link()
    same = {"scope": {}, "sheets": [_sheet("p_and_l", {"gross_revenue": "1,000"})]}
    db = _db(link)
    _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")
    write_db = _db(link)
    with (
        patch.object(sheets, "read_sheets", AsyncMock(side_effect=[same, same]), create=True),
        patch.object(
            sheets,
            "apply_cell_edits",
            AsyncMock(return_value={"rev": {"p_and_l": 18}, "computed": {}, "resync": []}),
            create=True,
        ),
    ):
        body = routes.CellsBody(
            edits=[{"sheet": "p_and_l", "key": "gross_revenue", "value": "1000"}]
        )
        await routes.write_worksheet_cells("tok", body, _request(), write_db, session)
    assert write_db.added == []


@pytest.mark.asyncio
async def test_a_row_insert_records_the_shape_it_changed():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link()
    before = {
        "scope": {},
        "sheets": [
            {
                "kind": "debt_schedule",
                "values": {},
                "rows": [
                    {"kind": "header", "r": 1},
                    {"kind": "data", "r": 2, "row_key": "a", "block": "debts"},
                ],
            }
        ],
    }
    after_rows = [
        {"kind": "header", "r": 1},
        {"kind": "data", "r": 2, "row_key": "a", "block": "debts"},
        {"kind": "data", "r": 3, "row_key": "b", "block": "debts"},
    ]
    db = _db(link)
    _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")
    write_db = _db(link)
    with (
        patch.object(sheets, "read_sheets", AsyncMock(return_value=before), create=True),
        patch.object(
            sheets,
            "apply_row_op",
            AsyncMock(return_value={"rows": after_rows, "rev": {"debt_schedule": 19}}),
            create=True,
        ),
    ):
        body = routes.RowsBody(sheet="debt_schedule", op="insert", after="a")
        result = await routes.write_worksheet_rows("tok", body, _request(), write_db, session)

    assert [r["row_key"] for r in result["rows"] if r["kind"] == "data"] == ["a", "b"]
    (row,) = write_db.added
    assert row.sheet_kind == "debt_schedule" and row.revision == 19
    assert row.changes["debt_schedule.rows"]["before"] == ["a"]
    assert row.changes["debt_schedule.rows"]["after"] == ["a", "b"]
    assert row.changes["debt_schedule.rows"]["op"] == "insert"


@pytest.mark.asyncio
async def test_a_view_only_link_cannot_add_rows_either():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link(permission="view")
    db = _db(link)
    _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")
    op = AsyncMock()
    with patch.object(sheets, "apply_row_op", op, create=True):
        body = routes.RowsBody(sheet="debt_schedule", op="delete", row_id="a")
        with pytest.raises(HTTPException) as err:
            await routes.write_worksheet_rows("tok", body, _request(), _db(link), session)
    assert err.value.status_code == 403 and op.await_count == 0


@pytest.mark.asyncio
async def test_a_link_cannot_write_in_a_loop():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link()
    db = _db(link)
    _a, session, _e = await wl.resolve(db, "tok", pin=PIN, client_ip="203.0.113.4")
    payload = {"scope": {}, "sheets": [_sheet("p_and_l", {"gross_revenue": "1"})]}
    with (
        patch.object(sheets, "read_sheets", AsyncMock(return_value=payload), create=True),
        patch.object(
            sheets,
            "apply_cell_edits",
            AsyncMock(return_value={"rev": {"p_and_l": 1}, "computed": {}, "resync": []}),
            create=True,
        ),
    ):
        body = routes.CellsBody(edits=[{"sheet": "p_and_l", "key": "gross_revenue", "value": "1"}])
        for _ in range(link_throttle.WRITE_BURST_LIMIT):
            await routes.write_worksheet_cells("tok", body, _request(), _db(link), session)
        with pytest.raises(HTTPException) as err:
            await routes.write_worksheet_cells("tok", body, _request(), _db(link), session)
    assert err.value.status_code == 429


@pytest.mark.asyncio
async def test_unlock_returns_a_session_the_read_accepts():
    from app.routers import worksheets as routes
    from app.services import sheets

    link = _link()
    unlocked = await routes.unlock_worksheet(
        "tok", routes.WorksheetUnlockBody(pin=PIN), _request(), _db(link)
    )
    assert unlocked.session and unlocked.expires_at > datetime.now(UTC)
    with patch.object(
        sheets, "read_sheets", AsyncMock(return_value=_read_payload(ALL_SHEETS)), create=True
    ):
        payload = await routes.read_worksheet(
            "tok", _request(), _db(link), unlocked.session
        )
    assert payload["can_edit"] is True and payload["completed"] is False
    assert payload["scope"]["open_at"] == "p_and_l"


@pytest.mark.asyncio
async def test_a_pin_is_mandatory_where_the_stakes_say_so():
    assert wl.pin_required_for("edit", ["p_and_l"]) is True
    assert wl.pin_required_for("view", ["pfs"]) is True
    assert wl.pin_required_for("view", ["p_and_l", "balance_sheet"]) is False

    link = _link(pin_hash=None, pin_set_at=None)
    code = await wl.set_pin(link)
    assert len(code) == 6 and code.isdigit() and link.pin_hash and link.pin_set_at
    # And the PIN it set is the one that opens the link.
    access, session, _e = await wl.resolve(_db(link), "tok", pin=code, client_ip="203.0.113.4")
    assert session and access.can_edit
    # Rotating it ends that session, without a revocation list anywhere.
    await wl.set_pin(link, "739104")
    with pytest.raises(HTTPException) as err:
        await wl.resolve(_db(link), "tok", session=session, client_ip="203.0.113.4")
    assert err.value.detail["code"] == "pin_required"


@pytest.mark.asyncio
async def test_a_wrong_pin_is_committed_not_merely_flushed():
    """`get_db` rolls back on any exception, and a wrong PIN raises one. A
    counter that is only flushed is a counter that never reaches five."""
    link = _link()
    db = _db(link)
    with pytest.raises(HTTPException):
        await wl.resolve(db, "tok", pin="000000", client_ip="203.0.113.4")
    assert link.pin_attempts == 1
    assert db.commit.await_count == 1, "the attempt would be rolled back on the way out"


# --- the mint route must actually set the PIN it is designed around ---------


def test_an_edit_link_and_any_pfs_link_require_a_pin():
    """`pin_required_for` is the rule; the mint route is what has to honour it."""
    from app.services import worksheet_links as wl

    assert wl.pin_required_for("edit", ["p_and_l"]) is True
    assert wl.pin_required_for("view", ["pfs"]) is True
    assert wl.pin_required_for("view", ["p_and_l", "balance_sheet"]) is False


def test_the_mint_route_sets_a_pin_when_the_rule_says_so():
    """Source pin: the route calls the rule and then set_pin, and hands the code
    back once. Without this the PIN columns exist and nothing ever writes one."""
    import inspect

    from app.routers import application_profiles as ap

    source = inspect.getsource(ap.mint_worksheet_link)
    assert "pin_required_for(payload.permission, wanted)" in source
    assert "await worksheet_links.set_pin(link)" in source
    assert '"pin": pin' in source
