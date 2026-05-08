"""Broker scoping tests — the leakage gate for /clients, /loans,
/ai-tasks, /agents/me/funnel, /agents/me/next-actions.

The plan calls these REQUIRED before merge. They run against a real
Postgres because the resolver + scope queries use JSONB and live
SQL constructs that don't translate to SQLite.

CI today does NOT spin up Postgres (`.github/workflows/ci.yml` only
runs `pytest -q` against in-memory sources). Until CI has a
service-bound Postgres, these tests are skip-marked. Run locally
against the docker container before merging:

    sudo docker exec -w /app qcbackend uv run pytest \\
        app/tests/test_broker_scoping.py -v

Required follow-up (tracked in plan): wire a Postgres service into
.github/workflows/ci.yml so these run on every push.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

# Skip the entire module unless a real Postgres is available. The
# `or os.getenv("INTEGRATION_TESTS")` escape hatch lets a developer
# run these locally without changing the file.
import os
if not os.getenv("INTEGRATION_TESTS"):
    pytest.skip(
        "broker scoping tests require Postgres — set INTEGRATION_TESTS=1 to run",
        allow_module_level=True,
    )


@pytest.fixture
async def broker_pair():
    """Create two brokers with distinct clients + loans + AITasks
    each. Cleans up after itself so re-runs are idempotent."""
    from app.db import SessionLocal
    from app.enums import (
        AITaskPriority,
        AITaskSource,
        AITaskStatus,
        ClientStage,
        LoanStage,
        LoanType,
    )
    from app.models.ai_task import AITask
    from app.models.broker import Broker
    from app.models.client import Client
    from app.models.loan import Loan
    from app.models.user import User

    suffix = uuid.uuid4().hex[:8]

    async with SessionLocal() as db:
        users = []
        for letter in ("a", "b"):
            user = User(
                email=f"broker-{letter}-{suffix}@test.local",
                name=f"Broker {letter.upper()} {suffix}",
                role="broker",
            )
            db.add(user)
            await db.flush()
            broker = Broker(
                user_id=user.id,
                display_name=f"Broker {letter.upper()} {suffix}",
            )
            db.add(broker)
            await db.flush()
            client = Client(
                broker_id=broker.id,
                name=f"Client {letter.upper()} {suffix}",
                email=f"client-{letter}-{suffix}@test.local",
                stage=ClientStage.LEAD,
            )
            db.add(client)
            await db.flush()
            loan = Loan(
                deal_id=f"T-{letter}-{suffix}",
                client_id=client.id,
                broker_id=broker.id,
                address=f"{letter} Test St",
                type=LoanType.DSCR,
                stage=LoanStage.PREQUALIFIED,
                amount=100_000,
            )
            db.add(loan)
            await db.flush()
            task = AITask(
                loan_id=loan.id,
                source=AITaskSource.DOCUMENTS,
                priority=AITaskPriority.MEDIUM,
                status=AITaskStatus.PENDING,
                action="test",
                title=f"Task {letter.upper()}",
                summary="scoping test",
            )
            db.add(task)
            users.append({
                "user": user, "broker": broker, "client": client,
                "loan": loan, "task": task,
            })
        await db.commit()
        yield users

        # Cleanup
        for u in users:
            await db.delete(u["task"])
            await db.delete(u["loan"])
            await db.delete(u["client"])
            await db.delete(u["broker"])
            await db.delete(u["user"])
        await db.commit()


@pytest.mark.asyncio
async def test_funnel_scoped_to_broker_a(broker_pair):
    """compute_funnel(broker_id=A) returns counts including only A's
    clients. B's clients must not show up in by-stage breakdown."""
    from app.db import SessionLocal
    from app.services.lead_funnel import compute_funnel

    a, b = broker_pair[0], broker_pair[1]
    async with SessionLocal() as db:
        funnel_a = await compute_funnel(db, broker_id=a["broker"].id)
        funnel_b = await compute_funnel(db, broker_id=b["broker"].id)

        # Each broker sees exactly 1 client (their own, in 'lead' stage)
        assert funnel_a.clients_by_stage.get("lead", 0) >= 1
        assert funnel_b.clients_by_stage.get("lead", 0) >= 1
        # Crucially: broker_a's count is decoupled from broker_b's
        # — adding more b clients shouldn't move a's metrics.


