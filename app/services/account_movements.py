"""Evidence-only account movements; this read model never changes financial facts.

The caller supplies one profile's canonical account identities. Monthly totals
and recurring-payment aggregates are coverage context, never transaction lines.
Only two independently located, accepted lines can support an internal pair.
"""

from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.bucket_evidence import statement_period_month

SCHEMA_VERSION = "qc_account_movements.v1"
MAX_RECORDS = 10_000
MAX_NODES = 50
MAX_LINKS = 100
MAX_PAGE_SIZE = 200
PAIR_DAYS = 3
_TRANSFER = re.compile(r"\b(?:transfer|xfer|sweep|book\s+entry)\b", re.I)
_TARGET = re.compile(r"\b(?:to|from|acct|account|a/c)\s*(?:ending\s*(?:in)?\s*)?(?:[xX*•#-]+\s*)?(\d{4,})\b", re.I)


def _id(*values: Any) -> str:
    return hashlib.sha256("|".join(str(value) for value in values).encode()).hexdigest()[:24]


def _text(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for pattern, sample in (("%Y-%m-%d", text[:10]), ("%m/%d/%Y", text)):
        try:
            return datetime.strptime(sample, pattern).date()
        except ValueError:
            pass
    return None


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    text = re.sub(r"[,$\s]", "", str(value))
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
        return number if number.is_finite() and abs(number) < Decimal("1000000000000") and number == number.quantize(Decimal("0.01")) else None
    except InvalidOperation:
        return None


def _currency(value: Any) -> str | None:
    text = _text(value).upper()
    return text if re.fullmatch(r"[A-Z]{3}", text) and text != "XXX" else None


def _integer(value: Any) -> int | None:
    text = str(value or "")
    return int(text) if text.isdigit() and 0 < int(text) <= 100_000 else None


def _period(source: dict, fallback: Any = None) -> tuple[date | None, date | None]:
    start, end = _day(source.get("period_start")), _day(source.get("period_end"))
    stated = str(source.get("statement_period") or fallback or "")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", stated)
    month = re.fullmatch(r"(\d{4})-(\d{2})", statement_period_month(source.get("month") or stated) or "")
    if not dates and month:
        year, number = (int(part) for part in month.groups())
        if 1 <= number <= 12:
            return date(year, number, 1), date(year, number, monthrange(year, number)[1])
    return start or (_day(dates[0]) if dates else None), end or (_day(dates[-1]) if len(dates) > 1 else None)


def _signed(raw: dict) -> tuple[Decimal | None, str | None, list[str]]:
    amount = _money(raw.get("signed_amount", raw.get("amount")))
    direction = _text(raw.get("direction") or raw.get("debit_credit") or raw.get("transaction_type")).casefold()
    direction = "out" if direction in {"out", "outbound", "debit", "withdrawal", "dr"} else "in" if direction in {"in", "inbound", "credit", "deposit", "cr"} else None
    debit, credit = _money(raw.get("debit")), _money(raw.get("credit"))
    if debit not in (None, 0) and credit not in (None, 0):
        return None, None, ["contradictory_direction"]
    column_amount = -abs(debit) if debit not in (None, 0) else abs(credit) if credit not in (None, 0) else None
    if column_amount is not None:
        column_direction = "out" if column_amount < 0 else "in"
        if direction and direction != column_direction or amount is not None and amount < 0 < column_amount:
            return None, None, ["contradictory_direction"]
        if amount is not None and abs(amount) != abs(column_amount):
            return None, None, ["contradictory_amount"]
        if "signed_amount" in raw and amount is not None and amount != column_amount:
            return None, None, ["contradictory_direction"]
        return column_amount, column_direction, []
    if amount is None:
        return None, direction, ["amount_unknown"]
    if direction == "in" and amount < 0:
        return None, None, ["contradictory_direction"]
    if direction:
        return (-abs(amount) if direction == "out" else amount), direction, []
    if "signed_amount" in raw or raw.get("amount_convention") == "credits_positive" or amount < 0:
        return amount, "out" if amount < 0 else "in", []
    return amount, None, ["direction_unknown"]


def _arrays(facts: dict) -> list[tuple[dict, str, list]]:
    # File-level lines win over a duplicated per-month copy of the same list.
    for field in ("transactions", "transaction_lines", "transfer_lines"):
        if isinstance(facts.get(field), list) and facts[field]:
            return [(facts, field, facts[field])]
    found = []
    for month in facts.get("months") or []:
        if not isinstance(month, dict):
            continue
        for field in ("transactions", "transaction_lines", "transfer_lines"):
            if isinstance(month.get(field), list) and month[field]:
                found.append((month, field, month[field]))
                break
    return found


def _source(item: dict, raw: dict, signed: Decimal | None, direction: str | None, on: date | None, currency: str | None) -> dict:
    locator = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    return {"file_id": str(item.get("file_id") or ""), "file_name": _text(item.get("file_name")),
            "page": _integer(raw.get("page", locator.get("page"))),
            "row": _integer(raw.get("source_row", raw.get("row", locator.get("row")))),
            "date": on.isoformat() if on else None, "description": _text(raw.get("description") or raw.get("memo") or raw.get("payee")),
            "direction": direction, "amount": float(abs(signed)) if signed is not None else None,
            "currency": currency, "quote": _text(raw.get("quote") or locator.get("quote"), 320) or None}


def _target_keys(raw: dict, description: str, members: dict[str, str], accounts: dict[str, dict]) -> set[str]:
    keys = set()
    explicit = raw.get("counterparty_account_key") or raw.get("counterparty_account")
    if isinstance(explicit, str) and explicit in members:
        keys.add(members[explicit])
    digits = raw.get("counterparty_account_last4") or raw.get("transfer_to_last4") or raw.get("transfer_from_last4")
    suffixes = {str(digits)[-4:]} if digits and re.fullmatch(r"\d{4,}", str(digits)) else set()
    suffixes.update(match[-4:] for match in _TARGET.findall(description))
    for key, account in accounts.items():
        for member in account.get("members") or [key]:
            last4 = re.search(r"••(\d{4})$", str(member))
            if last4 and last4.group(1) in suffixes:
                keys.add(key)
    return keys


def _records(analyses: list[dict], accounts: dict[str, dict], members: dict[str, str]) -> tuple[list[dict], dict, list[dict]]:
    records, summaries, sources = [], [], []
    coverage = {"transaction_records": 0, "files_with_detail": [], "files_missing_detail": [],
                "ignored_summary_records": 0, "truncated": False, "superseded_detail_file_ids": []}
    for item in analyses:
        if "bank_statement" not in {item.get("classification"), *(item.get("classifications") or [])}:
            continue
        facts = item.get("key_facts") or {}
        if not isinstance(facts, dict):
            continue
        key = members.get(str(item.get("movement_account_key") or ""))
        if key not in accounts:
            continue
        file_id = str(item.get("file_id") or "")
        arrays = _arrays(facts)
        coverage["files_with_detail" if arrays else "files_missing_detail"].append(file_id)
        coverage["ignored_summary_records"] += sum(len(source.get(field) or []) for source in [facts, *(facts.get("months") or [])] if isinstance(source, dict) for field in ("recurring_debits", "recurring_credits"))
        summary_sources = facts.get("months") or [facts]
        for source in summary_sources:
            if not isinstance(source, dict):
                continue
            start, end = _period(source, item.get("period_hint") or facts.get("statement_period"))
            month = statement_period_month(source.get("month") or source.get("statement_period") or item.get("period_hint") or facts.get("statement_period")) or ""
            if re.fullmatch(r"\d{4}-\d{2}", month):
                summaries.append({"key": key, "period": month, "currency": _currency(source.get("currency") or facts.get("currency")),
                    "beginning": _money(source.get("beginning_balance")), "ending": _money(source.get("ending_balance")),
                    "file_id": file_id, "accepted": bool(item.get("accepted")),
                    "observed": facts.get("source") != "transactions_export", "start": start, "end": end,
                    "analyzed_at": str(item.get("analyzed_at") or "")})
        for source, field, values in arrays:
            start, end = _period(source, item.get("period_hint") or facts.get("statement_period"))
            sources.append({"item": item, "facts": facts, "source": source, "field": field, "values": values,
                            "key": key, "start": start, "end": end, "file_id": file_id,
                            "currency": _currency(source.get("currency") or facts.get("currency")),
                            "export": facts.get("source") == "transactions_export"})
    # A PDF and export for overlapping activity are alternate readings, not a
    # second set of cash flows. Preserve repeated rows within the chosen file.
    chosen = []
    def source_rank(value):
        locations = sum(isinstance(row, dict) and bool(row.get("page") or row.get("source_row") or row.get("row")) for row in value["values"])
        return (bool(value["item"].get("accepted")), bool(value["source"].get("transactions_complete", value["facts"].get("transactions_complete"))),
                value["field"] != "transfer_lines", bool(value["currency"]), bool(value["values"]) and locations == len(value["values"]),
                str(value["item"].get("analyzed_at") or ""), locations, value["file_id"])
    for source in sorted(sources, key=source_rank, reverse=True):
        overlap = next((other for other in chosen if source["key"] == other["key"]
                        and (not source["currency"] or not other["currency"] or source["currency"] == other["currency"])
                        and source["start"] and source["end"] and other["start"] and other["end"]
                        and source["start"] <= other["end"] and other["start"] <= source["end"]
                        and source["file_id"] != other["file_id"]), None)
        if overlap is not None:
            coverage["superseded_detail_file_ids"].append(source["file_id"])
            continue
        chosen.append(source)
    seen = {}
    for group in chosen:
        item, facts, source = group["item"], group["facts"], group["source"]
        for index, raw in enumerate(group["values"]):
            if len(records) >= MAX_RECORDS:
                coverage["truncated"] = True
                break
            if not isinstance(raw, dict):
                continue
            if any(field in raw for field in ("total_amount", "average_amount", "occurrences")):
                coverage["ignored_summary_records"] += 1
                continue
            signed, direction, reasons = _signed(raw)
            if signed == 0:
                continue
            on = _day(raw.get("posted_date") or raw.get("date") or raw.get("transaction_date"))
            currency = _currency(raw.get("currency")) or group["currency"]
            if on is None:
                reasons.append("date_unknown")
            if not group["start"] or not group["end"]:
                reasons.append("statement_period_unknown")
            elif group["start"] > group["end"]:
                reasons.append("contradictory_statement_period")
            elif group["start"] and group["end"] and not group["start"] <= on <= group["end"]:
                reasons.append("outside_statement_period")
            if currency is None:
                reasons.append("currency_unknown")
            if _currency(raw.get("currency")) and group["currency"] and _currency(raw["currency"]) != group["currency"]:
                reasons.append("contradictory_currency")
            evidence = _source(item, raw, signed, direction, on, currency)
            if not evidence["file_id"] or (evidence["page"] is None and evidence["row"] is None):
                reasons.append("source_location_missing")
            if not item.get("accepted"):
                reasons.append("source_not_accepted")
            raw_account = raw.get("account_last4")
            if facts.get("account_identity_ambiguous"):
                reasons.append("account_identity_ambiguous")
            if raw_account and not any(str(member).endswith("••" + str(raw_account)[-4:]) for member in accounts[group["key"]].get("members") or [group["key"]]):
                reasons.append("account_identity_conflict")
            reference = re.sub(r"[^a-z0-9]", "", _text(raw.get("transfer_reference") or raw.get("reference") or raw.get("trace_number"), 80).casefold())
            reference = reference if len(reference) >= 4 else ""
            description = evidence["description"]
            targets = _target_keys(raw, description, members, accounts)
            kind = _text(raw.get("kind") or raw.get("type") or raw.get("kind_guess")).replace("_", " ")
            holder = re.sub(r"[^a-z0-9]", "", _text(source.get("account_holder") or facts.get("account_holder")).casefold())
            identity = raw.get("transaction_id") or raw.get("bank_transaction_id")
            fingerprint = (group["key"], "transaction", str(identity)) if identity else (
                group["key"], str(item.get("content_hash") or group["file_id"]), str(group["start"]), index,
                str(on), str(signed), direction, currency, description,
            )
            if fingerprint in seen:
                prior = seen[fingerprint]
                if (prior["on"], prior["signed"], prior["currency"]) != (on, signed, currency):
                    prior["reasons"].append("conflicting_transaction_identity")
                elif evidence not in prior["evidence"] and len(prior["evidence"]) < 4:
                    prior["evidence"].append(evidence)
                continue
            record = {"id": _id(*fingerprint), "key": group["key"], "on": on, "period": on.strftime("%Y-%m") if on else (group["start"].strftime("%Y-%m") if group["start"] else "unknown"),
                "signed": signed, "direction": direction, "currency": currency, "reference": reference, "targets": targets,
                "holder": holder, "transfer": bool(_TRANSFER.search(description) or targets or _TRANSFER.search(kind)),
                "reasons": reasons, "evidence": [evidence],
                "complete": bool(source.get("transactions_complete", facts.get("transactions_complete"))) and not bool(source.get("transactions_truncated", facts.get("transactions_truncated"))) and group["field"] != "transfer_lines",
                "coverage_start": group["start"], "coverage_end": group["end"],
            }
            records.append(record)
            seen[fingerprint] = record
    coverage["transaction_records"] = len(records)
    for key in ("files_with_detail", "files_missing_detail", "superseded_detail_file_ids"):
        coverage[key] = sorted(set(coverage[key]))
    coverage["truncated"] = coverage["truncated"] or any(bool(item.get("key_facts", {}).get("transactions_truncated")) for item in analyses)
    return records, coverage, summaries


def _movement(out: dict | None, incoming: dict | None, reasons: list[str], status: str) -> dict:
    own = out or incoming
    assert own is not None
    evidence = [source for record in (out, incoming) if record is not None for source in record["evidence"]]
    periods = sorted({record["period"] for record in (out, incoming) if record is not None})
    return {"id": _id(out["id"] if out else "", incoming["id"] if incoming else ""), "period": own["period"], "periods": periods,
            "from_account": out["key"] if out and out["direction"] == "out" else None, "to_account": incoming["key"] if incoming and incoming["direction"] == "in" else None,
            "account_key": own["key"], "direction": own["direction"],
            "amount": float(abs(own["signed"])) if own["signed"] is not None else None, "currency": own["currency"],
            "out_on": out["on"].isoformat() if out and out["on"] and out["direction"] == "out" else None,
            "in_on": incoming["on"].isoformat() if incoming and incoming["on"] else None,
            "lag_days": (incoming["on"] - out["on"]).days if out and incoming and out["on"] and incoming["on"] else None,
            "status": status, "reason_codes": sorted(set(reasons)), "evidence": evidence[:8]}


def build_account_movements(analyses: list[dict], accounts: list[dict], *, limit: int = 50, offset: int = 0) -> dict:
    """A bounded graph and evidence preview; no IO or underwriting mutations."""
    index = {str(account["key"]): account for account in accounts}
    members = {str(member): key for key, account in index.items() for member in [key, *(account.get("members") or [])]}
    records, coverage, summaries = _records(analyses, index, members)
    candidates = defaultdict(list)
    counterpart_issues = defaultdict(set)
    inbound = defaultdict(list)
    invalid = {"contradictory_direction", "contradictory_amount", "contradictory_statement_period", "outside_statement_period", "contradictory_currency", "account_identity_conflict", "account_identity_ambiguous", "conflicting_transaction_identity"}
    for record in records:
        if record["direction"] == "in" and record["on"] and record["signed"] is not None and not invalid.intersection(record["reasons"]):
            inbound[(abs(record["signed"]), record["on"])].append(record)
    for out in records:
        if out["direction"] != "out" or not out["on"] or out["signed"] is None or invalid.intersection(out["reasons"]):
            continue
        for delta in range(-PAIR_DAYS, PAIR_DAYS + 1):
            for incoming in inbound.get((abs(out["signed"]), out["on"] + timedelta(days=delta)), []):
                if out["key"] == incoming["key"] or not (out["transfer"] or incoming["transfer"]):
                    continue
                if out["targets"] and incoming["key"] not in out["targets"] or incoming["targets"] and out["key"] not in incoming["targets"]:
                    continue
                if out["currency"] and incoming["currency"] and out["currency"] != incoming["currency"]:
                    counterpart_issues[out["id"]].add("counterpart_currency_mismatch")
                    counterpart_issues[incoming["id"]].add("counterpart_currency_mismatch")
                    continue
                # Only uniqueness matters: two candidates are already ambiguous.
                if len(candidates[out["id"]]) < 6:
                    candidates[out["id"]].append(incoming)
                if len(candidates[incoming["id"]]) < 6:
                    candidates[incoming["id"]].append(out)
    movements, used = [], set()
    internal = defaultdict(lambda: {"in": Decimal(0), "out": Decimal(0)})
    for out in records:
        peers = candidates[out["id"]]
        if out["direction"] != "out" or len(peers) != 1 or len(candidates[peers[0]["id"]]) != 1:
            continue
        incoming = peers[0]
        reasons = [*out["reasons"], *incoming["reasons"]]
        target_proof = len(out["targets"]) == 1 and incoming["key"] in out["targets"] or len(incoming["targets"]) == 1 and out["key"] in incoming["targets"]
        reference_proof = bool(out["reference"] and out["reference"] == incoming["reference"] and out["holder"] and out["holder"] == incoming["holder"])
        if not target_proof and not reference_proof:
            reasons.append("counterparty_unconfirmed")
        if out["holder"] and incoming["holder"] and out["holder"] != incoming["holder"]:
            reasons.append("account_holder_conflict")
        status = "supported" if not reasons else "review"
        movements.append(_movement(out, incoming, reasons, status))
        used.update((out["id"], incoming["id"]))
        if status == "supported":
            internal[(out["key"], out["period"], out["currency"])]["out"] += abs(out["signed"])
            internal[(incoming["key"], incoming["period"], incoming["currency"])]["in"] += abs(incoming["signed"])
    for record in records:
        if record["id"] in used or not record["transfer"]:
            continue
        reasons = [*record["reasons"], *counterpart_issues[record["id"]]]
        if len(candidates[record["id"]]) > 1:
            reasons.append("ambiguous_repeated_amount")
        elif candidates[record["id"]]:
            reasons.append("ambiguous_counterpart")
        else:
            reasons.append("counterpart_not_observed")
        if record["key"] in record["targets"]:
            reasons.append("same_account_alias")
        movements.append(_movement(record if record["direction"] != "in" else None,
                                   record if record["direction"] == "in" else None, reasons,
                                   "review" if record["reasons"] or counterpart_issues[record["id"]] or candidates[record["id"]] or "same_account_alias" in reasons else "unmatched"))
    movements.sort(key=lambda value: (value["out_on"] or value["in_on"] or "", value["id"]), reverse=True)
    observed = defaultdict(list)
    for record in records:
        observed[(record["key"], record["period"], record["currency"])].append(record)
    # The cockpit already selects and deduplicates the statement's own account
    # month (including consolidated PDFs). Reuse those balance values rather
    # than accidentally reading a savings-account subsummary as the checking.
    for key, account in index.items():
        for month in account.get("series") or []:
            period = str(month.get("month") or "")
            if not re.fullmatch(r"\d{4}-\d{2}", period):
                continue
            file_ids = {str(value) for value in month.get("file_ids") or []}
            backing = [summary for summary in summaries if summary["key"] == key and summary["period"] == period and summary["file_id"] in file_ids]
            if not backing:
                continue
            currencies = {summary["currency"] for summary in backing}
            chosen = backing[0]
            # All raw variants for this canonical account-month are replaced;
            # a conflicting currency stays unknown rather than duplicated.
            summaries = [summary for summary in summaries if not (summary["key"] == key and summary["period"] == period)]
            summaries.append({**chosen, "canonical": True,
                "currency": next(iter(currencies)) if len(currencies) == 1 else None,
                "beginning": _money(month.get("beginning_balance")), "ending": _money(month.get("ending_balance")),
                "accepted": bool(month.get("accepted")), "observed": all(summary["observed"] for summary in backing)})
    summary_by = {}
    for summary in sorted(summaries, key=lambda value: (value.get("canonical", False), value["observed"], value["accepted"], value["analyzed_at"]), reverse=True):
        summary_by.setdefault((summary["key"], summary["period"], summary["currency"]), summary)
    nodes = []
    for key, account in sorted(index.items()):
        groups = sorted({group for group in (*observed, *summary_by) if group[0] == key}, key=lambda group: (group[1], group[2] or ""))
        periods = []
        for group in groups:
            listed, summary = observed[group], summary_by.get(group)
            valid = [record for record in listed if record["signed"] is not None and record["direction"] and not invalid.intersection(record["reasons"])]
            first = _day(group[1] + "-01")
            last = date(first.year, first.month, monthrange(first.year, first.month)[1]) if first else None
            complete = bool(valid) and len(valid) == len(listed) and all(record["complete"] and not record["reasons"]
                and record["coverage_start"] and record["coverage_end"] and first and last
                and record["coverage_start"] <= first and record["coverage_end"] >= last for record in listed) and not coverage["truncated"]
            money_in = sum((record["signed"] for record in valid if record["direction"] == "in"), Decimal(0)) if valid else None
            money_out = sum((-record["signed"] for record in valid if record["direction"] == "out"), Decimal(0)) if valid else None
            net = money_in - money_out if valid else None
            beginning, ending = (summary["beginning"], summary["ending"]) if summary else (None, None)
            if summary and not summary["observed"]:
                beginning = ending = None
            change = ending - beginning if beginning is not None and ending is not None and summary["observed"] else None
            reasons = {reason for record in listed for reason in record["reasons"]}
            if not listed:
                reasons.add("transaction_detail_missing")
            elif not complete:
                reasons.add("partial_transaction_coverage")
            if group[2] is None:
                reasons.add("currency_unknown")
            if summary and not summary["observed"]:
                reasons.add("export_balance_is_computed")
            bridge_period_matches = summary is not None and summary["start"] == first and summary["end"] == last
            if summary and not bridge_period_matches:
                reasons.add("balance_period_differs")
            files = {source["file_id"] for record in listed for source in record["evidence"]}
            if summary:
                files.add(summary["file_id"])
            inside = internal[group]
            values = {"observed_in": money_in, "observed_out": money_out, "observed_net": net,
                "internal_in": inside["in"] if valid else None, "internal_out": inside["out"] if valid else None,
                "internal_net": inside["in"] - inside["out"] if valid else None,
                "beginning_balance": beginning, "ending_balance": ending, "observed_balance_change": change,
                "explained_change": net if valid and bridge_period_matches else None,
                "unexplained_change": change - net if complete and bridge_period_matches and change is not None and group[2] is not None else None}
            periods.append({"period": group[1], "currency": group[2], **{name: float(value) if value is not None else None for name, value in values.items()},
                            "coverage_status": "complete" if complete else "partial" if listed else "missing",
                            "balance_period_start": summary["start"].isoformat() if summary and summary["start"] else None,
                            "balance_period_end": summary["end"].isoformat() if summary and summary["end"] else None,
                            "source_file_ids": sorted(files), "reason_codes": sorted(reasons)})
        nodes.append({"key": key, "label": account.get("label") or key, "counted": bool(account.get("counted", True)),
                      "currencies": sorted({value["currency"] for value in periods if value["currency"] is not None}) + ([None] if any(value["currency"] is None for value in periods) else []), "periods": periods[-60:]})
    links = {}
    for movement in movements:
        if movement["from_account"] is None or movement["to_account"] is None:
            continue
        for period in movement["periods"]:
            group = (movement["from_account"], movement["to_account"], period, movement["currency"], movement["status"])
            link = links.setdefault(group, {"id": _id(*group), "from_account": group[0], "to_account": group[1], "period": group[2], "currency": group[3],
                                           "periods": [period], "status": group[4], "amount": 0.0, "transaction_count": 0, "sample_movements": []})
            link["amount"] = round(link["amount"] + (movement["amount"] or 0), 2)
            link["transaction_count"] += 1
            if len(link["sample_movements"]) < 3:
                link["sample_movements"].append(movement)
    limit, offset = max(1, min(int(limit), MAX_PAGE_SIZE)), max(0, int(offset))
    total = len(movements)
    coverage["truncated"] = coverage["truncated"] or len(nodes) > MAX_NODES or len(links) > MAX_LINKS
    coverage["details_truncated"] = offset > 0 or offset + limit < total
    visible_keys = {node["key"] for node in nodes[:MAX_NODES]}
    visible_links = [link for link in links.values() if link["from_account"] in visible_keys and link["to_account"] in visible_keys]
    notes = ["Account movements are an evidence review aid. They do not change revenue, debt service, DSCR or bank balances.",
             "Recurring-payment aggregates and monthly totals are never matched as individual transfers."]
    if coverage["files_missing_detail"]:
        notes.append("Some statements contain summary readings only. Dated transaction lines are needed to trace their movements.")
    if coverage["superseded_detail_file_ids"]:
        notes.append("Overlapping or revised statement/export details were not added together; the strongest current reading is used.")
    partial = (coverage["files_missing_detail"] or coverage["truncated"]
               or any(value["status"] != "supported" for value in movements)
               or any(period["coverage_status"] != "complete" for node in nodes for period in node["periods"]))
    return {"schema_version": SCHEMA_VERSION, "status": "no_transaction_evidence" if not records else "partial" if partial else "available",
            "periods": sorted({period["period"] for node in nodes for period in node["periods"]}), "nodes": nodes[:MAX_NODES],
            "links": visible_links[:MAX_LINKS], "movements": movements[offset:offset + limit], "coverage": coverage,
            "pagination": {"offset": offset, "limit": limit, "total": total, "next_offset": offset + limit if offset + limit < total else None}, "notes": notes}
