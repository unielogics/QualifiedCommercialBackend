"""Reviewing the lines of a debt schedule before they stand as the file's schedule.

A client can hand us a schedule they made themselves (an AI-built PDF, an
Excel). The analyzer reads its lines, and some of them are not loans at all:
an ACH debit somebody guessed was a funder, a payroll provider, a vendor. The
lines that are loans almost never say what a lender asks first — the interest
rate, the amount originally approved and what is still owed — and an advance
usually gives a payment per business day instead of a month.

The only tool the desk had was "adopt every line", which makes each of them an
obligation in the debt service. This module is the step before that: it lists
the lines (the file's reading and the rows already on the file) so a person can
take off what is not a debt, answer the three questions, and save the rest as
the schedule.

Rules this keeps, all of them already law elsewhere in the schedule code:

* **Removal is dismissal.** `dos_debts` is a retention-guarded table and every
  schedule reader selects `status == "active"`. A line taken off is kept as a
  dismissed row so it does not return the next time the file is read, and so
  the desk can put it back.
* **A figure nobody gave is unknown, never nought.** A blank stays None.
* **`rate` is a percent a year.** A factor rate (1.35) has its own column and
  its own question; typing one into the percent column prints "1.35%".
* **The payload is the answer.** A line the reading has since moved on from is
  saved as sent rather than refused.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerDebt
from app.models.application_profile import ApplicationProfile
from app.services import debt_types, drafted_forms
from app.services import financial_cockpit as cockpit
from app.services import financial_statements as fs
from app.services.payee_classification import KIND_LABELS, classify_payee, match_names

#: Why a line comes off, in the words the desk picks from.
REMOVE_REASONS: dict[str, str] = {
    "not_a_loan": "Not a loan or debt (an operating expense)",
    "duplicate": "The same debt as another line",
    "paid_off": "Already paid off or closed",
    "other_business": "Belongs to another business or person",
    "other": "Another reason",
}

#: What `classify_payee` calls things that are paid regularly but are not debt.
#: A tax payment or a rent payment is a debt only in the shapes the debt-type
#: list already names (an installment plan, a lease), so those two are not
#: flagged when the line itself says it is one.
NOT_DEBT_KINDS = frozenset(
    {
        "payroll",
        "rent",
        "tax",
        "insurance",
        "utilities",
        "merchant_services",
        "bank_fees",
        "owner_draw",
        "transfer",
    }
)
_KIND_EXEMPT_TYPES = {"tax": {"tax_payment_plan"}, "rent": {"lease", "real_estate"}}

ORIGIN_LABELS: dict[str, str] = {
    "admin": "Recorded by the desk",
    "client_form": "Typed by the client",
    "client": "Typed by the client",
    "borrower": "Typed by the client",
    "ai_draft": "Drafted by the AI",
    "document": "Read from a document",
}

SAVED_NOTE = "Reviewed from the uploaded debt schedule."
#: What the older adopt-every-line route wrote into notes; boilerplate, not a caveat.
_ADOPTED_NOTE = "Adopted from the uploaded debt schedule."

#: The cadences a row may store: the only ones every reader of
#: `payment_frequency` multiplies correctly.
CADENCES = ("daily", "weekly", "biweekly", "monthly")

_NOTE_LIMIT = 200
_NOTE_COUNT = 3
_RAW_VALUE_LIMIT = 300
_REMOVED_ROW_LIMIT = 30
_HISTORY_KEYS = ("removed", "proposed", "reviewed")


# ---------------------------------------------------------------------------
# Reading the file's lines
# ---------------------------------------------------------------------------


def cadence_of(value: Any) -> str | None:
    """How often a payment is made, as one of the four cadences a row stores.

    The analyzer writes this as it found it — "weekday", "bi-weekly",
    "daily ACH" — and also writes words that say nothing ("recurring",
    "variable", "irregular"), which are unknown rather than monthly.
    """
    text = " ".join(re.sub(r"[_\-/]+", " ", str(value or "").casefold()).split())
    if not text:
        return None
    if re.search(r"\bsemi ?monthly\b|\btwice (a|per|each) month\b|\b2x (a |per )?month\b", text):
        return None
    if re.search(r"\bbi ?weekly\b|\bevery (other|2|two) weeks?\b|\bfortnight(ly)?\b", text):
        return "biweekly"
    if re.search(r"\bdaily\b|\bweekdays?\b|\bbusiness days?\b|\bevery day\b|\beach day\b|\b(per|a) day\b", text):
        return "daily"
    if re.search(r"\bweekly\b|\bevery week\b|\beach week\b|\b(per|a) week\b", text):
        return "weekly"
    if re.search(r"\bmonthly\b|\bevery month\b|\beach month\b|\b(per|a) month\b", text):
        return "monthly"
    return None


def _number(value: Any) -> float | None:
    """A money figure off a line, or None when there is none. Never 0 for unknown."""
    amount = fs._money_or_none(value)
    return None if amount is None else round(float(amount), 2)


def _factor(value: Any) -> float | None:
    """A factor rate (1.35) when the line carries one that looks like it."""
    amount = fs._money_or_none(value)
    if amount is None:
        return None
    number = float(amount)
    return round(number, 3) if 1 <= number <= 5 else None


def _clean(value: Any, limit: int = _RAW_VALUE_LIMIT) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def _slug(lender: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", lender.casefold()).strip("-")[:60] or "line"


#: The notes the review shows beside the box they are about, not in the general list.
_BESIDE_THEIR_BOX = ("balance_note", "per_payment_note", "payment_note")


def _notes_from_file(raw: dict[str, Any]) -> list[str]:
    """What the document said about a line beside the figures: its own
    caveats ("Expired Oct 22"). The balance and payment notes are returned
    apart, so they can sit under the box they describe."""
    picked: list[str] = []
    for key in sorted(key for key in raw if isinstance(raw.get(key), str) and key not in _BESIDE_THEIR_BOX):
        if not (key.endswith("_note") or key in {"note", "notes", "comment", "comments"}):
            continue
        text = _clean(raw[key], _NOTE_LIMIT)
        if text and text != _ADOPTED_NOTE and text not in picked:
            picked.append(text)
    return picked[:_NOTE_COUNT]


def _raw_primitives(raw: dict[str, Any]) -> dict[str, Any]:
    """The line as the file gave it, trimmed to what is safe to keep on a row."""
    kept: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            kept[str(key)[:60]] = value
        elif isinstance(value, str):
            kept[str(key)[:60]] = _clean(value)
    return kept


def normalise_line(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One debt line off a document, in the vocabulary a schedule row stores.

    Both shapes the analyzer has written are read (`interest_rate` /
    `rate_pct`, `payment_status` / `current`, `per_payment_amount` /
    `payment_amount`). Returns None for a line that names no lender.
    """
    lender = _clean(fs._first_present(raw, "lender", "creditor", "funder", "name"), 180)
    if not lender:
        return None
    declared = fs._first_present(raw, "debt_type", "type", "kind", "category")
    debt_type = debt_types.normalize_debt_type(declared) or ""
    payment_amount = _number(
        fs._first_present(raw, "payment_amount", "per_payment_amount", "payment", "installment_amount", "installment")
    )
    cadence = cadence_of(fs._first_present(raw, "payment_frequency", "frequency", "cadence", "payment_cadence"))
    monthly = _number(raw.get("monthly_payment"))
    if monthly is None and payment_amount is not None and cadence is not None:
        monthly = cockpit.monthly_equivalent(payment_amount, cadence)
    original = _number(
        fs._first_present(raw, "original_amount", "funded_amount", "amount_approved", "approved_amount", "loan_amount", "original_balance")
    )
    balance = _number(fs._first_present(raw, "current_balance", "balance", "outstanding_balance"))
    rate = fs._rate(fs._first_present(raw, "interest_rate", "rate_pct", "rate", "apr"))
    originated = fs._date(fs._first_present(raw, "origination_date", "originated_on", "origin_date", "originated_date"))
    maturity = fs._date(fs._first_present(raw, "maturity_date", "maturity_on"))
    advance = debt_type in debt_types.ADVANCE_DEBT_TYPES
    return {
        "lender": lender,
        "debt_type": debt_type,
        "advance": advance,
        "original_amount": original,
        "balance": balance,
        "rate": rate,
        "factor_rate": _factor(fs._first_present(raw, "factor_rate", "factor")),
        # A payment per business day or week keeps its cadence; a monthly one is
        # simply the month; an amount whose cadence the file does not say keeps
        # the amount and leaves the question open.
        "payment_amount": None if cadence == "monthly" else payment_amount,
        "payment_frequency": cadence if cadence in {"daily", "weekly", "biweekly"} else None,
        "monthly_payment": monthly if monthly is not None else (payment_amount if cadence == "monthly" else None),
        "payment_status": fs._document_payment_status(raw),
        "balance_note": _clean(raw.get("balance_note"), _NOTE_LIMIT) or None,
        "payment_note": _clean(fs._first_present(raw, "per_payment_note", "payment_note"), _NOTE_LIMIT) or None,
        "notes_from_file": _notes_from_file(raw),
        # Kept for a row that is created from the line; never shown as a question.
        "extras": {
            "originated_on": originated,
            "maturity_on": maturity,
            "secured": fs._document_secured(raw),
            "collateral": _clean(fs._first_present(raw, "collateral", "secured_by")) or None,
            "mca_modified": fs._yes_no(raw.get("mca_modified")) if advance else None,
            "mca_modification_note": (_clean(raw.get("mca_modification_note") or raw.get("mca_note")) or None)
            if advance
            else None,
        },
        "raw": _raw_primitives(raw),
    }


