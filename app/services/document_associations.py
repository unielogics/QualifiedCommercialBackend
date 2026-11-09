"""Explicit business association of exact source bytes, scoped to one file.

Association waives only the identity comparison. It never changes a source
name, accepts a document type, promotes a version, or creates a legal alias.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.services.financial_metric_review import stamp
from app.services.financial_tax_earnings import entities_match, normalize_business_identity


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _facts(analysis):
    payload = _get(analysis, "analysis", {}) or {}
    facts = _get(analysis, "key_facts", None) or payload.get("key_facts") or {}
    return facts if isinstance(facts, dict) else {}


def raw_analysis(analysis):
    while type(analysis).__name__ in {"PendingDocumentAnalysis", "PendingFinancialAnalysis"}:
        analysis = getattr(analysis, "original", None) or getattr(analysis, "source", None)
    return analysis


def observed_names(analysis):
    facts = _facts(analysis)
    payload = _get(analysis, "analysis", {}) or {}
    profile = payload.get("profile_facts") or {}
    legal = profile.get("legal_entity_name")
    legal = legal.get("value") if isinstance(legal, dict) else legal
    names = [facts.get(key) for key in ("entity_name", "business_name", "account_holder", "borrower_entity", "borrower_name", "taxpayer")]
    names.append(legal)
    for row in facts.get("income_statements") or []:
        if isinstance(row, dict):
            names.append(row.get("business_name") or row.get("entity_name"))
    return list(dict.fromkeys(value.strip() for value in names if isinstance(value, str) and value.strip()))


def source_excluded(record):
    return bool(record and record.get("status") in {"denied", "stale"})


def identity_matches(expected, observed, record=None):
    """Deny first; a confirmation covers this source's reviewed primary name."""
    if source_excluded(record):
        return False
    if not expected:
        return True
    if entities_match(expected, observed):
        return True
    if record and record.get("status") == "accepted":
        primary = record.get("observed_entity")
        return (not primary and not observed) or normalize_business_identity(primary) == normalize_business_identity(observed)
    return False


def owner_context(profile):
    return {key: str(getattr(profile, key, None)) if getattr(profile, key, None) else None
            for key in ("client_id", "intake_id", "dealer_id", "loan_id", "deal_id")}


def record_for_source(profile_id, expected_entity, file, analysis, state=None, *, document_accepted=False, owner=None):
    analysis = raw_analysis(analysis)
    file_id = str(_get(file, "file_id", None) or _get(file, "id", ""))
    analysis_id = _get(analysis, "analysis_id", None) or _get(analysis, "id", None)
    file_hash = _get(file, "content_hash", None)
    analysis_hash = _get(analysis, "content_hash", None)
    family = str(_get(file, "document_family_id", None) or file_id)
    names = _get(analysis, "association_observed_entities", None) or observed_names(analysis)
    primary = names[0] if names else None
    reviewable = not _get(file, "deleted_at", None) and _get(file, "document_version_status", "current") != "duplicate"
    payload = {"profile_id": str(profile_id), "file_id": file_id, "content_hash": file_hash,
               "owner_context": owner or {}, "bucket_id": str(_get(file, "bucket_id", "") or ""),
               "analysis_id": str(analysis_id) if analysis_id else None,
               "analysis_version": _get(analysis, "analysis_version", 0), "expected_entity": expected_entity,
               "observed_entities": names, "classification": _get(analysis, "association_classification", None) or _get(analysis, "classification", None),
               "document_family_id": family, "document_version_number": _get(file, "document_version_number", 1) or 1}
    source_stamp = stamp(payload)
    history = [row for row in (state or {}).get("events", []) if row.get("identity_file_id")
               and (row["identity_file_id"] == file_id or row.get("document_family_id") == family)]
    latest = history[-1] if history else None
    current_event = latest if latest and latest.get("source_stamp") == source_stamp else None
    stale = bool(latest and latest.get("action") != "reset" and current_event is None)
    automatic = "matched" if expected_entity and entities_match(expected_entity, primary) else "conflict" if expected_entity and primary else "unconfirmed"
    status = "stale" if stale else "accepted" if current_event and current_event["action"] == "accept" else "denied" if current_event and current_event["action"] == "deny" else automatic
    created = _get(file, "created_at", None) or _get(file, "document_created_at", None)
    return {**payload, "file_name": _get(file, "file_name", "Document"), "observed_entity": primary,
            "document_created_at": created.isoformat() if isinstance(created, datetime) else created,
            "source_stamp": source_stamp, "status": status, "automatic_status": automatic,
            "allows_financial_use": status in {"matched", "accepted"}, "document_accepted": bool(document_accepted),
            "can_review": bool(reviewable and analysis_id and re.fullmatch(r"[a-fA-F0-9]{64}", file_hash or "")
                               and file_hash == analysis_hash and _get(analysis, "status", "completed") == "completed"),
            "current_decision": current_event,
            "association_stamp": stamp({"source_stamp": source_stamp, "status": status, "decision_id": latest.get("id") if latest else None}),
            "history": [{**row, "stale": row.get("source_stamp") != source_stamp} for row in history]}


