"""Bank-account identity & role heuristics — Phase 3 Wave 1.

propose_role is PURE (unit-testable): it turns an extraction account hint plus
the statement's month summaries (and the dealer's other-account deposit
context) into a (role, rationale) proposal.

match_or_create_account is the DB choke point every statement ingest calls:
it resolves the hint to an existing dos_accounts row (mask first, then
institution+name) or creates one with the AI proposal applied.

PRECEDENCE CONTRACT (hard rule): AI drafts, human correction wins and is never
overwritten. An account whose role_set_by=='admin' NEVER has its role changed
here — a differing proposal is discarded entirely for admin rows. For
role_set_by=='ai' rows a differing proposal only refreshes
ai_proposed_role/ai_rationale; the effective role column itself is never
flipped after creation (Wave 1: role changes are an explicit admin PATCH).
Flushes, never commits — callers own the transaction boundary.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DealerAccount, DealerFinancialPeriod

_PAYROLL_KEYWORDS = ("payroll", "paychex", "adp", "gusto")
_SAVINGS_KEYWORDS = ("savings", "money market", "mmkt", "reserve savings")


def _hint_text(hint: dict[str, Any]) -> str:
    return " ".join(
        str(hint.get(k) or "") for k in ("name_hint", "kind_hint", "institution")
    ).lower()


def _months_deposits(months: list[dict[str, Any]] | None) -> float:
    total = 0.0
    for m in months or []:
        if isinstance(m, dict):
            try:
                total += float(m.get("total_deposits") or 0.0)
            except (TypeError, ValueError):
                continue
    return round(total, 2)


def propose_role(
    account_hint: dict[str, Any],
    months: list[dict[str, Any]] | None,
    other_accounts_max_deposits: float | None = None,
) -> tuple[str, str]:
    """Heuristic role proposal -> (role, rationale). Pure.

    Order: payroll keywords in the name hint -> payroll; savings keywords ->
    savings; deposits dominant vs the dealer's other accounts (or no other
    accounts observed yet) -> primary_operating; default secondary.
    """
    text = _hint_text(account_hint or {})
    if any(k in text for k in _PAYROLL_KEYWORDS):
        return "payroll", "Account name/kind hint contains a payroll keyword"
    if any(k in text for k in _SAVINGS_KEYWORDS):
        return "savings", "Account name/kind hint indicates a savings account"
    deposits = _months_deposits(months)
    if deposits > 0:
        if other_accounts_max_deposits is None or other_accounts_max_deposits <= 0:
            return (
                "primary_operating",
                f"First deposit account observed (${deposits:,.0f} in statement deposits) — treated as primary operating",
            )
        if deposits > other_accounts_max_deposits:
            return (
                "primary_operating",
                f"Dominant deposits (${deposits:,.0f}) vs the dealer's other accounts (max ${other_accounts_max_deposits:,.0f})",
            )
    return "secondary", "No payroll/savings signal and deposits are not dominant for this dealer"


async def _other_accounts_max_deposits(
    db: AsyncSession, dealer_id: UUID, exclude_account_id: UUID | None
) -> float | None:
    """Max total deposits across the dealer's OTHER accounts (from the
    per-account period rows). None when no other account has observed deposits."""
    q = (
        select(func.sum(DealerFinancialPeriod.deposits))
        .where(
            DealerFinancialPeriod.dealer_id == dealer_id,
            DealerFinancialPeriod.account_id.is_not(None),
            DealerFinancialPeriod.deposits.is_not(None),
        )
        .group_by(DealerFinancialPeriod.account_id)
    )
    if exclude_account_id is not None:
        q = q.where(DealerFinancialPeriod.account_id != exclude_account_id)
    totals = [float(v) for v in (await db.execute(q)).scalars().all() if v is not None]
    return max(totals) if totals else None


def apply_proposal(account: DealerAccount, role: str, rationale: str) -> bool:
    """Apply an AI role proposal to an EXISTING account under the precedence
    contract. Pure state-machine on the row (unit-testable with a bare object):

    - role_set_by=='admin': untouched entirely — human correction wins, the
      proposal is discarded. Returns False.
    - role_set_by=='ai' and the proposal differs from the current proposal:
      refresh ai_proposed_role/ai_rationale ONLY (role column unchanged).
    Returns True when any field changed.
    """
    if account.role_set_by == "admin":
        return False
    if account.ai_proposed_role != role or account.ai_rationale != rationale:
        account.ai_proposed_role = role
        account.ai_rationale = rationale
        return True
    return False


async def match_or_create_account(
    db: AsyncSession,
    dealer_id: UUID,
    hint: dict[str, Any],
    months: list[dict[str, Any]] | None,
) -> DealerAccount:
    """Resolve an extraction account hint {institution, name_hint, mask,
    kind_hint} to a dos_accounts row.

    Match order: (1) mask (last-4) within the dealer, (2) lower(institution) +
    lower(name). On create, role = the AI proposal (role_set_by='ai'). On
    match, apply_proposal enforces the precedence contract (admin never
    overwritten; ai rows get proposal-side refresh only)."""
    hint = hint or {}
    mask = str(hint.get("mask") or "").strip()[-4:] or None
    institution = (str(hint.get("institution") or "").strip() or None)
    name_hint = (str(hint.get("name_hint") or "").strip() or None)

    account: DealerAccount | None = None
    if mask:
        account = (
            await db.execute(
                select(DealerAccount)
                .where(DealerAccount.dealer_id == dealer_id, DealerAccount.mask == mask)
                .order_by(DealerAccount.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if account is None and (institution or name_hint):
        name_for_match = name_hint or institution or ""
        q = select(DealerAccount).where(
            DealerAccount.dealer_id == dealer_id,
            func.lower(DealerAccount.name) == name_for_match.lower(),
        )
        if institution:
            q = q.where(func.lower(DealerAccount.institution) == institution.lower())
        account = (
            await db.execute(q.order_by(DealerAccount.created_at.asc()).limit(1))
        ).scalar_one_or_none()

    other_max = await _other_accounts_max_deposits(
        db, dealer_id, account.id if account is not None else None
    )
    role, rationale = propose_role(hint, months, other_max)

    if account is not None:
        # Fill in identity gaps the hint can close; never touch admin role.
        if account.institution is None and institution:
            account.institution = institution[:160]
        if account.mask is None and mask:
            account.mask = mask
        apply_proposal(account, role, rationale)
        await db.flush()
        return account

    name = (name_hint or institution or (f"Account ****{mask}" if mask else "Bank account"))[:160]
    account = DealerAccount(
        dealer_id=dealer_id,
        name=name,
        institution=institution[:160] if institution else None,
        mask=mask,
        role=role,
        ai_proposed_role=role,
        ai_rationale=rationale,
        role_set_by="ai",
        status="active",
    )
    db.add(account)
    await db.flush()
    return account
