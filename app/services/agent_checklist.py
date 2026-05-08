"""Per-loan checklist resolver.

The single function `resolve_loan_checklist` is the source of truth
for "what items does the AI collect for THIS loan?" — used by:
  - kickoff (`materialize_kickoff_items`)
  - anchor scheduler (`fire_anchor_dependents`)
  - reminder cron (`evaluate_doc_reminders`)
  - the GET /loans/{id}/workflow endpoint that powers the desktop
    Workflow tab

Resolution order:
  1. Start with the firm `LoanTypeChecklist.docs` for `loan.type`.
  2. Filter by `item.side ∈ (loan.side, "both")`.
  3. If the loan has a `broker_id`, load `broker.settings_data`:
     - Drop firm items whose name is in
       `overlay[loan_type:side].disabled_firm_items`.
     - Append `overlay[loan_type:side].extra_items` (also filtered
       by side — agent's custom rows are scoped to a specific side).
  4. Return the resulting list.

Returns the resolved `DocChecklistItem` list as plain Pydantic
models, ready for downstream consumers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import Broker
from app.schemas.broker_settings import AgentSettingsData
from app.schemas.settings import (
    AppSettingsData,
    DocChecklistItem,
    LoanTypeChecklist,
)

if TYPE_CHECKING:
    from app.models.loan import Loan

log = logging.getLogger(__name__)


def _coerce_overlay(raw: dict | None) -> AgentSettingsData:
    """Defensive parse — `broker.settings_data` is JSONB and could
    be NULL or missing fields after a schema add. Always return a
    valid `AgentSettingsData` (empty = no-op overlay)."""
    if not raw:
        return AgentSettingsData()
    try:
        return AgentSettingsData.model_validate(raw)
    except Exception:  # noqa: BLE001
        log.warning("agent settings_data malformed, falling back to empty", exc_info=True)
        return AgentSettingsData()


async def _load_broker_overlay(
    db: AsyncSession, broker_id
) -> AgentSettingsData:
    """One round-trip to fetch the broker's overlay. Returns an
    empty overlay if the broker has no `settings_data` yet."""
    row = (
        await db.execute(
            select(Broker.settings_data).where(Broker.id == broker_id)
        )
    ).scalar_one_or_none()
    return _coerce_overlay(row)


def _filter_by_side(
    items: list[DocChecklistItem], loan_side: str
) -> list[DocChecklistItem]:
    """Drop items tagged for the OTHER side. `both` always passes."""
    return [
        item for item in items
        if item.side == "both" or item.side == loan_side
    ]


async def resolve_loan_checklist(
    db: AsyncSession,
    *,
    loan: "Loan",
    settings: AppSettingsData,
) -> tuple[list[DocChecklistItem], LoanTypeChecklist]:
    """Compute the effective doc checklist for one loan.

    Returns `(items, base_checklist)`:
      - `items` is the resolved + side-filtered + agent-overlaid list
      - `base_checklist` is the firm's `LoanTypeChecklist` for
        `loan.type` (used by the cron to read the per-loan-type
        cadence fallbacks like `first_reminder_days`)
    """
    loan_type_str = str(loan.type)
    loan_side_str = str(loan.side) if hasattr(loan, "side") else "buyer"

    base = settings.checklists.get(loan_type_str)
    if base is None:
        # No firm checklist configured — empty list + a defaults
        # carrier so callers can still read the cadence fallbacks.
        base = LoanTypeChecklist()

    # Step 1+2: firm baseline filtered by side
    firm_items = _filter_by_side(list(base.docs), loan_side_str)

    # Step 3: apply agent overlay if loan has a broker.
    # Post-codex-PR shape: checklists keyed by `side` only (no
    # loan-type axis). Existing v1 rows with `loan_type:side` keys
    # are silently stripped by AgentSettingsData's migration validator.
    overlay_items: list[DocChecklistItem] = firm_items
    broker_cadence = None
    if getattr(loan, "broker_id", None):
        agent_settings = await _load_broker_overlay(db, loan.broker_id)
        broker_cadence = agent_settings.cadence
        overlay = agent_settings.checklists.get(loan_side_str)
        if overlay is not None:
            disabled = {n.strip() for n in overlay.disabled_firm_items if n}
            overlay_items = [
                item for item in firm_items if item.name not in disabled
            ]
            extras = _filter_by_side(list(overlay.extra_items), loan_side_str)
            # Agent-custom items are always external — defensive guard.
            for it in extras:
                if it.type == "internal":
                    log.warning(
                        "agent overlay tried to add internal item %r — ignored",
                        it.name,
                    )
                    continue
                overlay_items.append(it)

    # Step 4: per-Client overrides (alembic 0025). When the loan was
    # promoted from a lead, the agent may have customized the
    # checklist on THAT specific lead — disable items they don't
    # want to chase, add lead-specific extras. Layered last so
    # per-lead intent always wins.
    client_overrides = await _load_client_overrides(db, loan.client_id)
    if client_overrides is not None:
        co_disabled = {
            n.strip() for n in client_overrides.disabled_firm_items if n
        }
        if co_disabled:
            overlay_items = [
                item for item in overlay_items if item.name not in co_disabled
            ]
        co_extras = _filter_by_side(list(client_overrides.extra_items), loan_side_str)
        for it in co_extras:
            if it.type == "internal":
                continue
            overlay_items.append(it)

    # Step 5: cadence merge (firm → broker → client). Each field
    # cascades — a non-null broker value overrides firm; a non-null
    # client value overrides both. Returned via the synthesized
    # `base_checklist` so kickoff + reminder cron pick up the merged
    # cadence without each callsite reaching for the raw overrides.
    client_cadence = await _load_client_cadence(db, loan.client_id)
    merged_base = _merge_cadence(base, broker_cadence, client_cadence)
    return overlay_items, merged_base


async def _load_client_overrides(
    db: AsyncSession, client_id
) -> "AgentChecklistOverlay | None":
    """Load `Client.checklist_overrides` JSONB and parse into an
    `AgentChecklistOverlay`. Returns None when the client has no
    per-lead overrides configured (the common case)."""
    from app.models.client import Client as _Client
    from app.schemas.broker_settings import AgentChecklistOverlay

    if client_id is None:
        return None
    row = (
        await db.execute(
            select(_Client.checklist_overrides).where(_Client.id == client_id)
        )
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        return AgentChecklistOverlay.model_validate(row)
    except Exception:  # noqa: BLE001
        log.warning(
            "client %s checklist_overrides malformed, ignoring",
            client_id, exc_info=True,
        )
        return None


async def _load_client_cadence(
    db: AsyncSession, client_id
) -> "AgentCadenceOverride | None":
    """Load `Client.ai_cadence_override` JSONB and parse into an
    `AgentCadenceOverride`. None = no per-lead cadence configured."""
    from app.models.client import Client as _Client
    from app.schemas.broker_settings import AgentCadenceOverride

    if client_id is None:
        return None
    row = (
        await db.execute(
            select(_Client.ai_cadence_override).where(_Client.id == client_id)
        )
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        return AgentCadenceOverride.model_validate(row)
    except Exception:  # noqa: BLE001
        log.warning(
            "client %s ai_cadence_override malformed, ignoring",
            client_id, exc_info=True,
        )
        return None


def _merge_cadence(
    base: LoanTypeChecklist,
    broker_cadence: "AgentCadenceOverride | None",
    client_cadence: "AgentCadenceOverride | None",
) -> LoanTypeChecklist:
    """Cascade cadence values: firm `base` → broker → client.

    Per field, the lowest-priority non-null value wins:
      client.X        if not None,
      else broker.X   if not None,
      else base.X.

    Returns a NEW LoanTypeChecklist (mutating `base` would leak the
    merge into the firm settings). The `docs` list and other fields
    pass through untouched — only cadence is overlaid."""
    if broker_cadence is None and client_cadence is None:
        return base

    def _pick(field: str, default: int) -> int:
        if client_cadence is not None:
            v = getattr(client_cadence, field, None)
            if v is not None:
                return int(v)
        if broker_cadence is not None:
            v = getattr(broker_cadence, field, None)
            if v is not None:
                return int(v)
        return default

    return LoanTypeChecklist(
        docs=base.docs,
        first_reminder_days=_pick("first_reminder_days", base.first_reminder_days),
        second_reminder_days=_pick("second_reminder_days", base.second_reminder_days),
        escalate_after_days=_pick("escalate_after_days", base.escalate_after_days),
        auto_approve_risk_score=base.auto_approve_risk_score,
    )
