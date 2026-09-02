"""Plaid Assets-first evidence ingestion.

An Asset Report is the verified, machine-readable bank source. Its JSON feeds
the existing account, cash-event, period, and metric pipeline; its Plaid PDF is
retained beside uploaded documents for human review. No second underwriting
model is introduced.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import (
    DealerBusiness,
    DealerCashEvent,
    DealerPlaidItem,
)
from app.models.application_profile import PlaidAssetReport
from app.services import plaid_lifecycle

from . import buckets_link, plaid_client
from .accounts import match_or_create_account
from .audit import log_action
from .extract import _persist_plan, apply_extraction, load_active_rules, store_document_bytes

logger = logging.getLogger(__name__)

DEFAULT_ASSET_REPORT_DAYS = 210
ASSET_REPORT_REFRESH_DAYS = 30
_NSF_TERMS = (
    "non-sufficient",
    "nonsufficient",
    "insufficient funds",
    "nsf",
    "overdraft fee",
    "returned item",
    "return item",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2)


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _merge_document_evidence_month(
    current: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge account-level risk evidence into the retained Asset Report month.

    Account periods remain separate and are combined by the metric engine. The
    document summary only needs to retain every observed NSF and negative-day
    marker so later audit/readiness reads do not stop at the first account.
    """
    if current is None:
        return dict(incoming)

    merged = dict(current)
    nsf_values = [
        int(float(value))
        for value in (current.get("nsf_count"), incoming.get("nsf_count"))
        if value is not None
    ]
    merged["nsf_count"] = sum(nsf_values) if nsf_values else None

    date_lists = [
        value
        for value in (
            current.get("negative_balance_dates"),
            incoming.get("negative_balance_dates"),
        )
        if isinstance(value, list)
    ]
    if date_lists:
        merged_dates = sorted(
            {
                str(value).strip()
                for values in date_lists
                for value in values
                if str(value).strip()
            }
        )
        merged["negative_balance_dates"] = merged_dates
        merged["negative_balance_days"] = len(merged_dates)
    else:
        merged["negative_balance_dates"] = None
        merged["negative_balance_days"] = None
    return merged