async def expected_business(db, profile):
    from app.dealer_os.models import DealerBusiness
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    intake = await db.get(PublicUnderwritingIntake, profile.intake_id) if getattr(profile, "intake_id", None) else None
    dealer = await db.get(DealerBusiness, profile.dealer_id) if getattr(profile, "dealer_id", None) else None
    from sqlalchemy.ext.asyncio import AsyncSession

    if isinstance(db, AsyncSession):
        if intake is not None:
            await db.refresh(intake, attribute_names=["business_name"])
        if dealer is not None:
            await db.refresh(dealer, attribute_names=["legal_name", "name"])
    if intake and intake.business_name:
        return intake.business_name
    if dealer:
        return dealer.legal_name or dealer.name
    if getattr(profile, "loan_id", None):
        from app.models.loan import Loan

        loan = await db.get(Loan, profile.loan_id)
        return getattr(loan, "business_name", None) if loan else None
    return None


async def load_context(db, profile, files, analyses, expected_entity=None, accepted_ids=None):
    expected = expected_entity if expected_entity is not None else await expected_business(db, profile)
    values = files.values() if isinstance(files, dict) else files
    return {str(file.id): record_for_source(profile.id, expected, file, analyses.get(file.id),
            getattr(profile, "financial_metric_review", None), document_accepted=file.id in (accepted_ids or set()), owner=owner_context(profile)) for file in values}


def attach_rows(rows, *, profile_id, expected_entity, state=None, owner=None):
    return [{**row, "identity_association": record_for_source(profile_id, expected_entity, row, row, state,
              document_accepted=row.get("accepted", False), owner=owner)} for row in rows]


def summary(record):
    return {key: record.get(key) for key in ("file_id", "content_hash", "analysis_id", "status", "expected_entity",
            "observed_entity", "association_stamp")} if record else None


def display_record(record):
    """Shared evidence surfaces include status, never private review text."""
    if not record:
        return None
    return {**record, "current_decision": {key: record["current_decision"].get(key) for key in ("action", "at", "revision")}
            if record.get("current_decision") else None, "history": []}


def review_context_stamp(profile, expected_entity):
    return stamp({"owner": owner_context(profile), "expected_entity": expected_entity,
                  "revision": (getattr(profile, "financial_metric_review", None) or {}).get("revision", 0)})


async def review_context_current(db, profile, captured_stamp):
    """Call after the document and profile locks; never publish an old model run."""
    return captured_stamp == review_context_stamp(profile, await expected_business(db, profile))


async def additional_context(db, profile, file_ids, expected_entity=None):
    """Literal published-version ancestors, without promoting their old facts."""
    from app.models.bucket import BucketFile, BucketFileAnalysis
    from app.services import application_profiles

    if not file_ids:
        return {}
    allowed = await application_profiles._profile_evidence_file_ids(db, profile)
    selected = {key for key in file_ids if key is not None and key in allowed}
    if not selected:
        return {}
    files = list((await db.execute(select(BucketFile).where(BucketFile.id.in_(selected), BucketFile.deleted_at.is_(None)))).scalars().all())
    rows = list((await db.execute(select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id.in_(selected)).order_by(
        BucketFileAnalysis.analysis_version.desc(), BucketFileAnalysis.created_at.desc()))).scalars().all())
    by_id = {file.id: file for file in files}
    latest = {}
    for analysis in rows:
        if analysis.bucket_file_id in by_id and analysis.content_hash == by_id[analysis.bucket_file_id].content_hash:
            latest.setdefault(analysis.bucket_file_id, analysis)
    return await load_context(db, profile, files, latest, expected_entity=expected_entity)


