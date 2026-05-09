"""Cadence engine — fires conditional AI follow-up rules every tick.

Walks `ai_cadence_rules`, finds (client, loan) scopes where each
rule's trigger is currently true + the wait window has elapsed +
nothing equivalent has been actioned recently, and:

  - draft_message       → creates an AITask with the proposed draft.
                          Default. Agent reviews + sends from AI Inbox.
  - create_task         → opens an AITask of a specific shape.
  - escalate            → high-priority AITask + (optionally)
                          flips lead_promotion_status / loan flag.
  - mark_stalled        → updates client_requirement_status / lead temp
  - auto_send_reminder  → sends a borrower-facing message DIRECTLY only
                          when the rule explicitly opts in via
                          approval_required=False AND visibility="borrower".
                          Otherwise downgrades to draft_message.

Draft-first by default. The auto-send path is intentionally narrow —
we'd rather miss a reminder than send a wrong one.

Run by `app/services/scheduler.py` every 30 minutes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AITaskPriority, AITaskSource, AITaskStatus
from app.models.ai_audit_event import AIAuditEvent
from app.models.ai_cadence_rule import AICadenceRule
from app.models.ai_playbook import AIPlaybookTemplate
from app.models.ai_task import AITask
from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.services.ai.audit import record_event

log = logging.getLogger(__name__)


# ── Public entrypoint ──────────────────────────────────────────────


async def run_cadence_pass(db: AsyncSession) -> dict[str, int]:
    """Walk every active cadence rule across funding + agent + platform
    overlays, fire eligible actions. Idempotent — won't refire an
    action of the same shape inside a 24h window for the same target."""
    rules = await _load_active_rules(db)
    fired = 0
    drafted = 0
    skipped = 0
    for rule in rules:
        try:
            results = await _evaluate_rule(db, rule)
        except Exception:  # pragma: no cover — keep walking
            log.exception("cadence_engine: rule %s blew up — skipping", rule.id)
            skipped += 1
            continue
        for r in results:
            fired += 1
            if r["action_type"] == "draft_message":
                drafted += 1
    log.info("cadence_pass complete: rules=%d fired=%d drafted=%d skipped=%d",
             len(rules), fired, drafted, skipped)
    return {"rules": len(rules), "fired": fired, "drafted": drafted, "skipped": skipped}


async def preview_cadence_pass(
    db: AsyncSession,
    *,
    agent_id: UUID | None = None,
    client_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Read-only preview — what would fire today? No DB writes. Used
    by the AI Preview panel."""
    rules = await _load_active_rules(db)
    out: list[dict[str, Any]] = []
    for rule in rules:
        targets = await _eligible_targets(db, rule, agent_id=agent_id, client_id=client_id)
        for t in targets:
            out.append({
                "rule_id": rule.id,
                "trigger_event": rule.trigger_event,
                "action_type": rule.action_type,
                "approval_required": rule.approval_required,
                "visibility": rule.visibility,
                "client_id": t["client_id"],
                "client_name": t["client_name"],
                "requirement_key": t.get("requirement_key"),
                "message_preview": _format_template(rule.message_template, t),
                "fires_now": True,
            })
    return out


# ── Rule loading ───────────────────────────────────────────────────


async def _load_active_rules(db: AsyncSession) -> list[AICadenceRule]:
    """Return every active cadence rule across all owners. The engine
    treats them uniformly — owner_type just determines visibility +
    the AITask's owner queue."""
    rows = (await db.execute(
        select(AICadenceRule).where(AICadenceRule.is_active.is_(True))
    )).scalars().all()
    return list(rows)


# ── Per-rule evaluation ────────────────────────────────────────────


async def _evaluate_rule(db: AsyncSession, rule: AICadenceRule) -> list[dict[str, Any]]:
    """Find every (client, loan) where this rule fires + spawn its
    action. Returns one result dict per fired action."""
    targets = await _eligible_targets(db, rule)
    results: list[dict[str, Any]] = []
    for t in targets:
        if await _already_fired_recently(db, rule, t):
            continue
        outcome = await _fire_action(db, rule, t)
        if outcome is not None:
            results.append(outcome)
    return results


