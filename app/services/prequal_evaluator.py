"""Deferred auto-approval evaluator for pending prequal requests.

Runs from the scheduler (`job_evaluate_pending_prequals`, every 2
minutes) instead of inline during submit. The borrower's submit
returns immediately with `status=pending`; this job picks up the
request a moment later, runs the deterministic gate + (future) LLM
sanity layer, and either approves it or stamps the blockers on
`admin_notes` so the admin queue surfaces them.

Why deferred:
  - Submit response stays fast (no WeasyPrint or Anthropic call
    blocking the borrower's submit).
  - Aligns with operator expectations: "AI takes a few minutes to
    think" — the perceived UX is exactly what's actually happening.
  - Failed PDF renders no longer fail the borrower's submit (the
    request is already saved).

Idempotency: each pending row is evaluated at most once. After a
decision, either:
  - status flips to `approved` (auto-approve path), so it's no
    longer matched by the `pending` filter; OR
  - admin_notes gets the `[Auto-approval declined]` prefix, so the
    next tick skips it (we look at admin_notes prefix to detect
    already-evaluated).

Operator override: an admin manually approving from the queue runs
through `_apply_approval` directly via the route handler — same
pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models.app_settings import AppSettings
from app.models.prequal_request import PrequalRequest
from app.models.user import User
from app.schemas.prequal import PrequalRequestApprove
from app.schemas.settings import AppSettingsData
from app.services.prequal_auto_approve import evaluate

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Marker prefix on admin_notes that means "this request has already
# been evaluated and we wrote the blockers." Skip on subsequent ticks.
_DECLINED_PREFIX = "[Auto-approval declined]"


async def evaluate_pending_requests(*, limit: int = 20) -> dict[str, int]:
    """Scheduler job body. Returns {approved, declined, skipped} counts.

    Scope: rows with status='pending' AND admin_notes NULL or not
    starting with the declined-marker prefix. Caps work at `limit` per
    tick so a flood of submits doesn't peg the worker.
    """
    counts = {"approved": 0, "declined": 0, "skipped": 0}

    async with SessionLocal() as db:
        # Settings — single fetch per tick.
        settings_row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
        settings = AppSettingsData.model_validate(
            (settings_row.data if settings_row else {}) or {}
        )
        if not settings.prequal_auto_approval.enabled:
            log.info("prequal_evaluator: auto-approval disabled in settings; skipping tick")
            return counts

        # Pull pending rows that haven't been evaluated yet.
        rows = (
            await db.execute(
                select(PrequalRequest)
                .where(PrequalRequest.status == "pending")
                .order_by(PrequalRequest.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()

        if not rows:
            return counts

        for req in rows:
            # Skip rows we've already evaluated (declined branch).
            if req.admin_notes and req.admin_notes.startswith(_DECLINED_PREFIX):
                counts["skipped"] += 1
                continue

            # Resolve the requesting user + their client (for FICO / tier).
            requester = (
                await db.execute(
                    select(User)
                    .options(selectinload(User.client))
                    .where(User.id == req.requester_id)
                )
            ).scalar_one_or_none()
            client = requester.client if requester else None

            decision = evaluate(req, client, settings)

            if not decision.auto_approve:
                req.admin_notes = (
                    f"{_DECLINED_PREFIX}\n"
                    + "\n".join(f"• {r}" for r in decision.reasons)
                )
                counts["declined"] += 1
                # Yield so we don't peg the loop on a big batch.
                await db.flush()
                await asyncio.sleep(0)
                continue

            # Auto-approve path — call the same pipeline a manual
            # admin click goes through. Local import to avoid the
            # circular (prequal router imports the evaluator only
            # via the scheduler, but the evaluator calling back into
            # the router-level helper is fine because it's just a
            # function ref).
            from app.routers.prequal import _apply_approval

            auto_notes = (
                "[Auto-approved]\n"
                + "\n".join(f"• {r}" for r in decision.reasons)
            )
            payload = PrequalRequestApprove(
                approved_purchase_price=decision.approved_purchase_price,
                approved_loan_amount=decision.approved_loan_amount,
                admin_notes=auto_notes,
                approved_scenario=decision.scenario,
                expiration_days=None,
                borrower_entity=None,
            )
            try:
                await _apply_approval(
                    db, req, payload,
                    actor_user_id=None,
                    actor_label="ai",
                )
                counts["approved"] += 1
            except Exception:
                # Don't fail the whole tick on one bad row. Stamp the
                # row as declined-with-error so the admin queue
                # surfaces it; the operator can manually approve.
                log.exception("prequal_evaluator: auto-approve failed for %s", req.id)
                req.admin_notes = (
                    f"{_DECLINED_PREFIX}\n"
                    "• Auto-approval ran but the PDF render or persistence step failed.\n"
                    "  Operator should review and manually approve."
                )
                counts["declined"] += 1
            await asyncio.sleep(0)

        await db.commit()

    log.info("prequal_evaluator: %s", counts)
    return counts