def document_lines(documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every line the file's schedule documents carry, with the sources they came from.

    `documents` are newest first. A lender already taken from a newer document
    is not taken again from an older one (the newer schedule supersedes it); two
    lines from the same lender in one document are two debts and are both kept.
    Each line gets a key that is stable within the reading.
    """
    lines: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_keys: dict[str, int] = {}
    for document in documents:
        facts = document.get("key_facts") if isinstance(document.get("key_facts"), dict) else {}
        raw_lines = facts.get("debts") if isinstance(facts.get("debts"), list) else []
        file_id = str(document.get("file_id") or "")
        taken_before = [line["lender"] for line in lines if line["file_id"] != file_id]
        count = 0
        for raw in raw_lines:
            if not isinstance(raw, dict):
                continue
            line = normalise_line(raw)
            if line is None:
                continue
            if any(match_names(line["lender"], earlier) for earlier in taken_before):
                continue
            base = _slug(line["lender"])
            seen_keys[base] = seen_keys.get(base, 0) + 1
            line["key"] = base if seen_keys[base] == 1 else f"{base}-{seen_keys[base]}"
            line["file_id"] = file_id or None
            line["file_name"] = document.get("file_name")
            lines.append(line)
            count += 1
        sources.append(
            {
                "file_id": file_id,
                "file_name": str(document.get("file_name") or ""),
                "uploaded_by": document.get("uploaded_by"),
                "uploaded_at": document.get("uploaded_at"),
                "entity": _clean(facts.get("entity"), 160) or None,
                "schedule_date": _clean(facts.get("schedule_date"), 40) or None,
                "status_note": _clean(facts.get("status"), 200) or None,
                "scope_note": _clean(facts.get("scope_note"), 300) or None,
                "source_basis": _clean(facts.get("source_basis"), 300) or None,
                "line_count": count,
            }
        )
    return lines, sources


# ---------------------------------------------------------------------------
# Putting the file's lines beside the rows already on the file
# ---------------------------------------------------------------------------


def _evidence(row: Any) -> dict[str, Any]:
    value = getattr(row, "evidence", None)
    return value if isinstance(value, dict) else {}


def _hint(line: dict[str, Any]) -> dict[str, str] | None:
    """Say so when a line's name looks like something that is not a loan.

    Advice only. The name is all the classifier has, and a person decides."""
    kind, reason = classify_payee(
        line["lender"],
        cadence=line.get("payment_frequency"),
        reader_kind=line.get("debt_type") or None,
    )
    if kind not in NOT_DEBT_KINDS:
        return None
    if line.get("debt_type") in _KIND_EXEMPT_TYPES.get(kind, set()):
        return None
    return {"kind": kind, "label": KIND_LABELS.get(kind, kind), "reason": reason}


def _missing(line: dict[str, Any]) -> list[str]:
    """Which answers a line still lacks, in the order the review asks them."""
    missing: list[str] = []
    if not line.get("debt_type"):
        missing.append("debt_type")
    if line.get("advance"):
        if line.get("factor_rate") is None and line.get("rate") is None:
            missing.append("factor_rate")
    elif line.get("rate") is None:
        missing.append("rate")
    if line.get("original_amount") is None:
        missing.append("original_amount")
    if line.get("balance") is None:
        missing.append("balance")
    if line.get("monthly_payment") is None:
        missing.append("payment")
    if not line.get("payment_status"):
        missing.append("payment_status")
    return missing


#: The three the desk was asked to get: what it costs, what it was and what is left.
_THE_THREE = ("rate", "factor_rate", "original_amount", "balance")
_PAYMENT_FIGURES = ("payment_amount", "payment_frequency", "monthly_payment")


def _row_figures(row: Any) -> dict[str, Any]:
    """A schedule row's figures as plain numbers, the dismissal snapshot
    filling what the columns do not hold (a removed file-only line has none)."""
    proposed = _evidence(row).get("proposed")
    proposed = proposed if isinstance(proposed, dict) else {}

    def pick(name: str, number: bool = True) -> Any:
        stored = getattr(row, name, None)
        if stored is not None:
            return round(float(stored), 3 if name in {"rate", "factor_rate"} else 2) if number else stored
        fallback = proposed.get(name)
        return fallback if fallback is not None else None

    def word(name: str, allowed: tuple[str, ...]) -> str | None:
        """A stored word the review's shapes can carry; anything else (a legacy
        cadence, a status another flow wrote) reads as not stated, not as an error."""
        value = pick(name, number=False)
        return value if value in allowed else None

    return {
        "original_amount": pick("original_amount"),
        "balance": pick("balance"),
        "rate": pick("rate"),
        "factor_rate": pick("factor_rate"),
        "payment_amount": pick("payment_amount"),
        "payment_frequency": word("payment_frequency", CADENCES),
        "monthly_payment": pick("monthly_payment"),
        "payment_status": word("payment_status", ("current", "delinquent")),
    }


def _line_from(
    key: str,
    *,
    row: Any | None,
    document_line: dict[str, Any] | None,
    state: str,
) -> dict[str, Any]:
    """One output line: the row when there is one (it is the authority), with
    its blanks filled from the file's reading."""
    figures: dict[str, Any] = {
        "original_amount": None,
        "balance": None,
        "rate": None,
        "factor_rate": None,
        "payment_amount": None,
        "payment_frequency": None,
        "monthly_payment": None,
        "payment_status": None,
    }
    lender = ""
    debt_type = ""
    notes: list[str] = []
    balance_note = payment_note = None
    file_id = file_name = None
    if document_line is not None:
        lender = document_line["lender"]
        debt_type = document_line["debt_type"]
        figures.update({name: document_line.get(name) for name in figures})
        notes = list(document_line["notes_from_file"])
        balance_note, payment_note = document_line.get("balance_note"), document_line.get("payment_note")
        file_id, file_name = document_line.get("file_id"), document_line.get("file_name")
    if row is not None:
        lender = str(row.lender or lender)
        debt_type = debt_types.normalize_debt_type(row.category) or debt_type
        stored = _row_figures(row)
        # The three payment figures describe one payment; they come from the row
        # together or from the file together, never an amount from one beside a
        # cadence from the other.
        if any(stored[name] is not None for name in _PAYMENT_FIGURES):
            figures.update({name: stored[name] for name in _PAYMENT_FIGURES})
        for name, value in stored.items():
            if name not in _PAYMENT_FIGURES and value is not None:
                figures[name] = value
    advance = debt_type in debt_types.ADVANCE_DEBT_TYPES
    origin = getattr(row, "origin", None) if row is not None else None
    removed = _evidence(row).get("removed") if row is not None else None
    removed = removed if isinstance(removed, dict) else {}
    line = {
        "key": key,
        "row_id": str(row.id) if row is not None else None,
        "state": state,
        "origin": origin,
        "origin_label": ORIGIN_LABELS.get(origin or "", "On the schedule")
        if row is not None
        else "Read from the uploaded schedule",
        "file_id": file_id,
        "file_name": file_name,
        "lender": lender,
        "debt_type": debt_type,
        "advance": advance,
        **figures,
        "count_in_dscr": bool(getattr(row, "count_in_dscr", True)) if row is not None else debt_type != "credit_card",
        "balance_note": balance_note,
        "payment_note": payment_note,
        "notes_from_file": notes,
        "removed_reason": removed.get("reason") if removed.get("reason") in REMOVE_REASONS else None,
        "removed_by": removed.get("by_name") or None,
        "removed_at": removed.get("at"),
    }
    line["hint"] = _hint(line)
    line["missing"] = _missing(line)
    return line


