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
from sqlalchemy.orm import selectinload

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
# type. Operators override per-loan-type from the Settings UI; until
# they do, we don't want intake to silently skip doc collection.
#
# Each item carries:
#   - type: internal (operator-ordered, AITask) | external (borrower
#                     upload, Document)
#   - anchor: when this item gets created — "loan_created" fires at
#                     kickoff; "doc_received:<name>" fires when the
#                     named external item flips to RECEIVED
#   - due_offset_days: days from anchor → due
#   - per_unit: fan out to N rows on multi-unit properties
#   - internal_action: short tag for operator UI (only on internal)
#
# Internal items (Appraisal, Title, Insurance Binder, PFS) live in
# the operator AI Inbox — borrower never sees them in the vault.
_DEFAULT_DOCS_BY_TYPE: dict[str, list[DocChecklistItem]] = {
    "dscr": [
        # ── External: borrower-supplied at intake ────────────────
        DocChecklistItem(
            name="Driver's License", type="external",
            anchor="loan_created", due_offset_days=1,
        ),
        DocChecklistItem(
            name="Operating Agreement", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="EIN Letter (CP-575)", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Bank Statements (2 mo)", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        # Per-unit fan-out: a 4-plex needs 4 leases.
        DocChecklistItem(
            name="Lease", type="external", per_unit=True,
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Rent Roll", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Property Tax Statement", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        # ── Internal: operator-ordered ───────────────────────────
        DocChecklistItem(
            name="Personal Financial Statement (PFS)",
            display_name="Personal Financial Statement (PFS)",
            type="internal", internal_action="request_pfs",
            anchor="loan_created", due_offset_days=0,
        ),
        DocChecklistItem(
            name="Title Commitment", type="internal",
            internal_action="order_title",
            anchor="doc_received:Operating Agreement", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Insurance Binder", type="internal",
            internal_action="shop_insurance",
            anchor="doc_received:Property Tax Statement", due_offset_days=5,
        ),
        DocChecklistItem(
            name="Appraisal", type="internal",
            internal_action="order_appraisal",
            anchor="doc_received:Bank Statements (2 mo)", due_offset_days=2,
        ),
    ],
    "fix_and_flip": [
        DocChecklistItem(
            name="Driver's License", type="external",
            anchor="loan_created", due_offset_days=1,
        ),
        DocChecklistItem(
            name="Operating Agreement", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="EIN Letter (CP-575)", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Bank Statements (2 mo)", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Construction Budget / SOW", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Schedule of Real Estate Owned", type="external",
            anchor="loan_created", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Contractor Bid", type="external",
            anchor="loan_created", due_offset_days=5,
        ),
        DocChecklistItem(
            name="Personal Financial Statement (PFS)",
            display_name="Personal Financial Statement (PFS)",
            type="internal", internal_action="request_pfs",
            anchor="loan_created", due_offset_days=0,
        ),
        DocChecklistItem(
            name="Title Commitment", type="internal",
            internal_action="order_title",
            anchor="doc_received:Operating Agreement", due_offset_days=3,
        ),
        DocChecklistItem(
            name="Insurance Binder", type="internal",
            internal_action="shop_insurance",
            anchor="doc_received:Construction Budget / SOW", due_offset_days=5,
        ),
        DocChecklistItem(
            name="Appraisal", type="internal",
            internal_action="order_appraisal",
            anchor="doc_received:Bank Statements (2 mo)", due_offset_days=2,
        ),
    ],
    "ground_up": [
        DocChecklistItem(name="Driver's License", type="external",
                         anchor="loan_created", due_offset_days=1),
        DocChecklistItem(name="Operating Agreement", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="EIN Letter (CP-575)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Bank Statements (2 mo)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Plans & Permits", type="external",
                         anchor="loan_created", due_offset_days=5),
        DocChecklistItem(name="Construction Budget", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Personal Financial Statement (PFS)",
                         display_name="Personal Financial Statement (PFS)",
                         type="internal", internal_action="request_pfs",
                         anchor="loan_created", due_offset_days=0),
        DocChecklistItem(name="Builder's Risk Insurance", type="internal",
                         internal_action="shop_insurance",
                         anchor="doc_received:Construction Budget", due_offset_days=5),
        DocChecklistItem(name="Title Commitment", type="internal",
                         internal_action="order_title",
                         anchor="doc_received:Operating Agreement", due_offset_days=3),
        DocChecklistItem(name="Appraisal", type="internal",
                         internal_action="order_appraisal",
                         anchor="doc_received:Bank Statements (2 mo)", due_offset_days=2),
    ],
    "bridge": [
        DocChecklistItem(name="Driver's License", type="external",
                         anchor="loan_created", due_offset_days=1),
        DocChecklistItem(name="Operating Agreement", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="EIN Letter (CP-575)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Bank Statements (2 mo)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Exit Strategy", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Personal Financial Statement (PFS)",
                         display_name="Personal Financial Statement (PFS)",
                         type="internal", internal_action="request_pfs",
                         anchor="loan_created", due_offset_days=0),
        DocChecklistItem(name="Insurance Binder", type="internal",
                         internal_action="shop_insurance",
                         anchor="doc_received:Bank Statements (2 mo)", due_offset_days=5),
        DocChecklistItem(name="Title Commitment", type="internal",
                         internal_action="order_title",
                         anchor="doc_received:Operating Agreement", due_offset_days=3),
    ],
    "portfolio": [
        DocChecklistItem(name="Driver's License", type="external",
                         anchor="loan_created", due_offset_days=1),
        DocChecklistItem(name="Operating Agreement", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="EIN Letter (CP-575)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Bank Statements (2 mo)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Schedule of Real Estate Owned", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Rent Rolls (all properties)", type="external",
                         anchor="loan_created", due_offset_days=5),
        DocChecklistItem(name="Personal Financial Statement (PFS)",
                         display_name="Personal Financial Statement (PFS)",
                         type="internal", internal_action="request_pfs",
                         anchor="loan_created", due_offset_days=0),
    ],
    "cash_out_refi": [
        DocChecklistItem(name="Driver's License", type="external",
                         anchor="loan_created", due_offset_days=1),
        DocChecklistItem(name="Operating Agreement", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="EIN Letter (CP-575)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Bank Statements (2 mo)", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Existing Mortgage Statement", type="external",
                         anchor="loan_created", due_offset_days=3),
        DocChecklistItem(name="Personal Financial Statement (PFS)",
                         display_name="Personal Financial Statement (PFS)",
                         type="internal", internal_action="request_pfs",
                         anchor="loan_created", due_offset_days=0),
        DocChecklistItem(name="Insurance Binder", type="internal",
                         internal_action="shop_insurance",
                         anchor="doc_received:Existing Mortgage Statement", due_offset_days=5),
        DocChecklistItem(name="Title Commitment", type="internal",
                         internal_action="order_title",
                         anchor="doc_received:Operating Agreement", due_offset_days=3),
        DocChecklistItem(name="Appraisal", type="internal",
                         internal_action="order_appraisal",
                         anchor="doc_received:Bank Statements (2 mo)", due_offset_days=2),
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
    """Run the per-item checklist matrix for items anchored to
    `loan_created`. Items anchored on `doc_received:<name>` are
    materialized lazily by `fire_anchor_dependents` when the
    prerequisite doc flips to RECEIVED.

    Per-item behavior (centralized in
    `services/checklist_scheduler.materialize_item`):

      type=internal       → spawn AITask only
      type=external       → spawn Document(REQUESTED) + calendar event
                            (fan out to N rows when per_unit=True)

    Idempotent on retry — `(loan_id, name)` is the dedup key for
    Documents; `(loan_id, action)` is the dedup key for AITasks.

    Also posts the property-intake opener message into the
    borrower's per-loan AI thread so the first thing they see is
    a friendly chat asking about beds / baths / sqft / units.

    Returns the count of NEW Documents created (legacy contract —
    AITask count is logged but not returned to keep the existing
    callers happy).
    """
    from app.services.checklist_scheduler import materialize_kickoff_items  # local — avoid circular

    settings = _coerce_settings(settings_row)
    checklist = _checklist_for(settings, str(loan.type))
    if not checklist.docs:
        log.info("kickoff_loan: no checklist for type=%s loan=%s", loan.type, loan.deal_id)
        return 0

    docs_created, tasks_created = await materialize_kickoff_items(db, loan, checklist)

    # Post the property-intake opener so the borrower's per-loan
    # AI thread starts with a useful AI message instead of being
    # empty. Best-effort — never fail kickoff on a messaging hiccup.
    try:
        await _post_property_intake_opener(db, loan)
    except Exception:  # noqa: BLE001
        log.exception("kickoff_loan: property intake opener failed loan=%s", loan.deal_id)

    log.info(
        "kickoff_loan: loan=%s type=%s docs=%d tasks=%d total_in_checklist=%d",
        loan.deal_id, loan.type, docs_created, tasks_created, len(checklist.docs),
    )
    return docs_created


async def _post_property_intake_opener(db: AsyncSession, loan: Loan) -> None:
    """Drop the first AI message into the borrower's per-loan thread.
    Skips silently when:
      - the loan has no client (data drift)
      - the client has no linked user
      - the thread already has messages (re-kickoff shouldn't spam)
    """
    from sqlalchemy import select as _select
    from sqlalchemy.orm import selectinload as _selectinload
    from app.models.client import Client as _Client
    from app.models.ai_chat_thread import AIChatThread as _AIChatThread, AIChatMessage as _AIChatMessage
    from app.services.ai_messaging import post_ai_message

    client = (
        await db.execute(
            _select(_Client).where(_Client.id == loan.client_id)
        )
    ).scalar_one_or_none()
    if client is None or client.user_id is None:
        return

    # If the loan's thread already has messages, don't double-post.
    existing_thread = (
        await db.execute(
            _select(_AIChatThread).where(
                _AIChatThread.user_id == client.user_id,
                _AIChatThread.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if existing_thread is not None:
        msg_count = (
            await db.execute(
                _select(_AIChatMessage).where(_AIChatMessage.thread_id == existing_thread.id).limit(1)
            )
        ).scalar_one_or_none()
        if msg_count is not None:
            return

    address = loan.address or "the property"
    body = (
        f"Hi! Let's get your file moving. I have a few quick questions about "
        f"{address} so the underwriting team can size your deal accurately.\n\n"
        f"**How many bedrooms and bathrooms?**\n"
        f"**What year was it built?**\n"
        f"**About how many square feet?**\n"
        f"**How many units total?** (1 for a single-family, 2-4 for a small "
        f"multi, 5+ for a larger building)\n\n"
        f"Reply however you like — I'll fill in the details as we go."
    )
    await post_ai_message(
        db,
        user_id=client.user_id,
        loan_id=loan.id,
        body=body,
    )


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
            # otherwise the global default. Eager-load `client` so
            # the per-doc emitter can post into the borrower's
            # AIChatThread without a lazy-load.
            loan = None
            if doc.loan_id:
                loan = (
                    await db.execute(
                        select(Loan)
                        .options(selectinload(Loan.client))
                        .where(Loan.id == doc.loan_id)
                    )
                ).scalar_one_or_none()
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
                await _emit_first_reminder(db, doc, age, loan)
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


async def _emit_first_reminder(
    db: AsyncSession,
    doc: Document,
    age: int,
    loan: Loan | None,
) -> None:
    """Tier 1 — friendly nudge in the borrower's loan chat thread.

    Lands in TWO places for v1:
      - LoanChatMessage on the deal workspace (operator-facing,
        legacy surface), so brokers viewing the deal still see the
        nudge.
      - AIChatMessage on the borrower's per-loan thread (Phase 8
        + 0017 surface) so the borrower sees it in their Messages
        tab on mobile / desktop.
    """
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
    nudge_body = (
        f"Quick nudge — we still need **{doc.name}** to keep your file moving. "
        f"You can upload it from the Vault tab whenever it's ready."
    )
    if doc.loan_id:
        # Legacy: deal-workspace chat (operator-visible)
        db.add(
            LoanChatMessage(
                loan_id=doc.loan_id,
                from_role=DealChatRole.AI,
                from_user_id=None,
                body=nudge_body,
                client_visible=True,
            )
        )
    # New: borrower's per-loan AI chat thread.
    if loan is not None and loan.client is not None and loan.client.user_id is not None:
        try:
            from app.services.ai_messaging import post_ai_message  # local to avoid cycle
            await post_ai_message(
                db,
                user_id=loan.client.user_id,
                loan_id=loan.id,
                body=nudge_body,
            )
        except Exception:  # noqa: BLE001
            log.warning("doc reminder: post_ai_message failed for doc=%s", doc.id, exc_info=True)


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
