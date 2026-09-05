"""Saving a Personal Financial Statement, and finding it again.

The on-screen PFS used to render a PDF and drop the numbers. This module keeps
them, so a statement can be reopened, corrected, resumed, or finished by staff
on a borrower's behalf, and so it can be attached to the applicants it speaks
for rather than to a typed-in name.

`from_legacy_submission` exists so the older eight-asset form starts persisting
immediately, without waiting for the browser to move to the Form 413 layout.
Every surface that already submits a PFS — the public token room, the client
page, the broker and admin routers — begins saving rows the moment this ships,
and the richer form can land behind it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.financial_statement import FinancialStatement, FinancialStatementOwner
from app.services import pfs_schema

log = logging.getLogger(__name__)

#: The old fixed eight asset rows, in order, mapped onto their Form 413 line.
#: Real estate is the awkward one: the legacy form asked for *equity* (market
#: value less mortgages) on a single line, where 413 wants the asset and the
#: mortgage stated separately. Recording the equity figure as the asset with no
#: matching liability keeps net worth identical, which is the number anything
#: downstream actually reads.
_LEGACY_ASSET_KEYS = (
    "cash_on_hand",
    "savings_accounts",
    "stocks_and_bonds",
    "ira_or_retirement",
    "real_estate",
    "automobiles",
    "other_assets",       # business ownership / equity
    "other_personal_property",
)

#: The old six liability rows, in order. Several collapse onto one 413 line;
#: they are summed rather than overwriting each other.
_LEGACY_LIABILITY_KEYS = (
    "mortgages_on_real_estate",
    "installment_auto",
    "installment_other",   # credit cards
    "installment_other",   # personal loans
    "installment_other",   # student loans
    "other_liabilities",
)


def from_legacy_submission(
    *, assets: list[Any], liabilities: list[Any], owner_full_name: str, statement_date: str
) -> dict[str, Any]:
    """A legacy 8/6 submission as a Form 413 body.

    Positional, not by label: the labels are display strings that have already
    been reworded once, and matching on them is exactly the fragility this whole
    change is removing.
    """
    body = pfs_schema.empty_body()
    body["applicant"]["name"] = owner_full_name

    for index, row in enumerate(assets[: len(_LEGACY_ASSET_KEYS)]):
        key = _LEGACY_ASSET_KEYS[index]
        amount = getattr(row, "amount", None)
        body["assets"][key] = float(body["assets"].get(key) or 0) + float(amount or 0)

    for index, row in enumerate(liabilities[: len(_LEGACY_LIABILITY_KEYS)]):
        key = _LEGACY_LIABILITY_KEYS[index]
        amount = getattr(row, "amount", None)
        body["liabilities"][key] = float(body["liabilities"].get(key) or 0) + float(amount or 0)

    body["notes"] = f"Imported from the earlier eight-row form, dated {statement_date}."
    return body


def _parse_statement_date(value: str | None):
    """The legacy form took a free-text date. Store what parses, keep the rest
    in the body rather than guessing at a format."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


async def save_statement(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    body: dict[str, Any],
    statement_date: str | None = None,
    status: str = "draft",
    actor_user_id: UUID | None = None,
    statement: FinancialStatement | None = None,
) -> FinancialStatement:
    """Create or update a statement, recomputing the derived totals.

    Totals are written here rather than on read so the underwriting metric never
    depends on walking a JSON document, and so the two can never disagree.
    """
    totals = pfs_schema.totals(body)
    now = datetime.now(UTC)

    if statement is None:
        statement = FinancialStatement(
            profile_id=profile.id,
            created_by_user_id=actor_user_id,
        )
        db.add(statement)

    statement.body = body
    statement.schema_version = body.get("schema_version") or pfs_schema.SCHEMA_VERSION
    statement.statement_date = _parse_statement_date(statement_date) or statement.statement_date
    statement.total_assets = totals["total_assets"]
    statement.total_liabilities = totals["total_liabilities"]
    statement.net_worth = totals["net_worth"]
    statement.liquid_assets = totals["liquid_assets"]
    statement.status = status
    if status == "submitted" and statement.submitted_at is None:
        statement.submitted_at = now
        # Null when the borrower submitted it themselves through a link; set
        # when staff completed it for them, which the audit trail must show.
        statement.submitted_by_user_id = actor_user_id
    await db.flush()
    return statement


async def link_owners(
    db: AsyncSession,
    statement: FinancialStatement,
    *,
    application_owner_ids: list[UUID] | None = None,
    dealer_owner_ids: list[UUID] | None = None,
) -> None:
    """Say which applicants this statement speaks for.

    Replaces the whole set, so unlinking is the same call as linking. A joint
    statement — one sheet for a married couple — is simply two rows here.
    """
    existing = (
        (
            await db.execute(
                select(FinancialStatementOwner).where(
                    FinancialStatementOwner.statement_id == statement.id
                )
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await db.delete(row)
    await db.flush()

    for owner_id in application_owner_ids or []:
        db.add(
            FinancialStatementOwner(statement_id=statement.id, application_owner_id=owner_id)
        )
    for owner_id in dealer_owner_ids or []:
        db.add(FinancialStatementOwner(statement_id=statement.id, dealer_owner_id=owner_id))
    await db.flush()


async def latest_for_profile(
    db: AsyncSession, profile_id: UUID
) -> FinancialStatement | None:
    """The statement to show when a file has one. Newest wins."""
    return (
        await db.execute(
            select(FinancialStatement)
            .where(FinancialStatement.profile_id == profile_id)
            .order_by(FinancialStatement.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def for_profile(db: AsyncSession, profile_id: UUID) -> list[FinancialStatement]:
    return list(
        (
            await db.execute(
                select(FinancialStatement)
                .where(FinancialStatement.profile_id == profile_id)
                .order_by(FinancialStatement.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


def serialize(statement: FinancialStatement) -> dict[str, Any]:
    return {
        "id": statement.id,
        "profile_id": statement.profile_id,
        "statement_date": statement.statement_date,
        "schema_version": statement.schema_version,
        "status": statement.status,
        "body": statement.body,
        "total_assets": float(statement.total_assets or 0),
        "total_liabilities": float(statement.total_liabilities or 0),
        "net_worth": float(statement.net_worth or 0),
        "liquid_assets": float(statement.liquid_assets or 0),
        "submitted_at": statement.submitted_at,
        "filled_by_staff": statement.submitted_by_user_id is not None,
        "bucket_file_id": statement.bucket_file_id,
        "created_at": statement.created_at,
        "updated_at": statement.updated_at,
    }
