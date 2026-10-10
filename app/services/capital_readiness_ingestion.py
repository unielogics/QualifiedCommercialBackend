"""Promote completed statement extraction into immutable readiness periods.

Every row comes from one analysis of one exact file version. Missing entity,
period, basis or currency stays unresolved; this bridge never borrows those
attributes from another document or from a company-name guess.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.capital_readiness import ApplicationFinancialPeriod
from app.schemas.capital_readiness import FinancialPeriodCreate
from app.services.operator_file_links import selected_files_for_intake


def _value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else value


def _number(value: Any) -> Decimal | None:
    value = _value(value)
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "").replace("$", "")
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _first_number(facts: dict, *keys: str) -> Decimal | None:
    for key in keys:
        number = _number(facts.get(key))
        if number is not None:
            return number
    return None


def statement_period_payload(analysis: BucketFileAnalysis, *, statement_index: int | None = None) -> FinancialPeriodCreate | None:
    """Return typed, unverified values only when their source context is explicit."""
    if analysis.status != "completed" or analysis.classification not in {"current_p_and_l", "profit_and_loss", "income_statement"}:
        return None
    facts = (analysis.analysis or {}).get("key_facts")
    if not isinstance(facts, dict):
        return None
    if statement_index is not None:
        statements = facts.get("income_statements")
        if not isinstance(statements, list) or not 0 <= statement_index < len(statements) or not isinstance(statements[statement_index], dict):
            return None
        facts = statements[statement_index]
    entity = str(_value(facts.get("business_name") or facts.get("entity_name")) or "").strip()
    basis = str(_value(facts.get("basis") or facts.get("accounting_basis")) or "").strip().lower()
    currency = str(_value(facts.get("currency")) or "").strip().upper()
    if not entity or basis not in {"cash", "accrual", "tax"} or not re.fullmatch(r"[A-Z]{3}", currency):
        return None
    try:
        start = date.fromisoformat(str(_value(facts.get("period_start"))))
        end = date.fromisoformat(str(_value(facts.get("period_end"))))
    except (ValueError, TypeError):
        return None
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if end < start or not 1 <= months <= 60 or not re.fullmatch(r"[a-fA-F0-9]{64}", analysis.content_hash or ""):
        return None
    revenue = _first_number(facts, "gross_revenue", "revenue", "total_revenue")
    cogs = _first_number(facts, "cost_of_goods_sold", "cogs")
    gross = _first_number(facts, "gross_profit")
    net = _first_number(facts, "net_income", "net_profit")
    ebitda = _first_number(facts, "ebitda")
    # A missing interest, tax or depreciation line is unknown, not a zero.
    adjustments = [_first_number(facts, key) for key in ("interest", "income_taxes", "depreciation_and_amortization")]
    if ebitda is None and net is not None and all(value is not None for value in adjustments):
        ebitda = net + sum((value for value in adjustments if value is not None), Decimal("0"))
    if all(value is None for value in (revenue, cogs, gross, net, ebitda)):
        return None
    confidence = {"high": Decimal("0.85"), "medium": Decimal("0.65"), "low": Decimal("0.35")}.get(str(analysis.confidence or "").lower(), Decimal("0.35"))
    return FinancialPeriodCreate(
        entity_name=entity,
        accounting_basis=basis,
        currency=currency,
        period_start=start,
        period_end=end,
        months_covered=months,
        source_kind="ai_extracted",
        cogs_applicability="applicable" if cogs is not None else "unknown",
        revenue=revenue,
        cogs=cogs,
        gross_profit=gross,
        operating_expenses=_first_number(facts, "total_operating_expenses", "operating_expenses"),
        operating_income=_first_number(facts, "operating_income"),
        net_income=net,
        ebitda=ebitda,
        confidence=confidence,
        source_file_id=analysis.bucket_file_id,
        source_analysis_id=analysis.id,
        extractor_version=f"bucket_analysis_v{analysis.analysis_version}",
        content_hash=analysis.content_hash,
        idempotency_key=f"statement-analysis:{analysis.id}" + (f":{statement_index}" if statement_index is not None else ""),
    )


async def ingest_profile_financial_periods(db: AsyncSession, profile: ApplicationProfile) -> int:
    """Called under the profile lock during recalc; no provider call or commit."""
    selected_files = await selected_files_for_intake(db, profile.intake_id) if profile.intake_id else []
    source_scope = []
    if profile.primary_bucket_id is not None:
        source_scope.append(BucketFile.bucket_id == profile.primary_bucket_id)
    if selected_files:
        source_scope.append(BucketFile.id.in_([file.id for file in selected_files]))
    if not source_scope:
        return 0
    analyses = (
        await db.execute(
            select(BucketFileAnalysis)
            .join(BucketFile, BucketFile.id == BucketFileAnalysis.bucket_file_id)
            .where(
                or_(*source_scope),
                BucketFileAnalysis.bucket_id == BucketFile.bucket_id,
                BucketFile.deleted_at.is_(None),
                BucketFile.status == "uploaded",
                BucketFileAnalysis.status == "completed",
                BucketFileAnalysis.content_hash == BucketFile.content_hash,
            )
            .order_by(BucketFileAnalysis.created_at.desc(), BucketFileAnalysis.id.desc())
        )
    ).scalars().all()
    existing = (
        await db.execute(select(ApplicationFinancialPeriod.idempotency_key).where(ApplicationFinancialPeriod.profile_id == profile.id))
    ).scalars().all()
    seen_keys, seen_files = set(existing), set()
    created = 0
    # Import the newest analysis only; old immutable periods remain in history.
    for analysis in analyses:
        if analysis.bucket_file_id in seen_files:
            continue
        seen_files.add(analysis.bucket_file_id)
        facts = (analysis.analysis or {}).get("key_facts") or {}
        statements = facts.get("income_statements") if isinstance(facts, dict) else None
        indexes = list(range(min(len(statements), 60))) if isinstance(statements, list) and statements else [None]
        for index in indexes:
            payload = statement_period_payload(analysis, statement_index=index)
            if payload is None or payload.idempotency_key in seen_keys:
                continue
            from app.services.capital_readiness import period_warnings

            row = ApplicationFinancialPeriod(
                profile_id=profile.id,
                **payload.model_dump(exclude={"adjusted_ebitda"}),
                review_status="submitted",
                reconciliation_warnings=period_warnings(payload),
            )
            db.add(row)
            created += 1
            seen_keys.add(payload.idempotency_key)
    if created:
        await db.flush()
    return created
