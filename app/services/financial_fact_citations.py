"""Bounded, source-exact financial citations, separate from fact acceptance.

Quote validation proves where a number was printed, not that an underwriting
decision is correct. Missing or invalid citations never delete source numbers
or reject a readable document. This module performs no I/O or provider calls.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

VALIDATION_VERSION = "financial_citations_v1"
MAX_CITATIONS_PER_FIELD = 4
MAX_QUOTE_CHARS = 320
MAX_STATEMENTS = 60
FIELD_ALIASES = {
    "revenue": ("gross_revenue", "revenue", "total_revenue", "gross_receipts", "gross_receipts_current_year", "gross_receipts_or_sales"),
    "cogs": ("cost_of_goods_sold", "cogs"),
    "gross_profit": ("gross_profit",),
    "gross_income": ("gross_income",),
    "net_income": ("net_income", "net_profit"),
    "net_income_per_books": ("net_income_per_books", "net_income_per_books_current"),
    "ordinary_business_income": ("ordinary_business_income", "ordinary_business_income_loss", "ordinary_business_income_loss_current"),
    "taxable_income": ("taxable_income",),
    "total_income": ("total_income", "total_income_current", "total_income_line6"),
    "operating_income": ("operating_income",),
    "operating_expenses": ("total_operating_expenses", "operating_expenses"),
    "ebitda": ("ebitda",),
    "interest": ("interest",),
    "income_taxes": ("income_taxes",),
    "depreciation_and_amortization": ("depreciation_and_amortization",),
    "other_income": ("other_income",),
    "total_deposits_and_credits": ("total_deposits_and_credits",),
    "total_withdrawals_and_debits": ("total_withdrawals_and_debits",),
    "average_daily_balance": ("average_daily_balance", "average_ledger_balance"),
    "beginning_balance": ("beginning_balance",),
    "ending_balance": ("ending_balance",),
    "total_monthly_debt_service": ("total_monthly_debt_service",),
}
_NUMERIC_TOKEN = re.compile(
    r"(?<![\w.])\(?\s*\$?\s*[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?\s*\)?(?![\w.])"
)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace("$", "").replace(",", "")
    if len(raw) > 128:
        return None
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1].strip()
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and (number == 0 or -18 <= number.adjusted() < 16) else None


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    return value.strip() if isinstance(value, str) else ""


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _fact_value(facts: dict, field: str) -> Decimal | None:
    for alias in FIELD_ALIASES.get(field, (field,)):
        raw = facts.get(alias)
        if isinstance(raw, dict) and raw.get("measure") and raw["measure"] != field:
            continue
        value = _decimal(raw)
        if value is not None:
            return value
    return None


def _fact_context(facts: dict) -> dict[str, str | None]:
    def first(*keys: str) -> str | None:
        return next((_text(facts.get(key)) for key in keys if _text(facts.get(key))), None)
    return {
        "basis": first("basis", "accounting_basis"),
        "period_start": first("period_start"),
        "period_end": first("period_end"),
        "currency": first("currency"),
    }


def _source_statements(analysis: Any) -> list[tuple[int | None, dict]]:
    data = getattr(analysis, "analysis", None)
    facts = data.get("key_facts") if isinstance(data, dict) else None
    if not isinstance(facts, dict):
        return []
    statements = facts.get("income_statements")
    if isinstance(statements, list) and statements:
        return [(index, row) for index, row in enumerate(statements[:MAX_STATEMENTS]) if isinstance(row, dict)]
    return [(None, facts)]


def _candidate_map(data: dict, facts: dict, index: int | None) -> dict:
    candidates = facts.get("fact_citations")
    if isinstance(candidates, dict):
        return candidates
    candidates = data.get("fact_citations") if index is None else None
    return candidates if isinstance(candidates, dict) else {}


def _invalid(reason: str) -> dict[str, str]:
    return {"status": "unverified", "reason": reason}


def _validate_candidate(candidate: Any, *, field: str, value: Decimal, facts: dict,
                        source_pages: dict[int, str], source: dict, index: int | None) -> tuple[dict | None, str]:
    if not isinstance(candidate, dict):
        return None, "citation_invalid"
    page = candidate.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 10000:
        return None, "page_invalid"
    page_text = source_pages.get(page)
    if not isinstance(page_text, str) or not page_text.strip():
        return None, "page_not_searchable"
    label = _text(candidate.get("line_label") or candidate.get("label"))
    quote = _text(candidate.get("quote"))
    if not label or len(label) > 160 or not quote or len(quote) > MAX_QUOTE_CHARS:
        return None, "label_or_quote_invalid"
    normalized_quote, normalized_label = _normalized(quote), _normalized(label)
    if normalized_quote not in _normalized(page_text):
        return None, "quote_not_found_on_page"
    if normalized_label not in normalized_quote:
        return None, "label_not_in_quote"
    if _decimal(candidate.get("value")) != value:
        return None, "extracted_value_mismatch"
    if value not in {_decimal(match.group(0)) for match in _NUMERIC_TOKEN.finditer(quote)}:
        return None, "value_not_in_quote"
    measure = _text(candidate.get("measure"))
    if measure != field:
        return None, "measure_mismatch"
    # Numeric equivalence does not turn an ordinary/taxable-income line into
    # book net income. Reject the false attribution but retain its raw number.
    if field == "net_income" and any(token in normalized_label for token in ("ordinary", "taxable", "gross profit", "operating income", "deposit", "per books")):
        return None, "measure_label_mismatch"
    if field == "revenue" and any(token in normalized_label for token in ("deposit", "balance", "net income", "gross profit")):
        return None, "measure_label_mismatch"
    context = _fact_context(facts)
    for key, expected in context.items():
        submitted = _text(candidate.get(key))
        if submitted and expected and _normalized(submitted) != _normalized(expected):
            return None, "statement_context_mismatch"
    tax_form, tax_line = _text(candidate.get("tax_form")), _text(candidate.get("tax_line"))
    if len(tax_form) > 80 or len(tax_line) > 40:
        return None, "tax_reference_invalid"
    if tax_form:
        normalized_form = re.sub(r"[^a-z0-9]", "", _normalized(tax_form))
        prefix = "schedule" if normalized_form.startswith("schedule") else "form"
        form_id = normalized_form.removeprefix(prefix)
        form_pattern = rf"\b{prefix}\s*" + r"[\s\-]*".join(re.escape(character) for character in form_id) + r"(?![a-z0-9])"
        if not form_id or not re.search(form_pattern, _normalized(page_text)):
            return None, "tax_form_not_found_on_page"
    if tax_line:
        # A tax line must actually be printed immediately before its label.
        # A bare 21 elsewhere in a date or dollar figure proves nothing.
        prefix = rf"(?<![\w])(?:line\s+)?{re.escape(_normalized(tax_line))}\s+[.:\-]?\s*{re.escape(normalized_label)}"
        if not tax_form or not re.search(prefix, normalized_quote):
            return None, "tax_line_not_found_with_label"
    return {
        **source, "field": field, "measure": measure, "statement_index": index,
        "page": page, "line_label": label, "label": label, "quote": quote,
        "value": str(value), "verified": True, **context,
        "tax_form": tax_form or None, "tax_line": tax_line or None,
    }, "verified"


def build_financial_citation_manifest(analysis: Any, *, source_pages: dict[int, str] | None = None,
                                      source_kind: str = "legacy", source_hash: str | None = None) -> dict[str, Any]:
    """Validate new model references, or mark legacy facts without any I/O.

    For explicit analysis, source_pages must come from the already-fetched
    exact PDF bytes and source_hash must be their SHA256. Legacy/cache calls
    never upgrade unverified model references just by trusting their text.
    """
    data = getattr(analysis, "analysis", None)
    data = data if isinstance(data, dict) else {}
    source = {
        "file_id": str(getattr(analysis, "bucket_file_id", "") or ""),
        "analysis_id": str(getattr(analysis, "id", "") or ""),
        "content_hash": str(getattr(analysis, "content_hash", "") or ""),
    }
    source_valid = bool(source["file_id"] and source["analysis_id"] and re.fullmatch(r"[a-fA-F0-9]{64}", source["content_hash"]))
    exact_bytes = source_valid and source_hash == source["content_hash"]
    records = []
    for index, facts in _source_statements(analysis):
        values = {field: value for field in FIELD_ALIASES if (value := _fact_value(facts, field)) is not None}
        citations, statuses = {}, {}
        candidates = _candidate_map(data, facts, index)
        for field, value in values.items():
            if not source_valid:
                statuses[field] = _invalid("source_identity_unavailable")
                continue
            if source_kind == "legacy":
                statuses[field] = _invalid("legacy_analysis_requires_explicit_reanalysis")
                continue
            if not exact_bytes:
                statuses[field] = _invalid("source_hash_mismatch")
                continue
            if not source_pages:
                statuses[field] = _invalid("source_not_searchable" if source_kind == "pdf_scanned" else "source_citation_unavailable")
                continue
            raw_candidates = candidates.get(field)
            if isinstance(raw_candidates, dict):
                raw_candidates = [raw_candidates]
            if not isinstance(raw_candidates, list) or not raw_candidates:
                statuses[field] = _invalid("citation_not_provided")
                continue
            valid, reason = [], "citation_invalid"
            for candidate in raw_candidates[:MAX_CITATIONS_PER_FIELD]:
                citation, reason = _validate_candidate(candidate, field=field, value=value, facts=facts,
                                                       source_pages=source_pages, source=source, index=index)
                if citation is not None:
                    valid.append(citation)
            if valid:
                citations[field] = valid
                statuses[field] = {"status": "verified", "reason": "exact_searchable_source_quote"}
            else:
                statuses[field] = _invalid(reason)
        records.append({"statement_index": index, "fact_citations": citations,
                        "fact_citation_status": statuses, "fact_values": {key: str(value) for key, value in values.items()}})
    manifest = {
        "validation_version": VALIDATION_VERSION,
        "source_file_id": source["file_id"], "source_analysis_id": source["analysis_id"],
        "content_hash": source["content_hash"], "source_kind": source_kind,
        "statements": records,
    }
    manifest["validation_hash"] = citation_manifest_hash(manifest)
    return manifest


def ensure_legacy_financial_citation_manifest(analysis: Any) -> None:
    """Add unverified lineage to old cache rows; never fetch or invoke AI."""
    data = getattr(analysis, "analysis", None)
    if not isinstance(data, dict) or isinstance(data.get("financial_fact_citations"), dict):
        return
    if _source_statements(analysis):
        analysis.analysis = {**data, "financial_fact_citations": build_financial_citation_manifest(analysis)}


def _record_for_analysis(analysis: Any, index: int | None) -> tuple[dict | None, dict | None, str]:
    if getattr(analysis, "status", None) != "completed":
        return None, None, "source_analysis_unavailable"
    data = getattr(analysis, "analysis", None)
    manifest = data.get("financial_fact_citations") if isinstance(data, dict) else None
    if not isinstance(manifest, dict) or manifest.get("validation_version") != VALIDATION_VERSION:
        return None, None, "legacy_analysis_requires_explicit_reanalysis"
    if manifest.get("validation_hash") != citation_manifest_hash(manifest):
        return None, None, "citation_manifest_changed"
    if manifest.get("source_file_id") != str(getattr(analysis, "bucket_file_id", "")) or manifest.get("source_analysis_id") != str(getattr(analysis, "id", "")) or manifest.get("content_hash") != getattr(analysis, "content_hash", None):
        return None, None, "source_hash_or_identity_mismatch"
    facts = next((facts for statement_index, facts in _source_statements(analysis) if statement_index == index), None)
    records = manifest.get("statements")
    record = next((row for row in records if isinstance(row, dict) and row.get("statement_index") == index), None) if isinstance(records, list) else None
    return record, facts, "statement_citation_unavailable" if record is None or facts is None else ""


def validated_fact_citations_for_analysis(analysis: Any, *, statement_index: int | None = None) -> dict[str, list[dict]]:
    """Read server-validated citations only while their exact fact is current."""
    record, facts, reason = _record_for_analysis(analysis, statement_index)
    if reason:
        return {}
    result = {}
    citations = record.get("fact_citations")
    if not isinstance(citations, dict):
        return {}
    for field, raw in citations.items():
        expected = _fact_value(facts, field)
        if expected is None or not isinstance(raw, list) or record.get("fact_values", {}).get(field) != str(expected):
            continue
        context = _fact_context(facts)
        valid = [dict(citation) for citation in raw[:MAX_CITATIONS_PER_FIELD] if isinstance(citation, dict)
                 and citation.get("verified") is True and citation.get("field") == field
                 and citation.get("file_id") == str(analysis.bucket_file_id)
                 and citation.get("analysis_id") == str(analysis.id)
                 and citation.get("content_hash") == analysis.content_hash
                 and citation.get("statement_index") == statement_index
                 and _decimal(citation.get("value")) == expected
                 and all(citation.get(key) == expected_value for key, expected_value in context.items())]
        if valid:
            result[field] = valid
    return result


def financial_analysis_citation_status(analysis: Any, *, statement_index: int | None = None) -> dict[str, dict[str, str]]:
    """Citation status for dated or undated source-local extracted numbers."""
    facts = next((row for index, row in _source_statements(analysis) if index == statement_index), {})
    fields = [field for field in FIELD_ALIASES if _fact_value(facts, field) is not None]
    record, _, reason = _record_for_analysis(analysis, statement_index)
    if reason:
        return {field: _invalid(reason) for field in fields}
    verified = validated_fact_citations_for_analysis(analysis, statement_index=statement_index)
    statuses = record.get("fact_citation_status") or {}
    result = {}
    for field in fields:
        if field in verified:
            result[field] = {"status": "verified", "reason": "exact_searchable_source_quote"}
        else:
            status = statuses.get(field) or {}
            reason = "citation_no_longer_current" if status.get("status") == "verified" else status.get("reason") or "citation_not_provided"
            result[field] = _invalid(reason)
    return result


def _period_index(period: Any, analysis: Any) -> tuple[int | None, str]:
    if analysis is None:
        return None, "source_analysis_unavailable"
    if str(getattr(period, "source_file_id", "")) != str(getattr(analysis, "bucket_file_id", "")) or str(getattr(period, "source_analysis_id", "")) != str(getattr(analysis, "id", "")) or getattr(period, "content_hash", None) != getattr(analysis, "content_hash", None):
        return None, "source_hash_or_identity_mismatch"
    prefix = f"statement-analysis:{analysis.id}"
    key = str(getattr(period, "idempotency_key", ""))
    if key == prefix:
        return None, ""
    suffix = key[len(prefix) + 1:] if key.startswith(prefix + ":") else ""
    if suffix.isdigit() and 0 <= int(suffix) < MAX_STATEMENTS:
        return int(suffix), ""
    return None, "statement_index_unavailable"


def _period_values(period: Any) -> dict[str, Decimal | None]:
    result = {field: _decimal(getattr(period, field, None)) for field in FIELD_ALIASES}
    if result["gross_profit"] is None and result["revenue"] is not None and result["cogs"] is not None:
        result["gross_profit"] = result["revenue"] - result["cogs"]
    return result


def validated_fact_citations_for_period(period: Any, analysis: Any) -> dict[str, list[dict]]:
    index, reason = _period_index(period, analysis)
    if reason:
        return {}
    record, facts, reason = _record_for_analysis(analysis, index)
    if reason:
        return {}
    context = _fact_context(facts)
    if any(context[key] and str(getattr(period, attribute, "")) != context[key]
           for key, attribute in (("period_start", "period_start"), ("period_end", "period_end"))):
        return {}
    if context["basis"] and _normalized(str(getattr(period, "accounting_basis", ""))) != _normalized(context["basis"]):
        return {}
    if context["currency"] and str(getattr(period, "currency", "")).upper() != context["currency"].upper():
        return {}
    values = _period_values(period)
    available = validated_fact_citations_for_analysis(analysis, statement_index=index)
    result = {field: citations for field, citations in available.items()
              if values.get(field) is not None and _fact_value(facts, field) == values[field]}
    if values["gross_profit"] is not None and _fact_value(facts, "gross_profit") is None and values["revenue"] is not None and values["cogs"] is not None and values["gross_profit"] == values["revenue"] - values["cogs"]:
        if all(field in result for field in ("revenue", "cogs")):
            result["gross_profit"] = [result[field][0] for field in ("revenue", "cogs")]
    if values["ebitda"] is not None and _fact_value(facts, "ebitda") is None:
        components = ("net_income", "interest", "income_taxes", "depreciation_and_amortization")
        component_values = [_fact_value(facts, field) for field in components]
        if all(value is not None for value in component_values) and sum(component_values) == values["ebitda"]:
            # Interest/tax/D&A are not stored separately in the period table;
            # their exact current extraction still supplies the derivation.
            if all(field in available for field in components) and values["net_income"] == component_values[0]:
                result["ebitda"] = [available[field][0] for field in components]
    return result


def financial_period_citation_status(period: Any, analysis: Any) -> dict[str, dict[str, str]]:
    values = _period_values(period)
    fields = [field for field, value in values.items() if value is not None]
    index, reason = _period_index(period, analysis)
    if reason:
        return {field: _invalid(reason) for field in fields}
    record, facts, reason = _record_for_analysis(analysis, index)
    if reason:
        return {field: _invalid(reason) for field in fields}
    verified = validated_fact_citations_for_period(period, analysis)
    statuses = record.get("fact_citation_status") or {}
    result = {}
    for field in fields:
        if field in verified:
            derived = _fact_value(facts, field) is None
            result[field] = {"status": "verified", "reason": "derived_from_verified_inputs" if derived else "exact_searchable_source_quote"}
        elif _fact_value(facts, field) is not None and _fact_value(facts, field) != values[field]:
            result[field] = _invalid("financial_period_value_mismatch")
        elif field == "gross_profit" and _fact_value(facts, field) is None and values["revenue"] is not None and values["cogs"] is not None and values[field] != values["revenue"] - values["cogs"]:
            result[field] = _invalid("financial_period_value_mismatch")
        else:
            status = statuses.get(field) or {}
            reason = "citation_no_longer_current" if status.get("status") == "verified" else status.get("reason") or "citation_not_provided"
            result[field] = _invalid(reason)
    return result


def citation_manifest_hash(manifest: dict) -> str:
    """Stable audit fingerprint over bounded citation evidence, never raw pages."""
    data = {key: value for key, value in manifest.items() if key != "validation_hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
