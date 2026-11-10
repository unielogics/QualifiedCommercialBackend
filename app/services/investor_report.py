"""A privacy-limited, source-bound investor read model; no financial writers."""
from __future__ import annotations

import math
import re
from copy import deepcopy

from sqlalchemy import select

from app.models.bucket import BucketFileAnalysis
from app.services import business_profile

LAYOUT_VERSION = "qc.investor_report.v5.five_pages.target_net_financing"
PAGE_LIMIT = 5


def record(value):
    return value if isinstance(value, dict) else {}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        answer = float(value)
        return answer if math.isfinite(answer) else None
    except (TypeError, ValueError):
        return None


def safe_text(value):
    """Remove identifiers from display prose without changing financial inputs."""
    value = str(value or "")
    value = re.sub(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b|\b\d{2}-\d{7}\b|(?<!\d)\d{9,}(?!\d)", "[redacted]", value)
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[private contact]", value)
    value = re.sub(r"(?<!\d)(?:\+?1[- .]?)?(?:\(\d{3}\)[- .]?|\d{3}[- .])\d{3}[- .]\d{4}(?!\d)", "[private contact]", value)
    value = re.sub(r"https?://\S*(?:X-Amz-Signature|X-Amz-Credential|AWSAccessKeyId|[?&]token=)\S*", "[access-controlled source link]", value, flags=re.I)
    value = re.sub(r"(?i)\b(?:ssn|ein|account|acct|routing)\s*(?:number|no\.?|#)?\s*[:#-]?\s*[\dXx*][\dXx* .-]{2,}", "[private identifier]", value)
    value = re.sub(r"(?i)\b(?:dob|date of birth)\s*[:#-]?\s*[0-9/-]+", "[private date]", value)
    return " ".join(value.split())


def safe_label(value, kind=None):
    if re.search(r"personal|credit|fico|\b1040\b|\bpfs\b", f"{kind or ''} {value or ''}", re.I):
        return "Restricted personal / credit evidence"
    return safe_text(value) or "Source document"


def company(intake, profile):
    declared = business_profile.of_intake(intake)
    address = {"street": declared.get("street"), "city": declared.get("city"), "state": declared.get("state"), "postal_code": declared.get("zip")}
    return {"name": getattr(intake, "business_name", None) or "Business name not supplied", "address": address,
        "formatted_address": ", ".join(str(value) for value in address.values() if value),
        "website": declared.get("website"), "website_none": declared.get("website_none", False),
        "industry": getattr(profile, "industry", None) or declared.get("industry"), "naics": getattr(profile, "naics_code", None)}


def _period(facts):
    value = facts.get("statement_period") or facts.get("period") or facts.get("tax_year") or facts.get("year")
    if isinstance(value, dict):
        return " - ".join(str(value.get(key)) for key in ("start", "end") if value.get(key)) or None
    return str(value) if value else " - ".join(str(facts.get(key)) for key in ("period_start", "period_end") if facts.get(key)) or None


def _pages(value):
    pages = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"page", "page_number", "source_page"} and isinstance(item, int) and not isinstance(item, bool) and item > 0:
                pages.add(item)
            elif key in {"pages", "page_numbers"} and isinstance(item, list):
                pages.update(i for i in item if isinstance(i, int) and not isinstance(i, bool) and i > 0)
            else:
                pages.update(_pages(item))
    elif isinstance(value, list):
        for item in value:
            pages.update(_pages(item))
    return sorted(pages)


