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
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.financial_statement import FinancialStatement, FinancialStatementOwner
from app.services import debt_types, pfs_schema
from app.services.debt_terms import DebtTermsError, monthly_source, normalize_terms
from app.services.extracted_facts import canonical_field_aliases
from app.services.loan_progress import originated_on, paid_down_pct

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
    from app.services.financial_workspace_legacy import protect_projection
    await protect_projection(db, profile, "pfs")
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
    token: str | None = None,
) -> tuple[Any, str]:
    """A new link, and the only time its token exists in readable form.

    Returns `(link, token)`. Only the hash is stored, so this token cannot be
    recovered later — a lost link is reminted, not looked up.

    `token` lets a caller supply the token instead of drawing one — the forms
    packet derives four child tokens from one base so a single link opens all
    four forms. Storage is unchanged: the hash, never the token.
    """
    import secrets
    from datetime import timedelta

    from app.models.financial_form_link import FinancialFormLink

    token = token or secrets.token_urlsafe(_TOKEN_BYTES)
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
    "mca_modified",
    "mca_modification_note",
    "notes",
    "factor_rate",
    "payment_amount",
    "payment_frequency",
)

#: The same columns, worded for a header row — the downloadable workbook and
#: anything else that prints the schedule read these beside `DEBT_COLUMNS`,
#: one to one, so a column added above is added here or the zip fails loudly.
DEBT_COLUMN_LABELS = (
    "Lender",
    "Type of debt",
    "Original amount",
    "Current balance",
    "Interest rate (%)",
    "Monthly payment",
    "Origination date",
    "Maturity date",
    "Secured or unsecured",
    "Payment status",
    "Collateral",
    "MCA terms or payments modified?",
    "How the advance was modified",
    "Notes",
    "Factor rate (×)",
    "Payment amount",
    "Payment frequency",
)
assert len(DEBT_COLUMN_LABELS) == len(DEBT_COLUMNS)

#: The type-of-debt vocabulary lives in `debt_types`; re-exported here because
#: everything that prints or stores the schedule already imports this module.
DEBT_TYPES = debt_types.DEBT_TYPES
DEBT_TYPE_SLUGS = debt_types.DEBT_TYPE_SLUGS
ADVANCE_DEBT_TYPES = debt_types.ADVANCE_DEBT_TYPES
normalize_debt_type = debt_types.normalize_debt_type
debt_type_label = debt_types.debt_type_label

#: What a schedule is incomplete without. The owner: "not all the fields are
#: mandatory, but lender name, type of debt, original amount, balance, interest
#: rate, monthly payment, current or delinquent we need it." The dates, whether
#: it is secured, the collateral and the note may stay blank. An advance also
#: has to say whether the funder modified its terms or payments.
REQUIRED_DEBT_FIELDS: tuple[str, ...] = (
    "lender",
    "debt_type",
    "original_amount",
    "balance",
    "rate",
    "monthly_payment",
    "payment_status",
)
ADVANCE_REQUIRED_DEBT_FIELDS: tuple[str, ...] = ("mca_modified",)

#: How a missing answer is named back to the person, in the words the form
#: uses for the field.
_DEBT_FIELD_WORDS: dict[str, str] = {
    "lender": "lender name",
    "debt_type": "type of debt",
    "original_amount": "original amount",
    "balance": "current balance",
    "rate": "interest rate",
    "monthly_payment": "monthly payment",
    "payment_status": "current or delinquent",
    "mca_modified": "whether the advance terms or payments were modified",
}

#: What the row's two choice fields accept. Anything else is discarded rather
#: than stored, so a stray value cannot end up rendered on a schedule we send
#: to a lender as though the borrower had said it.
_SECURED_CHOICES = {"secured", "unsecured"}
_PAYMENT_STATUS_CHOICES = {"current", "delinquent"}


def _choice(value: Any, allowed: set[str]) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in allowed else None


_YES = frozenset({"yes", "y", "true", "1"})
_NO = frozenset({"no", "n", "false", "0"})


def _yes_no(value: Any) -> bool | None:
    """A yes-or-no answer, or None for "not asked" / "not answered"."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().casefold()
    if text in _YES:
        return True
    if text in _NO:
        return False
    return None


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


#: What `dos_debts.rate` can hold — Numeric(6, 3). A figure past it is an
#: amount typed into the wrong box, and reads as "no rate" rather than a 500
#: at flush.
_RATE_CEILING = 999.999


def _rate(value: Any) -> float | None:
    """An interest rate as a number. "7.25%", "7.25" and 7.25 mean the same
    thing; a typed 0 is a 0% promotion, not a blank."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
    else:
        text = str(value).strip().rstrip("%").replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if not 0 <= number <= _RATE_CEILING:
        return None
    return number


