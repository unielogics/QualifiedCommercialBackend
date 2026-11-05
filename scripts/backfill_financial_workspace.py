"""Inventory legacy financial forms; adopt safe baselines only with --apply.

Run from qcbackend: python -m scripts.backfill_financial_workspace --limit 100
No PDFs are rendered, documents changed, outreach sent, or AI providers called.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

KINDS = ("debt_schedule", "pfs", "p_and_l", "balance_sheet")


@dataclass(frozen=True)
class LegacyForm:
    id: str
    subject: str
    period: tuple[str, ...]
    has_values: bool
    file_id: str | None = None
    owner_ids: tuple[str, ...] = ()

    @property
    def identity(self) -> tuple:
        return self.subject.strip().casefold(), self.period, self.owner_ids


@dataclass(frozen=True)
class Original:
    id: str
    subject: str
    period: tuple[str, ...]


@dataclass
class Inventory:
    managed: set[str] = field(default_factory=set)
    structured: dict[str, list[LegacyForm]] = field(default_factory=dict)
    originals: dict[str, list[Original]] = field(default_factory=dict)
    debt_count: int = 0
    dealer_only_debt_count: int = 0


@dataclass
class KindPlan:
    kind: str
    status: str
    reason: str
    review_file_ids: list[str] = field(default_factory=list)


def plan_inventory(inventory: Inventory) -> list[KindPlan]:
    """Make the conservative adoption decision without touching a session."""
    plans = []
    for kind in KINDS:
        originals = inventory.originals.get(kind, [])
        forms = inventory.structured.get(kind, [])
        populated = [row for row in forms if row.has_values]
        if kind in inventory.managed:
            plans.append(KindPlan(kind, "already_managed", "Existing family retained; rerunning does not replace its draft or versions."))
        elif kind == "debt_schedule" and (inventory.debt_count or originals):
            plans.append(KindPlan(kind, "eligible", f"Adopt {inventory.debt_count} debt row(s) and existing analyzed originals; {inventory.dealer_only_debt_count} dealer-only row(s) will acquire the profile anchor."))
        elif not populated and not originals:
            plans.append(KindPlan(kind, "empty", "No structured financial values or analyzed originals."))
        elif not forms or not forms[0].has_values:
            plans.append(KindPlan(kind, "requires_review", "No populated latest structured form. Assign the original's owner/reporting period and map it in the workspace; no empty baseline is created.", [row.id for row in originals]))
        elif not forms[0].subject.strip() or not forms[0].period or not all(forms[0].period):
            plans.append(KindPlan(kind, "requires_review", "The latest structured form has an unassigned owner/business or reporting date/period.", [row.id for row in originals]))
        elif len({row.identity for row in populated}) > 1:
            plans.append(KindPlan(kind, "requires_review", "Distinct legacy owners or reporting periods require separate families; resolve_family cannot safely combine them.", [row.id for row in originals]))
        else:
            latest = forms[0]
            unmatched = [row.id for row in originals if row.id != latest.file_id and (row.subject.strip().casefold() != latest.subject.strip().casefold() or row.period != latest.period)]
            plans.append(KindPlan(kind, "eligible", "Adopt the current structured baseline and identity-compatible originals; unmatched originals remain available for staff review.", unmatched))
    return plans


def _has_values(body: dict, kind: str) -> bool:
    sections = (body.get(key) or {} for key in ("assets", "liabilities", "income", "contingent")) if kind == "pfs" else (body.get("sections") or {}).values()
    return any(value not in (None, "") for section in sections for value in section.values()) or bool(kind == "pfs" and any((body.get("schedules") or {}).values()))


def _form(row: Any, kind: str, owners: dict[str, tuple[str, ...]]) -> LegacyForm:
    body = row.body or {}
    if kind == "pfs":
        subject = (body.get("applicant") or {}).get("name") or ""
        period = (str(body.get("as_of") or row.statement_date or ""),)
    else:
        header = body.get("header") or {}
        subject = header.get("business_name") or ""
        period = tuple(str(header.get(key) or getattr(row, key, None) or "") for key in (("period_start", "period_end") if kind == "p_and_l" else ("as_of_date",)))
    return LegacyForm(str(row.id), str(subject), period, _has_values(body, kind), str(row.bucket_file_id) if row.bucket_file_id else None, owners.get(str(row.id), ()))


async def inspect_inventory(db, profile) -> Inventory:
    from sqlalchemy import and_, or_, select

    from app.dealer_os.models import DealerDebt
    from app.models.bucket import BucketFile, BucketFileAnalysis
    from app.models.business_financial_statement import BusinessFinancialStatement
    from app.models.financial_form_version import FinancialFormFamily
    from app.models.financial_statement import FinancialStatement, FinancialStatementOwner
    from app.services.financial_workspace import CLASSIFICATIONS, _source_identity

    inventory = Inventory()
    inventory.managed = set((await db.execute(select(FinancialFormFamily.kind).where(FinancialFormFamily.profile_id == profile.id))).scalars().all())
    pfs = list((await db.execute(select(FinancialStatement).where(FinancialStatement.profile_id == profile.id).order_by(FinancialStatement.created_at.desc()))).scalars().all())
    owners: dict[str, list[str]] = {}
    if pfs:
        rows = (await db.execute(select(FinancialStatementOwner).where(FinancialStatementOwner.statement_id.in_([row.id for row in pfs])))).scalars().all()
        for row in rows:
            owners.setdefault(str(row.statement_id), []).append(f"application:{row.application_owner_id}" if row.application_owner_id else f"dealer:{row.dealer_owner_id}")
    owner_ids = {key: tuple(sorted(values)) for key, values in owners.items()}
    inventory.structured["pfs"] = [_form(row, "pfs", owner_ids) for row in pfs]
    statements = list((await db.execute(select(BusinessFinancialStatement).where(BusinessFinancialStatement.profile_id == profile.id).order_by(BusinessFinancialStatement.created_at.desc()))).scalars().all())
    for kind in ("p_and_l", "balance_sheet"):
        inventory.structured[kind] = [_form(row, kind, {}) for row in statements if row.kind == kind]
    scope = DealerDebt.profile_id == profile.id
    if profile.dealer_id:
        scope = or_(scope, and_(DealerDebt.profile_id.is_(None), DealerDebt.dealer_id == profile.dealer_id))
    debts = list((await db.execute(select(DealerDebt).where(scope))).scalars().all())
    inventory.debt_count = len(debts)
    inventory.dealer_only_debt_count = sum(row.profile_id is None for row in debts)
    if profile.primary_bucket_id:
        rows = (await db.execute(select(BucketFile, BucketFileAnalysis).join(BucketFileAnalysis, BucketFileAnalysis.bucket_file_id == BucketFile.id).where(BucketFile.bucket_id == profile.primary_bucket_id, BucketFile.deleted_at.is_(None), BucketFile.status == "uploaded", BucketFileAnalysis.status == "completed", BucketFileAnalysis.classification.in_(CLASSIFICATIONS.values())).order_by(BucketFileAnalysis.created_at.desc()))).all()
        seen = set()
        kinds = {classification: kind for kind, classification in CLASSIFICATIONS.items()}
        for file, analysis in rows:
            if file.id in seen:
                continue
            seen.add(file.id)
            kind = kinds[analysis.classification]
            identity = _source_identity(kind, (analysis.analysis or {}).get("key_facts") or {})
            subject = str(identity.get("subject") or "")
            period = tuple(str(identity.get(key) or "") for key in (("as_of",) if kind == "pfs" else ("period_start", "period_end") if kind == "p_and_l" else ("as_of_date",)))
            inventory.originals.setdefault(kind, []).append(Original(str(file.id), subject, period))
    return inventory


async def process_profile(db, profile, *, apply: bool = False, resolver=None) -> dict:
    if apply:
        from sqlalchemy import select

        from app.models.application_profile import ApplicationProfile

        await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.id == profile.id).with_for_update())
    inventory = await inspect_inventory(db, profile)
    plans = plan_inventory(inventory)
    adopted = []
    if apply:
        # No other writer, renderer, extraction provider, or publication route is invoked.
        if resolver is None:
            from app.services.financial_workspace import resolve_family

            resolver = resolve_family

        for plan in plans:
            if plan.status == "eligible":
                family, _draft = await resolver(db, profile, kind=plan.kind)
                adopted.append({"kind": plan.kind, "family_id": str(family.id), "current_version_id": str(family.current_version_id) if family.current_version_id else None})
        if adopted:
            await db.commit()
    return {"profile_id": str(profile.id), "mode": "apply" if apply else "dry_run", "plans": [asdict(plan) for plan in plans], "adopted": adopted}


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.application_profile import ApplicationProfile

    async with SessionLocal() as db:
        query = select(ApplicationProfile.id).order_by(ApplicationProfile.id)
        if args.profile_id:
            query = query.where(ApplicationProfile.id == args.profile_id)
        if args.after_profile_id:
            query = query.where(ApplicationProfile.id > args.after_profile_id)
        ids = list((await db.execute(query.limit(args.limit))).scalars().all())
    failures = 0
    for profile_id in ids:
        async with SessionLocal() as db:
            try:
                profile = await db.get(ApplicationProfile, profile_id)
                if profile is not None:
                    print(json.dumps(await process_profile(db, profile, apply=args.apply), default=str))
            except Exception as error:  # A bad legacy file must not abort the remaining batch.
                await db.rollback()
                failures += 1
                print(json.dumps({"profile_id": str(profile_id), "status": "failed", "error": f"{type(error).__name__}: {error}"}))
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "profiles_scanned": len(ids), "failures": failures, "next_cursor": str(ids[-1]) if ids and not args.profile_id else None}))
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit eligible baseline adoption. Default: inventory only.")
    parser.add_argument("--profile-id", type=UUID, help="Inspect one application profile.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum profiles in this batch (default: 100).")
    parser.add_argument("--after-profile-id", "--cursor", dest="after_profile_id", type=UUID, help="Resume after the next_cursor UUID from a prior batch.")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.profile_id and args.after_profile_id:
        parser.error("Use --profile-id or --after-profile-id, not both")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
