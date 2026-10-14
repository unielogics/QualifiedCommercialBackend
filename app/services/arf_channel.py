"""Source-backed ARF catalog, permissions and preliminary matching.

The supplied build specification is a source snapshot, not a live offer. Drafts
are deliberately visible internally and absent from borrower-facing catalogs.
No credit decision, approval probability, lender endpoint or missing taxonomy
is inferred by this module.
"""
from __future__ import annotations

import calendar
import json
import math
from collections import Counter
from datetime import UTC, date, datetime

from fastapi import HTTPException
from sqlalchemy import select, text

from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import (
    ApplicationExtractedFact,
    ApplicationProfile,
    ApplicationProgramSelection,
    ApplicationRequirementState,
)
from app.models.arf_channel import ApplicationAssessmentSnapshot, ARFChannelConfiguration
from app.models.funding_program import (
    FundingProgramCatalog,
    FundingProgramCommercialTerms,
    FundingProgramScope,
)

CHANNEL_KEY = "arf_external"
SOURCE_VERSION = "arf-supplied-snapshot-2026-10-14-v1"
ALGORITHM_VERSION = "arf-source-rules-v1"
APPROVAL_KEYS = {"public_copy", "rule_pack", "geography", "hosted_intake", "document_sharing", "credit_pull", "submission_route"}
TRADE_KEYS = {"plumbing", "electrical", "hvac"}
SOURCE_REFERENCES = [
    {"key": "S1", "title": "April 2026 All Products", "pages": [1, 2], "kind": "product_snapshot", "effective_date": None},
    {"key": "S2", "title": "March general criteria and industry types", "pages": [1], "kind": "criteria_snapshot", "effective_date": None},
    {"key": "S3", "title": "New Approved Industries", "pages": [1], "kind": "industry_overlay", "effective_date": None},
]
TRADE_OVERLAY = {
    "key": "arf_skilled_trades", "industries": ["plumbing", "electrical", "hvac"],
    "minimum_beacon": 651, "minimum_business_years": 3, "minimum_annual_revenue": 300000,
    "registered_entity_required": True, "ein_required": True, "standard_maximum": 250000,
    "minimum_w2_employees": None, "home_based_may_qualify": True,
    "product_mapping_confirmed": False, "source_reference": "S3 p1",
    "documents": ["business_bank_statements_6_months", "guarantor_primary_ids", "liability_insurance", "workers_compensation_insurance", "most_recent_federal_tax_return"],
    "tier_one": {"minimum_beacon": 700, "minimum_business_years": 5, "verified_homeownership": True, "single_entity_snapshot_limit": 600000, "multi_entity_snapshot_limit": 900000, "manual_review_required": True},
}


def _product(key, name, slug, title, description, minimum, draw, points, **features):
    return {"program_key": key, "name": name, "public_slug": slug, "title": title,
            "description": description, "funding_channel": CHANNEL_KEY, "qc_role": "referral_partner",
            "source_version": SOURCE_VERSION, "source_references": SOURCE_REFERENCES,
            "minimum_amount": minimum, "maximum_amount": 1500000, "maximum_term_months": 24,
            "minimum_draw": draw, "closing_points": points, "early_payoff_available": True,
            "features": features, "overlays": [TRADE_OVERLAY],
            "shared_criteria": {"minimum_equifax": 575, "minimum_same_ownership_months": 1,
                                "monthly_sales_observation": 17000, "annual_sales_observation": 200000,
                                "revenue_combination": "unconfirmed", "source_reference": "S2 p1"},
            "unresolved_review_items": ["Current rule pack and geography require lender confirmation", "General 36-month statement conflicts with product-specific 24-month snapshot", "Official lender forms and complete submission checklist not supplied"],
            "disclosures": ["Qualified Commercial acts as a referral partner; the creditor is identified in the actual lender offer.", "Subject to lender review, availability and confirmed terms. A complete file is not a credit approval.", "Updated bank data and lender desktop underwriting are required for each draw."]}


