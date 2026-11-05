"""Contract item 9: review drafts for unpublished programs, never published."""

from __future__ import annotations

import io
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.schemas.funding_program import FundingProgramVersionCreate
from app.scripts import ensure_program_review_drafts as script
from app.services import funding_program_drafts as drafts_mod
from app.services.funding_program_baselines import suggested_baseline
from app.services.funding_program_drafts import (
    REVIEW_DRAFT_DESCRIPTION,
    REVIEW_DRAFT_REASON,
    RULES_SOURCE_BASELINE,
    RULES_SOURCE_LEGACY,
    ensure_review_drafts,
    plan_review_drafts,
    review_draft_name,
)

LEGACY_FIT = {
    "fit": {"all": [{"field": "annual_revenue", "op": "gte", "value": 250_000}]},
    "priority": 5,
}


def _program(key: str, name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        program_key=key,
        name=name or key.replace("_", " ").title(),
        short_description=None,
        status="active",
    )


def _draft(program: SimpleNamespace, *, version: int = 1, rules: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        funding_program_id=program.id,
        version=version,
        status="draft",
        playbook_type="loan_product",
        rules=dict(rules or {}),
    )


def _scope(program: SimpleNamespace, vertical: str = "main_street") -> SimpleNamespace:
    return SimpleNamespace(
        program_id=program.id,
        vertical=vertical,
        scope_key="default",
        intake_variants=[],
        intent_keys=[],
        naics_prefixes=["44"],
        excluded_naics_prefixes=[],
        industry_keys=[],
        required_fact_keys=[],
        is_active=True,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), email="admin@example.com", role=Role.SUPER_ADMIN)


@contextmanager
def _catalog(programs, *, published=None, drafts=None, counts=None, scopes=None, create=None):
    create = create or AsyncMock(
        side_effect=lambda db, program, payload, user: SimpleNamespace(id=uuid4(), version=7)
    )
    publish = AsyncMock()
    with patch.object(
        drafts_mod.funding_programs, "catalog_rows", AsyncMock(return_value=list(programs))
    ), patch.object(
        drafts_mod.funding_programs,
        "published_versions_by_program",
        AsyncMock(return_value=dict(published or {})),
    ), patch.object(
        drafts_mod.funding_programs, "scopes_by_program", AsyncMock(return_value=dict(scopes or {}))
    ), patch.object(
        drafts_mod, "latest_drafts_by_program", AsyncMock(return_value=dict(drafts or {}))
    ), patch.object(
        drafts_mod, "requirement_counts", AsyncMock(return_value=dict(counts or {}))
    ), patch.object(drafts_mod.funding_programs, "create_version", create), patch.object(
        drafts_mod.funding_programs, "publish_version", publish
    ):
        yield SimpleNamespace(create=create, publish=publish)


def _db() -> SimpleNamespace:
    return SimpleNamespace(execute=AsyncMock(), add=Mock(), flush=AsyncMock(), commit=AsyncMock())


# --- skip rules -------------------------------------------------------------


@pytest.mark.asyncio
async def test_arf_programs_are_skipped_even_without_a_published_version() -> None:
    arf = _program("arf_working_capital")
    with _catalog([arf]) as fakes:
        plan = await plan_review_drafts(_db())
        created = await ensure_review_drafts(_db(), user=_user(), plan=plan)

    assert created == []
    assert plan[0].action == "skip"
    assert "arf_" in plan[0].reason
    fakes.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_programs_with_a_published_version_are_skipped() -> None:
    program = _program("equipment_financing")
    with _catalog([program], published={program.id: SimpleNamespace(version=4)}) as fakes:
        created = await ensure_review_drafts(_db(), user=_user())

    assert created == []
    fakes.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_authored_drafts_are_left_alone() -> None:
    program = _program("ez_term")
    authored = _draft(program, version=2, rules={})
    with _catalog([program], drafts={program.id: authored}, counts={authored.id: 3}) as fakes:
        plan = await plan_review_drafts(_db())
        created = await ensure_review_drafts(_db(), user=_user(), plan=plan)

    assert created == []
    assert plan[0].action == "skip"
    assert "3 requirement row(s)" in plan[0].reason
    fakes.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_latest_draft_decides_whether_a_person_authored_it() -> None:
    """A newer empty legacy draft behind an older hand-authored one still counts as human work."""
    program = _program("line_of_credit")
    latest = _draft(program, version=3, rules=LEGACY_FIT)
    with _catalog([program], drafts={program.id: latest}, counts={latest.id: 0}) as fakes:
        created = await ensure_review_drafts(_db(), user=_user())

    assert [item["program_key"] for item in created] == ["line_of_credit"]
    fakes.create.assert_awaited_once()


