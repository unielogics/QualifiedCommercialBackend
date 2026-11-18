"""A personal financial statement for each owner, tracked and versioned per person.

A statement belongs to a person. The financial form engine already keeps a
separate working draft and version history for each personal statement on a
file (a "family"), and an uploaded statement already becomes one. What it never
had was the person: nothing tied a statement to an owner, so a file with two
owners had one checklist row that the first statement satisfied, one link that
opened whichever statement was newest, and no way to see whose was missing.

This module is that tie, and the workflow built on it:

* **Whose statement it is.** A statement names its owners in
  `financial_form_families.identity["owner_ids"]`, which the engine already
  projects into `financial_statement_owners` when a version is saved. Where
  nobody has said, the statement's own applicant name decides it, and only when
  it names exactly one owner. A statement that could be two people's is nobody's
  until the desk says so: a wrong guess would show one owner another's finances.
* **Who has to give one.** Every owner at 20% or more, the same line the file
  already draws for credit authorization, plus any owner the desk asks, less any
  the desk marks as not needed.
* **A private link each.** An owner's link opens their own statement and
  nothing else, prefilled with their own name and address.
* **One history each.** Every saved version, online or uploaded, stays under the
  owner it belongs to.
* **A checklist that waits for all of them.** The requirement reads "1 of 2
  owner statements" and is met only when every required owner's statement is in.

Nothing here is summed across people. Net worth and liquidity are one person's.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import select

KIND = "pfs"
REQUIREMENT_KEY = "owner_personal_financial_statement"
LABEL = "Personal financial statement"

#: The ownership share from which an owner gives their own statement. The same
#: line `ApplicationOwner.credit_required` draws for credit authorization.
THRESHOLD_PCT = Decimal("20")

ACTION_KIND = "owner_statement_link"
INITIATION_SOURCE = "staff_owner_statement"

#: Two clicks closer together than this are one request.
DUPLICATE_WINDOW = timedelta(minutes=2)

#: What a statement is called before anybody has typed a name into it.
_PLACEHOLDER_NAMES = frozenset({"", "applicant", "business"})
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})

#: `default_statement_row` answers this when the file has no managed statement,
#: so the caller keeps its own older lookup.
UNMANAGED = object()


# ---------------------------------------------------------------------------
# One statement, and what can be read off it without a database
# ---------------------------------------------------------------------------


@dataclass
class Statement:
    """One personal statement on a file: its family, draft, versions and links."""

    family: Any
    draft: Any = None
    #: Newest first.
    versions: list = field(default_factory=list)
    #: Newest first.
    links: list = field(default_factory=list)


def _identity(statement: Statement) -> dict:
    value = getattr(statement.family, "identity", None)
    return value if isinstance(value, dict) else {}


def explicit_owner_ids(statement: Statement) -> list[str] | None:
    """The owners somebody said this statement is for. None when nobody has."""
    value = _identity(statement).get("owner_ids")
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def current_version(statement: Statement) -> Any:
    pointer = getattr(statement.family, "current_version_id", None)
    return next((row for row in statement.versions if row.id == pointer), None) if pointer else None


def pending_upload(statement: Statement) -> Any:
    """An uploaded statement the desk has not reviewed yet.

    The same rule `financial_version_selection._latest_pending_original` applies
    in SQL: the newest uploaded original that is not the current version and has
    nothing filed after it. It already counts as received.
    """
    pointer = getattr(statement.family, "current_version_id", None)
    for version in statement.versions:
        if getattr(version, "status", None) not in {"original", "filed"}:
            continue
        if version.status == "original" and getattr(version, "source_file_id", None) and version.id != pointer:
            return version
        return None
    return None


def received_version(statement: Statement) -> Any:
    """The version that makes this statement received, or None while it is not."""
    pending = pending_upload(statement)
    if pending is not None:
        return pending
    current = current_version(statement)
    if current is not None and getattr(current, "status", None) in {"filed", "original"}:
        return current
    return None


def file_ids(statement: Statement) -> set[str]:
    """Every stored file behind this statement's versions."""
    return {
        str(value)
        for version in statement.versions
        for value in (getattr(version, "pdf_file_id", None), getattr(version, "source_file_id", None))
        if value
    }


def has_answers(body: Any) -> bool:
    """Whether somebody has typed figures. The name block is prefilled, so it does not count."""
    if not isinstance(body, dict):
        return False
    for block in ("assets", "liabilities", "income", "contingent"):
        values = body.get(block)
        if isinstance(values, dict) and any(value not in (None, "") for value in values.values()):
            return True
    schedules = body.get("schedules")
    if isinstance(schedules, dict) and any(rows for rows in schedules.values()):
        return True
    return bool(str(body.get("notes") or "").strip())


def has_content(statement: Statement) -> bool:
    return bool(statement.versions) or has_answers(getattr(statement.draft, "body", None))


def applicant_name(statement: Statement) -> str:
    """The person the statement itself names: what was typed, then what was read off a document."""
    for body in (getattr(statement.draft, "body", None), getattr(current_version(statement), "body", None)):
        if isinstance(body, dict):
            name = str((body.get("applicant") or {}).get("name") or "").strip()
            if name:
                return name
    subject = str(_identity(statement).get("subject") or "").strip()
    if subject.casefold() not in _PLACEHOLDER_NAMES:
        return subject
    for version in statement.versions:
        facts = getattr(version, "key_facts", None) or {}
        for key in ("applicant_name", "name", "owner_name"):
            name = str(facts.get(key) or "").strip()
            if name:
                return name
    return ""


def _name_parts(value: Any) -> list[str]:
    cleaned = re.sub(r"[^\w\s]", " ", str(value or "").casefold())
    return [part for part in cleaned.split() if part not in _NAME_SUFFIXES]


def name_match(owner_name: Any, statement_name: Any) -> int:
    """How surely two names are one person: 3 the same, 2 same first and last, 1 an initial and the last name, 0 no."""
    left, right = _name_parts(owner_name), _name_parts(statement_name)
    if not left or not right:
        return 0
    if left == right:
        return 3
    if len(left) < 2 or len(right) < 2 or left[-1] != right[-1]:
        return 0
    if left[0] == right[0]:
        return 2
    if (len(left[0]) == 1 or len(right[0]) == 1) and left[0][0] == right[0][0]:
        return 1
    return 0


def owner_pct(owner: Any) -> Decimal | None:
    try:
        value = Decimal(str(owner.ownership_pct))
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return value if value.is_finite() and 0 <= value <= 100 else None


def owner_name(owner: Any) -> str:
    return " ".join(
        part for part in (str(getattr(owner, "first_name", "") or "").strip(), str(getattr(owner, "last_name", "") or "").strip()) if part
    )


def _created(value: Any) -> datetime:
    stamp = getattr(value, "created_at", None)
    if not isinstance(stamp, datetime):
        return datetime.min.replace(tzinfo=UTC)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Owners and their statements