PRODUCTS = [
    _product("arf_bankroll", "Bankroll", "business-line-of-credit", "Business Revolving Line of Credit", "A revolving business-capital option for recurring operating needs.", 5000, 5000, [3], revolving_window="26 weeks to one year", partial_paydown_minimum=5000),
    _product("arf_io_bankroll", "IO-Bankroll", "interest-only-line-of-credit", "Interest-Only Business Line of Credit", "A revolving line with an interest-only period, subject to confirmed lender terms.", 10000, 10000, [3], revolving_months=11, interest_only_months=12, initial_draw_fraction=0.25, partial_paydown_minimum=10000, public_criteria_conflict=True),
    _product("arf_elevate_bankroll", "Elevate Bankroll", "flexible-payment-line-of-credit", "Flexible-Payment Business Line of Credit", "A business line with lender-approved interest-only periods.", 5000, 5000, [4], revolving_window="26 weeks to one year", interest_only_plans=[[8], [4, 4]], notice_business_days=10, minimum_days_out_of_program=120, partial_paydown_minimum=5000, draws_and_paydowns_during_io=False, additional_finance_charges=True),
    _product("arf_io_flex_pay", "IO-Flex Pay", "interest-only-flex-pay", "Interest-Only Flex Pay Business Loan", "A business loan with an initial interest-only option.", 10000, 10000, [3, 4], classification="loan", revolving_window="26–40 weeks", interest_only_weeks=26, minimum_beacon=651, homeownership_required=True, partial_paydowns_allowed=False),
]
PRODUCT_BY_KEY = {p["program_key"]: p for p in PRODUCTS}


async def get_configuration(db):
    row = await db.get(ARFChannelConfiguration, CHANNEL_KEY)
    return row or ARFChannelConfiguration(channel_key=CHANNEL_KEY, version=1, intake_mode="referral_only", publication_state="draft_pending_approval", approvals={}, permitted_fields=[], permitted_documents=[])


def approval_valid(config, key):
    approval = (config.approvals or {}).get(key) or {}
    return approval.get("approved") is True and bool(str(approval.get("evidence_reference", "")).strip())


async def require_hosted_permission(db, *, requested_fields=None, requested_documents=None, capability=None):
    config = await get_configuration(db)
    if config.intake_mode != "partner_hosted_authorized" or not approval_valid(config, "hosted_intake"):
        raise HTTPException(403, "ARF is referral-only. Written hosted-intake authorization is required before collecting financial information.")
    if capability and not approval_valid(config, capability):
        raise HTTPException(403, f"ARF permission for {capability.replace('_', ' ')} has not been confirmed")
    if set(requested_fields or []) - set(config.permitted_fields or []):
        raise HTTPException(403, "Requested ARF fields exceed the written intake scope")
    if set(requested_documents or []) - set(config.permitted_documents or []):
        raise HTTPException(403, "Requested ARF documents exceed the written intake scope")
    return config


async def profile_uses_arf(db, profile):
    if getattr(profile, "funding_category", None) == CHANNEL_KEY:
        return True
    result = await db.execute(select(ApplicationProgramSelection.id).where(ApplicationProgramSelection.profile_id == profile.id, ApplicationProgramSelection.program_key.like("arf\\_%", escape="\\"), ApplicationProgramSelection.removed_at.is_(None)).limit(1))
    return result.scalar_one_or_none() is not None


async def get_profile_arf_configuration(db, profile):
    return await get_configuration(db) if await profile_uses_arf(db, profile) else None


async def require_profile_hosted_permission(db, profile, **kwargs):
    if await profile_uses_arf(db, profile):
        return await require_hosted_permission(db, **kwargs)
    return None


async def require_bucket_hosted_permission(db, bucket_id, **kwargs):
    profiles = list((await db.execute(select(ApplicationProfile).where(ApplicationProfile.primary_bucket_id == bucket_id))).scalars().all())
    for profile in profiles:
        await require_profile_hosted_permission(db, profile, **kwargs)


async def require_submission_permission(db, *, program_key=None):
    if program_key and not program_key.startswith("arf_"):
        return
    config = await require_hosted_permission(db, capability="document_sharing")
    if not all(approval_valid(config, key) for key in ("rule_pack", "geography", "submission_route")):
        raise HTTPException(409, "ARF rule pack, geography and submission route require written confirmation")