def normalize_asset_report(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Plaid Asset Report JSON to per-account canonical extractions.

    Plaid transaction amounts are positive for debits and negative for credits;
    the Field Desk ledger uses the opposite sign. Daily historical balances are
    used for opening, ending, average, low, and negative-day measurements.
    """
    report = payload.get("report") if isinstance(payload, dict) else None
    report = report if isinstance(report, dict) else payload
    results: list[dict[str, Any]] = []

    for item in report.get("items") or []:
        if not isinstance(item, dict):
            continue
        institution = str(item.get("institution_name") or "").strip() or None
        for account in item.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            monthly_transactions: dict[str, list[dict[str, Any]]] = defaultdict(list)
            monthly_balances: dict[str, list[tuple[date, float]]] = defaultdict(list)

            for transaction in account.get("transactions") or []:
                if not isinstance(transaction, dict):
                    continue
                occurred = _day(transaction.get("date"))
                plaid_amount = _number(transaction.get("amount"))
                if occurred is None or plaid_amount is None:
                    continue
                description = str(
                    transaction.get("original_description")
                    or transaction.get("name")
                    or "Bank transaction"
                ).strip()[:320]
                monthly_transactions[occurred.strftime("%Y-%m")].append(
                    {
                        "date": occurred.isoformat(),
                        "description": description,
                        "amount": round(-plaid_amount, 2),
                    }
                )

            for balance in account.get("historical_balances") or []:
                if not isinstance(balance, dict):
                    continue
                observed = _day(balance.get("date"))
                current = _number(balance.get("current"))
                if observed is None or current is None:
                    continue
                monthly_balances[observed.strftime("%Y-%m")].append((observed, current))

            months: list[dict[str, Any]] = []
            transactions: list[dict[str, Any]] = []
            for month_key in sorted(set(monthly_transactions) | set(monthly_balances)):
                month_transactions = monthly_transactions.get(month_key, [])
                transactions.extend(month_transactions)
                balances = sorted(monthly_balances.get(month_key, []), key=lambda row: row[0])
                balance_values = [row[1] for row in balances]
                descriptions = [str(row.get("description") or "").lower() for row in month_transactions]
                deposits = round(
                    sum(float(row["amount"]) for row in month_transactions if float(row["amount"]) > 0),
                    2,
                )
                withdrawals = round(
                    sum(abs(float(row["amount"])) for row in month_transactions if float(row["amount"]) < 0),
                    2,
                )
                months.append(
                    {
                        "month": month_key,
                        "total_deposits": deposits,
                        "total_withdrawals": withdrawals,
                        "beginning_balance": balance_values[0] if balance_values else None,
                        "ending_balance": balance_values[-1] if balance_values else None,
                        "average_ledger_balance": (
                            round(sum(balance_values) / len(balance_values), 2)
                            if balance_values
                            else None
                        ),
                        "low_daily_balance": min(balance_values) if balance_values else None,
                        "nsf_count": sum(
                            1 for description in descriptions if any(term in description for term in _NSF_TERMS)
                        ),
                        "negative_balance_dates": [
                            observed.isoformat() for observed, value in balances if value < 0
                        ],
                    }
                )

            current_balance = _number((account.get("balances") or {}).get("current"))
            if not months and current_balance is not None:
                months.append(
                    {
                        "month": date.today().strftime("%Y-%m"),
                        "total_deposits": None,
                        "total_withdrawals": None,
                        "beginning_balance": None,
                        "ending_balance": current_balance,
                        "average_ledger_balance": None,
                        "low_daily_balance": None,
                        "nsf_count": None,
                        "negative_balance_dates": None,
                    }
                )

            results.append(
                {
                    "account": {
                        "institution": institution,
                        "name_hint": account.get("official_name") or account.get("name"),
                        "mask": account.get("mask"),
                        "kind_hint": account.get("subtype") or account.get("type"),
                    },
                    "months": months,
                    "transactions": transactions,
                }
            )
    return results


async def ensure_asset_report(
    db: AsyncSession,
    *,
    dealer_id: UUID,
    days_requested: int = DEFAULT_ASSET_REPORT_DAYS,
) -> tuple[PlaidAssetReport, bool]:
    """Return a current report or create one for all active production Items."""
    items = list(
        (
            await db.execute(
                select(DealerPlaidItem).where(
                    DealerPlaidItem.dealer_id == dealer_id,
                    or_(
                        DealerPlaidItem.status == "active",
                        and_(
                            DealerPlaidItem.status == "error",
                            DealerPlaidItem.update_mode_reason.is_(None),
                            DealerPlaidItem.encrypted_access_token.is_not(None),
                        ),
                    ),
                    DealerPlaidItem.environment == plaid_client.environment(),
                )
            )
        ).scalars().all()
    )
    if not items:
        raise plaid_client.PlaidUnavailable("Connect at least one healthy production bank first")
    from app.services import plaid_policy

    items = [item for item in items if "assets" in plaid_policy.item_products(item)]
    if not items:
        raise plaid_client.PlaidUnavailable(
            "Authorize Plaid Assets on at least one connected bank first"
        )
    # A failed Statements pull (for example, an unentitled product) used to
    # mark an otherwise valid Item `error`. Assets may safely retry those
    # token-bearing Items. Login/revocation errors carry update_mode_reason or
    # have no token and are deliberately excluded above.
    for item in items:
        if item.status == "error":
            item.status = "active"
            item.error = None
    source_ids = sorted(str(item.id) for item in items)
    latest = (
        await db.execute(
            select(PlaidAssetReport)
            .where(
                PlaidAssetReport.dealer_id == dealer_id,
                PlaidAssetReport.environment == plaid_client.environment(),
                PlaidAssetReport.removed_at.is_(None),
            )
            .order_by(PlaidAssetReport.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and sorted(latest.source_item_ids or []) == source_ids:
        age = _now() - latest.created_at
        if latest.status in {"ready", "ingest_error"}:
            return latest, False
        if latest.status == "pending" and age < timedelta(days=1):
            return latest, False
        if (
            latest.status == "ingested"
            and age < timedelta(days=ASSET_REPORT_REFRESH_DAYS)
        ):
            return latest, False
        if latest.status == "error" and age < timedelta(days=1):
            return latest, False

    report = await plaid_lifecycle.create_asset_report(
        db,
        items=items,
        dealer_id=dealer_id,
        days_requested=days_requested,
    )
    return report, True


async def ingest_asset_report(db: AsyncSession, asset_report_id: str) -> PlaidAssetReport:
    """Fetch, retain, and normalize one ready Asset Report idempotently."""
    report = (
        await db.execute(
            select(PlaidAssetReport)
            .where(PlaidAssetReport.asset_report_id == asset_report_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if report is None:
        raise ValueError("Asset Report not found")
    if report.ingested_at is not None and report.document_id is not None:
        return report
    if report.dealer_id is None:
        raise ValueError("Asset Report is not linked to a Field Desk file")

    dealer = await db.get(DealerBusiness, report.dealer_id)
    if dealer is None:
        raise ValueError("Field Desk file not found for Asset Report")
    if not dealer.plaid_assets_enabled:
        await plaid_lifecycle.remove_asset_report(report, strict=False)
        await db.flush()
        return report

    token = plaid_client.decrypt_token(report.encrypted_asset_report_token)
    if not token:
        raise plaid_client.PlaidUnavailable("Asset Report token is unavailable")
    payload = await plaid_client.asset_report_get(token)
    pdf = await plaid_client.asset_report_pdf(token)
    normalized = normalize_asset_report(payload)
    if not normalized:
        raise plaid_client.PlaidUnavailable("Plaid returned no usable Asset Report accounts")

    doc = await store_document_bytes(
        db,
        dealer.id,
        pdf,
        f"Plaid Asset Report {report.created_at:%Y-%m-%d}.pdf",
        "application/pdf",
        kind="statement",
    )
    doc.status = "extracting"
    doc.detected_kind = "bank_statement"
    doc.doc_meta = {
        "source": "plaid_assets",
        "verified": True,
        "asset_report_id": report.asset_report_id,
        "days_requested": report.days_requested,
        "account_count": len(normalized),
    }

    rules = await load_active_rules(db, dealer.id)
    all_months: dict[str, dict[str, Any]] = {}
    event_count = 0
    account_names: list[str] = []
    for extraction in normalized:
        plan = apply_extraction(extraction, rules=rules)
        account = await match_or_create_account(db, dealer.id, extraction["account"], plan["months"])
        account_names.append(account.name)
        periods = sorted(plan["event_periods"])
        if periods:
            await db.execute(
                delete(DealerCashEvent).where(
                    DealerCashEvent.dealer_id == dealer.id,
                    DealerCashEvent.account_id == account.id,
                    DealerCashEvent.period.in_(periods),
                    DealerCashEvent.source == "document",
                    or_(
                        DealerCashEvent.categorized_by.is_(None),
                        DealerCashEvent.categorized_by != "admin",
                    ),
                )
            )
        await _persist_plan(
            db,
            dealer.id,
            plan,
            account_id=account.id,
            document_id=doc.id,
        )
        event_count += len(plan["events"])
        for month in plan["months"]:
            key = str(month.get("month") or "")
            if key:
                all_months[key] = _merge_document_evidence_month(
                    all_months.get(key), month
                )

    doc.account_id = None
    doc.extracted = {
        "source": "plaid_assets",
        "parser": "plaid_assets",
        "doc_type": "bank_statement",
        "months": [all_months[key] for key in sorted(all_months)],
        "transactions_count": event_count,
        "notes": [
            f"Verified Plaid Asset Report covering {report.days_requested} days.",
            f"Accounts: {', '.join(account_names[:12])}",
        ],
    }
    doc.status = "extracted"
    doc.error = None
    await buckets_link.push_document(db, dealer, doc, len(pdf))

    report.status = "ingested"
    report.error = None
    report.ingested_at = _now()
    report.document_id = doc.id
    source_ids = [UUID(value) for value in report.source_item_ids or []]
    if source_ids:
        rows = list(
            (
                await db.execute(select(DealerPlaidItem).where(DealerPlaidItem.id.in_(source_ids)))
            ).scalars().all()
        )
        for item in rows:
            item.status = "active"
            item.error = None
            item.last_pulled_at = _now()
            item.next_refresh_at = _now() + timedelta(days=ASSET_REPORT_REFRESH_DAYS)
    await log_action(
        db,
        dealer.id,
        None,
        "plaid.asset_report.ingested",
        "plaid_asset_report",
        entity_id=report.id,
        after={
            "document_id": str(doc.id),
            "months": sorted(all_months),
            "accounts": len(account_names),
            "transactions": event_count,
        },
    )
    await db.flush()
    return report


async def ingest_asset_report_background(asset_report_id: str) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as db:
        owner = (
            await db.execute(
                select(
                    PlaidAssetReport.dealer_id,
                    PlaidAssetReport.profile_id,
                ).where(PlaidAssetReport.asset_report_id == asset_report_id)
            )
        ).one_or_none()
        # Standalone application profiles retain their existing downloadable
        # Asset Report workflow. They do not own Dealer OS period/event rows,
        # so this Field Desk ingestion worker must leave them in `ready`.
        if owner is None or owner.dealer_id is None:
            return
        try:
            await ingest_asset_report(db, asset_report_id)
            await db.commit()
        except Exception as exc:
            await db.rollback()
            report = (
                await db.execute(
                    select(PlaidAssetReport).where(
                        PlaidAssetReport.asset_report_id == asset_report_id
                    )
                )
            ).scalar_one_or_none()
            if report is not None and report.ingested_at is None:
                report.status = "ingest_error"
                report.error = str(exc)[:2000]
                await db.commit()
            logger.exception("Plaid Asset Report ingestion failed for %s", asset_report_id)
