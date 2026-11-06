"""Pure, bounded extraction of printed Wells Fargo business-checking transfers.

This module performs no IO except reading supplied PDF bytes in memory. Summary
figures are audit evidence only; no transaction is inferred from a summary.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation

PARSER_VERSION = "wf_optimize_transfers.v1"
MAX_PAGES = 200
MAX_PAGE_CHARS = 200_000
MAX_TRANSFERS = 1000
_MONTHS = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
_PERIOD = re.compile(rf"\b({_MONTH})\s*(\d{{1,2}})\s*,?\s*(\d{{4}})\s*[-–—]\s*({_MONTH})\s*(\d{{1,2}})\s*,?\s*(\d{{4}})", re.I)
_TRANSFER = re.compile(r"\bOnline\s+Transfer\s+(From|To)\b", re.I)
_DATE_ROW = re.compile(r"^\s*(\d{1,2}/\d{1,2})(?:[ \t]+(\d{1,2}/\d{1,2}))?(?:[ \t]+(.*))?\s*$")
_AMOUNT = re.compile(r"^\s*([-+−]?\$?\(?\d[\d,]*\.\d{2}\)?)(?:[ \t]+(.*))?\s*$")
_REF = re.compile(r"\bRef\s*#\s*([A-Za-z0-9][A-Za-z0-9-]{1,79})", re.I)
_MASKED = re.compile(r"(?:[xX*•]{2,})\s*(\d{4})\b")
_ACCOUNT = re.compile(r"^[ \t]*Account[ \t]*number[ \t]*:[ \t]*([\d][\d -]{3,29})", re.I | re.M)
_DOLLAR = re.compile(r"[-+−]?\$\s*\(?\d[\d,]*\.\d{2}\)?")
_MONEY_TEXT = re.compile(r"(?<![\d.])[-+−]?\$?\(?\d[\d,]*\.\d{2}\)?(?!\d)")


def _number(value):
    try:
        return Decimal(value.replace("$", "").replace(",", "").replace(" ", "").replace("−", "-").replace("(", "-").replace(")", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _clean(value, limit):
    """Mask account-like digit runs while retaining source references/amounts."""
    held = []

    def hold(match):
        held.append(match[0])
        return "PROTECTEDTOKEN" + chr(65 + len(held) - 1) + "END"

    value = _REF.sub(hold, value)
    value = _MONEY_TEXT.sub(hold, value)
    value = _MASKED.sub(lambda match: "••" + match[1], value)
    value = re.sub(r"\b\d[\d -]{4,}\d\b", lambda match: "••" + re.sub(r"\D", "", match[0])[-4:] if len(re.sub(r"\D", "", match[0])) >= 6 else match[0], value)
    for index, original in enumerate(held):
        value = value.replace("PROTECTEDTOKEN" + chr(65 + index) + "END", original)
    return " ".join(value.split())[:limit]


def _section(line):
    text = " ".join(line.casefold().split()).rstrip(":")
    if text in {"credits", "electronic deposits/bank credits", "electronic deposits / bank credits", "deposits/credits", "deposits and credits"}:
        return "in"
    if text in {"debits", "electronic debits/bank debits", "electronic debits / bank debits", "other debits", "checks paid", "checks", "service charges"}:
        return "out"
    return None


def _date_in_period(mmdd, start, end):
    month, day = (int(part) for part in mmdd.split("/"))
    dates = []
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if start <= candidate <= end:
            dates.append(candidate)
    return dates[0] if len(dates) == 1 else None


def _summary_audit(pages):
    found = []
    for page, text in enumerate(pages, 1):
        match = re.search(r"Beginning\s+balance\s+Total\s+credits\s+Total\s+debits\s+Ending\s+balance", text, re.I)
        if not match:
            continue
        tail = text[match.end():match.end() + 1000]
        tail = re.split(r"(?m)^\s*(?:Credits|Debits|Electronic deposits/bank credits)\s*$", tail, maxsplit=1)[0]
        amounts = [_number(match[0]) for match in _DOLLAR.finditer(tail)]
        if len(amounts) != 4 or any(value is None for value in amounts):
            found.append({"page": page, "valid": False, "reason": "summary_four_values_not_found", "value_count": len(amounts)})
            continue
        beginning, credits, debits, ending = amounts
        difference = beginning + credits - abs(debits) - ending
        valid = credits >= 0 and abs(difference) <= Decimal("0.01")
        found.append({"page": page, "valid": valid, "reason": None if valid else "summary_reconciliation_mismatch",
                      "beginning_balance": float(beginning), "credits": float(credits), "debits": float(debits), "ending_balance": float(ending),
                      "reconciliation_difference": float(difference)})
    return found


def parse_wells_fargo_pages(pages: list[str], *, source_hash: str | None = None) -> dict:
    """Parse supplied physical-page text; incomplete evidence stays explicit."""
    if not isinstance(pages, list) or any(not isinstance(page, str) for page in pages):
        raise TypeError("physical_page_text_list_required")
    errors = []

    def error(code, page=None, row=None):
        item = {"code": code}
        if page is not None:
            item["page"] = page
        if row is not None:
            item["row"] = row
        if item not in errors:
            errors.append(item)

    bounded = pages[:MAX_PAGES]
    if len(pages) > MAX_PAGES or any(len(page) > MAX_PAGE_CHARS for page in bounded):
        error("source_limit_exceeded")
    bounded = [page[:MAX_PAGE_CHARS] for page in bounded]
    full = "\n".join(bounded)
    product = bool(re.search(r"Optimize\s+Business\s+Checking", full, re.I))
    bank = bool(re.search(r"Wells\s*Fargo|wellsfargo\.com", full, re.I))
    recognized = product and bank
    if not recognized:
        error("unrecognized_statement")
    accounts = {re.sub(r"\D", "", match[1])[-4:] for match in _ACCOUNT.finditer(full)}
    account = next(iter(accounts)) if len(accounts) == 1 else None
    if account is None:
        error("account_header_missing" if not accounts else "account_header_conflict")
    periods = set()
    for match in _PERIOD.finditer(full):
        try:
            periods.add((date(int(match[3]), _MONTHS[match[1][:3].casefold()], int(match[2])), date(int(match[6]), _MONTHS[match[4][:3].casefold()], int(match[5]))))
        except ValueError:
            error("invalid_statement_period")
    start, end = next(iter(periods)) if len(periods) == 1 else (None, None)
    if start is None or end is None or start > end or (end - start).days > 366:
        error("statement_period_missing_or_conflicting")
        start = end = None
    audit = _summary_audit(bounded)
    valid_audits = [row for row in audit if row["valid"]]
    stated_currencies = set(re.findall(r"\b(?:currency|all amounts(?: are)?(?: in)?)\s*[:=]?\s*(USD|CAD|EUR|GBP|AUD)\b", full, re.I))
    if any(code.upper() != "USD" for code in stated_currencies):
        error("statement_currency_conflict")
    currency = "USD" if recognized and valid_audits and not any(code.upper() != "USD" for code in stated_currencies) else None
    currency_basis = "inferred_us_statement_context" if currency else None
    candidates = sum(len(list(_TRANSFER.finditer(page))) for page in bounded)
    transfers, matched_candidates, section, seen_rows = [], 0, None, set()
    for page_number, page in enumerate(bounded, 1):
        lines = page.splitlines()
        anchors = [index for index, line in enumerate(lines) if _DATE_ROW.fullmatch(line)]
        consumed = set()
        for anchor_index, begin in enumerate(anchors):
            if begin in consumed:
                continue
            stop = anchors[anchor_index + 1] if anchor_index + 1 < len(anchors) else len(lines)
            first = _DATE_ROW.fullmatch(lines[begin])
            posted = first[2] or first[1]
            remainder = first[3] or ""
            amount_index = begin + 1
            # Text extraction can put effective and posted dates on two lines.
            if not remainder and stop == begin + 1 and stop < len(lines):
                second = _DATE_ROW.fullmatch(lines[stop])
                if second and not second[2]:
                    consumed.add(stop)
                    posted, remainder, amount_index = second[1], second[3] or "", stop + 1
                    stop = anchors[anchor_index + 2] if anchor_index + 2 < len(anchors) else len(lines)
            row_lines = ([remainder] if remainder else []) + lines[amount_index:stop]
            while row_lines and not row_lines[0].strip():
                row_lines.pop(0)
            trimmed = []
            for line in row_lines:
                if _section(line) or re.match(r"\s*(?:Page\s*\d|Account\s*number\s*:|Optimize\s+Business|Sheet\s+Seq|©|Customer\s+service)", line, re.I) or _PERIOD.search(line):
                    break
                trimmed.append(line)
            row_text = "\n".join(trimmed).strip()
            occurrences = list(_TRANSFER.finditer(row_text))
            if not occurrences:
                continue
            for line in lines[:begin]:
                section = _section(line) or section
            if len(occurrences) != 1:
                error("multiple_transfers_in_row", page_number, begin + 1)
                continue
            amount = _AMOUNT.fullmatch(trimmed[0]) if trimmed else None
            if not amount:
                error("transfer_amount_missing", page_number, begin + 1)
                continue
            number = _number(amount[1])
            direction = "in" if occurrences[0][1].casefold() == "from" else "out"
            refs = list(dict.fromkeys(match[1] for match in _REF.finditer(row_text)))
            day = _date_in_period(posted, start, end) if start and end else None
            reasons = []
            if number is None or number == 0:
                reasons.append("transfer_amount_invalid")
            if number is not None and number < 0 and direction == "in":
                reasons.append("transfer_sign_conflict")
            if section and direction != section:
                reasons.append("transfer_section_conflict")
            if day is None:
                reasons.append("transfer_posted_date_outside_or_unknown_period")
            if len(refs) != 1:
                reasons.append("transfer_reference_missing_or_ambiguous")
            for reason in reasons:
                error(reason, page_number, begin + 1)
            if reasons:
                continue
            if len(transfers) >= MAX_TRANSFERS:
                error("transfer_limit_exceeded")
                continue
            physical_key = (page_number, begin)
            if physical_key in seen_rows:
                continue
            seen_rows.add(physical_key)
            suffixes = list(dict.fromkeys(match[1] for match in _MASKED.finditer(row_text)))
            if len(suffixes) > 1:
                error("counterparty_suffix_ambiguous", page_number, begin + 1)
            detail = row_text[occurrences[0].start():]
            transfers.append({"date": day.isoformat(), "posted_date": day.isoformat(), "amount": float(abs(number)), "direction": direction,
                              "description": _clean(detail, 240), "counterparty_account_last4": suffixes[0] if len(suffixes) == 1 else None,
                              "transfer_reference": refs[0], "page": page_number, "source_text_line": begin + 1,
                              "quote": _clean(f"{posted} {row_text}", 320), "currency": currency, "currency_basis": currency_basis})
            matched_candidates += 1
        # Preserve a known section when it continues onto the next physical page.
        for line in lines:
            section = _section(line) or section
    if matched_candidates != candidates:
        error("unparsed_transfer_candidates")
    return {"parser_version": PARSER_VERSION, "source_hash": source_hash, "recognized": recognized,
            "account_last4": account, "period_start": start.isoformat() if start else None, "period_end": end.isoformat() if end else None,
            "currency": currency, "currency_basis": currency_basis, "transfer_lines": transfers, "summary_audit": audit,
            "coverage": {"physical_pages": len(pages), "pages_read": len(bounded), "candidate_count": candidates, "parsed_count": len(transfers),
                         "complete": recognized and account is not None and start is not None and not errors,
                         "source_page_range": [1, len(bounded)] if bounded else [], "errors": errors}}


def parse_wells_fargo_pdf(raw: bytes) -> dict:
    """Extract from immutable bytes in memory, with no storage/provider calls."""
    import fitz

    with fitz.open(stream=raw, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("password_protected_statement")
        if len(document) > MAX_PAGES:
            raise ValueError("statement_page_limit_exceeded")
        pages = [page.get_text("text") for page in document]
    return parse_wells_fargo_pages(pages, source_hash=hashlib.sha256(raw).hexdigest())
