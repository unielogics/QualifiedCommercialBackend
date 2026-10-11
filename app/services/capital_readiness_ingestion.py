"""Promote completed statement extraction into immutable readiness periods.

Every row comes from one analysis of one exact file version. Missing entity,
basis, or currency stays explicitly unresolved in a source-local provisional
row. Missing dates remain outside the canonical period ledger. This bridge
never borrows source context from another document or a company-name guess.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
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
    if len(raw) > 128:
        return None
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    # Protect Numeric(18, 2) storage and JSON conversion from hostile or OCR-
    # damaged exponents. The untouched extraction remains in the analysis.
    return result if result.is_finite() and (result == 0 or -18 <= result.adjusted() < 16) else None


def _first_number(facts: dict, *keys: str) -> Decimal | None:
    for key in keys:
        number = _number(facts.get(key))
        if number is not None:
            return number
    return None


def _first_text(facts: dict, *keys: str) -> str:
    for key in keys:
        value = _value(facts.get(key))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _statement_facts(analysis: BucketFileAnalysis, statement_index: int | None) -> dict | None:
    if analysis.status != "completed" or getattr(analysis, "classification", None) not in {
        "current_p_and_l", "profit_and_loss", "income_statement"
    }:
        return None
    data = getattr(analysis, "analysis", None) or {}
    if not isinstance(data, dict):
        return None
    facts = data.get("key_facts")
    if not isinstance(facts, dict):
        return None
    if statement_index is not None:
        statements = facts.get("income_statements")
        if not isinstance(statements, list) or not 0 <= statement_index < len(statements) or not isinstance(statements[statement_index], dict):
            return None
        return statements[statement_index]
    return facts


def statement_context_warnings(facts: dict) -> list[dict[str, str]]:
    """Missing labels warn without throwing away a coherent statement's numbers."""
    warnings = []
    if not _first_text(facts, "business_name", "entity_name"):
        warnings.append({
            "code": "financial_entity_unidentified",
            "severity": "evidence_gap",
            "detail": "The statement's entity is not identified. Ratios use this statement only; confirm the relationship before lending decisions.",
        })
    elif len(_first_text(facts, "business_name", "entity_name")) > 200:
        warnings.append({
            "code": "financial_source_context_invalid",
            "severity": "evidence_gap",
            "detail": "The extracted entity label exceeds the supported length. Numeric evidence is retained; correct the label before canonical period promotion.",
        })
    if _first_text(facts, "basis", "accounting_basis").lower() not in {"cash", "accrual", "tax"}:
        warnings.append({
            "code": "financial_accounting_basis_unknown",
            "severity": "evidence_gap",
            "detail": "Accounting basis is not established. Ratios are provisional and must not be combined with other statements.",
        })
    currency = _first_text(facts, "currency").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency) or currency == "XXX":
        warnings.append({
            "code": "financial_currency_unknown",
            "severity": "evidence_gap",
            "detail": "Currency is not established. Ratios use the same statement's units; do not combine its amounts with another source.",
        })
    return warnings


def _statement_dates(facts: dict) -> tuple[date, date, int] | None:
    try:
        start = date.fromisoformat(str(_value(facts.get("period_start"))))
        end = date.fromisoformat(str(_value(facts.get("period_end"))))
    except (ValueError, TypeError):
        return None
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    return (start, end, months) if end >= start and 1 <= months <= 60 else None


def statement_period_payload(analysis: BucketFileAnalysis, *, statement_index: int | None = None) -> FinancialPeriodCreate | None:
    """Return dated source-local observations; unknown labels stay provisional."""
    facts = _statement_facts(analysis, statement_index)
    if facts is None:
        return None
    entity = _first_text(facts, "business_name", "entity_name")
    if not entity:
        # This is an explicit source label, not a guessed company identity. The
        # analysis and column index prevent unrelated unidentified statements
        # from sharing a compatibility key.
        entity = f"[Unidentified statement:{analysis.id}:{statement_index if statement_index is not None else 'single'}]"
    basis = _first_text(facts, "basis", "accounting_basis").lower()
    if basis not in {"cash", "accrual", "tax"}:
        basis = "unknown"
    currency = _first_text(facts, "currency").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        # ISO XXX explicitly means no established currency. Never silently
        # assume USD or copy the currency from a different file.
        currency = "XXX"
    dates = _statement_dates(facts)
    if dates is None or not re.fullmatch(r"[a-fA-F0-9]{64}", analysis.content_hash or ""):
        return None
    start, end, months = dates
    revenue = _first_number(facts, "gross_revenue", "revenue", "total_revenue")
    cogs = _first_number(facts, "cost_of_goods_sold", "cogs")
    gross = _first_number(facts, "gross_profit")
    net = _first_number(facts, "net_income", "net_profit")
    ebitda = _first_number(facts, "ebitda")
    # A missing interest, tax or depreciation line is unknown, not a zero.
    adjustments = [_first_number(facts, key) for key in ("interest", "income_taxes", "depreciation_and_amortization")]
    if ebitda is None and net is not None and all(value is not None for value in adjustments):
        ebitda = net + sum((value for value in adjustments if value is not None), Decimal("0"))
        if ebitda and ebitda.adjusted() >= 16:
            ebitda = None
    if all(value is None for value in (revenue, cogs, gross, net, ebitda)):
        return None
    confidence = {"high": Decimal("0.85"), "medium": Decimal("0.65"), "low": Decimal("0.35")}.get(str(analysis.confidence or "").lower(), Decimal("0.35"))
    if statement_context_warnings(facts):
        confidence = min(confidence, Decimal("0.65"))
    try:
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
    except ValidationError:
        # Readable evidence is still accepted. An advisory projection must
        # never turn a successful extraction into a failed file analysis.
        return None