_MONEY_NOISE = re.compile(r"[,$\s]")


def _money_or_none(value: Any) -> Decimal | None:
    """A typed figure, or None when the field is blank or not a number.

    Not nought. "TBD" or "n/a" in a monthly payment used to parse as 0, and 0
    in `monthly_payment` tells the DSCR engine the obligation costs nothing a
    month. An unknown figure stays unknown, and the completeness check names
    it. Parentheses are the accountant's minus; a typed 0 is kept.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = _MONEY_NOISE.sub("", str(value))
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text.startswith("-"):
        negative, text = True, text[1:]
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def _blank_or_amount(value: Any) -> Decimal | None:
    """Compatibility for draft adapters; an unreported amount stays unknown."""
    return _money_or_none(value)


def _first_present(line: dict[str, Any], *keys: str) -> Any:
    """The first of these keys the line actually carries. Not `or`: a rate of
    0 is a 0% promotion, and `or` would step over it to the next key."""
    for key in keys:
        value = line.get(key)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return None


_DELINQUENT_WORDS = re.compile(
    r"\b(delinquent|past[ _]?due|not current|late|default(ed)?|arrears)\b"
)


def _document_payment_status(line: dict[str, Any]) -> str | None:
    """"current" or "delinquent" off a schedule line, whichever way the AI
    wrote it: the prompt asks for `payment_status`, older reads carry a
    `current` true/false. Anything else stays unanswered, so the completeness
    check asks for it instead of the row guessing."""
    text = str(line.get("payment_status") or "").strip().casefold().replace("-", " ")
    if text in _PAYMENT_STATUS_CHOICES:
        return text
    if text and _DELINQUENT_WORDS.search(text):
        return "delinquent"
    current = line.get("current")
    if isinstance(current, bool):
        return "current" if current else "delinquent"
    return None


def _document_secured(line: dict[str, Any]) -> str | None:
    """"secured" or "unsecured" off a schedule line. A line that names what
    backs it is secured, whatever else it said."""
    raw = line.get("secured")
    if isinstance(raw, bool):
        return "secured" if raw else "unsecured"
    text = str(raw or "").strip().casefold()
    if text in _SECURED_CHOICES:
        return text
    if text in _YES:
        return "secured"
    if text in _NO:
        return "unsecured"
    if str(_first_present(line, "secured_by", "collateral") or "").strip():
        return "secured"
    return None
ROW_PATCH_FIELDS = (
    "lender",
    "category",
    "monthly_payment",
    "balance",
    "payoff_amount",
    "payment_amount",
    "payment_frequency",
    # The three answers a lender asks of every line, and whether it is being
    # paid on time. They were missing here, so a row could be created with
    # them (the form) but never corrected on the cockpit or by the review.
    "original_amount",
    "rate",
    "factor_rate",
    "payment_status",
    # The rest of the twelve-field row, so one writer can correct any cell of it.
    "originated_on",
    "maturity_on",
    "secured",
    "collateral",
    "notes",
    "mca_modified",
    "mca_modification_note",
    "count_in_dscr",
    "vendor_key",
    "status",
)


def apply_debt_row_patch(row: Any, changes: dict[str, Any]) -> list[str]:
    """Write the sent fields onto a schedule row; returns the names changed.

    A category is normalised to the closed debt-type vocabulary (an unknown
    word is kept as typed, lower-cased). A monthly figure typed by the desk
    replaces a per-payment cadence; an empty vendor key unlinks the payee.
    """
    changed: list[str] = []
    for key in ROW_PATCH_FIELDS:
        if key not in changes:
            continue
        value = changes[key]
        if key == "category" and value is not None:
            value = debt_types.normalize_debt_type(value) or str(value).strip().lower()[:24]
        if key == "lender" and value is not None:
            value = str(value).strip()[:180] or getattr(row, "lender", None)
        if key == "vendor_key":
            value = (str(value).strip()[:60] or None) if value is not None else None
        if key in {"monthly_payment", "balance", "payoff_amount", "payment_amount", "original_amount"} and value is not None:
            value = round(float(value), 2)
        if key in {"rate", "factor_rate"} and value is not None:
            # `rate` is a percent a year; a factor ("1.35") belongs in `factor_rate`
            # and nowhere else, which is why the two are separate keys.
            value = round(float(value), 3)
        if key == "payment_status" and value is not None:
            value = _choice(value, _PAYMENT_STATUS_CHOICES)
            if value is None:
                continue  # an unreadable answer leaves the stored one alone
        if key in {"originated_on", "maturity_on"} and value is not None:
            value = value if isinstance(value, date) else _date(value)
        if key == "secured" and value is not None:
            value = _choice(value, _SECURED_CHOICES)
            if value is None:
                continue
        if key in {"collateral", "notes", "mca_modification_note"} and value is not None:
            value = " ".join(str(value).split()) if key != "notes" else str(value).strip()
            value = value[: {"collateral": 500, "notes": 2000, "mca_modification_note": 1000}[key]] or None
        if key == "mca_modified" and value is not None and not isinstance(value, bool):
            value = _yes_no(value)
        if getattr(row, key, None) != value:
            setattr(row, key, value)
            changed.append(key)
    if "category" in changes and getattr(row, "category", None) not in debt_types.ADVANCE_DEBT_TYPES:
        # An answer to a question only advances are asked leaves with the type.
        for field in ("mca_modified", "mca_modification_note"):
            if getattr(row, field, None) is not None:
                setattr(row, field, None)
                changed.append(field)
    if "monthly_payment" in changes and changes["monthly_payment"] is not None and "payment_amount" not in changes:
        # The desk typed the month; a per-payment cadence read earlier no longer describes it.
        if getattr(row, "payment_amount", None) is not None:
            row.payment_amount = None
            row.payment_frequency = None
            changed.append("payment_amount")
    return changed


def document_debts_as_body(document: dict[str, Any] | None) -> dict[str, Any]:
    """The lines the AI read off an uploaded debt schedule, as the desk's form
    body, so they can be adopted as the file's own rows with one click.

    Both shapes the AI has written are read (`interest_rate` / `rate_pct`,
    `payment_status` / `current`, `secured` / `secured_by`), and what comes out
    uses only the values the form stores, so the row cleaner downstream never
    has to discard an answer it cannot read. The MCA modification answer rides
    along; the cleaner keeps it only on an advance."""
    debts: list[dict[str, Any]] = []
    for line in (document or {}).get("debts") or []:
        if not isinstance(line, dict):
            continue
        lender = str(line.get("lender") or line.get("creditor") or line.get("name") or "").strip()
        if not lender:
            continue
        debts.append(
            {
                "lender": lender,
                "debt_type": _first_present(line, "debt_type", "type"),
                "original_amount": line.get("original_amount"),
                "balance": _first_present(line, "current_balance", "balance"),
                "rate": _first_present(line, "interest_rate", "rate_pct", "rate"),
                "monthly_payment": line.get("monthly_payment"),
                "factor_rate": line.get("factor_rate"),
                "payment_amount": line.get("payment_amount"),
                "payment_frequency": line.get("payment_frequency"),
                "originated_on": originated_on(line),
                "maturity_on": _first_present(line, "maturity_date", "maturity_on"),
                "collateral": _first_present(line, "collateral", "secured_by"),
                "secured": _document_secured(line),
                "payment_status": _document_payment_status(line),
                "mca_modified": line.get("mca_modified"),
                "mca_modification_note": _first_present(line, "mca_modification_note", "mca_note"),
                "notes": "Adopted from the uploaded debt schedule.",
            }
        )
    return {"debts": debts}


def debt_rows_from_body(body: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The rows out of a submitted debt-schedule form, cleaned.

    A row with no lender and no figures is someone tabbing through an empty
    line, not an obligation; it is dropped rather than stored as a blank.

    Returns None when the body carries no `debts` key at all, or a null one. That is a client
    that sent nothing — an autosave that fired before the form finished
    loading, say — and it must not be read as "this borrower owes nobody".
    An explicit empty list is an answer and comes back as `[]`.
    """
    if (body or {}).get("debts") is None:
        return None
    out: list[dict[str, Any]] = []
    for raw in (body or {}).get("debts") or []:
        if not isinstance(raw, dict):
            continue
        # A line with nothing on it is someone tabbing through, not an
        # obligation. A line with anything on it — a type, an amount, a note —
        # is kept, so what was typed is never silently dropped and the
        # completeness check can say what it still lacks.
        if not any(
            str(raw.get(key) if raw.get(key) is not None else "").strip() for key in DEBT_COLUMNS
        ):
            continue
        # The legacy form tolerates unreadable numeric text as unknown. The
        # workspace validates its editable cells before reaching this cleaner.
        prepared = {**raw, "monthly_payment": _money_or_none(raw.get("monthly_payment")), "rate": _rate(raw.get("rate"))}
        try:
            normalized = normalize_terms(prepared)
        except DebtTermsError as error:
            raise HTTPException(422, str(error)) from error
        lender = str(raw.get("lender") or "").strip()
        # A figure nobody typed is unknown, not nought. Writing 0 into
        # `monthly_payment` tells the DSCR engine this obligation costs nothing
        # a month, which is how a daily-paying advance disappears out of the
        # denominator and makes coverage look better than it is.
        balance = _money_or_none(raw.get("balance"))
        monthly = normalized["monthly_payment"]
        # A slug the form chose, or the slug a typed value means ("LOC",
        # "Term", a label picked in a spreadsheet). Unrecognised text is kept.
        debt_type = debt_types.normalize_debt_type(raw.get("debt_type"))
        advance = debt_type in debt_types.ADVANCE_DEBT_TYPES
        out.append(
            {
                # Which stored row this line came from, when the form was
                # seeded from one. This is what lets a save update rows in
                # place instead of deleting and recreating them.
                "id": str(raw.get("id") or "").strip() or None,
                "lender": (lender or "Unnamed lender")[:180],
                "debt_type": debt_type,
                "original_amount": _money_or_none(raw.get("original_amount")),
                "balance": balance,
                "rate": _rate(normalized.get("rate")),
                "monthly_payment": monthly,
                "monthly_payment_source": normalized.get("monthly_payment_source"),
                **{key: normalized[key] for key in ("factor_rate", "payment_amount", "payment_frequency") if key in normalized},
                # Which stored row this line came from, when the form was seeded
                # from one. Identity is what lets a save update a row in place
                # instead of deleting it and inserting a copy — the copy was the
                # duplicate that doubled the schedule.
                "originated_on": _date(raw.get("originated_on")),
                "maturity_on": _date(raw.get("maturity_on")),
                "secured": _choice(raw.get("secured"), _SECURED_CHOICES),
                "payment_status": _choice(raw.get("payment_status"), _PAYMENT_STATUS_CHOICES),
                "collateral": (str(raw.get("collateral") or "").strip() or None),
                # Asked of an advance and nothing else. An answer left on a row
                # whose type was then changed is an answer to a question nobody
                # asked, so it goes with the type.
                "mca_modified": _yes_no(raw.get("mca_modified")) if advance else None,
                "mca_modification_note": (
                    (str(raw.get("mca_modification_note") or "").strip() or None) if advance else None
                ),
                "notes": str(raw.get("notes") or "").strip() or None,
                # Whether the saver may write this row. The request is not
                # believed about this — a body that marked its own rows
                # read-only would skip the completeness check — so every row
                # starts writable and `debt_submit_problems` turns off the ones
                # the file says belong to another source. Never stored.
                "editable": True,
            }
        )
    return out