# ---------------------------------------------------------------------------


@dataclass
class OwnerRow:
    """One owner, and the statements that are theirs."""

    owner: Any
    #: Every statement tied to this owner, the one that counts first.
    statements: list[Statement] = field(default_factory=list)
    #: How the first statement came to be theirs: assigned, name, only_owner, primary.
    how: str | None = None
    required: bool = False
    #: ownership, requested, not_needed, under_threshold.
    reason: str = "under_threshold"
    request: dict = field(default_factory=dict)

    @property
    def statement(self) -> Statement | None:
        return self.statements[0] if self.statements else None


@dataclass
class Tracker:
    """Every owner on a file beside their statement, and the statements nobody owns."""

    rows: list[OwnerRow] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    unassigned: list[Statement] = field(default_factory=list)

    def row_for(self, owner_id: Any) -> OwnerRow | None:
        return next((row for row in self.rows if str(row.owner.id) == str(owner_id)), None)

    @property
    def needed(self) -> list[OwnerRow]:
        return [row for row in self.rows if row.required]


def _request_for(statements: list[Statement], owner_id: str) -> dict:
    """What the desk said about this owner's statement, wherever it was recorded."""
    newest: dict = {}
    for statement in statements:
        entry = (_identity(statement).get("owner_requests") or {}).get(owner_id)
        if isinstance(entry, dict) and str(entry.get("at") or "") >= str(newest.get("at") or ""):
            newest = entry
    return newest


def _rank(statement: Statement) -> tuple:
    """Which of one owner's statements counts: the latest received, then one with figures, then the newest."""
    received = received_version(statement)
    return (
        received is not None,
        _created(received) if received is not None else datetime.min.replace(tzinfo=UTC),
        has_content(statement),
        _created(statement.family),
    )


def build(owners: list, statements: list[Statement]) -> Tracker:
    """Tie each statement to its owner. No database, no writes.

    1. A statement the desk assigned belongs to those owners.
    2. Otherwise the name on the statement decides, when it names exactly one owner.
    3. On a file where one owner has to give a statement, every other statement is theirs.
    4. A statement nobody has put a name on is the primary owner's, when they have none.

    Anything left is unassigned. It counts for nobody until the desk assigns it.
    """
    active = [owner for owner in owners if not getattr(owner, "archived_at", None)]
    by_id = {str(owner.id): owner for owner in active}
    bound: dict[str, list[Statement]] = {key: [] for key in by_id}
    how: dict[tuple[str, int], str] = {}
    ordered = sorted(statements, key=lambda item: _created(item.family))
    free: list[Statement] = []

    def give(owner_id: str, statement: Statement, reason: str) -> None:
        bound[owner_id].append(statement)
        how[(owner_id, id(statement))] = reason

    for statement in ordered:
        explicit = explicit_owner_ids(statement)
        if explicit is None:
            free.append(statement)
            continue
        # An assignment to an owner who has since left the file is not handed to
        # somebody else by a guess: the statement waits for the desk.
        for owner_id in explicit:
            if owner_id in by_id:
                give(owner_id, statement, "assigned")

    # The desk took it away from an owner, or its owners have all left the
    # file: it waits for the desk and is never handed on by a guess. One the
    # desk set aside as no owner's (a spouse's, say) is not listed at all.
    unassigned: list[Statement] = [
        statement
        for statement in ordered
        if (explicit := explicit_owner_ids(statement)) is not None
        and not any(owner_id in by_id for owner_id in explicit)
        and not _identity(statement).get("set_aside")
    ]

    remaining: list[Statement] = []
    for statement in free:
        name = applicant_name(statement)
        scores = {owner_id: name_match(owner_name(owner), name) for owner_id, owner in by_id.items()}
        best = max(scores.values(), default=0)
        winners = [owner_id for owner_id, score in scores.items() if score == best and score > 0]
        if len(winners) == 1:
            give(winners[0], statement, "name")
        else:
            remaining.append(statement)

    needs_by_share = [owner_id for owner_id, owner in by_id.items() if (owner_pct(owner) or Decimal(0)) >= THRESHOLD_PCT]
    if len(needs_by_share) == 1 and remaining:
        for statement in remaining:
            give(needs_by_share[0], statement, "only_owner")
        remaining = []

    primary = next((owner_id for owner_id, owner in by_id.items() if getattr(owner, "is_primary", False)), None)
    if primary is None and len(by_id) == 1:
        primary = next(iter(by_id))
    for statement in list(remaining):
        if primary is not None and not bound[primary] and applicant_name(statement).casefold() in _PLACEHOLDER_NAMES:
            give(primary, statement, "primary")
            remaining.remove(statement)
    unassigned.extend(remaining)

    rows: list[OwnerRow] = []
    for owner_id, owner in by_id.items():
        mine = sorted(bound[owner_id], key=_rank, reverse=True)
        request = _request_for(statements, owner_id)
        share = owner_pct(owner) or Decimal(0)
        if request.get("state") == "not_needed":
            required, reason = False, "not_needed"
        elif share >= THRESHOLD_PCT:
            required, reason = True, "ownership"
        elif request.get("state") == "requested":
            required, reason = True, "requested"
        else:
            required, reason = False, "under_threshold"
        rows.append(
            OwnerRow(
                owner=owner,
                statements=mine,
                how=how.get((owner_id, id(mine[0]))) if mine else None,
                required=required,
                reason=reason,
                request=request,
            )
        )
    return Tracker(rows=rows, statements=list(statements), unassigned=sorted(unassigned, key=lambda item: _created(item.family)))


def default_person(tracker: Tracker, *, email: str | None = None) -> OwnerRow | None:
    """Who a link that names nobody is for: the person it was sent to, else the applicant."""
    wanted = (email or "").strip().lower()
    if wanted:
        matches = [row for row in tracker.rows if (getattr(row.owner, "email", None) or "").strip().lower() == wanted]
        if len(matches) == 1:
            return matches[0]
    primary = next((row for row in tracker.rows if getattr(row.owner, "is_primary", False)), None)
    if primary is not None:
        return primary
    return min(tracker.rows, key=lambda row: _created(row.owner)) if tracker.rows else None


def default_statement(tracker: Tracker, *, email: str | None = None) -> tuple[OwnerRow | None, Statement | None]:
    """The statement a link that names nobody opens. Never another owner's.

    With owners on the file it is the applicant's own, or nothing yet. A file
    with no owner rows keeps the older rule: its newest statement.
    """
    person = default_person(tracker, email=email)
    if person is not None:
        return person, person.statement
    newest = max(tracker.statements, key=lambda item: _created(item.family), default=None)
    return None, newest


# ---------------------------------------------------------------------------
# Status, in the desk's words
# ---------------------------------------------------------------------------


