from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bucket import BucketFile, BucketFileAnalysis, BucketRequestedDocument

CATEGORY_CLASSIFICATIONS: dict[str, set[str]] = {
    "bank statement": {"bank_statement"},
    "tax return": {"tax_return"},
    "p&l": {"current_p_and_l"},
    "profit and loss": {"current_p_and_l"},
    "profit & loss": {"current_p_and_l"},
    "lease": {"lease_or_rent", "commercial_lease"},
    "rent": {"lease_or_rent"},
    "rent roll": {"lease_or_rent"},
    "purchase contract": {"purchase_contract"},
    "payoff": {"payoff_or_mortgage_statement"},
    "mortgage statement": {"payoff_or_mortgage_statement"},
    "insurance": {"insurance"},
    "hoa": {"hoa"},
    "entity": {"entity_or_vesting"},
    "vesting": {"entity_or_vesting"},
    "identity": {"identity"},
    "floorplan": {"floorplan_mca_inventory"},
    "mca": {"floorplan_mca_inventory"},
    "inventory": {"floorplan_mca_inventory"},
    "debt schedule": {"debt_schedule"},
    "personal financial statement": {"personal_financial_statement"},
    "pfs": {"personal_financial_statement"},
    "merchant processing": {"merchant_processing_statement"},
    "vendor quote": {"equipment_quote_or_invoice"},
    "equipment schedule": {"equipment_quote_or_invoice"},
    "fleet schedule": {"fleet_or_vehicle_schedule"},
    "operating authority": {"transportation_authority"},
    "ifta": {"transportation_authority"},
    "license": {"business_license_or_permit"},
    "permit": {"business_license_or_permit"},
    "franchise": {"franchise_agreement"},
    "receivable": {"accounts_receivable_aging"},
    "purchase ledger": {"inventory_or_purchase_ledger"},
    "payroll": {"payroll_report"},
}


def classifications_for_requested_doc(name: str, category: str | None) -> set[str]:
    value = f"{name} {category or ''}".casefold()
    matched: set[str] = set()
    for keyword, classifications in CATEGORY_CLASSIFICATIONS.items():
        if keyword in value:
            matched.update(classifications)
    return matched


def filename_evidence_classification(file_name: str) -> str | None:
    """Classify only high-signal filenames while document analysis is pending."""
    value = re.sub(r"[^a-z0-9]+", " ", Path(file_name).stem.casefold()).strip()
    compact = value.replace(" ", "")
    if "taxreturn" in compact or "business tax return" in value:
        return "tax_return"
    if any(token in compact for token in ("profitandloss", "profitloss", "balancesheet")) or "p l" in value:
        return "current_p_and_l"
    if "debt schedule" in value or "debtschedule" in compact:
        return "debt_schedule"
    if "personal financial statement" in value or compact.startswith("pfs"):
        return "personal_financial_statement"
    if "statement" in value or "estmt" in compact:
        if any(token in value for token in ("merchant", "processing", "credit card")):
            return "merchant_processing_statement"
        if any(token in value for token in ("income statement", "cash flow")):
            return "current_p_and_l"
        if any(token in value for token in ("brokerage", "investment")):
            return None
        if any(token in value for token in ("mortgage", "payoff", "loan statement")):
            return "payoff_or_mortgage_statement"
        return "bank_statement"
    return None


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_UUID_SUFFIX = re.compile(
    r"[-_. ]+[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def statement_months_from_filename(file_name: str) -> set[str]:
    value = _UUID_SUFFIX.sub("", Path(file_name).stem.casefold())
    result: set[str] = set()
    for match in re.finditer(r"(?<!\d)(20\d{2})[-_. /](0?[1-9]|1[0-2])(?:[-_. /](?:0?[1-9]|[12]\d|3[01]))?(?!\d)", value):
        result.add(f"{match.group(1)}-{int(match.group(2)):02d}")
    for match in re.finditer(r"(?<!\d)(0?[1-9]|1[0-2])[-_. /](?:0?[1-9]|[12]\d|3[01])[-_. /](20\d{2})(?!\d)", value):
        result.add(f"{match.group(2)}-{int(match.group(1)):02d}")
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    for match in re.finditer(rf"\b({month_names})\b[^0-9]{{0,12}}(20\d{{2}})\b", value):
        result.add(f"{match.group(2)}-{_MONTHS[match.group(1)]:02d}")
    for match in re.finditer(rf"\b(20\d{{2}})\b[^a-z0-9]{{0,12}}({month_names})\b", value):
        result.add(f"{match.group(1)}-{_MONTHS[match.group(2)]:02d}")
    return result


def statement_months_from_analysis(analysis: dict | None) -> set[str]:
    if not isinstance(analysis, dict):
        return set()
    key_facts = analysis.get("key_facts")
    if not isinstance(key_facts, dict):
        return set()
    values: list[object] = [key_facts.get("statement_period")]
    for row in key_facts.get("months") or []:
        if isinstance(row, dict):
            values.extend(row.get(key) for key in ("month", "statement_period", "period", "start_date"))
        else:
            values.append(row)
    result: set[str] = set()
    for value in values:
        match = re.search(r"(20\d{2})[-/](0[1-9]|1[0-2])", str(value or ""))
        if match:
            result.add(f"{match.group(1)}-{match.group(2)}")
    return result


def effective_file_classification(file_name: str, analysis: BucketFileAnalysis | None = None) -> str | None:
    if analysis is not None and analysis.status == "completed" and analysis.classification:
        return analysis.classification
    return filename_evidence_classification(file_name)


async def reconcile_uploaded_file(
    db: AsyncSession,
    file: BucketFile,
    analysis: BucketFileAnalysis | None = None,
) -> BucketRequestedDocument | None:
    """Route an unassigned upload only when one checklist destination is unambiguous."""
    if file.deleted_at is not None or file.status != "uploaded":
        return None
    classification = effective_file_classification(file.file_name, analysis)
    if classification == "bank_statement" and not file.statement_period:
        months = statement_months_from_analysis(analysis.analysis if analysis else None)
        months.update(statement_months_from_filename(file.file_name))
        if months:
            file.statement_period = sorted(months)[-1]

    if file.requested_document_id:
        requested = await db.get(BucketRequestedDocument, file.requested_document_id)
        if requested is not None and not requested.requires_signature:
            requested.status = "uploaded"
        return requested
    if not classification:
        return None

    documents = list(
        (
            await db.execute(
                select(BucketRequestedDocument).where(
                    BucketRequestedDocument.bucket_id == file.bucket_id,
                    BucketRequestedDocument.requires_signature.is_(False),
                )
            )
        ).scalars().all()
    )
    candidates = [
        document
        for document in documents
        if classification in classifications_for_requested_doc(document.name, document.category)
    ]
    if len(candidates) != 1:
        return None
    requested = candidates[0]
    if not requested.allow_multiple_files:
        existing = (
            await db.execute(
                select(BucketFile.id)
                .where(
                    BucketFile.requested_document_id == requested.id,
                    BucketFile.id != file.id,
                    BucketFile.status == "uploaded",
                    BucketFile.deleted_at.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
    file.requested_document_id = requested.id
    requested.status = "uploaded"
    return requested
