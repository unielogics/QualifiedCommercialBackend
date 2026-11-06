"""Reviewed mortgage-statement mappings and revocable, source-bound proof.

An attachment or model suggestion never satisfies a mortgage request. Staff
review is recorded on the server import; the draft references that proof, and
only an explicit published version promotes it into the collection engine.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.application_profile import (
    ApplicationRequirementEvidence,
)
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.financial_form_version import FinancialFormFamily, FinancialFormImport
from app.models.funding_program import ApplicationRequirementEvidenceDecision
from app.models.user import User
from app.schemas.application_profile import EvidenceDecisionOverride
from app.services import application_programs, file_requests, real_estate_schema
from app.services import financial_workspace as workspace

KIND = "real_estate_schedule"
SCOPE = "mortgage_statement"
STAFF_ROLES = {Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.BROKER, Role.LOAN_EXEC, Role.FIELD_REP}
LINK_KEY = "mortgage_statement_import_ids"
PROPERTY_KEYS = {spec["key"] for spec in real_estate_schema.PROPERTY_FIELDS}
MORTGAGE_KEYS = {spec["key"] for spec in real_estate_schema.MORTGAGE_FIELDS} - {"property_row_id"}
STATEMENT_INSTRUCTIONS = (
    "This is a mortgage-statement evidence review, not a real-estate valuation. Extract each distinct "
    "mortgage actually printed in a current/periodic mortgage loan statement, with its loan reference, "
    "statement date, balance, monthly payment, interest and escrow when printed. Keep different loans separate. "
    "Property address MUST be the explicitly identified collateral/property address, NEVER the borrower's "
    "mailing or correspondence address. Preserve apartment/unit numbers. Do not infer a property value, rent "
    "or ownership percentage from loan terms. If it is only a payoff quote, purchase document, schedule or "
    "unrelated PDF rather than a mortgage statement, return empty properties and mortgages with a warning. "
    "Unprinted values remain null. Source references identify actual printed pages and quotations."
)


def require_staff(actor):
    if not actor or not actor.id or actor.role not in STAFF_ROLES:
        raise HTTPException(403, "Mortgage evidence requires an internal staff reviewer")


def _normal(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _ref(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def identity_fingerprint(property_row: dict, mortgage_row: dict | None = None) -> str:
    # Unit, owner and reference distinguish loans. Balances/payments never do.
    material = [_normal(property_row.get(key)) for key in ("address", "city", "state", "postal_code", "country", "legal_owner")]
    material += [_normal((mortgage_row or {}).get("lender")), _ref((mortgage_row or {}).get("loan_reference"))]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def _row(body, block, row_id):
    return next((row for row in body.get(block, []) if str(row.get("id")) == str(row_id)), None)


def _target(body, property_id, mortgage_id):
    prop, loan = _row(body, "properties", property_id), _row(body, "mortgages", mortgage_id)
    if prop is None or loan is None or str(loan.get("property_row_id")) != str(prop["id"]):
        raise HTTPException(422, "Choose a mortgage belonging to this property on the current draft")
    return prop, loan


def _conflict(before, after):
    if before in (None, "", "unknown"):
        return False
    try:
        return Decimal(str(before).replace(",", "").replace("$", "")) != Decimal(str(after).replace(",", "").replace("$", ""))
    except InvalidOperation:
        return _normal(before) != _normal(after)


def candidates(body):
    result = []
    for loan in body.get("mortgages", []):
        if not any(loan.get(key) not in (None, "", "unknown") for key in MORTGAGE_KEYS):
            continue
        prop = _row(body, "properties", loan.get("property_row_id")) or {}
        result.append({"source_mortgage_id": str(loan["id"]),
            "property": {key: prop.get(key) for key in PROPERTY_KEYS},
            "mortgage": {key: loan.get(key) for key in MORTGAGE_KEYS}, "source_property_id": prop.get("id")})
    return result


def associations(source_candidates, body):
    result = []
    for candidate in source_candidates:
        source_prop, source_loan = candidate["property"], candidate["mortgage"]
        for loan in body.get("mortgages", []):
            prop = _row(body, "properties", loan.get("property_row_id"))
            if not prop:
                continue
            reasons, contradiction = [], False
            if source_prop.get("address") and prop.get("address"):
                if _normal(source_prop["address"]) == _normal(prop["address"]):
                    reasons.append("Exact property street address, including unit")
                else:
                    contradiction = True
            for key in ("city", "state", "postal_code"):
                if source_prop.get(key) and prop.get(key) and _normal(source_prop[key]) != _normal(prop[key]):
                    contradiction = True
            printed_ref, stored_ref = _ref(source_loan.get("loan_reference")), _ref(loan.get("loan_reference"))
            exact_reference = bool(printed_ref and stored_ref and printed_ref == stored_ref)
            if exact_reference:
                reasons.append("Exact loan reference")
            elif printed_ref and stored_ref:
                if min(len(printed_ref), len(stored_ref)) == 4 and (printed_ref.endswith(stored_ref) or stored_ref.endswith(printed_ref)):
                    reasons.append("Matching last four; full loan identity needs review")
                else:
                    contradiction = True
            if source_loan.get("lender") and loan.get("lender") and _normal(source_loan["lender"]) == _normal(loan["lender"]):
                reasons.append("Same lender (supporting detail only)")
            if not contradiction and reasons and (exact_reference or any("address" in reason for reason in reasons)):
                result.append({"source_mortgage_id": candidate["source_mortgage_id"], "property_row_id": str(prop["id"]),
                    "mortgage_row_id": str(loan["id"]), "reasons": reasons,
                    "match": "exact" if exact_reference else "possible"})
    # Even unique suggestions require staff confirmation; repeated addresses or
    # reused references never become an automatic association.
    return result


def _contradictions(candidate, prop, loan):
    conflicts = []
    for key in ("address", "city", "state", "postal_code", "legal_owner"):
        if candidate["property"].get(key) and prop.get(key) and _normal(candidate["property"][key]) != _normal(prop[key]):
            conflicts.append(f"Printed property {key.replace('_', ' ')} differs from the selected property")
    source_ref, current_ref = _ref(candidate["mortgage"].get("loan_reference")), _ref(loan.get("loan_reference"))
    if source_ref and current_ref and source_ref != current_ref:
        conflicts.append("Printed loan reference differs from the selected mortgage; masked references also require review")
    if candidate["mortgage"].get("lender") and loan.get("lender") and _normal(candidate["mortgage"]["lender"]) != _normal(loan["lender"]):
        conflicts.append("Printed lender differs from the selected mortgage")
    return conflicts


async def _source(db, profile, file_id, *, expected_hash=None, read=True):
    from app.services.document_version_selection import document_file_ids

    file = await workspace.source_file_for_profile(db, profile, file_id)
    if file_id not in await document_file_ids(db, [file_id], usable=False):
        raise HTTPException(409, "A newer document version exists. Review its latest mortgage statement instead.")
    if not (file.content_type == "application/pdf" or file.file_name.lower().endswith(".pdf")):
        raise HTTPException(422, "Choose a readable uploaded mortgage statement PDF")
    if getattr(file, "source_kind", None) == "drafted_form":
        raise HTTPException(422, "A generated financial form is not a mortgage statement")
    try:
        raw = await asyncio.to_thread(workspace._read_file, file) if read else None
    except Exception as error:
        raise HTTPException(502, "The mortgage statement PDF could not be read") from error
    content_hash = hashlib.sha256(raw).hexdigest() if raw is not None else file.content_hash
    if not content_hash or (file.content_hash and file.content_hash != content_hash) or (expected_hash and expected_hash != content_hash):
        raise HTTPException(409, "The statement source changed; read and review its current contents again")
    if not file.content_hash:
        file.content_hash = content_hash
    return file, raw, content_hash


async def _load_import(db, profile, import_id):
    imported = await db.get(FinancialFormImport, import_id)
    family = await db.get(FinancialFormFamily, imported.family_id) if imported else None
    if (family is None or family.profile_id != profile.id or family.kind != KIND
            or (imported.proposed_body or {}).get("import_scope") != SCOPE):
        raise HTTPException(404, "Mortgage statement review not found on this file")
    return imported, family


def _read(imported, body):
    proposed = imported.proposed_body
    context = proposed.get("import_context") or {}
    result = workspace._import_read(imported, KIND)
    return {**result, "evidence": {"candidates": candidates(proposed["extracted_body"]),
        "associations": [*(proposed.get("destination_associations") or []), *associations(candidates(proposed["extracted_body"]), body)],
        "target_property_id": context.get("target_property_id"), "target_mortgage_id": context.get("target_mortgage_id"),
        "source_mortgage_id": context.get("source_mortgage_id"),
        "create_property": context.get("create_property", False), "create_mortgage": context.get("create_mortgage", False),
        "needs_association": not context.get("target_mortgage_id"), "contradictions": context.get("contradictions", []),
        "association_override_required": bool(context.get("contradictions")), "override_reasons": context.get("contradictions", [])}}


async def propose_import(db, profile, *, family_id, file_id, expected_revision, property_row_id=None,
                         mortgage_row_id=None, source_mortgage_id=None, create_property=False, create_mortgage=False, actor=None):
    family, draft = await workspace.resolve_family(db, profile, kind=KIND, family_id=family_id, actor=actor)
    workspace._check_revision(draft, expected_revision)
    file, raw, content_hash = await _source(db, profile, file_id)
    analysis = (await db.execute(select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id == file.id,
        BucketFileAnalysis.status == "completed").order_by(BucketFileAnalysis.created_at.desc()).limit(1))).scalar_one_or_none()
    if analysis and (analysis.provider == "drafted_form" or analysis.classification not in {None, "other", "payoff_or_mortgage_statement", "collateral_debt_evidence", "mortgage_statement"}):
        raise HTTPException(422, "This source is not classified as mortgage statement evidence")
    prior = (await db.execute(select(FinancialFormImport).where(FinancialFormImport.family_id == family.id,
        FinancialFormImport.content_hash == content_hash,
        FinancialFormImport.proposed_body["import_scope"].astext == SCOPE).order_by(FinancialFormImport.created_at.desc()))).scalars().all()
    request_key = hashlib.sha256(json.dumps([str(file.id), str(property_row_id) if property_row_id else None,
        str(mortgage_row_id) if mortgage_row_id else None, str(source_mortgage_id) if source_mortgage_id else None,
        create_property, create_mortgage], separators=(",", ":")).encode()).hexdigest()
    replay = next((item for item in prior if item.status == "proposed" and item.base_revision == draft.revision
        and ((item.proposed_body or {}).get("import_context") or {}).get("request_key") == request_key), None)
    if replay:
        return _read(replay, draft.body)
    cached = prior[0] if prior else None
    if cached:
        extracted = copy.deepcopy(cached.proposed_body["extracted_body"])
        source_mappings = copy.deepcopy(cached.proposed_body["source_mappings"])
        for mapping in source_mappings:
            mapping["source"] = {**mapping["source"], "file_id": str(file.id),
                                 "analysis_id": str(analysis.id) if analysis else None, "content_hash": content_hash}
        warnings = list(cached.warnings)
    else:
        extracted, references, warnings, pages = await workspace._extract_full_form(db, KIND, file, raw,
            extra_instructions=STATEMENT_INSTRUCTIONS, document_role="mortgage_statement")
        source_mappings = workspace._mapping_records(KIND, extracted, file, analysis, content_hash, references, pages)
    available = candidates(extracted)
    if not available:
        raise HTTPException(422, "No readable mortgage statement loan details were found; choose the statement PDF rather than a payoff quote or unrelated document")
    context = {"kind": "real_estate_mortgage_statement", "create_property": create_property,
               "create_mortgage": create_mortgage, "request_key": request_key}
    mappings = []
    if property_row_id or mortgage_row_id or create_property or create_mortgage:
        candidate = next((item for item in available if item["source_mortgage_id"] == str(source_mortgage_id)), None)
        if candidate is None and source_mortgage_id is None and len(available) == 1:
            candidate = available[0]
        if candidate is None:
            raise HTTPException(422, "Select the distinct source mortgage to review")
        if create_property:
            if property_row_id or mortgage_row_id or not create_mortgage or not candidate["property"].get("address"):
                raise HTTPException(422, "Creating a property requires its printed collateral address and a new mortgage")
            property_row_id, prop = uuid4(), {}
        else:
            prop = _row(draft.body, "properties", property_row_id)
            if prop is None:
                raise HTTPException(422, "Choose the existing property")
        if create_mortgage:
            if mortgage_row_id:
                raise HTTPException(422, "Choose either an existing mortgage or a new mortgage")
            mortgage_row_id, loan = uuid4(), {}
        else:
            prop, loan = _target(draft.body, property_row_id, mortgage_row_id)
        context.update(target_property_id=str(property_row_id), target_mortgage_id=str(mortgage_row_id),
            source_mortgage_id=candidate["source_mortgage_id"], source_property_id=candidate["source_property_id"],
            contradictions=_contradictions(candidate, prop, loan))
        for mapping in source_mappings:
            block, _, field = mapping["key"].split(".") if mapping["key"].count(".") == 2 else (None, None, None)
            source_id = candidate["source_property_id"] if block == "properties" else candidate["source_mortgage_id"]
            target_id = property_row_id if block == "properties" else mortgage_row_id
            allowed = PROPERTY_KEYS if block == "properties" and create_property else MORTGAGE_KEYS if block == "mortgages" else set()
            if field not in allowed or mapping["key"] != f"{block}.{source_id}.{field}" or mapping["value"] in (None, "", "unknown"):
                continue
            before = (prop if block == "properties" else loan).get(field)
            mappings.append({**copy.deepcopy(mapping), "key": f"{block}.{target_id}.{field}", "before": before,
                             "conflict": _conflict(before, mapping["value"])})
    destination_associations = []
    if file.requested_document_id:
        for state in await file_requests.request_states(db, profile.id):
            destination = (state.provenance or {}).get("source_context") or {}
            if (state.requested_document_id == file.requested_document_id and destination.get("family_id") == str(family.id)
                    and destination.get("mortgage_row_id") and not (state.provenance or {}).get("cancelled_at")):
                try:
                    _target(draft.body, destination.get("property_row_id"), destination.get("mortgage_row_id"))
                except HTTPException:
                    continue
                destination_associations.extend({"source_mortgage_id": candidate["source_mortgage_id"],
                    "property_row_id": destination["property_row_id"], "mortgage_row_id": destination["mortgage_row_id"],
                    "match": "possible", "reasons": ["Uploaded to this exact mortgage request; confirm the distinct printed source loan"]}
                    for candidate in available)
    proposed = {"import_scope": SCOPE, "import_context": context, "extracted_body": extracted,
                "source_mappings": source_mappings, "destination_associations": destination_associations}
    imported = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=file.id, content_hash=content_hash,
        base_revision=draft.revision, proposed_body=proposed, mappings=mappings, warnings=warnings, status="proposed",
        created_by_user_id=getattr(actor, "id", None), created_at=datetime.now(UTC))
    db.add(imported)
    await db.flush()
    return _read(imported, draft.body)


async def apply_import(db, profile, import_id, *, expected_revision, keys, confirm_association=False,
                       confirm_latest_statement=False, association_override_reason="", actor=None):
    require_staff(actor)
    if not confirm_association or not confirm_latest_statement:
        raise HTTPException(422, "Confirm the exact property and loan association and that this is its latest mortgage statement")
    imported, family = await _load_import(db, profile, import_id)
    family, draft = await workspace.resolve_family(db, profile, kind=KIND, family_id=family.id, actor=actor)
    context = imported.proposed_body["import_context"]
    if imported.status == "applied":
        review = imported.proposed_body.get("review") or {}
        if sorted(keys) != review.get("selected_keys"):
            raise HTTPException(409, "This review was already applied with different fields")
        await _valid_proof(db, profile, family, draft.body, imported)
        return await workspace.load_workspace(db, profile, kind=KIND, family_id=family.id, actor=actor)
    if imported.status != "proposed":
        raise HTTPException(409, "This review was rejected, withdrawn or superseded; read and review the statement again")
    workspace._check_revision(draft, expected_revision)
    if imported.base_revision != draft.revision:
        raise HTTPException(409, {"code": "financial_draft_conflict", "message": "The draft changed; preview this statement against the current values"})
    await _source(db, profile, imported.source_file_id, expected_hash=imported.content_hash)
    if not context.get("target_mortgage_id"):
        raise HTTPException(422, "Choose the source mortgage and confirm its destination first")
    other_reviews = (await db.execute(select(FinancialFormImport).where(
        FinancialFormImport.family_id == family.id,
        FinancialFormImport.content_hash == imported.content_hash))).scalars().all()
    if any(other.id != imported.id and other.status == "applied"
           and (other.proposed_body or {}).get("import_scope") == SCOPE
           and ((other.proposed_body or {}).get("import_context") or {}).get("source_mortgage_id") == context["source_mortgage_id"]
           and ((other.proposed_body or {}).get("import_context") or {}).get("target_mortgage_id") != context["target_mortgage_id"]
           for other in other_reviews):
        raise HTTPException(409, "This distinct source loan is already associated with another mortgage; withdraw that association before reassigning it")
    if context.get("contradictions") and len(association_override_reason.strip()) < 12:
        raise HTTPException(422, "The printed property, lender or reference conflicts with this loan. Explain the reviewed association before applying it")
    all_mappings = {mapping["key"]: mapping for mapping in imported.mappings}
    if len(keys) != len(set(keys)) or set(keys) - set(all_mappings):
        raise HTTPException(422, "Choose only the proposed statement fields")
    body = copy.deepcopy(draft.body)
    prop = _row(body, "properties", context["target_property_id"])
    loan = _row(body, "mortgages", context["target_mortgage_id"])
    if context["create_property"]:
        if f"properties.{context['target_property_id']}.address" not in keys:
            raise HTTPException(422, "Accept the printed address when creating a property")
        prop = {"id": context["target_property_id"], "mortgage_status": "unknown"}
        body.setdefault("properties", []).append(prop)
        body["no_real_estate"] = False
    if context["create_mortgage"]:
        if not any(key in keys for key in (f"mortgages.{context['target_mortgage_id']}.{field}" for field in ("lender", "loan_reference", "balance", "monthly_payment"))):
            raise HTTPException(422, "Accept identifying loan details when creating a mortgage")
        loan = {"id": context["target_mortgage_id"], "property_row_id": context["target_property_id"], "status": "unknown"}
        body.setdefault("mortgages", []).append(loan)
    if prop is None or loan is None or str(loan.get("property_row_id")) != str(prop["id"]):
        raise HTTPException(422, "The reviewed mortgage destination no longer exists")
    for key in keys:
        block, row_id, field = key.split(".")
        row = prop if block == "properties" and row_id == str(prop["id"]) else loan if block == "mortgages" and row_id == str(loan["id"]) else None
        if row is None:
            raise HTTPException(422, "A proposed field belongs to another property or loan")
        row[field] = all_mappings[key]["value"]
        body.setdefault("field_sources", {})[key] = copy.deepcopy(all_mappings[key]["source"])
    loan[LINK_KEY] = [str(imported.id)]  # one current latest statement; previous proofs remain historical
    review = {"accepted": True, "reviewed_by_user_id": str(actor.id), "reviewed_at": datetime.now(UTC).isoformat(),
        "selected_keys": sorted(keys), "identity_hash": identity_fingerprint(prop, loan),
        "association_override_reason": association_override_reason.strip(), "statement_date": next(
            candidate["mortgage"].get("statement_date") for candidate in candidates(imported.proposed_body["extracted_body"])
            if candidate["source_mortgage_id"] == context["source_mortgage_id"])}
    imported.proposed_body = {**imported.proposed_body, "review": review}
    result = await workspace.save_draft(db, profile, kind=KIND, family_id=family.id,
        expected_revision=expected_revision, body=body, actor=actor)
    imported.status = "applied"
    await db.flush()
    return result


async def _valid_proof(db, profile, family, body, imported):
    context, review = (imported.proposed_body or {}).get("import_context") or {}, (imported.proposed_body or {}).get("review") or {}
    if (imported.family_id != family.id or imported.status != "applied"
            or (imported.proposed_body or {}).get("import_scope") != SCOPE
            or not review.get("accepted") or not review.get("reviewed_by_user_id")):
        raise HTTPException(422, "The mortgage statement proof is unreviewed, withdrawn or belongs to another schedule")
    try:
        reviewer = await db.get(User, UUID(review["reviewed_by_user_id"]))
    except (ValueError, TypeError):
        reviewer = None
    if reviewer is None or reviewer.role not in STAFF_ROLES:
        raise HTTPException(422, "An eligible internal staff reviewer must confirm this mortgage statement")
    prop, loan = _target(body, context.get("target_property_id"), context.get("target_mortgage_id"))
    if str(imported.id) not in loan.get(LINK_KEY, []):
        raise HTTPException(422, "This statement proof is not linked to this mortgage")
    if review.get("identity_hash") != identity_fingerprint(prop, loan):
        raise HTTPException(422, "The property, lender or loan reference changed; review the statement association again")
    file, _, _ = await _source(db, profile, imported.source_file_id, expected_hash=imported.content_hash)
    return prop, loan, file


async def validate_published_evidence(db, *, profile, family, body):
    if family.kind != KIND or family.profile_id != profile.id:
        raise HTTPException(404, "Real estate schedule not found")
    for loan in body.get("mortgages", []):
        ids = loan.get(LINK_KEY, [])
        if not isinstance(ids, list) or len(ids) > 1:
            raise HTTPException(422, "Choose one reviewed current mortgage statement per loan")
        for identifier in ids:
            try:
                imported = await db.get(FinancialFormImport, UUID(identifier))
            except (ValueError, TypeError):
                imported = None
            if imported is None:
                raise HTTPException(422, "Mortgage statement review proof not found")
            context = (imported.proposed_body or {}).get("import_context") or {}
            if (context.get("target_mortgage_id") != str(loan.get("id"))
                    or context.get("target_property_id") != str(loan.get("property_row_id"))):
                raise HTTPException(422, "This statement proof belongs to a different property or mortgage")
            await _valid_proof(db, profile, family, body, imported)


async def _decision(db, profile, state, file, reviewer, *, accepted, reason):
    links = list((await db.execute(select(ApplicationRequirementEvidence).where(
        ApplicationRequirementEvidence.requirement_state_id == state.id,
        ApplicationRequirementEvidence.file_id == file.id, ApplicationRequirementEvidence.removed_at.is_(None)))).scalars().all())
    link = next((row for row in links if row.file_id == file.id and row.removed_at is None), None)
    if link is None and accepted:
        link = ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=state.id, file_id=file.id,
            source="operator", linked_at=datetime.now(UTC), linked_by_user_id=reviewer.id,
            provenance={"source": "mortgage_statement_review"})
        db.add(link)
        await db.flush()
    if link is None:
        return
    await application_programs.override_evidence_decision(db, profile=profile, requirement_key=state.requirement_key,
        file_id=file.id, payload=EvidenceDecisionOverride(decision="accepted" if accepted else "rejected", reason_code="other",
            confirmed=True, reason=reason), user=reviewer)


async def sync_published_evidence(db, *, profile, family, version, states):
    """Attach exact reviewed evidence without moving its original upload slot."""
    await validate_published_evidence(db, profile=profile, family=family, body=version.body)
    for state in states:
        if state.status == "not_applicable" or (state.provenance or {}).get("cancelled_at"):
            continue
        scope = (state.provenance or {}).get("source_context") or {}
        loan = _row(version.body, "mortgages", scope.get("mortgage_row_id"))
        desired = loan.get(LINK_KEY, []) if loan else []
        desired_files = set()
        for identifier in desired:
            proof = await db.get(FinancialFormImport, UUID(identifier))
            if proof:
                desired_files.add(proof.source_file_id)
        links = list((await db.execute(select(ApplicationRequirementEvidence).where(
            ApplicationRequirementEvidence.requirement_state_id == state.id,
            ApplicationRequirementEvidence.removed_at.is_(None)))).scalars().all())
        for link in links:
            proof_id = (link.provenance or {}).get("mortgage_statement_import_id")
            if proof_id and proof_id not in desired:
                old = await db.get(FinancialFormImport, UUID(proof_id))
                reviewer_id = ((old.proposed_body or {}).get("review") or {}).get("reviewed_by_user_id") if old else None
                reviewer = await db.get(User, UUID(reviewer_id)) if reviewer_id else None
                if reviewer and link.file_id not in desired_files:
                    file = await workspace.source_file_for_profile(db, profile, link.file_id)
                    await _decision(db, profile, state, file, reviewer, accepted=False, reason="The current saved schedule no longer accepts this mortgage statement association")
                if old and old.status == "applied":
                    old.status = "superseded"
        for identifier in desired:
            imported = await db.get(FinancialFormImport, UUID(identifier))
            context = imported.proposed_body.get("import_context") or {}
            if context.get("target_mortgage_id") != scope.get("mortgage_row_id") or context.get("target_property_id") != scope.get("property_row_id"):
                raise HTTPException(422, "The statement proof does not belong to this collection request's exact property and loan")
            _prop, _loan, file = await _valid_proof(db, profile, family, version.body, imported)
            review = imported.proposed_body["review"]
            reviewer = await db.get(User, UUID(review["reviewed_by_user_id"]))
            if reviewer is None:
                raise HTTPException(422, "A current staff reviewer must confirm this mortgage statement")
            existing = next((link for link in links if link.file_id == file.id and link.removed_at is None), None)
            if existing:
                current = (await application_programs._latest_evidence_decisions(db, [existing])).get(existing.id)
                if current and current.actor_kind == "staff" and current.decision != "accepted":
                    reviewed_at = datetime.fromisoformat(review["reviewed_at"])
                    if current.created_at is None or current.created_at >= reviewed_at:
                        continue  # a later explicit rejection wins over old proof/retry
                if (existing.provenance or {}).get("mortgage_statement_import_id") == identifier and existing.verified_at:
                    continue
            await _decision(db, profile, state, file, reviewer, accepted=True,
                reason=f"Reviewed latest mortgage statement for the exact property/loan; review {identifier}, saved version {version.id}")
            refreshed = list((await db.execute(select(ApplicationRequirementEvidence).where(
                ApplicationRequirementEvidence.requirement_state_id == state.id,
                ApplicationRequirementEvidence.file_id == file.id, ApplicationRequirementEvidence.removed_at.is_(None)))).scalars().all())
            for link in refreshed:
                if link.file_id == file.id and link.removed_at is None:
                    link.provenance = {**(link.provenance or {}), "mortgage_statement_import_id": identifier,
                                       "identity_hash": review["identity_hash"]}
            imported.proposed_body = {**imported.proposed_body, "review": {**review, "published_version_id": str(version.id)}}
    await db.flush()


async def invalidate_changed_identity(db, *, profile, state, identity_hash):
    """A reused UUID cannot carry acceptance to a different property/loan."""
    links = list((await db.execute(select(ApplicationRequirementEvidence).where(
        ApplicationRequirementEvidence.requirement_state_id == state.id,
        ApplicationRequirementEvidence.removed_at.is_(None)))).scalars().all())
    current = await application_programs._latest_evidence_decisions(db, links) if links else {}
    for link in links:
        if link.removed_at is not None:
            continue
        file = await db.get(BucketFile, link.file_id)
        if file is None:
            continue
        previous = current.get(link.id)
        material = f"mortgage-identity:{link.id}:{identity_hash}:{file.content_hash}"
        decision_key = hashlib.sha256(material.encode()).hexdigest()
        existing = (await db.execute(select(ApplicationRequirementEvidenceDecision).where(
            ApplicationRequirementEvidenceDecision.idempotency_key == decision_key))).scalar_one_or_none()
        if existing is None:
            db.add(ApplicationRequirementEvidenceDecision(id=uuid4(), requirement_evidence_id=link.id,
                content_hash=file.content_hash or hashlib.sha256(str(file.id).encode()).hexdigest(),
                analysis_version=previous.analysis_version if previous else 0,
                policy_version=previous.policy_version if previous else 1, decision="needs_more", reason_code="other",
                explanation="The property's address/owner or the mortgage lender/reference changed; review this evidence for the new identity",
                actor_kind="system", supersedes_decision_id=previous.id if previous else None,
                idempotency_key=decision_key))
        link.verified_at, link.verified_by_user_id = None, None
        link.provenance = {**(link.provenance or {}), "identity_review_required": identity_hash}
    if links:
        state.status = "received_unverified"
        state.verified_at, state.verified_by_user_id = None, None
        state.state_reason = "Review the mortgage statement after the property or loan identity change"
    await db.flush()


async def _accepted_link(db, state, file):
    if state is None or not file.content_hash:
        return False
    links = list((await db.execute(select(ApplicationRequirementEvidence).where(
        ApplicationRequirementEvidence.requirement_state_id == state.id,
        ApplicationRequirementEvidence.file_id == file.id, ApplicationRequirementEvidence.removed_at.is_(None)))).scalars().all())
    current = await application_programs._latest_evidence_decisions(db, links) if links else {}
    for link in links:
        if link.file_id != file.id or link.removed_at is not None or not link.verified_at:
            continue
        decision = current.get(link.id)
        if decision and (decision.decision != "accepted" or decision.content_hash != file.content_hash):
            continue
        if link.verified_by_user_id or decision and decision.decision == "accepted":
            return True
    return False


async def reject_import(db, profile, import_id, *, actor):
    require_staff(actor)
    imported, _family = await _load_import(db, profile, import_id)
    if imported.status not in {"proposed", "rejected"}:
        raise HTTPException(409, "Use Remove statement to withdraw an already accepted association")
    imported.status = "rejected"
    await db.flush()
    return {"id": str(imported.id), "status": "rejected"}


async def unlink_evidence(db, profile, *, family_id, expected_revision, mortgage_row_id, import_id=None, file_id=None, actor=None):
    require_staff(actor)
    family, draft = await workspace.resolve_family(db, profile, kind=KIND, family_id=family_id, actor=actor)
    workspace._check_revision(draft, expected_revision)
    loan = _row(draft.body, "mortgages", mortgage_row_id)
    if loan is None:
        raise HTTPException(404, "Mortgage not found")
    body = copy.deepcopy(draft.body)
    keep = []
    withdrawn_files = {file_id} if file_id else set()
    states = await file_requests.request_states(db, profile.id)
    for identifier in loan.get(LINK_KEY, []):
        imported, bound_family = await _load_import(db, profile, UUID(identifier))
        if bound_family.id != family.id:
            raise HTTPException(422, "Statement proof belongs to another schedule")
        if (import_id and str(imported.id) != str(import_id)) or (file_id and imported.source_file_id != file_id):
            keep.append(identifier)
            continue
        imported.status = "withdrawn"
        withdrawn_files.add(imported.source_file_id)
    for state in states:
        scope = (state.provenance or {}).get("source_context") or {}
        if scope.get("family_id") != str(family.id) or scope.get("mortgage_row_id") != str(mortgage_row_id):
            continue
        for source_id in withdrawn_files:
            try:
                file = await workspace.source_file_for_profile(db, profile, source_id)
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                continue  # withdrawn/deleted source must remain removable
            rows = list((await db.execute(select(ApplicationRequirementEvidence).where(
                ApplicationRequirementEvidence.requirement_state_id == state.id,
                ApplicationRequirementEvidence.file_id == file.id))).scalars().all())
            active = [row for row in rows if row.file_id == file.id and row.removed_at is None]
            if not active and not rows and file.requested_document_id == state.requested_document_id:
                link = ApplicationRequirementEvidence(id=uuid4(), requirement_state_id=state.id, file_id=file.id,
                    source="operator", linked_at=datetime.now(UTC), linked_by_user_id=actor.id, provenance={"source": "explicit_withdrawal"})
                db.add(link)
                await db.flush()
                active = [link]
            if active:
                await _decision(db, profile, state, file, actor, accepted=False,
                    reason="Staff withdrew the mortgage statement association; a new explicit review is required")
                for link in active:
                    link.removed_at, link.removed_by_user_id = datetime.now(UTC), actor.id
                    link.verified_at, link.verified_by_user_id = None, None
    _row(body, "mortgages", mortgage_row_id)[LINK_KEY] = keep
    result = await workspace.save_draft(db, profile, kind=KIND, family_id=family.id,
        expected_revision=expected_revision, body=body, actor=actor)
    await db.flush()
    return result


async def list_evidence(db, profile, *, family_id, actor=None):
    family, draft = await workspace.resolve_family(db, profile, kind=KIND, family_id=family_id, actor=actor)
    states = [state for state in await file_requests.request_states(db, profile.id)
              if ((state.provenance or {}).get("source_context") or {}).get("family_id") == str(family.id)
              and not (state.provenance or {}).get("cancelled_at")]
    by_loan = {((state.provenance or {}).get("source_context") or {}).get("mortgage_row_id"): state for state in states}
    links = []
    for loan in draft.body.get("mortgages", []):
        for identifier in loan.get(LINK_KEY, []):
            scoped_file = None
            try:
                imported, owner_family = await _load_import(db, profile, UUID(identifier))
                if owner_family.id != family.id:
                    continue
                scoped_file = await workspace.source_file_for_profile(db, profile, imported.source_file_id)
                _prop, _loan, file = await _valid_proof(db, profile, family, draft.body, imported)
                review = imported.proposed_body["review"]
                published = bool(review.get("published_version_id"))
                accepted = await _accepted_link(db, by_loan.get(str(loan["id"])), file) if published else False
                links.append({"import_id": identifier, "file_id": str(file.id), "file_name": file.file_name,
                    "property_row_id": str(loan.get("property_row_id")), "mortgage_row_id": str(loan["id"]),
                    "statement_date": review.get("statement_date"), "status": "accepted" if accepted else "invalid" if published else "staged",
                    **({"reason": "The current evidence decision is no longer accepted; review the source again"} if published and not accepted else {})})
            except (HTTPException, ValueError, TypeError) as error:
                links.append({"import_id": identifier, "mortgage_row_id": str(loan.get("id")), "property_row_id": loan.get("property_row_id"),
                    "status": "invalid", "reason": str(getattr(error, "detail", "Invalid statement association")),
                    **({"file_id": str(scoped_file.id), "file_name": scoped_file.file_name} if scoped_file else {})})
    seen = {(link.get("file_id"), link.get("mortgage_row_id")) for link in links}
    for state in states:
        scope = state.provenance["source_context"]
        files = list((await db.execute(select(BucketFile).where(BucketFile.requested_document_id == state.requested_document_id,
            BucketFile.status == "uploaded", BucketFile.deleted_at.is_(None)))).scalars().all())
        evidence_links = list((await db.execute(select(ApplicationRequirementEvidence).where(
            ApplicationRequirementEvidence.requirement_state_id == state.id))).scalars().all())
        for link in evidence_links:
            file = await db.get(BucketFile, link.file_id)
            if file and file not in files:
                files.append(file)
        for file in files:
            if getattr(file, "deleted_at", None) or file.status != "uploaded":
                continue
            key = (str(file.id), scope.get("mortgage_row_id"))
            if key in seen:
                continue
            # Never expose a cross-profile source through a stale state/link.
            try:
                await workspace.source_file_for_profile(db, profile, file.id)
            except HTTPException:
                continue
            accepted = await _accepted_link(db, state, file)
            withdrawn = any(link.file_id == file.id and link.removed_at is not None and link.removed_by_user_id
                            for link in evidence_links) and not any(link.file_id == file.id and link.removed_at is None for link in evidence_links)
            links.append({"file_id": str(file.id), "file_name": file.file_name,
                "property_row_id": scope.get("property_row_id"), "mortgage_row_id": scope.get("mortgage_row_id"),
                "statement_date": None, "status": "invalid" if withdrawn else "accepted" if accepted else "unreviewed",
                "withdrawn": bool(withdrawn), "reason": "Association withdrawn; a new explicit review is required" if withdrawn else "Uploaded to this exact mortgage request"})
            seen.add(key)
    return {"family_id": str(family.id), "revision": draft.revision, "links": links}
