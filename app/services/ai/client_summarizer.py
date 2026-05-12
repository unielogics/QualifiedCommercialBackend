"""Per-client (account-wide) AI summarizer — Phase 8.

The scheduler hits `refresh_all_active_clients()` every day at 3am
UTC. The borrower-facing AI Underwriter and the smart-routing CTA
cards both read from the JSONB this writes onto `Client`.

Distinct from the per-loan `summarizer.py` ('The Associate'):
  - per-loan summarizer answers "what's the bottleneck on THIS deal?"
  - this aggregator answers "what's the borrower's whole portfolio
    look like, and what's the single most useful action across all
    of it?"

Output shape (`Client.living_profile` JSONB):

    {
      "outstanding_documents": [
        {"loan_id": uuid, "deal_id": "L-1234", "name": "...", "days_overdue": 7}
      ],
      "blocking_credit_issues": ["FICO below DSCR floor", ...],
      "next_actions": [
        {
          "title": "Upload your latest 2 years of tax returns",
          "owner": "client",
          "priority": "high",
          "cta": "upload_doc",
          "due_at": null
        }
      ],
      "rate_pressure_notes": [
        "Rates trending higher in the last week; consider locking soon."
      ],
      "suggested_next_loan": "DSCR Refi at 75% — current portfolio
                              ratios meet matrix"
    }

Plus `Client.living_summary` (human-readable narrative paragraph)
and `Client.living_refreshed_at` (timestamp).

Cost discipline:
  - Single Haiku call per client per refresh
  - Aggregator scopes to clients with at least one non-funded loan
    OR an open prequal — funded-and-quiet clients aren't refreshed
  - `refresh_all_active_clients(limit=50)` caps the daily fan-out
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocStatus, LoanStage
from app.models.activity import Activity
from app.models.client import Client
from app.models.credit_pull import CreditPull
from app.models.document import Document
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.services.ai.anthropic_client import get_client, model_light
from app.services.credit_summary import fico_tier, tier_max_ltv

log = logging.getLogger(__name__)


CLIENT_SUMMARIZER_SYSTEM = """### ROLE
You are the Account-wide AI Underwriter at Qualified Commercial. You see the borrower's WHOLE portfolio (all loans, all prequals, all documents, all credit) and your job is to surface the single most useful next-action ranking and a short narrative the borrower can act on.

### OUTPUT FORMAT
Return a JSON object EXACTLY in this shape:

{
  "summary": "2-3 sentence narrative the borrower can read on their home screen. No bullet points, no markdown.",
  "outstanding_documents": [
    {"loan_id": "uuid", "deal_id": "L-1234", "name": "Tax Returns 2023", "days_overdue": 7}
  ],
  "blocking_credit_issues": [
    "FICO 612 is below the DSCR product floor of 660."
  ],
  "next_actions": [
    {
      "title": "Upload your last 2 years of tax returns",
      "owner": "client",
      "priority": "high",
      "cta": "upload_doc",
      "due_at": null
    }
  ],
  "rate_pressure_notes": [
    "Rates moved up slightly last week — worth checking in if you're close to locking."
  ],
  "suggested_next_loan": "Optional: a short pitch for a follow-on loan that fits this borrower's portfolio (or null)."
}