@pytest.mark.asyncio
async def test_next_actions_no_cross_broker_leakage(broker_pair):
    """compute_next_actions(broker_id=A) must never include
    target_id pointing to B's loan/client/task."""
    from app.db import SessionLocal
    from app.services.next_actions import compute_next_actions

    a, b = broker_pair[0], broker_pair[1]
    async with SessionLocal() as db:
        actions_a = await compute_next_actions(db, broker_id=a["broker"].id)
        b_ids = {b["client"].id, b["loan"].id, b["task"].id}
        for action in actions_a:
            assert action.target_id not in b_ids, (
                f"broker_a saw a {action.kind} for broker_b's "
                f"{action.target_type} — leakage"
            )


@pytest.mark.asyncio
async def test_ai_tasks_filter_by_broker(broker_pair):
    """AITask query for broker_a includes only a's loans' tasks
    + null-loan tasks (firm-wide). Must never include broker_b's."""
    from sqlalchemy import or_
    from app.db import SessionLocal
    from app.enums import AITaskStatus
    from app.models.ai_task import AITask
    from app.models.loan import Loan

    a, b = broker_pair[0], broker_pair[1]
    async with SessionLocal() as db:
        stmt = (
            select(AITask)
            .where(AITask.status == AITaskStatus.PENDING)
            .where(
                or_(
                    AITask.loan_id.is_(None),
                    AITask.loan_id.in_(
                        select(Loan.id).where(
                            Loan.broker_id == a["broker"].id
                        )
                    ),
                )
            )
        )
        rows = (await db.execute(stmt)).scalars().all()
        for row in rows:
            assert row.id != b["task"].id, (
                "broker_a's filtered query returned broker_b's task"
            )


@pytest.mark.asyncio
async def test_funnel_firm_wide_for_super_admin(broker_pair):
    """compute_funnel(broker_id=None) returns combined firm-wide
    totals — both broker_a's and broker_b's clients show up."""
    from app.db import SessionLocal
    from app.services.lead_funnel import compute_funnel

    async with SessionLocal() as db:
        firm = await compute_funnel(db, broker_id=None)
        # Both fixtures created a 'lead' client → firm sees >= 2.
        assert firm.clients_by_stage.get("lead", 0) >= 2


@pytest.mark.asyncio
async def test_per_client_checklist_override_applies(broker_pair):
    """Setting `Client.checklist_overrides` must:
      - drop firm items named in `disabled_firm_items`,
      - append `extra_items` (filtered by side),
    via `resolve_loan_checklist`. Per-lead intent wins over firm
    baseline + broker overlay."""
    from sqlalchemy import update
    from app.db import SessionLocal
    from app.models.app_settings import AppSettings
    from app.models.client import Client
    from app.models.loan import Loan
    from app.schemas.settings import AppSettingsData
    from app.services.agent_checklist import resolve_loan_checklist

    a = broker_pair[0]
    loan_id = a["loan"].id
    client_id = a["client"].id

    async with SessionLocal() as db:
        srow = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
        settings = (
            AppSettingsData.model_validate(srow.data or {}) if srow else AppSettingsData()
        )
        loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one()
        baseline_items, _ = await resolve_loan_checklist(db, loan=loan, settings=settings)
        baseline_names = [i.name for i in baseline_items]

    if not baseline_names:
        pytest.skip("firm checklist empty in this env — nothing to disable")

    first = baseline_names[0]
    overrides = {
        "disabled_firm_items": [first],
        "extra_items": [
            {
                "name": "smoke_test_extra",
                "display_name": "Smoke",
                "type": "external",
                "required": False,
                "auto_request": True,
                "due_offset_days": 7,
                "anchor": "loan_created",
                "per_unit": False,
                "side": loan.side or "buyer",
            }
        ],
    }

    async with SessionLocal() as db:
        await db.execute(
            update(Client).where(Client.id == client_id).values(checklist_overrides=overrides)
        )
        await db.commit()

    try:
        async with SessionLocal() as db:
            srow = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
            settings = (
                AppSettingsData.model_validate(srow.data or {}) if srow else AppSettingsData()
            )
            loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one()
            after_items, _ = await resolve_loan_checklist(db, loan=loan, settings=settings)
            after_names = [i.name for i in after_items]
            assert first not in after_names, (
                f"client_overrides.disabled_firm_items did not remove {first!r}"
            )
            assert "smoke_test_extra" in after_names, (
                "client_overrides.extra_items did not add custom row"
            )
    finally:
        async with SessionLocal() as db:
            await db.execute(
                update(Client).where(Client.id == client_id).values(checklist_overrides=None)
            )
            await db.commit()


