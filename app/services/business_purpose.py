"""Business purpose only: the hold for owner-occupied personal-use requests.

QC funds business-purpose loans (Franco, 2026-11-16). A primary residence or
second home can secure one only when the borrower certifies the money is for
business or investment use, like a HELOC on the owner's home that funds their
company. When the borrower says the loan is for personal use on a home they
live in, the file is held: the AI stops collecting and quotes nothing, and a
person decides what happens next. Nothing is sent to the client automatically,
because a decline is a person's call.

How strong the evidence is decides how hard the stop is:

- ``held``: the borrower picked it on the start form, or the desk confirmed it.
- ``needs_review``: the AI read it from a chat message. The desk confirms it
  (becomes ``held``) or clears it. A later business-purpose answer clears an
  AI reading on its own; it never clears the borrower's own form answer,
  which only a person can overturn.
- ``cleared``: a person (or the borrower's later business-purpose answer to
  an AI reading) settled it. Kept so the history stays readable.

The review lives on ``intake_state["personal_use_review"]``, beside the
answers it is about (``funding_review_details.occupancy`` and
``business_purpose``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import real_estate_vocabulary as vocab

log = logging.getLogger(__name__)

REVIEW_KEY = "personal_use_review"
HELD = "held"
NEEDS_REVIEW = "needs_review"
CLEARED = "cleared"
OPEN_STATUSES = frozenset({HELD, NEEDS_REVIEW})

#: Where an answer came from. The form and the desk are the borrower's or a
#: person's own words; the chat is the AI's reading of them.
SOURCE_FORM = "application_form"
SOURCE_DESK = "desk"
SOURCE_CHAT = "chat"
_FIRM_SOURCES = frozenset({SOURCE_FORM, SOURCE_DESK})
_HISTORY_LIMIT = 20


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _state(intake: Any) -> dict[str, Any]:
    state = getattr(intake, "intake_state", None)
    return dict(state) if isinstance(state, dict) else {}


def details(intake: Any) -> dict[str, Any]:
    raw = _state(intake).get("funding_review_details")
    return raw if isinstance(raw, dict) else {}


def review(intake: Any) -> dict[str, Any] | None:
    raw = _state(intake).get(REVIEW_KEY)
    return raw if isinstance(raw, dict) else None


def is_open(intake: Any) -> bool:
    current = review(intake)
    return bool(current and current.get("status") in OPEN_STATUSES)


def is_held(intake: Any) -> bool:
    current = review(intake)
    return bool(current and current.get("status") == HELD)


def says_personal_use(answers: dict[str, Any]) -> bool:
    """Owner-occupied collateral and a stated personal purpose."""
    return vocab.is_owner_occupied(answers.get("occupancy")) and answers.get("business_purpose") is False


def says_business_purpose(answers: dict[str, Any]) -> bool:
    return answers.get("business_purpose") is True or (
        answers.get("occupancy") is not None and not vocab.is_owner_occupied(answers.get("occupancy"))
    )


def _write(intake: Any, record: dict[str, Any]) -> dict[str, Any]:
    state = _state(intake)
    previous = state.get(REVIEW_KEY) if isinstance(state.get(REVIEW_KEY), dict) else {}
    history = list(previous.get("history") or [])[-(_HISTORY_LIMIT - 1):] if previous else []
    if previous:
        history.append({key: previous.get(key) for key in ("status", "source", "at", "decided_by", "note")})
    stored = {**record, "history": history}
    state[REVIEW_KEY] = stored
    intake.intake_state = state
    return stored


def _who(intake: Any) -> str:
    return getattr(intake, "business_name", None) or getattr(intake, "full_name", None) or "a real-estate file"


async def _tell_desk(db: AsyncSession, intake: Any, *, title: str, body: str, status: str) -> None:
    """A desk notification plus a desk-only timeline line. Never raises."""
    try:
        from app.enums import Role
        from app.models.file_event import VISIBILITY_DESK
        from app.services import file_events
        from app.services.notifications import notify_users, users_with_roles

        admins = await users_with_roles(db, Role.SUPER_ADMIN)
        recipients = {user.id for user in admins}
        if recipients:
            await notify_users(
                db,
                recipient_ids=recipients,
                event_type="personal_use_review",
                category="underwriting",
                priority="high",
                title=title,
                body=body,
                target_type="intake",
                target_id=str(intake.id),
                deep_link=f"/files/{intake.id}",
                batch_key=f"personal_use_review:{intake.id}",
            )
        await file_events.emit(
            db,
            kind="intake.personal_use_review",
            visibility=VISIBILITY_DESK,
            title=title,
            body=body,
            intake_id=intake.id,
            bucket_id=getattr(intake, "bucket_id", None),
            actor_label="Qualified Commercial",
            target_type="intake",
            target_id=intake.id,
            meta={"status": status},
            already_notified=recipients,
        )
    except Exception:  # noqa: BLE001 - the answer is recorded whatever the notice does
        log.exception("personal-use review notice failed for intake %s", getattr(intake, "id", None))


async def after_answers(
    db: AsyncSession,
    intake: Any,
    *,
    source: str,
    answers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Re-read the file's occupancy and purpose after new answers arrive.

    ``answers`` are the ones just given (defaults to everything on file);
    ``source`` says whose words they are. Returns the review when it changed.
    """
    current = review(intake)
    status = current.get("status") if current else None
    given = answers if answers is not None else details(intake)
    merged = {**details(intake), **(answers or {})}

    if says_personal_use(merged):
        wanted = HELD if source in _FIRM_SOURCES else NEEDS_REVIEW
        if status == HELD or status == wanted:
            return None
        record = _write(
            intake,
            {
                "status": wanted,
                "source": source,
                "occupancy": vocab.occupancy_of(merged.get("occupancy")),
                "at": _now(),
                "note": (
                    "The borrower chose personal use on their own home."
                    if wanted == HELD
                    else "The AI read personal use on the borrower's own home from the chat. Confirm it with the borrower or clear it."
                ),
            },
        )
        occupancy_label = vocab.OCCUPANCY_LABELS.get(record["occupancy"] or "", "their own home")
        await _tell_desk(
            db,
            intake,
            title=(
                f"Personal-use request held: {_who(intake)}"
                if wanted == HELD
                else f"Possible personal-use request: {_who(intake)}"
            ),
            body=(
                f"The borrower describes a personal-use loan on their {occupancy_label.lower()}. "
                "QC funds business-purpose loans only, so the AI has stopped collecting and quotes nothing. "
                "A person decides what happens next; nothing has been sent to the client."
                if wanted == HELD
                else f"The chat suggests a personal-use loan on the borrower's {occupancy_label.lower()}. "
                "Confirm it with the borrower (hold the file) or clear it if the loan is for business use."
            ),
            status=wanted,
        )
        return record

    if status in OPEN_STATUSES and says_business_purpose(given if answers is not None else merged):
        if status == NEEDS_REVIEW and source in {SOURCE_CHAT, SOURCE_FORM}:
            record = _write(
                intake,
                {
                    "status": CLEARED,
                    "source": source,
                    "at": _now(),
                    "note": "The borrower's later answer says business or investment use.",
                },
            )
            await _tell_desk(
                db,
                intake,
                title=f"Personal-use flag cleared: {_who(intake)}",
                body="The borrower's later answer says the loan is for business or investment use.",
                status=CLEARED,
            )
            return record
        if status == HELD and source == SOURCE_CHAT:
            record = _write(
                intake,
                {
                    "status": NEEDS_REVIEW,
                    "source": source,
                    "occupancy": (current or {}).get("occupancy"),
                    "at": _now(),
                    "note": "The borrower chose personal use, then said business use in the chat. Confirm which it is.",
                },
            )
            await _tell_desk(
                db,
                intake,
                title=f"Personal-use answer changed: {_who(intake)}",
                body="The borrower chose personal use on the start form and now says business use in the chat. Confirm which it is and clear or keep the hold.",
                status=NEEDS_REVIEW,
            )
            return record
    return None