# --- rules source -----------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_fit_rules_win_when_the_latest_draft_has_them() -> None:
    program = _program("ez_term", name="EZ Term Loan")
    legacy = _draft(program, version=1, rules=LEGACY_FIT)
    db = _db()
    user = _user()
    with _catalog([program], drafts={program.id: legacy}, counts={legacy.id: 0}) as fakes:
        created = await ensure_review_drafts(db, user=user)

    fakes.create.assert_awaited_once()
    args = fakes.create.await_args.args
    assert args[0] is db
    assert args[1] is program
    assert args[3] is user
    payload = args[2]
    assert isinstance(payload, FundingProgramVersionCreate)
    assert payload.rules == LEGACY_FIT
    assert "baseline" not in payload.rules
    assert payload.name == "EZ Term Loan review draft"
    assert payload.description == REVIEW_DRAFT_DESCRIPTION
    assert payload.reason == REVIEW_DRAFT_REASON
    assert payload.confirmed is True
    baseline = suggested_baseline("ez_term", name="EZ Term Loan", current_scopes=[])
    assert [row.requirement_key for row in payload.requirements] == [
        row["requirement_key"] for row in baseline["requirements"]
    ]
    assert created == [
        {
            "program_key": "ez_term",
            "version": 7,
            "rules_source": RULES_SOURCE_LEGACY,
            "requirement_count": len(baseline["requirements"]),
        }
    ]
    fakes.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_baseline_rules_when_the_program_has_no_draft_at_all() -> None:
    program = _program("sba_express", name="SBA Express")
    with _catalog([program]) as fakes:
        created = await ensure_review_drafts(_db(), user=_user())

    payload = fakes.create.await_args.args[2]
    baseline = suggested_baseline("sba_express", name="SBA Express", current_scopes=[])
    assert payload.rules == baseline["rules"]
    assert payload.rules.get("fit")
    assert created[0]["rules_source"] == RULES_SOURCE_BASELINE
    assert created[0]["requirement_count"] == len(baseline["requirements"]) > 0


@pytest.mark.asyncio
async def test_baseline_rules_when_the_draft_has_no_fit_block() -> None:
    program = _program("microcap", name="MicroCap")
    empty = _draft(program, version=2, rules={"priority": 1, "fit": {}})
    with _catalog([program], drafts={program.id: empty}, counts={empty.id: 0}) as fakes:
        created = await ensure_review_drafts(_db(), user=_user())

    payload = fakes.create.await_args.args[2]
    assert payload.rules == suggested_baseline("microcap", name="MicroCap")["rules"]
    assert created[0]["rules_source"] == RULES_SOURCE_BASELINE