def requirement_definitions(product=None):
    fields = [
        ("business_legal_name", "Legal business name", "text", "qc_packaging"),
        ("current_ownership_started", "Current ownership start date", "date", "lender_explicit"),
        ("business_started", "Business operating start date", "date", "lender_explicit"),
        ("monthly_sales", "Monthly operating sales", "number", "lender_explicit"),
        ("annual_revenue", "Annual operating sales", "number", "lender_explicit"),
        ("requested_amount", "Requested financing amount", "number", "qc_packaging"),
        ("industry_source_label", "Industry and business activity", "text", "qc_packaging"),
    ]
    output = [{"requirement_key": f"arf_{key}", "canonical_key": key, "label": label, "requirement_kind": "field", "category": "borrower_info", "required_level": "required", "collection_config": {"field_key": key, "value_type": kind, "origin": origin, "subject": "business", "automation_approved": True}} for key, label, kind, origin in fields]
    outside_trades = {"not": {"field": "industry_key", "op": "in", "value": sorted(TRADE_KEYS)}}
    if not product or product["program_key"] != "arf_io_flex_pay":
        output.append({"requirement_key": "arf_equifax_score", "canonical_key": "equifax_score", "label": "Equifax credit score", "requirement_kind": "field", "category": "credit", "required_level": "required", "applies_when": outside_trades, "collection_config": {"field_key": "equifax_score", "value_type": "integer", "origin": "lender_explicit", "automation_approved": True}})
        output.append({"requirement_key": "arf_equifax_verified", "canonical_key": "equifax_verified", "label": "Verify the source, subject and accepted Equifax credit model", "requirement_kind": "human_review", "category": "credit", "required_level": "required", "applies_when": outside_trades, "collection_config": {"origin": "lender_explicit", "assessment_fact": "equifax_verified", "automation_approved": False}})
    conditional_fields = [("beacon_score", "Equifax Beacon score", "number"), ("beacon_verified", "Verify accepted Equifax Beacon source and score model", "human_review"), ("registered_entity", "Registered business entity", "boolean"), ("ein_verified", "Confirm the business EIN", "boolean")]
    for key, label, kind in conditional_fields:
        output.append({"requirement_key": f"arf_{key}", "canonical_key": key, "label": label, "requirement_kind": "human_review" if kind == "human_review" else "field", "category": "credit" if "beacon" in key else "borrower_info", "required_level": "required", "applies_when": {"field": "industry_key", "op": "in", "value": sorted(TRADE_KEYS)}, "collection_config": {"field_key": key, "value_type": "text" if kind == "human_review" else kind, "origin": "lender_explicit", "automation_approved": True, **({"assessment_fact": key} if kind == "human_review" else {})}})
    output.append({"requirement_key": "arf_industry_mapping_review", "canonical_key": "industry_mapping_approved", "label": "Confirm source-industry mapping and excluded activities", "requirement_kind": "human_review", "category": "ai_internal", "required_level": "required", "collection_config": {"origin": "pending_lender_confirmation", "assessment_fact": "industry_mapping_approved", "automation_approved": False}})
    if product and product["program_key"] == "arf_io_flex_pay":
        output = [item for item in output if item["canonical_key"] not in {"beacon_score", "beacon_verified"}]
        for key, label, kind in [("beacon_score", "Equifax Beacon score", "field"), ("beacon_verified", "Verify accepted Equifax Beacon source and score model", "human_review"), ("homeownership_verified", "Verify homeownership", "human_review")]:
            output.append({"requirement_key": f"arf_{key}", "canonical_key": key, "label": label, "requirement_kind": kind, "category": "credit", "required_level": "required", "collection_config": {"field_key": key, "value_type": "number" if key == "beacon_score" else "boolean", "origin": "lender_explicit", "automation_approved": True, **({"assessment_fact": key} if kind == "human_review" else {})}})
    if product and product["program_key"] == "arf_elevate_bankroll":
        output.append({"requirement_key": "arf_merchant_guarantor_history", "canonical_key": "merchant_guarantor_history", "label": "Confirm merchant-guarantor participation history and 120-day eligibility", "requirement_kind": "human_review", "category": "ai_internal", "required_level": "required", "collection_config": {"origin": "lender_explicit", "assessment_fact": "merchant_guarantor_eligibility_verified", "automation_approved": False}})
    for key, label in [("business_bank_statements_6_months", "Six months of business bank statements"), ("guarantor_primary_ids", "Guarantors’ primary identification"), ("liability_insurance", "Liability insurance"), ("workers_compensation_insurance", "Workers’ compensation insurance"), ("most_recent_federal_tax_return", "Most recent federal tax return — return type requires confirmation")]:
        output.append({"requirement_key": key, "canonical_key": key, "label": label, "requirement_kind": "document", "category": "insurance" if "insurance" in key else "financials", "required_level": "required", "applies_when": {"field": "industry_key", "op": "in", "value": sorted(TRADE_KEYS)}, "collection_config": {"origin": "lender_explicit", "subject": "guarantor" if key == "guarantor_primary_ids" else "business", "period": "six_months" if "6_months" in key else "latest" if "tax_return" in key else "current", "automation_approved": True}})
    return output