def active_link(statement: Statement | None, *, now: datetime | None = None) -> Any:
    """The newest link to this statement that still works."""
    if statement is None:
        return None
    now = now or datetime.now(UTC)
    for link in statement.links:
        if getattr(link, "revoked_at", None) is not None:
            continue
        expires = getattr(link, "expires_at", None)
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires is not None and expires <= now:
            continue
        if getattr(link, "permission", "edit") != "edit":
            continue
        return link
    return None


def row_status(row: OwnerRow, *, now: datetime | None = None) -> str:
    """not_needed, received, in_progress, opened, sent, not_started or not_required."""
    if row.reason == "not_needed":
        return "not_needed"
    for statement in row.statements:
        if received_version(statement) is not None:
            return "received"
    for statement in row.statements:
        if current_version(statement) is not None or has_answers(getattr(statement.draft, "body", None)):
            return "in_progress"
    links = [link for statement in row.statements if (link := active_link(statement, now=now)) is not None]
    if any(getattr(link, "last_used_at", None) for link in links):
        return "opened"
    if links:
        return "sent"
    return "not_started" if row.required else "not_required"


def _money(value: Any) -> float | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(number) if number.is_finite() else None


def figures_from_facts(facts: Any) -> dict[str, Any] | None:
    """The five figures a statement is read by. One person's, never summed."""
    facts = facts if isinstance(facts, dict) else {}
    figures = {
        "statement_date": facts.get("statement_date") or None,
        **{key: _money(facts.get(key)) for key in ("total_assets", "total_liabilities", "net_worth", "liquid_assets")},
    }
    return figures if any(value is not None for value in figures.values()) else None


def figures_of(version: Any) -> dict[str, Any] | None:
    return figures_from_facts(getattr(version, "key_facts", None))


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _version_read(statement: Statement, version: Any) -> dict[str, Any]:
    pointer = getattr(statement.family, "current_version_id", None)
    pending = pending_upload(statement)
    return {
        "id": str(version.id),
        "family_id": str(statement.family.id),
        "version_number": version.version_number,
        "created_at": _iso(getattr(version, "created_at", None)),
        "source": getattr(version, "source", None),
        "status": getattr(version, "status", None),
        "actor_name": getattr(version, "actor_name", None),
        "by_staff": getattr(version, "actor_user_id", None) is not None,
        "has_pdf": bool(getattr(version, "pdf_file_id", None) or getattr(version, "source_file_id", None)),
        "is_current": version.id == pointer,
        "awaiting_review": pending is not None and version.id == pending.id,
        "figures": figures_of(version),
    }


def _statement_read(statement: Statement, *, now: datetime) -> dict[str, Any]:
    received = received_version(statement)
    link = active_link(statement, now=now)
    draft = statement.draft
    current = current_version(statement)
    dirty = bool(draft is not None and has_answers(getattr(draft, "body", None)) and _differs(draft, current))
    return {
        "family_id": str(statement.family.id),
        "label": getattr(statement.family, "label", None) or LABEL,
        "applicant_name": applicant_name(statement) or None,
        "received": received is not None,
        "received_version": _version_read(statement, received) if received is not None else None,
        "draft_in_progress": dirty,
        "draft_updated_at": _iso(getattr(draft, "updated_at", None)) if dirty else None,
        "figures": figures_of(received) if received is not None else None,
        "versions": [_version_read(statement, version) for version in statement.versions],
        "link": {
            "sent_at": _iso(getattr(link, "created_at", None)),
            "sent_to": getattr(link, "invitee_email", None),
            "opened_at": _iso(getattr(link, "last_used_at", None)),
            "use_count": int(getattr(link, "use_count", 0) or 0),
            "completed_at": _iso(getattr(link, "completed_at", None)),
            "expires_at": _iso(getattr(link, "expires_at", None)),
        }
        if link is not None
        else None,
    }


def _differs(draft: Any, version: Any) -> bool:
    """Whether the working draft holds something its current saved version does not."""
    if version is None:
        return True
    from app.services.financial_workspace import body_hash

    return body_hash(getattr(draft, "body", None) or {}) != getattr(version, "body_sha256", None)


def read_model(tracker: Tracker, *, now: datetime | None = None) -> dict[str, Any]:
    """Every owner beside their statement, for the desk."""
    now = now or datetime.now(UTC)
    names = {str(row.owner.id): owner_name(row.owner) for row in tracker.rows}
    owners = []
    for row in tracker.rows:
        status_code = row_status(row, now=now)
        statement = row.statement
        joint = sorted(
            names[other]
            for other in (explicit_owner_ids(statement) or [] if statement is not None else [])
            if other in names and other != str(row.owner.id)
        )
        share = owner_pct(row.owner)
        owners.append(
            {
                "owner_id": str(row.owner.id),
                "full_name": names[str(row.owner.id)],
                "first_name": str(getattr(row.owner, "first_name", "") or ""),
                "last_name": str(getattr(row.owner, "last_name", "") or ""),
                "email": getattr(row.owner, "email", None) or None,
                "ownership_pct": float(share) if share is not None else None,
                "is_primary": bool(getattr(row.owner, "is_primary", False)),
                "required": row.required,
                "reason": row.reason,
                "request_note": str(row.request.get("reason") or "") or None,
                "request_by": str(row.request.get("by_name") or "") or None,
                "request_at": str(row.request.get("at") or "") or None,
                "status": status_code,
                "matched_by": row.how,
                "joint_with": joint,
                "statement": _statement_read(statement, now=now) if statement is not None else None,
                "other_statements": [_statement_read(item, now=now) for item in row.statements[1:]],
            }
        )
    needed = [item for item in owners if item["required"]]
    received = [item for item in needed if item["status"] == "received"]
    return {
        "threshold_pct": float(THRESHOLD_PCT),
        "required_count": len(needed),
        "received_count": len(received),
        "complete": bool(needed) and len(received) == len(needed),
        "missing_owner_ids": [item["owner_id"] for item in needed if item["status"] != "received"],
        "owners": owners,
        "unassigned": [_statement_read(statement, now=now) for statement in tracker.unassigned if has_content(statement)],
    }


# ---------------------------------------------------------------------------
# The checklist: one requirement, met only when every required owner's is in
# ---------------------------------------------------------------------------


def is_requirement(requirement: Any) -> bool:
    return (getattr(requirement, "canonical_key", None) or getattr(requirement, "requirement_key", None)) == REQUIREMENT_KEY


