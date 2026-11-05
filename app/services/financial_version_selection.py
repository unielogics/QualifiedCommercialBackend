"""Accepted numeric evidence and received financial coverage are separate views."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.models.financial_form_version import FinancialFormFamily, FinancialFormVersion
from app.services.debt_terms import DebtTermsError, normalize_terms


def active_debt_schedule_rows(body: dict | None) -> list[dict]:
    """Meaningful active obligations, retaining separate same-lender loans."""
    return [row for row in (body or {}).get("debts") or [] if isinstance(row, dict)
            and row.get("status", "active") == "active" and not row.get("duplicate_of_id")
            and any(row.get(key) not in (None, "") for key in (
                "lender", "balance", "monthly_payment", "original_amount", "rate", "factor_rate",
                "payment_amount", "payment_frequency", "debt_type", "notes", "collateral",
            ))]


def debt_schedule_monthly(body: dict | None, exclude_debt_ids=None) -> Decimal | None:
    excluded = {str(value) for value in exclude_debt_ids or []}
    total = Decimal("0")
    rows = active_debt_schedule_rows(body)
    if not rows and (body or {}).get("no_debt") is not True:
        return None
    for raw in rows:
        if not raw.get("count_in_dscr", True) or str(raw.get("id")) in excluded:
            continue
        try:
            monthly = normalize_terms(raw).get("monthly_payment")
        except DebtTermsError:
            return None
        if monthly is None:
            return None
        total += monthly
    return total


def debt_schedule_completion(body: dict | None) -> dict:
    """Minimum accepted debt information, without inventing unknown figures."""
    body = body or {}
    rows = active_debt_schedule_rows(body)
    no_debt = body.get("no_debt") is True
    missing = []
    if not rows:
        if not no_debt:
            missing.append("no_debt")
        reason = "No outstanding business debt declared." if no_debt else "Add the obligations or explicitly declare that the business has no outstanding debt."
    elif no_debt:
        missing.append("no_debt")
        reason = "Remove the no-debt declaration while active obligations are listed."
    else:
        for index, raw in enumerate(rows, 1):
            row_key = str(raw.get("id") or f"r{index}")
            if not str(raw.get("lender") or "").strip() or str(raw.get("lender")).strip().casefold() == "unnamed lender":
                missing.append(f"{row_key}.lender")
            try:
                row = normalize_terms(raw)
            except DebtTermsError:
                row = raw
            for field in ("balance", "monthly_payment"):
                try:
                    value = Decimal(str(row.get(field)).replace(",", "").replace("$", "").strip())
                    known = value.is_finite() and value >= 0
                except (InvalidOperation, ValueError, TypeError):
                    known = False
                if not known:
                    missing.append(f"{row_key}.{field}")
        reason = "All active obligations have a lender, balance and monthly payment." if not missing else "Complete each active obligation's lender, current balance and monthly payment. Other debt terms may remain unknown."
    return {"complete": not missing, "missing_fields": missing, "reason": reason, "no_debt": no_debt, "active_rows": len(rows)}


def is_accepted_debt_schedule_version(version: Any) -> bool:
    return (getattr(version, "source", None) in {"form", "legacy_baseline", "original"}
            and getattr(version, "status", None) in {"published", "filed", "original"}
            and debt_schedule_completion(getattr(version, "body", None))["complete"])


async def accepted_debt_schedule_snapshot(db, profile_id: UUID) -> FinancialFormVersion | None:
    """The current complete saved schedule, never a draft or pending upload."""
    if not isinstance(getattr(db, "info", None), dict):
        return None
    versions = (await db.execute(select(FinancialFormVersion).join(
        FinancialFormFamily, FinancialFormFamily.current_version_id == FinancialFormVersion.id,
    ).where(FinancialFormFamily.profile_id == profile_id, FinancialFormFamily.kind == "debt_schedule")
        .order_by(FinancialFormVersion.created_at.desc()))).scalars().all()
    return next((version for version in versions if is_accepted_debt_schedule_version(version)), None)


async def accepted_debt_schedule_authority(db, profile_id: UUID) -> bool:
    return await accepted_debt_schedule_snapshot(db, profile_id) is not None


async def accepted_debt_schedule_snapshot_for_dealer(db, dealer_id: UUID) -> FinancialFormVersion | None:
    from app.models.application_profile import ApplicationProfile

    if not isinstance(getattr(db, "info", None), dict):
        return None
    profiles = list((await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.dealer_id == dealer_id))).scalars().all())
    return await accepted_debt_schedule_snapshot(db, profiles[0]) if len(profiles) == 1 else None


async def accepted_debt_schedule_authority_for_dealer(db, dealer_id: UUID) -> bool:
    return await accepted_debt_schedule_snapshot_for_dealer(db, dealer_id) is not None


async def financial_coverage_file_ids(db, file_ids) -> set:
    """Choose received coverage with complete saved debt schedules authoritative.

    Read bodies rather than rewriting immutable older versions which predate
    automatic debt filing. A pending update cannot turn an accepted complete
    debt schedule back into another upload request.
    """
    ids = set(file_ids)
    if not ids:
        return set()
    version, family = FinancialFormVersion, FinancialFormFamily
    attached = or_(version.source_file_id.in_(ids), version.pdf_file_id.in_(ids))
    families = select(version.family_id).where(attached)
    rows = (await db.execute(select(version, family.current_version_id, family.kind)
                            .join(family, family.id == version.family_id)
                            .where(family.id.in_(families)))).all()
    managed = set()
    grouped = {}
    for item, current_id, kind in rows:
        managed.update(value for value in (item.source_file_id, item.pdf_file_id) if value in ids)
        grouped.setdefault(item.family_id, []).append((item, current_id, kind))
    selected = ids - managed
    for history in grouped.values():
        current = next((item for item, pointer, _ in history if item.id == pointer), None)
        kind = history[0][2]
        if current is not None and kind == "debt_schedule" and is_accepted_debt_schedule_version(current):
            selected.add(current.pdf_file_id or current.source_file_id)
            continue
        ordered = sorted((item for item, _, _ in history), key=lambda item: item.version_number, reverse=True)
        latest = next((item for item in ordered if item.status in {"original", "filed"}), None)
        if latest is not None and latest.status == "original" and latest.source_file_id and latest is not current:
            selected.add(latest.source_file_id)
        elif current is not None and current.status in {"filed", "original"}:
            selected.add(current.pdf_file_id or current.source_file_id)
    return selected & ids


def current_financial_file_clause(file_id):
    version = FinancialFormVersion
    attached = or_(version.source_file_id == file_id, version.pdf_file_id == file_id)
    managed = exists(select(version.id).where(attached))
    current = exists(select(version.id).join(FinancialFormFamily, FinancialFormFamily.id == version.family_id).where(
        version.id == FinancialFormFamily.current_version_id,
        or_(version.pdf_file_id == file_id, and_(version.pdf_file_id.is_(None), version.source_file_id == file_id)),
    ))
    return or_(~managed, current)


def _latest_pending_original(version, family):
    later = aliased(FinancialFormVersion)
    return and_(
        version.status == "original",
        version.source_file_id.is_not(None),
        or_(family.current_version_id.is_(None), version.id != family.current_version_id),
        ~exists(select(later.id).where(later.family_id == version.family_id,
                                      later.version_number > version.version_number,
                                      later.status.in_(["original", "filed"]))),
    )


def financial_coverage_file_clause(file_id):
    """One received representative per family, including an unreviewed update.

    A new pending original replaces that family's old PDF in collection
    coverage only. Numeric consumers continue using current_financial_file_clause.
    """
    version, family = FinancialFormVersion, FinancialFormFamily
    pending = aliased(FinancialFormVersion)
    managed = exists(select(version.id).where(or_(version.source_file_id == file_id,
                                                version.pdf_file_id == file_id)))
    has_pending = exists(select(pending.id).where(pending.family_id == family.id,
                                                 _latest_pending_original(pending, family)))
    selected = exists(select(version.id).join(family, family.id == version.family_id).where(or_(
        and_(_latest_pending_original(version, family), version.source_file_id == file_id),
        and_(version.id == family.current_version_id, version.status.in_(["filed", "original"]), ~has_pending,
             or_(version.pdf_file_id == file_id,
                 and_(version.pdf_file_id.is_(None), version.source_file_id == file_id))),
    )))
    return or_(~managed, selected)


async def pending_financial_original_ids(db, file_ids) -> set:
    ids = set(file_ids)
    if not ids or not isinstance(getattr(db, "info", None), dict):
        return set()
    version, family = FinancialFormVersion, FinancialFormFamily
    rows = await db.execute(select(version.source_file_id).join(family, family.id == version.family_id).where(
        version.source_file_id.in_(ids), _latest_pending_original(version, family),
    ))
    return set(rows.scalars().all())


@dataclass(frozen=True)
class PendingFinancialAnalysis:
    """A received/checking projection; the durable source analysis stays intact."""

    source: Any

    @property
    def financial_mapping_pending(self):
        return True

    @property
    def status(self):
        return "pending"

    @property
    def analysis(self):
        return {"key_facts": {}, "financial_mapping_pending": True}

    @property
    def analyzed_at(self):
        return None

    @property
    def summary(self):
        return "Received; the financial form is waiting for staff review and filing."

    @property
    def document_type_candidates(self):
        return [{"classification": self.classification, "confidence": self.confidence}]

    @property
    def evidence_claims(self):
        return []

    def __getattr__(self, name):
        return getattr(self.source, name)