async def ensure_catalog(db, user_id=None):
    """Idempotently import the supplied snapshot as drafts; never publish it."""
    # Multiple API workers can start together; serialize the first import so
    # a catalog uniqueness race cannot fail application startup.
    await db.execute(text("SELECT pg_advisory_xact_lock(20261014, 1170)"))
    config = await db.get(ARFChannelConfiguration, CHANNEL_KEY)
    if config is None:
        db.add(await get_configuration(db))
    created = []
    for index, definition in enumerate(PRODUCTS):
        row = (await db.execute(select(FundingProgramCatalog).where(FundingProgramCatalog.program_key == definition["program_key"])) ).scalar_one_or_none()
        if row is not None:
            continue
        row = FundingProgramCatalog(program_key=definition["program_key"], public_slug=definition["public_slug"], name=definition["name"], short_description=definition["description"], display_order=500 + index, status="active", aliases=[definition["title"]], created_by_user_id=user_id)
        db.add(row)
        await db.flush()
        db.add(FundingProgramScope(program_id=row.id, vertical="main_street", scope_key="arf_external", intake_variants=[], intent_keys=[], naics_prefixes=[], excluded_naics_prefixes=[], industry_keys=[], required_fact_keys=[], is_active=True))
        playbook = AIPlaybookTemplate(owner_type="platform", owner_id=None, playbook_type="loan_product", product_key=row.program_key, funding_program_id=row.id, name=row.name, description=row.short_description, version=1, status="draft", is_active=True, rules={"fit": {"field": "vertical", "op": "eq", "value": "main_street"}, "arf": definition, "unresolved_review_items": definition["unresolved_review_items"]})
        db.add(playbook)
        await db.flush()
        for order, item in enumerate(requirement_definitions(definition)):
            db.add(AICollectionRequirement(playbook_id=playbook.id, **item, blocks_stage="underwriting", visibility=["borrower", "underwriter"], can_underwriter_waive=False, verification_required=True, completion_mode="requires_human_verify", display_order=order, objective_text=item["label"], completion_criteria="Collect and verify against the confirmed source policy"))
        db.add(FundingProgramCommercialTerms(program_id=row.id, version=1, status="draft", minimum_amount=definition["minimum_amount"], maximum_amount=definition["maximum_amount"], maximum_term_months=definition["maximum_term_months"], pricing_basis="file_specific", disclosures=definition["disclosures"], source_reference=SOURCE_VERSION))
        created.append(row.program_key)
    await db.flush()
    return created


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", "").replace("$", ""))
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _anniversary(start, years=0, months=0):
    month_index = start.year * 12 + start.month - 1 + years * 12 + months
    year, month = divmod(month_index, 12)
    return date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))