def coverage_snapshot(tracker: Tracker, *, linked: set[str], accepted: set[str]) -> dict[str, Any]:
    """Which required owners the requirement's own evidence covers.

    `linked` and `accepted` are the files the evidence engine tied to the
    requirement and the ones it accepted. An owner is received when one of their
    statement's files is linked, and accepted when one is accepted.

    A file that belongs to no statement at all (a document uploaded before
    statements were tracked) cannot say whose it is. On a file where one owner
    has to give a statement it is theirs; with several it counts for nobody
    until the desk assigns it.
    """
    needed = tracker.needed
    claimed: set[str] = set()
    for statement in tracker.statements:
        claimed |= file_ids(statement)
    loose_linked = linked - claimed
    loose_accepted = accepted - claimed
    alone = len(needed) == 1
    owners = []
    for row in needed:
        mine: set[str] = set()
        for statement in row.statements:
            mine |= file_ids(statement)
        got = bool(mine & linked) or (alone and bool(loose_linked))
        ok = bool(mine & accepted) or (alone and bool(loose_accepted))
        owners.append(
            {
                "owner_id": str(row.owner.id),
                "name": owner_name(row.owner),
                "received": got,
                "accepted": ok,
                "family_id": str(row.statement.family.id) if row.statement is not None else None,
                "reason": row.reason,
            }
        )
    received = sum(item["received"] for item in owners)
    accepted_count = sum(item["accepted"] for item in owners)
    unassigned = [statement for statement in tracker.unassigned if file_ids(statement) & linked]
    snapshot = {
        "source": "owner_statements",
        "required_owner_count": len(owners),
        "received_owner_count": received,
        "accepted_owner_count": accepted_count,
        "complete": bool(owners) and received == len(owners),
        "accepted_complete": bool(owners) and accepted_count == len(owners),
        "owners": owners,
        "missing": [item["name"] for item in owners if not item["received"]],
        "unassigned_count": len(unassigned) + (0 if alone else len(loose_linked)),
        "not_needed": [owner_name(row.owner) for row in tracker.rows if row.reason == "not_needed"],
    }
    return snapshot


def _names(values: list[str]) -> str:
    if len(values) <= 1:
        return "".join(values)
    return f"{', '.join(values[:-1])} and {values[-1]}"


def reason_words(snapshot: dict[str, Any]) -> str:
    required, received = snapshot["required_owner_count"], snapshot["received_owner_count"]
    words = f"{received} of {required} owner {'statement' if required == 1 else 'statements'} received."
    if snapshot["missing"]:
        words += f" Still needed from {_names(snapshot['missing'])}."
    elif not snapshot["accepted_complete"]:
        words += " Each one is in; review is not finished."
    if snapshot["unassigned_count"]:
        count = snapshot["unassigned_count"]
        words += f" {count} uploaded {'statement is' if count == 1 else 'statements are'} not assigned to an owner yet."
    return words


def room_words(snapshot: dict[str, Any]) -> str:
    """What the shared room says under the row. Names and who is in, never a figure."""
    done = [item["name"] for item in snapshot["owners"] if item["received"]]
    lines = ["A statement is needed from each owner."]
    if done:
        lines.append(f"Received from {_names(done)}.")
    if snapshot["missing"]:
        lines.append(f"Still needed from {_names(snapshot['missing'])}.")
    lines.append("Each owner fills in their own online from a private link we email them, or uploads a completed copy here.")
    return " ".join(lines)


async def materialize(db: Any, profile: Any, requirement: Any, state: Any, requested: Any) -> bool:
    """Make the personal-statement requirement wait for every required owner.

    Runs after the evidence engine has linked and reviewed the files. It changes
    nothing on a file with no owner who has to give a statement, so a file
    without owner rows keeps the one-document rule it always had.
    """
    if not is_requirement(requirement):
        return False
    provenance = dict(getattr(state, "provenance", None) or {})
    tracker = await load(db, profile, with_links=False)
    if not tracker.needed:
        if "owner_statements" in provenance:
            previous = provenance.pop("owner_statements")
            if requested is not None and previous.get("slot_description") is not None and getattr(requested, "description", None) == previous.get("room_words"):
                requested.description = previous["slot_description"]
            state.provenance = provenance
        return False

    coverage = dict(provenance.get("coverage") or {})
    expired = {str(value) for value in coverage.get("expired_file_ids") or []}
    linked = {str(value) for value in provenance.get("linked_file_ids") or []} - expired
    accepted = {str(value) for value in provenance.get("accepted_file_ids") or []} - expired
    snapshot = coverage_snapshot(tracker, linked=linked, accepted=accepted)
    previous = provenance.get("owner_statements") or {}
    required = snapshot["required_owner_count"]
    several = required > 1

    from app.services import requirement_lifecycle

    settled = requirement_lifecycle.is_removed(state) or state.status in {"waived", "not_applicable", "failed", "stale"}
    if not settled:
        if state.status == "verified" and not snapshot["accepted_complete"]:
            state.status = "received_unverified"
            state.verified_at = None
            state.verified_by_user_id = None
            state.state_reason = reason_words(snapshot)
        elif state.status in {"received_unverified", "requested", "missing"} and (snapshot["missing"] or snapshot["unassigned_count"]):
            if state.status != "received_unverified" or several:
                state.state_reason = reason_words(snapshot)
        if requested is not None and not getattr(requested, "requires_signature", False):
            requested.status = "uploaded" if snapshot["accepted_complete"] and state.status == "verified" else "requested"

    slot_description = previous.get("slot_description")
    words = room_words(snapshot) if several else None
    if requested is not None:
        current_words = getattr(requested, "description", None)
        if several:
            if slot_description is None and current_words != previous.get("room_words"):
                slot_description = current_words or ""
            requested.description = words
        elif previous.get("room_words") and current_words == previous.get("room_words") and slot_description is not None:
            requested.description = slot_description
            slot_description = None

    snapshot["room_words"] = words
    snapshot["slot_description"] = slot_description
    completion = dict(provenance.get("collection_completion") or {})
    if completion.get("source") == "files" and not snapshot["complete"]:
        completion.update(
            complete=False,
            can_complete=False,
            reason="Every required owner needs their own statement before collection can be marked complete.",
        )
    state.provenance = {
        **provenance,
        "owner_statements": snapshot,
        "coverage": {
            **coverage,
            "current": snapshot["received_owner_count"],
            "required": required,
            "unit": "owner statements",
            "complete": snapshot["complete"],
        },
        "verified_coverage": {
            **dict(provenance.get("verified_coverage") or {}),
            "current": snapshot["accepted_owner_count"],
            "required": required,
            "unit": "owner statements",
            "complete": snapshot["accepted_complete"] and state.status in {"verified", "waived", "not_applicable"},
        },
        **({"collection_completion": completion} if completion else {}),
    }
    return True


async def all_received(db: Any, profile: Any) -> bool:
    """Whether every required owner's statement is in. True on a file that tracks no owners."""
    tracker = await load(db, profile, with_links=False)
    return all(row_status(row) == "received" for row in tracker.needed)


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


