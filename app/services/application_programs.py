"""Program selection, shared evidence reconciliation, and readiness for AI Intake."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LoanStage
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import (
    ApplicationProfile,
    ApplicationProgramRequirementOverride,
    ApplicationProgramSelection,
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.bucket import (
    BucketFile,
    BucketFileAnalysis,
    BucketRequestedDocument,
    BucketUploadLink,
)
from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.funding_program import (
    ApplicationEvidencePolicySelection,
    ApplicationRequirementEvidenceDecision,
    FundingProgramScope,
)
from app.models.loan import Loan
from app.models.operator_file import BucketIntakeLink, BucketIntakeLinkFile
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.application_profile import (
    ApplicationEvidenceOptionRead,
    ApplicationEvidenceSummary,
    ApplicationProgramReadiness,
    ApplicationProgramSelectionRead,
    ApplicationProgramsPatch,
    ApplicationRequirementEvidenceRead,
    ApplicationRequirementPatch,
    ApplicationRequirementRead,
    EvidenceDecisionOverride,
    EvidencePolicySelectionRead,
    MissingItemAutomationRead,
    ProgramFitCandidate,
    ProgramReadinessItem,
)
from app.services import application_profiles as profiles
from app.services import file_events
from app.services import funding_programs as program_catalog
from app.services.activity_log import log_activity
from app.services.bucket_evidence import (
    classifications_for_requested_doc,
    effective_file_classification,
    statement_months_from_analysis,
    statement_months_from_filename,
)
from app.services.main_street_programs import intent_kind, normalize_intent
from app.services.program_rules import ProgramRuleError, evaluate_rules, validate_rules

SATISFIED_STATES = {"verified", "waived", "not_applicable"}
OPEN_UNDERWRITING_STATES = {
    "submitted",
    "collecting_docs",
    "in_underwriting",
    "term_sheet_provided",
    "approved",
}
LEVEL_RANK = {"optional": 0, "recommended": 1, "required": 2}
EXPECTED_CLASSIFICATIONS: dict[str, set[str]] = {
    "business_bank_statements_6_months": {"bank_statement"},
    "business_tax_returns_2_years": {"tax_return"},
    "ytd_p_and_l_balance_sheet": {"current_p_and_l", "profit_and_loss", "balance_sheet"},
    "business_debt_schedule": {"debt_schedule"},
    "owner_personal_financial_statement": {"personal_financial_statement"},
    "real_estate_schedule": {"real_estate_schedule"},
    "property_debt_evidence": {"collateral_debt_evidence", "payoff_or_mortgage_statement"},
    "entity_or_vesting": {"entity_or_vesting"},
    "signed_credit_authorization": {"identity", "credit_authorization"},
    "current_advance_terms": {"floorplan_mca_inventory", "loan_agreement"},
}
BUSINESS_ENTITY_REQUIREMENTS = {
    "business_bank_statements_6_months",
    "business_tax_returns_2_years",
    "ytd_p_and_l_balance_sheet",
    "business_debt_schedule",
    "current_advance_terms",
}

POLICY_KEYS = {"business_baseline", "real_estate_baseline", "mca_baseline"}


def now() -> datetime:
    return datetime.now(UTC)


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("x", "").strip())
    except (TypeError, ValueError):
        return None


def _deep_values(value: Any, wanted_keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in wanted_keys:
                found.append(item)
            found.extend(_deep_values(item, wanted_keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_deep_values(item, wanted_keys))
    return found


def _fact_presence(value: Any, keys: set[str]) -> bool | None:
    values = _deep_values(value, {key.casefold() for key in keys})
    if not values:
        return None
    return any(item not in (None, "", False, 0, [], {}) for item in values)


def _is_lending_applicable(context: dict[str, Any]) -> bool:
    return context.get("intent_kind") not in {"non_lending", "route_out"}


async def _evidence_inventory(
    db: AsyncSession, profile: ApplicationProfile
) -> tuple[list[BucketFile], dict[uuid.UUID, BucketFileAnalysis], set[str]]:
    state = await profiles.evidence_state(db, profile)
    ids = [item.id for item in state.files]
    if not ids:
        return [], {}, set()
    files = list(
        (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.id.in_(ids),
                    BucketFile.deleted_at.is_(None),
                    BucketFile.status == "uploaded",
                )
            )
        )
        .scalars()
        .all()
    )
    analyses = list(
        (
            await db.execute(
                select(BucketFileAnalysis)
                .where(BucketFileAnalysis.bucket_file_id.in_(ids))
                .order_by(
                    BucketFileAnalysis.bucket_file_id,
                    BucketFileAnalysis.analysis_version.desc(),
                    BucketFileAnalysis.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID, BucketFileAnalysis] = {}
    for analysis in analyses:
        latest.setdefault(analysis.bucket_file_id, analysis)
    classifications = {
        analysis.classification
        for analysis in latest.values()
        if analysis.status == "completed" and analysis.classification
    }
    return files, latest, classifications


async def profile_fit_context(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    intake = (
        await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    )
    files, latest_analyses, _classifications = await _evidence_inventory(db, profile)
    evidence_links = list(
        (
            await db.execute(
                select(ApplicationRequirementEvidence)
                .join(
                    ApplicationRequirementState,
                    ApplicationRequirementState.id
                    == ApplicationRequirementEvidence.requirement_state_id,
                )
                .where(
                    ApplicationRequirementState.profile_id == profile.id,
                    ApplicationRequirementEvidence.removed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    latest_decisions = await _latest_evidence_decisions(db, evidence_links)
    accepted_file_ids = {
        link.file_id
        for link in evidence_links
        if (
            latest_decisions.get(link.id) is not None
            and latest_decisions[link.id].decision == "accepted"
        )
        or (latest_decisions.get(link.id) is None and link.verified_at is not None)
    }
    accepted_classifications = {
        analysis.classification
        for file_id, analysis in latest_analyses.items()
        if file_id in accepted_file_ids
        and analysis.status == "completed"
        and analysis.classification
    }
    snapshot = dict(intake.result_snapshot or {}) if intake else {}
    metrics = snapshot.get("key_metrics") if isinstance(snapshot.get("key_metrics"), dict) else {}
    revenue = _float(
        metrics.get("ytd_annualized_revenue")
        or metrics.get("annualized_revenue")
        or metrics.get("annual_revenue")
        or metrics.get("gross_revenue")
    )
    annualized_deposits = _float(
        metrics.get("annualized_adjusted_deposits") or metrics.get("annualized_deposits")
    )
    credit_score = _float(
        (intake.estimated_credit_score if intake else None)
        or metrics.get("estimated_credit_score")
        or metrics.get("credit_score")
        or metrics.get("fico")
    )
    intake_state = dict(intake.intake_state or {}) if intake else {}
    main_street_details = intake_state.get("main_street_details")
    main_street_details = main_street_details if isinstance(main_street_details, dict) else {}
    stated_intent = normalize_intent(main_street_details.get("intent"))
    stated_intent_kind = (
        intent_kind(stated_intent) if profile.vertical == "main_street" else "lending"
    )
    combined_intake_data = {
        "intake_state": intake_state,
        "result_snapshot": snapshot,
        "asset_rows": list(intake.asset_rows or []) if intake else [],
    }
    business_age_values = _deep_values(
        combined_intake_data,
        {"years_in_business", "time_in_business_years", "business_age_years"},
    )
    business_age = next(
        (_float(value) for value in business_age_values if _float(value) is not None),
        None,
    )
    declared_collateral = _fact_presence(
        combined_intake_data,
        {
            "real_estate_schedule",
            "collateral",
            "collateral_value",
            "property_address",
            "property_value",
        },
    )
    mca_obligations = _fact_presence(
        combined_intake_data,
        {
            "mca_obligations",
            "mca_balance",
            "current_advances",
            "merchant_cash_advances",
            "advance_balance",
        },
    )
    floorplan_inventory = _fact_presence(
        combined_intake_data,
        {
            "floorplan",
            "floorplan_balance",
            "inventory",
            "inventory_value",
            "vehicle_inventory",
        },
    )
    if intake and "mca" in str(intake.variant).casefold():
        mca_obligations = True
    return {
        "vertical": profile.vertical,
        "intake_variant": intake.variant if intake else None,
        "intent": stated_intent if profile.vertical == "main_street" else None,
        "intent_kind": stated_intent_kind,
        "funding_category": profile.funding_category,
        "entity_type": profile.entity_type,
        "industry": profile.industry,
        "subindustry": profile.subindustry,
        "industry_key": profile.industry,
        "naics_code": profile.naics_code,
        "loan_purpose": intake.loan_purpose if intake else None,
        "requested_amount": _float(intake.requested_loan_amount) if intake else None,
        "business_age_years": business_age,
        "revenue": revenue,
        "annual_revenue": revenue,
        "annualized_deposits": annualized_deposits,
        "deposits": annualized_deposits,
        "credit_score": credit_score,
        "estimated_credit_score": credit_score,
        "dscr": _float(metrics.get("estimated_dscr") or metrics.get("dscr")),
        "cash_flow": _float(
            metrics.get("estimated_ebitda_or_cash_flow") or metrics.get("cash_flow")
        ),
        "debt_burden": _float(metrics.get("estimated_debt_burden") or metrics.get("debt_burden")),
        "liquid_assets": _float(
            metrics.get("pfs_total_liquid_assets") or metrics.get("liquid_assets")
        ),
        "tax_returns_available": "tax_return" in accepted_classifications,
        "bank_statements_available": "bank_statement" in accepted_classifications,
        "evidence_count": len(files),
        "evidence_available": accepted_classifications,
        "declared_collateral": declared_collateral,
        "mca_obligations_present": mca_obligations,
        "floorplan_inventory_present": floorplan_inventory,
    }


def _scope_match(
    scope: FundingProgramScope,
    context: dict[str, Any],
) -> tuple[bool | None, list[str]]:
    """Return True, False, or unknown for one hard catalog scope."""

    unknown: list[str] = []
    variant = str(context.get("intake_variant") or "").casefold()
    variants = {str(value).casefold() for value in scope.intake_variants or []}
    if variants:
        if not variant:
            unknown.append("intake variant")
        elif variant not in variants:
            return False, ["Intake variant is outside this product scope"]

    intent = str(context.get("intent") or "").casefold()
    intents = {str(value).casefold() for value in scope.intent_keys or []}
    if intents:
        if not intent:
            unknown.append("funding purpose")
        elif intent not in intents:
            return False, ["Funding purpose is outside this product scope"]

    industry = str(context.get("industry_key") or "").casefold()
    naics = str(context.get("naics_code") or "").strip()
    industry_keys = {str(value).casefold() for value in scope.industry_keys or []}
    naics_prefixes = [str(value) for value in scope.naics_prefixes or []]
    if industry_keys or naics_prefixes:
        if not industry and not naics:
            unknown.append("industry or NAICS")
        elif industry not in industry_keys and not any(
            naics.startswith(prefix) for prefix in naics_prefixes
        ):
            return False, ["Industry is outside this product scope"]

    for key in scope.required_fact_keys or []:
        actual = context.get(str(key))
        if actual is False:
            return False, [f"Required {str(key).replace('_', ' ')} is not present"]
        if actual in (None, "", [], {}):
            unknown.append(str(key).replace("_", " "))

    if unknown:
        return None, [f"Needs {', '.join(dict.fromkeys(unknown))}"]
    return True, []


def _catalog_scope_match(
    scopes: list[FundingProgramScope],
    context: dict[str, Any],
) -> tuple[bool | None, list[str]]:
    relevant = [scope for scope in scopes if scope.vertical == context.get("vertical")]
    if not relevant:
        return False, ["Product is not offered for this vertical"]
    outcomes = [_scope_match(scope, context) for scope in relevant]
    matched = next((item for item in outcomes if item[0] is True), None)
    if matched:
        return matched
    unknown = [reason for outcome, reasons in outcomes if outcome is None for reason in reasons]
    if unknown:
        return None, list(dict.fromkeys(unknown))
    return False, [reason for _outcome, reasons in outcomes for reason in reasons]


def _rule_fields(node: Any) -> set[str]:
    if not isinstance(node, dict):
        return set()
    fields = {str(node["field"])} if isinstance(node.get("field"), str) else set()
    for key in ("all", "any"):
        for child in node.get(key) or []:
            fields.update(_rule_fields(child))
    if "not" in node:
        fields.update(_rule_fields(node["not"]))
    return fields


def _context_field(context: dict[str, Any], field: str) -> Any:
    current: Any = context
    for part in field.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


async def published_candidates(
    db: AsyncSession, profile: ApplicationProfile
) -> list[ProgramFitCandidate]:
    context = await profile_fit_context(db, profile)
    if context.get("intent_kind") in {"non_lending", "route_out"}:
        return []

    catalog = await program_catalog.catalog_rows(db)
    scopes = await program_catalog.scopes_by_program(db, [row.id for row in catalog])
    playbooks = list(
        (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.playbook_type == "loan_product",
                    AIPlaybookTemplate.status == "published",
                    AIPlaybookTemplate.is_active.is_(True),
                    AIPlaybookTemplate.funding_program_id.in_([row.id for row in catalog])
                    if catalog
                    else False,
                )
            )
        )
        .scalars()
        .all()
    )
    playbooks.sort(
        key=lambda row: (
            1 if row.owner_type == "funding" else 0,
            row.version,
            row.published_at or row.created_at,
            str(row.id),
        ),
        reverse=True,
    )
    latest: dict[uuid.UUID, AIPlaybookTemplate] = {}
    for row in playbooks:
        if row.funding_program_id is not None:
            latest.setdefault(row.funding_program_id, row)

    candidates: list[ProgramFitCandidate] = []
    for item in catalog:
        scope_state, scope_reasons = _catalog_scope_match(scopes.get(item.id, []), context)
        if scope_state is False:
            continue
        playbook = latest.get(item.id)
        if playbook is None:
            candidates.append(
                ProgramFitCandidate(
                    program_key=item.program_key,
                    program_name=item.name,
                    catalog_id=item.id,
                    public_slug=item.public_slug,
                    eligible=False,
                    recommendation_status="criteria_unavailable",
                    reasons=["Published underwriting criteria are not available"],
                )
            )
            continue
        try:
            validate_rules(playbook.rules or {})
            has_fit_rule = bool((playbook.rules or {}).get("fit"))
            result = evaluate_rules(playbook.rules or {}, context) if has_fit_rule else None
        except ProgramRuleError:
            result = None
            has_fit_rule = False
        priority = int((playbook.rules or {}).get("priority") or 0)
        missing_fields = [
            field
            for field in sorted(_rule_fields((playbook.rules or {}).get("fit")))
            if _context_field(context, field) in (None, "", [], {})
        ]
        if not has_fit_rule or result is None:
            recommendation_status = "criteria_unavailable"
            reasons = ["Published fit criteria are unavailable or invalid"]
            eligible = False
        elif scope_state is None or (not result.matched and missing_fields):
            recommendation_status = "needs_information"
            reasons = [
                *scope_reasons,
                *[f"Needs {field.replace('_', ' ')}" for field in missing_fields],
            ]
            eligible = False
        elif result.matched:
            recommendation_status = "recommended"
            reasons = result.reasons
            eligible = True
        else:
            recommendation_status = "not_eligible"
            reasons = result.reasons
            eligible = False
        candidates.append(
            ProgramFitCandidate(
                program_key=item.program_key,
                program_name=item.name,
                catalog_id=item.id,
                public_slug=item.public_slug,
                playbook_id=playbook.id,
                playbook_version=playbook.version,
                eligible=eligible,
                recommendation_status=recommendation_status,
                fit_score=round((result.confidence if result else 0) * 100, 2),
                confidence=result.confidence if result else 0,
                priority=priority,
                reasons=list(dict.fromkeys(reasons)),
            )
        )
    rank = {
        "recommended": 0,
        "needs_information": 1,
        "criteria_unavailable": 2,
        "not_eligible": 3,
    }
    return sorted(
        candidates,
        key=lambda item: (
            rank[item.recommendation_status],
            -item.confidence,
            -item.priority,
            item.program_key,
        ),
    )


def _automatic_candidate(
    profile: ApplicationProfile,
    candidates: list[ProgramFitCandidate],
) -> ProgramFitCandidate | None:
    eligible = next((item for item in candidates if item.eligible), None)
    return eligible


async def active_selections(
    db: AsyncSession, profile_id: uuid.UUID
) -> list[ApplicationProgramSelection]:
    return list(
        (
            await db.execute(
                select(ApplicationProgramSelection)
                .where(
                    ApplicationProgramSelection.profile_id == profile_id,
                    ApplicationProgramSelection.removed_at.is_(None),
                )
                .order_by(
                    ApplicationProgramSelection.selected_at, ApplicationProgramSelection.program_key
                )
            )
        )
        .scalars()
        .all()
    )


async def profile_for_chat_scope(
    db: AsyncSession,
    *,
    bucket_id: uuid.UUID,
    intake_id: uuid.UUID | None = None,
    upload_link_id: uuid.UUID | None = None,
) -> ApplicationProfile | None:
    """Resolve chat context through explicit lineage and reject ambiguous buckets."""
    if intake_id is not None:
        return (
            await db.execute(
                select(ApplicationProfile).where(
                    ApplicationProfile.intake_id == intake_id,
                    ApplicationProfile.primary_bucket_id == bucket_id,
                )
            )
        ).scalar_one_or_none()
    if upload_link_id is not None:
        rows = list(
            (
                await db.execute(
                    select(ApplicationProfile)
                    .join(
                        PublicUnderwritingIntake,
                        PublicUnderwritingIntake.id == ApplicationProfile.intake_id,
                    )
                    .where(
                        ApplicationProfile.primary_bucket_id == bucket_id,
                        PublicUnderwritingIntake.bucket_upload_link_id == upload_link_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(rows) == 1:
            return rows[0]
        if rows:
            return None
    rows = list(
        (
            await db.execute(
                select(ApplicationProfile).where(ApplicationProfile.primary_bucket_id == bucket_id)
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


async def _auto_select(
    db: AsyncSession,
    profile: ApplicationProfile,
    candidates: list[ProgramFitCandidate],
) -> list[ApplicationProgramSelection]:
    active = await active_selections(db, profile.id)
    if active or profile.program_selection_mode != "auto":
        return active
    candidate = _automatic_candidate(profile, candidates)
    if candidate is None or candidate.playbook_id is None or candidate.playbook_version is None:
        return []
    row = ApplicationProgramSelection(
        profile_id=profile.id,
        playbook_id=candidate.playbook_id,
        playbook_version=candidate.playbook_version,
        program_key=candidate.program_key,
        program_name=candidate.program_name,
        source="ai_auto",
        fit_score=candidate.fit_score,
        fit_confidence=candidate.confidence,
        fit_reasons=candidate.reasons,
    )
    db.add(row)
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        None,
        "programs.auto_selected",
        f"Pinned {candidate.program_name} version {candidate.playbook_version} from published criteria",
        target_type="application_program",
        target_id=row.id,
        metadata={
            "program_key": candidate.program_key,
            "playbook_id": str(candidate.playbook_id),
            "playbook_version": candidate.playbook_version,
            "fit_confidence": candidate.confidence,
        },
    )
    return [row]


async def set_programs(
    db: AsyncSession,
    profile: ApplicationProfile,
    payload: ApplicationProgramsPatch,
    user: User,
) -> None:
    active = await active_selections(db, profile.id)
    timestamp = now()
    if payload.return_to_ai:
        for selection in active:
            selection.removed_at = timestamp
            selection.removed_by_user_id = user.id
        profile.program_selection_mode = "auto"
        profile.program_selection_locked_at = None
        profile.program_selection_locked_by_user_id = None
        await db.flush()
        await _auto_select(db, profile, await published_candidates(db, profile))
        return

    wanted = list(dict.fromkeys(key.strip() for key in payload.program_keys if key.strip()))
    candidates = {
        candidate.program_key: candidate for candidate in await published_candidates(db, profile)
    }
    missing = [key for key in wanted if key not in candidates]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Published program not found: {', '.join(missing)}",
        )
    unavailable = [
        key
        for key in wanted
        if candidates[key].playbook_id is None
        or candidates[key].playbook_version is None
        or candidates[key].recommendation_status == "criteria_unavailable"
    ]
    if unavailable:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Published criteria are unavailable for: {', '.join(unavailable)}",
        )
    ineligible = [key for key in wanted if not candidates[key].eligible]
    if ineligible and len((payload.reason or "").strip()) < 8:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A reviewed reason is required to select an AI-ineligible program",
        )
    current = {selection.program_key: selection for selection in active}
    for key, selection in current.items():
        if key not in wanted:
            selection.removed_at = timestamp
            selection.removed_by_user_id = user.id
    for key in wanted:
        if key in current:
            continue
        candidate = candidates[key]
        if candidate.playbook_id is None or candidate.playbook_version is None:
            continue
        db.add(
            ApplicationProgramSelection(
                profile_id=profile.id,
                playbook_id=candidate.playbook_id,
                playbook_version=candidate.playbook_version,
                program_key=key,
                program_name=candidate.program_name,
                source="operator",
                fit_score=candidate.fit_score,
                fit_confidence=candidate.confidence,
                fit_reasons=candidate.reasons,
                selected_by_user_id=user.id,
                selected_at=timestamp,
            )
        )
    profile.program_selection_mode = "manual"
    profile.program_selection_locked_at = timestamp
    profile.program_selection_locked_by_user_id = user.id
    await db.flush()


async def _selection_requirements(
    db: AsyncSession,
    selections: list[ApplicationProgramSelection],
    context: dict[str, Any],
) -> dict[uuid.UUID, list[AICollectionRequirement]]:
    if not selections:
        return {}
    rows = list(
        (
            await db.execute(
                select(AICollectionRequirement)
                .where(
                    AICollectionRequirement.playbook_id.in_(
                        [item.playbook_id for item in selections]
                    )
                )
                .order_by(
                    AICollectionRequirement.display_order, AICollectionRequirement.requirement_key
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[AICollectionRequirement]] = defaultdict(list)
    for row in rows:
        if _legacy_condition_matches(row.applies_when, context):
            grouped[row.playbook_id].append(row)
    return grouped


async def _policy_requirements(
    db: AsyncSession,
    policies: list[ApplicationEvidencePolicySelection],
    context: dict[str, Any],
) -> dict[uuid.UUID, list[AICollectionRequirement]]:
    if not policies:
        return {}
    rows = list(
        (
            await db.execute(
                select(AICollectionRequirement)
                .where(
                    AICollectionRequirement.playbook_id.in_([item.playbook_id for item in policies])
                )
                .order_by(
                    AICollectionRequirement.display_order,
                    AICollectionRequirement.requirement_key,
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[AICollectionRequirement]] = defaultdict(list)
    for row in rows:
        if _legacy_condition_matches(row.applies_when, context):
            grouped[row.playbook_id].append(row)
    return grouped


def _legacy_condition_matches(condition: dict | None, context: dict[str, Any]) -> bool:
    if not condition:
        return True
    if any(key in condition for key in ("all", "any", "not", "field", "op")):
        try:
            return evaluate_rules({"fit": condition}, context).matched
        except ProgramRuleError:
            return False
    for key, expected in condition.items():
        actual = context.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _client_visible(requirement: AICollectionRequirement) -> bool:
    return bool({"borrower", "client"}.intersection(set(requirement.visibility or [])))


async def _requested_document(
    db: AsyncSession,
    profile: ApplicationProfile,
    requirement: AICollectionRequirement,
    program_keys: list[str],
    policy_keys: list[str],
) -> BucketRequestedDocument | None:
    if profile.primary_bucket_id is None or not _client_visible(requirement):
        return None
    requested = (
        await db.execute(
            select(BucketRequestedDocument)
            .where(
                BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                BucketRequestedDocument.requirement_key == requirement.requirement_key,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if requested is None:
        requested = (
            await db.execute(
                select(BucketRequestedDocument)
                .where(
                    BucketRequestedDocument.bucket_id == profile.primary_bucket_id,
                    BucketRequestedDocument.name == requirement.label,
                )
                .order_by(BucketRequestedDocument.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    source = {
        "kind": "program_readiness",
        "program_keys": program_keys,
        "policy_keys": policy_keys,
        "playbook_id": str(requirement.playbook_id),
    }
    if requested is None:
        requested = BucketRequestedDocument(
            bucket_id=profile.primary_bucket_id,
            name=requirement.label,
            category=requirement.category,
            description=requirement.objective_text or requirement.ai_request_message_template,
            required=requirement.required_level == "required",
            allow_multiple_files=True,
            status="requested",
            is_custom=False,
            requirement_key=requirement.requirement_key,
            requirement_source=source,
        )
        db.add(requested)
        await db.flush()
    else:
        requested.name = requirement.label
        requested.category = requirement.category
        requested.description = (
            requirement.objective_text or requirement.ai_request_message_template
        )
        requested.required = requirement.required_level == "required"
        requested.allow_multiple_files = True
        requested.requirement_key = requirement.requirement_key
        requested.requirement_source = source
        if requested.status == "not_applicable":
            requested.status = "requested"
    return requested


def _expected_classes(requirement: AICollectionRequirement) -> set[str]:
    return EXPECTED_CLASSIFICATIONS.get(requirement.requirement_key, set()).union(
        classifications_for_requested_doc(requirement.label, requirement.category)
    )


def _tax_years(file: BucketFile, analysis: BucketFileAnalysis | None) -> set[str]:
    values = [file.file_name]
    if analysis and isinstance(analysis.analysis, dict):
        facts = analysis.analysis.get("key_facts") or {}
        values.extend(str(facts.get(key) or "") for key in ("tax_year", "year", "period"))
    years: set[str] = set()
    for value in values:
        years.update(re.findall(r"\b(20\d{2})\b", str(value)))
    return years


def _analysis_supports(analysis: BucketFileAnalysis | None) -> set[str]:
    if analysis is None or not isinstance(analysis.analysis, dict):
        return set()
    raw = analysis.analysis.get("baseline_categories_supported") or []
    if not isinstance(raw, list):
        return set()
    return {re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_") for value in raw if value}


def _coverage_for_files(
    requirement: AICollectionRequirement,
    files: list[BucketFile],
    analyses: dict[uuid.UUID, BucketFileAnalysis],
) -> tuple[bool, dict[str, Any]]:
    expected = _expected_classes(requirement)
    coverage: dict[str, Any] = {
        "matched_files": len(files),
        "expected_classifications": sorted(expected),
        "current": len(files),
        "required": 1,
        "unit": "documents",
    }
    complete = bool(files)
    if requirement.requirement_key == "business_bank_statements_6_months":
        months: set[str] = set()
        unknown_period_files = 0
        for file in files:
            file_months: set[str] = set()
            if file.statement_period:
                file_months.add(file.statement_period)
            file_months.update(statement_months_from_filename(file.file_name))
            analysis = analyses.get(file.id)
            file_months.update(
                statement_months_from_analysis(analysis.analysis if analysis else None)
            )
            if file_months:
                months.update(file_months)
            else:
                unknown_period_files += 1
        current = len(months)
        coverage.update(
            {
                "months": sorted(months),
                "unknown_period_files": unknown_period_files,
                "required_months": 6,
                "current": current,
                "required": 6,
                "unit": "months",
            }
        )
        complete = current >= 6
    elif requirement.requirement_key == "business_tax_returns_2_years":
        years: set[str] = set()
        unknown_year_files = 0
        for file in files:
            file_years = _tax_years(file, analyses.get(file.id))
            if file_years:
                years.update(file_years)
            else:
                unknown_year_files += 1
        current = len(years)
        coverage.update(
            {
                "years": sorted(years),
                "unknown_year_files": unknown_year_files,
                "required_years": 2,
                "current": current,
                "required": 2,
                "unit": "years",
            }
        )
        complete = current >= 2
    elif requirement.requirement_key == "ytd_p_and_l_balance_sheet":
        classifications = set()
        support_categories: set[str] = set()
        for file in files:
            analysis = analyses.get(file.id)
            classifications.add(effective_file_classification(file.file_name, analysis))
            support_categories.update(_analysis_supports(analysis))
        has_profit_and_loss = bool(
            {"current_p_and_l", "profit_and_loss"}.intersection(classifications)
        ) or bool(
            {"current_p_and_l", "profit_and_loss", "p_and_l", "income_statement"}.intersection(
                support_categories
            )
        )
        has_balance_sheet = (
            "balance_sheet" in classifications or "balance_sheet" in support_categories
        )
        combined_template = any(
            "profit-loss-balance-sheet" in file.file_name.casefold()
            or "p&l and balance sheet" in file.file_name.casefold()
            for file in files
        )
        current = 2 if combined_template else int(has_profit_and_loss) + int(has_balance_sheet)
        coverage.update(
            {
                "classifications": sorted(value for value in classifications if value),
                "profit_and_loss": has_profit_and_loss or combined_template,
                "balance_sheet": has_balance_sheet or combined_template,
                "current": current,
                "required": 2,
                "unit": "document types",
            }
        )
        complete = current >= 2
    coverage["complete"] = complete
    return complete, coverage


def _matching_evidence_files(
    requirement: AICollectionRequirement,
    requested: BucketRequestedDocument | None,
    files: list[BucketFile],
    analyses: dict[uuid.UUID, BucketFileAnalysis],
    *,
    preferred_file_ids: set[uuid.UUID] | None = None,
    trust_preferred: bool = False,
) -> tuple[list[BucketFile], bool, dict[str, Any]]:
    expected = _expected_classes(requirement)
    preferred_ids = preferred_file_ids or set()
    explicit = []
    for file in files:
        if not requested or file.requested_document_id != requested.id:
            continue
        analysis = analyses.get(file.id)
        # An explicit request link is strong provenance, but it must not allow
        # a completed classifier result for a different document type to pass.
        if not (
            analysis
            and analysis.status == "completed"
            and analysis.classification
            and analysis.classification not in expected
        ):
            explicit.append(file)
    classified = [
        file
        for file in files
        if (analysis := analyses.get(file.id))
        and analysis.status == "completed"
        and analysis.classification in expected
    ]
    matches = list({file.id: file for file in [*explicit, *classified]}.values())
    if trust_preferred:
        preferred = [file for file in files if file.id in preferred_ids]
        matches = list({file.id: file for file in [*preferred, *matches]}.values())
    matches.sort(
        key=lambda file: (file.id in preferred_ids, file.created_at),
        reverse=True,
    )
    if not matches:
        complete, coverage = _coverage_for_files(requirement, [], analyses)
        return [], complete, coverage
    complete, coverage = _coverage_for_files(requirement, matches, analyses)
    return matches, complete, coverage


def _matching_evidence(
    requirement: AICollectionRequirement,
    requested: BucketRequestedDocument | None,
    files: list[BucketFile],
    analyses: dict[uuid.UUID, BucketFileAnalysis],
    *,
    preferred_file_id: uuid.UUID | None = None,
    trust_preferred: bool = False,
) -> tuple[BucketFile | None, bool, dict[str, Any]]:
    """Compatibility wrapper for callers that only need the primary match."""

    matches, complete, coverage = _matching_evidence_files(
        requirement,
        requested,
        files,
        analyses,
        preferred_file_ids={preferred_file_id} if preferred_file_id else set(),
        trust_preferred=trust_preferred,
    )
    return (matches[0] if matches else None), complete, coverage


def _filename_suggestions(
    requirement: AICollectionRequirement,
    files: list[BucketFile],
    analyses: dict[uuid.UUID, BucketFileAnalysis],
) -> list[BucketFile]:
    """Surface high-signal legacy uploads without silently verifying them."""

    expected = _expected_classes(requirement)
    suggestions: list[BucketFile] = []
    for file in files:
        analysis = analyses.get(file.id)
        if analysis and analysis.status == "completed" and analysis.classification:
            continue
        if effective_file_classification(file.file_name) in expected:
            suggestions.append(file)
    return suggestions


async def _sync_requirement_evidence(
    db: AsyncSession,
    state: ApplicationRequirementState,
    inventory: dict[uuid.UUID, BucketFile],
    desired_sources: dict[uuid.UUID, str],
) -> list[ApplicationRequirementEvidence]:
    rows = list(
        (
            await db.execute(
                select(ApplicationRequirementEvidence).where(
                    ApplicationRequirementEvidence.requirement_state_id == state.id
                )
            )
        )
        .scalars()
        .all()
    )
    active = {row.file_id: row for row in rows if row.removed_at is None}
    excluded = {
        row.file_id
        for row in rows
        if row.removed_at is not None and row.removed_by_user_id is not None
    }
    timestamp = now()
    for file_id, row in list(active.items()):
        if file_id not in inventory or (
            row.source != "operator" and file_id not in desired_sources
        ):
            row.removed_at = timestamp
            row.reason = "Evidence no longer matches this requirement"
            active.pop(file_id)
    for file_id, source in desired_sources.items():
        if file_id in excluded and file_id not in active:
            continue
        row = active.get(file_id)
        if row is None:
            row = ApplicationRequirementEvidence(
                requirement_state_id=state.id,
                file_id=file_id,
                source=source,
                linked_at=timestamp,
                provenance={"source": source},
            )
            db.add(row)
            active[file_id] = row
        elif row.source == "filename_suggestion" and source == "automatic":
            row.source = "automatic"
            row.provenance = {"source": source}
    await db.flush()
    return sorted(
        active.values(),
        key=lambda row: inventory[row.file_id].created_at,
        reverse=True,
    )


def _normalized_entity(value: object) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    suffixes = {
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "ltd",
        "limited",
        "pllc",
    }
    while words and words[-1] in suffixes:
        words.pop()
    return "".join(words)


def _analysis_fact(analysis: BucketFileAnalysis | None, key: str) -> object | None:
    if analysis is None or not isinstance(analysis.analysis, dict):
        return None
    profile_facts = analysis.analysis.get("profile_facts")
    if not isinstance(profile_facts, dict):
        return None
    value = profile_facts.get(key)
    return value.get("value") if isinstance(value, dict) else value


def _analysis_business_entity(analysis: BucketFileAnalysis | None) -> object | None:
    entity = _analysis_fact(analysis, "legal_entity_name")
    if entity:
        return entity
    if analysis is None or not isinstance(analysis.analysis, dict):
        return None
    key_facts = analysis.analysis.get("key_facts")
    if not isinstance(key_facts, dict):
        return None
    return key_facts.get("business_name") or key_facts.get("account_holder")


def _blocking_analysis_text(analysis: BucketFileAnalysis) -> str:
    detail = analysis.analysis if isinstance(analysis.analysis, dict) else {}
    values = [*(detail.get("limitations") or []), *(detail.get("red_flags") or [])]
    return " ".join(str(value).casefold() for value in values)


def _automatic_evidence_decision(
    *,
    requirement: AICollectionRequirement,
    file: BucketFile,
    analysis: BucketFileAnalysis | None,
    expected_entity: str | None,
    duplicate_content: bool,
) -> tuple[str, str, str, str | None]:
    if duplicate_content:
        return (
            "rejected",
            "duplicate",
            "An identical active copy is already linked and contributes coverage only once.",
            analysis.confidence if analysis else None,
        )
    if analysis is None or analysis.status in {"pending", "running"}:
        return (
            "processing",
            "analysis_pending",
            "AI extraction and document validation are still running.",
            analysis.confidence if analysis else None,
        )
    if analysis.status == "failed":
        return (
            "failed",
            "analysis_failed",
            analysis.error or "AI document analysis failed and can be retried.",
            analysis.confidence,
        )
    if analysis.status == "skipped":
        if analysis.skip_reason == "zip_parent_archive":
            return (
                "needs_more",
                "archive_container",
                "The ZIP container does not count as evidence; its extracted files are reviewed individually.",
                analysis.confidence,
            )
        return (
            "rejected",
            "unreadable",
            analysis.skip_detail or "The file could not be read for evidence analysis.",
            analysis.confidence,
        )
    if file.content_hash and analysis.content_hash != file.content_hash:
        return (
            "processing",
            "analysis_stale",
            "The file changed and its current content is being analyzed.",
            analysis.confidence,
        )

    expected = _expected_classes(requirement)
    classification = str(analysis.classification or "")
    if classification == "unreadable":
        return (
            "rejected",
            "unreadable",
            "The document is not readable enough to support this requirement.",
            analysis.confidence,
        )
    if not expected:
        return (
            "needs_more",
            "criteria_unavailable",
            "Published criteria do not define an automatic document classification for this requirement.",
            analysis.confidence,
        )
    if classification not in expected:
        return (
            "rejected",
            "wrong_document",
            f"AI classified this as {classification or 'an unknown document type'}, not evidence for this requirement.",
            analysis.confidence,
        )
    if str(analysis.confidence or "").casefold() != "high":
        return (
            "needs_more",
            "low_confidence",
            "The document type could not be validated with high confidence.",
            analysis.confidence,
        )

    extracted_entity = _analysis_business_entity(analysis)
    wanted_entity = _normalized_entity(expected_entity)
    found_entity = _normalized_entity(extracted_entity)
    if wanted_entity and not found_entity:
        return (
            "needs_more",
            "entity_unconfirmed",
            "AI could not confirm that this document belongs to the application entity.",
            analysis.confidence,
        )
    if wanted_entity and found_entity and wanted_entity != found_entity:
        return (
            "rejected",
            "wrong_entity",
            f"The document names {extracted_entity}, which does not match the application entity.",
            analysis.confidence,
        )

    blocking_text = _blocking_analysis_text(analysis)
    if any(
        token in blocking_text
        for token in ("tamper", "altered", "fraud", "illegible", "unreadable")
    ):
        return (
            "rejected",
            "integrity_or_readability",
            "AI found a document-integrity or readability issue that prevents acceptance.",
            analysis.confidence,
        )
    if any(
        token in blocking_text
        for token in ("missing page", "pages missing", "incomplete", "cut off", "partial document")
    ):
        return (
            "needs_more",
            "incomplete",
            "The document appears incomplete and does not yet support full evidence coverage.",
            analysis.confidence,
        )

    if requirement.requirement_key == "business_bank_statements_6_months":
        months = statement_months_from_analysis(analysis.analysis)
        months.update(statement_months_from_filename(file.file_name))
        if file.statement_period:
            months.add(file.statement_period)
        if not months:
            return (
                "needs_more",
                "wrong_period",
                "AI could not establish the statement month, so this file cannot increase coverage.",
                analysis.confidence,
            )
    if requirement.requirement_key == "business_tax_returns_2_years" and not _tax_years(
        file, analysis
    ):
        return (
            "needs_more",
            "wrong_period",
            "AI could not establish the tax year, so this file cannot increase coverage.",
            analysis.confidence,
        )
    return (
        "accepted",
        "validated",
        "AI validated the document type, entity, readable content, and applicable period.",
        analysis.confidence,
    )


async def _latest_evidence_decisions(
    db: AsyncSession,
    links: list[ApplicationRequirementEvidence],
) -> dict[uuid.UUID, ApplicationRequirementEvidenceDecision]:
    if not links:
        return {}
    rows = list(
        (
            await db.execute(
                select(ApplicationRequirementEvidenceDecision)
                .where(
                    ApplicationRequirementEvidenceDecision.requirement_evidence_id.in_(
                        [link.id for link in links]
                    )
                )
                .order_by(
                    ApplicationRequirementEvidenceDecision.created_at.desc(),
                    ApplicationRequirementEvidenceDecision.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID, ApplicationRequirementEvidenceDecision] = {}
    for row in rows:
        latest.setdefault(row.requirement_evidence_id, row)
    return latest


async def _reconcile_evidence_decisions(
    db: AsyncSession,
    *,
    requirement: AICollectionRequirement,
    links: list[ApplicationRequirementEvidence],
    inventory: dict[uuid.UUID, BucketFile],
    analyses: dict[uuid.UUID, BucketFileAnalysis],
    expected_entity: str | None,
    criteria_version: int,
) -> dict[uuid.UUID, ApplicationRequirementEvidenceDecision]:
    latest = await _latest_evidence_decisions(db, links)
    seen_hashes: set[str] = set()
    timestamp = now()
    effective: dict[uuid.UUID, ApplicationRequirementEvidenceDecision] = {}
    for link in links:
        file = inventory[link.file_id]
        analysis = analyses.get(file.id)
        content_hash = (
            file.content_hash
            or (analysis.content_hash if analysis else None)
            or hashlib.sha256(f"pending:{file.id}".encode()).hexdigest()
        )
        duplicate = content_hash in seen_hashes
        seen_hashes.add(content_hash)
        current = latest.get(link.id)
        analysis_version = analysis.analysis_version if analysis else 0
        if (
            current is not None
            and current.actor_kind == "staff"
            and current.content_hash == content_hash
            and current.analysis_version == analysis_version
        ):
            effective[link.id] = current
        else:
            decision, reason_code, explanation, confidence = _automatic_evidence_decision(
                requirement=requirement,
                file=file,
                analysis=analysis,
                expected_entity=expected_entity,
                duplicate_content=duplicate,
            )
            raw_key = ":".join(
                (
                    "ai-evidence-v1",
                    str(link.id),
                    content_hash,
                    str(analysis_version),
                    str(criteria_version),
                    analysis.analyzed_at.isoformat()
                    if analysis and analysis.analyzed_at
                    else "pending",
                )
            )
            idempotency_key = hashlib.sha256(raw_key.encode()).hexdigest()
            row = (
                await db.execute(
                    select(ApplicationRequirementEvidenceDecision).where(
                        ApplicationRequirementEvidenceDecision.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ApplicationRequirementEvidenceDecision(
                    requirement_evidence_id=link.id,
                    analysis_id=analysis.id if analysis else None,
                    content_hash=content_hash,
                    analysis_version=analysis_version,
                    policy_version=criteria_version,
                    decision=decision,
                    reason_code=reason_code,
                    explanation=explanation,
                    confidence=confidence,
                    actor_kind="ai",
                    supersedes_decision_id=current.id if current else None,
                    idempotency_key=idempotency_key,
                )
                db.add(row)
            effective[link.id] = row

        accepted = effective[link.id].decision == "accepted"
        can_auto_verify = accepted and not requirement.verification_required
        if can_auto_verify and link.verified_at is None:
            link.verified_at = timestamp
            link.verified_by_user_id = None
            link.reason = "Accepted by AI evidence review"
        elif (
            not can_auto_verify
            and link.verified_at is not None
            and link.verified_by_user_id is None
            and link.reason == "Accepted by AI evidence review"
        ):
            link.verified_at = None
            link.reason = effective[link.id].explanation
    await db.flush()
    return effective


async def _materialize_requirements(
    db: AsyncSession,
    profile: ApplicationProfile,
    selections: list[ApplicationProgramSelection],
    grouped: dict[uuid.UUID, list[AICollectionRequirement]],
    policies: list[ApplicationEvidencePolicySelection],
    grouped_policies: dict[uuid.UUID, list[AICollectionRequirement]],
) -> tuple[list[ApplicationRequirementState], dict[uuid.UUID, list[str]]]:
    files, analyses, _classifications = await _evidence_inventory(db, profile)
    inventory = {file.id: file for file in files}
    intake = (
        await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    )
    expected_entity = intake.business_name if intake else None
    criteria_versions = {
        item.playbook_id: item.playbook_version for item in [*selections, *policies]
    }
    existing = {
        item.requirement_key: item
        for item in (
            await db.execute(
                select(ApplicationRequirementState).where(
                    ApplicationRequirementState.profile_id == profile.id
                )
            )
        )
        .scalars()
        .all()
    }
    merged: dict[str, dict[str, Any]] = {}
    per_selection: dict[uuid.UUID, list[str]] = defaultdict(list)

    def merge_requirement(
        requirement: AICollectionRequirement,
        *,
        program_key: str | None = None,
        policy_key: str | None = None,
    ) -> None:
        current = merged.get(requirement.requirement_key)
        if current is None:
            current = {
                "requirement": requirement,
                "program_keys": set(),
                "policy_keys": set(),
            }
            merged[requirement.requirement_key] = current
        elif (
            LEVEL_RANK.get(requirement.required_level, 0),
            bool(requirement.verification_required),
        ) > (
            LEVEL_RANK.get(current["requirement"].required_level, 0),
            bool(current["requirement"].verification_required),
        ):
            current["requirement"] = requirement
        if program_key:
            current["program_keys"].add(program_key)
        if policy_key:
            current["policy_keys"].add(policy_key)

    for selection in selections:
        for requirement in grouped.get(selection.playbook_id, []):
            per_selection[selection.id].append(requirement.requirement_key)
            merge_requirement(requirement, program_key=selection.program_key)
    for policy in policies:
        for requirement in grouped_policies.get(policy.playbook_id, []):
            merge_requirement(requirement, policy_key=policy.policy_key)

    active_keys = set(merged)
    for key, state in existing.items():
        if key not in active_keys and state.status not in {"waived", "not_applicable"}:
            state.status = "stale"
            state.state_reason = "No longer required by the selected programs"
        if key not in active_keys and state.requested_document_id:
            requested = await db.get(BucketRequestedDocument, state.requested_document_id)
            if (
                requested
                and isinstance(requested.requirement_source, dict)
                and requested.requirement_source.get("kind") == "program_readiness"
            ):
                requested.status = "not_applicable"

    result: list[ApplicationRequirementState] = []
    for key, merged_item in merged.items():
        requirement = merged_item["requirement"]
        source_programs = merged_item["program_keys"]
        source_policies = merged_item["policy_keys"]
        requested = await _requested_document(
            db,
            profile,
            requirement,
            sorted(source_programs),
            sorted(source_policies),
        )
        state = existing.get(key)
        if state is None:
            state = ApplicationRequirementState(
                profile_id=profile.id,
                requirement_key=key,
                label=requirement.label,
                category=requirement.category,
                required_level=requirement.required_level,
                status="requested" if requested else "missing",
                requested_document_id=requested.id if requested else None,
                verification_required=requirement.verification_required,
                source_program_keys=sorted(source_programs),
                source_policy_keys=sorted(source_policies),
            )
            if requested:
                state.first_requested_at = requested.created_at or now()
            db.add(state)
            await db.flush()
        state.label = requirement.label
        state.category = requirement.category
        state.required_level = requirement.required_level
        state.verification_required = requirement.verification_required
        state.source_program_keys = sorted(source_programs)
        state.source_policy_keys = sorted(source_policies)
        if requested:
            state.requested_document_id = requested.id

        prior_links = list(
            (
                await db.execute(
                    select(ApplicationRequirementEvidence).where(
                        ApplicationRequirementEvidence.requirement_state_id == state.id,
                        ApplicationRequirementEvidence.removed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        prior_link_ids = {row.file_id for row in prior_links}
        manual_ids = {row.file_id for row in prior_links if row.source == "operator"}
        matched_files, _matched_complete, matched_provenance = _matching_evidence_files(
            requirement,
            requested,
            files,
            analyses,
            preferred_file_ids=manual_ids,
            trust_preferred=True,
        )
        desired_sources = {file.id: "automatic" for file in matched_files}
        links = await _sync_requirement_evidence(db, state, inventory, desired_sources)
        linked_files = [inventory[row.file_id] for row in links]
        _coverage_complete, coverage = _coverage_for_files(requirement, linked_files, analyses)
        decisions = await _reconcile_evidence_decisions(
            db,
            requirement=requirement,
            links=links,
            inventory=inventory,
            analyses=analyses,
            expected_entity=(
                expected_entity
                if requirement.requirement_key in BUSINESS_ENTITY_REQUIREMENTS
                else None
            ),
            criteria_version=criteria_versions.get(requirement.playbook_id, 1),
        )
        accepted_links = [
            row
            for row in links
            if row.verified_at is not None
            and (
                row.verified_by_user_id is not None
                or decisions.get(row.id) is not None
                and decisions[row.id].decision == "accepted"
            )
        ]
        accepted_files = [inventory[row.file_id] for row in accepted_links]
        accepted_complete, accepted_coverage = _coverage_for_files(
            requirement, accepted_files, analyses
        )
        if requested and not requested.requires_signature:
            requested.status = "uploaded" if accepted_complete else "requested"
        state.evidence_file_id = linked_files[0].id if linked_files else None
        state.provenance = {
            **matched_provenance,
            "source": "multi_evidence",
            "linked_file_ids": [str(file.id) for file in linked_files],
            "automatic_file_ids": [str(row.file_id) for row in links if row.source == "automatic"],
            "suggested_file_ids": [
                str(row.file_id) for row in links if row.source == "filename_suggestion"
            ],
            "coverage": coverage,
            "verified_coverage": accepted_coverage,
            "accepted_file_ids": [str(row.file_id) for row in accepted_links],
        }
        if state.status not in {"waived", "not_applicable"}:
            if linked_files:
                state.received_at = state.received_at or min(
                    file.created_at for file in linked_files
                )
                if requirement.expiration_days and all(
                    file.created_at < now() - timedelta(days=requirement.expiration_days)
                    for file in linked_files
                ):
                    state.status = "stale"
                    state.state_reason = "Evidence is older than the published program allows"
                elif state.status == "failed" and prior_link_ids == {row.file_id for row in links}:
                    pass
                elif accepted_complete:
                    state.status = "verified"
                    newest_verification = max(
                        accepted_links,
                        key=lambda row: row.verified_at or datetime.min.replace(tzinfo=UTC),
                    )
                    state.verified_at = newest_verification.verified_at
                    state.verified_by_user_id = newest_verification.verified_by_user_id
                    state.state_reason = (
                        "Accepted by automatic AI evidence review"
                        if newest_verification.verified_by_user_id is None
                        else "Accepted by underwriting override"
                    )
                else:
                    state.status = "received_unverified"
                    state.verified_at = None
                    state.verified_by_user_id = None
                    current = int(accepted_coverage.get("current") or 0)
                    required = int(accepted_coverage.get("required") or 1)
                    unit = str(accepted_coverage.get("unit") or "documents")
                    decision_values = {row.decision for row in decisions.values()}
                    if "processing" in decision_values:
                        state.state_reason = "AI evidence analysis is in progress"
                    elif "failed" in decision_values:
                        state.state_reason = "AI evidence analysis failed; retry is available"
                    elif decision_values.intersection({"rejected", "needs_more"}):
                        state.state_reason = f"AI accepted {current} of {required} required {unit}; some evidence needs attention"
                    else:
                        state.state_reason = f"AI accepted {current} of {required} required {unit}"
            else:
                state.evidence_file_id = None
                if state.status != "failed":
                    state.status = "requested" if requested else "missing"
                    state.verified_at = None
                    state.verified_by_user_id = None
                    state.state_reason = "Awaiting client evidence"
        result.append(state)
    await db.flush()
    return result, per_selection


async def _active_overrides(
    db: AsyncSession, selections: list[ApplicationProgramSelection]
) -> dict[tuple[uuid.UUID, str], ApplicationProgramRequirementOverride]:
    if not selections:
        return {}
    rows = (
        (
            await db.execute(
                select(ApplicationProgramRequirementOverride).where(
                    ApplicationProgramRequirementOverride.selection_id.in_(
                        [item.id for item in selections]
                    ),
                    ApplicationProgramRequirementOverride.restored_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {(row.selection_id, row.requirement_key): row for row in rows}


async def _automation_state(
    db: AsyncSession,
    profile: ApplicationProfile,
    requirements: list[ApplicationRequirementState],
    visibility: dict[str, bool],
    effectively_satisfied: set[str],
) -> MissingItemAutomationRead:
    missing = next(
        (
            item
            for item in requirements
            if item.required_level == "required"
            and visibility.get(item.requirement_key, False)
            and item.requirement_key not in effectively_satisfied
            and _requirement_needs_client_evidence(item)
        ),
        None,
    )
    intake = (
        await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    )
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    email = profiles.normalized_email(
        (intake.email if intake else None) or (client.email if client else None)
    )
    link = None
    if profile.primary_bucket_id:
        link = (
            await db.execute(
                select(BucketUploadLink.id)
                .where(
                    BucketUploadLink.bucket_id == profile.primary_bucket_id,
                    BucketUploadLink.status == "active",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    stop_reason = None
    if profile.underwriting_status not in OPEN_UNDERWRITING_STATES:
        stop_reason = "File is closed or denied"
    elif missing is None:
        stop_reason = "No missing client-visible requirement"
    elif not email:
        stop_reason = "No verified client email"
    elif client and await email_is_suppressed(db, client.id):
        stop_reason = "Client opted out of automated email"
    elif link is None:
        stop_reason = "No active secure room"
    elif (
        profile.missing_item_email_attempts >= 3
        and profile.missing_item_email_requirement_key == missing.requirement_key
    ):
        stop_reason = "Maximum automatic attempts reached"
    eligible = stop_reason is None
    if missing and profile.missing_item_email_requirement_key != missing.requirement_key:
        profile.missing_item_email_requirement_key = missing.requirement_key
        profile.missing_item_email_attempts = 0
        profile.missing_item_email_next_send_at = now()
    next_send = profile.missing_item_email_next_send_at
    if eligible and next_send is None:
        next_send = max(
            now(),
            (profile.missing_item_email_last_sent_at or now() - timedelta(days=1))
            + timedelta(hours=24),
        )
        profile.missing_item_email_next_send_at = next_send
    return MissingItemAutomationRead(
        enabled=profile.missing_item_email_enabled,
        eligible=eligible,
        next_requirement_key=missing.requirement_key if missing else None,
        next_send_at=next_send if profile.missing_item_email_enabled and eligible else None,
        last_sent_at=profile.missing_item_email_last_sent_at,
        attempts=profile.missing_item_email_attempts,
        stop_reason=stop_reason,
    )


def _requirement_needs_client_evidence(state: ApplicationRequirementState) -> bool:
    if state.status in SATISFIED_STATES:
        return False
    if state.status in {"missing", "requested", "stale", "failed"}:
        return True
    coverage = dict((state.provenance or {}).get("coverage") or {})
    if coverage:
        return not bool(coverage.get("complete"))
    return state.evidence_file_id is None


async def email_is_suppressed(db: AsyncSession, client_id: uuid.UUID) -> bool:
    rows = (
        (
            await db.execute(
                select(ClientAIPlan.ai_secretary_settings)
                .where(ClientAIPlan.client_id == client_id)
                .order_by(ClientAIPlan.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for settings in rows:
        if not isinstance(settings, dict):
            continue
        email_opt_out = settings.get("email_opt_out") or {}
        if isinstance(email_opt_out, dict) and email_opt_out.get("opted_out_at"):
            return True
    return False


def _requirement_is_effectively_accepted(
    state: ApplicationRequirementState,
    override: ApplicationProgramRequirementOverride | None,
) -> bool:
    if state.status in SATISFIED_STATES:
        return True
    if override and override.disposition in {"waived", "not_applicable"}:
        return True
    return False


async def _auto_start_underwriting_if_loaded(
    db: AsyncSession,
    profile: ApplicationProfile,
    loaded_program_keys: list[str],
) -> bool:
    """Advance criteria-complete files once, without moving later stages back."""
    if not loaded_program_keys or profile.underwriting_status not in {
        "submitted",
        "collecting_docs",
    }:
        return False

    previous_status = profile.underwriting_status
    profile.underwriting_status = "in_underwriting"
    profile.underwriting_updated_at = now()
    profile.underwriting_updated_by_user_id = None

    loan: Loan | None = await db.get(Loan, profile.loan_id) if profile.loan_id else None
    previous_loan_stage: str | None = None
    if loan is not None:
        previous_loan_stage = getattr(loan.stage, "value", str(loan.stage))
        if previous_loan_stage in {LoanStage.PREQUALIFIED.value, LoanStage.COLLECTING_DOCS.value}:
            loan.stage = LoanStage.PROCESSING
            await log_activity(
                db,
                loan_id=loan.id,
                actor_label="system",
                kind="loan.stage_changed",
                summary="Required evidence is complete; the file moved to underwriting",
                payload={
                    "from": previous_loan_stage,
                    "to": LoanStage.PROCESSING.value,
                    "source": "program_readiness",
                    "program_keys": loaded_program_keys,
                },
            )

    await profiles.log_profile_action(
        db,
        profile,
        None,
        "underwriting.auto_started",
        "Required evidence is complete; the file moved to underwriting",
        target_type="loan" if loan else "application_profile",
        target_id=loan.id if loan else profile.id,
        metadata={
            "from": previous_status,
            "to": "in_underwriting",
            "loan_stage_from": previous_loan_stage,
            "loan_stage_to": (
                LoanStage.PROCESSING.value
                if loan is not None
                and previous_loan_stage
                in {LoanStage.PREQUALIFIED.value, LoanStage.COLLECTING_DOCS.value}
                else previous_loan_stage
            ),
            "source": "program_readiness",
            "program_keys": loaded_program_keys,
        },
    )
    await file_events.emit(
        db,
        profile=profile,
        kind="status.changed",
        visibility=file_events.VISIBILITY_CLIENT,
        title="Your file moved to underwriting",
        actor_label="Qualified Commercial",
        target_type="application_profile",
        target_id=profile.id,
        meta={
            "from": previous_status,
            "to": "in_underwriting",
            "source": "program_readiness",
            "automatic": True,
            "program_keys": loaded_program_keys,
        },
    )
    await db.flush()
    return True


async def get_program_readiness(
    db: AsyncSession, profile: ApplicationProfile
) -> ApplicationProgramReadiness:
    # Readiness GETs may perform deterministic first-use materialization. Lock
    # this profile so simultaneous tabs cannot race the active unique indexes.
    await db.execute(
        select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update()
    )
    context = await profile_fit_context(db, profile)
    lending_applicable = _is_lending_applicable(context)
    candidates = await published_candidates(db, profile)
    stored_selections = (
        await _auto_select(db, profile, candidates)
        if lending_applicable
        else await active_selections(db, profile.id)
    )
    # Preserve pinned selections in storage in case the enquiry returns to a
    # lending path, but do not apply or expose lending criteria while routed
    # to a non-lending workflow.
    selections = stored_selections if lending_applicable else []
    grouped = await _selection_requirements(db, selections, context)
    policies = await ensure_evidence_policy(db, profile) if lending_applicable else []
    grouped_policies = await _policy_requirements(db, policies, context)
    states, per_selection = await _materialize_requirements(
        db,
        profile,
        selections,
        grouped,
        policies,
        grouped_policies,
    )
    overrides = await _active_overrides(db, selections)
    requirement_map = {item.requirement_key: item for item in states}
    visibility: dict[str, bool] = {}
    can_waive: dict[str, bool] = {}
    for rows in [*grouped.values(), *grouped_policies.values()]:
        for row in rows:
            visibility[row.requirement_key] = visibility.get(
                row.requirement_key, False
            ) or _client_visible(row)
            can_waive[row.requirement_key] = (
                can_waive.get(row.requirement_key, False) or row.can_underwriter_waive
            )

    programs: list[ProgramReadinessItem] = []
    fully_loaded_program_keys: list[str] = []
    policy_requirement_keys = [item.requirement_key for item in states if item.source_policy_keys]
    for selection in selections:
        keys = list(dict.fromkeys([*policy_requirement_keys, *per_selection.get(selection.id, [])]))
        required_keys = [
            key
            for key in keys
            if requirement_map.get(key) and requirement_map[key].required_level == "required"
        ]
        blocking = []
        loading_blockers = []
        satisfied = 0
        for key in required_keys:
            state = requirement_map[key]
            override = overrides.get((selection.id, key))
            is_satisfied = state.status in SATISFIED_STATES or bool(
                override and override.disposition in {"waived", "not_applicable"}
            )
            if is_satisfied:
                satisfied += 1
            else:
                blocking.append(key)
            if not _requirement_is_effectively_accepted(state, override):
                loading_blockers.append(key)
        if required_keys and not loading_blockers:
            fully_loaded_program_keys.append(selection.program_key)
        percent = 0 if not required_keys else round((satisfied / len(required_keys)) * 100)
        programs.append(
            ProgramReadinessItem(
                selection_id=selection.id,
                program_key=selection.program_key,
                program_name=selection.program_name,
                complete=bool(required_keys) and not blocking,
                completion_percent=percent,
                required_count=len(required_keys),
                satisfied_count=satisfied,
                blocking_requirement_keys=blocking,
                requirement_keys=keys,
            )
        )
    advanced = False
    if lending_applicable:
        advanced = await _auto_start_underwriting_if_loaded(db, profile, fully_loaded_program_keys)
    effectively_satisfied: set[str] = set()
    for item in states:
        if item.status in SATISFIED_STATES:
            effectively_satisfied.add(item.requirement_key)
            continue
        relevant = [
            selection
            for selection in selections
            if item.requirement_key in per_selection.get(selection.id, [])
        ]
        if relevant and all(
            (selection.id, item.requirement_key) in overrides
            and overrides[(selection.id, item.requirement_key)].disposition
            in {"waived", "not_applicable"}
            for selection in relevant
        ):
            effectively_satisfied.add(item.requirement_key)
    automation = await _automation_state(
        db,
        profile,
        states,
        visibility,
        effectively_satisfied,
    )
    links_by_state: dict[uuid.UUID, list[tuple[ApplicationRequirementEvidence, BucketFile]]] = (
        defaultdict(list)
    )
    state_ids = [item.id for item in states]
    if state_ids:
        evidence_rows = (
            await db.execute(
                select(ApplicationRequirementEvidence, BucketFile)
                .join(BucketFile, BucketFile.id == ApplicationRequirementEvidence.file_id)
                .where(
                    ApplicationRequirementEvidence.requirement_state_id.in_(state_ids),
                    ApplicationRequirementEvidence.removed_at.is_(None),
                    BucketFile.deleted_at.is_(None),
                    BucketFile.status == "uploaded",
                )
                .order_by(BucketFile.created_at.desc())
            )
        ).all()
        for link, file in evidence_rows:
            links_by_state[link.requirement_state_id].append((link, file))
    all_links = [link for rows in links_by_state.values() for link, _file in rows]
    latest_decisions = await _latest_evidence_decisions(db, all_links)
    available_files, available_analyses, _available_classes = await _evidence_inventory(db, profile)
    requirement_definitions: dict[str, AICollectionRequirement] = {}
    for rows in [*grouped.values(), *grouped_policies.values()]:
        for definition in rows:
            requirement_definitions.setdefault(definition.requirement_key, definition)

    def evidence_read(
        state: ApplicationRequirementState,
        link: ApplicationRequirementEvidence,
        file: BucketFile,
    ) -> ApplicationRequirementEvidenceRead:
        decision = latest_decisions.get(link.id)
        definition = requirement_definitions.get(state.requirement_key)
        contribution: dict[str, Any] = {}
        if definition is not None:
            _complete, contribution = _coverage_for_files(definition, [file], available_analyses)
        inferred_decision = "accepted" if link.verified_at else "processing"
        return ApplicationRequirementEvidenceRead(
            file_id=file.id,
            file_name=file.file_name,
            bucket_id=file.bucket_id,
            created_at=file.created_at,
            source=link.source,
            verified=link.verified_at is not None,
            verified_at=link.verified_at,
            ai_decision=decision.decision if decision else inferred_decision,
            ai_reason_code=decision.reason_code if decision else None,
            ai_explanation=decision.explanation if decision else None,
            ai_confidence=decision.confidence if decision else None,
            decision_actor=(
                decision.actor_kind if decision else "staff" if link.verified_by_user_id else None
            ),
            analysis_id=decision.analysis_id if decision else None,
            coverage_contribution=contribution,
        )

    bank_state = next(
        (item for item in states if item.requirement_key == "business_bank_statements_6_months"),
        None,
    )
    bank_links = links_by_state.get(bank_state.id, []) if bank_state else []
    bank_decision_counts: Counter[str] = Counter()
    for link, _file in bank_links:
        decision = latest_decisions.get(link.id)
        effective_decision = (
            decision.decision
            if decision is not None
            else "accepted"
            if link.verified_at is not None
            else "processing"
        )
        bank_decision_counts[effective_decision] += 1
    bank_coverage = (
        dict((bank_state.provenance or {}).get("verified_coverage") or {}) if bank_state else {}
    )
    evidence_summary = ApplicationEvidenceSummary(
        bank_statement_months=list(bank_coverage.get("months") or []),
        bank_statement_required_months=int(
            bank_coverage.get("required_months") or bank_coverage.get("required") or 6
        ),
        bank_statement_file_count=len(bank_links),
        bank_statement_accepted_count=bank_decision_counts["accepted"],
        bank_statement_processing_count=bank_decision_counts["processing"],
        bank_statement_needs_more_count=bank_decision_counts["needs_more"],
        bank_statement_rejected_count=bank_decision_counts["rejected"],
        bank_statement_failed_count=bank_decision_counts["failed"],
        bank_statement_coverage_complete=bool(bank_coverage.get("complete")),
    )

    return ApplicationProgramReadiness(
        profile_id=profile.id,
        lending_applicable=lending_applicable,
        selection_mode=profile.program_selection_mode,
        selections=[
            ApplicationProgramSelectionRead(
                id=item.id,
                program_key=item.program_key,
                program_name=item.program_name,
                playbook_id=item.playbook_id,
                playbook_version=item.playbook_version,
                source=item.source,
                fit_score=_float(item.fit_score),
                fit_confidence=_float(item.fit_confidence),
                fit_reasons=list(item.fit_reasons or []),
                selected_at=item.selected_at,
                needs_scope_review=item.needs_scope_review,
            )
            for item in selections
        ],
        evidence_policies=[
            EvidencePolicySelectionRead(
                id=item.id,
                policy_key=item.policy_key,
                policy_name=item.policy_name,
                playbook_id=item.playbook_id,
                playbook_version=item.playbook_version,
                selected_at=item.selected_at,
            )
            for item in policies
        ],
        candidates=candidates,
        programs=programs,
        requirements=[
            ApplicationRequirementRead(
                requirement_key=item.requirement_key,
                label=item.label,
                category=item.category,
                required_level=item.required_level,
                status=item.status,
                requested_document_id=item.requested_document_id,
                evidence_file_id=item.evidence_file_id,
                evidence_file_name=(
                    links_by_state[item.id][0][1].file_name if links_by_state.get(item.id) else None
                ),
                evidence_files=[
                    evidence_read(item, link, file)
                    for link, file in links_by_state.get(item.id, [])
                ],
                evidence_count=len(links_by_state.get(item.id, [])),
                verified_evidence_count=sum(
                    link.verified_at is not None for link, _file in links_by_state.get(item.id, [])
                ),
                coverage=dict((item.provenance or {}).get("coverage") or {}),
                verified_coverage=dict((item.provenance or {}).get("verified_coverage") or {}),
                coverage_complete=bool(
                    ((item.provenance or {}).get("coverage") or {}).get("complete")
                ),
                verified_coverage_complete=bool(
                    ((item.provenance or {}).get("verified_coverage") or {}).get("complete")
                ),
                allow_multiple_files=True,
                verification_required=item.verification_required,
                source_program_keys=list(item.source_program_keys or []),
                source_policy_keys=list(item.source_policy_keys or []),
                program_overrides={
                    selection.program_key: overrides[
                        (selection.id, item.requirement_key)
                    ].disposition
                    for selection in selections
                    if (selection.id, item.requirement_key) in overrides
                },
                client_visible=visibility.get(item.requirement_key, False),
                can_waive=can_waive.get(item.requirement_key, False),
                state_reason=item.state_reason,
                last_requested_at=item.last_requested_at,
                received_at=item.received_at,
                verified_at=item.verified_at,
                provenance=dict(item.provenance or {}),
            )
            for item in states
        ],
        available_evidence_files=[
            ApplicationEvidenceOptionRead(
                file_id=file.id,
                file_name=file.file_name,
                bucket_id=file.bucket_id,
                created_at=file.created_at,
            )
            for file in sorted(available_files, key=lambda row: row.created_at, reverse=True)
        ],
        evidence_summary=evidence_summary,
        can_advance=lending_applicable and any(item.complete for item in programs),
        automatic_stage_status=(
            "not_applicable"
            if not lending_applicable
            else "advanced"
            if advanced
            else "already_in_underwriting"
            if profile.underwriting_status not in {"submitted", "collecting_docs"}
            else "ready"
            if any(item.complete for item in programs)
            else "not_ready"
        ),
        automation=automation,
    )


async def patch_requirement(
    db: AsyncSession,
    profile: ApplicationProfile,
    requirement_key: str,
    payload: ApplicationRequirementPatch,
    user: User,
) -> None:
    readiness = await get_program_readiness(db, profile)
    state = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == requirement_key,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    timestamp = now()
    requirement_read = next(
        (item for item in readiness.requirements if item.requirement_key == requirement_key),
        None,
    )
    if requirement_read is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    selected_file_ids = set(payload.evidence_file_ids)
    if payload.evidence_file_id is not None:
        selected_file_ids.add(payload.evidence_file_id)
    link_rows = list(
        (
            await db.execute(
                select(ApplicationRequirementEvidence).where(
                    ApplicationRequirementEvidence.requirement_state_id == state.id,
                    ApplicationRequirementEvidence.removed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    active_links = {row.file_id: row for row in link_rows}
    if payload.action == "link_evidence":
        evidence = await profiles.evidence_state(db, profile)
        allowed_ids = {item.id for item in evidence.files}
        if selected_file_ids.difference(allowed_ids):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence file not found")
        for file_id in selected_file_ids:
            row = active_links.get(file_id)
            if row is None:
                row = ApplicationRequirementEvidence(
                    requirement_state_id=state.id,
                    file_id=file_id,
                    source="operator",
                    linked_at=timestamp,
                    linked_by_user_id=user.id,
                    reason=payload.reason or "Linked by underwriting staff",
                    provenance={"source": "operator", "actor_id": str(user.id)},
                )
                db.add(row)
                active_links[file_id] = row
            else:
                row.source = "operator"
                row.linked_by_user_id = user.id
                row.reason = payload.reason or "Confirmed by underwriting staff"
                row.provenance = {"source": "operator", "actor_id": str(user.id)}
        state.evidence_file_id = next(iter(selected_file_ids), state.evidence_file_id)
        state.received_at = timestamp
        state.status = "received_unverified"
        state.state_reason = payload.reason or "Evidence linked by underwriting staff"
    elif payload.action == "unlink_evidence":
        if selected_file_ids.difference(active_links):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Linked evidence file not found")
        for file_id in selected_file_ids:
            row = active_links[file_id]
            row.removed_at = timestamp
            row.removed_by_user_id = user.id
            row.reason = payload.reason or "Unlinked by underwriting staff"
            active_links.pop(file_id)
        state.evidence_file_id = next(iter(active_links), None)
        state.status = "received_unverified" if active_links else "requested"
        state.verified_at = None
        state.verified_by_user_id = None
        state.state_reason = payload.reason or "Evidence link removed by underwriting staff"
    elif payload.action == "verify":
        if not state.verification_required:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Routine financial evidence is decided automatically; use an evidence override when needed",
            )
        target_ids = selected_file_ids or set(active_links)
        if not target_ids:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Link evidence before verifying this requirement"
            )
        if target_ids.difference(active_links):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Linked evidence file not found")
        for file_id in target_ids:
            row = active_links[file_id]
            row.verified_at = timestamp
            row.verified_by_user_id = user.id
            row.reason = payload.reason or "Verified by underwriting staff"
        # The subsequent deterministic materialization decides whether the
        # verified set meets periods, years, or document-type coverage.
        state.status = "received_unverified"
        state.state_reason = payload.reason or "Verified by underwriting staff"
    elif payload.action == "unverify":
        target_ids = selected_file_ids or set(active_links)
        if target_ids.difference(active_links):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Linked evidence file not found")
        for file_id in target_ids:
            row = active_links[file_id]
            row.verified_at = None
            row.verified_by_user_id = None
            row.reason = payload.reason or "Verification removed by underwriting staff"
        state.status = "received_unverified" if active_links else "requested"
        state.verified_at = None
        state.verified_by_user_id = None
        state.state_reason = payload.reason or "Verification removed by underwriting staff"
    elif payload.action == "failed":
        state.status = "failed"
        state.state_reason = payload.reason or "Evidence failed review"
    elif payload.action in {"waive", "not_applicable"}:
        selected = {item.program_key: item for item in await active_selections(db, profile.id)}
        source_keys = set(requirement_read.source_program_keys)
        if not source_keys and requirement_read.source_policy_keys:
            if payload.action == "waive" and not requirement_read.can_waive:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Published evidence policy does not allow this requirement to be waived",
                )
            state.status = "waived" if payload.action == "waive" else "not_applicable"
            state.state_reason = (payload.reason or "").strip()
            state.verified_at = timestamp
            state.verified_by_user_id = user.id
            await db.flush()
            return
        target_keys = list(source_keys) if payload.all_programs else payload.program_keys
        if not target_keys:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select at least one program")
        unknown = [key for key in target_keys if key not in selected or key not in source_keys]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Override target does not use this requirement",
            )
        if payload.action == "waive":
            requirements = list(
                (
                    await db.execute(
                        select(AICollectionRequirement).where(
                            AICollectionRequirement.playbook_id.in_(
                                [selected[key].playbook_id for key in target_keys]
                            ),
                            AICollectionRequirement.requirement_key == requirement_key,
                        )
                    )
                )
                .scalars()
                .all()
            )
            waivable_playbooks = {
                row.playbook_id for row in requirements if row.can_underwriter_waive
            }
            blocked = [
                key for key in target_keys if selected[key].playbook_id not in waivable_playbooks
            ]
            if blocked:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Published criteria do not allow a waiver for: {', '.join(blocked)}",
                )
        for key in target_keys:
            selection = selected[key]
            current = (
                await db.execute(
                    select(ApplicationProgramRequirementOverride).where(
                        ApplicationProgramRequirementOverride.selection_id == selection.id,
                        ApplicationProgramRequirementOverride.requirement_key == requirement_key,
                        ApplicationProgramRequirementOverride.restored_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if current:
                current.disposition = payload.action
                current.reason = (payload.reason or "").strip()
                current.created_by_user_id = user.id
            else:
                db.add(
                    ApplicationProgramRequirementOverride(
                        selection_id=selection.id,
                        requirement_key=requirement_key,
                        disposition=payload.action,
                        reason=(payload.reason or "").strip(),
                        created_by_user_id=user.id,
                    )
                )
    elif payload.action == "restore":
        active_ids = [item.id for item in await active_selections(db, profile.id)]
        overrides = list(
            (
                await db.execute(
                    select(ApplicationProgramRequirementOverride).where(
                        ApplicationProgramRequirementOverride.selection_id.in_(active_ids)
                        if active_ids
                        else False,
                        ApplicationProgramRequirementOverride.requirement_key == requirement_key,
                        ApplicationProgramRequirementOverride.restored_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for override in overrides:
            override.restored_at = timestamp
            override.restored_by_user_id = user.id
        state.status = "received_unverified" if active_links else "requested"
        state.state_reason = payload.reason or "Requirement restored"
    await db.flush()


def analysis_is_high_confidence_match(
    requirement: ApplicationRequirementRead,
    file: BucketFile,
    analysis: BucketFileAnalysis | None,
) -> bool:
    """Only accept current, high-confidence, content-level classifications."""

    expected = EXPECTED_CLASSIFICATIONS.get(requirement.requirement_key, set()).union(
        classifications_for_requested_doc(requirement.label, requirement.category)
    )
    if not expected or analysis is None or analysis.status != "completed":
        return False
    if file.content_hash and analysis.content_hash != file.content_hash:
        return False
    return (
        str(analysis.confidence or "").casefold() == "high" and analysis.classification in expected
    )


async def accept_high_confidence_ai_evidence(
    db: AsyncSession,
    profile: ApplicationProfile,
    requirement_keys: list[str],
    _user: User,
) -> dict[str, Any]:
    """Compatibility refresh for clients that still call the old review route.

    Evidence acceptance is now produced automatically by immutable decisions.
    This route intentionally performs no staff verification mutation.
    """
    readiness = await get_program_readiness(db, profile)
    selected_keys = (
        set(requirement_keys)
        if requirement_keys
        else {item.requirement_key for item in readiness.requirements}
    )
    requirements = {
        item.requirement_key: item
        for item in readiness.requirements
        if item.requirement_key in selected_keys
    }
    if selected_keys.difference(requirements):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    evidence = [row for item in requirements.values() for row in item.evidence_files]
    accepted = sum(row.ai_decision == "accepted" and row.verified for row in evidence)
    retained = sum(row.ai_decision in {"needs_more", "rejected"} for row in evidence)
    analysis_required = sum(row.ai_decision in {"processing", "failed"} for row in evidence)
    return {
        "readiness": readiness,
        "reviewed_file_count": len(evidence),
        "verified_file_count": 0,
        "already_verified_count": accepted,
        "retained_for_staff_count": retained,
        "analysis_required_count": analysis_required,
    }


def _policy_key_for_profile(profile: ApplicationProfile) -> str:
    return {
        "real_estate": "real_estate_baseline",
        "mca": "mca_baseline",
    }.get(profile.vertical, "business_baseline")


async def active_evidence_policies(
    db: AsyncSession, profile_id: uuid.UUID
) -> list[ApplicationEvidencePolicySelection]:
    return list(
        (
            await db.execute(
                select(ApplicationEvidencePolicySelection)
                .where(
                    ApplicationEvidencePolicySelection.profile_id == profile_id,
                    ApplicationEvidencePolicySelection.replaced_at.is_(None),
                )
                .order_by(ApplicationEvidencePolicySelection.selected_at)
            )
        )
        .scalars()
        .all()
    )


async def ensure_evidence_policy(
    db: AsyncSession, profile: ApplicationProfile
) -> list[ApplicationEvidencePolicySelection]:
    wanted_key = _policy_key_for_profile(profile)
    active = await active_evidence_policies(db, profile.id)
    matching = [item for item in active if item.policy_key == wanted_key]
    if matching:
        return matching
    timestamp = now()
    for item in active:
        item.replaced_at = timestamp
    playbooks = list(
        (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.playbook_type == "evidence_policy",
                    AIPlaybookTemplate.product_key == wanted_key,
                    AIPlaybookTemplate.status == "published",
                    AIPlaybookTemplate.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    playbooks.sort(
        key=lambda row: (
            1 if row.owner_type == "funding" else 0,
            row.version,
            row.published_at or row.created_at,
            str(row.id),
        ),
        reverse=True,
    )
    if not playbooks:
        return []
    playbook = playbooks[0]
    policy = ApplicationEvidencePolicySelection(
        profile_id=profile.id,
        playbook_id=playbook.id,
        playbook_version=playbook.version,
        policy_key=wanted_key,
        policy_name=playbook.name,
        selected_at=timestamp,
    )
    db.add(policy)
    await db.flush()
    return [policy]


async def override_evidence_decision(
    db: AsyncSession,
    *,
    profile: ApplicationProfile,
    requirement_key: str,
    file_id: uuid.UUID,
    payload: EvidenceDecisionOverride,
    user: User,
) -> ApplicationProgramReadiness:
    await get_program_readiness(db, profile)
    state = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == requirement_key,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirement not found")
    link = (
        await db.execute(
            select(ApplicationRequirementEvidence).where(
                ApplicationRequirementEvidence.requirement_state_id == state.id,
                ApplicationRequirementEvidence.file_id == file_id,
                ApplicationRequirementEvidence.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence is not linked to this requirement")
    file = await db.get(BucketFile, file_id)
    if file is None or file.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence file not found")
    analysis = (
        await db.execute(
            select(BucketFileAnalysis)
            .where(BucketFileAnalysis.bucket_file_id == file.id)
            .order_by(
                BucketFileAnalysis.analysis_version.desc(),
                BucketFileAnalysis.created_at.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    current = (await _latest_evidence_decisions(db, [link])).get(link.id)
    content_hash = (
        file.content_hash
        or (analysis.content_hash if analysis else None)
        or hashlib.sha256(f"pending:{file.id}".encode()).hexdigest()
    )
    analysis_version = analysis.analysis_version if analysis else 0
    raw_key = ":".join(
        (
            "staff-evidence-v1",
            str(link.id),
            content_hash,
            str(analysis_version),
            payload.decision,
            payload.reason_code,
            payload.reason.strip(),
        )
    )
    idempotency_key = hashlib.sha256(raw_key.encode()).hexdigest()
    decision = (
        await db.execute(
            select(ApplicationRequirementEvidenceDecision).where(
                ApplicationRequirementEvidenceDecision.idempotency_key == idempotency_key
            )
        )
    ).scalar_one_or_none()
    if decision is None:
        decision = ApplicationRequirementEvidenceDecision(
            requirement_evidence_id=link.id,
            analysis_id=analysis.id if analysis else None,
            content_hash=content_hash,
            analysis_version=analysis_version,
            policy_version=current.policy_version if current else 1,
            decision=payload.decision,
            reason_code=payload.reason_code,
            explanation=payload.reason.strip(),
            confidence=analysis.confidence if analysis else None,
            actor_kind="staff",
            actor_user_id=user.id,
            supersedes_decision_id=current.id if current else None,
            idempotency_key=idempotency_key,
        )
        db.add(decision)
    if payload.decision == "accepted":
        link.verified_at = now()
        link.verified_by_user_id = user.id
        link.reason = payload.reason.strip()
    else:
        link.verified_at = None
        link.verified_by_user_id = None
        link.reason = payload.reason.strip()
    await db.flush()
    return await get_program_readiness(db, profile)


async def reconcile_profiles_for_file(db: AsyncSession, file: BucketFile) -> list[uuid.UUID]:
    linked_intake_ids = list(
        (
            await db.execute(
                select(BucketIntakeLink.intake_id)
                .join(
                    BucketIntakeLinkFile,
                    BucketIntakeLinkFile.link_id == BucketIntakeLink.id,
                )
                .where(
                    BucketIntakeLinkFile.bucket_file_id == file.id,
                    BucketIntakeLinkFile.removed_at.is_(None),
                    BucketIntakeLink.unlinked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    profile_filter = [ApplicationProfile.primary_bucket_id == file.bucket_id]
    if linked_intake_ids:
        profile_filter.append(ApplicationProfile.intake_id.in_(linked_intake_ids))
    rows = list(
        (await db.execute(select(ApplicationProfile).where(or_(*profile_filter)))).scalars().all()
    )
    profile_ids: list[uuid.UUID] = []
    for profile in {row.id: row for row in rows}.values():
        await get_program_readiness(db, profile)
        profile_ids.append(profile.id)
    return profile_ids


def validate_playbook_rules(rules: dict[str, Any] | None) -> None:
    validate_rules(rules)