class Register:
    def __init__(self, files, analyses):
        self.rows = []
        self.files = {}
        for file in sorted(files, key=lambda row: str(row.id)):
            analysis = analyses.get(str(file.id))
            raw = record(getattr(analysis, "analysis", None))
            facts = record(raw.get("key_facts"))
            kind = getattr(analysis, "classification", None) or "supporting_document"
            file_name = getattr(file, "file_name", None)
            restricted = bool(re.search(r"personal|credit|\bpfs\b", f"{kind} {file_name or ''}", re.I))
            row = self.add(label=safe_label(file_name, kind), kind=kind, period=_period(facts), file_id=str(file.id),
                analysis_id=str(analysis.id) if analysis else None, version_number=getattr(file, "document_version_number", None),
                content_hash=getattr(file, "content_hash", None) or None,
                pages=[] if restricted else _pages(raw.get("financial_fact_citations")), metric_keys=[],
                status="restricted_context_not_business_baseline" if restricted else "analyzed" if analysis else "supporting_not_current_analysis")
            self.files[str(file.id)] = row

    def add(self, **fields):
        row = {"id": f"S{len(self.rows) + 1}", "label": "Source reference", "kind": "file_record", "period": None,
            "file_id": None, "version_id": None, "version_number": None, "analysis_id": None, "content_hash": None,
            "pages": [], "metric_keys": [], "status": "recorded", **fields}
        row["version"] = row.get("version_number")
        row["page"] = row["pages"][0] if len(row["pages"]) == 1 else None
        row["used_for"] = row["metric_keys"]
        self.rows.append(row)
        return row

    def use(self, file_id, section):
        row = self.files.get(str(file_id))
        if row and section not in row["metric_keys"]:
            row["metric_keys"].append(section)
        return row["id"] if row else None


def _currency_for_month(analysis, period):
    from app.services.bucket_evidence import statement_period_month
    facts = record(record(getattr(analysis, "analysis", None)).get("key_facts"))
    values = []
    for month in facts.get("months") or []:
        if isinstance(month, dict) and statement_period_month(month.get("statement_period") or month.get("month")) == period:
            values.append(month.get("currency") or facts.get("currency"))
    values = values or [facts.get("currency")]
    currencies = {str(value).upper() for value in values if value}
    return next(iter(currencies)) if len(currencies) == 1 and all(values) and re.fullmatch(r"[A-Z]{3}", next(iter(currencies))) else None


def banking(cockpit, analyses, register):
    bank = record(record(cockpit).get("bank"))
    accounts = [row for row in bank.get("accounts") or [] if row.get("counted") is not False]
    series = []
    for index, account in enumerate(accounts, 1):
        months = []
        for row in account.get("series") or []:
            period = str(row.get("month") or "")
            ids = row.get("file_ids") or []
            currencies = [_currency_for_month(analyses.get(str(fid)), period) for fid in ids]
            currencies = currencies or [None]
            currency = currencies[0] if all(value == currencies[0] for value in currencies) else None
            months.append({"period": period, "deposits": number(row.get("deposits")), "withdrawals": number(row.get("withdrawals")),
                "net_cash_flow": number(row.get("net")), "ending_balance": number(row.get("ending_balance")),
                "beginning_balance": number(row.get("beginning_balance")), "nsf_count": number(row.get("nsf")),
                "currency": currency, "source_ids": [ref for fid in ids if (ref := register.use(fid, "banking"))]})
        series.append({"label": f"Account {index}", "months": sorted(months, key=lambda row: row["period"])})
    notes = ["Statement credits and debits include account movements; deposits are not recognized revenue and net account movement is not NOI or EBITDA."]
    all_currencies = {month["currency"] for account in series for month in account["months"]}
    comparable = bool(all_currencies) and None not in all_currencies and len(all_currencies) == 1
    currency = next(iter(all_currencies)) if comparable else None
    if len(series) > 1 and comparable:
        periods = sorted({row["period"] for account in series for row in account["months"]})
        months = []
        for period in periods:
            rows = [row for account in series for row in account["months"] if row["period"] == period]
            complete_accounts = len(rows) == len(series)
            month = {"period": period, "currency": currency, "source_ids": sorted({item for row in rows for item in row["source_ids"]}), "partial": not complete_accounts}
            for key in ("deposits", "withdrawals", "net_cash_flow", "ending_balance", "beginning_balance", "nsf_count"):
                values = [row[key] for row in rows]
                month[key] = round(sum(values), 2) if complete_accounts and all(value is not None for value in values) else None
            months.append(month)
        if any(row["partial"] for row in months):
            notes.append("Combined values are withheld for months missing a counted account; account coverage is not assumed constant.")
        scope = "all_counted_accounts"
    elif series:
        selected = max(series, key=lambda row: (len(row["months"]), row["months"][-1]["period"] if row["months"] else ""))
        months = selected["months"]
        scope = "single_account" if len(series) == 1 else "single_account_currency_not_comparable"
        if len(series) > 1:
            notes.append(f"The chart and table show {selected['label']} only. Other counted accounts are not combined because their source currencies are missing or differ; all account sources remain in the file register.")
        if not comparable:
            notes.append("Source currency is unconfirmed; values are shown without an assumed currency or conversion.")
    else:
        months, scope = [], "no_current_statement_series"
    return {"months": months[-12:], "account_series": series, "currency": currency, "account_count": len(accounts),
        "displayed_account_count": len(accounts) if scope == "all_counted_accounts" else 1 if series else 0,
        "aggregation_scope": scope, "coverage_notes": notes, "total_month_count": len(months), "displayed_month_count": min(12, len(months))}


