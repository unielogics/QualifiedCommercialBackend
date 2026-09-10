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
from datetime import UTC, date, datetime
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


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------

#: Long enough that guessing is not a strategy. The URL is the whole credential
#: on these links, so this is the only thing standing in front of a balance
#: sheet.
_TOKEN_BYTES = 32

#: Links die on their own. An open link with no end date is a permanent
#: credential to someone's finances living in whatever inbox it was forwarded
#: to; 30 days is long enough for a borrower who means to get to it.
DEFAULT_LINK_TTL_DAYS = 30


def hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


async def mint_link(
    db: AsyncSession,
    profile: ApplicationProfile,
    *,
    kind: str,
    statement_id: UUID | None = None,
    label: str | None = None,
    invitee_email: str | None = None,
    created_by: UUID | None = None,
    ttl_days: int = DEFAULT_LINK_TTL_DAYS,
) -> tuple[Any, str]:
    """A new link, and the only time its token exists in readable form.

    Returns `(link, token)`. Only the hash is stored, so this token cannot be
    recovered later — a lost link is reminted, not looked up.
    """
    import secrets
    from datetime import timedelta

    from app.models.financial_form_link import FinancialFormLink

    token = secrets.token_urlsafe(_TOKEN_BYTES)
    link = FinancialFormLink(
        profile_id=profile.id,
        kind=kind,
        statement_id=statement_id,
        token_hash=hash_token(token),
        label=label,
        invitee_email=invitee_email,
        created_by=created_by,
        expires_at=datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None,
    )
    db.add(link)
    await db.flush()
    return link, token


async def link_for_token(db: AsyncSession, token: str):
    """The live link behind a token, or None.

    Expiry and revocation are checked here rather than by callers, so no route
    can forget one of them.
    """
    from app.models.financial_form_link import FinancialFormLink

    link = (
        await db.execute(
            select(FinancialFormLink).where(FinancialFormLink.token_hash == hash_token(token))
        )
    ).scalar_one_or_none()
    if link is None or not link.is_open:
        return None
    return link


# ---------------------------------------------------------------------------
# The business debt schedule
# ---------------------------------------------------------------------------

#: What the borrower is asked for per obligation. Deliberately short: a debt
#: schedule someone abandons halfway is worth less than a complete one with
#: four columns, and the desk can enrich a row afterwards.
DEBT_COLUMNS = (
    "lender",
    "debt_type",
    "original_amount",
    "balance",
    "rate",
    "monthly_payment",
    "originated_on",
    "maturity_on",
    "secured",
    "payment_status",
    "collateral",
    "notes",
)

#: What the row's two choice fields accept. Anything else is discarded rather
#: than stored, so a stray value cannot end up rendered on a schedule we send
#: to a lender as though the borrower had said it.
_SECURED_CHOICES = {"secured", "unsecured"}
_PAYMENT_STATUS_CHOICES = {"current", "delinquent"}


def _choice(value: Any, allowed: set[str]) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in allowed else None


def _date(value: Any) -> date | None:
    """An ISO date off a date input, or nothing.

    The browser sends yyyy-mm-dd and nothing else, but a hand-built request or
    a paste can send anything, and an unparseable date is not worth a 500.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _rate(value: Any) -> float | None:
    """An interest rate as a number. "7.25%" and "7.25" mean the same thing."""
    text = str(value or "").strip().rstrip("%").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def debt_rows_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows out of a submitted debt-schedule form, cleaned.

    A row with no lender and no figures is someone tabbing through an empty
    line, not an obligation; it is dropped rather than stored as a blank.
    """
    out: list[dict[str, Any]] = []
    for raw in (body or {}).get("debts") or []:
        if not isinstance(raw, dict):
            continue
        lender = str(raw.get("lender") or "").strip()
        balance = pfs_schema._amount(raw.get("balance"))
        monthly = pfs_schema._amount(raw.get("monthly_payment"))
        if not lender and not balance and not monthly:
            continue
        out.append(
            {
                "lender": (lender or "Unnamed lender")[:180],
                "debt_type": (str(raw.get("debt_type") or "").strip() or None),
                "original_amount": pfs_schema._amount(raw.get("original_amount")) or None,
                "balance": balance,
                "rate": _rate(raw.get("rate")),
                "monthly_payment": monthly,
                "originated_on": _date(raw.get("originated_on")),
                "maturity_on": _date(raw.get("maturity_on")),
                "secured": _choice(raw.get("secured"), _SECURED_CHOICES),
                "payment_status": _choice(raw.get("payment_status"), _PAYMENT_STATUS_CHOICES),
                "collateral": (str(raw.get("collateral") or "").strip() or None),
                "notes": str(raw.get("notes") or "").strip() or None,
            }
        )
    return out


