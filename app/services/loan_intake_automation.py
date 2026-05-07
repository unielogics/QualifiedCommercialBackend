"""Per-loan-type doc collection + reminder + escalation pipeline.

This is what wakes up when a Loan is created. It reads the operator's
checklist for the loan type (`AppSettings.checklists[loan.type]`),
auto-creates `Document(status=REQUESTED)` rows, and emits a
`document_due` calendar event for each. The scheduler's daily
`job_doc_reminders` then walks outstanding docs and escalates by tier.

Public surface:
  kickoff_loan(db, loan, settings)        — call once on Loan create
  evaluate_doc_reminders()                 — daily scheduler job

The whole thing leans hard on idempotency: kickoff is keyed by
`(loan_id, document.name)` so re-runs (e.g. seed re-runs, manual
trigger) never duplicate. Reminder escalation is keyed by Activity
log presence so the same doc can't be reminded twice at the same
tier.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.enums import (
    AITaskPriority,
    AITaskSource,
    AITaskStatus,
    DealChatRole,
    DocStatus,
)
from app.models.activity import Activity
from app.models.ai_task import AITask
from app.models.app_settings import AppSettings
from app.models.document import Document
from app.models.loan import Loan
from app.models.loan_chat_message import LoanChatMessage
from app.schemas.settings import (
    AppSettingsData,
    DocChecklistItem,
    LoanTypeChecklist,
)
from app.services import calendar_emitter

log = logging.getLogger(__name__)


# Sane defaults when AppSettings.checklists has no entry for a loan
# type. Operators can override per-loan-type from the Settings UI;
# until they do, we don't want intake to silently skip doc collection.
_DEFAULT_DOCS_BY_TYPE: dict[str, list[DocChecklistItem]] = {
    "dscr": [
        DocChecklistItem(name="Driver's License",         required=True, auto_request=True),
        DocChecklistItem(name="Operating Agreement",      required=True, auto_request=True),
        DocChecklistItem(name="EIN Letter (CP-575)",      required=True, auto_request=True),
        DocChecklistItem(name="Bank Statements (2 mo)",   required=True, auto_request=True),
        DocChecklistItem(name="Lease(s) / Rent Roll",     required=True, auto_request=True),
        DocChecklistItem(name="Insurance Binder",         required=True, auto_request=True),
        DocChecklistItem(name="Property Tax Statement",   required=True, auto_request=True),
    ],
    "fix_and_flip": [
        DocChecklistItem(name="Driver's License",         required=True, auto_request=True),
        DocChecklistItem(name="Operating Agreement",      required=True, auto_request=True),
        DocChecklistItem(name="EIN Letter (CP-575)",      required=True, auto_request=True),
        DocChecklistItem(name="Bank Statements (2 mo)",   required=True, auto_request=True),
        DocChecklistItem(name="Construction Budget / SOW", required=True, auto_request=True),
        DocChecklistItem(name="Schedule of Real Estate Owned", required=True, auto_request=True),
        DocChecklistItem(name="Contractor Bid",           required=True, auto_request=True),
    ],
    "ground_up": [
        DocChecklistItem(name="Driver's License",         required=True, auto_request=True),
        DocChecklistItem(name="Operating Agreement",      required=True, auto_request=True),
        DocChecklistItem(name="EIN Letter (CP-575)",      required=True, auto_request=True),
        DocChecklistItem(name="Bank Statements (2 mo)",   required=True, auto_request=True),
        DocChecklistItem(name="Plans & Permits",          required=True, auto_request=True),
        DocChecklistItem(name="Construction Budget",      required=True, auto_request=True),
        DocChecklistItem(name="Builder's Risk Insurance", required=True, auto_request=True),
    ],
    "bridge": [
        DocChecklistItem(name="Driver's License",         required=True, auto_request=True),
        DocChecklistItem(name="Operating Agreement",      required=True, auto_request=True),
        DocChecklistItem(name="EIN Letter (CP-575)",      required=True, auto_request=True),
        DocChecklistItem(name="Bank Statements (2 mo)",   required=True, auto_request=True),
        DocChecklistItem(name="Exit Strategy",            required=True, auto_request=True),
        DocChecklistItem(name="Insurance Binder",         required=True, auto_request=True),
    ],
    "portfolio": [
        DocChecklistItem(name="Driver's License",         required=True, auto_request=True),
        DocChecklistItem(name="Operating Agreement",      required=True, auto_request=True),
        DocChecklistItem(name="EIN Letter (CP-575)",      required=True, auto_request=True),
        DocChecklistItem(name="Bank Statements (2 mo)",   required=True, auto_request=True),
        DocChecklistItem(name="Schedule of Real Estate Owned", required=True, auto_request=True),
        DocChecklistItem(name="Rent Rolls (all properties)", required=True, auto_request=True),
    ],
    "cash_out_refi": [
        DocChecklistItem(name="Driver's License",         required=True, auto_request=True),
        DocChecklistItem(name="Operating Agreement",      required=True, auto_request=True),
        DocChecklistItem(name="EIN Letter (CP-575)",      required=True, auto_request=True),
        DocChecklistItem(name="Bank Statements (2 mo)",   required=True, auto_request=True),
        DocChecklistItem(name="Existing Mortgage Statement", required=True, auto_request=True),
        DocChecklistItem(name="Insurance Binder",         required=True, auto_request=True),
    ],
}

# Reminder cadence used when AppSettings.checklists has no entry.
_DEFAULT_FIRST_DAYS = 3
_DEFAULT_SECOND_DAYS = 7
_DEFAULT_ESCALATE_DAYS = 14


def _coerce_settings(settings_row: AppSettings | None) -> AppSettingsData:
    if settings_row is None:
        return AppSettingsData()
    try:
        return AppSettingsData.model_validate(settings_row.data or {})
    except Exception:  # noqa: BLE001
        log.warning("settings parse failed in loan_intake_automation; using defaults")
        return AppSettingsData()


def _checklist_for(settings: AppSettingsData, loan_type: str) -> LoanTypeChecklist:
    """Returns the operator-configured checklist OR a sane default.
    Defaults track the firm's current paper trail; operators override
    via Settings → Doc checklists."""
    cfg = settings.checklists.get(loan_type)
    if cfg is not None and cfg.docs:
        return cfg
    return LoanTypeChecklist(
        docs=_DEFAULT_DOCS_BY_TYPE.get(loan_type, []),
        first_reminder_days=_DEFAULT_FIRST_DAYS,
        second_reminder_days=_DEFAULT_SECOND_DAYS,
        escalate_after_days=_DEFAULT_ESCALATE_DAYS,
    )


# ── kickoff ─────────────────────────────────────────────────────────────


async def kickoff_loan(
    db: AsyncSession,
    loan: Loan,
    settings_row: AppSettings | None,
) -> int:
    """Idempotent — creates Document rows + calendar reminders for a
    freshly-spawned Loan based on the operator's checklist for that
    loan type. Skips items whose `auto_request=False` (operator opts
    them out at the settings level).

    Returns the count of new documents created.

    The doc-name+loan_id pair is the de-dup key; a re-run leaves the
    existing rows alone. Calendar reminders ride on the document's
    UUID so they're separately idempotent via the partial unique
    index in alembic 0013."""
    settings = _coerce_settings(settings_row)
    checklist = _checklist_for(settings, str(loan.type))
    if not checklist.docs:
        log.info("kickoff_loan: no checklist for type=%s loan=%s", loan.type, loan.deal_id)
        return 0

    # Look up which doc names already exist on this loan so we don't
    # re-request them on a kickoff retry.
    existing = (
        await db.execute(
            select(Document.name).where(Document.loan_id == loan.id)
        )
    ).scalars().all()
    existing_set = {n.lower().strip() for n in existing}

    today = date.today()
    created = 0
    for item in checklist.docs:
        if not item.auto_request:
            continue
        if item.name.lower().strip() in existing_set:
            continue
        doc = Document(
            loan_id=loan.id,
            name=item.name,
            status=DocStatus.REQUESTED,
            requested_on=today,
        )
        db.add(doc)
        await db.flush()
        await db.refresh(doc)

        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=None,
                actor_label="ai",
                kind="document.requested",
                summary=f"Auto-requested at intake: {item.name}",
                payload={"doc_id": str(doc.id), "auto": True, "loan_type": str(loan.type)},
            )
        )

        # Calendar reminder due in `first_reminder_days`. Phase 4 is
        # the first place to honor the operator's configured cadence.
        await calendar_emitter.emit_for_document_request(
            db, doc, due_in_days=checklist.first_reminder_days
        )
        created += 1

    log.info(
        "kickoff_loan: loan=%s type=%s created=%d total_in_checklist=%d",
        loan.deal_id, loan.type, created, len(checklist.docs),
    )
    return created


# ── reminders + escalation ──────────────────────────────────────────────


async def evaluate_doc_reminders() -> dict[str, int]:
    """Daily scheduler job. Walks all outstanding Document rows and
    emits reminder Activity rows + AITasks at three escalating tiers.

    Returns a dict with counts for visibility in scheduler logs:
      {first_emitted, second_emitted, escalated}
    """
    counts = {"first_emitted": 0, "second_emitted": 0, "escalated": 0}
    today = date.today()

    async with SessionLocal() as db:
        settings_row = (
            await db.execute(select(AppSettings).limit(1))
        ).scalar_one_or_none()
        settings = _coerce_settings(settings_row)

        # All currently-requested docs.
        docs = (
            await db.execute(
                select(Document).where(
                    Document.status == DocStatus.REQUESTED,
                    Document.requested_on.is_not(None),
                )
            )
        ).scalars().all()

        # Pre-fetch which docs already got each reminder tier today
        # so we don't double-emit when the cron retries.
        already_first = await _docs_with_activity_kind(db, "doc.reminder.first")
        already_second = await _docs_with_activity_kind(db, "doc.reminder.second")
        already_escalated = await _docs_with_activity_kind(db, "doc.escalated")

        for doc in docs:
            if doc.requested_on is None:
                continue
            age = (today - doc.requested_on).days
            doc_id_str = str(doc.id)

            # Use the loan-type's specific cadence when available,
            # otherwise the global default.
            loan = await db.get(Loan, doc.loan_id) if doc.loan_id else None
            checklist = (
                _checklist_for(settings, str(loan.type)) if loan else None
            )
            first = checklist.first_reminder_days if checklist else _DEFAULT_FIRST_DAYS
            second = checklist.second_reminder_days if checklist else _DEFAULT_SECOND_DAYS
            escalate = checklist.escalate_after_days if checklist else _DEFAULT_ESCALATE_DAYS

            if age >= escalate and doc_id_str not in already_escalated:
                _emit_escalation(db, doc, age)
                counts["escalated"] += 1
            elif age >= second and doc_id_str not in already_second:
                _emit_second_reminder(db, doc, age)
                counts["second_emitted"] += 1
            elif age >= first and doc_id_str not in already_first:
                _emit_first_reminder(db, doc, age)
                counts["first_emitted"] += 1

        await db.commit()

    log.info("evaluate_doc_reminders: %s", counts)
    return counts


async def _docs_with_activity_kind(db: AsyncSession, kind: str) -> set[str]:
    """Returns the set of doc_id strings that already have an
    Activity row of the given kind (any time). Used to gate reminders
    so each tier fires at most once per doc."""
    rows = (
        await db.execute(
            select(Activity.payload).where(Activity.kind == kind)
        )
    ).scalars().all()
    out: set[str] = set()
    for payload in rows:
        if isinstance(payload, dict):
            doc_id = payload.get("doc_id")
            if doc_id:
                out.add(str(doc_id))
    return out


def _activity_payload_for_doc(doc: Document, age: int) -> dict[str, Any]:
    return {
        "doc_id": str(doc.id),
        "loan_id": str(doc.loan_id) if doc.loan_id else None,
        "doc_name": doc.name,
        "age_days": age,
    }


def _emit_first_reminder(db: AsyncSession, doc: Document, age: int) -> None:
    """Tier 1 — friendly nudge in the borrower's loan chat."""
    db.add(
        Activity(
            loan_id=doc.loan_id,
            actor_id=None,
            actor_label="ai",
            kind="doc.reminder.first",
            summary=f"First reminder: {doc.name} ({age}d outstanding)",
            payload=_activity_payload_for_doc(doc, age),
        )
    )
    if doc.loan_id:
        db.add(
            LoanChatMessage(
                loan_id=doc.loan_id,
                from_role=DealChatRole.AI,
                from_user_id=None,
                body=(
                    f"Quick nudge — we still need **{doc.name}** to keep your file moving. "
                    f"You can upload it from the Vault tab whenever it's ready."
                ),
                client_visible=True,
            )
        )