def _score_band(value):
    value = number(value)
    if value is None or not 300 <= value <= 850:
        return None
    for low, high in ((300, 619), (620, 659), (660, 679), (680, 719), (720, 759), (760, 850)):
        if low <= value <= high:
            return f"{low}-{high}"
    return None


def credit_summary(owners, collections, register):
    reports = [collections.get(str(owner.id), {}) for owner in owners]
    required = [row for owner, row in zip(owners, reports, strict=True) if bool(getattr(owner, "credit_required", False))]
    bands, bureaus, models, dates, statuses = set(), set(), set(), [], []
    flag_count, high_count, risk_available = 0, 0, False
    for index, (owner, row) in enumerate(zip(owners, reports, strict=True), 1):
        if not row.get("report_available"):
            continue
        verified = row.get("score_verified") is True and row.get("status") == "collected"
        if verified and (band := _score_band(row.get("score"))):
            bands.add(band)
        source = str(row.get("fico_source") or "")
        for item in row.get("score_models") or []:
            source += " " + str(item.get("bureau") or "") + " " + str(item.get("model") or "")
        for label in ("TransUnion", "Equifax", "Experian"):
            if label.lower() in source.lower():
                bureaus.add(label)
        for match in re.finditer(r"(?i)\b(FICO(?:\s+Score)?(?:\s+Classic)?\s*\d{1,2}|VantageScore(?:\s*\d(?:\.\d)?)?)\b", source):
            models.add(" ".join(match.group(1).split()))
        if row.get("pulled_at"):
            dates.append(str(row["pulled_at"])[:10])
        statuses.append(row.get("status") or "needs_review")
        risk = record(record(getattr(owner, "credit_summary", None)).get("credit_risk"))
        if verified and isinstance(risk.get("flag_count"), int) and isinstance(risk.get("high_count"), int):
            risk_available = True
            flag_count += max(0, risk["flag_count"])
            high_count += max(0, risk["high_count"])
        register.add(label=f"Linked owner credit report {index}", kind="linked_owner_credit", period=str(row.get("pulled_at") or "")[:10] or None,
            metric_keys=["credit"], source_id=str(row.get("credit_pull_id")) if row.get("credit_pull_id") else None,
            status="current_linked_report" if verified else "linked_report_needs_review")
    current = sum(row.get("status") == "collected" for row in required)
    ownership = [number(getattr(owner, "ownership_pct", None)) for owner in owners]
    roster_complete = bool(ownership and all(value is not None for value in ownership)
        and abs(sum(ownership) - 100) < .01 and not any(getattr(owner, "backfill_needs_review", False) for owner in owners))
    roster_note = ("No owners meet the file's current required-credit threshold. " if roster_complete else
        "A required-owner credit roster is not established; confirm ownership completeness. ")
    return {"report_count": len(statuses), "required_owner_count": len(required), "current_required_report_count": current,
        "coverage_status": "not_required" if roster_complete and not required else "established" if roster_complete and required and current == len(required) else "pending",
        "score_bands": sorted(bands), "score_band": ", ".join(sorted(bands)) or None, "bureaus": sorted(bureaus), "models": sorted(models),
        "as_of": max(dates) if dates else None, "risk_flag_count": flag_count if risk_available else None,
        "high_risk_flag_count": high_count if risk_available else None,
        "risk_summary": (f"{current} of {len(required)} required owner reports are current. " if required else roster_note) +
            (f"{flag_count} review flags ({high_count} high priority) were recorded." if risk_available else "Detailed risk review is not established in this snapshot.") +
            " Score bands are context only; bureau/model-specific program checks remain separate.",
        "privacy": "No owner names, personal addresses, dates of birth, identifiers or tradeline details are included."}