def mark_foreign_rows(rows: list[dict[str, Any]], existing: list[Any], *, origin: str) -> None:
    """Set `editable` from the file: a row whose id resolves to a stored row
    another origin owns is one this saver cannot write, and the completeness
    check must not hold the saver to it. Decided here from the stored rows,
    never from the request."""
    foreign = {str(row.id) for row in existing if row.origin != origin}
    for row in rows:
        row["editable"] = (row.get("id") or "") not in foreign


def debt_row_problems(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Which required answers each row still lacks, for a submit.

    A draft is never checked: the page autosaves half-typed rows on purpose,
    and refusing them would lose what someone had typed so far. A submit is
    the borrower's finished answer, and a lender reading a schedule without a
    type, a rate or whether it is current has to come back and ask — which is
    what the owner is paying this form to stop.

    A row the saver cannot write (`editable` False on the seeded body) is
    skipped: a borrower is not kept from submitting by a desk row they have no
    way to complete. Rows are numbered from one, the way the form shows them.
    """
    problems: list[dict[str, Any]] = []
    for index, row in enumerate(rows or [], start=1):
        if row.get("editable") is False:
            continue
        missing = [
            field
            for field in REQUIRED_DEBT_FIELDS
            if row.get(field) in (None, "") or (field == "lender" and row.get(field) == "Unnamed lender")
        ]
        if row.get("debt_type") in debt_types.ADVANCE_DEBT_TYPES and row.get("factor_rate") is not None:
            # A contract's factor is its pricing answer, never an invented APR.
            missing = [field for field in missing if field != "rate"]
        if row.get("debt_type") in debt_types.ADVANCE_DEBT_TYPES:
            missing.extend(field for field in ADVANCE_REQUIRED_DEBT_FIELDS if row.get(field) is None)
        if missing:
            lender = row.get("lender")
            problems.append(
                {
                    "index": index,
                    "lender": None if lender in (None, "", "Unnamed lender") else lender,
                    "missing": missing,
                }
            )
    return problems


async def debt_submit_problems(
    db: AsyncSession, profile: ApplicationProfile, rows: list[dict[str, Any]], *, origin: str
) -> list[dict[str, Any]]:
    """`debt_row_problems` for a submit on a file: the rows another source owns
    are read off the file's own schedule and skipped, everything else is held
    to the required answers."""
    from app.dealer_os.models import DealerDebt

    existing = list(
        (
            await db.execute(
                select(DealerDebt).where(
                    DealerDebt.profile_id == profile.id,
                    DealerDebt.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    mark_foreign_rows(rows, existing, origin=origin)
    return debt_row_problems(rows)


def describe_debt_problems(problems: list[dict[str, Any]]) -> str:
    """The problems as one sentence per obligation, for a 422 the page shows
    as written: "Obligation 2 (Newtek) still needs: interest rate, current or
    delinquent." """
    lines: list[str] = []
    for problem in problems:
        who = f" ({problem['lender']})" if problem.get("lender") else ""
        words = ", ".join(_DEBT_FIELD_WORDS.get(field, field.replace("_", " ")) for field in problem["missing"])
        lines.append(f"Obligation {problem['index']}{who} still needs: {words}.")
    return " ".join(lines)


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
                    float(row["original_amount"]) if row.get("original_amount") is not None else None
                ),
                "current_balance": float(row["balance"]) if row.get("balance") is not None else None,
                "paid_down_pct": paid_down_pct(row.get("original_amount"), row.get("balance")),
                "monthly_payment": float(row["monthly_payment"]) if row.get("monthly_payment") is not None else None,
                "rate": float(row["rate"]) if row.get("rate") is not None else None,
                "factor_rate": float(row["factor_rate"]) if row.get("factor_rate") is not None else None,
                "payment_amount": float(row["payment_amount"]) if row.get("payment_amount") is not None else None,
                "payment_frequency": row.get("payment_frequency"),
                "monthly_payment_source": row.get("monthly_payment_source"),
                "maturity_date": row["maturity_on"].isoformat() if row.get("maturity_on") else None,
                # The rest of the row, so a reader of the facts sees what the
                # PDF shows: what the debt is, its rate, whether it is current,
                # and — for an advance — whether the funder modified it. `type`
                # is the key the cockpit's document reader already looks at.
                "debt_type": row.get("debt_type"),
                "type": row.get("debt_type"),
                "debt_type_label": debt_types.debt_type_label(row.get("debt_type")),
                "interest_rate": float(row["rate"]) if row.get("rate") is not None else None,
                "originated_date": row["originated_on"].isoformat() if row.get("originated_on") else None,
                "originated_on": originated_on(row),
                "secured": row.get("secured"),
                "payment_status": row.get("payment_status"),
                "collateral": row.get("collateral"),
                "mca_modified": row.get("mca_modified"),
                "mca_modification_note": row.get("mca_modification_note"),
            }
            for row in rows
        ],
        "total_monthly_debt_service": float(sum((row["monthly_payment"] or 0) for row in rows)) if all(row.get("monthly_payment") is not None for row in rows) else None,
        "total_outstanding_balance": float(sum((row["balance"] or 0) for row in rows)) if all(row.get("balance") is not None for row in rows) else None,
    }


