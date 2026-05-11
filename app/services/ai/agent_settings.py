"""Read-side helpers for the agent's AI settings (working hours,
sending control, voice, knowledge FAQ).

Single source of truth: the agent's "cadence" AIPlaybookTemplate row,
keyed by (owner_type='agent', owner_id=user_id, playbook_type='cadence').
The `rules` JSONB holds all of:

  rules.sending_control       "draft_only" | "ask_before_sending" | "auto_send_portal"
  rules.working_hours         see working_hours.normalize()
  rules.attempt_limit         {"max_attempts": int, "create_task_when_reached": bool,
                               "mark_stalled": bool}
  rules.voice                 {"tone": ..., "follow_up_style": ..., "signature": ...}
  rules.knowledge.faq_text    free-text the AI uses as context

Anything not yet configured by the agent falls back to the FIRM-level
default (the funding-owned `communication` playbook patched by
super-admins via /lending-admin/communication-rules). Library defaults
in services/ai/working_hours.py are the final floor — callers don't
need to special-case any of these tiers themselves."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_playbook import AIPlaybookTemplate
from app.models.broker import Broker
from app.models.client import Client
from app.models.loan import Loan
from app.services.ai.working_hours import default_working_hours

log = logging.getLogger(__name__)


async def load_agent_cadence_rules(
    db: AsyncSession,
    agent_user_id: UUID | None,
) -> dict[str, Any]:
    """Return the agent's full `cadence` playbook rules dict.

    `agent_user_id` is the USERS.id of the broker — NOT brokers.id.
    AIPlaybookTemplate.owner_id is the user id for agent overlays
    (see `routers/me.py:269` upsert)."""
    if agent_user_id is None:
        return {}
    pb = (
        await db.execute(
            select(AIPlaybookTemplate).where(
                AIPlaybookTemplate.owner_type == "agent",
                AIPlaybookTemplate.owner_id == agent_user_id,
                AIPlaybookTemplate.playbook_type == "cadence",
            )
        )
    ).scalar_one_or_none()
    if pb is None or not pb.rules:
        return {}
    return dict(pb.rules)


async def load_agent_user_id_for_loan(
    db: AsyncSession,
    loan: Loan,
) -> UUID | None:
    """Resolve the responsible agent (broker) USER id for a loan.

    Path: loan.broker_id → brokers.user_id. Returns None for loans
    that aren't broker-owned (self-serve, super-admin-created).
    Cheap — single PK lookup."""
    if loan.broker_id is None:
        return None
    broker = await db.get(Broker, loan.broker_id)
    return broker.user_id if broker else None


async def load_agent_user_id_for_client(
    db: AsyncSession,
    client_id: UUID,
) -> UUID | None:
    """Same idea but starts from a Client row — used by the dispatcher
    which always has a CRS → Client handle."""
    client = await db.get(Client, client_id)
    if client is None or client.broker_id is None:
        return None
    broker = await db.get(Broker, client.broker_id)
    return broker.user_id if broker else None


async def load_firm_communication_rules(
    db: AsyncSession,
) -> dict[str, Any]:
    """Firm-wide overrides — the funding-owned cadence playbook keyed
    by product_key='communication'. Same row the super-admin
    /lending-admin/communication-rules endpoint writes to. Returns the
    union dict across any rows that exist (multiple funding admins
    could each have a row; first non-empty value per key wins).

    NEVER raises — falls back to {} on DB error so a missing/broken
    firm row can't take down the AI."""
    try:
        rows = (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.owner_type == "funding",
                    AIPlaybookTemplate.playbook_type == "cadence",
                    AIPlaybookTemplate.product_key == "communication",
                    AIPlaybookTemplate.is_active.is_(True),
                )
            )
        ).scalars().all()
    except Exception:
        log.exception("agent_settings.load_firm_communication_rules failed")
        return {}
    merged: dict[str, Any] = {}
    for r in rows:
        if not isinstance(r.rules, dict):
            continue
        for k, v in r.rules.items():
            if k not in merged and v not in (None, "", [], {}):
                merged[k] = v
    return merged


def _resolve_working_hours(
    agent_rules: dict[str, Any],
    firm_rules: dict[str, Any],
) -> dict[str, Any]:
    """Three-tier precedence: agent > firm > library default. We wrap
    whichever tier first defines `working_hours`. The library default
    is supplied by `_wrap()` when neither tier has a config."""
    if isinstance(agent_rules, dict) and isinstance(agent_rules.get("working_hours"), dict):
        return _wrap(agent_rules)
    if isinstance(firm_rules, dict) and isinstance(firm_rules.get("working_hours"), dict):
        return _wrap(firm_rules)
    return _wrap({})


async def working_hours_for_loan(
    db: AsyncSession,
    loan: Loan,
) -> dict[str, Any]:
    """Convenience: the full working_hours config for the agent on a
    loan, ready to hand to `working_hours.evaluate()`. Agent
    config takes precedence; firm-level config is the fallback when
    the agent hasn't set one; library defaults are the final floor."""
    user_id = await load_agent_user_id_for_loan(db, loan)
    agent_rules = await load_agent_cadence_rules(db, user_id)
    firm_rules = await load_firm_communication_rules(db)
    return _resolve_working_hours(agent_rules, firm_rules)


async def working_hours_for_client(
    db: AsyncSession,
    client_id: UUID,
) -> dict[str, Any]:
    user_id = await load_agent_user_id_for_client(db, client_id)
    agent_rules = await load_agent_cadence_rules(db, user_id)
    firm_rules = await load_firm_communication_rules(db)
    return _resolve_working_hours(agent_rules, firm_rules)


def sending_control_to_outreach_mode(value: str | None) -> str:
    """Map the agent-facing radio choice to the file-level outreach_mode
    enum stored on ClientAIPlan.ai_secretary_settings. The frontend
    deals only in the three friendly values; the runtime keeps using
    the legacy five-value mode."""
    if value == "auto_send_portal":
        return "portal_auto"
    if value == "ask_before_sending":
        # Still an inbox draft, but flagged for per-message approval.
        # The cadence engine treats this identically to draft_first
        # (always inbox-first); the per-message UX in AI Inbox is
        # what surfaces the approval prompt.
        return "draft_first"
    # "draft_only" (default) and anything unrecognised → safest mode.
    return "draft_first"


def _wrap(rules: dict[str, Any]) -> dict[str, Any]:
    """Internal — package a bare rules dict for working_hours.evaluate().
    Returns a dict shaped `{"working_hours": {...}}` so the working_hours
    module's `normalize()` finds the inner block in either case."""
    if not rules:
        return {"working_hours": default_working_hours()}
    if "working_hours" in rules:
        return rules
    return {**rules, "working_hours": default_working_hours()}