### RULES
- Only the keys shown above. Don't invent extras.
- `owner` is one of "client" | "broker" | "ai".
- `priority` is one of "low" | "medium" | "high".
- `cta` is one of "upload_doc" | "run_credit" | "complete_profile" | "accept_prequal_offer" | "decline_prequal_offer" | "submit_prequal" | "respond_to_message" | "none".
- Cap `next_actions` at 5 items, ranked by impact-to-close.
- Never invent loan IDs, deal IDs, FICO scores, or facts. If the context doesn't have it, leave the field empty.
- Privacy gate: NEVER reveal specific lender names. Refer to them as "the lender" or "the underwriter."
- Rate language: NEVER use "basis points" / "bps", spread, markup, or any rate-breakdown vocabulary in `rate_pressure_notes` or anywhere else. Speak in plain trends ("rates moved up slightly", "consider locking soon") without numeric bps deltas.
- Strict JSON only. No prose around the object. No code fences.
"""


@dataclass
class ClientLivingProfile:
    """In-memory shape returned to callers; the JSONB on the Client
    table mirrors this dict exactly."""

    summary: str
    outstanding_documents: list[dict[str, Any]]
    blocking_credit_issues: list[str]
    next_actions: list[dict[str, Any]]
    rate_pressure_notes: list[str]
    suggested_next_loan: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "outstanding_documents": self.outstanding_documents,
            "blocking_credit_issues": self.blocking_credit_issues,
            "next_actions": self.next_actions,
            "rate_pressure_notes": self.rate_pressure_notes,
            "suggested_next_loan": self.suggested_next_loan,
        }


def _empty_profile(reason: str) -> ClientLivingProfile:
    """Fallback when there's nothing meaningful to summarize OR when
    the AI call fails. Keeps the JSONB present so frontends can
    render an empty state without null-checking every field."""
    return ClientLivingProfile(
        summary=reason,
        outstanding_documents=[],
        blocking_credit_issues=[],
        next_actions=[],
        rate_pressure_notes=[],
        suggested_next_loan=None,
    )


async def _build_client_context(db: AsyncSession, client: Client) -> str:
    """Render the cross-loan context block fed to the LLM. Same
    pattern as `_build_account_context` in routers/ai.py but
    operator-detached (no User identity since this runs from a
    cron, not a request)."""
    lines: list[str] = ["=== CLIENT PORTFOLIO ==="]
    lines.append(f"Client ID (UUID): {client.id}")
    lines.append(f"Borrower: {client.name or '?'} <{client.email or '—'}>")
    if client.fico is not None:
        tier = fico_tier(client.fico)
        cap = tier_max_ltv(tier)
        lines.append(
            f"FICO: {client.fico} (tier={tier}, tier-max-LTV={cap * 100:.0f}%)"
        )
    else:
        lines.append("FICO: not yet pulled")

    # Latest credit pull — relevant for expirations
    latest = (
        await db.execute(
            select(CreditPull)
            .where(CreditPull.client_id == client.id)
            .order_by(CreditPull.pulled_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest and latest.pulled_at:
        pulled = latest.pulled_at.date().isoformat()
        expires = latest.expires_at.date().isoformat() if latest.expires_at else "n/a"
        now = datetime.now(timezone.utc)
        expired = bool(latest.expires_at and latest.expires_at < now)
        lines.append(
            f"Latest credit pull: {pulled} → expires {expires}"
            f"{' (EXPIRED)' if expired else ''}"
        )

    # Loans
    loans = (
        await db.execute(
            select(Loan)
            .where(Loan.client_id == client.id)
            .order_by(Loan.created_at.desc())
        )
    ).scalars().all()
    if loans:
        lines.append("")
        lines.append(f"Loans ({len(loans)}):")
        for loan in loans:
            stage = loan.stage.value if hasattr(loan.stage, "value") else str(loan.stage)
            ltype = loan.type.value if hasattr(loan.type, "value") else str(loan.type)
            lines.append(
                f"  - {loan.deal_id} (loan_id={loan.id}) · {loan.address}"
                f" · {ltype} · stage={stage}"
                f" · ${float(loan.amount or 0):,.0f}"
            )
            if loan.status_summary:
                lines.append(f"    Latest: {loan.status_summary}")

    # Outstanding docs across all loans
    if loans:
        loan_ids = [l.id for l in loans]
        docs = (
            await db.execute(
                select(Document)
                .where(
                    Document.loan_id.in_(loan_ids),
                    Document.status.in_(
                        [DocStatus.REQUESTED, DocStatus.PENDING, DocStatus.FLAGGED]
                    ),
                )
                .order_by(Document.requested_on.asc().nullslast())
            )
        ).scalars().all()
        if docs:
            deal_by_id = {l.id: l.deal_id for l in loans}
            now = datetime.now(timezone.utc)
            lines.append("")
            lines.append("Outstanding documents:")
            for d in docs:
                deal = deal_by_id.get(d.loan_id, "?")
                requested_str = d.requested_on.isoformat() if d.requested_on else "—"
                days_overdue: int | None = None
                if d.requested_on:
                    days_overdue = (now.date() - d.requested_on.date()).days
                status_str = d.status.value if hasattr(d.status, "value") else str(d.status)
                overdue_str = f" · {days_overdue}d since request" if days_overdue is not None else ""
                lines.append(
                    f"  - {d.name} ({status_str}) · loan {deal}"
                    f" (loan_id={d.loan_id}) · requested {requested_str}{overdue_str}"
                )

    # Active prequals — scoped to the client's primary User. Client→User
    # is 1:1 in the current schema; if that becomes 1:N we'll switch to
    # an `IN (...)` filter on the user IDs.
    prequals: list[PrequalRequest] = []
    if client.user_id is not None:
        prequals = (
            await db.execute(
                select(PrequalRequest)
                .where(PrequalRequest.requester_id == client.user_id)
                .where(
                    PrequalRequest.status.in_(["pending", "approved", "offer_accepted"]),
                )
                .order_by(PrequalRequest.created_at.desc())
            )
        ).scalars().all()
    if prequals:
        lines.append("")
        lines.append("Active prequal requests:")
        for r in prequals:
            lines.append(
                f"  - {r.target_property_address} (prequal_id={r.id})"
                f" · {r.loan_type} · status={r.status}"
                f" · quote_number={r.quote_number or '—'}"
            )

    # Recent cross-loan activity
    if loans:
        loan_ids = [l.id for l in loans]
        activities = (
            await db.execute(
                select(Activity)
                .where(Activity.loan_id.in_(loan_ids))
                .order_by(Activity.occurred_at.desc())
                .limit(15)
            )
        ).scalars().all()
        if activities:
            lines.append("")
            lines.append("Recent activity (newest first):")
            for a in activities:
                ts = a.occurred_at.date().isoformat() if a.occurred_at else "—"
                lines.append(f"  - [{ts}] [{a.kind}] {a.summary}")

    lines.append("=== END CLIENT PORTFOLIO ===")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """LLMs sometimes wrap JSON in ```json … ```; strip that."""
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl > 0:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _coerce_profile(parsed: dict[str, Any]) -> ClientLivingProfile:
    """Defensively map the LLM JSON onto our dataclass — drops
    unknown keys, fills missing ones with empty defaults."""
    def _list(key: str) -> list[Any]:
        v = parsed.get(key)
        return v if isinstance(v, list) else []

    return ClientLivingProfile(
        summary=str(parsed.get("summary") or "").strip()[:1200],
        outstanding_documents=[d for d in _list("outstanding_documents") if isinstance(d, dict)][:25],
        blocking_credit_issues=[str(s) for s in _list("blocking_credit_issues") if s][:10],
        next_actions=[a for a in _list("next_actions") if isinstance(a, dict)][:5],
        rate_pressure_notes=[str(s) for s in _list("rate_pressure_notes") if s][:5],
        suggested_next_loan=(parsed.get("suggested_next_loan") or None),
    )


async def refresh_client_summary(
    db: AsyncSession, client_id: UUID
) -> ClientLivingProfile:
    """Aggregate the client's portfolio + activity, call Haiku for
    the typed JSON, persist onto Client.living_profile / summary /
    refreshed_at. Returns the dataclass so callers can render the
    fresh state without re-reading.

    Falls back to a placeholder profile (and still updates
    refreshed_at) on Anthropic failure or when the API key is unset
    — frontends shouldn't have to deal with a null living_profile.
    """
    client = (
        await db.execute(
            select(Client)
            .options(selectinload(Client.user))
            .where(Client.id == client_id)
        )
    ).scalar_one_or_none()
    if client is None:
        raise ValueError(f"Client {client_id} not found")

    context_block = await _build_client_context(db, client)
    settings = get_settings()

    profile: ClientLivingProfile
    if not settings.anthropic_api_key:
        profile = _empty_profile(
            "Account summary will populate once the AI key is configured."
        )
    else:
        anthropic = get_client()
        try:
            result = await anthropic.messages.create(
                model=model_light(),
                max_tokens=900,
                system=CLIENT_SUMMARIZER_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Summarize this borrower's portfolio and produce the "
                            "JSON object as specified. Below is the full context.\n\n"
                            + context_block
                        ),
                    }
                ],
            )
            raw = "".join(
                block.text for block in result.content
                if getattr(block, "type", None) == "text"
            )
            cleaned = _strip_code_fence(raw)
            try:
                parsed = json.loads(cleaned)
                profile = _coerce_profile(parsed if isinstance(parsed, dict) else {})
            except json.JSONDecodeError:
                log.warning(
                    "client_summarizer: client=%s returned non-JSON; using fallback",
                    client_id,
                )
                profile = _empty_profile(
                    "Couldn't parse the AI response. We'll retry on the next refresh."
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("client_summarizer: client=%s Anthropic failed: %s", client_id, exc)
            profile = _empty_profile(
                "Account summary is temporarily unavailable. Try again in a few minutes."
            )

    client.living_profile = profile.to_dict()
    client.living_summary = profile.summary or None
    client.living_refreshed_at = datetime.now(timezone.utc)
    await db.commit()
    return profile


async def _select_active_clients(db: AsyncSession, *, limit: int) -> list[UUID]:
    """A client is 'active' if they have at least one non-funded loan
    OR an open prequal request. Funded-and-quiet clients aren't
    refreshed — saves spend on dormant accounts."""
    # Loans-side: anything not yet funded counts as 'active'.
    loan_clients = (
        await db.execute(
            select(Loan.client_id)
            .where(Loan.client_id.isnot(None))
            .where(Loan.stage != LoanStage.FUNDED)
            .distinct()
        )
    ).scalars().all()

    # Prequal-side: requester_id is a User; we resolve to client_id via the
    # User → Client link. For v1 simplicity we just include every prequal's
    # requesting user's client_id.
    from app.models.user import User  # local import to avoid cycles
    prequal_clients = (
        await db.execute(
            select(User.client_id)
            .join(PrequalRequest, PrequalRequest.requester_id == User.id)
            .where(PrequalRequest.status.in_(["pending", "approved", "offer_accepted"]))
            .where(User.client_id.isnot(None))
            .distinct()
        )
    ).scalars().all()

    seen: set[UUID] = set()
    out: list[UUID] = []
    for cid in [*loan_clients, *prequal_clients]:
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
        if len(out) >= limit:
            break
    return out


async def refresh_all_active_clients(*, limit: int = 50) -> int:
    """Scheduler job body — opens its own session, picks up to `limit`
    active clients, refreshes each one. Per-client failures are
    isolated. Returns the count of clients successfully refreshed."""
    refreshed = 0
    async with SessionLocal() as db:
        client_ids = await _select_active_clients(db, limit=limit)
        log.info("client_summarizer: refreshing %d active clients", len(client_ids))
        for cid in client_ids:
            try:
                await refresh_client_summary(db, cid)
                refreshed += 1
            except Exception:  # noqa: BLE001
                log.exception("client_summarizer: refresh failed client=%s", cid)
                # rollback so the next iteration starts clean
                await db.rollback()
    return refreshed