def version_projection(item, records):
    related = [records.get(str(item.get(key))) for key in ("source_file_id", "original_source_file_id")]
    if not any(source_excluded(row) for row in related):
        return item
    return {key: value for key, value in {**item, "key_facts": {}, "authoritative": False,
            "identity_association_conflict": True, "financial_values_excluded": True,
            "association_note": "The source business association is denied or stale. The immutable version remains in history; review and republish before using its figures."}.items()
            if key not in {"debt_service_for_dscr", "debt_rows"}}


def change(state, records, *, file_id, action, reason, expected_revision, source_stamp, idempotency_key, actor, now=None):
    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    state = state or {}
    request = {"identity_file_id": str(file_id), "action": action, "reason": reason.strip(),
               "expected_revision": expected_revision, "source_stamp": source_stamp}
    request_stamp = stamp(request)
    for event in state.get("events", []):
        if event.get("idempotency_key") == idempotency_key:
            if event.get("request_stamp") != request_stamp:
                raise HTTPException(409, "Idempotency key already used for a different review")
            return state, False
    if state.get("revision", 0) != expected_revision:
        raise HTTPException(409, "Document or financial reviews changed; reload before saving")
    target = records.get(str(file_id))
    if not target or not target["can_review"]:
        raise HTTPException(422, "Review the latest completed analysis and exact current document version")
    if target["source_stamp"] != source_stamp:
        raise HTTPException(409, "Document content, analysis, version or business identity changed; reload before saving")
    if action not in {"accept", "deny", "reset"} or not reason.strip() or len(reason) > 2000:
        raise HTTPException(422, "An association action and reason are required")
    event = {"id": str(uuid.uuid4()), **request, "content_hash": target["content_hash"], "analysis_id": target["analysis_id"],
             "document_family_id": target["document_family_id"], "document_version_number": target["document_version_number"],
             "expected_entity": target["expected_entity"], "observed_entities": target["observed_entities"],
             "actor_id": str(actor.id), "actor_name": getattr(actor, "name", None) or getattr(actor, "full_name", None),
             "at": (now or datetime.now(UTC)).isoformat(), "revision": expected_revision + 1,
             "idempotency_key": idempotency_key, "request_stamp": request_stamp}
    return {**state, "revision": expected_revision + 1, "events": [*state.get("events", []), event]}, True


async def read(db, profile):
    from app.services import application_programs

    files, analyses, _ = await application_programs._evidence_inventory(db, profile, include_pending_financial=True)
    accepted = await application_programs.accepted_evidence_file_ids(db, profile, files, analyses)
    expected = await expected_business(db, profile)
    records = await load_context(db, profile, files, analyses, expected_entity=expected, accepted_ids=accepted)
    return {"profile_id": str(profile.id), "revision": (getattr(profile, "financial_metric_review", None) or {}).get("revision", 0),
            "expected_entity": expected, "documents": list(records.values())}


async def save(db, profile, actor, **payload):
    from app.models.bucket import BucketFile
    from app.services import application_profiles
    from app.services.document_version_selection import document_file_ids, lock_document_roots
    from app.services.financial_metric_review import _persist_review
    from app.services.financial_workspace import lock_financial_profile

    if actor.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(403, "Underwriting role required")
    with db.no_autoflush:
        ids = await application_profiles._profile_evidence_file_ids(db, profile)
        if payload["file_id"] not in ids:
            raise HTTPException(404, "Evidence file not found on this application")
        file = await db.get(BucketFile, payload["file_id"])
        await lock_document_roots(db, [file])
        await lock_financial_profile(db, profile)
        if payload["file_id"] not in await document_file_ids(db, [payload["file_id"]], usable=False):
            raise HTTPException(409, "A newer document version exists; review its current source instead")
        profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.id == profile.id).execution_options(populate_existing=True))).scalar_one()
        await db.refresh(file)
    current = await read(db, profile)
    state, changed = change(getattr(profile, "financial_metric_review", None), {row["file_id"]: row for row in current["documents"]}, actor=actor, **payload)
    if changed:
        await _persist_review(db, profile, actor, state, action=f"document.association.{payload['action']}",
                              summary="Reviewed the business association of one exact source document", event_prefix="document-association")
    return await read(db, profile)