def evaluate_arf_facts(product, facts, *, as_of=None, version=1, approved=False):
    """Evaluate independent source assertions, preserving unknowns/conflicts."""
    if isinstance(product, str):
        product = PRODUCT_BY_KEY[product]
    as_of = as_of or date.today()
    results = []
    def add(key, state, explanation, source, observed=None, required=None):
        results.append({"key": key, "state": state, "explanation": explanation, "source_reference": source, "observed": observed, "required": required, "source_refs": (facts.get("fact_sources", {}).get(key) or {}).get("source_refs", [])})
    def floor(key, minimum, source):
        value = _number(facts.get(key))
        provenance = facts.get("fact_sources", {}).get(key)
        if provenance and provenance.get("verification_status") != "verified":
            value = None
        add(key, "unknown" if value is None else "pass" if value >= minimum else "fail", f"{key.replace('_', ' ')} must be at least {minimum:,}", source, value, minimum)
    def truth(key, source):
        value = facts.get(key)
        add(key, "unknown" if not isinstance(value, bool) else "pass" if value else "fail", f"Confirm {key.replace('_', ' ')}", source, value, True)
    def age(key, years=0, months=0, source="S2 p1"):
        start = _date(facts.get(key))
        duration = f"{years} years" if years else f"{months} calendar months"
        add(key, "unknown" if start is None else "pass" if _anniversary(start, years, months) <= as_of else "fail", f"Confirm {duration} of qualifying history", source, str(start) if start else None)
    trade = facts.get("industry_key") in TRADE_KEYS
    industry = facts.get("industry_source_label")
    if facts.get("industry_mapping_approved") is not True:
        add("industry_mapping", "unknown", "An approved source-industry mapping is required; the complete taxonomy has not been supplied", "S2 p1", industry)
    if facts.get("industry_excluded_activity") is True:
        add("industry_exclusions", "fail", "Business activity falls within an explicit industry exception; lender review required", "S2 p1")
    if trade:
        overlay = product.get("overlays", [TRADE_OVERLAY])[0]
        floor("beacon_score", overlay["minimum_beacon"], "S3 p1")
        truth("beacon_verified", "S3 p1")
        age("business_started", years=overlay["minimum_business_years"], source="S3 p1")
        floor("annual_revenue", overlay["minimum_annual_revenue"], "S3 p1")
        truth("registered_entity", "S3 p1")
        truth("ein_verified", "S3 p1")
        if not overlay.get("product_mapping_confirmed"):
            add("trade_product_mapping", "conflict", "Lender must confirm which named products use the skilled-trades overlay", "S3 p1")
        requested = _number(facts.get("requested_amount"))
        if requested is not None and requested > overlay["standard_maximum"]:
            add("trade_standard_limit", "conflict", "Requested amount exceeds the standard trade snapshot; Tier 1 is a manual review branch and cannot raise the cap automatically", "S3 p1", requested, overlay["standard_maximum"])
            tier = overlay["tier_one"]
            floor("beacon_score", tier["minimum_beacon"], "S3 p1 Tier 1")
            age("business_started", years=tier["minimum_business_years"], source="S3 p1 Tier 1")
            truth("homeownership_verified", "S3 p1 Tier 1")
    else:
        base = product["shared_criteria"]
        if facts.get("equifax_score") is None and facts.get("beacon_verified") is True:
            facts = {**facts, "equifax_score": facts.get("beacon_score"), "equifax_verified": True}
        floor("equifax_score", base["minimum_equifax"], "S2 p1")
        truth("equifax_verified", "S2 p1")
        age("current_ownership_started", months=base["minimum_same_ownership_months"])
        floor("monthly_sales", base["monthly_sales_observation"], "S2 p1")
        floor("annual_revenue", base["annual_sales_observation"], "S2 p1")
        monthly, annual = _number(facts.get("monthly_sales")), _number(facts.get("annual_revenue"))
        if monthly is None or annual is None:
            for row in results:
                if row["key"] in {"monthly_sales", "annual_revenue"} and row["state"] == "fail":
                    row["state"] = "unknown"
        if monthly is not None and annual is not None and ((monthly >= base["monthly_sales_observation"]) != (annual >= base["annual_sales_observation"])):
            # Neither the AND nor OR interpretation is supplied by the sheet.
            for row in results:
                if row["key"] in {"monthly_sales", "annual_revenue"}:
                    row["state"] = "conflict"
            add("revenue_combination", "conflict", "Monthly and annual sales thresholds disagree; their AND/OR treatment requires lender clarification", "S2 p1")
    requested = _number(facts.get("requested_amount"))
    maximum = product["maximum_amount"]
    add("requested_amount", "unknown" if requested is None else "pass" if product["minimum_amount"] <= requested <= maximum else "fail", "Requested amount compared with the supplied product snapshot", "S1 p1", requested, {"minimum": product["minimum_amount"], "maximum": maximum})
    features = product["features"]
    if features.get("minimum_beacon"):
        floor("beacon_score", features["minimum_beacon"], "S1 p2")
        truth("beacon_verified", "S1 p2")
        truth("homeownership_verified", "S1 p2")
    if features.get("minimum_days_out_of_program"):
        confirmed_new = facts.get("new_merchant_guarantor_confirmed")
        days = _number(facts.get("days_out_of_arf_program"))
        add("merchant_guarantor_history", "pass" if facts.get("merchant_guarantor_eligibility_verified") is True or confirmed_new is True or days is not None and days >= features["minimum_days_out_of_program"] else "unknown" if days is None else "fail", "New merchant-guarantor status or at least 120 days out of the program requires supporting confirmation", "S1 p2", days)
    if features.get("initial_draw_fraction"):
        limit = _number(facts.get("approved_line_limit"))
        initial = _number(facts.get("requested_initial_draw"))
        minimum = max(limit * features["initial_draw_fraction"], product["minimum_draw"]) if limit is not None else None
        add("initial_draw", "not_applicable" if limit is None and initial is None else "unknown" if limit is None or initial is None else "pass" if minimum <= initial <= limit else "fail", "Initial draw uses the lender-approved line limit, never the requested draw", "S1 p2", initial, minimum)
    if features.get("public_criteria_conflict") and not features.get("public_criteria_conflict_resolved"):
        add("io_bankroll_public_conflict", "conflict", "Supplied IO-Bankroll sheet conflicts with a public-page observation; lender must identify the governing version", "S1 pp1–2; external observation W2")
    selected_payoffs = _number(facts.get("selected_competitor_payoff_count"))
    if selected_payoffs is not None and selected_payoffs > 2:
        add("competitor_payoff_count", "fail", "At most two competitors may be selected under the source snapshot; other outstanding debts remain in analysis", "S2 p1", selected_payoffs, 2)
    failures = [r for r in results if r["state"] == "fail"]
    conflicts = [r for r in results if r["state"] == "conflict"]
    missing = [r["key"] for r in results if r["state"] == "unknown"]
    status = "outside_published_criteria" if failures else "lender_clarification_required" if conflicts else "missing_information" if missing else "potentially_eligible"
    confidence = "low" if not approved or conflicts else "medium" if missing else "high"
    return {"program_key": product["program_key"], "program_name": product["name"], "version": version, "source_version": product["source_version"], "approved": approved, "status": status, "credit_decision": "not_submitted", "confidence": confidence, "confidence_basis": "Source approval, evidence completeness and unresolved conflicts; not probability of credit approval", "applied_overlays": ["arf_skilled_trades"] if trade else [], "rule_results": results, "missing_items": list(dict.fromkeys(missing)), "conflicts": conflicts, "failed_rules": failures, "strengths": [r["explanation"] for r in results if r["state"] == "pass"], "permission_blockers": [], "manual_review_required": bool(conflicts or not approved), "explanation": "Preliminary source-based routing only; the lender makes the credit decision."}


