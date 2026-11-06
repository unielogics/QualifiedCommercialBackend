"""Reviewed document/image terms and evidence association for one explicitly chosen loan.

Preview creates an import, never a debt row or evidence link. Applying selected
fields reuses the workspace draft boundary and attaches the source only to the
chosen active obligation. Publication remains an explicit Save version.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.models.bucket import BucketFileAnalysis
from app.models.financial_form_version import FinancialFormImport
from app.services import financial_statements
from app.services import financial_workspace as workspace
from app.services.debt_terms import (
    FREQUENCY_MONTHLY_MULT,
    DebtTermsError,
    monthly_equivalent,
    normalize_terms,
)

CONTRACT_CLASSES = {
    "collateral_debt_evidence", "payoff_or_mortgage_statement", "debt_schedule", "loan_agreement",
    "mca_agreement", "merchant_cash_advance", "financing_contract", "contract", "other",
    "floorplan_mca_inventory", "advance_agreement", "payoff_letter", "loan_statement",
}
DOCUMENT_ROLES = {"agreement", "payoff", "supporting"}
CONTRACT_INSTRUCTIONS = (
    "This source is explicitly selected loan evidence: a financing agreement, loan/SBA account statement, MCA contract, payoff letter, or still screenshot. "
    "Return every distinct obligation it actually describes, in source order; never choose or merge loans. "
    "factor_rate is the separately printed MCA payback multiplier (for example 1.38), never an APR. "
    "rate is only a printed interest percentage. Never convert one into the other. "
    "payment_amount is the contract's native payment/remittance, and payment_frequency is its printed "
    "daily, weekly, biweekly (every two weeks), or monthly cadence. Semimonthly/twice-monthly is NOT "
    "biweekly: leave unsupported or unstated frequency null and explain it in warnings. "
    "monthly_payment is populated only if the source explicitly states a monthly payment; the server "
    "calculates any monthly equivalent. Missing contract terms stay null. "
    "A bank deposit/account balance is never a loan principal balance. Bank debits alone are observations, "
    "not proof of contractual frequency, interest rate, original amount, or remaining debt. "
    "Extract all printed debt-schema fields including type, original amount, balance, APR, originated/maturity "
    "dates, secured status, payment status and collateral; SBA is a loan type, never an MCA factor."
)


def _name(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _conflict(before, after) -> bool:
    if before in (None, ""):
        return False
    try:
        return Decimal(re.sub(r"[,$%\s]", "", str(before))) != Decimal(re.sub(r"[,$%\s]", "", str(after)))
    except InvalidOperation:
        return _name(before) != _name(after)


def select_obligation(body: dict, target: dict) -> dict:
    """A selected destination is not permission to choose an arbitrary loan."""
    meaningful = tuple(key for key in financial_statements.DEBT_COLUMNS if key not in {"lender", "debt_type", "notes"})
    candidates = [row for row in body.get("debts") or [] if isinstance(row, dict)
                  and any(row.get(key) not in (None, "") for key in meaningful)]
    if len(candidates) == 1:
        return candidates[0]
    named = [row for row in candidates if _name(row.get("lender"))
             and _name(row.get("lender")) == _name(target.get("lender"))]
    if len(named) == 1:
        return named[0]
    if not candidates:
        raise HTTPException(422, "This PDF did not contain readable loan terms for the selected obligation")
    raise HTTPException(409, {"code": "ambiguous_contract_obligation", "message":
        "This PDF contains several obligations and cannot identify one safely. Choose a document for this specific loan or separate the relevant contract pages.",
        "candidates": [{key: row.get(key) for key in ("lender", "original_amount", "balance", "payment_amount", "payment_frequency")}
                       for row in candidates[:30]]})


def _target(body: dict, row_id: UUID | str) -> dict:
    row = next((row for row in body.get("debts") or [] if str(row.get("id")) == str(row_id)), None)
    if row is None:
        raise HTTPException(404, "The selected obligation is not on this financial draft")
    if row.get("status", "active") != "active" or row.get("duplicate_of_id"):
        raise HTTPException(409, "Restore this deleted or duplicate obligation before reading new terms")
    return row


def import_context(imported: FinancialFormImport) -> dict | None:
    body = imported.proposed_body or {}
    context = body.get("import_context")
    if not body.get("target_debt_id") and not context:
        return None
    if (not isinstance(context, dict) or context.get("kind") != "debt_document"
            or context.get("target_row_id") != body.get("target_debt_id") or context.get("role") not in DOCUMENT_ROLES):
        raise HTTPException(422, "This loan document import has invalid target metadata")
    return context


def validate_targeted_import(imported: FinancialFormImport, *, kind: str, current_body: dict, mappings: dict) -> dict | None:
    context = import_context(imported)
    if context is None:
        return None
    if kind != "debt_schedule":
        raise HTTPException(422, "Loan document terms require a debt schedule")
    target = _target(current_body, context["target_row_id"])
    allowed = {f"{target['id']}.{key}" for key in financial_statements.DEBT_COLUMNS}
    if set(mappings) - allowed:
        raise HTTPException(422, "This import contains fields outside the selected obligation")
    source_rows = imported.proposed_body.get("debts") or []
    if len(source_rows) != 1 or str(source_rows[0].get("id")) != str(target["id"]):
        raise HTTPException(422, "This import does not match its selected obligation")
    # Accepting native terms may deterministically change monthly. An unchecked
    # conflicting monthly proposal must not be bypassed by that side effect.
    prefix = f"{target['id']}."
    changed_native = False
    for field in ("payment_amount", "payment_frequency"):
        if prefix + field not in mappings:
            continue
        old, new = target.get(field), mappings[prefix + field]
        if field == "payment_amount":
            try:
                changed_native |= monthly_equivalent(old, "monthly") != monthly_equivalent(new, "monthly")
            except DebtTermsError as error:
                raise HTTPException(422, str(error)) from error
        else:
            changed_native |= old != new
    if changed_native:
        try:
            expected_monthly = monthly_equivalent(mappings.get(prefix + "payment_amount", target.get("payment_amount")),
                                                  mappings.get(prefix + "payment_frequency", target.get("payment_frequency")))
            old_monthly = monthly_equivalent(target.get("monthly_payment"), "monthly")
            accepted_monthly = monthly_equivalent(mappings.get(prefix + "monthly_payment"), "monthly")
        except DebtTermsError as error:
            raise HTTPException(422, str(error)) from error
        if expected_monthly is not None and old_monthly is not None and expected_monthly != old_monthly and accepted_monthly != expected_monthly:
            raise HTTPException(409, {"code": "monthly_equivalent_review_required", "message":
                "Review and select the monthly equivalent before applying these payment terms, or deselect the native payment fields."})
    return context


def attach_accepted_source(body: dict, imported: FinancialFormImport, context: dict) -> None:
    row = _target(body, context["target_row_id"])
    row["evidence_links"] = workspace._merge_import_links(row.get("evidence_links") or row.get("documents") or [], [{
        "file_id": str(imported.source_file_id), "role": context["role"], "is_current": False,
    }])
    row["evidence"] = {**(row.get("evidence") or {}), "last_import_file_id": str(imported.source_file_id)}


def _read(imported: FinancialFormImport, target: dict) -> dict:
    context = import_context(imported)
    result = workspace._import_read(imported, "debt_schedule")
    mappings = copy.deepcopy(imported.mappings)
    for mapping in mappings:
        field = mapping["key"].rpartition(".")[2]
        mapping["before"] = target.get(field)
        mapping["conflict"] = _conflict(target.get(field), mapping["value"])
    return {**result, "target_row_id": context["target_row_id"], "role": context["role"], "mappings": mappings}


async def propose_document_import(db, profile, *, row_id: UUID, file_id: UUID, expected_revision: int,
                                  family_id: UUID | None = None, role: str = "agreement", actor=None) -> dict:
    if role not in DOCUMENT_ROLES:
        raise HTTPException(422, "Choose a supported loan document role")
    family, draft = await workspace.resolve_family(db, profile, kind="debt_schedule", family_id=family_id, actor=actor)
    workspace._check_revision(draft, expected_revision)
    target = _target(draft.body, row_id)
    file = await workspace.source_file_for_profile(db, profile, file_id)
    from app.services.document_version_selection import document_file_ids

    if file.id not in await document_file_ids(db, [file.id], usable=False):
        raise HTTPException(409, "A newer document version exists. Read the latest loan terms instead.")
    query = select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id == file.id,
                                             BucketFileAnalysis.status == "completed")
    if file.content_hash:
        query = query.where(BucketFileAnalysis.content_hash == file.content_hash)
    analysis = (await db.execute(query.order_by(BucketFileAnalysis.created_at.desc()).limit(1))).scalar_one_or_none()
    if ((analysis and (getattr(analysis, "provider", None) == "drafted_form" or (analysis.analysis or {}).get("financial_version")))
            or getattr(file, "source_entity_type", None) in {"financial_form_version", "financial_version"}):
        raise HTTPException(409, "Choose an uploaded agreement or payoff letter rather than a generated financial version")
    image_named = str(file.content_type or "").lower() in {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/heic", "image/heif"} or file.file_name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif"))
    if analysis and analysis.classification and analysis.classification not in CONTRACT_CLASSES and not (
        analysis.classification == "bank_statement" and image_named
    ):
        raise HTTPException(409, "This source was classified as a different document. Choose this loan’s agreement, statement, payoff letter, or screenshot")
    try:
        raw = await asyncio.to_thread(workspace._read_file, file)
    except Exception as error:
        raise HTTPException(502, "The uploaded loan source could not be read") from error
    from app.services.financial_source_images import validated_image_media

    image_media = await asyncio.to_thread(validated_image_media, raw)
    if analysis and analysis.classification == "bank_statement" and image_media is None:
        raise HTTPException(409, "Use a loan-specific statement or screenshot to map terms; general bank statements remain supporting evidence.")
    if image_media is None and b"%PDF" not in raw[:1024]:
        raise HTTPException(422, "Choose an uploaded PDF or a readable PNG, JPEG, GIF, WebP, or HEIC still image")
    content_hash = hashlib.sha256(raw).hexdigest()
    if file.content_hash and file.content_hash != content_hash:
        raise HTTPException(409, "The source document changed. Reanalyze its current contents before mapping terms")
    cached = (await db.execute(select(FinancialFormImport).where(
        FinancialFormImport.family_id == family.id, FinancialFormImport.source_file_id == file.id,
        FinancialFormImport.proposed_body["target_debt_id"].astext == str(row_id),
        FinancialFormImport.proposed_body["document_role"].astext == role,
        FinancialFormImport.content_hash == content_hash,
    ).order_by(FinancialFormImport.created_at.desc()).limit(1))).scalar_one_or_none()
    if cached and cached.status == "proposed" and cached.base_revision == draft.revision:
        return _read(cached, target)
    if cached:
        proposed, mappings, warnings = copy.deepcopy(cached.proposed_body), copy.deepcopy(cached.mappings), list(cached.warnings)
    else:
        body, references, warnings, pages = await workspace._extract_full_form(db, "debt_schedule", file, raw, extra_instructions=CONTRACT_INSTRUCTIONS)
        candidate = select_obligation(body, target)
        if candidate.get("lender") and target.get("lender") and _name(candidate["lender"]) != _name(target["lender"]):
            warnings.append(f"The source names {candidate['lender']}; the selected row is {target['lender']}. Confirm that this document belongs to this loan before applying its terms.")
        try:
            candidate = workspace._json(normalize_terms(candidate))
            equivalent = monthly_equivalent(candidate.get("payment_amount"), candidate.get("payment_frequency"))
        except DebtTermsError as error:
            raise HTTPException(422, str(error)) from error
        printed_monthly = candidate.get("monthly_payment") if candidate.get("monthly_payment_source") == "stated" else None
        if equivalent is not None:
            if printed_monthly is not None and monthly_equivalent(printed_monthly, "monthly") != equivalent:
                warnings.append(f"The source also states a monthly payment of {printed_monthly}. The monthly equivalent from its native payment terms is {equivalent}; review this difference before accepting the calculated equivalent.")
            candidate["monthly_payment"], candidate["monthly_payment_source"] = str(equivalent), "native_terms"
        source_id = str(candidate["id"])
        body["debts"] = [candidate if str(row.get("id")) == source_id else row for row in body.get("debts") or []]
        mappings = workspace._mapping_records("debt_schedule", body, file, analysis, content_hash, references, pages)
        mappings = [mapping for mapping in mappings if mapping["key"].startswith(f"{source_id}.")]
        if image_media:
            for mapping in mappings:
                mapping["source"].pop("page", None)
                mapping["source"].update(kind="image", content_type=image_media)
                mapping["confidence"] = "needs_review"
        # Native terms drive the deterministic equivalent when accepted. A
        # calculated monthly value must never masquerade as a printed field.
        if candidate.get("monthly_payment_source") == "native_terms":
            native = {mapping["key"].rpartition(".")[2]: mapping for mapping in mappings}
            monthly = native.get("monthly_payment")
            if monthly:
                monthly["derived"] = True
                monthly["source"] = {"file_id": str(file.id), "analysis_id": str(analysis.id) if analysis else None,
                    "content_hash": content_hash, "kind": "calculation", "calculation": "payment_amount × monthly multiplier",
                    "multiplier": str(FREQUENCY_MONTHLY_MULT[candidate["payment_frequency"]]),
                    "derived_from": {field: copy.deepcopy(native[field]["source"]) for field in ("payment_amount", "payment_frequency") if field in native}}
                if printed_monthly is not None:
                    monthly["source"]["printed_monthly_payment"] = printed_monthly
        for mapping in mappings:
            mapping["key"] = f"{row_id}.{mapping['key'].rpartition('.')[2]}"
        values = {key: copy.deepcopy(candidate[key]) for key in financial_statements.DEBT_COLUMNS if candidate.get(key) not in (None, "")}
        proposed = {"debts": [{"id": str(row_id), **values}], "target_debt_id": str(row_id), "document_role": role,
                    "import_context": {"kind": "debt_document", "target_row_id": str(row_id), "role": role}}
        if candidate.get("monthly_payment_source") == "native_terms":
            proposed["debts"][0]["monthly_payment_source"] = "native_terms"
            warnings.append("The monthly equivalent is calculated from the printed payment and frequency when those fields are accepted; it is not a separately printed monthly payment.")
        if not mappings:
            raise HTTPException(422, "No supported printed loan terms were found in this source")
    imported = FinancialFormImport(id=uuid4(), family_id=family.id, source_file_id=file.id, content_hash=content_hash,
        base_revision=draft.revision, proposed_body=proposed, mappings=mappings, warnings=warnings,
        status="proposed", created_at=workspace.now(), created_by_user_id=getattr(actor, "id", None))
    db.add(imported)
    await db.flush()
    return _read(imported, target)