def debt_key_facts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The same shape the old form emitted, so nothing downstream changes."""
    return {
        "debts": [
            {
                "lender": row["lender"],
                # These two were hardcoded null because the form had nowhere to
                # collect them. It does now, and the analyzer's own schedule
                # extraction reads both by name.
                "original_amount": (
                    float(row["original_amount"]) if row.get("original_amount") else None
                ),
                "current_balance": float(row["balance"]),
                "monthly_payment": float(row["monthly_payment"]),
                "maturity_date": row["maturity_on"].isoformat() if row.get("maturity_on") else None,
            }
            for row in rows
        ],
        "total_monthly_debt_service": float(sum(row["monthly_payment"] for row in rows)),
        "total_outstanding_balance": float(sum(row["balance"] for row in rows)),
    }


async def replace_debt_rows(
    db: AsyncSession, profile: ApplicationProfile, rows: list[dict[str, Any]], *, origin: str
) -> None:
    """Put the borrower's answer on the file's actual debt schedule.

    Replaces this source's previous rows rather than appending: the form is the
    whole answer, and submitting it twice should not leave the file claiming
    double the debt. Rows from other sources — an AI draft, a desk edit, a
    document — are untouched.
    """
    from app.dealer_os.models import DealerDebt

    existing = (
        (
            await db.execute(
                select(DealerDebt).where(
                    DealerDebt.profile_id == profile.id, DealerDebt.origin == origin
                )
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await db.delete(row)
    await db.flush()

    for row in rows:
        db.add(
            DealerDebt(
                profile_id=profile.id,
                dealer_id=profile.dealer_id,
                lender=row["lender"],
                # `category` is the column that already existed for this; the
                # form calls it "type of debt" because that is what a schedule
                # calls it. Falls back to the old default so nothing downstream
                # meets a null it never had to handle.
                category=(row.get("debt_type") or "loan")[:24],
                original_amount=row.get("original_amount"),
                balance=row["balance"],
                rate=row.get("rate"),
                monthly_payment=row["monthly_payment"],
                originated_on=row.get("originated_on"),
                maturity_on=row.get("maturity_on"),
                secured=row.get("secured"),
                payment_status=row.get("payment_status"),
                collateral=row.get("collateral"),
                notes=row.get("notes"),
                origin=origin,
                status="active",
            )
        )
    await db.flush()


async def debt_body_for_profile(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    """The file's current schedule, shaped for the form.

    Seeded from whatever is already on the file — an AI draft, rows the desk
    added, a previous submission — so a borrower confirms and corrects rather
    than retyping what we already know.
    """
    from app.dealer_os.models import DealerDebt

    rows = (
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.profile_id == profile.id, DealerDebt.status == "active")
                .order_by(DealerDebt.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "debts": [
            {
                "lender": row.lender,
                # "loan" is the column's historic default, not something anyone
                # typed, so it seeds as blank rather than as an answer.
                "debt_type": "" if (row.category or "loan") == "loan" else row.category,
                "original_amount": str(row.original_amount or ""),
                "balance": str(row.balance or ""),
                "rate": str(row.rate or ""),
                "monthly_payment": str(row.monthly_payment or ""),
                "originated_on": row.originated_on.isoformat() if row.originated_on else "",
                "maturity_on": row.maturity_on.isoformat() if row.maturity_on else "",
                "secured": row.secured or "",
                "payment_status": row.payment_status or "",
                "collateral": row.collateral or "",
                "notes": row.notes or "",
            }
            for row in rows
        ]
    }


# ---------------------------------------------------------------------------
# What the file already knows about who is filling the form in.
# ---------------------------------------------------------------------------


async def form_prefill(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    """Business and owner identity, for seeding a form before anyone types.

    A borrower opening a link we sent them should not be asked their own
    business name. We already hold it — from the dealer record, the intake they
    completed, or a document the analyzer read — and asking again reads as a
    system that is not paying attention, on the first field of a long form.

    Everything returned here is a suggestion. It seeds blanks only, is fully
    editable, and the borrower's typing always wins: the file's copy of a name
    can be stale, and the person on the form is the better authority on it.

    Precedence matches `production_prefill`: the dealer record, then the intake,
    then facts read off uploaded documents.
    """
    from app.dealer_os.models import DealerBusiness
    from app.models.application_profile import ApplicationExtractedFact
    from app.models.public_underwriting_intake import PublicUnderwritingIntake
    from app.services import application_profiles as profiles

    def text(value: Any) -> str:
        return str(value or "").strip()

    business = ""
    if profile.dealer_id:
        dealer = await db.get(DealerBusiness, profile.dealer_id)
        if dealer is not None:
            business = text(dealer.legal_name) or text(dealer.name)
    if not business and profile.intake_id:
        intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
        if intake is not None:
            business = text(intake.business_name)
    if not business:
        fact = (
            await db.execute(
                select(ApplicationExtractedFact)
                .where(
                    ApplicationExtractedFact.profile_id == profile.id,
                    ApplicationExtractedFact.field_key == "legal_entity_name",
                    ApplicationExtractedFact.status != "rejected",
                )
                .order_by(ApplicationExtractedFact.created_at.desc())
            )
        ).scalars().first()
        if fact is not None:
            raw = fact.normalized_value or (
                fact.value if isinstance(fact.value, str) else (fact.value or {}).get("value")
            )
            business = text(raw)

    owners = await profiles.owner_rows(db, profile)
    owner = next((row for row in owners if getattr(row, "is_primary", False)), None)
    owner = owner or (owners[0] if owners else None)

    name = ""
    address = ""
    phone = ""
    if owner is not None:
        name = " ".join(part for part in (text(owner.first_name), text(owner.last_name)) if part)
        # One line, the way an address goes on a form, and only the parts we
        # actually hold — "Miami, , 33101" is worse than nothing to type over.
        street = text(owner.street)
        locality = ", ".join(part for part in (text(owner.city), text(owner.state)) if part)
        locality = " ".join(part for part in (locality, text(owner.zip)) if part)
        address = ", ".join(part for part in (street, locality) if part)
        phone = text(owner.phone)

    return {
        "business_name": business or None,
        "owner_name": name or None,
        "home_address": address or None,
        "business_phone": phone or None,
        #: How many owners the file carries. A PFS belongs to one person, and a
        #: form seeded with the primary owner's name on a two-owner file should
        #: say so rather than let the second owner file under the first's name.
        "owner_count": len(owners),
    }


def seed_pfs_applicant(body: dict[str, Any], prefill: dict[str, Any]) -> dict[str, Any]:
    """Fill the 413's identity block from the file, without ever overwriting.

    Blanks only. A borrower who corrected their address on a draft must not find
    it reverted the next time they open the link.
    """
    applicant = dict(body.get("applicant") or {})
    for field, key in (
        ("name", "owner_name"),
        ("business_name", "business_name"),
        ("home_address", "home_address"),
        ("business_phone", "business_phone"),
    ):
        if not str(applicant.get(field) or "").strip() and prefill.get(key):
            applicant[field] = prefill[key]
    return {**body, "applicant": applicant}
