"""Activity-log helper + summary-dirty drain.

Three responsibilities:

1. `log_activity(db, *, loan_id, ...)` — drop-in replacement for the
   `db.add(Activity(...))` calls scattered across routers. ALSO
   flips the parent `Loan.summary_dirty=True` flag so the next drain
   tick picks it up. Idempotency: setting `summary_dirty=True` twice
   is a no-op.

2. `log_change(...) / log_loan_diff(...) / diff_changes(...)` — new
   diff-aware writers. Use these when a state mutation has a clear
   before/after (e.g. operator edits the base rate). The payload
   carries a structured `changes` list so the AI prompt + the
   activity UI can render "Base rate moved from 7.50% to 7.80% on
   2026-05-12" instead of the opaque "loan.updated".

3. `drain_summary_dirty(limit=N)` — scheduler job (every 5 min). Picks
   up to N dirty Loan rows and runs the summarizer on each. Bounds
   Anthropic spend by `limit` per tick; failed refreshes leave the
   dirty flag set so they retry next tick.

Plus a **visibility registry** mapping every `kind` string to one of
client_visible | operator_visible | internal_only. The borrower
portal, AI's client-audience prompt, and any shared feeds all filter
by this. Unknown kinds default to operator_visible so additions don't
leak to borrowers without an explicit registry entry.

Why a flag rather than synchronous summarizer-on-write?
  - Summarizer call to Haiku is ~1-2s. Doing it inside a request
    blocks the API.
  - Activity inserts often come in bursts (operator updates 5 fields,
    each writes an Activity). We want one summary refresh per burst,
    not five.
  - Dirty + drain keeps the model fresh-enough (5-min worst-case lag)
    without coupling refresh latency to user-facing requests.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

# Don't re-summarize a single loan more than once per this window —
# a chatty loan was triggering ~20 redundant Living-Profile rebuilds a
# day. The dirty flag stays set so it still refreshes once the window
# passes (or sooner if forced elsewhere).
MIN_RESUMMARIZE_MINUTES = 60
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.activity import Activity
from app.models.loan import Loan
from app.services.ai import engagement, summarizer

log = logging.getLogger(__name__)


async def log_activity(
    db: AsyncSession,
    *,
    loan_id: UUID | None,
    actor_id: UUID | None = None,
    actor_label: str | None = None,
    kind: str,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    mark_dirty: bool = True,
) -> Activity:
    """Drop-in for `db.add(Activity(...))` that ALSO flips the parent
    Loan's `summary_dirty=True` so the dirty-drain job picks it up.

    `mark_dirty=False` opts out — used for the dirty-drain job's own
    success Activity rows so we don't infinite-loop ourselves.

    Returns the Activity (already in the session, not yet committed)."""
    act = Activity(
        loan_id=loan_id,
        actor_id=actor_id,
        actor_label=actor_label,
        kind=kind,
        summary=summary,
        payload=payload,
    )
    db.add(act)
    if mark_dirty and loan_id is not None:
        loan = await db.get(Loan, loan_id)
        if loan is not None and not loan.summary_dirty:
            loan.summary_dirty = True
    return act


async def mark_loan_dirty(db: AsyncSession, loan_id: UUID) -> None:
    """Plain dirty-flag flipper for paths that are creating Activity
    rows directly (not through log_activity). Cheap, idempotent."""
    loan = await db.get(Loan, loan_id)
    if loan is not None and not loan.summary_dirty:
        loan.summary_dirty = True


async def drain_summary_dirty(*, limit: int = 20) -> int:
    """Scheduler job (every 5 min). Picks up to `limit` Loan rows
    where summary_dirty=True, runs the summarizer on each, then
    clears the flag. Failures leave the flag set so the next tick
    retries.

    Returns the count of successful refreshes (for telemetry).

    Bounded concurrency: we run summaries SEQUENTIALLY (with an
    asyncio.sleep(0) between iterations so the event loop stays
    responsive). The summarizer hits Anthropic; running 20 in
    parallel would burst the rate limit and fight the orchestrator
    for tokens. Sequential at ~1-2s each = ~20-40s per tick which
    fits comfortably inside the 5-min interval.

    Engagement-paused loans are short-circuited (no LLM call) but
    we still clear the dirty flag so they don't accumulate forever.
    The next user-driven event will set the flag again."""
    refreshed = 0
    failed = 0

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Loan).where(Loan.summary_dirty == True).limit(limit)
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        for loan in rows:
            # Engagement gate — don't burn LLM tokens on paused loans.
            # `is_paused` is sync; takes the loan instance directly.
            if engagement.is_paused(loan):
                loan.summary_dirty = False
                loan.summary_refreshed_at = datetime.now(timezone.utc)
                continue

            # Throttle: skip if summarized within the window. Leave the
            # dirty flag SET so it refreshes once the window passes — no
            # LLM call burned in the meantime.
            if loan.summary_refreshed_at and (
                now - loan.summary_refreshed_at
            ) < timedelta(minutes=MIN_RESUMMARIZE_MINUTES):
                continue

            try:
                await summarizer.refresh_summary(db, loan.id)
                loan.summary_dirty = False
                loan.summary_refreshed_at = datetime.now(timezone.utc)
                refreshed += 1
            except Exception:
                # Leave dirty=True so the next tick retries. The
                # exception is logged but doesn't propagate — we want
                # the rest of the batch to keep flowing.
                log.exception(
                    "drain_summary_dirty: refresh failed loan=%s deal_id=%s",
                    loan.id, getattr(loan, "deal_id", "?"),
                )
                failed += 1

            # Yield to the event loop so the API stays responsive.
            await asyncio.sleep(0)

        await db.commit()

    log.info(
        "drain_summary_dirty: refreshed=%d failed=%d limit=%d",
        refreshed, failed, limit,
    )
    return refreshed


# ── Visibility registry ────────────────────────────────────────────


CLIENT_VISIBLE = "client_visible"
OPERATOR_VISIBLE = "operator_visible"
INTERNAL_ONLY = "internal_only"


# Maps kind → visibility. Unknown kinds default to OPERATOR_VISIBLE so
# we surface to the operator audit feed but never to the borrower
# without explicit opt-in. When a new kind lands it should be tagged
# here in the same commit.
_KIND_VISIBILITY: dict[str, str] = {
    # Loan lifecycle
    "loan.created": CLIENT_VISIBLE,
    "loan.stage_change": CLIENT_VISIBLE,
    "loan.lender_connected": CLIENT_VISIBLE,
    "loan.lender_disconnected": OPERATOR_VISIBLE,
    "loan.property_updated": CLIENT_VISIBLE,
    "loan.property_intake_updated": CLIENT_VISIBLE,
    "loan.property_intake_completed": CLIENT_VISIBLE,
    # Criteria edits — operator-visible only. Borrower sees the
    # outcome via the criteria/file UI, not every intermediate edit.
    "loan.criteria_changed": OPERATOR_VISIBLE,
    "loan.scenario_saved": OPERATOR_VISIBLE,
    "loan.scenario_promoted": OPERATOR_VISIBLE,
    # Pricing — internal only. NEVER expose markup mechanics.
    "loan.pricing_changed": INTERNAL_ONLY,
    "loan.rate_sheet_attached": INTERNAL_ONLY,
    # Documents — borrower sees their own uploads / requests
    "document.requested": CLIENT_VISIBLE,
    "document.received": CLIENT_VISIBLE,
    "document.status_changed": CLIENT_VISIBLE,
    "document.due_date_changed": CLIENT_VISIBLE,
    "document.custom_created": CLIENT_VISIBLE,
    "document.routed": OPERATOR_VISIBLE,
    # Credit — operator-visible at the change level; raw bureau JSON
    # never lands in the summary text, only counts/state.
    "credit.pulled": OPERATOR_VISIBLE,
    "credit.pull_expired": OPERATOR_VISIBLE,
    "credit.fico_override_set": OPERATOR_VISIBLE,
    "credit.fico_override_cleared": OPERATOR_VISIBLE,
    "credit.fico_changed": OPERATOR_VISIBLE,
    # HUD — operator only.
    "hud.line_added": OPERATOR_VISIBLE,
    "hud.line_edited": OPERATOR_VISIBLE,
    "hud.line_deleted": OPERATOR_VISIBLE,
    "hud.share_link_created": OPERATOR_VISIBLE,
    "hud.share_link_revoked": OPERATOR_VISIBLE,
    "hud.line_added_by_invitee": OPERATOR_VISIBLE,
    # Instructions / AI corrections — operator only
    "instruction.created": OPERATOR_VISIBLE,
    "instruction.deactivated": OPERATOR_VISIBLE,
    "ai_modify.correction_added": INTERNAL_ONLY,
    "ai_modify.correction_dismissed": INTERNAL_ONLY,
    # AI engagement / tasks
    "ai.paused_by_super_admin": INTERNAL_ONLY,
    "ai.resumed_by_super_admin": INTERNAL_ONLY,
    "ai.calendar_event_proposed": OPERATOR_VISIBLE,
    "ai_task.broker_suggestion": OPERATOR_VISIBLE,
    "ai_task.approved": OPERATOR_VISIBLE,
    "ai_task.rejected": OPERATOR_VISIBLE,
    "ai_task.dismissed": INTERNAL_ONLY,
    # Calendar
    "calendar.created": OPERATOR_VISIBLE,
    "calendar.deleted": OPERATOR_VISIBLE,
    "calendar.completed": CLIENT_VISIBLE,
    "calendar.cancelled": CLIENT_VISIBLE,
    # Prequal flow
    "prequal.requested": CLIENT_VISIBLE,
    "prequal.revised": CLIENT_VISIBLE,
    "prequal.rejected": CLIENT_VISIBLE,
    "prequal.offer_accepted": CLIENT_VISIBLE,
    "analysis.created": OPERATOR_VISIBLE,
    "analysis.shared_to_client": CLIENT_VISIBLE,
    "analysis.prequal_requested": CLIENT_VISIBLE,
    # Intake
    "intake.submitted": CLIENT_VISIBLE,
    # Misc
    "email.draft_dismissed": OPERATOR_VISIBLE,
    "settings.updated": INTERNAL_ONLY,
    "summary.refreshed": INTERNAL_ONLY,
    "ai_task.broker_suggestion_filed": OPERATOR_VISIBLE,
    "loan.property_intake_completed": CLIENT_VISIBLE,
}


def kind_visibility(kind: str) -> str:
    """Resolve a kind's visibility. Unknown kinds → OPERATOR_VISIBLE
    (safe default — never leak to client without an explicit
    registry entry)."""
    return _KIND_VISIBILITY.get(kind, OPERATOR_VISIBLE)


def is_visible_to(kind: str, audience: str) -> bool:
    """True when activities of this kind should appear for the given
    audience. `audience` values: 'client' | 'broker' | 'super_admin'."""
    vis = kind_visibility(kind)
    if vis == CLIENT_VISIBLE:
        return True
    if vis == OPERATOR_VISIBLE:
        return audience in ("broker", "super_admin")
    # INTERNAL_ONLY — super_admin only.
    return audience == "super_admin"


# ── Diff-aware writers ─────────────────────────────────────────────


from typing import Iterable, Mapping  # noqa: E402

# Re-export for callers; lets them write `from activity_log import User`
# rather than knowing where the model lives.
from app.models.user import User  # noqa: E402, F401


async def log_change(
    db: AsyncSession,
    *,
    kind: str,
    summary: str,
    loan_id: UUID | None = None,
    client_id: UUID | None = None,
    actor: User | None = None,
    actor_label: str | None = None,
    payload: dict[str, Any] | None = None,
    mark_dirty: bool = True,
) -> Activity:
    """Diff-aware activity writer. Pass `actor` (the User) when known —
    we'll fill in `actor_id` and a sensible `actor_label` (their role).

    `payload` conventional keys:
      - `before` / `after`   for single-value scalar changes
      - `changes`            list of {field, before, after} for batch diffs
      - `target_id`          UUID of the row that changed (when not the loan itself)
      - any kind-specific keys

    Calls into `log_activity` so the summary-dirty machinery stays
    wired. Caller is responsible for flush/commit."""
    actor_id = actor.id if actor else None
    if actor_label is None and actor is not None:
        role = getattr(actor, "role", None)
        actor_label = role.value if hasattr(role, "value") else (str(role) if role else "user")

    act = await log_activity(
        db,
        loan_id=loan_id,
        actor_id=actor_id,
        actor_label=actor_label,
        kind=kind,
        summary=summary[:512],
        payload=payload,
        mark_dirty=mark_dirty,
    )
    # log_activity doesn't accept client_id today — set it directly
    # on the just-created Activity for non-loan-scoped events (agent CRM).
    if client_id is not None and act.loan_id is None:
        act.client_id = client_id
    return act


def diff_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    fields: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Return [{field, before, after}, ...] for keys whose value
    changed. Compares scalars with `!=` — for floats / Decimals we
    coerce to float first so 7.5 == Decimal('7.50000').

    `fields` optionally restricts the comparison to a known subset
    (preferred — avoids accidentally diffing computed columns or
    `updated_at`)."""
    keys = list(fields) if fields is not None else list(after.keys())
    changes: list[dict[str, Any]] = []
    for k in keys:
        b = before.get(k)
        a = after.get(k)
        if _values_equal(b, a):
            continue
        changes.append({"field": k, "before": _serialize(b), "after": _serialize(a)})
    return changes


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    # Coerce Decimal / numeric → float for comparison
    try:
        af = float(a)  # type: ignore[arg-type]
        bf = float(b)  # type: ignore[arg-type]
        return af == bf
    except (TypeError, ValueError):
        pass
    # Enum unwrap
    av = a.value if hasattr(a, "value") else a
    bv = b.value if hasattr(b, "value") else b
    return av == bv