def build_review(rows: list[Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    """The review from plain inputs.

    `rows`: every `dos_debts` row on the file, any status. `documents`: the
    file's schedule documents, newest first, each `{file_id, file_name,
    uploaded_by, uploaded_at, key_facts}`.

    A line from a document is matched one-to-one with a row by lender (exact
    first, then the same lender under another spelling), active rows before
    dismissed ones. A line with an active row is on the schedule; with a
    dismissed row it is removed; with none it is only read.
    """
    document_items, sources = document_lines(documents)
    ordered = sorted(
        rows,
        key=lambda row: (
            getattr(row, "status", "active") != "active",
            str(getattr(row, "created_at", "") or ""),
            str(row.id),
        ),
    )
    claimed: set[str] = set()

    def take(lender: str) -> Any | None:
        free = [row for row in ordered if str(row.id) not in claimed]
        wanted = fs._lender_key(lender)
        if not wanted:
            return None
        pool = [row for row in free if fs._lender_key(row.lender) == wanted] or [
            row for row in free if match_names(row.lender or "", lender)
        ]
        if not pool:
            return None
        claimed.add(str(pool[0].id))
        return pool[0]

    lines: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in document_items:
        row = take(item["lender"])
        state = "read" if row is None else "removed" if row.status != "active" else "on_schedule"
        line = _line_from(item["key"], row=row, document_line=item, state=state)
        (removed if state == "removed" else lines).append(line)

    for row in ordered:
        if str(row.id) in claimed:
            continue
        if row.status == "active":
            lines.append(_line_from(f"row:{row.id}", row=row, document_line=None, state="on_schedule"))

    history = [row for row in ordered if str(row.id) not in claimed and row.status != "active"]
    history.sort(key=lambda row: str(getattr(row, "updated_at", "") or getattr(row, "created_at", "") or ""), reverse=True)
    for row in history[:_REMOVED_ROW_LIMIT]:
        removed.append(_line_from(f"row:{row.id}", row=row, document_line=None, state="removed"))

    staying = lines
    return {
        "sources": sources,
        "lines": lines,
        "removed": removed,
        "counts": {
            "read": sum(1 for line in lines if line["state"] == "read"),
            "on_schedule": sum(1 for line in lines if line["state"] == "on_schedule"),
            "removed": len(removed),
            "missing_answers": sum(
                1 for line in staying for name in line["missing"] if name in _THE_THREE
            ),
        },
    }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


async def load_rows(db: AsyncSession, profile: ApplicationProfile) -> list[DealerDebt]:
    """Every row on the file, dismissed ones included. Deliberately not scoped
    by origin: the schedule is the file's, whoever wrote a row."""
    return list(
        (
            await db.execute(
                select(DealerDebt)
                .where(DealerDebt.profile_id == profile.id)
                .order_by(DealerDebt.created_at.asc(), DealerDebt.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def schedule_documents(db: AsyncSession, profile: ApplicationProfile) -> list[dict[str, Any]]:
    """The uploaded schedules the lines are read from, newest first.

    Not our own PDFs of the rows (a drafted form says what the rows said when
    it was filed, including rows since removed), and not a schedule the desk
    unlinked from the requirement: that is how a wrong file is replaced.
    """
    from app.services import application_programs as programs

    files, latest, _classes = await programs._evidence_inventory(db, profile)
    set_aside = await cockpit.set_aside_schedule_files(db, profile)
    by_id = {str(file.id): file for file in files}
    documents: list[dict[str, Any]] = []
    for item in cockpit._analysis_rows(files, latest, set()):
        if "debt_schedule" not in cockpit._classes(item) or item["file_id"] in set_aside:
            continue
        file = by_id.get(item["file_id"])
        analysis = latest.get(file.id) if file is not None else None
        if file is None or analysis is None or analysis.provider == drafted_forms.DRAFTED_FORM_PROVIDER:
            continue
        created = getattr(file, "created_at", None)
        documents.append(
            {
                "file_id": item["file_id"],
                "file_name": item["file_name"],
                "uploaded_by": getattr(file, "uploaded_by_name", None),
                "uploaded_at": created.isoformat() if created else None,
                "key_facts": item["key_facts"],
            }
        )
    documents.sort(key=lambda document: document["uploaded_at"] or "", reverse=True)
    return documents


async def read_review(db: AsyncSession, profile: ApplicationProfile) -> dict[str, Any]:
    review = build_review(await load_rows(db, profile), await schedule_documents(db, profile))
    review["profile_id"] = str(profile.id)
    return review


# ---------------------------------------------------------------------------
# Applying the desk's decisions
# ---------------------------------------------------------------------------


def _payment_changes(line: Any) -> dict[str, Any]:
    """The three payment columns that one answer sets.

    A payment per business day or week is kept as it is with its cadence and
    the month it comes to; a monthly payment is the month and nothing else. An
    amount without a cadence is refused at the schema, because the cockpit
    reads a per-payment figure with no cadence as monthly.
    """
    sent = line.model_fields_set
    if not {"payment_amount", "payment_frequency", "monthly_payment"} & sent:
        return {}
    amount = line.payment_amount if "payment_amount" in sent else None
    cadence = line.payment_frequency if "payment_frequency" in sent else None
    monthly = line.monthly_payment if "monthly_payment" in sent else None
    if amount is not None and cadence in {"daily", "weekly", "biweekly"}:
        return {
            "payment_amount": amount,
            "payment_frequency": cadence,
            "monthly_payment": monthly if monthly is not None else cockpit.monthly_equivalent(amount, cadence),
        }
    if amount is not None and cadence == "monthly" and monthly is None:
        monthly = amount
    return {"monthly_payment": monthly, "payment_amount": None, "payment_frequency": None}


def _changes(line: Any) -> dict[str, Any]:
    """The patch a kept line makes on its row, from only the fields it sent."""
    sent = line.model_fields_set
    changes: dict[str, Any] = {"lender": line.lender}
    if line.debt_type:
        changes["category"] = line.debt_type
    for name in ("original_amount", "balance", "rate", "factor_rate", "payment_status"):
        if name in sent:
            changes[name] = getattr(line, name)
    changes.update(_payment_changes(line))
    return changes


def _figures_of(line: Any) -> dict[str, Any]:
    """What was on the line when it came off, kept inside the evidence only."""
    figures = {
        name: getattr(line, name)
        for name in (
            "debt_type",
            "original_amount",
            "balance",
            "rate",
            "factor_rate",
            "payment_amount",
            "payment_frequency",
            "monthly_payment",
            "payment_status",
        )
        if getattr(line, name, None) not in (None, "")
    }
    return figures


def _find_existing(rows: list[Any], lender: str, claimed: set[str]) -> Any | None:
    """A row already on the file for this lender, for a line the client sent
    without a row id (the same save sent twice must not add twice)."""
    free = [row for row in rows if str(row.id) not in claimed]
    wanted = fs._lender_key(lender)
    if not wanted:
        return None
    pool = [row for row in free if fs._lender_key(row.lender) == wanted] or [
        row for row in free if match_names(row.lender or "", lender)
    ]
    pool.sort(key=lambda row: getattr(row, "status", "active") != "active")
    return pool[0] if pool else None


async def apply_review(
    db: AsyncSession,
    profile: ApplicationProfile,
    user: Any,
    lines: list[Any],
) -> dict[str, int]:
    """Write the desk's decisions onto the file's schedule. Does not commit.

    Kept line with a row: the row is corrected in place (a dismissed one is put
    back). Kept line without one: a row is added for it, under the desk's own
    origin. Removed line with a row: the row is dismissed. Removed line that was
    only in the file: a dismissed row records it, with its figures inside the
    evidence only, so it does not come back and no reader summing the columns
    can count it.
    """
    rows = await load_rows(db, profile)
    document_items, _sources = document_lines(await schedule_documents(db, profile))
    by_key = {item["key"]: item for item in document_items}
    by_id = {str(row.id): row for row in rows}
    claimed: set[str] = set()
    saved = {"kept": 0, "added": 0, "updated": 0, "removed": 0, "restored": 0}
    stamp = datetime.now(UTC).isoformat()
    actor = str(user.id) if getattr(user, "id", None) else None
    actor_name = (getattr(user, "name", None) or getattr(user, "email", None) or None) if user is not None else None

    for line in lines:
        item = by_key.get(line.key)
        row = by_id.get(str(line.row_id)) if line.row_id else None
        if row is not None and str(row.id) in claimed:
            row = None
        if row is None:
            row = _find_existing(rows, line.lender, claimed)
        if row is not None:
            claimed.add(str(row.id))

        if line.keep:
            saved["kept"] += 1
            reviewed = {"by": actor, "at": stamp}
            if row is None:
                row = DealerDebt(
                    id=uuid.uuid4(),
                    profile_id=profile.id,
                    dealer_id=profile.dealer_id,
                    origin="admin",
                    status="active",
                    category=debt_types.normalize_debt_type(line.debt_type) or "loan",
                    lender=line.lender,
                    count_in_dscr=(debt_types.normalize_debt_type(line.debt_type) != "credit_card"),
                    notes=SAVED_NOTE if item is not None else None,
                )
                fs.apply_debt_row_patch(row, _changes(line))
                if item is not None:
                    for name, value in item["extras"].items():
                        if value is not None:
                            setattr(row, name, value)
                row.evidence = {
                    "source": "debt_schedule_review",
                    "file_id": str(line.file_id) if line.file_id else (item or {}).get("file_id"),
                    "file_name": (item or {}).get("file_name"),
                    "line": (item or {}).get("raw") or {},
                    "reviewed": reviewed,
                }
                db.add(row)
                rows.append(row)
                claimed.add(str(row.id))
                saved["added"] += 1
            else:
                restoring = row.status != "active"
                changed = fs.apply_debt_row_patch(row, _changes(line))
                evidence = {
                    key: value for key, value in _evidence(row).items() if key not in {"removed", "proposed"}
                }
                evidence["reviewed"] = reviewed
                if restoring:
                    row.status = "active"
                    if _evidence(row).get("source") == "debt_schedule_review":
                        # A line that was only in the file comes back counting,
                        # now that its figures are in the columns.
                        row.count_in_dscr = debt_types.normalize_debt_type(row.category) != "credit_card"
                    saved["restored"] += 1
                elif changed:
                    saved["updated"] += 1
                row.evidence = evidence
            continue

        reason = line.remove_reason or "not_a_loan"
        removal = {
            "reason": reason,
            "note": (line.remove_note or "").strip() or None,
            "by": actor,
            "by_name": actor_name,
            "at": stamp,
        }
        if row is None:
            fresh = DealerDebt(
                id=uuid.uuid4(),
                profile_id=profile.id,
                dealer_id=profile.dealer_id,
                origin="admin",
                status="dismissed",
                category=debt_types.normalize_debt_type(line.debt_type) or "loan",
                lender=line.lender,
                count_in_dscr=False,
            )
            fresh.evidence = {
                "source": "debt_schedule_review",
                "file_id": str(line.file_id) if line.file_id else (item or {}).get("file_id"),
                "file_name": (item or {}).get("file_name"),
                "line": (item or {}).get("raw") or {},
                "removed": removal,
                "proposed": _figures_of(line),
            }
            db.add(fresh)
            rows.append(fresh)
            claimed.add(str(fresh.id))
            saved["removed"] += 1
            continue
        evidence = dict(_evidence(row))
        if row.status == "active":
            row.status = "dismissed"
            saved["removed"] += 1
            evidence["proposed"] = _figures_of(line) or evidence.get("proposed") or {}
        evidence["removed"] = removal
        row.evidence = evidence

    await db.flush()
    return saved
