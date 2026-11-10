"""Staff document receipt is separate from a lender's financial conditions.

The immutable evidence decision records what was received. It never asserts
that a linked score has another bureau/model or that a business has positive
earnings. Those conditions remain independent program-readiness blockers.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.application_profile import (
    ApplicationRequirementEvidence,
    ApplicationRequirementState,
)
from app.models.funding_program import ApplicationRequirementEvidenceDecision


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def business_tax_requirement(requirement):
    key = getattr(requirement, "canonical_key", None) or requirement.requirement_key
    config = getattr(requirement, "collection_config", None) or {}
    return key.startswith("business_tax_return") or (
        config.get("document_type") == "tax_return"
        and config.get("subject", "business") in {"business", "company", "current business"}
    )


def tax_eligible_files(requirement, files, analyses, *, expected_entity=None, associations=None):
    """Only business returns may fulfill a business-return collection request."""
    if not business_tax_requirement(requirement):
        return files
    from app.services import application_programs as programs
    from app.services.document_associations import identity_matches, source_excluded
    from app.services.financial_tax_earnings import tax_source_is_business

    candidates = []
    config = getattr(requirement, "collection_config", None) or {}
    target_year = None
    if config.get("reporting_period") == "prior_calendar_year":
        from app.services.mca_funding_policy import required_tax_year

        target_year = str(required_tax_year())
    for file in files:
        analysis = analyses.get(file.id)
        facts = (getattr(analysis, "analysis", None) or {}).get("key_facts") or {}
        association = (associations or {}).get(str(file.id))
        entity = next((facts.get(key) for key in ("entity_name", "business_name", "entity", "taxpayer") if facts.get(key)), None)
        if not tax_source_is_business(facts) or source_excluded(association):
            continue
        if expected_entity and not identity_matches(expected_entity, entity, association):
            continue
        if target_year is not None:
            # A comparative prior-year figure inside an older return or the
            # filename alone cannot change its actual filing year.
            explicit_year = facts.get("tax_year") or facts.get("fiscal_year") or facts.get("return_year")
            years = re.findall(r"\b(?:19|20)\d{2}\b", str(explicit_year or ""))
            if years != [target_year]:
                continue
        candidates.append(file)
    years = programs._configured_period_count(requirement, "years")
    if years == 1 and candidates:
        def period(file):
            analysis = analyses.get(file.id)
            facts = (getattr(analysis, "analysis", None) or {}).get("key_facts") or {}
            return str(facts.get("period_end") or facts.get("tax_year") or "")
        latest = max(period(file) for file in candidates)
        candidates = [file for file in candidates if period(file) == latest]
    return candidates


def _binding(file, analysis):
    return {"file_id": str(file.id), "content_hash": file.content_hash or getattr(analysis, "content_hash", None),
            "analysis_id": str(analysis.id) if analysis else None,
            "analysis_version": analysis.analysis_version if analysis else None}


def snapshot(state, requirement, files, analyses, *, expected_entity=None, associations=None, counted_accounts=None, include_structured=True):
    from app.services import application_programs as programs
    from app.services import requirement_lifecycle, use_of_proceeds
    from app.services.bucket_evidence import analysis_classifications

    budget = (state.provenance or {}).get("use_of_funds_collection")
    if include_structured and use_of_proceeds.is_requirement(requirement) and isinstance(budget, dict):
        return dict(budget)

    credit = (state.provenance or {}).get("credit_collection")
    if isinstance(credit, dict):
        complete = bool(credit.get("complete") and credit.get("required_owner_count"))
        return {"complete": complete, "can_complete": False, "source": "linked_owner_credit",
                "fingerprint": credit["fingerprint"], "eligible_file_ids": [], "suggested_file_ids": [],
                "reason": "A current linked report from one bureau completes report collection. Program score conditions are reviewed separately." if complete else "Link a current, consented report for each required owner."}
    expected = programs._expected_classes(requirement)
    candidates = []
    for file in files:
        if use_of_proceeds.is_requirement(requirement) and not use_of_proceeds.file_is_bound(state, file):
            continue
        analysis = analyses.get(file.id)
        if (analysis is None or analysis.status != "completed" or not analysis.content_hash
                or file.content_hash and analysis.content_hash != file.content_hash
                or analysis.classification == "unreadable" or getattr(analysis, "financial_mapping_pending", False)):
            continue
        if expected and not analysis_classifications(analysis).intersection(expected):
            continue
        if programs._file_is_expired(requirement, file):
            continue
        association = (associations or {}).get(str(file.id))
        if association and association.get("status") in {"denied", "stale"}:
            continue
        candidates.append(file)
    candidates = tax_eligible_files(requirement, candidates, analyses, expected_entity=expected_entity, associations=associations)
    if counted_accounts and programs._configured_period_count(requirement, "months"):
        from app.services.financial_cockpit import bank_file_counted

        candidates = [file for file in candidates if bank_file_counted(analyses[file.id].analysis, counted_accounts)]
    candidates.sort(key=lambda file: (file.created_at, str(file.id)), reverse=True)
    enough, _coverage = programs._coverage_for_files(requirement, candidates, analyses, counted_accounts=counted_accounts)
    bindings = [_binding(file, analyses.get(file.id)) for file in candidates]
    metadata = state.provenance or {}
    stamp = digest({"profile_id": state.profile_id, "requirement_key": state.requirement_key,
                    "collection_config": getattr(requirement, "collection_config", None) or {},
                    "canonical_key": getattr(requirement, "canonical_key", None) or requirement.requirement_key, "sources": bindings,
                    "counted_accounts": sorted(counted_accounts or []),
                    "associations": {str(file.id): (associations or {}).get(str(file.id), {}).get("association_stamp") for file in candidates}})
    removed = requirement_lifecycle.is_removed(state)
    available = bool(enough and not removed and state.status not in {"waived", "not_applicable"})
    return {"source": "files", "complete": state.status == "verified", "can_complete": available,
            "fingerprint": stamp, "eligible_file_ids": [str(file.id) for file in candidates],
            "suggested_file_ids": [str(file.id) for file in candidates],
            "completed_at": metadata.get("collection_completion_receipt", {}).get("at"),
            "reason": None if enough else "Link readable, current evidence covering the requested document type and reporting period before marking this item complete."}


def qualification_review(state, decisions=None):
    """A documentary collection receipt never supplies program qualification."""
    metadata = state.provenance or {}
    checks = metadata.get("review_checks") or []
    if state.status in {"waived", "not_applicable"} and state.verified_by_user_id is not None:
        return {"status": "not_required", "blocking": False, "reasons": [], "checks": checks,
                "note": "An explicit staff disposition is recorded; it does not change the underlying source facts."}
    credit = metadata.get("credit_collection")
    if credit:
        reasons = list(credit.get("review_blockers") or [])
        review = metadata.get("credit_review") or {}
        failure = metadata.get("credit_qualification_failure") or {}
        if review.get("fingerprint") == credit.get("fingerprint") and review.get("decision") in {"failed", "incomplete"}:
            return {"status": "failed" if review["decision"] == "failed" else "pending", "blocking": True,
                    "reasons": list(dict.fromkeys([review.get("reason") or "Staff program review is incomplete", *reasons])), "checks": checks}
        if failure.get("fingerprint") == credit.get("fingerprint"):
            return {"status": "failed", "blocking": True, "reasons": [failure.get("reason") or "Staff credit qualification review failed"], "checks": checks}
        current_met = review.get("fingerprint") == credit.get("fingerprint") and review.get("decision") == "met"
        if not reasons and (metadata.get("requires_human") or getattr(state, "verification_required", False)) and not current_met:
            reasons.append("The reports are collected. Staff must separately sign off the published program credit conditions.")
        return {"status": "pending" if reasons else "met", "blocking": bool(reasons),
                "reasons": reasons, "checks": checks,
                "note": "Report collection accepts one bureau. The selected program's published bureau, model and minimum are separate conditions."}
    if not checks:
        return {"status": "not_required", "blocking": False, "reasons": [], "checks": []}
    values = list((decisions or {}).values())
    accepted = [row for row in values if row.decision == "accepted"]
    condition_verified = bool(state.status == "verified" and accepted and all(
        row.reason_code in {"staff_requirement_verified", "document_checks_passed"} for row in accepted
    ))
    blocks = any(check.get("severity", "block") == "block" for check in checks)
    return {"status": "met" if condition_verified else "pending", "blocking": not condition_verified and blocks,
            "reasons": [] if condition_verified else ["The document is collected. Review the program's financial conditions separately; collection does not establish positive earnings or a program minimum."],
            "checks": checks}


async def complete(db, profile, requirement_key, payload, actor):
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    from app.services import application_profiles as profiles
    from app.services import application_programs as programs
    from app.services import (
        document_associations,
        requirement_collection_receipts,
        requirement_lifecycle,
    )
    from app.services.owner_credit_review import lock_review_sources

    await lock_review_sources(db, profile)
    readiness = await programs.get_program_readiness(db, profile)
    resolved = requirement_lifecycle.resolve_requirement_key(readiness.requirements, requirement_key)
    if resolved is None:
        raise HTTPException(409, "The checklist changed. Reload its current requirement before completing it.")
    state = (await db.execute(select(ApplicationRequirementState).where(
        ApplicationRequirementState.profile_id == profile.id, ApplicationRequirementState.requirement_key == resolved,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    view = next(row for row in readiness.requirements if row.requirement_key == resolved)
    current = view.collection_completion
    if not payload.reason.strip():
        raise HTTPException(422, "A collection review reason is required")
    request_stamp = digest({"file_ids": sorted(str(value) for value in payload.file_ids), "reason": payload.reason.strip(),
                            "expected_fingerprint": payload.expected_fingerprint})
    receipts = await requirement_collection_receipts.receipts(db, profile, state)
    receipt = next((row for row in receipts if row.get("idempotency_key") == payload.idempotency_key), None)
    if receipt is not None:
        if receipt.get("request_stamp") != request_stamp:
            raise HTTPException(409, "That retry key was already used for a different collection review")
        return readiness
    if current.get("fingerprint") != payload.expected_fingerprint:
        raise HTTPException(409, "The evidence or collection scope changed. Reload and review the current sources.")
    if current.get("source") == "linked_owner_credit":
        if not current.get("complete"):
            raise HTTPException(422, current.get("reason"))
        return readiness
    if state.status in {"waived", "not_applicable"}:
        raise HTTPException(409, "Restore this requirement before recording new document collection")
    if current.get("source") == "use_of_funds":
        from app.services.use_of_proceeds import complete_collection

        await complete_collection(db, profile, state, payload, actor, request_stamp=request_stamp)
        return await programs.get_program_readiness(db, profile)
    selected = set(payload.file_ids)
    if not selected or selected.difference({uuid.UUID(value) for value in current.get("eligible_file_ids", [])}):
        raise HTTPException(422, "Choose current eligible evidence for this document request")
    files, analyses, _ = await programs._evidence_inventory(db, profile, include_pending_financial=True, refresh=True)
    expected_entity = await document_associations.expected_business(db, profile)
    associations = await document_associations.load_context(db, profile, files, analyses, expected_entity=expected_entity)
    fresh = snapshot(state, view, files, analyses, expected_entity=expected_entity, associations=associations,
                     counted_accounts=programs._counted_bank_accounts(profile))
    if fresh["fingerprint"] != payload.expected_fingerprint:
        raise HTTPException(409, "The source files changed during review. Reload before marking this item complete.")
    by_id = {file.id: file for file in files}
    complete_coverage, _ = programs._coverage_for_files(view, [by_id[value] for value in selected], analyses,
                                                       counted_accounts=programs._counted_bank_accounts(profile))
    if not complete_coverage:
        raise HTTPException(422, "The selected evidence does not cover the requested documents or reporting periods")
    timestamp = datetime.now(UTC)
    links = list((await db.scalars(select(ApplicationRequirementEvidence).where(
        ApplicationRequirementEvidence.requirement_state_id == state.id, ApplicationRequirementEvidence.removed_at.is_(None),
    ))).all())
    linked = {link.file_id: link for link in links}
    for file_id in sorted(selected, key=str):
        link = linked.get(file_id)
        if link is None:
            link = ApplicationRequirementEvidence(requirement_state_id=state.id, file_id=file_id, source="operator",
                linked_at=timestamp, linked_by_user_id=actor.id, provenance={"source": "operator"})
            db.add(link)
            await db.flush()
        analysis, file = analyses[file_id], by_id[file_id]
        prior = await programs._latest_evidence_decisions(db, [link])
        previous = prior.get(link.id)
        db.add(ApplicationRequirementEvidenceDecision(requirement_evidence_id=link.id, analysis_id=analysis.id,
            content_hash=file.content_hash or analysis.content_hash, analysis_version=analysis.analysis_version,
            policy_version=int((state.provenance or {}).get("definition_version") or (previous.policy_version if previous else 1)),
            decision="accepted", reason_code="staff_collection_complete",
            explanation=payload.reason.strip(), confidence=analysis.confidence, actor_kind="staff", actor_user_id=actor.id,
            supersedes_decision_id=previous.id if previous else None,
            idempotency_key=digest({"requirement": str(state.id), "retry": payload.idempotency_key, "file": str(file_id)})))
        link.verified_at, link.verified_by_user_id, link.reason = timestamp, actor.id, payload.reason.strip()
    previous_receipt = (state.provenance or {}).get("collection_completion_receipt")
    history = [*((state.provenance or {}).get("collection_completion_history") or []), *([previous_receipt] if previous_receipt else [])]
    state.provenance = {**(state.provenance or {}), "collection_completion_history": history, "collection_completion_receipt": {
        "at": timestamp.isoformat(), "actor_id": str(actor.id), "reason": payload.reason.strip(),
        "fingerprint": payload.expected_fingerprint, "file_ids": sorted(str(value) for value in selected),
        "idempotency_key": payload.idempotency_key, "request_stamp": request_stamp,
        "qualification_approved": False,
    }}
    await requirement_collection_receipts.record(db, profile, state, state.provenance["collection_completion_receipt"])
    await profiles.log_profile_action(db, profile, actor, "requirement.collection_completed", "Marked document collection complete",
        target_type="application_requirement", target_id=state.id, metadata={"requirement_key": resolved, **state.provenance["collection_completion_receipt"]})
    from app.services import file_events

    await file_events.emit(db, profile=profile, kind="requirement.collection_completed", visibility=file_events.VISIBILITY_CLIENT,
        title=f"Document request completed: {state.label}", actor=actor, target_type="application_requirement", target_id=state.id,
        meta={"requirement_key": resolved, "collection_complete": True})
    await db.flush()
    return await programs.get_program_readiness(db, profile)