def _emit_second_reminder(db: AsyncSession, doc: Document, age: int) -> None:
    """Tier 2 — broker AITask alongside the chat reminder."""
    db.add(
        Activity(
            loan_id=doc.loan_id,
            actor_id=None,
            actor_label="ai",
            kind="doc.reminder.second",
            summary=f"Second reminder: {doc.name} ({age}d outstanding)",
            payload=_activity_payload_for_doc(doc, age),
        )
    )
    db.add(
        AITask(
            loan_id=doc.loan_id,
            source=AITaskSource.DOCUMENTS,
            priority=AITaskPriority.MEDIUM,
            status=AITaskStatus.PENDING,
            action="follow_up_doc",
            title=f"Doc still missing — {doc.name}",
            summary=(
                f"{doc.name} has been outstanding for {age} days on this loan. "
                f"The borrower has been auto-nudged twice. Consider a personal "
                f"call or extending the deadline."
            ),
            confidence=0.9,
            agent="doc-watch",
            draft_payload={"doc_id": str(doc.id), "tier": "second"},
        )
    )


def _emit_escalation(db: AsyncSession, doc: Document, age: int) -> None:
    """Tier 3 — super-admin AITask, calendar high-priority bump."""
    db.add(
        Activity(
            loan_id=doc.loan_id,
            actor_id=None,
            actor_label="ai",
            kind="doc.escalated",
            summary=f"Escalated: {doc.name} ({age}d outstanding)",
            payload=_activity_payload_for_doc(doc, age),
        )
    )
    db.add(
        AITask(
            loan_id=doc.loan_id,
            source=AITaskSource.DOCUMENTS,
            priority=AITaskPriority.HIGH,
            status=AITaskStatus.PENDING,
            action="escalate_doc",
            title=f"DOC ESCALATION — {doc.name}",
            summary=(
                f"{doc.name} has been outstanding for {age} days. Two prior "
                f"reminders have been sent. Escalating to super-admin for "
                f"review — may need a call, scope change, or removal from "
                f"the checklist."
            ),
            confidence=0.95,
            agent="doc-watch",
            draft_payload={"doc_id": str(doc.id), "tier": "escalated"},
        )
    )