def _link_refs(value, register, section):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"file_id", "source_file_id", "pdf_file_id", "original_source_file_id"} and item:
                register.use(item, section)
            elif key in {"source_file_ids", "file_ids"} and isinstance(item, list):
                for file_id in item:
                    register.use(file_id, section)
            elif isinstance(item, (dict, list)):
                _link_refs(item, register, section)
    elif isinstance(value, list):
        for row in value:
            _link_refs(row, register, section)


def collateral_report(current, terms, register):
    """Show the offered version; later disclosures remain identifiable proposals."""
    current, terms = record(current), record(terms)
    has_terms = terms.get("source") == "term_sheet" or bool(terms.get("recorded_terms_ref"))
    recorded_revision = terms.get("collateral_revision") if has_terms else None
    raw = record(terms.get("collateral_snapshot")) if has_terms else current
    # Legacy terms did not freeze security: the present-day schedule cannot
    # retroactively become collateral offered on that historical document.
    unrecorded = has_terms and recorded_revision is None
    stale = bool(terms.get("collateral_stale")) or (has_terms and recorded_revision != current.get("revision", 0))
    result = {"enabled": bool(raw.get("enabled")) and not unrecorded,
        "revision": raw.get("revision", recorded_revision if has_terms else 0),
        "recorded_revision": recorded_revision, "current_revision": current.get("revision", 0),
        "basis": "unrecorded_terms" if unrecorded else "recorded_terms" if has_terms else "staff_proposal",
        "active_proposal": terms.get("active_proposal") is not False,
        "terms_stale": stale, "properties": [], "assets": [],
        "note": "Staff-disclosed collateral; ownership, valuation, priority and available lending capacity are unverified.",
        "current_proposal": deepcopy(current) if stale else None}
    if result["enabled"]:
        for group in ("properties", "assets"):
            for row in raw.get(group) or []:
                if not isinstance(row, dict):
                    continue
                address = record(row.get("address"))
                result[group].append({"id": row.get("id"),
                    "address": safe_text(", ".join(str(address.get(key)) for key in ("street", "city", "state", "postal_code") if address.get(key))),
                    "address_fields": deepcopy(address), "real_estate_property_id": row.get("real_estate_property_id"),
                    "description": safe_text(row.get("description")), "asset_type": safe_text(row.get("asset_type")),
                    "owner_name": safe_text(row.get("owner_name")), "property_type": safe_text(row.get("property_type")),
                    "value": number(row.get("estimated_value")), "existing_liens": number(row.get("existing_liens_total")),
                    "available_lien_amount": number(row.get("available_lien_amount")), "lien_position": row.get("proposed_lien_position"),
                    "valuation_date": row.get("valuation_date"), "currency": row.get("currency"), "notes": safe_text(row.get("notes")),
                    "source_ids": [ref for file_id in row.get("source_file_ids") or [] if (ref := register.use(file_id, "collateral"))]})
    register.add(label="Collateral recorded with financing terms" if has_terms else "Staff proposed collateral disclosure",
        kind="staff_collateral_disclosure", version_number=result["revision"],
        period=str(raw.get("updated_at") or "")[:10] or None, metric_keys=["collateral"],
        status="unrecorded_terms_collateral" if unrecorded else "staff_declared_unverified")
    if stale:
        register.add(label="New collateral proposal - updated terms version required", kind="staff_collateral_proposal",
            version_number=current.get("revision", 0), metric_keys=["collateral"], status="not_recorded_in_financing_terms")
        for row in [*(current.get("properties") or []), *(current.get("assets") or [])]:
            for file_id in row.get("source_file_ids") or []:
                register.use(file_id, "collateral")
    return result


