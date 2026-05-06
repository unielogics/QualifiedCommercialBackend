"""Derive a borrower-facing credit summary + product eligibility from a
parsed iSoftPull report.

Two outputs:

    `summarize(report)` -> ClientSummary
      Conservative bullets + headline FICO + simulator tier hint.
      Safe to surface to the borrower in the mobile/desktop UI.

    `match_products(fico, derogatory_flag)` -> list of qualifying SKUs
      Filters the rate sheet by min_fico (and a couple of conservative
      FICO+derogatory rules) so we can tell the borrower exactly which
      programs they can shop right now.

Goes hand in hand with services/isoftpull_report_parser.py — the report
parser turns HTML into ScrapedReport; this module turns ScrapedReport
into something a borrower (and the simulator) can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from app.routers.rates import _RATES, RateSKU
from app.services.isoftpull_report_parser import ScrapedReport, TradeAccount


# ── FICO tier mapping (mirrors qcdesktop/qcmobile lib/eligibility.ts) ──────


def fico_tier(fico: int | None) -> str:
    if fico is None:
        return "blocked"
    if fico < 620:
        return "blocked"
    if fico < 680:
        return "warn"
    if fico < 720:
        return "basic"
    return "pro"


def tier_max_ltv(tier: str) -> float:
    return {"blocked": 0.0, "warn": 0.65, "basic": 0.65, "pro": 0.75}.get(tier, 0.0)


# ── Tradeline aggregates ────────────────────────────────────────────────────

_DEROG_KEYWORDS = (
    "delinquen", "past due date", "charge", "collection", "120 days",
    "90 days", "60 days", "default", "repossess",
)
_OPEN_KEYWORDS = ("open", "current")


def _to_int(s: str | None) -> int | None:
    if s is None:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _is_derogatory(rating: str | None) -> bool:
    if not rating:
        return False
    text = rating.lower()
    return any(k in text for k in _DEROG_KEYWORDS)


def _is_open(status: str | None) -> bool:
    if not status:
        return False
    text = status.lower()
    return any(k in text for k in _OPEN_KEYWORDS)


@dataclass
class TradeAggregates:
    open_count: int = 0
    closed_count: int = 0
    derogatory_count: int = 0
    total_balance: int = 0
    total_credit_limit: int = 0
    total_monthly_payment: int = 0
    revolving_balance: int = 0
    revolving_credit_limit: int = 0
    revolving_utilization: float | None = None  # 0..1
    has_mortgage: bool = False
    oldest_account_opened: date | None = None
    by_type: dict[str, int] = field(default_factory=dict)


def aggregate_trades(accounts: Iterable[TradeAccount]) -> TradeAggregates:
    agg = TradeAggregates()
    for ta in accounts:
        f = ta.fields
        status = f.get("account_status")
        rating = f.get("account_rating")
        portfolio = (f.get("portfolio_type") or "").lower()
        balance = _to_int(f.get("balance")) or 0
        limit = _to_int(f.get("credit_limit")) or 0
        monthly = _to_int(f.get("monthly_payment_amount")) or 0
        opened = _parse_date(f.get("date_of_opening"))
        atype = (f.get("account_type") or "").lower()

        if _is_open(status):
            agg.open_count += 1
        else:
            agg.closed_count += 1
        if _is_derogatory(rating):
            agg.derogatory_count += 1
        agg.total_balance += balance
        agg.total_credit_limit += limit
        agg.total_monthly_payment += monthly
        if portfolio == "revolving":
            agg.revolving_balance += balance
            agg.revolving_credit_limit += limit
        if "real estate" in atype or "mortgage" in atype:
            agg.has_mortgage = True
        if opened is not None:
            if agg.oldest_account_opened is None or opened < agg.oldest_account_opened:
                agg.oldest_account_opened = opened
        bucket = atype or "unknown"
        agg.by_type[bucket] = agg.by_type.get(bucket, 0) + 1

    if agg.revolving_credit_limit > 0:
        agg.revolving_utilization = round(
            agg.revolving_balance / agg.revolving_credit_limit, 3
        )
    return agg


def recent_inquiry_count(report: ScrapedReport, months: int = 6) -> int:
    cutoff_year = date.today().year
    cutoff_month = date.today().month - months
    while cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1
    cutoff = date(cutoff_year, cutoff_month, 1)
    count = 0
    for inq in report.inquiries:
        d = _parse_date(inq.fields.get("date"))
        if d and d >= cutoff:
            count += 1
    return count


# ── Product matching ───────────────────────────────────────────────────────


@dataclass
class ProductMatch:
    sku: RateSKU
    qualifies: bool
    reason: str | None = None  # populated when qualifies=False


def match_products(
    *,
    fico: int | None,
    has_recent_derogatory: bool = False,
    recent_inquiries_6mo: int = 0,
) -> list[ProductMatch]:
    """Return one ProductMatch per SKU on the rate sheet.

    Conservative rules:
      - FICO floor from sku.min_fico is hard.
      - Recent derogatory: blocks DSCR (which is meant to be the cleanest
        product). FF/GU/Bridge stay in scope — those are short-term hard-
        money products and tolerate spotty credit.
      - 6+ inquiries in 6 months: blocks DSCR purchase 80 LTV (the highest
        leverage tier needs cleaner profiles).
    """
    out: list[ProductMatch] = []
    for sku in _RATES:
        if fico is None:
            out.append(ProductMatch(sku=sku, qualifies=False, reason="no_credit"))
            continue
        if fico < sku.min_fico:
            out.append(ProductMatch(
                sku=sku, qualifies=False,
                reason=f"fico_below_min ({fico} < {sku.min_fico})",
            ))
            continue
        if has_recent_derogatory and sku.loan_type.value == "dscr":
            out.append(ProductMatch(
                sku=sku, qualifies=False,
                reason="derogatory_history (DSCR requires clean profile)",
            ))
            continue
        if recent_inquiries_6mo >= 6 and sku.max_ltv >= 0.80:
            out.append(ProductMatch(
                sku=sku, qualifies=False,
                reason="excessive_inquiries (high-leverage SKUs require cleaner profiles)",
            ))
            continue
        out.append(ProductMatch(sku=sku, qualifies=True))
    return out


# ── Borrower-facing summary ────────────────────────────────────────────────


@dataclass
class SummaryBullet:
    kind: str  # "positive" | "neutral" | "warn"
    label: str
    detail: str | None = None


@dataclass
class ClientSummary:
    """What a borrower sees on their dashboard after the soft pull lands."""
    fico: int | None
    fico_model: str | None
    tier: str
    tier_max_ltv: float
    bullets: list[SummaryBullet] = field(default_factory=list)
    aggregates: TradeAggregates = field(default_factory=TradeAggregates)
    recent_inquiries_6mo: int = 0
    available_products: list[dict] = field(default_factory=list)
    blocked_products: list[dict] = field(default_factory=list)
    fraud_flag: str | None = None  # populated when iSoftPull surfaced a fraud_shield indicator


def _bullet_for_inquiries(count: int) -> SummaryBullet:
    if count == 0:
        return SummaryBullet("positive", "No recent credit inquiries", "Last 6 months")
    if count <= 2:
        return SummaryBullet("neutral", f"{count} recent inquiries", "Last 6 months")
    return SummaryBullet("warn", f"{count} recent inquiries", "Last 6 months — may affect rates")


def _bullet_for_utilization(util: float | None) -> SummaryBullet | None:
    if util is None:
        return None
    pct = round(util * 100)
    if util < 0.30:
        return SummaryBullet("positive", f"Revolving utilization {pct}%", "Healthy — under 30%")
    if util < 0.60:
        return SummaryBullet("neutral", f"Revolving utilization {pct}%", "30–60% — manageable")
    return SummaryBullet("warn", f"Revolving utilization {pct}%", "Above 60% — consider paying down")


def _bullet_for_derogatory(count: int) -> SummaryBullet | None:
    if count == 0:
        return SummaryBullet("positive", "No derogatory accounts on file")
    if count == 1:
        return SummaryBullet("warn", "1 derogatory account on file")
    return SummaryBullet("warn", f"{count} derogatory accounts on file")


def _bullet_for_mortgage(has_mortgage: bool) -> SummaryBullet:
    if has_mortgage:
        return SummaryBullet("positive", "Existing mortgage history", "Counts toward investor experience")
    return SummaryBullet("neutral", "No mortgage history on file")


def _bullet_for_age(oldest: date | None) -> SummaryBullet | None:
    if oldest is None:
        return None
    years = (date.today() - oldest).days // 365
    if years >= 10:
        return SummaryBullet("positive", f"Credit history {years} years", "Long-established profile")
    if years >= 5:
        return SummaryBullet("neutral", f"Credit history {years} years")
    return SummaryBullet("warn", f"Credit history {years} years", "Younger profile — limits some products")


def summarize(report: ScrapedReport) -> ClientSummary:
    fico = report.best_score
    tier = fico_tier(fico)
    aggs = aggregate_trades(report.trade_accounts)
    inq6 = recent_inquiry_count(report, months=6)

    matches = match_products(
        fico=fico,
        has_recent_derogatory=aggs.derogatory_count > 0,
        recent_inquiries_6mo=inq6,
    )

    bullets: list[SummaryBullet] = []
    bullets.append(_bullet_for_derogatory(aggs.derogatory_count) or SummaryBullet("neutral", ""))
    util_bullet = _bullet_for_utilization(aggs.revolving_utilization)
    if util_bullet:
        bullets.append(util_bullet)
    bullets.append(_bullet_for_inquiries(inq6))
    bullets.append(_bullet_for_mortgage(aggs.has_mortgage))
    age_bullet = _bullet_for_age(aggs.oldest_account_opened)
    if age_bullet:
        bullets.append(age_bullet)

    fraud_flag = report.identity_risk.fraud_shield.get("text") if report.identity_risk.fraud_shield else None

    return ClientSummary(
        fico=fico,
        fico_model=report.best_score_model,
        tier=tier,
        tier_max_ltv=tier_max_ltv(tier),
        bullets=[b for b in bullets if b.label],
        aggregates=aggs,
        recent_inquiries_6mo=inq6,
        available_products=[
            {
                "id": m.sku.id,
                "label": m.sku.label,
                "loan_type": m.sku.loan_type.value,
                "rate": m.sku.rate,
                "max_ltv": m.sku.max_ltv,
                "term": m.sku.term,
                "min_fico": m.sku.min_fico,
            }
            for m in matches
            if m.qualifies
        ],
        blocked_products=[
            {
                "id": m.sku.id,
                "label": m.sku.label,
                "loan_type": m.sku.loan_type.value,
                "min_fico": m.sku.min_fico,
                "reason": m.reason,
            }
            for m in matches
            if not m.qualifies
        ],
        fraud_flag=fraud_flag,
    )
