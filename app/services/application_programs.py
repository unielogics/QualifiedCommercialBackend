"""Program selection, shared evidence reconciliation, and readiness for AI Intake."""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import (
    ApplicationProfile,
    ApplicationProgramRequirementOverride,
    ApplicationProgramSelection,
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
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.user import User
from app.schemas.application_profile import (
    ApplicationProgramReadiness,
    ApplicationProgramSelectionRead,
    ApplicationProgramsPatch,
    ApplicationRequirementPatch,
    ApplicationRequirementRead,
    MissingItemAutomationRead,
    ProgramFitCandidate,
    ProgramReadinessItem,
)
from app.services import application_profiles as profiles
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


def now() -> datetime:
    return datetime.now(UTC)


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("x", "").strip())
    except (TypeError, ValueError):
        return None


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
        ).scalars().all()
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
        ).scalars().all()
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
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    files, _latest, classifications = await _evidence_inventory(db, profile)
    snapshot = dict(intake.result_snapshot or {}) if intake else {}
    metrics = snapshot.get("key_metrics") if isinstance(snapshot.get("key_metrics"), dict) else {}
    revenue = _float(
        metrics.get("ytd_annualized_revenue")
        or metrics.get("annualized_revenue")
        or metrics.get("annual_revenue")
        or metrics.get("gross_revenue")
    )
    annualized_deposits = _float(
        metrics.get("annualized_adjusted_deposits")
        or metrics.get("annualized_deposits")
    )
    intake_state = dict(intake.intake_state or {}) if intake else {}
    main_street_details = intake_state.get("main_street_details")
    main_street_details = main_street_details if isinstance(main_street_details, dict) else {}
    stated_intent = normalize_intent(main_street_details.get("intent"))
    stated_intent_kind = (
        intent_kind(stated_intent) if profile.vertical == "main_street" else "lending"
    )
    return {
        "vertical": profile.vertical,
        "intent": stated_intent if profile.vertical == "main_street" else None,
        "intent_kind": stated_intent_kind,
        "funding_category": profile.funding_category,
        "entity_type": profile.entity_type,
        "industry": profile.industry,
        "subindustry": profile.subindustry,
        "naics_code": profile.naics_code,
        "requested_amount": _float(intake.requested_loan_amount) if intake else None,
        "revenue": revenue,
        "annual_revenue": revenue,
        "annualized_deposits": annualized_deposits,
        "deposits": annualized_deposits,
        "dscr": _float(metrics.get("estimated_dscr") or metrics.get("dscr")),
        "cash_flow": _float(metrics.get("estimated_ebitda_or_cash_flow") or metrics.get("cash_flow")),
        "debt_burden": _float(metrics.get("estimated_debt_burden") or metrics.get("debt_burden")),
        "liquid_assets": _float(metrics.get("pfs_total_liquid_assets") or metrics.get("liquid_assets")),
        "tax_returns_available": "tax_return" in classifications,
        "bank_statements_available": "bank_statement" in classifications,
        "evidence_count": len(files),
        "evidence_available": classifications,
    }


