"""Attach credit pulls to the owners they belong to.

A soft pull lands on the client (``credit_pulls.client_id``); the file's
ownership step reads the owner row (``application_owners.credit_pull_id``).
Until the two point at each other the step stays pending however many pulls
the client ran. This module is the one place that joins them: the desk links
by hand from the file, the signature path links the signer, and in automatic
mode every completed pull is matched to the one owner on the same client who
carries its name, with a report of what connected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CreditPullStatus
from app.models.application_profile import ApplicationOwner, ApplicationProfile
from app.models.credit_pull import CreditPull

log = logging.getLogger(__name__)

MODE_MANUAL = "manual"
MODE_AUTOMATIC = "automatic"
MODES = (MODE_MANUAL, MODE_AUTOMATIC)

HOW_MANUAL = "manual"
HOW_AUTOMATIC = "automatic"
HOW_SIGNATURE = "signature"
HOW_SWEEP = "sweep"

LEVEL_EXACT = "exact"
LEVEL_LIKELY = "likely"
LEVEL_WEAK = "weak"
_LEVEL_RANK = {LEVEL_EXACT: 0, LEVEL_LIKELY: 1, LEVEL_WEAK: 2}

SETTINGS_KEY = "credit_linking"
SWEEP_MINUTES = 30


class LinkConflict(Exception):
    """The pull already belongs to an owner on this same file."""

    def __init__(self, owner_id: UUID | None, owner_name: str) -> None:
        super().__init__(f"This pull is already linked to {owner_name}")
        self.owner_id = owner_id
        self.owner_name = owner_name


# --- words and facts ----------------------------------------------------------


def normalize_name(first: str | None, last: str | None) -> str:
    return " ".join(f"{first or ''} {last or ''}".casefold().split())


def tier_for(score: int | None) -> str | None:
    if score is None:
        return None
    if score >= 720:
        return "Tier 1"
    if score >= 660:
        return "Tier 2"
    return "Tier 3"


def _status_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def is_completed(pull: Any) -> bool:
    return _status_value(getattr(pull, "status", None)) == CreditPullStatus.COMPLETED.value


def pull_facts(pull: Any) -> dict[str, Any]:
    """What the desk needs to know about a pull before trusting its number."""

    from app.services.credit_pull_core import credit_status_extras

    extras = credit_status_extras(pull)
    expires = getattr(pull, "expires_at", None)
    pulled = getattr(pull, "pulled_at", None)
    return {
        "pull_id": str(pull.id),
        "status": _status_value(getattr(pull, "status", None)),
        "score": getattr(pull, "fico", None),
        "fico_source": extras["fico_source"],
        "score_verified": extras["score_verified"],
        "report_available": extras["report_available"],
        "attention": extras["attention"],
        "report_import_status": (getattr(pull, "bureau_response", None) or {}).get("report_import_status"),
        "pulled_at": pulled.isoformat() if pulled else None,
        "expires_at": expires.isoformat() if expires else None,
        "expired": bool(expires and expires < datetime.now(UTC)),
    }


def score_words(facts: dict[str, Any]) -> str:
    score = facts.get("score")
    if score is None:
        return "no score returned"
    if facts.get("fico_source") == "intelligence-proxy":
        return f"{score} estimated from the identity check, not a bureau score"
    return f"score {score}"


def _date_words(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return f"{value:%b} {value.day}, {value:%Y}"
    return "an unknown date"


def match_evidence(owner: Any, pull: Any, *, same_client: bool) -> dict[str, Any]:
    owner_name = normalize_name(owner.first_name, owner.last_name)
    pull_name = normalize_name(
        getattr(pull, "legal_first_name", None), getattr(pull, "legal_last_name", None)
    )
    owner_dob = getattr(owner, "dob", None)
    pull_dob = getattr(pull, "dob", None)
    owner_email = (getattr(owner, "email", None) or "").strip().casefold()
    pull_email = (getattr(pull, "email", None) or "").strip().casefold()
    return {
        "name_match": bool(owner_name) and owner_name == pull_name,
        "dob_match": (owner_dob == pull_dob) if owner_dob and pull_dob else None,
        "email_match": bool(owner_email and pull_email and owner_email == pull_email),
        "same_client": bool(same_client),
    }


def confidence(evidence: dict[str, Any]) -> str | None:
    """How sure the match is, or None when the pull is clearly someone else."""

    if evidence.get("dob_match") is False:
        return None
    if evidence.get("name_match"):
        if evidence.get("dob_match") or evidence.get("email_match") or evidence.get("same_client"):
            return LEVEL_EXACT
        return LEVEL_LIKELY
    if evidence.get("same_client"):
        return LEVEL_WEAK
    return None


def confidence_words(level: str, evidence: dict[str, Any]) -> str:
    if level == LEVEL_EXACT:
        if evidence.get("dob_match"):
            return "Name and date of birth match"
        if evidence.get("email_match"):
            return "Name and email match"
        return "Name matches on this client"
    if level == LEVEL_LIKELY:
        return "Name matches on another client; confirm it is the same person"
    return "On this client, but the name differs"


@dataclass
class Candidate:
    pull: Any
    evidence: dict[str, Any]
    level: str
    linked_here: bool
    linked_elsewhere: bool

    def read(self) -> dict[str, Any]:
        return {
            **pull_facts(self.pull),
            "first_name": getattr(self.pull, "legal_first_name", None),
            "last_name": getattr(self.pull, "legal_last_name", None),
            "email": getattr(self.pull, "email", None),
            "dob_on_file": bool(getattr(self.pull, "dob", None)),
            "client_id": str(self.pull.client_id)
            if getattr(self.pull, "client_id", None)
            else None,
            "evidence": self.evidence,
            "confidence": self.level,
            "why": confidence_words(self.level, self.evidence),
            "linked_to_this_owner": self.linked_here,
            "linked_elsewhere": self.linked_elsewhere,
        }


def _sort_key(candidate: Candidate) -> tuple:
    pulled = getattr(candidate.pull, "pulled_at", None)
    stamp = pulled.timestamp() if isinstance(pulled, datetime) else 0.0
    return (not candidate.linked_here, _LEVEL_RANK.get(candidate.level, 9), -stamp)


# --- finding candidates -------------------------------------------------------


def _name_predicate(first: str | None, last: str | None):
    first = (first or "").strip().lower()
    last = (last or "").strip().lower()
    if not first or not last:
        return None
    return and_(
        func.lower(func.trim(CreditPull.legal_first_name)) == first,
        func.lower(func.trim(CreditPull.legal_last_name)) == last,
    )


async def candidate_pulls(db: AsyncSession, profile: Any, owner: Any) -> list[Candidate]:
    """Completed pulls that could be this owner's: on the file's client, or
    anywhere under the owner's exact name. Sorted most trustworthy first."""

    conditions = []
    if getattr(profile, "client_id", None):
        conditions.append(CreditPull.client_id == profile.client_id)
    by_name = _name_predicate(owner.first_name, owner.last_name)
    if by_name is not None:
        conditions.append(by_name)
    if not conditions:
        return []
    rows = (
        (
            await db.execute(
                select(CreditPull)
                .where(or_(*conditions), CreditPull.status == CreditPullStatus.COMPLETED.value)
                .order_by(CreditPull.pulled_at.desc().nulls_last(), CreditPull.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    out: list[Candidate] = []
    for pull in rows:
        same_client = (
            bool(getattr(profile, "client_id", None)) and pull.client_id == profile.client_id
        )
        evidence = match_evidence(owner, pull, same_client=same_client)
        level = confidence(evidence)
        linked_here = pull.application_owner_id == owner.id
        if level is None and not linked_here:
            continue
        out.append(
            Candidate(
                pull=pull,
                evidence=evidence,
                level=level or LEVEL_WEAK,
                linked_here=linked_here,
                linked_elsewhere=pull.application_owner_id not in (None, owner.id),
            )
        )
    out.sort(key=_sort_key)
    return out


async def _owners_for_pull(db: AsyncSession, pull: Any) -> list[tuple[Any, Any]]:
    """Active owners on the client's own files who carry the pull's name and
    have no pull yet. Dealer-backed files keep their own credit flow."""

    by_name = _name_predicate(
        getattr(pull, "legal_first_name", None), getattr(pull, "legal_last_name", None)
    )
    if by_name is None or not getattr(pull, "client_id", None):
        return []
    first = (pull.legal_first_name or "").strip().lower()
    last = (pull.legal_last_name or "").strip().lower()
    rows = (
        await db.execute(
            select(ApplicationOwner, ApplicationProfile)
            .join(ApplicationProfile, ApplicationProfile.id == ApplicationOwner.profile_id)
            .where(
                ApplicationProfile.client_id == pull.client_id,
                ApplicationProfile.dealer_id.is_(None),
                ApplicationOwner.archived_at.is_(None),
                ApplicationOwner.credit_pull_id.is_(None),
                func.lower(func.trim(ApplicationOwner.first_name)) == first,
                func.lower(func.trim(ApplicationOwner.last_name)) == last,
            )
        )
    ).all()
    matched = []
    for owner, profile in rows:
        evidence = match_evidence(owner, pull, same_client=True)
        if confidence(evidence) == LEVEL_EXACT:
            matched.append((owner, profile))
    return matched


# --- linking ------------------------------------------------------------------


def link_words(owner: Any, facts: dict[str, Any], how: str) -> str:
    lead = {
        HOW_MANUAL: "Linked by hand",
        HOW_AUTOMATIC: "Linked automatically",
        HOW_SIGNATURE: "Linked from the signed authorization",
        HOW_SWEEP: "Linked by the desk's match",
    }.get(how, "Linked")
    return f"{lead}: pull from {_date_words(facts.get('pulled_at'))}, {score_words(facts)}, to {owner.full_name}"


async def _record(
    db: AsyncSession,
    profile: Any,
    owner: Any,
    *,
    actor: Any,
    action: str,
    kind: str,
    title: str,
    detail: str,
    meta: dict[str, Any],
) -> None:
    from app.services import application_profiles as profiles
    from app.services import file_events

    await profiles.log_profile_action(
        db, profile, actor, action, detail, target_type="owner", target_id=owner.id, metadata=meta
    )
    await file_events.emit(
        db,
        profile=profile,
        kind=kind,
        visibility=file_events.VISIBILITY_DESK,
        title=title,
        body=detail,
        actor=actor,
        target_type="owner",
        target_id=owner.id,
        meta=meta,
    )


async def _progress(db: AsyncSession, profile: Any) -> None:
    try:
        from app.dealer_os.services import application_precall

        await application_precall.on_progress(db, profile, commit=False)
    except Exception:  # noqa: BLE001 - the checklist tick never blocks the link
        log.exception(
            "credit_links: pre-call progress failed profile=%s", getattr(profile, "id", None)
        )


async def link_pull(
    db: AsyncSession,
    profile: Any,
    owner: Any,
    pull: Any,
    *,
    actor: Any = None,
    how: str = HOW_MANUAL,
) -> dict[str, Any]:
    """Point the owner at the pull and the pull at the owner.

    One pull may serve the same person on several of the client's files, so
    the pull keeps its first owner and only a second owner on the *same* file
    is refused. Identity fields the owner row lacks are filled from the pull.
    """

    if pull.application_owner_id not in (None, owner.id):
        other = await db.get(ApplicationOwner, pull.application_owner_id)
        if other is not None and other.profile_id == owner.profile_id:
            raise LinkConflict(other.id, other.full_name)
    previous = getattr(owner, "credit_pull_id", None)
    if previous and previous != pull.id:
        old = await db.get(CreditPull, previous)
        if old is not None and old.application_owner_id == owner.id:
            old.application_owner_id = None
    from app.services.credit_report_import import ensure_report_imported

    await ensure_report_imported(db, pull)
    now = datetime.now(UTC)
    facts = pull_facts(pull)
    if pull.application_owner_id is None:
        pull.application_owner_id = owner.id
    owner.credit_pull_id = pull.id
    owner.credit_score = pull.fico
    owner.credit_tier = tier_for(pull.fico)
    owner.credit_pulled_at = pull.pulled_at or now
    owner.credit_summary = {
        **facts,
        "linked_how": how,
        "linked_at": now.isoformat(),
        "linked_by": str(actor.id) if actor is not None and getattr(actor, "id", None) else None,
        "report_import_status": (pull.bureau_response or {}).get("report_import_status"),
    }
    for field_name in ("dob", "street", "city", "state", "zip"):
        if not getattr(owner, field_name, None) and getattr(pull, field_name, None):
            setattr(owner, field_name, getattr(pull, field_name))
    await db.flush()
    words = link_words(owner, facts, how)
    meta = {
        "pull_id": str(pull.id),
        "how": how,
        "score": pull.fico,
        "fico_source": facts["fico_source"],
    }
    await _record(
        db,
        profile,
        owner,
        actor=actor,
        action="owner.credit_linked",
        kind="credit.linked",
        title=f"Credit pull linked to {owner.full_name}",
        detail=words,
        meta=meta,
    )
    await _progress(db, profile)
    return {
        "owner_id": str(owner.id),
        "owner_name": owner.full_name,
        "profile_id": str(profile.id),
        "intake_id": str(profile.intake_id) if getattr(profile, "intake_id", None) else None,
        "how": how,
        "words": words,
        **facts,
    }


async def unlink_pull(db: AsyncSession, profile: Any, owner: Any, *, actor: Any = None) -> None:
    pull = (
        await db.get(CreditPull, owner.credit_pull_id)
        if getattr(owner, "credit_pull_id", None)
        else None
    )
    if pull is not None and pull.application_owner_id == owner.id:
        pull.application_owner_id = None
    owner.credit_pull_id = None
    owner.credit_score = None
    owner.credit_tier = None
    owner.credit_pulled_at = None
    owner.credit_summary = None
    await db.flush()
    await _record(
        db,
        profile,
        owner,
        actor=actor,
        action="owner.credit_unlinked",
        kind="credit.unlinked",
        title=f"Credit pull unlinked from {owner.full_name}",
        detail=f"The pull no longer counts for {owner.full_name}; the owner's credit step is pending again",
        meta={"pull_id": str(pull.id) if pull is not None else None},
    )


# --- automatic linking --------------------------------------------------------


async def auto_link_profile(
    db: AsyncSession, profile: Any, *, actor: Any = None, how: str = HOW_AUTOMATIC
) -> dict[str, Any]:
    """Link every owner on this file whose match is exact; say who still waits."""

    owners = (
        (
            await db.execute(
                select(ApplicationOwner).where(
                    ApplicationOwner.profile_id == profile.id,
                    ApplicationOwner.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    linked: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for owner in owners:
        if owner.credit_pull_id is not None:
            continue
        candidates = await candidate_pulls(db, profile, owner)
        exact = [c for c in candidates if c.level == LEVEL_EXACT and not c.linked_elsewhere]
        if exact:
            try:
                linked.append(
                    await link_pull(db, profile, owner, exact[0].pull, actor=actor, how=how)
                )
            except LinkConflict as conflict:
                waiting.append(
                    {
                        "owner_id": str(owner.id),
                        "owner_name": owner.full_name,
                        "reason": str(conflict),
                    }
                )
            continue
        if candidates:
            reason = "Only a name match on another client or a different name on this client; link it by hand if it is the same person"
        else:
            reason = "No completed pull carries this owner's name"
        waiting.append({"owner_id": str(owner.id), "owner_name": owner.full_name, "reason": reason})
    return {"profile_id": str(profile.id), "linked": linked, "waiting": waiting}


async def link_fresh_pull(
    db: AsyncSession, pull: Any, *, actor: Any = None, how: str = HOW_AUTOMATIC
) -> tuple[list[dict[str, Any]], str | None]:
    """Attach one unlinked pull to the single owner it matches, or say why not."""

    matches = await _owners_for_pull(db, pull)
    if len(matches) == 1:
        owner, profile = matches[0]
        return [await link_pull(db, profile, owner, pull, actor=actor, how=how)], None
    if not matches:
        return [], "No owner with this name is waiting on the client's files"
    return [], f"{len(matches)} owners carry this name on the client's files; link by hand"


async def run_everywhere(
    db: AsyncSession, *, actor: Any = None, how: str = HOW_AUTOMATIC
) -> dict[str, Any]:
    """Sweep every completed pull nobody owns yet and report what connected."""

    pulls = (
        (
            await db.execute(
                select(CreditPull)
                .where(
                    CreditPull.status == CreditPullStatus.COMPLETED.value,
                    CreditPull.application_owner_id.is_(None),
                )
                .order_by(CreditPull.pulled_at.asc().nulls_last(), CreditPull.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    linked: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for pull in pulls:
        done, reason = await link_fresh_pull(db, pull, actor=actor, how=how)
        if done:
            linked.extend(done)
            continue
        facts = pull_facts(pull)
        unmatched.append(
            {
                "pull_id": str(pull.id),
                "name": f"{pull.legal_first_name or ''} {pull.legal_last_name or ''}".strip()
                or "Unnamed",
                "client_id": str(pull.client_id) if pull.client_id else None,
                "pulled_at": facts["pulled_at"],
                "score_words": score_words(facts),
                "reason": reason,
            }
        )
    return {
        "at": datetime.now(UTC).isoformat(),
        "how": how,
        "pulls_checked": len(pulls),
        "linked": linked,
        "unmatched": unmatched,
    }


def report_words(report: dict[str, Any]) -> str:
    linked = report.get("linked") or []
    unmatched = report.get("unmatched") or []
    parts = [f"{len(linked)} pull{'' if len(linked) == 1 else 's'} connected"]
    if unmatched:
        parts.append(f"{len(unmatched)} still unmatched")
    checked = report.get("pulls_checked")
    if checked is not None:
        parts.append(f"{checked} checked")
    return ", ".join(parts)


# --- mode, memory and the hook ------------------------------------------------


async def _settings_row(db: AsyncSession, *, lock: bool = False):
    from app.models.app_settings import AppSettings
    from app.services.app_settings_lock import lock_app_settings

    if lock:
        await lock_app_settings(db)
    statement = select(AppSettings).limit(1)
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


def _section(row: Any) -> dict[str, Any]:
    data = getattr(row, "data", None) if row is not None else None
    section = (data or {}).get(SETTINGS_KEY) if isinstance(data, dict) else None
    return dict(section) if isinstance(section, dict) else {}


async def mode(db: AsyncSession) -> str:
    value = _section(await _settings_row(db)).get("mode")
    return value if value in MODES else MODE_MANUAL


async def read_settings(db: AsyncSession) -> dict[str, Any]:
    section = _section(await _settings_row(db))
    return {
        "mode": section.get("mode") if section.get("mode") in MODES else MODE_MANUAL,
        "last_run": section.get("last_run"),
        "sweep_minutes": SWEEP_MINUTES,
    }


async def write_section(db: AsyncSession, **changes: Any) -> dict[str, Any]:
    """Merge into the credit_linking section under the settings lock.

    The settings row is created on first use the way the settings router does
    it; the JSON is reassigned whole so SQLAlchemy sees the change.
    """
    from app.models.app_settings import AppSettings

    row = await _settings_row(db, lock=True)
    if row is None:
        row = AppSettings(singleton=True, data={})
        db.add(row)
        await db.flush()
    data = dict(row.data or {})
    section = dict(data.get(SETTINGS_KEY) or {})
    section.update(changes)
    data[SETTINGS_KEY] = section
    row.data = data
    await db.flush()
    return section


async def remember_run(db: AsyncSession, report: dict[str, Any]) -> None:
    await write_section(db, last_run=report)


async def notify_desk(db: AsyncSession, report: dict[str, Any]) -> None:
    """Tell the super admins what the sweep connected, one notice per run."""

    from app.enums import Role
    from app.services.notifications import notify_users, users_with_roles

    linked = report.get("linked") or []
    if not linked:
        return
    admins = await users_with_roles(db, Role.SUPER_ADMIN)
    lines = [f"{entry['owner_name']}: {score_words(entry)}" for entry in linked[:12]]
    if len(linked) > 12:
        lines.append(f"and {len(linked) - 12} more")
    await notify_users(
        db,
        recipient_ids={user.id for user in admins},
        event_type="credit.links_connected",
        title=f"{len(linked)} credit pull{'' if len(linked) == 1 else 's'} connected to file owners",
        body="\n".join(lines),
        category="system",
        deep_link="/settings?section=credit_linking",
        meta={
            "report_at": report.get("at"),
            "linked": len(linked),
            "unmatched": len(report.get("unmatched") or []),
        },
    )


async def after_pull(db: AsyncSession, pull: Any) -> list[dict[str, Any]]:
    """Automatic mode: attach a pull to its owner the moment it completes.

    Manual mode leaves the pull for the desk to link from the file.
    """

    if _status_value(getattr(pull, "status", None)) != CreditPullStatus.COMPLETED.value:
        return []
    if await mode(db) != MODE_AUTOMATIC:
        return []
    linked, _reason = await link_fresh_pull(db, pull, how=HOW_AUTOMATIC)
    return linked


async def run_scheduled_sweep() -> dict[str, Any] | None:
    """The scheduler's tick: in automatic mode, connect what is waiting and
    tell the desk. Opens its own session like every other job."""

    from app.db import SessionLocal

    async with SessionLocal() as db:
        if await mode(db) != MODE_AUTOMATIC:
            return None
        report = await run_everywhere(db, how=HOW_AUTOMATIC)
        if report["linked"]:
            await remember_run(db, report)
            await notify_desk(db, report)
            await db.commit()
            log.info("credit_links sweep: %s", report_words(report))
        return report