@pytest.mark.asyncio
async def test_requirements_always_come_from_the_baseline_with_existing_scopes() -> None:
    program = _program("jumbo_term", name="Jumbo Term")
    legacy = _draft(program, rules=LEGACY_FIT)
    scope = _scope(program, vertical="dealer")
    spy = Mock(side_effect=suggested_baseline)
    with _catalog(
        [program], drafts={program.id: legacy}, counts={legacy.id: 0}, scopes={program.id: [scope]}
    ) as fakes, patch.object(drafts_mod, "suggested_baseline", spy):
        created = await ensure_review_drafts(_db(), user=_user())

    spy.assert_called_once()
    assert spy.call_args.args == ("jumbo_term",)
    assert spy.call_args.kwargs["name"] == "Jumbo Term"
    passed_scopes = spy.call_args.kwargs["current_scopes"]
    assert isinstance(passed_scopes, list) and isinstance(passed_scopes[0], dict)
    assert passed_scopes[0]["vertical"] == "dealer"
    assert passed_scopes[0]["naics_prefixes"] == ["44"]
    payload = fakes.create.await_args.args[2]
    assert payload.rules == LEGACY_FIT
    assert created[0]["requirement_count"] == len(payload.requirements) > 0


# --- validation is reported, never raised -----------------------------------


@pytest.mark.asyncio
async def test_invalid_baseline_requirements_are_reported_not_raised() -> None:
    program = _program("portfolio_lending")
    broken = suggested_baseline("portfolio_lending")
    broken["requirements"][0]["category"] = "made_up_category"
    with _catalog([program]) as fakes, patch.object(
        drafts_mod, "suggested_baseline", Mock(return_value=broken)
    ):
        plan = await plan_review_drafts(_db())
        created = await ensure_review_drafts(_db(), user=_user(), plan=plan)

    assert created == []
    assert plan[0].action == "skip"
    assert plan[0].rules_source == RULES_SOURCE_BASELINE
    assert "baseline requirements failed validation" in plan[0].reason
    fakes.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_legacy_rules_are_reported_not_raised() -> None:
    program = _program("hybrid_term_loc")
    bad = _draft(program, rules={"fit": {"field": "made_up_field", "op": "gte", "value": 1}})
    with _catalog([program], drafts={program.id: bad}, counts={bad.id: 0}) as fakes:
        plan = await plan_review_drafts(_db())
        created = await ensure_review_drafts(_db(), user=_user(), plan=plan)

    assert created == []
    assert plan[0].action == "skip"
    assert plan[0].rules_source == RULES_SOURCE_LEGACY
    assert "legacy_draft rules failed validation" in plan[0].reason
    assert "made_up_field" in plan[0].reason
    fakes.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_rejection_from_create_version_is_reported_and_the_run_continues() -> None:
    first = _program("sba_grocery")
    second = _program("sba_made_in_america")

    async def _create(db, program, payload, user):
        if program is first:
            raise HTTPException(422, detail=[{"loc": ["body", "rules", "fit"], "msg": "nope"}])
        return SimpleNamespace(id=uuid4(), version=2)

    with _catalog([first, second], create=AsyncMock(side_effect=_create)) as fakes:
        plan = await plan_review_drafts(_db())
        created = await ensure_review_drafts(_db(), user=_user(), plan=plan)

    assert [item["program_key"] for item in created] == ["sba_made_in_america"]
    assert plan[0].action == "skip"
    assert "create_version rejected the draft" in plan[0].reason
    assert fakes.create.await_count == 2


# --- one draft per program, filters, naming ---------------------------------


@pytest.mark.asyncio
async def test_one_draft_per_eligible_program_and_nothing_is_published() -> None:
    programs = [
        _program("ez_term"),
        _program("floorplan_support"),
        _program("arf_working_capital"),
        _program("equipment_financing"),
    ]
    legacy = _draft(programs[0], rules=LEGACY_FIT)
    with _catalog(
        programs,
        published={programs[3].id: SimpleNamespace(version=1)},
        drafts={programs[0].id: legacy},
        counts={legacy.id: 0},
    ) as fakes:
        created = await ensure_review_drafts(_db(), user=_user())

    assert sorted(item["program_key"] for item in created) == ["ez_term", "floorplan_support"]
    assert fakes.create.await_count == 2
    assert {call.args[1].program_key for call in fakes.create.await_args_list} == {
        "ez_term",
        "floorplan_support",
    }
    fakes.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_program_keys_filter_restricts_the_plan_and_reports_unknown_keys() -> None:
    kept = _program("transportation_finance")
    other = _program("dealer_working_capital")
    with _catalog([other, kept]) as fakes:
        plan = await plan_review_drafts(_db(), program_keys=["transportation_finance", "nope"])
        created = await ensure_review_drafts(
            _db(), user=_user(), program_keys=["transportation_finance", "nope"]
        )

    assert [entry.program_key for entry in plan] == ["transportation_finance", "nope"]
    assert plan[0].action == "create"
    assert plan[1].action == "skip"
    assert plan[1].reason == "not an active catalog program"
    assert [item["program_key"] for item in created] == ["transportation_finance"]
    assert fakes.create.await_count == 1