async def profile_facts(db, profile):
    from app.services.application_programs import profile_fit_context
    facts = await profile_fit_context(db, profile)
    rows = list((await db.execute(select(ApplicationExtractedFact).where(ApplicationExtractedFact.profile_id == profile.id, ApplicationExtractedFact.status == "accepted").order_by(ApplicationExtractedFact.created_at))).scalars().all())
    grouped = {}
    for row in rows:
        raw = row.value.get("value") if isinstance(row.value, dict) else row.value
        if raw is None:
            raw = row.normalized_value
        grouped.setdefault(row.field_key, []).append((raw, row))
    for key, values in grouped.items():
        if len({json.dumps(value, sort_keys=True, default=str) for value, _ in values}) != 1:
            facts[key] = None
            facts.setdefault("fact_sources", {})[key] = {"verification_status": "conflicting", "source_refs": [{"kind": "accepted_fact", "id": str(row.id)} for _, row in values]}
        else:
            facts[key] = values[-1][0]
            facts.setdefault("fact_sources", {})[key] = {"verification_status": "verified" if all(row.reviewed_by_user_id for _, row in values) else "self_reported", "source_refs": [{"kind": "accepted_fact", "id": str(row.id), "source_file_id": str(row.source_file_id) if row.source_file_id else None} for _, row in values]}
            if (key.endswith("_verified") or key.endswith("_approved") or key == "new_merchant_guarantor_confirmed") and not all(row.reviewed_by_user_id for _, row in values):
                facts[key] = None
    # These fields require explicit source-model verification; a generic or
    # self-reported credit score must never masquerade as Equifax Beacon.
    explicit_beacon = facts.get("beacon_verified") is True and (facts.get("fact_sources", {}).get("beacon_verified") or {}).get("verification_status") == "verified"
    if not explicit_beacon and (str(facts.get("credit_bureau", "")).casefold() != "equifax" or str(facts.get("credit_model", "")).casefold() != "beacon" or facts.get("credit_verified") is not True):
        facts["beacon_verified"] = None
    # Internal reviews encode explicit verified assertions, not borrower facts.
    # A completed review carries the staff identity and original requirement.
    verified_states = list((await db.execute(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id, ApplicationRequirementState.status == "verified"))).scalars().all())
    allowed_review_facts = {"equifax_verified", "beacon_verified", "homeownership_verified", "industry_mapping_approved", "merchant_guarantor_eligibility_verified"}
    for state in verified_states:
        metadata = state.provenance or {}
        key = (metadata.get("collection_config") or {}).get("assessment_fact")
        if key in allowed_review_facts and metadata.get("requirement_kind") == "human_review" and state.verified_by_user_id:
            facts[key] = True
            facts.setdefault("fact_sources", {})[key] = {"verification_status": "verified", "source_refs": [{"kind": "requirement_review", "id": str(state.id), "reviewed_by": str(state.verified_by_user_id)}]}
    return json.loads(json.dumps(facts, default=lambda value: sorted(value) if isinstance(value, set) else str(value)))


