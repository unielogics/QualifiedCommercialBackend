"""Compare printed structured identities without guessing from file names."""
from __future__ import annotations

import re
from typing import Any

from app.services.bucket_evidence import statement_period_month


def _record(value):
    return value if isinstance(value, dict) else {}


def _identity(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold()) or None


def _first(source, keys):
    return next((_identity(source.get(key)) for key in keys if source.get(key) not in (None, "")), None)


def _periods(facts: dict) -> set[str]:
    values = [facts.get("statement_period"), facts.get("period"), facts.get("month")]
    for row in facts.get("months") or []:
        if isinstance(row, dict):
            values.append(row.get("statement_period") or row.get("period") or row.get("month") or row.get("end_date"))
        else:
            values.append(row)
    if facts.get("period_end"):
        values.append(facts["period_end"])
    return {month for value in values if (month := statement_period_month(value))}


def _years(facts: dict) -> set[str]:
    values = [facts.get("tax_year")]
    for row in facts.get("tax_years") or []:
        values.append(row.get("year") if isinstance(row, dict) else row)
    return {str(value) for value in values if re.fullmatch(r"(?:19|20)\d{2}", str(value or ""))}


def _kind(value):
    if value in {"loan_agreement", "collateral_debt_evidence", "advance_agreement", "mca_agreement", "payoff_or_mortgage_statement", "payoff_letter"}:
        return "loan_evidence"
    return {"profit_and_loss": "current_p_and_l", "p_and_l": "current_p_and_l"}.get(value, value)


def structured_document_identity(facts: dict, classification: str | None) -> dict:
    facts = _record(facts)
    account = _record(facts.get("account"))
    business = _record(facts.get("business_identity"))
    owner = _first(facts, ("borrower_entity", "borrower_name", "business_name", "owner_name", "applicant_name")) or _first(business, ("legal_name",))
    account_reference = _first(facts, ("account_number", "bank_account_number")) or _first(account, ("account_number",))
    mask = _first(facts, ("account_mask", "account_last_four", "account_last4")) or _first(account, ("mask", "last_four", "last4"))
    if account_reference and account_reference.isdigit():
        mask = mask or account_reference[-4:]
        if len(account_reference) <= 4:
            account_reference = None
    loan = _first(facts, ("loan_reference", "loan_number", "loan_account_number", "advance_id"))
    loans = facts.get("debts") or facts.get("loans") or facts.get("advances") or []
    if isinstance(loans, list) and len(loans) == 1 and isinstance(loans[0], dict):
        loan = loan or _first(loans[0], ("loan_reference", "loan_number", "loan_account_number", "advance_id"))
        owner = owner or _first(loans[0], ("borrower_entity", "borrower_name", "business_name"))
    kind = _kind(classification)
    return {"kind": kind, "owner": owner, "account_reference": account_reference, "account_mask": mask,
            "loan_reference": loan, "periods": sorted(_periods(facts)) if kind == "bank_statement" else [],
            "tax_years": sorted(_years(facts)) if kind == "tax_return" else []}


def document_identity_conflict(new: dict, old: dict) -> str | None:
    """Only conflicting known keys block promotion; unknowns remain unknown.

    Payoff issue/good-through dates are intentionally absent: a new dated quote
    for the same loan is exactly the replacement this workflow supports.
    """
    for key, label in (
        ("kind", "document type"), ("owner", "owner or borrower"),
        ("account_reference", "bank account"), ("account_mask", "bank account"),
        ("account_id", "bank account"),
        ("loan_reference", "loan reference"), ("periods", "statement reporting period"),
        ("tax_years", "tax year"),
    ):
        left, right = new.get(key), old.get(key)
        if key == "kind" and (left in {None, "other", "unreadable"} or right in {None, "other", "unreadable"}):
            continue
        if left and right and left != right:
            return f"The replacement identifies a different {label}. Upload it as a separate document or resolve the association before using its figures."
    return None


async def bucket_identity_conflict(db: Any, file: Any, analysis: Any) -> str | None:
    if not isinstance(getattr(db, "info", None), dict) or (getattr(file, "document_version_number", None) or 1) <= 1:
        return None
    from sqlalchemy import or_, select

    from app.models.bucket import BucketFile, BucketFileAnalysis

    prior = (await db.execute(select(BucketFileAnalysis).join(BucketFile, BucketFile.id == BucketFileAnalysis.bucket_file_id).where(
        BucketFile.bucket_id == file.bucket_id, BucketFile.document_family_id == file.document_family_id,
        BucketFile.document_version_number < file.document_version_number,
        BucketFile.document_version_status != "duplicate", BucketFileAnalysis.status == "completed",
        BucketFileAnalysis.confidence == "high", BucketFileAnalysis.content_hash == BucketFile.content_hash,
        or_(BucketFileAnalysis.usability_status.is_(None), BucketFileAnalysis.usability_status.in_(["usable", "usable_with_warning", "processing"])),
    ).order_by(BucketFile.document_version_number.desc(), BucketFileAnalysis.analysis_version.desc(), BucketFileAnalysis.created_at.desc()).limit(1))).scalar_one_or_none()
    if prior is None:
        return None
    new_identity = structured_document_identity((analysis.analysis or {}).get("key_facts") or {}, analysis.classification)
    old_identity = structured_document_identity((prior.analysis or {}).get("key_facts") or {}, prior.classification)
    return document_identity_conflict(new_identity, old_identity)