async def build(db, intake, profile, cockpit, financials, files, context):
    from app.services import application_profiles, deal_budget, investor_collateral
    from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION
    from app.services.credit_requirement_sync import current_owner_collections
    from app.services.financial_workspace import filter_current_files
    from app.services.financing_budget_disclosure import public_budget
    current = await filter_current_files(db, files) if getattr(profile, "primary_bucket_id", None) else []
    analyses = {}
    if current:
        rows = list((await db.execute(select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id.in_([file.id for file in current]),
            BucketFileAnalysis.bucket_id == profile.primary_bucket_id, BucketFileAnalysis.status == "completed",
            BucketFileAnalysis.analysis_version == CURRENT_FILE_ANALYSIS_VERSION).order_by(BucketFileAnalysis.created_at.desc(), BucketFileAnalysis.id.desc()))).scalars().all())
        hashes = {str(file.id): file.content_hash for file in current}
        for row in rows:
            if row.content_hash == hashes.get(str(row.bucket_file_id)):
                analyses.setdefault(str(row.bucket_file_id), row)
    register = Register(files, analyses)
    register.add(label="Business identity and address in this application", kind="application_business_profile",
        source_id=str(intake.id) if getattr(intake, "id", None) else None, metric_keys=["company"], status="application_record")
    for program in financials.get("selected_programs") or []:
        register.add(label=safe_text(program.get("name") or "Selected financing program"), kind="program_playbook",
            source_id=program.get("playbook_id"), version_number=program.get("playbook_version"),
            metric_keys=["programs"], status="manual_scope_review_required" if program.get("needs_scope_review") else "published_program_selection")
    for item in context.get("financial_versions") or []:
        row = register.files.get(str(item.get("source_file_id")))
        if row:
            row.update(version_id=item.get("version_id"), version_number=item.get("version_number"), version=item.get("version_number"),
                kind=item.get("kind") or row["kind"], status="current_financial_version")
            register.use(row["file_id"], "financials")
    bank = banking(cockpit or {}, analyses, register)
    owners = await application_profiles.owner_rows(db, profile) if getattr(profile, "primary_bucket_id", None) else []
    collections = await current_owner_collections(db, owners)
    credit = credit_summary(owners, collections, register)
    collateral = collateral_report(investor_collateral.stored(profile), record(financials.get("proposed_terms")), register)
    budget = deal_budget.reporting(profile, record(financials.get("proposed_terms")))
    financials["deal_budget"] = deepcopy(budget)
    public_costs = public_budget(budget)
    register.add(label="Recorded financing cost budget" if public_costs["basis"] == "recorded_terms" else "Proposed financing cost budget",
        kind="staff_deal_budget", version_number=public_costs.get("revision"), metric_keys=["deal_budget", "terms", "use_of_funds"],
        status="new_proposal_requires_terms_version" if public_costs["stale"] else public_costs["basis"])
    selected_costs = record(budget.get("recorded")) if public_costs["basis"] == "recorded_terms" else record(budget.get("current"))
    for source in sorted({str(row.get("source_ref")) for row in selected_costs.get("rows") or [] if row.get("source_ref")}):
        register.add(label="Configured pricing source", kind="pricing_setting", source_id=source,
            metric_keys=["deal_budget"], status="staff_reviewed_setting_reference")
    request_revision = record(getattr(profile, "current_funding_request", None)).get("revision", 0)
    use_revision = getattr(profile, "use_of_funds_revision", 0)
    register.add(label=f"Current funding request r{request_revision} and use-of-funds r{use_revision}", kind="staff_funding_request",
        version_number=use_revision, request_revision=request_revision, metric_keys=["use_of_funds"], status="declared_budget_not_payoff_evidence")
    terms = record(financials.get("proposed_terms"))
    if terms:
        recorded = record(record(record(cockpit).get("plan")).get("terms_link")).get("recorded") or record(getattr(profile, "financial_plan", None)).get("recorded_terms_ref") or record(getattr(profile, "financial_plan", None)).get("terms_ref") or {}
        register.add(label="Saved financing terms" if terms.get("source") == "term_sheet" else "Staff working refinance scenario",
            kind="saved_loan_terms" if terms.get("source") == "term_sheet" else "financial_plan", metric_keys=["terms", "financials"],
            source_id=recorded.get("id") if terms.get("source") == "term_sheet" else None,
            version_number=recorded.get("version") if terms.get("source") == "term_sheet" else None,
            period=str(record(getattr(profile, "financial_plan", None)).get("updated_at") or "")[:10] or None,
            status="recorded_terms" if terms.get("source") == "term_sheet" else "conditional_staff_scenario")
    review = record(getattr(profile, "financial_metric_review", None))
    if review:
        register.add(label="Staff reviewed calculation inputs", kind="metric_review", period=str(review.get("updated_at") or "")[:10] or None,
            metric_keys=["financials"], status="source_bound_staff_corrections")
    operating = record(record(financials.get("operating_earnings")).get("improvement_plan"))
    if operating:
        snapshot = record(operating.get("snapshot"))
        register.add(label="Operating expenses and earnings plan", kind="operating_plan", version_number=snapshot.get("version_number") or snapshot.get("revision"),
            metric_keys=["operating_plan", "financials"], status=snapshot.get("state") or "conditional_proposals")
    _link_refs(financials.get("operating_earnings"), register, "financials")
    _link_refs(financials.get("debt_evidence"), register, "financials")
    _link_refs(financials.get("tax_years"), register, "financials")
    _link_refs(financials.get("p_and_l"), register, "financials")
    _link_refs(operating, register, "operating_plan")
    # Source-selection metadata distinguishes prior-year trend/personal context
    # from the accepted latest-period business basis; values never cross over.
    _link_refs(record(cockpit).get("metric_review"), register, "financials")
    for source in record(record(cockpit).get("ebitda")).get("source_selection", {}).get("sources", []):
        row = register.files.get(str(source.get("file_id")))
        if row is None:
            continue
        if source.get("selected"):
            row["status"] = "current_business_earnings_baseline"
        elif source.get("comparative_only"):
            row["status"] = "prior_period_trend_only"
            row["metric_keys"] = ["financial_trend"]
        elif not source.get("eligible"):
            row["status"] = "excluded_personal_or_unconfirmed_source"
            row["metric_keys"] = ["source_selection_exclusion"]
        elif not source.get("included"):
            row["status"] = "excluded_by_staff"
            row["metric_keys"] = ["source_selection_exclusion"]
        else:
            row["status"] = "alternative_source_not_added_to_baseline"
    for row in register.rows:
        row["used_for"] = list(row["metric_keys"])
    sections = {key: [row["id"] for row in register.rows if key in row["metric_keys"]] for key in ("company", "programs", "banking", "financials", "terms", "collateral", "credit", "use_of_funds", "operating_plan", "deal_budget")}
    return {"layout_version": LAYOUT_VERSION, "max_pages": PAGE_LIMIT, "company": company(intake, profile), "banking": bank,
        "credit": credit, "collateral": collateral, "deal_budget": public_costs, "references": deepcopy(register.rows), "section_reference_ids": sections,
        "reference_note": "The complete immutable register is retained with this generated report in the file; source originals remain access-controlled."}