async def program_candidate(db, profile, item, playbook):
    from app.schemas.application_profile import ProgramFitCandidate
    result = evaluate_arf_facts((playbook.rules or {}).get("arf") or PRODUCT_BY_KEY[item.program_key], await profile_facts(db, profile), version=playbook.version, approved=playbook.status == "published")
    status = {"potentially_eligible": "recommended", "missing_information": "needs_information", "outside_published_criteria": "not_eligible", "lender_clarification_required": "custom_review"}[result["status"]]
    config = await get_configuration(db)
    if config.intake_mode == "referral_only" and status == "recommended":
        status = "custom_review"
        result["manual_review_required"] = True
        result["rule_results"].append({"state": "conflict", "explanation": "ARF remains referral-only until written hosted-intake authorization is recorded"})
    if profile.vertical != "main_street":
        status = "not_eligible"
    return ProgramFitCandidate(program_key=item.program_key, program_name=item.name, catalog_id=item.id, public_slug=item.public_slug, playbook_id=playbook.id, playbook_version=playbook.version, eligible=status == "recommended", recommendation_status=status, criteria_status="published", reasons=[r["explanation"] for r in result["rule_results"] if r["state"] in {"fail", "unknown", "conflict"}], scope_status="matched" if profile.vertical == "main_street" else "outside_scope", rule_state="fail" if result["failed_rules"] else "unknown" if result["missing_items"] or result["conflicts"] else "pass", rule_results=result["rule_results"], evidence_gaps=result["missing_items"], requires_review=result["manual_review_required"])


async def assessment(db, profile, *, persist=False):
    from fastapi.encoders import jsonable_encoder

    from app.models.lender import Lender
    from app.models.submission_batch import LenderOffering
    from app.services.application_programs import FINANCIAL_FIT_FIELDS, _evidence_inventory
    from app.services.bucket_evidence import analysis_usability
    from app.services.submission_batches import digest, fit_for_offering
    if persist:
        await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update())
    facts = await profile_facts(db, profile)
    config = await get_configuration(db)
    programs = list((await db.execute(select(FundingProgramCatalog).where(FundingProgramCatalog.program_key.in_(PRODUCT_BY_KEY)))).scalars().all())
    products = []
    for item in programs:
        playbook = (await db.execute(select(AIPlaybookTemplate).where(AIPlaybookTemplate.funding_program_id == item.id, AIPlaybookTemplate.is_active.is_(True)).order_by((AIPlaybookTemplate.status == "published").desc(), AIPlaybookTemplate.version.desc()).limit(1))).scalar_one_or_none()
        if playbook:
            result = evaluate_arf_facts((playbook.rules or {}).get("arf") or PRODUCT_BY_KEY[item.program_key], facts, version=playbook.version, approved=playbook.status == "published")
            if config.intake_mode == "referral_only":
                result["permission_blockers"].append("referral_only")
            products.append(result)
    states = list((await db.execute(select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id))).scalars().all())
    files, analyses, _ = await _evidence_inventory(db, profile)
    usages = Counter()
    for file in files:
        analysis = analyses.get(file.id)
        usages[analysis_usability(analysis)] += 1
    required_states = [state for state in states if state.required_level == "required"]
    missing = [{"key": state.requirement_key, "status": state.status} for state in required_states if state.status not in {"verified", "waived", "not_applicable"}]
    complete = {"status": "needs_information" if missing else "ready_for_review" if required_states else "not_assessed", "missing_items": missing, "satisfied": len(required_states) - len(missing), "total": len(required_states)}
    flags = list(dict.fromkeys(row["explanation"] for product in products for row in product["failed_rules"]))
    exceptions = list(dict.fromkeys(row["explanation"] for product in products for row in product["conflicts"]))
    selections = list((await db.execute(select(ApplicationProgramSelection).where(ApplicationProgramSelection.profile_id == profile.id, ApplicationProgramSelection.removed_at.is_(None)))).scalars().all())
    selected_keys = {selection.program_key for selection in selections}
    lenders = list((await db.execute(select(Lender).where(Lender.is_active.is_(True)).order_by(Lender.name))).scalars().all())
    offerings = list((await db.execute(select(LenderOffering).where(LenderOffering.status == "active").order_by(LenderOffering.version.desc(), LenderOffering.id))).scalars().all())
    lender_fit = []
    lender_facts = dict(facts)
    for key in FINANCIAL_FIT_FIELDS:
        if (facts.get("fact_sources", {}).get(key) or {}).get("verification_status") != "verified":
            lender_facts[key] = None
    for lender in lenders:
        options = [offering for offering in offerings if offering.lender_id == lender.id]
        offering = next((option for option in options if option.program_key in selected_keys), next((option for option in options if option.program_key == "*"), None))
        fit = fit_for_offering(offering, lender_facts, selected_keys)
        lender_fit.append({"lender_id": str(lender.id), "lender_name": lender.name, "offering_id": str(offering.id) if offering else None, "offering_version": offering.version if offering else None, "program_key": offering.program_key if offering else None, "checklist_confirmed": bool(offering and offering.checklist_confirmed), **fit})
    source = {"facts": facts, "products": [{"program_key": p["program_key"], "version": p["version"], "source_version": p["source_version"], "rules": p["rule_results"]} for p in products], "requirements": missing, "files": [{"id": str(f.id), "hash": f.content_hash, "analysis_id": str(analyses[f.id].id) if f.id in analyses else None} for f in files], "channel_version": config.version, "lender_fit": lender_fit, "offering_versions": [{"id": str(offering.id), "version": offering.version, "program_key": offering.program_key, "fit_rules": offering.fit_rules, "requirements": offering.requirements, "checklist_confirmed": offering.checklist_confirmed} for offering in offerings]}
    fingerprint = digest(source)
    payload = {"algorithm_version": ALGORITHM_VERSION, "source_fingerprint": fingerprint, "assessed_at": datetime.now(UTC), "document_usability": dict(usages), "file_completeness": complete, "products": products, "lender_fit": lender_fit, "facts": facts, "risk_flags": flags, "strengths": list(dict.fromkeys(s for p in products for s in p["strengths"])), "exceptions": exceptions, "explanation": "Preliminary assessment; lender options and submission readiness are evaluated independently."}
    for key in ("facts", "products", "lender_fit"):
        payload[key] = jsonable_encoder(payload[key])
    latest = (await db.execute(select(ApplicationAssessmentSnapshot).where(ApplicationAssessmentSnapshot.profile_id == profile.id).order_by(ApplicationAssessmentSnapshot.version.desc()).limit(1))).scalar_one_or_none()
    if latest and latest.source_fingerprint == fingerprint:
        return {"id": str(latest.id), "version": latest.version, **payload, "assessed_at": latest.assessed_at}
    version = (latest.version if latest else 0) + 1
    if persist:
        row = ApplicationAssessmentSnapshot(profile_id=profile.id, version=version, **payload)
        db.add(row)
        await db.flush()
        return {"id": str(row.id), "version": version, **payload}
    return {"id": None, "version": version, **payload}