def test_review_draft_name_is_bounded_to_the_column() -> None:
    assert review_draft_name("EZ Term Loan") == "EZ Term Loan review draft"
    long_name = "x" * 200
    bounded = review_draft_name(long_name)
    assert len(bounded) <= 160
    assert bounded.endswith(" review draft")


def test_every_unpublished_program_baseline_builds_a_valid_payload() -> None:
    """The live catalog's twelve unpublished non-ARF programs all validate as drafts."""
    keys = [
        "ez_term", "microcap", "line_of_credit", "jumbo_term", "hybrid_term_loc",
        "transportation_finance", "sba_grocery", "sba_made_in_america",
        "sba_express", "portfolio_lending", "dealer_working_capital", "floorplan_support",
    ]
    for key in keys:
        payload, source, error = drafts_mod.build_review_draft_payload(
            _program(key), legacy_draft=None, scopes=[]
        )
        assert error is None, (key, error)
        assert payload is not None and len(payload.requirements) > 0, key
        assert source == RULES_SOURCE_BASELINE


# --- script entry point -----------------------------------------------------


class _Session:
    def __init__(self, user) -> None:
        self.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: user)
        )
        self.commit = AsyncMock()
        self.add = Mock()
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


async def _run_script(argv: list[str], user) -> tuple[int, str, str, _Session]:
    session = _Session(user)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = await script.run(argv, session_factory=lambda: session)
    return code, out.getvalue(), err.getvalue(), session


@pytest.mark.asyncio
async def test_script_dry_run_prints_the_plan_and_writes_nothing() -> None:
    program = _program("ez_term")
    legacy = _draft(program, rules=LEGACY_FIT)
    with _catalog([program], drafts={program.id: legacy}, counts={legacy.id: 0}) as fakes:
        code, out, _, session = await _run_script(
            ["--user-email", "Admin@Example.com", "--dry-run"], _user()
        )

    assert code == 0
    assert "CREATE ez_term: rules from legacy_draft" in out
    assert "Dry run: nothing written." in out
    fakes.create.assert_not_awaited()
    fakes.publish.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_script_live_run_creates_drafts_and_commits_once() -> None:
    program = _program("sba_express")
    with _catalog([program]) as fakes:
        code, out, _, session = await _run_script(["--user-email", "admin@example.com"], _user())

    assert code == 0
    fakes.create.assert_awaited_once()
    fakes.publish.assert_not_awaited()
    session.commit.assert_awaited_once()
    assert "Created 1 review draft(s)." in out
    assert "sba_express: version 7 (baseline" in out


@pytest.mark.asyncio
async def test_script_refuses_an_unknown_user_before_reading_the_catalog() -> None:
    with _catalog([_program("sba_express")]) as fakes:
        code, _, err, session = await _run_script(["--user-email", "ghost@example.com"], None)

    assert code == 2
    assert "no active user" in err
    fakes.create.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_script_refuses_a_non_super_admin() -> None:
    borrower = SimpleNamespace(id=uuid4(), email="uw@example.com", role=Role.CLIENT)
    with _catalog([_program("sba_express")]) as fakes:
        code, _, err, session = await _run_script(["--user-email", "uw@example.com"], borrower)

    assert code == 3
    assert "not a super admin" in err
    fakes.create.assert_not_awaited()
    session.commit.assert_not_awaited()