def analysis_snapshot(financials):
    """Freeze safe normalized analyses; never raw credit reports or account payloads."""
    def freeze(value):
        if isinstance(value, dict):
            return {key: freeze(item) for key, item in value.items() if key not in {"download_url", "preview_url", "presigned_url"}}
        if isinstance(value, list):
            return [freeze(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"https?://\S*(?:X-Amz-Signature|X-Amz-Credential|AWSAccessKeyId|[?&]token=)\S*", "[access-controlled source link]", value, flags=re.I)
        return deepcopy(value)
    return freeze({key: financials.get(key) for key in ("investor_report", "current_key_metrics", "use_of_funds", "plan",
        "proposed_terms", "deal_budget", "operating_earnings", "tax_years", "p_and_l", "package_readiness", "package_options", "reviewed_math")})


def reviewed_math(cockpit):
    from app.services.financial_calculation_policy import financial_authority
    calculation_keys = ("key", "label", "kind", "value", "source_value", "reviewed_value", "formula", "period", "months_covered",
        "currency", "basis", "numerator", "denominator", "complete", "provisional", "input_keys", "active_input_keys", "source_refs")
    operand_keys = ("key", "label", "value", "signed_value", "included", "fact_key", "source_refs")
    calculations = []
    for source in record(record(cockpit).get("metric_review")).get("calculations") or []:
        row = {key: deepcopy(source[key]) for key in calculation_keys if key in source}
        row["fundamentals"] = [{key: deepcopy(item[key]) for key in operand_keys if key in item} for item in source.get("fundamentals") or []]
        row["adjustments"] = [{key: deepcopy(item[key]) for key in ("kind", "label", "amount", "value", "signed_value", "included", "reason", "source_refs") if key in item} for item in source.get("adjustments") or []]
        calculations.append(row)
    return {"authority": financial_authority(cockpit or {}), "calculations": calculations}


def analysis_notes(snapshot):
    rows = []
    plan = record(record(snapshot.get("operating_earnings")).get("improvement_plan"))
    for item in plan.get("expenses") or []:
        if item.get("notes"):
            rows.append({"section": "Operating expense", "label": safe_text(item.get("label") or "Expense"), "text": safe_text(item["notes"])})
    for item in plan.get("adjustments") or []:
        parts = [str(item[key]) for key in ("explanation", "source_note") if item.get(key)]
        parts.extend(str(issue) for issue in item.get("issues") or [])
        if parts:
            rows.append({"section": "Earnings action", "label": safe_text(item.get("label") or "Proposed action"), "text": "\n".join(safe_text(part) for part in parts)})
    if plan.get("plan_notes"):
        rows.append({"section": "Staff assumptions", "label": "Operating-plan notes", "text": safe_text(plan["plan_notes"])})
    for item in plan.get("missing") or []:
        rows.append({"section": "Missing inputs", "label": "Operating plan", "text": safe_text(item)})
    for item in plan.get("risk_flags") or []:
        rows.append({"section": "Review conditions", "label": "Operating plan", "text": safe_text(item)})
    if record(record(snapshot.get("proposed_terms")).get("risk")).get("words"):
        rows.append({"section": "Repayment risk", "label": "Financing terms", "text": safe_text(snapshot["proposed_terms"]["risk"]["words"])})
    earnings = record(snapshot.get("operating_earnings"))
    if earnings.get("historical_unavailable_reason"):
        rows.append({"section": "Earnings baseline", "label": "Evidence limitation", "text": safe_text(earnings["historical_unavailable_reason"])})
    trend = record(earnings.get("revenue_trend"))
    for text in trend.get("notes") or []:
        rows.append({"section": "Revenue trend", "label": "Comparison notes", "text": safe_text(text)})
    assumptions = record(trend.get("forecast")).get("assumptions") or []
    for text in assumptions if isinstance(assumptions, list) else [assumptions]:
        rows.append({"section": "Revenue forecast", "label": "Conditional assumption", "text": safe_text(text)})
    for item in record(snapshot.get("plan")).get("modifications") or []:
        if isinstance(item, dict) and item.get("detail"):
            rows.append({"section": "Refinance plan", "label": safe_text(item.get("label") or "Plan modification"), "text": safe_text(item["detail"])})
    for item in record(snapshot.get("package_readiness")).get("requirements") or []:
        if item.get("effectively_open"):
            rows.append({"section": "Open evidence condition", "label": safe_text(item.get("label") or item.get("requirement_key")), "text": safe_text(item.get("state_reason") or item.get("status") or "Evidence remains open")})
    collateral = record(record(snapshot.get("investor_report")).get("collateral"))
    for item in [*(collateral.get("properties") or []), *(collateral.get("assets") or [])]:
        if item.get("notes"):
            rows.append({"section": "Collateral disclosure", "label": safe_text(item.get("address") or item.get("description")), "text": safe_text(item["notes"])})
    return rows