def statement_profitability_diagnostics(analysis: BucketFileAnalysis) -> list[dict[str, Any]]:
    """Informative same-statement ratios, even when dated attribution is pending.

    No periods, entities, income, or zero values are invented. This is the
    fixed QC educational baseline, not published lender eligibility or a
    confirmed Capital Readiness score. Canonical scoring uses its own policy.
    """
    facts = _statement_facts(analysis, None)
    if facts is None or not re.fullmatch(r"[a-fA-F0-9]{64}", analysis.content_hash or ""):
        return []
    statements = facts.get("income_statements")
    indexes = list(range(min(len(statements), 60))) if isinstance(statements, list) and statements else [None]
    # Imported lazily because the readiness service also imports this bridge.
    from app.services.capital_readiness import classify_gross_margin, classify_net_margin

    diagnostics = []
    for index in indexes:
        statement = _statement_facts(analysis, index)
        if statement is None:
            continue
        revenue = _first_number(statement, "gross_revenue", "revenue", "total_revenue")
        cogs = _first_number(statement, "cost_of_goods_sold", "cogs")
        stated_gross = _first_number(statement, "gross_profit")
        derived_gross = revenue - cogs if revenue is not None and cogs is not None else None
        gross = stated_gross if stated_gross is not None else derived_gross
        net = _first_number(statement, "net_income", "net_profit")
        if all(value is None for value in (revenue, cogs, gross, net)):
            continue
        warnings: list[dict[str, Any]] = statement_context_warnings(statement)
        dates = _statement_dates(statement)
        payload = statement_period_payload(analysis, statement_index=index)
        if dates is None:
            warnings.append({
                "code": "financial_period_unknown",
                "severity": "evidence_gap",
                "detail": "The statement period is missing or invalid. These ratios are source-local only; no annualization, trend, or period aggregation is available.",
            })
        if stated_gross is not None and derived_gross is not None and abs(stated_gross - derived_gross) > Decimal("1.00"):
            warnings.append({
                "code": "gross_profit_reconciliation",
                "severity": "requires_review",
                "stated": float(stated_gross),
                "derived": float(derived_gross),
                "difference": float(abs(stated_gross - derived_gross)),
                "detail": "Printed gross profit differs from revenue minus COGS. The displayed printed-profit ratio is provisional; resolve the arithmetic before relying on it.",
            })
        if revenue is not None and revenue <= 0:
            warnings.append({
                "code": "nonpositive_revenue",
                "severity": "requires_review",
                "detail": "Margin percentages are unavailable when revenue is zero or negative.",
            })
        gross_margin = gross / revenue * 100 if gross is not None and revenue is not None and revenue > 0 else None
        net_margin = net / revenue * 100 if net is not None and revenue is not None and revenue > 0 else None
        diagnostics.append({
            "policy_key": "qc_lending_margin_v1",
            "formula_version": "same_statement_ratio_v1",
            "state": "provisional",
            "source_file_id": str(analysis.bucket_file_id),
            "source_analysis_id": str(analysis.id),
            "content_hash": analysis.content_hash,
            "statement_index": index,
            "entity_name": _first_text(statement, "business_name", "entity_name") or None,
            "accounting_basis": payload.accounting_basis if payload is not None else _first_text(statement, "basis", "accounting_basis") or None,
            "currency": payload.currency if payload is not None else _first_text(statement, "currency") or None,
            "period_start": dates[0].isoformat() if dates is not None else None,
            "period_end": dates[1].isoformat() if dates is not None else None,
            "revenue": float(revenue) if revenue is not None else None,
            "cogs": float(cogs) if cogs is not None else None,
            "gross_profit": float(gross) if gross is not None else None,
            "gross_profit_source": "stated" if stated_gross is not None else "derived" if derived_gross is not None else "unavailable",
            "derived_gross_profit": float(derived_gross) if derived_gross is not None else None,
            "net_income": float(net) if net is not None else None,
            "gross_margin_pct": float(gross_margin) if gross_margin is not None else None,
            "gross_margin_unrounded": str(gross_margin) if gross_margin is not None else None,
            "gross_margin_status": classify_gross_margin(gross_margin),
            "net_margin_pct": float(net_margin) if net_margin is not None else None,
            "net_margin_unrounded": str(net_margin) if net_margin is not None else None,
            "net_margin_status": classify_net_margin(net_margin),
            "warnings": warnings,
        })
    return diagnostics


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
                reconciliation_warnings=period_warnings(payload) + statement_context_warnings(_statement_facts(analysis, index) or {}),
            )
            db.add(row)
            created += 1
            seen_keys.add(payload.idempotency_key)
    if created:
        await db.flush()
    return created