#: Everything the schedule form is allowed to write. Every other column on
#: DealerDebt belongs to somebody else — `count_in_dscr` is toggled from the
#: DSCR composer, `term_months` / `payoff_amount` come from the refinance workbench, and
#: `vendor_key` / `document_id` record where a row was read from. Existing
#: `evidence` is preserved while its monthly-payment source marker is updated.
#: `dealer_id` and `origin` are identity, not answers. A save must leave all of
#: them exactly as it found them.
def _apply_debt_form_fields(target: Any, row: dict[str, Any]) -> None:
    try:
        row.update(normalize_terms(row, previous=target if getattr(target, "lender", None) is not None else None))
    except DebtTermsError as error:
        raise HTTPException(422, str(error)) from error
    target.lender = row["lender"]
    # `category` is the column that already existed for this; the form calls it
    # "type of debt" because that is what a schedule calls it. Falls back to the
    # old default so nothing downstream meets a null it never had to handle.
    target.category = (row.get("debt_type") or "loan")[:24]
    target.original_amount = row.get("original_amount")
    target.balance = row.get("balance")
    target.rate = row.get("rate")
    target.monthly_payment = row.get("monthly_payment")
    for key in ("factor_rate", "payment_amount", "payment_frequency"):
        if key in row:
            setattr(target, key, row[key])
    evidence = getattr(target, "evidence", None)
    target.evidence = {**(evidence if isinstance(evidence, dict) else {}), "monthly_payment_source": row.get("monthly_payment_source")}
    target.originated_on = row.get("originated_on")
    target.maturity_on = row.get("maturity_on")
    target.secured = row.get("secured")
    target.payment_status = row.get("payment_status")
    target.collateral = row.get("collateral")
    target.mca_modified = row.get("mca_modified")
    target.mca_modification_note = row.get("mca_modification_note")
    target.notes = row.get("notes")