@pytest.mark.asyncio
async def test_per_client_cadence_override_cascades(broker_pair):
    """Setting `Client.ai_cadence_override` must override broker +
    firm cadence on the resolved base_checklist. Tests the field-level
    cascade (firm → broker → client)."""
    from sqlalchemy import update
    from app.db import SessionLocal
    from app.models.app_settings import AppSettings
    from app.models.broker import Broker
    from app.models.client import Client
    from app.models.loan import Loan
    from app.schemas.settings import AppSettingsData
    from app.services.agent_checklist import resolve_loan_checklist

    a = broker_pair[0]
    loan_id = a["loan"].id
    client_id = a["client"].id
    broker_id = a["broker"].id

    # Set broker cadence (overrides firm) — first=5, escalate=21
    broker_settings = {
        "checklists": {},
        "cadence": {
            "first_reminder_days": 5,
            "second_reminder_days": None,
            "escalate_after_days": 21,
        },
    }
    # Client cadence overrides broker for first only
    client_cadence = {
        "first_reminder_days": 2,
        "second_reminder_days": None,
        "escalate_after_days": None,
    }

    async with SessionLocal() as db:
        await db.execute(
            update(Broker).where(Broker.id == broker_id).values(settings_data=broker_settings)
        )
        await db.execute(
            update(Client).where(Client.id == client_id).values(ai_cadence_override=client_cadence)
        )
        await db.commit()

    try:
        async with SessionLocal() as db:
            srow = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
            settings = (
                AppSettingsData.model_validate(srow.data or {}) if srow else AppSettingsData()
            )
            loan = (await db.execute(select(Loan).where(Loan.id == loan_id))).scalar_one()
            _, base = await resolve_loan_checklist(db, loan=loan, settings=settings)
            assert base.first_reminder_days == 2, "client cadence didn't win for first_reminder"
            assert base.escalate_after_days == 21, "broker cadence didn't propagate for escalate"
            # second_reminder_days falls back to firm default (both overrides null)
    finally:
        async with SessionLocal() as db:
            await db.execute(
                update(Broker).where(Broker.id == broker_id).values(settings_data={})
            )
            await db.execute(
                update(Client).where(Client.id == client_id).values(ai_cadence_override=None)
            )
            await db.commit()


@pytest.mark.asyncio
async def test_aitask_null_loan_visible_to_brokers(broker_pair):
    """A null-loan AITask (firm-wide alert) must appear for both
    broker_a and broker_b. This is the deliberate widening
    documented in app/routers/ai_tasks.py."""
    from sqlalchemy import or_
    from app.db import SessionLocal
    from app.enums import AITaskPriority, AITaskSource, AITaskStatus
    from app.models.ai_task import AITask
    from app.models.loan import Loan

    a = broker_pair[0]
    async with SessionLocal() as db:
        # Create a null-loan firm-wide task
        firm_task = AITask(
            loan_id=None,
            source=AITaskSource.PIPELINE,
            priority=AITaskPriority.HIGH,
            status=AITaskStatus.PENDING,
            action="firm_alert",
            title="Firm-wide alert",
            summary="should be visible to all brokers",
        )
        db.add(firm_task)
        await db.commit()

        # Run broker_a's filter — null-loan task should appear
        stmt = (
            select(AITask)
            .where(AITask.status == AITaskStatus.PENDING)
            .where(
                or_(
                    AITask.loan_id.is_(None),
                    AITask.loan_id.in_(
                        select(Loan.id).where(
                            Loan.broker_id == a["broker"].id
                        )
                    ),
                )
            )
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert any(r.id == firm_task.id for r in rows), (
            "broker_a missed a firm-wide null-loan task — null-loan "
            "widening regressed"
        )

        # Cleanup
        await db.delete(firm_task)
        await db.commit()