async def _owners(db: Any, profile: Any) -> list:
    """The file's active owners, from whichever table backs it.

    The same split as `application_profiles.owner_rows`, without that reader's
    `populate_existing`: this runs inside the form engine's own writes and must
    not reload an owner somebody is in the middle of editing.
    """
    from app.dealer_os.models import DealerOwner
    from app.models.application_profile import ApplicationOwner

    if getattr(profile, "dealer_id", None):
        query = select(DealerOwner).where(DealerOwner.dealer_id == profile.dealer_id).order_by(DealerOwner.created_at.asc(), DealerOwner.id.asc())
    else:
        query = (
            select(ApplicationOwner)
            .where(ApplicationOwner.profile_id == profile.id, ApplicationOwner.archived_at.is_(None))
            .order_by(ApplicationOwner.created_at.asc(), ApplicationOwner.id.asc())
        )
    # Only owner rows. A scripted test session answers every query with
    # whatever it holds, and nothing but an owner may be read as one.
    return [row for row in (await db.execute(query)).scalars().all() if isinstance(row, ApplicationOwner | DealerOwner)]


async def load(db: Any, profile: Any, *, families: list | None = None, with_links: bool = True) -> Tracker:
    """Every owner and every personal statement on the file, tied together."""
    from app.models.financial_form_link import FinancialFormLink
    from app.models.financial_form_version import (
        FinancialFormDraft,
        FinancialFormFamily,
        FinancialFormVersion,
    )
    from app.services import financial_workspace as workspace

    if not isinstance(getattr(db, "info", None), dict):
        return Tracker()
    owners = await _owners(db, profile)
    if families is None:
        families = await workspace._families(db, profile.id, KIND)
    statements = {
        family.id: Statement(family=family)
        for family in families
        if isinstance(family, FinancialFormFamily) and family.kind == KIND
    }
    if statements:
        ids = list(statements)
        versions = (
            await db.execute(
                select(FinancialFormVersion)
                .where(FinancialFormVersion.family_id.in_(ids))
                .order_by(FinancialFormVersion.version_number.desc())
            )
        ).scalars().all()
        for version in versions:
            if isinstance(version, FinancialFormVersion) and version.family_id in statements:
                statements[version.family_id].versions.append(version)
        drafts = (await db.execute(select(FinancialFormDraft).where(FinancialFormDraft.family_id.in_(ids)))).scalars().all()
        for draft in drafts:
            if isinstance(draft, FinancialFormDraft) and draft.family_id in statements:
                statements[draft.family_id].draft = draft
        if with_links:
            by_statement = {
                str(_identity(statement).get("statement_id")): statement
                for statement in statements.values()
                if _identity(statement).get("statement_id")
            }
            links = (
                await db.execute(
                    select(FinancialFormLink)
                    .where(FinancialFormLink.profile_id == profile.id, FinancialFormLink.kind == KIND)
                    .order_by(FinancialFormLink.created_at.desc())
                )
            ).scalars().all()
            for link in links:
                if not isinstance(link, FinancialFormLink):
                    continue
                target = (
                    statements.get(link.financial_family_id)
                    if link.financial_family_id
                    else by_statement.get(str(link.statement_id))
                    if link.statement_id
                    else None
                )
                if target is not None:
                    target.links.append(link)
    return build(owners, list(statements.values()))


async def read(db: Any, profile: Any) -> dict[str, Any]:
    tracker = await load(db, profile)
    result = read_model(tracker)
    result["profile_id"] = str(profile.id)
    from app.routers.application_profiles import _requested_slot

    slot = await _requested_slot(db, profile, KIND)
    result["on_checklist"] = slot is not None
    result["loose_files"] = await _loose_files(db, profile, tracker, slot)
    return result


async def _loose_files(db: Any, profile: Any, tracker: Tracker, slot: Any) -> list[dict[str, Any]]:
    """Uploaded statements that belong to no statement at all, so the desk can say whose each is."""
    if getattr(profile, "primary_bucket_id", None) is None:
        return []
    from app.models.bucket import BucketFile, BucketFileAnalysis
    from app.services.document_version_selection import current_document_file_clause

    rows = (
        await db.execute(
            select(BucketFile, BucketFileAnalysis)
            .join(BucketFileAnalysis, BucketFileAnalysis.bucket_file_id == BucketFile.id)
            .where(
                BucketFile.bucket_id == profile.primary_bucket_id,
                BucketFile.deleted_at.is_(None),
                BucketFile.status == "uploaded",
                BucketFileAnalysis.status == "completed",
                BucketFileAnalysis.classification == "personal_financial_statement",
                current_document_file_clause(BucketFile.id),
            )
            .order_by(BucketFile.created_at.desc(), BucketFileAnalysis.created_at.desc())
        )
    ).all()
    claimed: set[str] = set()
    for statement in tracker.statements:
        claimed |= file_ids(statement)
    seen: set[str] = set()
    loose = []
    for file, analysis in rows:
        key = str(file.id)
        if key in seen or key in claimed:
            continue
        seen.add(key)
        facts = (analysis.analysis or {}).get("key_facts") or {}
        loose.append(
            {
                "file_id": key,
                "file_name": file.file_name,
                "uploaded_at": _iso(file.created_at),
                "applicant_name": str(facts.get("applicant_name") or facts.get("name") or "") or None,
                "figures": figures_from_facts(facts),
            }
        )
    return loose


# ---------------------------------------------------------------------------
# The older "one statement per file" readers, made safe for several owners
# ---------------------------------------------------------------------------


async def default_family(db: Any, profile_id: UUID, families: list) -> Any:
    """The statement a reader that names nobody gets. Never another owner's.

    `financial_workspace.existing_family` used to hand back the file's newest
    personal statement, whoever it belonged to. With owners on the file that is
    the applicant's own statement, or nothing when they have not started one.
    """
    from app.models.application_profile import ApplicationProfile

    profile = await db.get(ApplicationProfile, profile_id)
    if profile is None:
        return families[0] if families else None
    tracker = await load(db, profile, families=families, with_links=False)
    _person, statement = default_statement(tracker)
    return statement.family if statement is not None else None


async def default_statement_row(db: Any, profile_id: UUID) -> Any:
    """The stored statement behind `default_family`, for readers that still ask for a row.

    `UNMANAGED` when the file has no managed statement, so the caller's own
    newest-wins lookup stands. Otherwise the applicant's statement row, or a
    statement no family has claimed yet, or None. Never a row another owner's
    statement projected.
    """
    from app.models.application_profile import ApplicationProfile
    from app.models.financial_statement import FinancialStatement
    from app.services import financial_workspace as workspace

    families = await workspace._families(db, profile_id, KIND)
    if not families:
        return UNMANAGED
    profile = await db.get(ApplicationProfile, profile_id)
    if profile is None:
        return UNMANAGED
    tracker = await load(db, profile, families=families, with_links=False)
    _person, statement = default_statement(tracker)
    if statement is not None:
        pointer = _identity(statement).get("statement_id")
        return await db.get(FinancialStatement, UUID(str(pointer))) if pointer else None
    return await _waiting_statement(db, profile, tracker)