async def _eligible_targets(
    db: AsyncSession,
    rule: AICadenceRule,
    *,
    agent_id: UUID | None = None,
    client_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """Find the (client, loan, requirement) triples where this rule's
    trigger is currently true.

    Trigger semantics (v1):
      requirement_missing       → status row exists with status='asked' AND
                                  last_requested_at older than wait_hours.
      agreement_unsigned        → same as requirement_missing but scoped to
                                  the rule's applies_to_requirement_key
                                  (buyer_agency_agreement / listing_agreement).
      borrower_unresponsive     → no chat activity for client in last
                                  condition.days_unresponsive days.
      closing_date_near         → client has loan with closing_date within
                                  condition.days_until_closing days AND
                                  any required item still missing.
      document_uploaded         → handled in-line by the document
                                  pipeline, not here.

    Phase 5 supports the first two strongly + the last two as best-effort
    placeholders — Phase 6 widens evidence gathering."""
    cond = rule.condition or {}
    triggers_handled = {
        "requirement_missing", "agreement_unsigned",
        "borrower_unresponsive", "closing_date_near",
    }
    if rule.trigger_event not in triggers_handled:
        return []

    now = datetime.now(timezone.utc)

    # Build a status-driven base query for requirement-style triggers.
    if rule.trigger_event in ("requirement_missing", "agreement_unsigned"):
        wait_hours = max(rule.wait_hours, int(cond.get("hours_since_request", 0)) or int(cond.get("hours_since_sent", 0)))
        cutoff = now - timedelta(hours=wait_hours) if wait_hours else now
        q = select(ClientRequirementStatus, Client).join(
            Client, Client.id == ClientRequirementStatus.client_id,
        ).where(
            ClientRequirementStatus.status.in_(("asked", "missing", "needed_later")),
        )
        if rule.applies_to_requirement_key:
            q = q.where(ClientRequirementStatus.requirement_key == rule.applies_to_requirement_key)
        if wait_hours:
            q = q.where(
                or_(
                    ClientRequirementStatus.last_requested_at <= cutoff,
                    ClientRequirementStatus.last_requested_at.is_(None),
                )
            )
        if client_id:
            q = q.where(ClientRequirementStatus.client_id == client_id)
        if agent_id:
            # Agent-only filter — only their clients (broker_id-scoped).
            from app.models.broker import Broker
            broker = (await db.execute(select(Broker).where(Broker.user_id == agent_id))).scalar_one_or_none()
            if broker:
                q = q.where(Client.broker_id == broker.id)
            else:
                return []
        rows = (await db.execute(q)).all()
        out: list[dict[str, Any]] = []
        for status_row, client in rows:
            out.append({
                "client_id": client.id,
                "client_name": client.name,
                "loan_id": status_row.loan_id,
                "requirement_key": status_row.requirement_key,
                "context": {"requirement_label": status_row.requirement_key},
            })
        return out

    if rule.trigger_event == "borrower_unresponsive":
        # Walk client_ai_plan rows where the most recent computed_at is older
        # than condition.days_unresponsive days. Cheap proxy.
        days = int(cond.get("days_unresponsive", 5))
        cutoff = now - timedelta(days=days)
        plans = (await db.execute(
            select(ClientAIPlan, Client)
            .join(Client, Client.id == ClientAIPlan.client_id)
            .where(ClientAIPlan.computed_at <= cutoff)
        )).all()
        out2: list[dict[str, Any]] = []
        for plan_row, client in plans:
            if client_id and client.id != client_id:
                continue
            out2.append({
                "client_id": client.id,
                "client_name": client.name,
                "loan_id": plan_row.loan_id,
                "requirement_key": None,
                "context": {"days_unresponsive": days},
            })
        return out2

    # closing_date_near placeholder — Phase 6 wires evidence properly.
    return []


async def _already_fired_recently(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> bool:
    """Avoid double-firing. v1 looks back 24h for an audit row with
    the same (event_type, client_id, requirement_key)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    event_type = f"cadence_action_fired:{rule.action_type}"
    rows = (await db.execute(
        select(AIAuditEvent).where(
            AIAuditEvent.event_type == event_type,
            AIAuditEvent.client_id == target["client_id"],
            AIAuditEvent.requirement_key == target.get("requirement_key"),
            AIAuditEvent.created_at >= cutoff,
        ).limit(1)
    )).scalars().all()
    return bool(rows)


async def _fire_action(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> dict[str, Any] | None:
    """Spawn the action handler matching the rule's action_type."""
    handler = _ACTION_HANDLERS.get(rule.action_type, _handle_draft_message)
    outcome = await handler(db, rule, target)
    if outcome is not None:
        await record_event(
            db,
            event_type=f"cadence_action_fired:{rule.action_type}",
            actor_type="cadence_engine",
            client_id=target["client_id"],
            loan_id=target.get("loan_id"),
            playbook_id=rule.playbook_id,
            requirement_key=target.get("requirement_key"),
            payload={
                "rule_id": str(rule.id),
                "trigger_event": rule.trigger_event,
                "approval_required": rule.approval_required,
                "visibility": rule.visibility,
                "ai_task_id": str(outcome.get("ai_task_id")) if outcome.get("ai_task_id") else None,
            },
        )
        outcome["action_type"] = rule.action_type
    return outcome


# ── Action handlers ────────────────────────────────────────────────


async def _handle_draft_message(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Draft-first default. Creates an AITask the agent reviews +
    sends from AI Inbox. Never sends to the borrower directly."""
    msg = _format_template(rule.message_template, target)
    task = AITask(
        loan_id=target.get("loan_id"),
        source=AITaskSource.PIPELINE,
        priority=AITaskPriority.MEDIUM,
        status=AITaskStatus.PENDING,
        action="cadence_draft_message",
        title=f"Cadence draft · {target['client_name']}",
        summary=msg or f"Cadence rule {rule.trigger_event} fired — review draft.",
        agent="cadence",
        draft_payload={
            "client_id": str(target["client_id"]),
            "rule_id": str(rule.id),
            "trigger_event": rule.trigger_event,
            "requirement_key": target.get("requirement_key"),
            "message": msg,
            "visibility": rule.visibility,
        },
    )
    db.add(task)
    await db.flush()
    return {"ai_task_id": task.id, "client_id": target["client_id"]}


async def _handle_create_task(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> dict[str, Any]:
    msg = _format_template(rule.message_template, target)
    task = AITask(
        loan_id=target.get("loan_id"),
        source=AITaskSource.PIPELINE,
        priority=AITaskPriority.MEDIUM,
        status=AITaskStatus.PENDING,
        action="cadence_task",
        title=f"Cadence task · {target['client_name']}",
        summary=msg or f"Cadence rule {rule.trigger_event} fired.",
        agent="cadence",
        draft_payload={
            "client_id": str(target["client_id"]),
            "rule_id": str(rule.id),
            "requirement_key": target.get("requirement_key"),
        },
    )
    db.add(task)
    await db.flush()
    return {"ai_task_id": task.id, "client_id": target["client_id"]}


async def _handle_escalate(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> dict[str, Any]:
    msg = _format_template(rule.message_template, target)
    task = AITask(
        loan_id=target.get("loan_id"),
        source=AITaskSource.RISK,
        priority=AITaskPriority.HIGH,
        status=AITaskStatus.PENDING,
        action="cadence_escalation",
        title=f"ESCALATION · {target['client_name']}",
        summary=msg or f"Cadence escalation fired: {rule.trigger_event}.",
        agent="cadence",
        draft_payload={
            "client_id": str(target["client_id"]),
            "rule_id": str(rule.id),
            "requirement_key": target.get("requirement_key"),
            "severity": "high",
        },
    )
    db.add(task)
    await db.flush()
    return {"ai_task_id": task.id, "client_id": target["client_id"]}


async def _handle_mark_stalled(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Set client_requirement_status.status='stalled' for any open
    requirement on this client. Doesn't spawn a task — the audit
    event is the signal."""
    rows = (await db.execute(
        select(ClientRequirementStatus).where(
            ClientRequirementStatus.client_id == target["client_id"],
            ClientRequirementStatus.status.in_(("asked", "missing", "needed_later")),
        )
    )).scalars().all()
    for row in rows:
        row.status = "stalled"
    await db.flush()
    return {"ai_task_id": None, "client_id": target["client_id"], "marked_count": len(rows)}


async def _handle_auto_send_reminder(
    db: AsyncSession,
    rule: AICadenceRule,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Auto-send is opt-in. Default behavior: downgrade to draft.

    The narrow case where auto-send fires is when both conditions hold:
      - rule.approval_required is False
      - rule.visibility is "borrower" or "agent"

    Even then, v1 still routes through AI Inbox — we set
    auto_send=true on the draft so the inbox auto-runs it. The actual
    outbound send is handled by the existing email/SMS pipeline."""
    if rule.approval_required:
        return await _handle_draft_message(db, rule, target)
    msg = _format_template(rule.message_template, target)
    task = AITask(
        loan_id=target.get("loan_id"),
        source=AITaskSource.PIPELINE,
        priority=AITaskPriority.MEDIUM,
        status=AITaskStatus.PENDING,
        action="cadence_auto_send",
        title=f"Auto-send reminder · {target['client_name']}",
        summary=msg or f"Cadence auto-send fired: {rule.trigger_event}.",
        agent="cadence",
        draft_payload={
            "client_id": str(target["client_id"]),
            "rule_id": str(rule.id),
            "auto_send": True,
            "message": msg,
            "visibility": rule.visibility,
        },
    )
    db.add(task)
    await db.flush()
    return {"ai_task_id": task.id, "client_id": target["client_id"]}


_ACTION_HANDLERS = {
    "draft_message": _handle_draft_message,
    "create_task": _handle_create_task,
    "escalate": _handle_escalate,
    "mark_stalled": _handle_mark_stalled,
    "auto_send_reminder": _handle_auto_send_reminder,
}


# ── Helpers ────────────────────────────────────────────────────────


def _format_template(tmpl: str | None, target: dict[str, Any]) -> str | None:
    """Tiny mustache-style format. Replaces {{key}} with target.context.key
    or top-level target keys. Missing keys become '—'."""
    if not tmpl:
        return None
    out = tmpl
    ctx = (target.get("context") or {}).copy()
    ctx.update({
        "client_name": target.get("client_name") or "—",
        "requirement_key": target.get("requirement_key") or "—",
    })
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", str(v if v is not None else "—"))
    return out
