"""Evidence-based desk review, not a credit decision or a replacement score."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

VERSION = 1


def key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def fields(row: dict) -> dict:
    return {key(str(k)): v for k, v in (row.get("fields", row) or {}).items()}


def number(value) -> Decimal | None:
    text = str(value or "").strip().replace(",", "").replace("$", "").replace("%", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def report_date(value) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return None


def analyze_report(report: dict, *, as_of: date | datetime | None = None) -> dict:
    reference = as_of.date() if isinstance(as_of, datetime) else as_of or datetime.now(UTC).date()
    flags, limitations = [], []

    def add(code, title, severity, path, evidence, detail):
        flags.append({"id": f"{code}:{path}", "code": code, "title": title, "severity": severity,
                      "source": path, "evidence": evidence, "detail": detail})

    trades = report.get("trade_accounts") or []
    for i, row in enumerate(trades):
        f = fields(row)
        path = f"trade_accounts[{i}]"
        label = str(f.get("company") or f.get("creditor") or f"Account {i + 1}")[:100]
        status = str(f.get("account_status") or f.get("payment_status") or "")
        rating = str(f.get("account_rating") or "")
        text = f"{status} {rating}".lower()
        past_due = number(f.get("past_due") or f.get("amount_past_due") or f.get("past_due_amount"))
        days = number(f.get("days_past_due"))
        current_late = bool((past_due is not None and past_due > 0) or (days is not None and days >= 30))
        if re.search(r"\b(?:past due|delinquent|\d{2,3} days late)\b", status.lower()) and not re.search(r"\b(?:not|no|was|previously)\b", status.lower()):
            current_late = True
        if current_late:
            add("past_due", f"{label}: past-due account", "high", path,
                {k: f[k] for k in ("account_status", "payment_status", "past_due", "amount_past_due", "past_due_amount", "days_past_due") if k in f},
                "The report identifies an outstanding delinquency. Verify the current balance and payment status with the creditor.")
        late_date = report_date(f.get("last_delinquency_date") or f.get("max_delinquency_date") or f.get("date_of_last_late_payment"))
        late_counts = {k: v for k, v in f.items() if re.search(r"(?:30|60|90).*late.*(?:count|month)|late.*(?:30|60|90)", k) and (number(v) or 0) > 0}
        historical = bool(late_counts or (late_date and late_date <= reference) or (re.search(r"\b(?:late|delinquent)\b", rating.lower()) and not re.search(r"\b(?:not|no)\b", rating.lower())))
        if historical:
            recent = bool(late_date and 0 <= (reference - late_date).days <= 183)
            add("recent_late" if recent else "payment_history", f"{label}: {'late payment within 6 months' if recent else 'late-payment history'}",
                "high" if recent else "warning", path,
                {**late_counts, **({"delinquency_date": late_date.isoformat()} if late_date else {}), **({"account_rating": rating} if rating else {})},
                "Recency is measured against the report date, not the account's last update." if late_date else "A late-payment signal is present, but its date is unavailable; do not assume it is recent.")
        for code, pattern, title in (("charge_off", r"charge[ -]?off|charged off", "charge-off reported"),
                                      ("repossession", r"repossession|repossessed", "repossession reported"),
                                      ("foreclosure", r"foreclos", "foreclosure reported")):
            if re.search(pattern, text) and not re.search(r"\b(?:no|not)\b", text):
                add(code, f"{label}: {title}", "high", path, {"status": status, "rating": rating}, "Review the full tradeline, dates, balance, and any dispute before making a decision.")
        balance, limit = number(f.get("balance")), number(f.get("credit_limit"))
        if limit and limit > 0 and balance is not None and balance >= 0 and balance / limit >= Decimal("0.80"):
            add("utilization", f"{label}: high reported utilization", "warning", path,
                {"balance": str(balance), "credit_limit": str(limit), "utilization_pct": round(float(balance / limit * 100), 1)},
                "At least 80% of the reported limit is used. This is a review threshold, not an approval or denial rule.")
    for section in ("collections", "public_records"):
        for i, row in enumerate(report.get(section) or []):
            f = fields(row)
            path = f"{section}[{i}]"
            # Only inspect classification/status fields, never a person's name or address.
            classification = " ".join(str(v) for k, v in f.items() if re.search(r"type|status|chapter|disposition|record|remark", k)).lower()
            if section == "collections":
                balance = number(f.get("balance") or f.get("current_balance"))
                add("collection", "Collection account reported", "high" if balance and balance > 0 else "warning", path,
                    {k: v for k, v in f.items() if re.search(r"balance|status|date|paid", k)},
                    "Review whether the collection is outstanding, paid, disputed, or included in bankruptcy; its presence alone does not establish an unpaid balance.")
            elif (re.search(r"bankrupt|chapter\s*(?:7|11|13)", classification) or str(f.get("chapter", "")).strip() in ("7", "11", "13")) and not re.search(r"\bno bankruptcy\b", classification):
                add("bankruptcy", "Bankruptcy record reported", "high", path,
                    {k: v for k, v in f.items() if re.search(r"type|status|chapter|date|disposition", k)},
                    "Check filing and discharge/dismissal dates and affected accounts. A historical record does not mean an active bankruptcy.")
            elif classification and not re.search(r"none|no records|no bankruptcy", classification):
                add("public_record", "Public record requires review", "warning", path,
                    {k: v for k, v in f.items() if re.search(r"type|status|date|disposition", k)}, "Inspect the original record and verify its current disposition.")
    if not trades:
        limitations.append("No tradelines were available for payment-history review.")
    if trades and not any(report_date(fields(t).get("max_delinquency_date") or fields(t).get("last_delinquency_date") or fields(t).get("date_of_last_late_payment")) for t in trades):
        limitations.append("Dated late-payment history is unavailable; recent late payments cannot be ruled out.")
    flags.sort(key=lambda f: (f["severity"] != "high", f["id"]))
    return {"version": VERSION, "as_of": reference.isoformat(), "flags": flags,
            "high_count": sum(f["severity"] == "high" for f in flags), "flag_count": len(flags),
            "level": "high" if any(f["severity"] == "high" for f in flags) else "warning" if flags else "limited" if limitations else "no_flags_detected",
            "limitations": limitations, "advisory": "Review support only. Verify report accuracy and disputes; no automated credit decision is made."}