async def resolve(
    db: AsyncSession,
    intake: Any,
    *,
    decision: str,
    actor: Any,
    note: str | None = None,
) -> dict[str, Any]:
    """The desk's decision: ``hold`` confirms personal use, ``clear`` records business use."""
    if decision not in {"hold", "clear"}:
        raise ValueError("decision must be hold or clear")
    actor_name = getattr(actor, "name", None) or getattr(actor, "email", None) or "Desk"
    state = _state(intake)
    current_details = dict(state.get("funding_review_details") or {}) if isinstance(state.get("funding_review_details"), dict) else {}
    sources = dict(current_details.get("sources") or {}) if isinstance(current_details.get("sources"), dict) else {}
    if decision == "clear":
        current_details["business_purpose"] = True
        sources["business_purpose"] = SOURCE_DESK
    else:
        current_details["business_purpose"] = False
        sources["business_purpose"] = SOURCE_DESK
    current_details["sources"] = sources
    state["funding_review_details"] = current_details
    intake.intake_state = state
    record = _write(
        intake,
        {
            "status": CLEARED if decision == "clear" else HELD,
            "source": SOURCE_DESK,
            "occupancy": vocab.occupancy_of(current_details.get("occupancy")),
            "at": _now(),
            "decided_by": str(getattr(actor, "id", "") or "") or None,
            "decided_by_name": actor_name,
            "note": (note or "").strip()[:500]
            or ("Cleared by the desk: business or investment use." if decision == "clear" else "Held by the desk: personal use confirmed."),
        },
    )
    try:
        from app.models.file_event import VISIBILITY_DESK
        from app.services import file_events

        await file_events.emit(
            db,
            kind="intake.personal_use_review",
            visibility=VISIBILITY_DESK,
            title=("Personal-use flag cleared" if decision == "clear" else "Personal-use hold confirmed") + f" by {actor_name}",
            body=record["note"],
            intake_id=intake.id,
            bucket_id=getattr(intake, "bucket_id", None),
            actor=actor,
            target_type="intake",
            target_id=intake.id,
            meta={"status": record["status"]},
        )
    except Exception:  # noqa: BLE001
        log.exception("personal-use decision event failed for intake %s", getattr(intake, "id", None))
    return record


def client_context(intake: Any) -> dict[str, Any] | None:
    """What the AI is told: only whether to stop, never who decided or why."""
    current = review(intake)
    if not current or current.get("status") not in OPEN_STATUSES:
        return None
    return {
        "status": current.get("status"),
        "occupancy": current.get("occupancy"),
        "instruction": (
            "Qualified Commercial funds business-purpose loans only. The borrower describes a personal-use loan on "
            "their own home. Say plainly that QC lends for business or investment purposes and that a team member "
            "will follow up. Do not collect more documents, do not discuss terms, rates or eligibility, and do not "
            "promise or deny anything."
            if current.get("status") == HELD
            else "The borrower may be describing a personal-use loan on their own home. Ask one direct question: is "
            "this loan for a business or investment purpose? Do not discuss terms, rates or eligibility until it is answered."
        ),
    }


def desk_read(intake: Any) -> dict[str, Any] | None:
    """The review as the desk sees it (status, source, note, who decided, when)."""
    current = review(intake)
    if not current:
        return None
    return {
        key: current.get(key)
        for key in ("status", "source", "occupancy", "at", "note", "decided_by_name")
    }
