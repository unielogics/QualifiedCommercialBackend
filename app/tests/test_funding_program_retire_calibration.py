"""Retiring a funding program must tell the files that still hold it.

Retire used to be a status flip and nothing more: `active_selections` never
joins the catalog, so a withdrawn program kept generating requirements and
kept appearing in a readiness snapshot that an AI prompt reads back. These
tests pin the two halves of the fix — the file is told, and readiness is
refreshed for exactly the files affected.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.routers import funding_programs as funding_programs_router
from app.schemas.application_profile import ApplicationProgramSelectionRead
from app.services import application_programs, funding_programs


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def unique(self) -> "_Result":
        return self

    def all(self) -> list[object]:
        return list(self._rows)


class _DB:
    """Records the statements it is asked to run; returns canned rows."""

    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = list(rows or [])
        self.statements: list[object] = []
        self.commits = 0

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)

    async def commit(self) -> None:
        self.commits += 1


async def test_only_a_retired_catalog_row_counts_as_retired() -> None:
    # 'business_baseline' is a live selection key that is not a catalog row at
    # all. Absent must not be mistaken for retired.
    db = _DB(["sba_7a"])
    retired = await funding_programs.retired_program_keys(
        db, ["sba_7a", "business_baseline"]
    )
    assert retired == {"sba_7a"}
    assert "retired" in str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))


async def test_no_selected_keys_asks_the_database_nothing() -> None:
    db = _DB(["sba_7a"])
    assert await funding_programs.retired_program_keys(db, []) == set()
    assert await funding_programs.retired_program_keys(db, ["", None]) == set()  # type: ignore[list-item]
    assert db.statements == []


async def test_a_session_stub_without_execute_is_tolerated() -> None:
    # Readiness is exercised with fake sessions throughout the suite; a new
    # lookup there must not break them.
    assert await funding_programs.retired_program_keys(SimpleNamespace(), ["sba_7a"]) == set()
    assert await application_programs.profiles_with_active_selection(
        SimpleNamespace(), "sba_7a"
    ) == []


async def test_a_selection_is_not_flagged_retired_by_default() -> None:
    row = ApplicationProgramSelectionRead(
        id=uuid.uuid4(),
        program_key="sba_7a",
        program_name="SBA 7(a)",
        playbook_id=uuid.uuid4(),
        playbook_version=1,
        source="operator",
        selected_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    assert row.program_retired is False


async def test_affected_files_are_the_ones_holding_the_program() -> None:
    profile = SimpleNamespace(id=uuid.uuid4())
    db = _DB([profile])
    found = await application_programs.profiles_with_active_selection(db, "sba_7a")
    assert found == [profile]
    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "sba_7a" in sql
    # Only live selections; a removed one must not drag a file back in.
    assert "removed_at IS NULL" in sql


async def test_an_empty_program_key_asks_nothing() -> None:
    db = _DB()
    assert await application_programs.profiles_with_active_selection(db, "") == []
    assert db.statements == []


async def test_retiring_refreshes_readiness_only_for_the_files_that_hold_it() -> None:
    program = SimpleNamespace(program_key="sba_7a", name="SBA 7(a)")
    held = [SimpleNamespace(id=uuid.uuid4())]
    db = _DB()
    payload = SimpleNamespace(retired=True, reason="withdrawn", confirmed=True)

    with patch.object(
        funding_programs_router.funding_programs,
        "catalog_item_or_404",
        AsyncMock(return_value=program),
    ), patch.object(
        funding_programs_router.funding_programs, "retire_program", AsyncMock()
    ) as retire, patch.object(
        funding_programs_router.funding_programs,
        "admin_catalog",
        AsyncMock(return_value=[]),
    ), patch.object(
        funding_programs_router.application_programs,
        "profiles_with_active_selection",
        AsyncMock(return_value=held),
    ) as affected, patch.object(
        funding_programs_router.capital_readiness,
        "advisory_recompute_profiles",
        AsyncMock(),
    ) as recompute:
        await funding_programs_router.retire_funding_program(
            "sba_7a", payload, SimpleNamespace(id=uuid.uuid4()), db
        )

    retire.assert_awaited_once()
    affected.assert_awaited_once_with(db, "sba_7a")
    recompute.assert_awaited_once()
    assert recompute.await_args.args[1] == held
    event_key = recompute.await_args.kwargs["event_key"]
    assert event_key.startswith("funding-program:sba_7a:retired:")
    # Readiness must be refreshed before the transaction closes.
    assert db.commits == 1


async def test_restoring_refreshes_readiness_under_its_own_event_key() -> None:
    program = SimpleNamespace(program_key="sba_7a", name="SBA 7(a)")
    db = _DB()
    payload = SimpleNamespace(retired=False, reason="back", confirmed=True)

    with patch.object(
        funding_programs_router.funding_programs,
        "catalog_item_or_404",
        AsyncMock(return_value=program),
    ), patch.object(
        funding_programs_router.funding_programs, "retire_program", AsyncMock()
    ), patch.object(
        funding_programs_router.funding_programs,
        "admin_catalog",
        AsyncMock(return_value=[]),
    ), patch.object(
        funding_programs_router.application_programs,
        "profiles_with_active_selection",
        AsyncMock(return_value=[]),
    ), patch.object(
        funding_programs_router.capital_readiness,
        "advisory_recompute_profiles",
        AsyncMock(),
    ) as recompute:
        await funding_programs_router.retire_funding_program(
            "sba_7a", payload, SimpleNamespace(id=uuid.uuid4()), db
        )

    assert "restored" in recompute.await_args.kwargs["event_key"]