def _serialize(v: Any) -> Any:
    """JSON-safe coercion. Decimal → float, Enum → value, datetime →
    isoformat, UUID → str. Anything else passes through."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, (str, int, bool, list, dict)):
        return v
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    return str(v)


# Fields we care about diffing on a Loan PATCH. Anything not in this
# list is ignored — keeps the activity stream focused on substantive
# underwriting changes vs metadata churn (updated_at, etc.).
LOAN_DIFF_FIELDS: tuple[str, ...] = (
    # Pricing
    "amount", "base_rate", "discount_points", "final_rate",
    "origination_pct", "lender_fees",
    # Structure
    "term_months", "amortization_style", "prepay_penalty",
    # Collateral / sizing
    "ltv", "ltc", "arv", "purpose", "property_type",
    # Income / DSCR
    "monthly_rent", "dscr", "vacancy_pct", "expense_ratio_pct",
    # Carrying costs
    "annual_taxes", "annual_insurance", "monthly_hoa", "reserves_required",
    # Borrower
    "fico_override", "entity_type", "experience_tier",
    # Type-specific
    "construction_holdback_pct", "draw_count", "exit_strategy",
    "cash_to_borrower", "seasoning_months", "property_count",
    # Lifecycle
    "stage", "lender_id", "close_date",
)


_PRICING_DIFF_KEYS: frozenset[str] = frozenset({
    "base_rate", "discount_points", "origination_pct", "lender_fees",
})


def loan_snapshot(loan: Any) -> dict[str, Any]:
    """Return a dict-snapshot of the loan's diff-relevant fields.
    Take this BEFORE applying a mutation, take it AGAIN after, then
    pass both to `log_loan_diff()`."""
    return {f: getattr(loan, f, None) for f in LOAN_DIFF_FIELDS}


async def log_loan_diff(
    db: AsyncSession,
    *,
    loan: Any,
    before: Mapping[str, Any],
    actor: User | None = None,
    source: str = "operator_edit",
) -> Activity | None:
    """Compare a loan's current state to a pre-mutation snapshot and
    write a single Activity row with the structured diff. Returns
    None when nothing meaningful changed (no row written).

    `source` is folded into the payload — distinguishes operator-edited
    criteria from AI-driven mutations from automated re-pricing.

    Pricing-only edits get kind=`loan.pricing_changed` (INTERNAL_ONLY
    visibility); other criteria edits get `loan.criteria_changed`
    (operator-visible). When BOTH are changed in one save, the row is
    `loan.criteria_changed` — the audience filter will strip the
    pricing-only keys when rendering to non-operator surfaces.
    """
    after = loan_snapshot(loan)
    changes = diff_changes(before, after, fields=LOAN_DIFF_FIELDS)
    if not changes:
        return None

    non_pricing = [c for c in changes if c["field"] not in _PRICING_DIFF_KEYS]
    kind = "loan.criteria_changed" if non_pricing else "loan.pricing_changed"
    summary = summarize_diff(changes)

    return await log_change(
        db,
        kind=kind,
        summary=summary,
        loan_id=getattr(loan, "id", None),
        actor=actor,
        payload={"source": source, "changes": changes},
    )


# ── Humanization helpers ───────────────────────────────────────────
#
# The activity payload stores raw column names ("base_rate") and raw
# values (Decimal("7.5000")). For human display — both the Activity.summary
# column and the AI prompt's diff lines — those need to become labels +
# formatted values: "Base rate: 7.50% → 7.80%".
#
# The frontend has a mirror of this mapping in
# src/lib/activityFormat.ts. Keep them in sync when adding fields.


# Field display labels keyed on the raw column name. Anything not in
# this map falls back to a Title Case of the snake_case field.
_FIELD_LABELS: dict[str, str] = {
    "amount": "Loan amount",
    "base_rate": "Base rate",
    "discount_points": "Discount points",
    "final_rate": "Final rate",
    "origination_pct": "Origination",
    "lender_fees": "Lender fees",
    "term_months": "Term",
    "amortization_style": "Amortization",
    "prepay_penalty": "Prepay penalty",
    "ltv": "LTV",
    "ltc": "LTC",
    "arv": "ARV",
    "purpose": "Purpose",
    "property_type": "Property type",
    "monthly_rent": "Monthly rent",
    "dscr": "DSCR",
    "vacancy_pct": "Vacancy",
    "expense_ratio_pct": "Expense ratio",
    "annual_taxes": "Annual taxes",
    "annual_insurance": "Annual insurance",
    "monthly_hoa": "Monthly HOA",
    "reserves_required": "Reserves required",
    "fico_override": "FICO override",
    "entity_type": "Entity type",
    "experience_tier": "Experience tier",
    "construction_holdback_pct": "Construction holdback",
    "draw_count": "Draws",
    "exit_strategy": "Exit strategy",
    "cash_to_borrower": "Cash to borrower",
    "seasoning_months": "Seasoning",
    "property_count": "Properties",
    "stage": "Stage",
    "lender_id": "Lender",
    "close_date": "Close date",
    # HUD line fields
    "label": "Label",
    "category": "Category",
    "payee": "Payee",
    "note": "Note",
}


# Value-formatting kinds keyed by column name. Drives the unit / scale
# logic in `format_field_value()`. "percent_fraction" means a value
# stored as a 0–1 decimal (LTV 0.75 → "75.00%"); "percent_rate" means
# already in percent units (base_rate 7.5 → "7.50%"); "months_to_years"
# converts when the value divides cleanly by 12.
_FIELD_VALUE_KINDS: dict[str, str] = {
    "amount": "money",
    "arv": "money",
    "lender_fees": "money",
    "monthly_rent": "money",
    "annual_taxes": "money",
    "annual_insurance": "money",
    "monthly_hoa": "money",
    "reserves_required": "money",
    "cash_to_borrower": "money",
    "base_rate": "percent_rate",
    "final_rate": "percent_rate",
    "origination_pct": "percent_fraction",
    "ltv": "percent_fraction",
    "ltc": "percent_fraction",
    "vacancy_pct": "percent_fraction",
    "expense_ratio_pct": "percent_fraction",
    "construction_holdback_pct": "percent_fraction",
    "discount_points": "points",
    "dscr": "ratio",
    "term_months": "months",
    "seasoning_months": "months",
    "fico_override": "integer",
    "draw_count": "integer",
    "property_count": "integer",
    "close_date": "date",
    "amortization_style": "enum",
    "prepay_penalty": "enum",
    "purpose": "enum",
    "property_type": "enum",
    "entity_type": "enum",
    "experience_tier": "enum",
    "exit_strategy": "enum",
    "stage": "enum",
}


def field_label(field: str) -> str:
    """Human label for an activity-payload field name. Unknown fields
    fall back to Title Case (snake_case stripped)."""
    if field in _FIELD_LABELS:
        return _FIELD_LABELS[field]
    return field.replace("_", " ").capitalize()


def format_field_value(field: str, value: Any) -> str:
    """Format a raw payload value for human display, keyed off the
    field name. None becomes em-dash. Returns a string."""
    if value is None:
        return "—"
    kind = _FIELD_VALUE_KINDS.get(field)
    return _format_value_by_kind(value, kind)


def _format_value_by_kind(value: Any, kind: str | None) -> str:
    try:
        if kind == "money":
            return f"${float(value):,.0f}"
        if kind == "percent_rate":
            return f"{float(value):.2f}%"
        if kind == "percent_fraction":
            return f"{float(value) * 100:.2f}%"
        if kind == "points":
            return f"{float(value):.3f} pts"
        if kind == "ratio":
            return f"{float(value):.2f}"
        if kind == "months":
            n = int(float(value))
            if n > 0 and n % 12 == 0:
                years = n // 12
                return f"{years} year{'s' if years != 1 else ''}"
            return f"{n} month{'s' if n != 1 else ''}"
        if kind == "integer":
            return f"{int(float(value)):,}"
        if kind == "enum":
            s = str(value)
            # Strip the StrEnum prefix if it leaked in ("LoanStage.PREQUALIFIED")
            if "." in s:
                s = s.rsplit(".", 1)[-1]
            return s.replace("_", " ").title()
        if kind == "date":
            # ISO date already; strip the time portion when present.
            s = str(value)
            return s.split("T")[0] if "T" in s else s
    except (TypeError, ValueError):
        pass
    # Fallback — generic short form.
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def summarize_diff(changes: list[dict[str, Any]]) -> str:
    """Human-readable one-liner for the Activity row. Caps at 3
    fields with "…and N more" for the rest so the summary column
    (varchar 512) never overflows on a bulk edit. Uses field_label +
    format_field_value so "base_rate" becomes "Base rate" and 7.5
    becomes "7.50%"."""
    if not changes:
        return "No changes"
    parts = []
    for c in changes[:3]:
        parts.append(format_field_change(c))
    extra = len(changes) - 3
    if extra > 0:
        parts.append(f"and {extra} more field{'s' if extra != 1 else ''}")
    return ", ".join(parts)


def format_field_change(change: dict[str, Any]) -> str:
    """Single field's human-readable diff line:
    "Base rate: 7.50% → 7.80%"."""
    field = change.get("field") or "?"
    return (
        f"{field_label(field)}: "
        f"{format_field_value(field, change.get('before'))} → "
        f"{format_field_value(field, change.get('after'))}"
    )


def filter_payload_for_audience(
    payload: dict[str, Any] | None,
    *,
    kind: str,
    audience: str,
) -> dict[str, Any] | None:
    """Strip payload keys that shouldn't reach the given audience.

    Even on an OPERATOR_VISIBLE kind like `loan.criteria_changed`,
    the diff may include pricing keys (base_rate, discount_points)
    that the client must not see. When audience='client' we drop any
    `changes[]` entries whose `field` is in the pricing set."""
    if payload is None:
        return None
    if audience != "client":
        return payload
    changes = payload.get("changes")
    if not isinstance(changes, list):
        return payload
    filtered = [c for c in changes if isinstance(c, dict) and c.get("field") not in _PRICING_DIFF_KEYS]
    if filtered == changes:
        return payload
    return {**payload, "changes": filtered}