async def published_candidates(
    db: AsyncSession, profile: ApplicationProfile
) -> list[ProgramFitCandidate]:
    rows = list(
        (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.playbook_type == "loan_product",
                    AIPlaybookTemplate.status == "published",
                    AIPlaybookTemplate.is_active.is_(True),
                    AIPlaybookTemplate.product_key.is_not(None),
                )
            )
        ).scalars().all()
    )
    # A funding-published program overrides a platform program with the same key.
    rows.sort(
        key=lambda row: (
            str(row.product_key),
            1 if row.owner_type == "funding" else 0,
            row.version,
            str(row.id),
        ),
        reverse=True,
    )
    latest: dict[str, AIPlaybookTemplate] = {}
    for row in rows:
        latest.setdefault(str(row.product_key), row)
    context = await profile_fit_context(db, profile)
    if context.get("intent_kind") in {"non_lending", "route_out"}:
        return []
    candidates: list[ProgramFitCandidate] = []
    for key, row in latest.items():
        try:
            result = evaluate_rules(row.rules or {}, context)
        except ProgramRuleError:
            result = None
        priority = int((row.rules or {}).get("priority") or 0)
        candidates.append(
            ProgramFitCandidate(
                program_key=key,
                program_name=row.name,
                playbook_id=row.id,
                playbook_version=row.version,
                eligible=bool(result and result.matched),
                fit_score=round((result.confidence if result else 0) * 100, 2),
                confidence=result.confidence if result else 0,
                priority=priority,
                reasons=(result.reasons if result else ["Published fit rule is invalid"]),
            )
        )
    return sorted(
        candidates,
        key=lambda item: (
            not item.eligible,
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
    if eligible is not None:
        return eligible
    baseline_key = {
        "real_estate": "real_estate_baseline",
        "mca": "mca_baseline",
    }.get(profile.vertical, "business_baseline")
    return next((item for item in candidates if item.program_key == baseline_key), None)


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
                .order_by(ApplicationProgramSelection.selected_at, ApplicationProgramSelection.program_key)
            )
        ).scalars().all()
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
            ).scalars().all()
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
        ).scalars().all()
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
    if candidate is None:
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
    candidates = {candidate.program_key: candidate for candidate in await published_candidates(db, profile)}
    if not wanted:
        baseline_key = {
            "real_estate": "real_estate_baseline",
            "mca": "mca_baseline",
        }.get(profile.vertical, "business_baseline")
        if baseline_key in candidates:
            wanted = [baseline_key]
    missing = [key for key in wanted if key not in candidates]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Published program not found: {', '.join(missing)}",
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
                .where(AICollectionRequirement.playbook_id.in_([item.playbook_id for item in selections]))
                .order_by(AICollectionRequirement.display_order, AICollectionRequirement.requirement_key)
            )
        ).scalars().all()
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
        "playbook_id": str(requirement.playbook_id),
    }
    if requested is None:
        requested = BucketRequestedDocument(
            bucket_id=profile.primary_bucket_id,
            name=requirement.label,
            category=requirement.category,
            description=requirement.objective_text or requirement.ai_request_message_template,
            required=requirement.required_level == "required",
            allow_multiple_files=requirement.requirement_key in {
                "business_bank_statements_6_months",
                "business_tax_returns_2_years",
            },
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
        requested.allow_multiple_files = requirement.requirement_key in {
            "business_bank_statements_6_months",
            "business_tax_returns_2_years",
        }
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


def _matching_evidence(
    requirement: AICollectionRequirement,
    requested: BucketRequestedDocument | None,
    files: list[BucketFile],
    analyses: dict[uuid.UUID, BucketFileAnalysis],
    *,
    preferred_file_id: uuid.UUID | None = None,
    trust_preferred: bool = False,
) -> tuple[BucketFile | None, bool, dict[str, Any]]:
    expected = _expected_classes(requirement)
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
    preferred = next((file for file in files if file.id == preferred_file_id), None)
    if preferred and trust_preferred:
        matches = list({file.id: file for file in [preferred, *matches]}.values())
    matches.sort(
        key=lambda file: (file.id == preferred_file_id, file.created_at),
        reverse=True,
    )
    if not matches:
        return None, False, {"expected_classifications": sorted(expected), "matched_files": 0}
    complete = True
    coverage: dict[str, Any] = {"matched_files": len(matches), "expected_classifications": sorted(expected)}
    if requirement.requirement_key == "business_bank_statements_6_months":
        months: set[str] = set()
        for file in matches:
            if file.statement_period:
                months.add(file.statement_period)
            months.update(statement_months_from_filename(file.file_name))
            analysis = analyses.get(file.id)
            months.update(statement_months_from_analysis(analysis.analysis if analysis else None))
        coverage.update({"months": sorted(months), "required_months": 6})
        complete = len(months) >= 6
    elif requirement.requirement_key == "business_tax_returns_2_years":
        years: set[str] = set()
        for file in matches:
            years.update(_tax_years(file, analyses.get(file.id)))
        coverage.update({"years": sorted(years), "required_years": 2})
        complete = len(years) >= 2 or len(matches) >= 2
    elif requirement.requirement_key == "ytd_p_and_l_balance_sheet":
        classifications = {
            effective_file_classification(file.file_name, analyses.get(file.id))
            for file in matches
        }
        has_profit_and_loss = bool(
            {"current_p_and_l", "profit_and_loss"}.intersection(classifications)
        )
        has_balance_sheet = "balance_sheet" in classifications
        combined_template = any(
            "profit-loss-balance-sheet" in file.file_name.casefold()
            or "p&l and balance sheet" in file.file_name.casefold()
            for file in matches
        )
        coverage.update(
            {
                "classifications": sorted(value for value in classifications if value),
                "profit_and_loss": has_profit_and_loss or combined_template,
                "balance_sheet": has_balance_sheet or combined_template,
            }
        )
        complete = (has_profit_and_loss and has_balance_sheet) or combined_template
    return matches[0], complete, coverage


async def _materialize_requirements(
    db: AsyncSession,
    profile: ApplicationProfile,
    selections: list[ApplicationProgramSelection],
    grouped: dict[uuid.UUID, list[AICollectionRequirement]],
) -> tuple[list[ApplicationRequirementState], dict[uuid.UUID, list[str]]]:
    files, analyses, _classifications = await _evidence_inventory(db, profile)
    existing = {
        item.requirement_key: item
        for item in (
            await db.execute(
                select(ApplicationRequirementState).where(ApplicationRequirementState.profile_id == profile.id)
            )
        ).scalars().all()
    }
    merged: dict[str, tuple[AICollectionRequirement, set[str]]] = {}
    per_selection: dict[uuid.UUID, list[str]] = defaultdict(list)
    for selection in selections:
        for requirement in grouped.get(selection.playbook_id, []):
            per_selection[selection.id].append(requirement.requirement_key)
            current = merged.get(requirement.requirement_key)
            if current is None or LEVEL_RANK.get(requirement.required_level, 0) > LEVEL_RANK.get(current[0].required_level, 0):
                prior_sources = current[1] if current else set()
                merged[requirement.requirement_key] = (
                    requirement,
                    {*prior_sources, selection.program_key},
                )
            else:
                current[1].add(selection.program_key)

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
    for key, (requirement, source_programs) in merged.items():
        requested = await _requested_document(db, profile, requirement, sorted(source_programs))
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
        if requested:
            state.requested_document_id = requested.id
        prior_provenance = dict(state.provenance or {}) if state else {}
        preserve_operator_choice = prior_provenance.get("source") == "operator_link"
        file, coverage_complete, provenance = _matching_evidence(
            requirement,
            requested,
            files,
            analyses,
            preferred_file_id=state.evidence_file_id if state else None,
            trust_preferred=preserve_operator_choice or bool(state and state.status == "verified"),
        )
        if state.status not in {"waived", "not_applicable"}:
            if file:
                prior_file_id = state.evidence_file_id
                was_verified = state.status == "verified" and prior_file_id == file.id
                state.evidence_file_id = file.id
                state.received_at = state.received_at or file.created_at
                source = (
                    prior_provenance.get("source")
                    if prior_file_id == file.id and prior_provenance.get("source")
                    else "explicit_request_or_content_classification"
                )
                state.provenance = {
                    **provenance,
                    "source": source,
                    "analysis_id": str(analyses[file.id].id) if file.id in analyses else None,
                }
                if requirement.expiration_days and file.created_at < now() - timedelta(days=requirement.expiration_days):
                    state.status = "stale"
                    state.state_reason = "Evidence is older than the published program allows"
                elif state.status == "failed" and prior_file_id == file.id:
                    pass
                elif coverage_complete and not requirement.verification_required:
                    state.status = "verified"
                    state.verified_at = state.verified_at or now()
                    state.verified_by_user_id = None
                    state.state_reason = "Verified automatically by published criteria"
                elif not was_verified:
                    state.status = "received_unverified"
                    state.state_reason = (
                        "Evidence received; staff verification required"
                        if coverage_complete
                        else "Evidence received but required period coverage is incomplete"
                    )
            else:
                state.evidence_file_id = None
                state.provenance = provenance
                if state.status != "failed":
                    state.status = "requested" if requested else "missing"
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
        await db.execute(
            select(ApplicationProgramRequirementOverride).where(
                ApplicationProgramRequirementOverride.selection_id.in_([item.id for item in selections]),
                ApplicationProgramRequirementOverride.restored_at.is_(None),
            )
        )
    ).scalars().all()
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
        ),
        None,
    )
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if profile.intake_id else None
    client = await db.get(Client, profile.client_id) if profile.client_id else None
    email = profiles.normalized_email((intake.email if intake else None) or (client.email if client else None))
    link = None
    if profile.primary_bucket_id:
        link = (
            await db.execute(
                select(BucketUploadLink.id).where(
                    BucketUploadLink.bucket_id == profile.primary_bucket_id,
                    BucketUploadLink.status == "active",
                ).limit(1)
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
    elif profile.missing_item_email_attempts >= 3 and profile.missing_item_email_requirement_key == missing.requirement_key:
        stop_reason = "Maximum automatic attempts reached"
    eligible = stop_reason is None
    if missing and profile.missing_item_email_requirement_key != missing.requirement_key:
        profile.missing_item_email_requirement_key = missing.requirement_key
        profile.missing_item_email_attempts = 0
        profile.missing_item_email_next_send_at = now()
    next_send = profile.missing_item_email_next_send_at
    if eligible and next_send is None:
        next_send = max(now(), (profile.missing_item_email_last_sent_at or now() - timedelta(days=1)) + timedelta(hours=24))
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


async def email_is_suppressed(db: AsyncSession, client_id: uuid.UUID) -> bool:
    rows = (
        await db.execute(
            select(ClientAIPlan.ai_secretary_settings)
            .where(ClientAIPlan.client_id == client_id)
            .order_by(ClientAIPlan.updated_at.desc())
        )
    ).scalars().all()
    for settings in rows:
        if not isinstance(settings, dict):
            continue
        email_opt_out = settings.get("email_opt_out") or {}
        if isinstance(email_opt_out, dict) and email_opt_out.get("opted_out_at"):
            return True
    return False


async def get_program_readiness(
    db: AsyncSession, profile: ApplicationProfile
) -> ApplicationProgramReadiness:
    # Readiness GETs may perform deterministic first-use materialization. Lock
    # this profile so simultaneous tabs cannot race the active unique indexes.
    await db.execute(
        select(ApplicationProfile.id)
        .where(ApplicationProfile.id == profile.id)
        .with_for_update()
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
    states, per_selection = await _materialize_requirements(db, profile, selections, grouped)
    overrides = await _active_overrides(db, selections)
    requirement_map = {item.requirement_key: item for item in states}
    visibility: dict[str, bool] = {}
    can_waive: dict[str, bool] = {}
    for rows in grouped.values():
        for row in rows:
            visibility[row.requirement_key] = visibility.get(row.requirement_key, False) or _client_visible(row)
            can_waive[row.requirement_key] = can_waive.get(row.requirement_key, False) or row.can_underwriter_waive

    programs: list[ProgramReadinessItem] = []
    for selection in selections:
        keys = list(dict.fromkeys(per_selection.get(selection.id, [])))
        required_keys = [
            key for key in keys if requirement_map.get(key) and requirement_map[key].required_level == "required"
        ]
        blocking = []
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
        percent = 100 if not required_keys else round((satisfied / len(required_keys)) * 100)
        programs.append(
            ProgramReadinessItem(
                selection_id=selection.id,
                program_key=selection.program_key,
                program_name=selection.program_name,
                complete=not blocking,
                completion_percent=percent,
                required_count=len(required_keys),
                satisfied_count=satisfied,
                blocking_requirement_keys=blocking,
                requirement_keys=keys,
            )
        )
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
    file_names = {}
    evidence_ids = [item.evidence_file_id for item in states if item.evidence_file_id]
    if evidence_ids:
        file_names = {
            file_id: file_name
            for file_id, file_name in (
                await db.execute(select(BucketFile.id, BucketFile.file_name).where(BucketFile.id.in_(evidence_ids)))
            ).all()
        }
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
            )
            for item in selections
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
                evidence_file_name=file_names.get(item.evidence_file_id),
                verification_required=item.verification_required,
                source_program_keys=list(item.source_program_keys or []),
                program_overrides={
                    selection.program_key: overrides[(selection.id, item.requirement_key)].disposition
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
        can_advance=lending_applicable and any(item.complete for item in programs),
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
    if payload.action == "link_evidence":
        evidence = await profiles.evidence_state(db, profile)
        if payload.evidence_file_id not in {item.id for item in evidence.files}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence file not found")
        state.evidence_file_id = payload.evidence_file_id
        state.received_at = timestamp
        state.status = "received_unverified"
        state.state_reason = payload.reason or "Evidence linked by underwriting staff"
        state.provenance = {"source": "operator_link", "actor_id": str(user.id)}
    elif payload.action == "verify":
        if state.evidence_file_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Link evidence before verifying this requirement")
        state.status = "verified"
        state.verified_at = timestamp
        state.verified_by_user_id = user.id
        state.state_reason = payload.reason or "Verified by underwriting staff"
    elif payload.action == "unverify":
        state.status = "received_unverified" if state.evidence_file_id else "requested"
        state.verified_at = None
        state.verified_by_user_id = None
        state.state_reason = payload.reason or "Verification removed by underwriting staff"
    elif payload.action == "failed":
        state.status = "failed"
        state.state_reason = payload.reason or "Evidence failed review"
    elif payload.action in {"waive", "not_applicable"}:
        selected = {item.program_key: item for item in await active_selections(db, profile.id)}
        source_keys = set(requirement_read.source_program_keys)
        target_keys = list(source_keys) if payload.all_programs else payload.program_keys
        if not target_keys:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Select at least one program")
        unknown = [key for key in target_keys if key not in selected or key not in source_keys]
        if unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Override target does not use this requirement")
        if payload.action == "waive":
            requirements = list(
                (
                    await db.execute(
                        select(AICollectionRequirement).where(
                            AICollectionRequirement.playbook_id.in_([selected[key].playbook_id for key in target_keys]),
                            AICollectionRequirement.requirement_key == requirement_key,
                        )
                    )
                ).scalars().all()
            )
            waivable_playbooks = {
                row.playbook_id for row in requirements if row.can_underwriter_waive
            }
            blocked = [key for key in target_keys if selected[key].playbook_id not in waivable_playbooks]
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
                        ApplicationProgramRequirementOverride.selection_id.in_(active_ids) if active_ids else False,
                        ApplicationProgramRequirementOverride.requirement_key == requirement_key,
                        ApplicationProgramRequirementOverride.restored_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        for override in overrides:
            override.restored_at = timestamp
            override.restored_by_user_id = user.id
        state.status = "received_unverified" if state.evidence_file_id else "requested"
        state.state_reason = payload.reason or "Requirement restored"
    await db.flush()


def validate_playbook_rules(rules: dict[str, Any] | None) -> None:
    validate_rules(rules)