def joinable(tracker: Tracker, families: list, document_name: Any) -> list:
    """Which existing statements an uploaded document may join as a new version.

    The engine's own rule treats a statement nobody has named as a match for any
    document, which on a file with several owners files an upload under
    whichever owner's statement is newest. Here a document that names a person
    may join that person's statement, or one nobody owns that carries the same
    name; a document that names nobody joins the file's only statement when one
    owner at most has to give one, and otherwise starts its own, for the desk to
    assign.
    """
    if not tracker.rows:
        return list(families)
    owners_of: dict[Any, set[str]] = {}
    for row in tracker.rows:
        for statement in row.statements:
            owners_of.setdefault(statement.family.id, set()).add(str(row.owner.id))
    by_family = {statement.family.id: statement for statement in tracker.statements}
    name = str(document_name or "").strip()
    if name.casefold() in _PLACEHOLDER_NAMES:
        return list(families) if len(families) == 1 and len(tracker.needed) <= 1 else []
    scores = {str(row.owner.id): name_match(owner_name(row.owner), name) for row in tracker.rows}
    best = max(scores.values(), default=0)
    winners = [owner_id for owner_id, score in scores.items() if score == best and score > 0]
    person = winners[0] if len(winners) == 1 else None
    kept = []
    for family in families:
        tied = owners_of.get(family.id)
        if tied:
            if person is not None and person in tied:
                kept.append(family)
            continue
        statement = by_family.get(family.id)
        if statement is not None and explicit_owner_ids(statement) is None and applicant_name(statement).casefold() == name.casefold():
            kept.append(family)
    return kept


async def import_candidates(db: Any, profile: Any, families: list, identity: dict) -> list:
    """`joinable`, read from the file."""
    if not families:
        return []
    tracker = await load(db, profile, families=families, with_links=False)
    return joinable(tracker, families, identity.get("subject"))


# ---------------------------------------------------------------------------
# Writing: an owner's statement, their link, and what the desk decides
# ---------------------------------------------------------------------------


def _family_label(names: list[str]) -> str:
    people = " & ".join(name for name in names if name)
    return f"{LABEL} · {people}"[:255] if people else LABEL


def owner_prefill(owner: Any, *, business_name: str | None = None) -> dict[str, Any]:
    """The owner's own details, in the shape `financial_statements.form_prefill` returns."""
    street = str(getattr(owner, "street", "") or "").strip()
    locality = ", ".join(part for part in (str(getattr(owner, "city", "") or "").strip(), str(getattr(owner, "state", "") or "").strip()) if part)
    locality = " ".join(part for part in (locality, str(getattr(owner, "zip", "") or "").strip()) if part)
    return {
        "business_name": business_name or None,
        "owner_name": owner_name(owner) or None,
        "home_address": ", ".join(part for part in (street, locality) if part) or None,
        "business_phone": str(getattr(owner, "phone", "") or "").strip() or None,
    }


async def _owner_or_404(db: Any, profile: Any, owner_id: UUID) -> Any:
    owner = next((row for row in await _owners(db, profile) if row.id == owner_id), None)
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found")
    return owner


async def _start(db: Any, profile: Any, owner: Any, *, actor: Any = None) -> Any:
    """A new, empty statement for one owner, prefilled with their own details."""
    from app.models.financial_form_version import FinancialFormDraft, FinancialFormFamily
    from app.services import financial_statements, pfs_schema
    from app.services import financial_workspace as workspace

    name = owner_name(owner)
    family_id = uuid4()
    identity = {"subject": name or "applicant", "owner_ids": [str(owner.id)]}
    # The key only has to be unique on the file. Including the family's own id
    # keeps a second statement for the same person from colliding with the first.
    family = FinancialFormFamily(
        id=family_id,
        profile_id=profile.id,
        kind=KIND,
        identity_key=workspace.body_hash({**identity, "family": str(family_id)}),
        identity=identity,
        label=_family_label([name]),
        created_at=workspace.now(),
    )
    db.add(family)
    await db.flush()
    business = (await financial_statements.form_prefill(db, profile)).get("business_name")
    body = financial_statements.seed_pfs_applicant(pfs_schema.empty_body(), owner_prefill(owner, business_name=business))
    db.add(
        FinancialFormDraft(
            family_id=family.id,
            body=workspace.validate_body(KIND, body),
            revision=0,
            updated_at=workspace.now(),
            updated_by_user_id=getattr(actor, "id", None),
        )
    )
    await db.flush()
    return family