def _lender_key(value: Any) -> str:
    text = " ".join(str(value or "").split()).casefold()
    return "" if text in {"", "unnamed lender"} else text


def _same_money(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False


async def save_debt_rows(
    db: AsyncSession,
    profile: ApplicationProfile,
    rows: list[dict[str, Any]] | None,
    *,
    origin: str,
) -> None:
    """Put a form's answer on the file's debt schedule, touching only what the
    saver owns.

    **The rule.** A form is seeded with every active row on the file so the
    person sees the whole schedule. A save may only *write* rows its own
    `origin` owns. A row another origin owns is matched — so it is never
    inserted a second time — and otherwise left completely alone: not updated,
    not deleted. That is what stops the schedule doubling, and it keeps the
    precedence the rest of the system relies on (`dealer_os` never lets
    extraction overwrite an `admin` row; a borrower's link must not either).

    **Why this shape.** The old save deleted only its own origin's rows and
    then inserted everything the form sent back — including the other
    origins' rows it had been seeded with. Three borrower debts plus one desk
    save made six, and `count_in_dscr` defaults to true, so the phantom rows
    went into the debt-service denominator. A first repair widened the delete
    to the whole file; that let an empty autosave from a no-login link wipe
    every row on the file, and let a borrower delete desk rows. The duplicate
    was never the delete's fault. It was the re-insert. So rows are matched
    and updated in place, and the delete scope stays exactly where it was.

    Matching is by the `id` the form was given. An id that resolves to nothing
    carries no claim and falls through to content matching among unclaimed
    rows on lender, with the figures as a tie-break, so a page loaded before
    ids existed still updates rather than re-creates. `rows is None` means the
    client sent no `debts` key at all — nothing was submitted, and nothing
    happens.
    """
    if rows is None:
        return

    from app.services.financial_workspace_legacy import protect_projection
    await protect_projection(db, profile, "debt_schedule")

    from app.dealer_os.models import DealerDebt

    existing = list(
        (
            await db.execute(
                select(DealerDebt)
                .where(
                    DealerDebt.profile_id == profile.id,
                    DealerDebt.status == "active",
                )
                .order_by(DealerDebt.created_at.asc(), DealerDebt.id.asc())
            )
        )
        .scalars()
        .all()
    )
    by_id = {str(row.id): row for row in existing}
    claimed: set[str] = set()
    pairs: list[tuple[dict[str, Any], Any]] = []

    # Explicit identity first, so content matching can never steal a row out
    # from under a form that named it.
    for row in rows:
        target = by_id.get(row.get("id") or "")
        if target is not None and str(target.id) not in claimed:
            claimed.add(str(target.id))
            pairs.append((row, target))
        else:
            pairs.append((row, None))

    # Then the rest by content. Lender is the identity; the figures only break
    # a tie, because correcting a figure is the whole point of the form and
    # must not turn a row into a stranger.
    for index, (row, target) in enumerate(pairs):
        if target is not None:
            continue
        wanted = _lender_key(row["lender"])
        same_lender = [
            candidate
            for candidate in existing
            if str(candidate.id) not in claimed and _lender_key(candidate.lender) == wanted
        ]
        if not same_lender:
            continue

        def _figures_match(candidate: Any, line: dict[str, Any] = row) -> bool:
            return _same_money(candidate.balance, line.get("balance")) and _same_money(
                candidate.monthly_payment, line.get("monthly_payment")
            )

        # A row this origin owns is the same debt whatever the figures say —
        # correcting a figure is what the form is for. A row another origin
        # owns is only "the copy I was shown" when the figures still agree;
        # otherwise this is a new debt at the same lender and must be inserted,
        # not swallowed by a row the saver could not have changed anyway.
        owned = [c for c in same_lender if c.origin == origin]
        chosen = None
        if owned:
            exact = [c for c in owned if _figures_match(c)]
            chosen = (exact or owned)[0]
        else:
            foreign_exact = [c for c in same_lender if _figures_match(c)]
            chosen = foreign_exact[0] if foreign_exact else None
        if chosen is None:
            continue
        claimed.add(str(chosen.id))
        pairs[index] = (row, chosen)

    for row, target in pairs:
        if target is None:
            fresh = DealerDebt(
                profile_id=profile.id,
                dealer_id=profile.dealer_id,
                origin=origin,
                status="active",
            )
            _apply_debt_form_fields(fresh, row)
            db.add(fresh)
        elif target.origin == origin:
            _apply_debt_form_fields(target, row)
        # else: somebody else's row. Seen, matched, left alone.

    # A row the saver no longer lists is retired, not deleted: `dos_debts` is
    # one of the retention-guarded history tables (0239), whose BEFORE DELETE
    # trigger refuses a physical delete outside a governed purge — the delete
    # that used to sit here 500'd the borrower's link the moment they removed
    # an obligation. "dismissed" is the status the dealer desk already uses for
    # the same thing, and every schedule reader selects `status == "active"`.
    for row in existing:
        if str(row.id) not in claimed and row.origin == origin:
            row.status = "dismissed"
    await db.flush()


async def debt_body_for_profile(
    db: AsyncSession, profile: ApplicationProfile, *, origin: str | None = None
) -> dict[str, Any]:
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
                "id": str(row.id),
                # Whether the caller's save will write this row. A desk row is
                # shown to a borrower for context; their save leaves it alone,
                # and the form should say so rather than accept an edit it will
                # discard. None (no origin given) means the caller is not a form.
                "editable": origin is None or row.origin == origin,
                "owner": row.origin,
                "lender": row.lender,
                # "loan" is the column's historic default, not something anyone
                # typed, so it seeds as blank rather than as an answer. A value
                # typed before the list existed ("LOC", "Term") seeds as the
                # slug it means, so the form's list shows it selected.
                "debt_type": debt_types.normalize_debt_type(row.category) or "",
                # A stored 0 is an answer (a 0% promotion, a paid-down line)
                # and seeds as "0", not as a blank the check would then name.
                "original_amount": "" if row.original_amount is None else str(row.original_amount),
                "balance": "" if row.balance is None else str(row.balance),
                "rate": "" if row.rate is None else str(row.rate),
                "monthly_payment": "" if row.monthly_payment is None else str(row.monthly_payment),
                "factor_rate": str(row.factor_rate) if row.factor_rate is not None else "",
                "payment_amount": str(row.payment_amount) if row.payment_amount is not None else "",
                "payment_frequency": row.payment_frequency or "",
                "monthly_payment_source": monthly_source(row),
                "originated_on": row.originated_on.isoformat() if row.originated_on else "",
                "maturity_on": row.maturity_on.isoformat() if row.maturity_on else "",
                "secured": row.secured or "",
                "payment_status": row.payment_status or "",
                "collateral": row.collateral or "",
                "mca_modified": "" if row.mca_modified is None else ("yes" if row.mca_modified else "no"),
                "mca_modification_note": row.mca_modification_note or "",
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
                    ApplicationExtractedFact.field_key.in_(
                        canonical_field_aliases("legal_entity_name")
                    ),
                    ApplicationExtractedFact.status.in_(["accepted", "suggested"]),
                )
                .order_by(
                    (ApplicationExtractedFact.status == "accepted").desc(),
                    ApplicationExtractedFact.created_at.desc(),
                )
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