async def public_catalog(db):
    from app.services.funding_programs import published_versions_by_program
    config = await get_configuration(db)
    output = {"channel_key": CHANNEL_KEY, "qc_role": "referral_partner", "source_version": SOURCE_VERSION, "intake_mode": config.intake_mode, "publication_state": config.publication_state, "products": [], "industries": [], "source_taxonomy_complete": False}
    if config.publication_state != "published" or not approval_valid(config, "public_copy") or not approval_valid(config, "rule_pack"):
        return output
    rows = list((await db.execute(select(FundingProgramCatalog).where(FundingProgramCatalog.program_key.in_(PRODUCT_BY_KEY), FundingProgramCatalog.status == "active").order_by(FundingProgramCatalog.display_order))).scalars().all())
    published = await published_versions_by_program(db, [r.id for r in rows])
    for row in rows:
        version = published.get(row.id)
        definition = (version.rules or {}).get("arf") if version else None
        if definition and not (version.rules or {}).get("unresolved_review_items"):
            # Only explicitly approved public fields; internal policy/conflict
            # notes and source documents never cross the public boundary.
            requirements = list((await db.execute(select(AICollectionRequirement).where(AICollectionRequirement.playbook_id == version.id).order_by(AICollectionRequirement.display_order))).scalars().all())
            public_requirements = [{"key": requirement.requirement_key, "label": requirement.label, "kind": requirement.requirement_kind, "conditional": bool(requirement.applies_when), "origin": (requirement.collection_config or {}).get("origin", "qc_packaging")} for requirement in requirements if requirement.requirement_kind != "human_review" and "borrower" in (requirement.visibility or [])]
            output["products"].append({key: definition[key] for key in ("program_key", "name", "public_slug", "title", "description", "minimum_amount", "maximum_amount", "maximum_term_months", "features", "disclosures", "source_version")} | {"version": version.version, "requirements": public_requirements})
    output["industries"] = [{"key": "plumbing", "slug": "plumbing-business", "name": "Plumbing Business Financing", "uses": ["Service vehicles", "Tools and emergency inventory", "Technicians"]}, {"key": "electrical", "slug": "electrical-contractor", "name": "Electrical Contractor Financing", "uses": ["Job-site equipment and safety gear", "Fleet", "Licensed electricians"]}, {"key": "hvac", "slug": "hvac-business", "name": "HVAC Business Financing", "uses": ["Units and parts", "Service trucks", "Seasonal technicians"]}]
    return output