async def statement_for(db: Any, profile: Any, owner: Any, *, actor: Any = None) -> Any:
    """The owner's statement, started if they have none. Writes the tie down so a later rename cannot loosen it."""
    from app.services import financial_workspace as workspace

    await workspace.lock_financial_profile(db, profile)
    tracker = await load(db, profile, with_links=False)
    row = tracker.row_for(owner.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found")
    if row.statement is None:
        return await _start(db, profile, owner, actor=actor)
    family = row.statement.family
    if str(owner.id) not in (explicit_owner_ids(row.statement) or []):
        family.identity = {**(family.identity or {}), "owner_ids": sorted({*(explicit_owner_ids(row.statement) or []), str(owner.id)})}
        if (family.label or LABEL) == LABEL:
            family.label = _family_label([owner_name(owner)])
        await db.flush()
    return family


async def _waiting_statement(db: Any, profile: Any, tracker: Tracker) -> Any:
    """A stored statement no family has claimed: one filled in before statements were versioned."""
    from app.models.financial_statement import FinancialStatement

    claimed = {str(_identity(item).get("statement_id")) for item in tracker.statements if _identity(item).get("statement_id")}
    rows = (
        await db.execute(
            select(FinancialStatement)
            .where(FinancialStatement.profile_id == profile.id)
            .order_by(FinancialStatement.created_at.desc())
        )
    ).scalars().all()
    return next((row for row in rows if str(row.id) not in claimed), None)


async def start_default(db: Any, profile: Any, *, actor: Any = None) -> Any:
    """The applicant's own statement, when a reader that names nobody opens the form for the first time.

    None when the file has no owner rows, or when an older stored statement is
    waiting to be adopted: the engine's own first-open path handles both.
    """
    if not isinstance(getattr(db, "info", None), dict):
        return None
    tracker = await load(db, profile, with_links=False)
    person, statement = default_statement(tracker)
    if statement is not None:
        return statement.family
    if person is None or await _waiting_statement(db, profile, tracker) is not None:
        return None
    return await _start(db, profile, person.owner, actor=actor)


async def link_target(db: Any, profile: Any, *, email: str | None = None, actor: Any = None) -> tuple[UUID | None, UUID | None]:
    """`(statement_id, family_id)` for a link that names no owner.

    The applicant's own statement, or the statement of the owner the link is
    being sent to; started when they have none and somebody else already does.
    `(None, None)` only when there is nothing on the file yet and no owner rows,
    so the caller keeps its older placeholder path.
    """
    from app.services import financial_workspace as workspace

    if not isinstance(getattr(db, "info", None), dict):
        return None, None
    await workspace.lock_financial_profile(db, profile)
    tracker = await load(db, profile, with_links=False)
    person, statement = default_statement(tracker, email=email)
    if statement is not None:
        pointer = _identity(statement).get("statement_id")
        return (UUID(str(pointer)) if pointer else None), statement.family.id
    waiting = await _waiting_statement(db, profile, tracker)
    if waiting is not None and (person is None or person is default_person(tracker)):
        # Filled in before statements were versioned: the engine adopts it on first open.
        return waiting.id, None
    if person is None:
        return None, None
    family = await _start(db, profile, person.owner, actor=actor)
    return None, family.id


async def person_for_family(db: Any, profile: Any, family_id: UUID) -> Any:
    """The owner a statement belongs to, for prefilling and naming their page. None when nobody is tied to it."""
    tracker = await load(db, profile, with_links=False)
    for row in tracker.rows:
        if any(statement.family.id == family_id for statement in row.statements):
            return row.owner
    return None


async def mint_link(db: Any, profile: Any, owner: Any, *, user: Any, label: str, invitee_email: str | None = None) -> tuple[Any, str]:
    """A private link to one owner's own statement. The token exists in readable form only here."""
    from app.services import financial_statements

    family = await statement_for(db, profile, owner, actor=user)
    pointer = (family.identity or {}).get("statement_id")
    link, token = await financial_statements.mint_link(
        db,
        profile,
        kind=KIND,
        statement_id=UUID(str(pointer)) if pointer else None,
        label=label[:120],
        invitee_email=invitee_email,
        created_by=getattr(user, "id", None),
    )
    link.financial_family_id = family.id
    # Sending a link is the desk asking for the statement: it asks an owner
    # under the line, and it takes back an earlier "not needed".
    _ask(family, owner, user=user)
    await db.flush()
    return link, token


def _ask(family: Any, owner: Any, *, user: Any) -> None:
    current = ((family.identity or {}).get("owner_requests") or {}).get(str(owner.id)) or {}
    if (owner_pct(owner) or Decimal(0)) >= THRESHOLD_PCT:
        if current:
            identity = dict(family.identity or {})
            identity["owner_requests"] = {key: value for key, value in (identity.get("owner_requests") or {}).items() if key != str(owner.id)}
            family.identity = identity
    elif current.get("state") != "requested":
        _record_request(family, owner, state="requested", reason=None, user=user)


def _record_request(family: Any, owner: Any, *, state: str, reason: str | None, user: Any) -> None:
    identity = dict(family.identity or {})
    requests = dict(identity.get("owner_requests") or {})
    requests[str(owner.id)] = {
        "state": state,
        "reason": (reason or "").strip() or None,
        "by": str(getattr(user, "id", "") or "") or None,
        "by_name": getattr(user, "name", None) or getattr(user, "email", None),
        "at": datetime.now(UTC).isoformat(),
    }
    identity["owner_requests"] = requests
    family.identity = identity


def compose_owner_email(
    *, first_name: str, business: str | None, url: str, expires_on: str | None, sender: str | None
) -> tuple[str, str]:
    """The subject and plain-text body. The link is the only credential in it."""
    whose = f" for {business}" if business else ""
    lines = [
        f"Hello {first_name}," if first_name else "Hello,",
        "",
        f"To keep the application{whose} moving, we need a personal financial statement from each owner. This one is yours.",
        "",
        "The quickest way is to fill it in online. You do not need to sign in:",
        url,
        "",
        "This link opens your own statement and nobody else's, so please do not forward it. Each owner receives their own."
        + (f" It works until {expires_on}." if expires_on else ""),
        "",
        "You can save and come back to it. If you already have a completed statement, "
        + (f"{sender} can add it to your file instead." if sender else "we can add it to your file instead."),
    ]
    if sender:
        lines += ["", "Thank you,", sender]
    subject = "Action needed: your personal financial statement" + (f" for {business}" if business else "")
    return subject[:200], "\n".join(lines)


def _long_date(value: Any) -> str | None:
    return f"{value:%B} {value.day}, {value:%Y}" if isinstance(value, datetime) else None


async def email_link(db: Any, profile: Any, owner: Any, *, user: Any, slot: Any = None) -> Any:
    """Email one owner the link to their own statement. Safe to click twice."""
    from app.config import get_settings
    from app.models.application_profile import ApplicationRoomDelivery
    from app.models.client import Client
    from app.services import application_profiles as profiles
    from app.services import file_events, financial_statements
    from app.services.application_programs import email_is_suppressed
    from app.services.email.user_mailer import send_as_user

    if getattr(profile, "primary_bucket_id", None) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This file has no document room to request into")
    recipient = profiles.normalized_email(getattr(owner, "email", None))
    if not recipient:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Add {owner_name(owner) or 'this owner'}'s personal email before sending their link")
    client = await db.get(Client, profile.client_id) if getattr(profile, "client_id", None) else None
    if client is not None and profiles.normalized_email(client.email) == recipient and await email_is_suppressed(db, profile.client_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Client opted out of automated email")

    now = datetime.now(UTC)
    recent = (
        await db.execute(
            select(ApplicationRoomDelivery)
            .where(
                ApplicationRoomDelivery.profile_id == profile.id,
                ApplicationRoomDelivery.action_kind == ACTION_KIND,
                ApplicationRoomDelivery.recipient_email == recipient,
                ApplicationRoomDelivery.status == "sent",
                ApplicationRoomDelivery.created_at >= now - DUPLICATE_WINDOW,
            )
            .order_by(ApplicationRoomDelivery.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if recent is not None:
        return recent

    link, token = await mint_link(db, profile, owner, user=user, label=f"Emailed to {owner_name(owner)}"[:120], invitee_email=recipient)
    business = (await financial_statements.form_prefill(db, profile)).get("business_name")
    sender = (getattr(user, "name", None) or "").strip() or None
    subject, body = compose_owner_email(
        first_name=str(getattr(owner, "first_name", "") or "").strip(),
        business=business,
        url=f"{get_settings().frontend_app_url.rstrip('/')}/forms/{KIND}/{token}",
        expires_on=_long_date(getattr(link, "expires_at", None)),
        sender=sender,
    )
    result = await send_as_user(db, user.id, to_emails=[recipient], subject=subject, body_text=body)
    delivery = ApplicationRoomDelivery(
        profile_id=profile.id,
        bucket_id=profile.primary_bucket_id,
        requested_document_id=getattr(slot, "id", None),
        action_kind=ACTION_KIND,
        channel="email",
        recipient_email=recipient,
        status="sent" if result.ok else "failed",
        detail=result.detail,
        provider_result={"accepted": result.ok, "message_id": result.message_id, "owner_id": str(owner.id)},
        created_by_user_id=user.id,
        initiation_source=INITIATION_SOURCE,
        idempotency_key=f"ownerpfs:{hashlib.sha256(f'{profile.id}:{owner.id}:{uuid4()}'.encode()).hexdigest()}",
        attempt_number=1,
    )
    db.add(delivery)
    await db.flush()
    name = owner_name(owner) or "an owner"
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "owner_statement.link_emailed" if result.ok else "owner_statement.link_email_failed",
        f"{LABEL}: emailed {name} the link to their own statement" if result.ok else f"{LABEL} for {name}: {result.detail}",
        target_type="owner",
        target_id=owner.id,
        metadata={"link_id": str(link.id), "delivery_id": str(delivery.id), "provider_accepted": result.ok},
    )
    if result.ok:
        await file_events.emit(
            db,
            profile=profile,
            kind="document.link_emailed",
            visibility=file_events.VISIBILITY_TEAM,
            title=f"Emailed {name} the link to their personal financial statement",
            actor=user,
            target_type="owner",
            target_id=owner.id,
            meta={"kind": KIND},
        )
    return delivery


async def assign(db: Any, profile: Any, family_id: UUID, owner_ids: list[UUID], *, user: Any, set_aside: bool = False) -> Any:
    """Say whose statement this is.

    Two owners make it a joint statement. No owners takes it away from whoever
    had it and leaves it waiting to be assigned; with `set_aside` it is recorded
    as no owner's on this file (a spouse's, say) and stops being listed.
    """
    from app.models.application_profile import ApplicationOwner
    from app.models.financial_form_version import FinancialFormFamily
    from app.models.financial_statement import FinancialStatement
    from app.services import application_profiles as profiles
    from app.services import financial_statements
    from app.services import financial_workspace as workspace

    await workspace.lock_financial_profile(db, profile)
    family = await db.get(FinancialFormFamily, family_id)
    if family is None or family.profile_id != profile.id or family.kind != KIND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Personal financial statement not found")
    owners = {row.id: row for row in await _owners(db, profile)}
    chosen = list(dict.fromkeys(owner_ids))
    if any(owner_id not in owners for owner_id in chosen):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found")
    names = [owner_name(owners[owner_id]) for owner_id in chosen]
    family.identity = {
        **{key: value for key, value in (family.identity or {}).items() if key != "set_aside"},
        "owner_ids": sorted(str(owner_id) for owner_id in chosen),
        **({"set_aside": True} if set_aside and not chosen else {}),
    }
    family.label = _family_label(names)
    pointer = (family.identity or {}).get("statement_id")
    statement = await db.get(FinancialStatement, UUID(str(pointer))) if pointer else None
    if statement is not None:
        people = [owners[owner_id] for owner_id in chosen]
        await financial_statements.link_owners(
            db,
            statement,
            application_owner_ids=[row.id for row in people if isinstance(row, ApplicationOwner)],
            dealer_owner_ids=[row.id for row in people if not isinstance(row, ApplicationOwner)],
        )
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "owner_statement.assigned",
        f"{LABEL}: set as {_names(names)}'s"
        if names
        else f"{LABEL}: set aside as no owner's on this file"
        if set_aside
        else f"{LABEL}: taken off its owner, waiting to be assigned",
        target_type="financial_form",
        target_id=family.id,
        metadata={"owner_ids": [str(owner_id) for owner_id in chosen], "set_aside": bool(set_aside and not chosen)},
    )
    return family


async def adopt_file(db: Any, profile: Any, file_id: UUID, owner: Any, *, user: Any) -> Any:
    """Put an uploaded statement that belongs to no statement into an owner's history."""
    from app.models.bucket import BucketFile, BucketFileAnalysis
    from app.models.financial_form_version import FinancialFormVersion
    from app.services import application_profiles as profiles
    from app.services import financial_workspace as workspace
    from app.services import sheet_layout

    file = await db.get(BucketFile, file_id)
    if file is None or file.bucket_id != profile.primary_bucket_id or file.deleted_at is not None or file.status != "uploaded":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found on this file")
    analysis = (
        await db.execute(
            select(BucketFileAnalysis)
            .where(BucketFileAnalysis.bucket_file_id == file.id, BucketFileAnalysis.status == "completed")
            .order_by(BucketFileAnalysis.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if analysis is None or analysis.classification != workspace.CLASSIFICATIONS[KIND]:
        raise HTTPException(status.HTTP_409_CONFLICT, "This document was not read as a personal financial statement")
    taken = (
        await db.execute(
            select(FinancialFormVersion.id).where(
                (FinancialFormVersion.source_file_id == file.id) | (FinancialFormVersion.pdf_file_id == file.id)
            ).limit(1)
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This document is already part of a statement. Assign that statement instead.")
    family = await statement_for(db, profile, owner, actor=user)
    numbers = (
        await db.execute(select(FinancialFormVersion.version_number).where(FinancialFormVersion.family_id == family.id))
    ).scalars().all()
    is_pdf = file.content_type == "application/pdf" or (file.file_name or "").lower().endswith(".pdf")
    version = FinancialFormVersion(
        id=uuid4(),
        family_id=family.id,
        version_number=max(numbers, default=0) + 1,
        schema_version=sheet_layout.SCHEMA_VERSIONS[KIND],
        body={},
        key_facts=(analysis.analysis or {}).get("key_facts") or {},
        body_sha256=workspace.body_hash({}),
        pdf_sha256=analysis.content_hash or file.content_hash,
        source="legacy_baseline" if analysis.provider == "drafted_form" else "original",
        status="original",
        parent_version_id=family.current_version_id,
        source_file_id=file.id,
        pdf_file_id=file.id if is_pdf else None,
        actor_name=file.uploaded_by_name,
        created_at=file.created_at or workspace.now(),
    )
    db.add(version)
    await db.flush()
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "owner_statement.file_assigned",
        f"{LABEL}: added an uploaded statement to {owner_name(owner)}'s history",
        target_type="owner",
        target_id=owner.id,
        metadata={"file_id": str(file.id), "family_id": str(family.id), "version_id": str(version.id)},
    )
    return family


async def set_requirement(db: Any, profile: Any, owner: Any, *, required: bool, reason: str | None, user: Any) -> None:
    """Ask an owner under the line for a statement, or record that one is not needed from this owner."""
    from app.services import application_profiles as profiles

    if not required and len((reason or "").strip()) < 8:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Say why a statement is not needed from this owner")
    family = await statement_for(db, profile, owner, actor=user)
    if required:
        # An owner at or over the line needs no request to be required: this takes them back to the rule.
        _ask(family, owner, user=user)
    else:
        _record_request(family, owner, state="not_needed", reason=reason, user=user)
    await db.flush()
    name = owner_name(owner) or "an owner"
    await profiles.log_profile_action(
        db,
        profile,
        user,
        "owner_statement.required" if required else "owner_statement.not_needed",
        f"{LABEL}: asked for one from {name}" if required else f"{LABEL}: not needed from {name}. {(reason or '').strip()}",
        target_type="owner",
        target_id=owner.id,
    )
