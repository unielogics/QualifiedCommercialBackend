from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from scripts.backfill_financial_workspace import (
    Inventory,
    LegacyForm,
    Original,
    parse_args,
    plan_inventory,
    process_profile,
)


def form(subject="Jane Smith", period=("2026-11-01",), **kwargs):
    return LegacyForm(str(uuid4()), subject, period, True, **kwargs)


def test_backfill_defaults_to_dry_run_and_supports_cursor():
    assert parse_args([]).apply is False
    cursor = uuid4()
    assert parse_args(["--cursor", str(cursor), "--limit", "25"]).after_profile_id == cursor


def test_backfill_skips_empty_and_retains_managed_families():
    plans = {row.kind: row for row in plan_inventory(Inventory(managed={"pfs"}))}
    assert plans["pfs"].status == "already_managed"
    assert all(row.status == "empty" for kind, row in plans.items() if kind != "pfs")


def test_backfill_reports_ambiguous_owners_and_periods_instead_of_merging():
    inventory = Inventory(structured={"pfs": [form(), form(subject="John Smith")]})
    assert next(row for row in plan_inventory(inventory) if row.kind == "pfs").status == "requires_review"
    inventory.structured["pfs"] = [form(period=("2026-11-01",)), form(period=("2025-11-01",))]
    assert next(row for row in plan_inventory(inventory) if row.kind == "pfs").status == "requires_review"


def test_backfill_leaves_original_only_pfs_for_explicit_mapping():
    inventory = Inventory(originals={"pfs": [Original("original-a", "Jane Smith", ("2026-11-01",))]})
    plan = next(row for row in plan_inventory(inventory) if row.kind == "pfs")
    assert plan.status == "requires_review"
    assert plan.review_file_ids == ["original-a"]


def test_backfill_adopts_debts_and_safe_structured_baselines_only():
    inventory = Inventory(debt_count=2, dealer_only_debt_count=1, structured={"pfs": [form()]})
    eligible = [row.kind for row in plan_inventory(inventory) if row.status == "eligible"]
    assert eligible == ["debt_schedule", "pfs"]


@pytest.mark.asyncio
async def test_dry_run_never_resolves_or_mutates_any_family():
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock(), execute=AsyncMock(), add=AsyncMock())
    inventory = Inventory(debt_count=2, structured={"pfs": [form()]})
    resolve = AsyncMock()
    with patch("scripts.backfill_financial_workspace.inspect_inventory", AsyncMock(return_value=inventory)):
        result = await process_profile(db, SimpleNamespace(id=uuid4()), resolver=resolve)
    assert result["mode"] == "dry_run" and result["adopted"] == []
    resolve.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_apply_resolves_only_eligible_kind_and_commits_once():
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    family = SimpleNamespace(id=uuid4(), current_version_id=None)
    resolve = AsyncMock(return_value=(family, object()))
    with patch("scripts.backfill_financial_workspace.inspect_inventory", AsyncMock(return_value=Inventory(debt_count=1))):
        result = await process_profile(db, SimpleNamespace(id=uuid4()), apply=True, resolver=resolve)
    assert [row["kind"] for row in result["adopted"]] == ["debt_schedule"]
    assert resolve.await_count == 1
    assert resolve.await_args.kwargs["kind"] == "debt_schedule"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_rerun_does_not_resolve_already_managed_kinds():
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    resolve = AsyncMock()
    with patch("scripts.backfill_financial_workspace.inspect_inventory", AsyncMock(return_value=Inventory(managed={"debt_schedule"}, debt_count=1))):
        await process_profile(db, SimpleNamespace(id=uuid4()), apply=True, resolver=resolve)
    resolve.assert_not_awaited()
    db.commit.assert_not_awaited()
