"""Dealer Prospect drafting, review, collateral, and at-most-once delivery.

The critical invariant is that a draft is claimed and committed as ``sending``
before SES is called.  SES has no idempotency token.  In the ambiguous crash
window after provider acceptance, leaving the draft in ``sending`` and requiring
manual reconciliation is the only honest way to guarantee it is never sent a
second time automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import struct
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import formataddr
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from pydantic import ValidationError
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dealer_os.models import DealerFieldDeskProfile, DealerRepCompany, DealerRepContact
from app.models.activity import Activity
from app.models.app_settings import AppSettings
from app.models.booking_settings import BookingSettings
from app.models.dealer_prospect import (
    DealerProspect,
    DealerProspectActivity,
    DealerProspectStageDefinition,
)
from app.models.funding_program import FundingProgramCatalog, FundingProgramScope
from app.models.message_send import MessageSend
from app.models.notification import Notification
from app.models.prospect_outreach import (
    DealerProspectEmailDraft,
    DealerProspectEmailDraftAsset,
    EmailSuppression,
    MarketingCollateralAsset,
    MarketingCollateralAssetEvent,
)
from app.models.user import User
from app.schemas.prospect_outreach import (
    ProspectEmailDraftCreate,
    ProspectEmailDraftRead,
    ProspectOutreachPolicyPatch,
    ProspectTestEmailRequest,
    ProspectTestEmailResponse,
)
from app.schemas.settings import ProspectOutreachAISettings
from app.services.app_settings_lock import lock_app_settings

log = logging.getLogger(__name__)

DEALER_WEBSITE = "https://qualifiedcommercial.com/industries/auto"
COLLATERAL_ASSIGNMENT = "dealer_outreach"
DEFAULT_PURPOSE = "dealer_information"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_FINANCIAL_CLAIM_RE = re.compile(
    r"(?:\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|m|million|thousand))?|\b\d+(?:\.\d+)?\s?%)",
    re.IGNORECASE,
)
_FORBIDDEN_COPY = (
    "faster than anyone else",
    "faster than other",
    "better than other",
    "better than competitors",
    "guaranteed approval",
    "guaranteed financing",
    "pre-approved",
    "preapproved",
    "pre-qualified",
    "prequalified",
    "prequalify",
    "no credit check",
    "approval is guaranteed",
    "we guarantee",
    "you are approved",
    "you've been approved",
    "you have been approved",
    "attached is",
    "attached are",
    "see attached",
    "in the attachment",
)
_UNSUPPORTED_FUNDING_TIMELINE_RE = re.compile(
    r"\b(?:same[- ]day funding|(?:funds?|funding)\s+(?:will|can|should|may)\s+"
    r"(?:arrive|close|be\s+(?:available|deposited|delivered))\s+"
    r"(?:today|tomorrow|by\s+\w+|in\s+\d+\s+\w+)|"
    r"fund(?:ed|ing)?\s+(?:in|within|by)\s+(?:\d+|one|two|three|a few)\s+"
    r"(?:hours?|business days?|days?|weeks?)|(?:approval|funding)\s+(?:in|within)\s+"
    r"(?:\d+|one|two|three|a few)\s+(?:hours?|business days?|days?|weeks?)|"
    r"(?:fast|quick|instant)\s+(?:funding|approval))\b",
    re.IGNORECASE,
)
_UNSUPPORTED_RECIPIENT_OUTCOME_RE = re.compile(
    r"\b(?:(?:based\s+on\s+[^.!?]{0,80},?\s*)?(?:you|your\s+(?:dealership|business))\s+"
    r"(?:are\s+)?(?:qualified|eligible|prequalified|pre-qualified)|"
    r"your\s+(?:dealership|business)\s+qualifies|you\s+qualify|"
    r"you\s+(?:will|would|can|should|may)\s+be\s+(?:approved|qualified|eligible)|"
    r"you\s+(?:will|would|can|should)\s+(?:receive|obtain|secure|get)\s+"
    r"(?:an?\s+)?(?:approval|financing|funding))\b",
    re.IGNORECASE,
)
_RECIPIENT_RATE_RE = re.compile(
    r"\b(?:your\s+(?:rate|apr)|(?:rate|apr)\s+for\s+you|"
    r"you\s+(?:will|would|can|should|may)\s+(?:receive|get|have)\s+"
    r"(?:an?\s+)?(?:rate|apr))\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"(?:https?://|www\.|mailto:|\b(?:[a-z0-9-]+\.)+[a-z]{2,63}(?:/[^\s]*)?)",
    re.IGNORECASE,
)
_APPLICATION_OWNED_COPY_RE = re.compile(
    r"\b(?:unsubscribe|opt[ -]?out|reply\s+stop|stop\s+receiving)\b",
    re.IGNORECASE,
)
_SIGNATURE_RE = re.compile(
    r"(?:^|\n)\s*(?:best(?:\s+regards)?|kind\s+regards|sincerely|warmly|thanks|thank\s+you)"
    r"\s*,?\s*(?:\n|$)",
    re.IGNORECASE,
)
_PRODUCT_CLAIM_RE = re.compile(
    r"\b(?:[a-z0-9][a-z0-9()&/+.-]*[ \t]+){0,4}"
    r"(?:loans?|financing|lines? of credit|advances?|factoring|leases?|facilit(?:y|ies)|"
    r"mortgages?|refinancing|cash advances?|credit facilities|capital programs?)\b",
    re.IGNORECASE,
)
_AI_CAPABILITY_CLAIM_RE = re.compile(
    r"\b(?:we|our|qualified commercial)\s+"
    r"(?:can\s+)?(?:offer|provide|fund|finance|arrange|deliver|specialize(?:s|d)?\s+in|"
    r"help(?:s|ed)?\s+with|support(?:s|ed)?\s+with|have)\b",
    re.IGNORECASE,
)
_GENERIC_PRODUCT_PHRASES = {
    "commercial financing",
    "dealer financing",
    "financing options",
    "financing goals",
    "financing resources",
    "lender financing",
}
_ACTIVE_PDF_MARKERS = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile")
_EICAR = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"
LOCKED_DRAFTING_RULES = (
    "Never promise approval, guaranteed financing, rates, funding timelines, or lender outcomes.",
    "Never invent or alter products, program facts, amounts, eligibility, or company capabilities.",
    "Never prequalify the dealer or claim Qualified Commercial is faster or better than competitors.",
    "Never add links, attachment claims, signatures, compliance text, or unsubscribe language; the application owns those sections.",
    "Never describe an SBA Microloan above the published $50,000 maximum.",
)
TEST_EMAIL_HOURLY_LIMIT = 10


class OutreachConflict(RuntimeError):
    pass


class OutreachBlocked(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class OutreachNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ComposedCopy:
    subject: str
    body: str
    source: str
    model_id: str | None = None
    # Application-owned diagnostics. Raw provider errors must never be exposed
    # to an operator or placed in an outbound message.
    generation_reason: str = "approved_fallback"
    instruction_disposition: str = "none"


@dataclass(frozen=True)
class PdfValidation:
    sha256: str
    size_bytes: int
    status: str
    detail: str


@dataclass(frozen=True)
class ProspectIdentity:
    contact_id: uuid.UUID
    contact_name: str
    dealer_name: str
    email: str
    owner_user_id: uuid.UUID | None


async def load_outreach_ai_settings(
    db: AsyncSession, *, lock: bool = False
) -> ProspectOutreachAISettings:
    if lock:
        await lock_app_settings(db)
    statement = select(AppSettings).limit(1)
    if lock:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    raw = (row.data or {}).get("prospect_outreach_ai", {}) if row is not None else {}
    try:
        return ProspectOutreachAISettings.model_validate(raw or {})
    except ValidationError as exc:
        raise OutreachBlocked(
            "drafting_policy_unavailable",
            "The Dealer Prospect email policy is invalid. No email can be drafted or sent until an administrator saves a valid policy.",
        ) from exc


async def update_outreach_ai_settings(
    db: AsyncSession,
    *,
    actor: User,
    payload: ProspectOutreachPolicyPatch,
) -> ProspectOutreachAISettings:
    await lock_app_settings(db)
    row = (await db.execute(select(AppSettings).limit(1).with_for_update())).scalar_one_or_none()
    if row is None:
        row = AppSettings(singleton=True, data={})
        db.add(row)
        await db.flush()
    data = dict(row.data or {})
    current = data.get("prospect_outreach_ai")
    current_data = current if isinstance(current, dict) else {}
    policy = ProspectOutreachAISettings(
        drafting_guidance=(
            payload.drafting_guidance
            if payload.drafting_guidance is not None
            else current_data.get("drafting_guidance", "")
        ),
        additional_blocked_phrases=(
            payload.additional_blocked_phrases
            if payload.additional_blocked_phrases is not None
            else current_data.get("additional_blocked_phrases", [])
        ),
        updated_at=utcnow(),
        updated_by_user_id=actor.id,
    )
    data["prospect_outreach_ai"] = policy.model_dump(mode="json")
    row.data = data
    actor_role = getattr(actor, "role", "operator")
    db.add(
        Activity(
            loan_id=None,
            actor_id=actor.id,
            actor_label=(
                actor_role.value if getattr(actor_role, "value", None) else str(actor_role)
            ),
            kind="prospect.outreach_ai_policy_updated",
            summary="Updated Dealer Prospect email AI guidance and blocked phrases",
            payload={
                "blocked_phrase_count": len(policy.additional_blocked_phrases),
                "has_guidance": bool(policy.drafting_guidance),
                "changed_fields": sorted(payload.model_fields_set),
            },
        )
    )
    await db.flush()
    return policy


def utcnow() -> datetime:
    return datetime.now(UTC)


def _notification_copy(row: DealerProspectEmailDraft) -> tuple[str, str, str]:
    if row.status == "pending_review":
        when = row.auto_send_at.isoformat() if row.auto_send_at else "the scheduled time"
        return (
            "Dealer email awaiting review",
            f"“{row.subject}” will send automatically at {when}.",
            "high",
        )
    if row.status == "editing":
        return (
            "Dealer email review paused",
            f"“{row.subject}” requires explicit approval after editing.",
            "medium",
        )
    if row.status == "sending":
        return "Dealer email sending", f"“{row.subject}” is being delivered.", "medium"
    if row.status == "sent":
        return "Dealer email sent", f"“{row.subject}” was sent.", "low"
    if row.status == "cancelled":
        return "Dealer email cancelled", f"“{row.subject}” was cancelled.", "low"
    if row.status == "blocked":
        return (
            "Dealer email blocked",
            (row.failure_detail or f"“{row.subject}” cannot be sent.")[:500],
            "high",
        )
    return (
        "Dealer email failed",
        (row.failure_detail or f"“{row.subject}” could not be delivered.")[:500],
        "high",
    )


async def sync_draft_notifications(
    db: AsyncSession,
    row: DealerProspectEmailDraft,
    *,
    prospect: DealerProspect | None = None,
) -> None:
    """Create or update one in-app countdown notification per responsible user."""
    if prospect is None:
        prospect = await db.get(DealerProspect, row.prospect_id)
    recipient_ids = {
        value
        for value in (
            row.created_by_user_id,
            prospect.owner_user_id if prospect is not None else None,
        )
        if value is not None
    }
    if not recipient_ids:
        return
    target_id = str(row.id)
    existing = list(
        (
            await db.execute(
                select(Notification).where(
                    Notification.target_type == "dealer_prospect_email_draft",
                    Notification.target_id == target_id,
                    Notification.recipient_user_id.in_(recipient_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_recipient = {item.recipient_user_id: item for item in existing}
    title, body, priority = _notification_copy(row)
    meta = {
        "draft_id": target_id,
        "prospect_id": str(row.prospect_id),
        "send_after": row.auto_send_at.isoformat() if row.auto_send_at else None,
        "status": row.status,
    }
    for recipient_id in recipient_ids:
        notice = by_recipient.get(recipient_id)
        if notice is None:
            db.add(
                Notification(
                    recipient_user_id=recipient_id,
                    event_type="dealer_prospect_email_draft",
                    category="messages",
                    priority=priority,
                    title=title,
                    body=body,
                    target_type="dealer_prospect_email_draft",
                    target_id=target_id,
                    deep_link=f"/contacts/prospects/{row.prospect_id}",
                    channels=["in_app"],
                    meta=meta,
                    batch_key=f"dealer-prospect-email-draft:{target_id}",
                )
            )
            continue
        notice.event_type = "dealer_prospect_email_draft"
        notice.category = "messages"
        notice.priority = priority
        notice.title = title
        notice.body = body
        notice.deep_link = f"/contacts/prospects/{row.prospect_id}"
        notice.channels = ["in_app"]
        notice.meta = meta


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def valid_email(value: str | None) -> bool:
    return bool(_EMAIL_RE.fullmatch(normalize_email(value)))


def attachment_bundle_too_large(total_bytes: int) -> bool:
    """All-or-none attachment gate shared by draft creation and dispatch."""
    return int(total_bytes) > get_settings().prospect_email_max_attachment_bytes


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokenized_reply_to(base_email: str, token: str) -> str:
    local, separator, domain = (base_email or "").strip().partition("@")
    if not separator or not local or not domain:
        raise OutreachBlocked("sender_not_configured", "Dealer Desk Reply-To address is invalid.")
    # Remove an existing plus tag so configuration cannot create nested aliases.
    local = local.split("+", 1)[0]
    return f"{local}+{token}@{domain}".lower()


def reply_contact_email(base_email: str) -> str:
    """Return the public mailbox address without its internal correlation tag."""
    local, separator, domain = normalize_email(base_email).partition("@")
    if not separator or not local or not domain:
        return ""
    return f"{local.split('+', 1)[0]}@{domain}"


def request_fingerprint(prospect: Any, payload: ProspectEmailDraftCreate) -> str:
    raw = {
        "prospect_id": str(prospect.id),
        "email": normalize_email(
            getattr(prospect, "email", None) or getattr(prospect, "email_normalized", None)
        ),
        "purpose": payload.purpose,
        "ai_instructions": payload.ai_instructions,
        "verified_conversation_context": payload.verified_conversation_context,
        "private_note": payload.private_note,
    }
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_request_fingerprint(actor: User, payload: ProspectTestEmailRequest) -> str:
    """Bind a self-test idempotency key to the complete operator request."""
    raw = {
        "actor_id": str(actor.id),
        "recipient": normalize_email(actor.email),
        "purpose": payload.purpose,
        "sample_contact_name": payload.sample_contact_name,
        "sample_dealer_name": payload.sample_dealer_name,
        "ai_instructions": payload.ai_instructions,
        "verified_conversation_context": payload.verified_conversation_context,
        "include_collateral": payload.include_collateral,
    }
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _first_name(value: str | None) -> str:
    clean = " ".join((value or "").split())
    return clean.split(" ", 1)[0] if clean else "there"


def _clean_label(value: str | None, fallback: str) -> str:
    return " ".join((value or "").split())[:180] or fallback


def _insert_verified_conversation_context(body: str, context: str | None) -> str:
    """Place authenticated operator context exactly once after the greeting.

    The returned body still passes through the complete copy validator. This
    helper establishes placement and de-duplication; it grants no exemption
    from product, financial-claim, link, or firm blocked-phrase rules.
    """
    clean_context = " ".join((context or "").split())
    clean_body = (body or "").strip()
    if not clean_context:
        return clean_body

    # Remove exact prior occurrences with flexible whitespace before inserting
    # the normalized operator-supplied sentence in the canonical location.
    context_pattern = re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(part) for part in clean_context.split()) + r"(?!\w)",
        re.IGNORECASE,
    )
    without_context = context_pattern.sub("", clean_body).strip()
    lines = without_context.splitlines()
    first_content = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_content is not None and re.match(
        r"^(?:hi|hello|dear)\b[^\n]*[,!:]\s*$",
        lines[first_content].strip(),
        re.IGNORECASE,
    ):
        before = "\n".join(lines[: first_content + 1]).strip()
        after = "\n".join(lines[first_content + 1 :]).strip()
        return f"{before}\n\n{clean_context}" + (f"\n\n{after}" if after else "")
    return f"{clean_context}" + (f"\n\n{without_context}" if without_context else "")


def _purpose_fallback(*, purpose: str, contact_name: str, dealer_name: str) -> ComposedCopy:
    first = _first_name(contact_name)
    dealer = _clean_label(dealer_name, "your dealership")
    if purpose == "missed_call":
        return ComposedCopy(
            subject=f"Sorry we missed you — {dealer}",
            body=(
                f"Hi {first},\n\nI tried to reach you and wanted to leave a quick note. "
                "Qualified Commercial's Dealer Desk is available to learn about what your "
                "dealership is planning. Reply when it is convenient and we can discuss "
                "practical next steps."
            ),
            source="fallback",
        )
    if purpose == "callback_confirmation":
        return ComposedCopy(
            subject=f"Following up with {dealer}",
            body=(
                f"Hi {first},\n\nThank you for speaking with me. I will follow up at the time "
                "we discussed. If anything changes, reply here and we can find a better time."
            ),
            source="fallback",
        )
    if purpose == "booking":
        return ComposedCopy(
            subject=f"Next steps for {dealer}",
            body=(
                f"Hi {first},\n\nThank you for your interest. Choose a time below that works "
                "for you, and we can discuss what your dealership is planning and the information "
                "lender review may require."
            ),
            source="fallback",
        )
    return ComposedCopy(
        subject=f"Commercial financing resources for {dealer}",
        body=(
            f"Hi {first},\n\nI am following up with an overview of dealer-focused programs "
            f"Qualified Commercial can discuss with {dealer}.\n\nReply with what you are "
            "planning and we can help identify practical next steps."
        ),
        source="fallback",
    )


def _dealer_catalog_snapshot(
    rows: Iterable[tuple[FundingProgramCatalog, FundingProgramScope]],
) -> list[dict[str, Any]]:
    """Snapshot active canonical programs and the dealer scopes admitting them."""
    grouped: dict[uuid.UUID, dict[str, Any]] = {}
    for program, scope in rows:
        # Keep this defensive check even though the SQL query has the same
        # predicates: callers and tests cannot accidentally turn a Main Street
        # or retired program into Dealer Desk marketing copy.
        if (
            program.status != "active"
            or scope.vertical != "dealer"
            or not scope.is_active
        ):
            continue
        item = grouped.setdefault(
            program.id,
            {
                "program_id": str(program.id),
                "program_key": program.program_key,
                "public_slug": program.public_slug,
                "name": _clean_label(program.name, program.program_key),
                "short_description": (
                    " ".join((program.short_description or "").split())[:1000] or None
                ),
                "display_order": int(program.display_order),
                "catalog_updated_at": (
                    program.updated_at.isoformat() if program.updated_at is not None else None
                ),
                "dealer_scopes": [],
            },
        )
        item["dealer_scopes"].append(
            {
                "scope_key": scope.scope_key,
                "required_fact_keys": sorted(set(scope.required_fact_keys or [])),
                "intake_variants": sorted(set(scope.intake_variants or [])),
                "intent_keys": sorted(set(scope.intent_keys or [])),
                "naics_prefixes": sorted(set(scope.naics_prefixes or [])),
                "industry_keys": sorted(set(scope.industry_keys or [])),
                "scope_updated_at": (
                    scope.updated_at.isoformat() if scope.updated_at is not None else None
                ),
            }
        )

    snapshot: list[dict[str, Any]] = []
    for item in grouped.values():
        item["dealer_scopes"].sort(key=lambda value: value["scope_key"])
        required = sorted(
            {
                key
                for scope in item["dealer_scopes"]
                for key in scope["required_fact_keys"]
            }
        )
        # An unconditioned dealer scope makes the program generally available
        # for discussion. Otherwise it belongs under specialized options.
        unrestricted_scope = any(
            not any(
                scope[key]
                for key in (
                    "required_fact_keys",
                    "industry_keys",
                    "naics_prefixes",
                    "intake_variants",
                    "intent_keys",
                )
            )
            for scope in item["dealer_scopes"]
        )
        item["required_fact_keys"] = [] if unrestricted_scope else required
        item["is_specialized"] = not unrestricted_scope
        snapshot.append(item)
    return sorted(snapshot, key=lambda row: (row["display_order"], row["name"], row["program_key"]))


async def _active_catalog_snapshot(db: AsyncSession) -> list[dict[str, Any]]:
    rows = list(
        (
            await db.execute(
                select(FundingProgramCatalog, FundingProgramScope)
                .join(
                    FundingProgramScope,
                    FundingProgramScope.program_id == FundingProgramCatalog.id,
                )
                .where(
                    FundingProgramCatalog.status == "active",
                    FundingProgramScope.vertical == "dealer",
                    FundingProgramScope.is_active.is_(True),
                )
                .order_by(
                    FundingProgramCatalog.display_order,
                    FundingProgramCatalog.name,
                    FundingProgramScope.scope_key,
                )
            )
        ).all()
    )
    snapshot = _dealer_catalog_snapshot(rows)
    if not snapshot:
        raise OutreachBlocked(
            "dealer_program_catalog_not_configured",
            "No active dealer-scoped funding programs are configured. Activate at least one canonical Dealer program before sending outreach.",
        )
    return snapshot


def catalog_version(snapshot: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _approved_program_section(snapshot: list[dict[str, Any]]) -> str:
    """Render canonical dealer program names, never model-authored prose."""
    standard: list[str] = []
    specialized: list[str] = []
    seen: set[str] = set()
    for row in snapshot:
        if not isinstance(row, dict):
            continue
        clean = " ".join(str(row.get("name") or "").split())[:160]
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        target = specialized if row.get("is_specialized", bool(row.get("required_fact_keys"))) else standard
        target.append(clean)
    if not standard and not specialized:
        raise OutreachBlocked(
            "dealer_program_catalog_not_configured",
            "The active Dealer program catalog contains no displayable program names.",
        )
    sections: list[str] = []
    if standard:
        sections.append(
            "Dealer-focused programs we can discuss:\n"
            + "\n".join(f"- {name}" for name in standard)
        )
    if specialized:
        sections.append(
            "Specialized options, when relevant:\n"
            + "\n".join(f"- {name}" for name in specialized)
        )
    sections.append(
        "Availability and terms depend on lender review, eligibility, underwriting, and documentation."
    )
    return "\n\n".join(sections)


def _with_approved_program_section(body: str, snapshot: list[dict[str, Any]]) -> str:
    section = _approved_program_section(snapshot)
    return f"{body.rstrip()}\n\n{section}" if section else body.rstrip()


def _parse_model_json(text: str) -> tuple[str, str]:
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
    start, end = clean.find("{"), clean.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model did not return JSON")
    parsed = json.loads(clean[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model response is not an object")
    return str(parsed.get("subject") or "").strip(), str(parsed.get("body") or "").strip()


def _approved_financial_claim(claim: str, snapshot: list[dict[str, Any]]) -> bool:
    compact = claim.lower().replace(",", "").replace(" ", "")
    approved_text = json.dumps(snapshot, sort_keys=True).lower().replace(",", "").replace(" ", "")
    if compact in approved_text:
        return True
    if compact.startswith("$"):
        match = re.fullmatch(r"\$([\d.]+)(k|m|million|thousand)?", compact)
        if not match:
            return False
        amount = float(match.group(1))
        suffix = match.group(2) or ""
        if suffix in {"k", "thousand"}:
            amount *= 1_000
        elif suffix in {"m", "million"}:
            amount *= 1_000_000
        allowed = {
            float(value)
            for row in snapshot
            for value in (row.get("amount_min"), row.get("amount_max"))
            if isinstance(value, int | float)
        }
        return any(abs(value - amount) < 0.01 for value in allowed)
    return False


def _validate_additional_blocked_phrases(
    *, subject: str, body: str, phrases: Iterable[str]
) -> None:
    def key(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return " ".join(re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).split())

    combined = key(f"{subject}\n{body}")
    for raw in phrases:
        phrase = key(str(raw or ""))
        if phrase and f" {phrase} " in f" {combined} ":
            raise ValueError(f"copy contains firm-blocked phrase: {raw}")


def validate_generated_copy(
    *,
    subject: str,
    body: str,
    catalog_snapshot: list[dict[str, Any]],
    additional_blocked_phrases: Iterable[str] = (),
) -> None:
    if not subject or len(subject) > 240 or not body or len(body) > 12_000:
        raise ValueError("model response has invalid subject/body length")
    if "\r" in subject or "\n" in subject:
        raise ValueError("email subject must be a single line")
    combined = f"{subject}\n{body}"
    lower = combined.lower()
    if any(phrase in lower for phrase in _FORBIDDEN_COPY):
        raise ValueError("model response contains an unsupported claim")
    if _UNSUPPORTED_FUNDING_TIMELINE_RE.search(combined):
        raise ValueError("model response contains an unsupported funding timeline")
    if _UNSUPPORTED_RECIPIENT_OUTCOME_RE.search(combined):
        raise ValueError("model response prequalifies the recipient or promises an outcome")
    if _RECIPIENT_RATE_RE.search(combined):
        raise ValueError("model response assigns a rate to the recipient")
    if _URL_RE.search(combined):
        raise ValueError("model response contains a link; links are application-owned")
    if _APPLICATION_OWNED_COPY_RE.search(combined):
        raise ValueError("model response contains application-owned unsubscribe language")
    if _SIGNATURE_RE.search(body):
        raise ValueError("model response contains an application-owned signature sign-off")
    if re.search(
        r"\b(?:guarantee|guaranteed|attached|attachments?|enclosures?)\b", lower
    ) or re.search(
        r"\b(?:(?:your|you are|you've|you have been)\s+approved|approval\s+(?:is|ready|confirmed))\b",
        lower,
    ):
        raise ValueError(
            "model response contains application-owned approval or attachment language"
        )

    if "sba micro" in lower or "microloan" in lower:
        for claim, suffix in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)\s*([kKmM]?)", combined):
            amount = float(claim.replace(",", ""))
            if suffix.lower() == "k":
                amount *= 1_000
            elif suffix.lower() == "m":
                amount *= 1_000_000
            if amount > 50_000:
                raise ValueError("SBA microloan claim exceeds the published maximum")

    # Every rate/amount in generated prose must occur in the approved catalog
    # snapshot.  Formatting is normalized to prevent '$50,000' vs '$50000'
    # from bypassing the comparison.
    for claim in _FINANCIAL_CLAIM_RE.findall(combined):
        if not _approved_financial_claim(claim, catalog_snapshot):
            raise ValueError(f"model response contains unapproved financial claim: {claim}")

    approved_text = " ".join(
        " ".join(re.sub(r"[^a-z0-9]+", " ", json.dumps(row, sort_keys=True).lower()).split())
        for row in catalog_snapshot
    )
    for match in _PRODUCT_CLAIM_RE.finditer(lower):
        claim = " ".join(re.sub(r"[^a-z0-9]+", " ", match.group(0)).split())
        if claim in _GENERIC_PRODUCT_PHRASES:
            continue
        if claim not in approved_text:
            raise ValueError(
                f"model response contains unapproved product language: {match.group(0)}"
            )

    # The model is allowed to personalize the introduction and call to action,
    # but it is never trusted to author capability or product language.  The
    # application appends exact names from the versioned catalog after this
    # validation step.  This intentionally fails closed even if a claimed
    # product sounds plausible.
    generic_safe = lower
    for phrase in _GENERIC_PRODUCT_PHRASES | {
        "financing resources",
        "financing information",
    }:
        generic_safe = generic_safe.replace(phrase, " ")
    if _AI_CAPABILITY_CLAIM_RE.search(generic_safe):
        raise ValueError("model response contains application-owned capability language")
    _validate_additional_blocked_phrases(
        subject=subject,
        body=body,
        phrases=additional_blocked_phrases,
    )


async def validate_current_draft_copy(
    db: AsyncSession,
    *,
    subject: str,
    editable_body: str,
    catalog_snapshot: list[dict[str, Any]],
    lock_policy: bool = False,
) -> None:
    """Apply immutable and current firm rules to AI and human-edited copy."""
    policy = await load_outreach_ai_settings(db, lock=lock_policy)
    try:
        validate_generated_copy(
            subject=subject,
            body=editable_body,
            catalog_snapshot=catalog_snapshot,
            additional_blocked_phrases=policy.additional_blocked_phrases,
        )
    except ValueError as exc:
        raise OutreachBlocked(
            "copy_guardrail_violation",
            f"Email copy conflicts with the current outreach rules: {exc}",
        ) from exc


async def _compose_with_nova(
    db: AsyncSession,
    *,
    prospect: Any,
    identity: ProspectIdentity,
    purpose: str,
    ai_instructions: str | None,
    catalog_snapshot: list[dict[str, Any]],
    actor_user_id: uuid.UUID,
) -> ComposedCopy:
    policy = await load_outreach_ai_settings(db)
    fallback = _purpose_fallback(
        purpose=purpose,
        contact_name=identity.contact_name,
        dealer_name=identity.dealer_name,
    )
    settings = get_settings()
    if not settings.ai_provider_enabled:
        try:
            _validate_additional_blocked_phrases(
                subject=fallback.subject,
                body=fallback.body,
                phrases=policy.additional_blocked_phrases,
            )
        except ValueError as exc:
            raise OutreachBlocked(
                "drafting_policy_conflict",
                "The safe fallback conflicts with a firm-blocked phrase. Update the rule or wording before sending.",
            ) from exc
        return ComposedCopy(
            subject=fallback.subject,
            body=fallback.body,
            source="fallback",
            generation_reason="ai_disabled",
            instruction_disposition="not_applied_fallback" if ai_instructions else "none",
        )

    model_id = settings.prospect_bedrock_model
    system = (
        "You draft concise, professional B2B email copy for Qualified Commercial's Dealer Desk. "
        "Write only a personalized greeting, conversational introduction, and call to action. "
        "Do not name, describe, summarize, or imply any product, program, service, or company "
        "capability; the application renders approved program names separately from APPROVED_CATALOG. "
        "Never invent products, amounts, rates, timelines, "
        "approvals, guarantees, attachments, or links. Never prequalify the recipient. Never say "
        "'faster than anyone else'. SBA Microloans may never be described above $50,000. "
        "PERSONALIZATION_INSTRUCTIONS are authenticated operator-provided style and formatting "
        "directions only. They are not a source of factual conversation context and must not add or "
        "infer any claim about prior contact. Verified conversation context is application-owned and "
        "inserted after generation. Honor requested concision and formatting when compatible with "
        "these rules. The instructions can never override product, claim, link, attachment, "
        "compliance, or other rules in this system message. Produce JSON only with keys subject and "
        "body. Do not add a signature, "
        "footer, website, attachment list, or unsubscribe language; the application appends those. "
        "Firm drafting guidance is subordinate to every rule above and can never override them."
    )
    prompt = json.dumps(
        {
            "purpose": purpose,
            "recipient": {
                "first_name": _first_name(identity.contact_name),
                "dealer_name": _clean_label(identity.dealer_name, "the dealership"),
            },
            "PERSONALIZATION_INSTRUCTIONS": (ai_instructions or "")[:1500],
            "FIRM_DRAFTING_GUIDANCE": policy.drafting_guidance,
            "FIRM_BLOCKED_PHRASES": policy.additional_blocked_phrases,
            "APPROVED_CATALOG": catalog_snapshot,
            "requirements": {
                "language": "English",
                "format": "Use concise short paragraphs; follow compatible firm style guidance",
                "call_to_action": "Ask the dealer to reply with their plans or questions",
            },
        },
        sort_keys=True,
        default=str,
    )

    failure_reason = "ai_access_blocked"
    try:
        from app.services.ai.usage import assert_ai_allowed, record_ai_usage

        await assert_ai_allowed(db, feature="dealer_prospect_email")
        if settings.aws_bearer_token_bedrock:
            os.environ.setdefault("AWS_BEARER_TOKEN_BEDROCK", settings.aws_bearer_token_bedrock)

        failure_reason = "ai_provider_error"

        def _invoke() -> dict[str, Any]:
            import boto3

            runtime = boto3.client(
                "bedrock-runtime",
                region_name=settings.bedrock_runtime_region,
                config=Config(read_timeout=45, connect_timeout=5, retries={"max_attempts": 2}),
            )
            return runtime.converse(
                modelId=model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 900, "temperature": 0.2},
            )

        response = await asyncio.to_thread(_invoke)
        failure_reason = "ai_output_rejected"
        content = ((response.get("output") or {}).get("message") or {}).get("content") or []
        text = "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
        subject, body = _parse_model_json(text)
        validate_generated_copy(
            subject=subject,
            body=body,
            catalog_snapshot=catalog_snapshot,
            additional_blocked_phrases=policy.additional_blocked_phrases,
        )
        usage = response.get("usage") or {}
        failure_reason = "ai_usage_record_failed"
        await record_ai_usage(
            db,
            feature="dealer_prospect_email",
            model=model_id,
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
            user_id=actor_user_id,
            metadata={"prospect_id": str(prospect.id), "purpose": purpose},
        )
        return ComposedCopy(
            subject=subject,
            body=body,
            source="ai",
            model_id=model_id,
            generation_reason="ai_generated",
            instruction_disposition="submitted_to_ai" if ai_instructions else "none",
        )
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ClientError):
            error_code = str((exc.response.get("Error") or {}).get("Code") or "")
            if error_code.casefold() in {
                "accessdenied",
                "accessdeniedexception",
                "forbiddenexception",
                "unauthorizedexception",
            }:
                failure_reason = "ai_access_blocked"
                log.warning(
                    "prospect outreach: Nova access denied; using safe fallback (code=%s)",
                    error_code,
                )
            else:
                log.warning(
                    "prospect outreach: Nova provider error; using safe fallback (code=%s)",
                    error_code or "unknown",
                )
        else:
            log.warning("prospect outreach: Nova draft failed; using safe fallback: %s", exc)
        try:
            _validate_additional_blocked_phrases(
                subject=fallback.subject,
                body=fallback.body,
                phrases=policy.additional_blocked_phrases,
            )
        except ValueError as policy_exc:
            raise OutreachBlocked(
                "drafting_policy_conflict",
                "The safe fallback conflicts with a firm-blocked phrase. Update the rule or wording before sending.",
            ) from policy_exc
        return ComposedCopy(
            subject=fallback.subject,
            body=fallback.body,
            source="fallback",
            generation_reason=failure_reason,
            instruction_disposition="not_applied_fallback" if ai_instructions else "none",
        )


async def _load_signature(db: AsyncSession, actor: User) -> list[str]:
    profile = (
        await db.execute(
            select(DealerFieldDeskProfile).where(DealerFieldDeskProfile.user_id == actor.id)
        )
    ).scalar_one_or_none()
    name = _clean_label(
        getattr(profile, "display_name", None) if profile else None,
        _clean_label(actor.name, "Dealer Desk"),
    )
    title = _clean_label(
        getattr(profile, "title", None) if profile else None,
        _clean_label(actor.title, "Relationship Manager"),
    )
    phone = _clean_label(
        getattr(profile, "phone", None) if profile else None,
        _clean_label(actor.phone, ""),
    )
    display_email = normalize_email(
        getattr(profile, "display_email", None) if profile else None
    ) or normalize_email(actor.email)
    return [part for part in (name, title, phone, display_email) if part]


def _locked_footer(
    *,
    signature: list[str],
    attachment_names: list[str],
    unsubscribe_url: str | None,
    booking_url: str | None = None,
    test_mode: bool = False,
) -> str:
    settings = get_settings()
    mailing_address = " ".join(str(getattr(settings, "prospect_mailing_address", "") or "").split())
    if not mailing_address:
        raise OutreachBlocked(
            "mailing_address_not_configured",
            "A valid company mailing address is required before Dealer Desk email can be sent.",
        )
    reply_contact = reply_contact_email(settings.prospect_reply_to_email)
    alternate_contact = normalize_email(getattr(settings, "prospect_alternate_contact_email", ""))
    parts: list[str] = []
    if test_mode:
        parts.extend(
            [
                "TEST EMAIL — no dealer was contacted and no pipeline automation was changed.",
                "",
            ]
        )
    if reply_contact:
        reply_note = (
            "Please reply directly to this email with any questions. "
            f"Replies are monitored at {reply_contact}."
        )
        if alternate_contact and alternate_contact != reply_contact:
            reply_note += f" You may also contact {alternate_contact}."
        parts.extend([reply_note, ""])
    parts.append(f"Learn more: {DEALER_WEBSITE}")
    if booking_url:
        parts.append(f"Book a time: {booking_url}")
    if attachment_names:
        parts.append("Attached for reference: " + ", ".join(attachment_names))
    parts.extend(["", "Best,", *signature])
    parts.extend(
        [
            "",
            "---",
            f"Qualified Commercial · {mailing_address}",
            (
                "This is a commercial message. Financing is subject to eligibility, lender review, "
                "underwriting, and documentation; no approval or terms are guaranteed."
            ),
            (
                f"Unsubscribe from Dealer Desk email: {unsubscribe_url}"
                if unsubscribe_url
                else "Test email only — live outreach includes a one-click unsubscribe link."
            ),
        ]
    )
    return "\n".join(parts).strip()


def _render_body(editable_body: str, locked_footer: str) -> str:
    editable = (editable_body or "").strip()
    # If the UI submits the previously rendered body, remove the exact locked
    # suffix before rebuilding it.  It can neither be duplicated nor edited.
    if locked_footer and editable.endswith(locked_footer):
        editable = editable[: -len(locked_footer)].rstrip()
    return f"{editable}\n\n{locked_footer}".strip()


def _footer_with_secure_bundle(footer: str, *, bundle_url: str, expires_at: datetime) -> str:
    lines = [
        line
        for line in (footer or "").splitlines()
        if not line.startswith("Attached for reference:")
        and not line.startswith("Secure dealer information bundle")
    ]
    learn_more_at = next(
        (index for index, line in enumerate(lines) if line.startswith("Learn more:")),
        None,
    )
    insert_at = learn_more_at + 1 if learn_more_at is not None else 0
    if len(lines) > insert_at and lines[insert_at].startswith("Book a time:"):
        insert_at += 1
    lines.insert(
        insert_at,
        f"Secure dealer information bundle (expires {expires_at.date().isoformat()}): {bundle_url}",
    )
    return "\n".join(lines).strip()


def _plain_html(value: str) -> str:
    escaped = html.escape(value)
    escaped = re.sub(
        r"(https://[^\s<]+)",
        lambda match: (
            f'<a href="{html.escape(match.group(1), quote=True)}">{html.escape(match.group(1))}</a>'
        ),
        escaped,
    )
    return (
        '<div style="font-family:Arial,sans-serif;line-height:1.5">'
        + escaped.replace("\n", "<br>")
        + "</div>"
    )


async def is_suppressed(db: AsyncSession, email: str) -> EmailSuppression | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    return (
        await db.execute(
            select(EmailSuppression).where(
                EmailSuppression.email_normalized == normalized,
                EmailSuppression.active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def set_suppression(
    db: AsyncSession,
    *,
    email: str,
    reason: str,
    source: str,
    actor_user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> EmailSuppression:
    normalized = normalize_email(email)
    if not valid_email(normalized):
        raise OutreachBlocked("invalid_email", "A valid email address is required.")
    # Serialize suppression/DNC writes with an in-flight prospect dispatcher.
    # The prospect row is the stable lock even before a suppression row exists.
    await db.execute(
        select(DealerProspect.id)
        .where(DealerProspect.email_normalized == normalized)
        .with_for_update()
    )
    row = (
        await db.execute(
            select(EmailSuppression)
            .where(EmailSuppression.email_normalized == normalized)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = EmailSuppression(
            email_normalized=normalized,
            reason=reason,
            source=source[:48],
            active=True,
            details=details or {},
            created_by_user_id=actor_user_id,
        )
        db.add(row)
    else:
        row.reason = reason
        row.source = source[:48]
        row.active = True
        row.details = details or {}
        row.revoked_at = None
        row.revoked_by_user_id = None
        row.created_by_user_id = actor_user_id or row.created_by_user_id
    await db.flush()
    return row


async def revoke_suppression(
    db: AsyncSession, row: EmailSuppression, *, actor_user_id: uuid.UUID
) -> EmailSuppression:
    row.active = False
    row.revoked_at = utcnow()
    row.revoked_by_user_id = actor_user_id
    await db.flush()
    return row


async def _active_collateral(db: AsyncSession) -> list[MarketingCollateralAsset]:
    return list(
        (
            await db.execute(
                select(MarketingCollateralAsset)
                .where(
                    MarketingCollateralAsset.assignment == COLLATERAL_ASSIGNMENT,
                    MarketingCollateralAsset.status == "active",
                    MarketingCollateralAsset.validation_status == "passed_antivirus",
                )
                .order_by(
                    MarketingCollateralAsset.sort_order,
                    MarketingCollateralAsset.logical_key,
                    MarketingCollateralAsset.version,
                )
            )
        )
        .scalars()
        .all()
    )


async def prospect_identity(db: AsyncSession, prospect: DealerProspect) -> ProspectIdentity:
    contact = await db.get(DealerRepContact, prospect.primary_contact_id)
    company = await db.get(DealerRepCompany, prospect.company_id)
    if contact is None or company is None:
        raise OutreachBlocked(
            "prospect_contact_incomplete", "The prospect's contact or dealership record is missing."
        )
    email = normalize_email(contact.email or prospect.email_normalized)
    return ProspectIdentity(
        contact_id=contact.id,
        contact_name=_clean_label(contact.full_name, "there"),
        dealer_name=_clean_label(company.name, "the dealership"),
        email=email,
        owner_user_id=prospect.owner_user_id,
    )


async def booking_url_for_draft(
    db: AsyncSession, *, creator_user_id: uuid.UUID, owner_user_id: uuid.UUID | None
) -> str | None:
    """Return a configured live booking URL, preferring the draft creator."""
    candidate_ids = list(dict.fromkeys([creator_user_id, owner_user_id]))
    candidate_ids = [value for value in candidate_ids if value is not None]
    if not candidate_ids:
        return None
    rows = list(
        (
            await db.execute(
                select(BookingSettings).where(
                    BookingSettings.user_id.in_(candidate_ids),
                    BookingSettings.enabled.is_(True),
                    BookingSettings.slug.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_user = {row.user_id: row for row in rows if row.slug}
    selected = next((by_user[user_id] for user_id in candidate_ids if user_id in by_user), None)
    if selected is None:
        return None
    base = get_settings().frontend_app_url.rstrip("/")
    return f"{base}/book/{quote(selected.slug or '', safe='')}" if base else None


async def create_draft(
    db: AsyncSession,
    *,
    prospect: Any,
    actor: User,
    payload: ProspectEmailDraftCreate,
) -> DealerProspectEmailDraft:
    fingerprint = request_fingerprint(prospect, payload)
    existing = (
        await db.execute(
            select(DealerProspectEmailDraft).where(
                DealerProspectEmailDraft.idempotency_key == payload.idempotency_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise OutreachConflict("Idempotency key was already used with a different request.")
        return existing

    if prospect.do_not_contact:
        raise OutreachBlocked(
            "do_not_contact", prospect.do_not_contact_reason or "Prospect is marked do not contact."
        )
    identity = await prospect_identity(db, prospect)
    recipient = identity.email
    if not valid_email(recipient):
        raise OutreachBlocked("invalid_recipient", "The prospect needs a valid email address.")
    suppression = await is_suppressed(db, recipient)
    if suppression is not None:
        raise OutreachBlocked(
            "email_suppressed", f"Email is suppressed ({suppression.reason.replace('_', ' ')})."
        )
    booking_url = None
    if payload.purpose == "booking":
        booking_url = await booking_url_for_draft(
            db,
            creator_user_id=actor.id,
            owner_user_id=prospect.owner_user_id,
        )
        if not booking_url:
            raise OutreachBlocked(
                "booking_link_unavailable",
                "Enable a booking page for the sender or prospect owner before drafting this email.",
            )

    catalog = await _active_catalog_snapshot(db)
    composed = await _compose_with_nova(
        db,
        prospect=prospect,
        identity=identity,
        purpose=payload.purpose,
        ai_instructions=payload.ai_instructions,
        catalog_snapshot=catalog,
        actor_user_id=actor.id,
    )
    body_with_context = _insert_verified_conversation_context(
        composed.body,
        payload.verified_conversation_context,
    )
    editable_body = (
        _with_approved_program_section(body_with_context, catalog)
        if payload.purpose == "dealer_information"
        else body_with_context
    )
    await validate_current_draft_copy(
        db,
        subject=composed.subject,
        editable_body=editable_body,
        catalog_snapshot=catalog,
    )

    assets = await _active_collateral(db)
    attachment_names = [asset.file_name for asset in assets]
    total_bytes = sum(int(asset.size_bytes) for asset in assets)
    settings = get_settings()
    now = utcnow()
    draft_id = uuid.uuid4()
    reply_token = secrets.token_urlsafe(18)
    unsubscribe_token = secrets.token_urlsafe(32)
    unsubscribe_url = (
        f"{settings.public_api_url.rstrip('/')}/api/v1/dealer-os/"
        f"prospect-email-unsubscribe/{quote(unsubscribe_token, safe='')}"
    )
    signature = await _load_signature(db, actor)
    locked_footer = _locked_footer(
        signature=signature,
        attachment_names=attachment_names,
        unsubscribe_url=unsubscribe_url,
        booking_url=booking_url,
    )
    rendered_body = _render_body(editable_body, locked_footer)
    oversized = attachment_bundle_too_large(total_bytes)
    draft = DealerProspectEmailDraft(
        id=draft_id,
        prospect_id=prospect.id,
        created_by_user_id=actor.id,
        recipient_email=recipient,
        from_email=normalize_email(settings.prospect_from_email),
        from_name=settings.prospect_from_name.strip() or "Qualified Commercial Dealer Desk",
        reply_to_email=tokenized_reply_to(settings.prospect_reply_to_email, reply_token),
        reply_token_hash=token_hash(reply_token),
        unsubscribe_token_hash=token_hash(unsubscribe_token),
        rfc_message_id=f"<prospect-{draft_id}@qualifiedcommercial.com>",
        subject=composed.subject[:240],
        editable_body=editable_body,
        locked_footer_text=locked_footer,
        body_text=rendered_body,
        body_html=_plain_html(rendered_body),
        ai_instructions=payload.ai_instructions,
        purpose=payload.purpose,
        draft_source=composed.source,
        model_id=composed.model_id,
        catalog_version=catalog_version(catalog),
        catalog_snapshot=catalog,
        status="blocked" if oversized else "pending_review",
        auto_send_at=(
            None
            if oversized
            else now + timedelta(seconds=max(1, settings.prospect_email_review_seconds))
        ),
        failure_code="attachment_bundle_too_large" if oversized else None,
        failure_detail=(
            "The complete approved PDF bundle exceeds the email delivery limit. "
            "No files were omitted; use an approved secure bundle link."
            if oversized
            else None
        ),
        idempotency_key=payload.idempotency_key,
        request_fingerprint=fingerprint,
        attachment_count=len(assets),
        attachment_total_bytes=total_bytes,
    )
    try:
        # The preflight lookup avoids wasted AI work in the common replay case;
        # the savepoint handles the true concurrent race on the database unique
        # key without rolling back the caller's prospect transition.
        async with db.begin_nested():
            db.add(draft)
            await db.flush()
    except IntegrityError as exc:
        existing = (
            await db.execute(
                select(DealerProspectEmailDraft).where(
                    DealerProspectEmailDraft.idempotency_key == payload.idempotency_key
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise OutreachConflict("The email draft conflicted with another request.") from exc
        if existing.request_fingerprint != fingerprint:
            raise OutreachConflict(
                "Idempotency key was already used with a different request."
            ) from exc
        return existing
    for asset in assets:
        db.add(
            DealerProspectEmailDraftAsset(
                draft_id=draft.id,
                asset_id=asset.id,
                asset_name=asset.name,
                asset_version=asset.version,
                file_name=asset.file_name,
                content_type=asset.content_type,
                size_bytes=asset.size_bytes,
                sha256=asset.sha256,
                validation_status=asset.validation_status,
                document_bytes=asset.document_bytes,
                sort_order=asset.sort_order,
            )
        )
    db.add(
        DealerProspectActivity(
            prospect_id=prospect.id,
            actor_user_id=actor.id,
            kind="email.draft_created",
            body=f"Email draft created: {draft.subject}",
            metadata_json={
                "draft_id": str(draft.id),
                "source": draft.draft_source,
                "generation_reason": composed.generation_reason,
                "instruction_disposition": composed.instruction_disposition,
                "send_after": draft.auto_send_at.isoformat() if draft.auto_send_at else None,
                "attachment_count": draft.attachment_count,
                "blocked": oversized,
            },
        )
    )
    prospect.last_activity_at = now
    await _record_private_note(
        db,
        prospect_id=prospect.id,
        actor_user_id=actor.id,
        note=payload.private_note,
        draft_id=draft.id,
    )
    await sync_draft_notifications(db, draft, prospect=prospect)
    await db.flush()
    return draft


def _test_email_message_id(actor_id: uuid.UUID, idempotency_key: uuid.UUID) -> str:
    return f"<prospect-test-{actor_id}-{idempotency_key}@qualifiedcommercial.com>"


async def _lock_test_email_actor(db: AsyncSession, actor_id: uuid.UUID) -> None:
    """Serialize one admin's quota and idempotency decisions in PostgreSQL."""
    digest = hashlib.sha256(f"dealer-prospect-test:{actor_id}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    await db.execute(select(func.pg_advisory_xact_lock(lock_key)))


_TEST_REASON_CODES = {
    "ai_generated": "g",
    "ai_disabled": "d",
    "ai_access_blocked": "a",
    "ai_provider_error": "p",
    "ai_output_rejected": "o",
    "ai_usage_record_failed": "u",
    "approved_fallback": "f",
}
_TEST_REASON_FROM_CODE = {value: key for key, value in _TEST_REASON_CODES.items()}
_TEST_DISPOSITION_CODES = {
    "none": "n",
    "submitted_to_ai": "s",
    "not_applied_fallback": "f",
}
_TEST_DISPOSITION_FROM_CODE = {
    value: key for key, value in _TEST_DISPOSITION_CODES.items()
}


def _test_template_key(
    purpose: str,
    composed: ComposedCopy,
    request_hash: str,
) -> str:
    """Persist safe generation diagnostics inside the existing ledger field."""
    reason = _TEST_REASON_CODES.get(composed.generation_reason, "f")
    disposition = _TEST_DISPOSITION_CODES.get(composed.instruction_disposition, "n")
    fingerprint = request_hash[:12].casefold()
    if not re.fullmatch(r"[0-9a-f]{12}", fingerprint):
        raise ValueError("test request fingerprint must be a SHA-256 hex digest")
    # Longest supported purpose remains below MessageSend.template_key's 64 chars.
    return f"prospect_test_{purpose}_{reason}_{disposition}_{fingerprint}_{composed.source}"


def _test_generation_metadata(template_key: str | None) -> tuple[str, str, str]:
    key = template_key or ""
    source = "ai" if key.endswith("_ai") else "fallback"
    match = re.search(
        r"_([gdapouf])_([nsf])_(?:[0-9a-f]{12}_)?(ai|fallback)$",
        key,
    )
    if match:
        return (
            match.group(3),
            _TEST_REASON_FROM_CODE.get(match.group(1), "fallback_reason_not_recorded"),
            _TEST_DISPOSITION_FROM_CODE.get(match.group(2), "unknown"),
        )
    return (
        source,
        "ai_generated" if source == "ai" else "fallback_reason_not_recorded",
        "unknown",
    )


def _test_template_fingerprint(template_key: str | None) -> str | None:
    match = re.search(r"_([0-9a-f]{12})_(?:ai|fallback)$", template_key or "")
    return match.group(1) if match else None


def _test_generation_banner(composed: ComposedCopy) -> str:
    reason_labels = {
        "ai_generated": "AI generation completed",
        "ai_disabled": "AI drafting is disabled",
        "ai_access_blocked": "AI drafting was unavailable",
        "ai_provider_error": "AI provider could not produce a draft",
        "ai_output_rejected": "AI output did not pass validation",
        "ai_usage_record_failed": "AI usage could not be recorded",
        "approved_fallback": "approved fallback requested",
    }
    generation = (
        "AI draft"
        if composed.source == "ai"
        else "Approved deterministic fallback"
    )
    if composed.instruction_disposition == "submitted_to_ai":
        instruction = "submitted to AI and checked by the email guardrails"
    elif composed.instruction_disposition == "not_applied_fallback":
        instruction = "NOT APPLIED because the approved fallback was used"
    else:
        instruction = "none supplied"
    return "\n".join(
        [
            "=== TEST DRAFT STATUS ===",
            f"Generation: {generation} ({reason_labels.get(composed.generation_reason, 'safe fallback')}).",
            f"One-off instructions: {instruction}.",
            "=========================",
        ]
    )


def _test_response_from_ledger(row: MessageSend) -> ProspectTestEmailResponse:
    source, generation_reason, instruction_disposition = _test_generation_metadata(
        row.template_key
    )
    delivery_state = (
        "sent"
        if row.status in {"sent", "delivered", "opened"}
        else "failed"
        if row.status in {"failed", "blocked", "bounced", "complained"}
        else "uncertain"
    )
    return ProspectTestEmailResponse(
        ok=delivery_state == "sent",
        delivery_state=delivery_state,
        to_email=row.to_email or "",
        subject=row.subject or "[TEST] Dealer outreach",
        draft_source=source,
        generation_reason=generation_reason,
        instruction_disposition=instruction_disposition,
        attachment_names=list(row.attachment_names or []),
        detail=(
            "Delivery is still in progress or its provider outcome is uncertain. "
            "No duplicate was sent; check the inbox before starting a separate test."
            if delivery_state == "uncertain"
            else row.detail or "Already processed; no duplicate was sent."
        ),
    )


async def send_test_email(
    db: AsyncSession,
    *,
    actor: User,
    payload: ProspectTestEmailRequest,
) -> ProspectTestEmailResponse:
    """Render the production recipe and durably send it only to the operator.

    The actor-scoped advisory lock makes quota and idempotency atomic. A queued
    MessageSend is committed before SES is called, so a timeout or crash leaves
    an ambiguous row that can never be retried with the same key.
    """
    recipient = normalize_email(actor.email)
    if not valid_email(recipient):
        raise OutreachBlocked(
            "invalid_test_recipient",
            "Your login account needs a valid email address before a test can be sent.",
        )
    request_hash = test_request_fingerprint(actor, payload)
    await _lock_test_email_actor(db, actor.id)
    test_message_id = _test_email_message_id(actor.id, payload.idempotency_key)
    existing = (
        await db.execute(
            select(MessageSend).where(
                MessageSend.context == "dealer_prospect_test",
                MessageSend.owner_user_id == actor.id,
                MessageSend.rfc_message_id == test_message_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        stored_fingerprint = _test_template_fingerprint(existing.template_key)
        # Old rows predate request-bound idempotency and remain replayable. New
        # rows must match the complete request before their result is reused.
        if stored_fingerprint is not None and stored_fingerprint != request_hash[:12]:
            raise OutreachConflict(
                "Idempotency key was already used with a different test email request."
            )
        return _test_response_from_ledger(existing)
    if await is_suppressed(db, recipient) is not None:
        raise OutreachBlocked(
            "test_recipient_suppressed",
            "Your login email is suppressed and cannot receive a test message.",
        )
    recent_since = utcnow() - timedelta(hours=1)
    recent_count = int(
        (
            await db.execute(
                select(func.count(MessageSend.id)).where(
                    MessageSend.context == "dealer_prospect_test",
                    MessageSend.owner_user_id == actor.id,
                    MessageSend.created_at >= recent_since,
                )
            )
        ).scalar_one()
        or 0
    )
    if recent_count >= TEST_EMAIL_HOURLY_LIMIT:
        raise OutreachBlocked(
            "test_email_rate_limited",
            f"Up to {TEST_EMAIL_HOURLY_LIMIT} dealer outreach tests may be sent per hour.",
        )

    identity = ProspectIdentity(
        contact_id=uuid.uuid4(),
        contact_name=payload.sample_contact_name,
        dealer_name=payload.sample_dealer_name,
        email=recipient,
        owner_user_id=actor.id,
    )
    catalog = await _active_catalog_snapshot(db)
    composed = await _compose_with_nova(
        db,
        prospect=SimpleNamespace(id=uuid.uuid4()),
        identity=identity,
        purpose=payload.purpose,
        ai_instructions=payload.ai_instructions,
        catalog_snapshot=catalog,
        actor_user_id=actor.id,
    )
    body_with_context = _insert_verified_conversation_context(
        composed.body,
        payload.verified_conversation_context,
    )
    editable_body = (
        _with_approved_program_section(body_with_context, catalog)
        if payload.purpose == "dealer_information"
        else body_with_context
    )
    await validate_current_draft_copy(
        db,
        subject=composed.subject,
        editable_body=editable_body,
        catalog_snapshot=catalog,
    )

    assets = await _active_collateral(db) if payload.include_collateral else []
    total_bytes = sum(int(asset.size_bytes) for asset in assets)
    if attachment_bundle_too_large(total_bytes):
        raise OutreachBlocked(
            "attachment_bundle_too_large",
            "The complete approved PDF bundle is too large for a test email. Turn off test attachments or update the collateral library.",
        )
    if any(
        len(bytes(asset.document_bytes)) != int(asset.size_bytes)
        or hashlib.sha256(bytes(asset.document_bytes)).hexdigest() != asset.sha256
        for asset in assets
    ):
        raise OutreachBlocked(
            "attachment_snapshot_corrupt",
            "Approved collateral failed its integrity check; no test email was sent.",
        )

    booking_url = None
    if payload.purpose == "booking":
        booking_url = await booking_url_for_draft(
            db,
            creator_user_id=actor.id,
            owner_user_id=actor.id,
        )
        if not booking_url:
            raise OutreachBlocked(
                "booking_link_unavailable",
                "Enable your booking page before testing the Booking link email.",
            )
    attachment_names = [asset.file_name for asset in assets]
    footer = _locked_footer(
        signature=await _load_signature(db, actor),
        attachment_names=attachment_names,
        unsubscribe_url=None,
        booking_url=booking_url,
        test_mode=True,
    )
    body_text = f"{_test_generation_banner(composed)}\n\n{_render_body(editable_body, footer)}"
    subject = f"[TEST] {composed.subject}"[:240]
    settings = get_settings()
    from_email = normalize_email(settings.prospect_from_email)
    reply_to = reply_contact_email(settings.prospect_reply_to_email)
    if not valid_email(from_email) or not valid_email(reply_to):
        raise OutreachBlocked(
            "sender_not_configured",
            "Dealer Desk sender and support Reply-To must be valid before a test can be sent.",
        )

    from app.services.messaging.outbox import Draft as OutboxDraft
    from app.services.messaging.outbox import Subject as OutboxSubject
    from app.services.messaging.outbox import record as record_outbox

    outbox_draft = OutboxDraft(
        to=recipient,
        subject=subject,
        body_text=body_text,
        body_html=_plain_html(body_text),
        attachments=[
            (asset.file_name, bytes(asset.document_bytes), asset.content_type) for asset in assets
        ],
        from_email=from_email,
        from_name=settings.prospect_from_name.strip() or "Qualified Commercial Dealer Desk",
        reply_to=reply_to,
        headers={"Message-ID": test_message_id},
    )
    ledger = await record_outbox(
        db,
        channel="email",
        status="queued",
        draft=outbox_draft,
        context="dealer_prospect_test",
        template_key=_test_template_key(payload.purpose, composed, request_hash),
        subject=OutboxSubject(owner_user_id=actor.id),
    )
    if ledger is None:
        raise OutreachBlocked(
            "test_ledger_unavailable",
            "The audited outbox could not reserve this test; no email was sent.",
        )
    await db.commit()

    from app.services.email import ses_client

    try:
        provider_result = ses_client.send_raw_email(
            to_emails=[recipient],
            subject=subject,
            body_text=body_text,
            body_html=outbox_draft.body_html,
            attachments=list(outbox_draft.attachments),
            source_email=from_email,
            source_name=outbox_draft.from_name,
            reply_to=reply_to,
            headers=dict(outbox_draft.headers),
        )
        ok = bool(provider_result.ok)
        provider_detail = provider_result.detail or ""
        message_id = provider_result.message_id
    except Exception as exc:  # noqa: BLE001
        log.exception("dealer prospect test email provider call failed")
        ok, provider_detail, message_id = False, f"send_failed: {exc}", None

    ledger.provider = "ses"
    ledger.provider_message_id = message_id
    delivery_state = "sent" if ok else "uncertain"
    if ok:
        detail = provider_detail
        ledger.status = "sent"
        ledger.detail = detail[:500]
        ledger.failed_at = None
    else:
        # SES has no idempotency token. A timeout or transport error can occur
        # after the provider accepted the message, so treating it as a safe
        # failure would make a retry capable of sending a duplicate. Preserve
        # the durable queued reservation until an operator explicitly starts a
        # separate attempt after checking the inbox.
        detail = (
            "SES did not return a definitive acceptance. No automatic retry was made because "
            "delivery may have occurred. Check the inbox before starting a separate test."
        )
        ledger.status = "queued"
        ledger.detail = f"delivery_uncertain: {provider_detail}"[:500]
        ledger.failed_at = None
    db.add(
        Activity(
            loan_id=None,
            actor_id=actor.id,
            actor_label=(
                actor.role.value if getattr(actor.role, "value", None) else str(actor.role)
            ),
            kind="prospect.outreach_test_email_attempted",
            summary=(
                f"Dealer Prospect {payload.purpose} test email "
                f"{'accepted by provider' if ok else 'has an uncertain provider outcome'}"
            ),
            payload={
                "recipient": recipient,
                "purpose": payload.purpose,
                "draft_source": composed.source,
                "generation_reason": composed.generation_reason,
                "instruction_disposition": composed.instruction_disposition,
                "attachment_count": len(assets),
                "provider_accepted": ok,
                "delivery_state": delivery_state,
                "idempotency_key": str(payload.idempotency_key),
            },
        )
    )
    await db.commit()
    return ProspectTestEmailResponse(
        ok=ok,
        delivery_state=delivery_state,
        to_email=recipient,
        subject=subject,
        draft_source=composed.source,
        generation_reason=composed.generation_reason,
        instruction_disposition=composed.instruction_disposition,
        attachment_names=attachment_names,
        detail=detail,
    )


async def _record_private_note(
    db: AsyncSession,
    *,
    prospect_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    note: str | None,
    draft_id: uuid.UUID,
) -> None:
    """Write the composer's private note without ever placing it on the draft.

    Imported lazily so outreach can be tested independently while the core
    prospect migration/module is developed in parallel.
    """
    if not note:
        return
    db.add(
        DealerProspectActivity(
            prospect_id=prospect_id,
            actor_user_id=actor_user_id,
            kind="internal_note",
            body=note,
            metadata_json={"draft_id": str(draft_id), "private": True},
        )
    )


async def load_draft(
    db: AsyncSession, draft_id: uuid.UUID, *, lock: bool = False
) -> DealerProspectEmailDraft:
    stmt = select(DealerProspectEmailDraft).where(DealerProspectEmailDraft.id == draft_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise OutreachNotFound("Email draft not found.")
    return row


def _check_version(row: DealerProspectEmailDraft, expected: int | None) -> None:
    if expected is not None and int(row.version) != int(expected):
        raise OutreachConflict(
            f"Draft changed (expected version {expected}, current version {row.version})."
        )


async def start_editing(
    db: AsyncSession, draft_id: uuid.UUID, *, expected_version: int | None
) -> DealerProspectEmailDraft:
    row = await load_draft(db, draft_id, lock=True)
    _check_version(row, expected_version)
    if row.status == "editing":
        return row
    if row.status != "pending_review":
        raise OutreachConflict(f"A {row.status} draft cannot enter edit mode.")
    row.status = "editing"
    row.auto_send_at = None
    row.review_stopped_at = utcnow()
    row.version += 1
    await sync_draft_notifications(db, row)
    await db.flush()
    return row


async def edit_draft(
    db: AsyncSession,
    draft_id: uuid.UUID,
    *,
    expected_version: int,
    subject: str | None,
    body: str | None,
) -> DealerProspectEmailDraft:
    row = await load_draft(db, draft_id, lock=True)
    _check_version(row, expected_version)
    if row.status not in {"pending_review", "editing"}:
        raise OutreachConflict(f"A {row.status} draft cannot be edited.")
    if subject is None and body is None:
        raise OutreachConflict("Provide a subject or body to edit.")
    next_subject = subject.strip()[:240] if subject is not None else row.subject
    next_body = row.editable_body
    if body is not None:
        editable = body.strip()
        if row.locked_footer_text and editable.endswith(row.locked_footer_text):
            editable = editable[: -len(row.locked_footer_text)].rstrip()
        if not editable:
            raise OutreachConflict("The editable email body cannot be blank.")
        next_body = editable
    await validate_current_draft_copy(
        db,
        subject=next_subject,
        editable_body=next_body,
        catalog_snapshot=row.catalog_snapshot or [],
    )
    if row.status == "pending_review":
        row.status = "editing"
        row.auto_send_at = None
        row.review_stopped_at = utcnow()
    row.subject = next_subject
    row.editable_body = next_body
    row.body_text = _render_body(row.editable_body, row.locked_footer_text)
    row.body_html = _plain_html(row.body_text)
    row.version += 1
    await sync_draft_notifications(db, row)
    await db.flush()
    return row


async def cancel_draft(
    db: AsyncSession, draft_id: uuid.UUID, *, expected_version: int | None
) -> DealerProspectEmailDraft:
    row = await load_draft(db, draft_id, lock=True)
    _check_version(row, expected_version)
    if row.status == "cancelled":
        return row
    if row.status not in {"pending_review", "editing", "blocked", "failed"}:
        raise OutreachConflict(f"A {row.status} draft cannot be cancelled.")
    row.status = "cancelled"
    row.auto_send_at = None
    row.cancelled_at = utcnow()
    row.secure_bundle_token_hash = None
    row.secure_bundle_expires_at = None
    row.version += 1
    await sync_draft_notifications(db, row)
    await db.flush()
    return row


async def select_secure_bundle(
    db: AsyncSession,
    draft_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
    expected_version: int | None,
) -> DealerProspectEmailDraft:
    """Explicitly replace an oversized PDF attachment set with one ZIP link."""
    row = await load_draft(db, draft_id, lock=True)
    _check_version(row, expected_version)
    if row.delivery_mode == "secure_link" and row.status == "pending_review":
        return row
    if row.status != "blocked" or row.failure_code != "attachment_bundle_too_large":
        raise OutreachConflict("Secure bundle is available only for an oversized blocked draft.")
    count = int(
        (
            await db.execute(
                select(func.count(DealerProspectEmailDraftAsset.id)).where(
                    DealerProspectEmailDraftAsset.draft_id == row.id,
                    DealerProspectEmailDraftAsset.validation_status == "passed_antivirus",
                )
            )
        ).scalar_one()
    )
    if count != int(row.attachment_count) or count < 1:
        raise OutreachBlocked(
            "attachment_snapshot_mismatch",
            "The exact collateral snapshot is incomplete; a secure bundle was not created.",
        )
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + timedelta(days=max(1, settings.prospect_secure_bundle_days))
    bundle_url = (
        f"{settings.public_api_url.rstrip('/')}/api/v1/dealer-os/"
        f"prospect-email-bundles/{quote(token, safe='')}"
    )
    row.delivery_mode = "secure_link"
    row.secure_bundle_token_hash = token_hash(token)
    row.secure_bundle_expires_at = expires
    row.secure_bundle_selected_at = now
    row.secure_bundle_selected_by_user_id = actor_user_id
    row.locked_footer_text = _footer_with_secure_bundle(
        row.locked_footer_text, bundle_url=bundle_url, expires_at=expires
    )
    row.body_text = _render_body(row.editable_body, row.locked_footer_text)
    row.body_html = _plain_html(row.body_text)
    row.status = "pending_review"
    row.auto_send_at = now + timedelta(seconds=max(1, settings.prospect_email_review_seconds))
    row.review_stopped_at = None
    row.failure_code = None
    row.failure_detail = None
    row.version += 1
    db.add(
        DealerProspectActivity(
            prospect_id=row.prospect_id,
            actor_user_id=actor_user_id,
            kind="email.secure_bundle_selected",
            body="Oversized collateral changed to an expiring secure bundle link.",
            metadata_json={
                "draft_id": str(row.id),
                "attachment_count": row.attachment_count,
                "attachment_total_bytes": row.attachment_total_bytes,
                "expires_at": expires.isoformat(),
            },
        )
    )
    await sync_draft_notifications(db, row)
    await db.flush()
    return row


async def load_secure_bundle(
    db: AsyncSession, token: str, *, lock: bool = False
) -> tuple[DealerProspectEmailDraft, list[DealerProspectEmailDraftAsset]]:
    digest = token_hash(token)
    stmt = select(DealerProspectEmailDraft).where(
        DealerProspectEmailDraft.secure_bundle_token_hash == digest,
        DealerProspectEmailDraft.delivery_mode == "secure_link",
    )
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise OutreachNotFound("Secure bundle not found.")
    if row.status not in {"sending", "sent"}:
        # The URL is rendered into the review copy before delivery, but the
        # public resource does not become usable until dispatch has claimed it.
        raise OutreachNotFound("Secure bundle not found.")
    if row.secure_bundle_expires_at is None or row.secure_bundle_expires_at <= utcnow():
        raise OutreachBlocked("secure_bundle_expired", "This secure bundle link has expired.")
    assets = list(
        (
            await db.execute(
                select(DealerProspectEmailDraftAsset)
                .where(DealerProspectEmailDraftAsset.draft_id == row.id)
                .order_by(
                    DealerProspectEmailDraftAsset.sort_order,
                    DealerProspectEmailDraftAsset.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if len(assets) != int(row.attachment_count):
        raise OutreachBlocked("attachment_snapshot_mismatch", "The secure bundle is incomplete.")
    if any(item.validation_status != "passed_antivirus" for item in assets):
        raise OutreachBlocked(
            "attachment_not_antivirus_scanned",
            "The secure bundle includes collateral without a verified antivirus scan.",
        )
    return row, assets


async def _block_draft(
    db: AsyncSession, row: DealerProspectEmailDraft, *, code: str, detail: str
) -> DealerProspectEmailDraft:
    row.status = "blocked"
    row.auto_send_at = None
    row.failure_code = code
    row.failure_detail = detail
    row.secure_bundle_token_hash = None
    row.secure_bundle_expires_at = None
    row.version += 1
    await sync_draft_notifications(db, row)
    await db.commit()
    return row


async def _advance_new_prospect_after_delivery(
    db: AsyncSession,
    row: DealerProspectEmailDraft,
) -> DealerProspect | None:
    """Move New -> Emailed only after SES reports successful delivery.

    The prospect is reloaded under a row lock because the provider call occurs
    outside a database transaction. A concurrent outcome or board move wins;
    this helper never overwrites any stage other than the still-current New.
    """
    prospect = (
        await db.execute(
            select(DealerProspect)
            .where(DealerProspect.id == row.prospect_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if prospect is None or prospect.archived_at is not None:
        return prospect
    current_stage = await db.get(DealerProspectStageDefinition, prospect.stage_definition_id)
    if current_stage is None or current_stage.key != "new":
        return prospect
    emailed = (
        await db.execute(
            select(DealerProspectStageDefinition).where(
                DealerProspectStageDefinition.key == "emailed",
                DealerProspectStageDefinition.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if emailed is None:
        log.error("successful prospect email could not advance: Emailed stage is unavailable")
        return prospect
    before_version = int(prospect.version)
    prospect.stage_definition_id = emailed.id
    prospect.version = before_version + 1
    at = utcnow()
    prospect.last_activity_at = at
    db.add(
        DealerProspectActivity(
            prospect_id=prospect.id,
            actor_user_id=row.approved_by_user_id or row.created_by_user_id,
            kind="stage_moved",
            body="Moved automatically after successful dealer email delivery.",
            metadata_json={
                "from_stage_key": "new",
                "to_stage_key": "emailed",
                "selected_action": "email_delivery",
                "draft_id": str(row.id),
                "version_before": before_version,
                "version_after": prospect.version,
                "reversible": False,
            },
        )
    )
    return prospect


async def dispatch_draft(
    db: AsyncSession,
    draft_id: uuid.UUID,
    *,
    expected_version: int | None = None,
    approved_by_user_id: uuid.UUID | None = None,
    automatic: bool = False,
) -> DealerProspectEmailDraft:
    """Claim, commit, and deliver one draft at most once."""
    row = await load_draft(db, draft_id, lock=True)
    _check_version(row, expected_version)
    if row.status in {"sent", "sending"}:
        return row
    allowed = {"pending_review"} if automatic else {"pending_review", "editing"}
    if row.status not in allowed:
        raise OutreachConflict(f"A {row.status} draft cannot be sent.")
    now = utcnow()
    if automatic and (row.auto_send_at is None or row.auto_send_at > now):
        return row
    if not get_settings().dealer_prospect_pipeline_enabled:
        return await _block_draft(
            db,
            row,
            code="pipeline_disabled",
            detail=(
                "Dealer Prospect outreach was disabled before delivery. Nothing was sent; "
                "create a new draft after the pipeline is enabled."
            ),
        )
    try:
        await validate_current_draft_copy(
            db,
            subject=row.subject,
            editable_body=row.editable_body,
            catalog_snapshot=row.catalog_snapshot or [],
        )
    except OutreachBlocked as exc:
        return await _block_draft(db, row, code=exc.code, detail=exc.detail)
    if not re.search(r"(?m)^Qualified Commercial · \S.+$", row.locked_footer_text or ""):
        return await _block_draft(
            db,
            row,
            code="compliance_footer_missing",
            detail="Required company mailing address is missing from the locked email footer.",
        )
    if not valid_email(row.recipient_email):
        return await _block_draft(
            db, row, code="invalid_recipient", detail="Recipient address is no longer valid."
        )
    suppression = await is_suppressed(db, row.recipient_email)
    if suppression is not None:
        return await _block_draft(
            db,
            row,
            code="email_suppressed",
            detail=f"Recipient is suppressed ({suppression.reason.replace('_', ' ')}).",
        )
    prospect = await db.get(DealerProspect, row.prospect_id, with_for_update=True)
    if prospect is None or prospect.archived_at is not None:
        return await _block_draft(
            db, row, code="prospect_unavailable", detail="Prospect is archived or unavailable."
        )
    if prospect.do_not_contact:
        return await _block_draft(
            db,
            row,
            code="do_not_contact",
            detail=prospect.do_not_contact_reason or "Prospect is marked do not contact.",
        )
    identity = await prospect_identity(db, prospect)
    if identity.email != normalize_email(row.recipient_email):
        return await _block_draft(
            db,
            row,
            code="recipient_changed",
            detail="Prospect email changed after this draft was created; nothing was sent.",
        )
    if not valid_email(row.from_email) or not valid_email(row.reply_to_email):
        return await _block_draft(
            db, row, code="sender_not_configured", detail="Dealer Desk sender is not configured."
        )
    actor = await db.get(User, row.created_by_user_id) if row.created_by_user_id else None
    if actor is None or actor.deleted_at is not None or actor.account_status != "active":
        return await _block_draft(
            db,
            row,
            code="creator_not_authorized",
            detail="The draft creator is no longer authorized to send prospect email.",
        )
    try:
        # Re-run the same owner/assignment/role predicate used by interactive
        # Field Desk reads. Creating a draft is not evergreen permission to
        # send it after reassignment or a role change.
        from app.dealer_os.services.prospects import load_visible_prospect

        await load_visible_prospect(db, actor, prospect.id)
    except HTTPException:
        return await _block_draft(
            db,
            row,
            code="creator_not_authorized",
            detail="The draft creator no longer has access to this prospect.",
        )
    snapshots = list(
        (
            await db.execute(
                select(DealerProspectEmailDraftAsset)
                .where(DealerProspectEmailDraftAsset.draft_id == row.id)
                .order_by(
                    DealerProspectEmailDraftAsset.sort_order, DealerProspectEmailDraftAsset.id
                )
            )
        )
        .scalars()
        .all()
    )
    actual_total = sum(int(item.size_bytes) for item in snapshots)
    if actual_total != int(row.attachment_total_bytes) or len(snapshots) != int(
        row.attachment_count
    ):
        return await _block_draft(
            db,
            row,
            code="attachment_snapshot_mismatch",
            detail="The immutable collateral snapshot is incomplete; nothing was sent.",
        )
    if any(item.validation_status != "passed_antivirus" for item in snapshots):
        return await _block_draft(
            db,
            row,
            code="attachment_not_antivirus_scanned",
            detail=(
                "An attachment snapshot lacks a verified managed antivirus scan; nothing was sent."
            ),
        )
    corrupted = next(
        (
            item
            for item in snapshots
            if hashlib.sha256(bytes(item.document_bytes)).hexdigest() != item.sha256
            or len(bytes(item.document_bytes)) != int(item.size_bytes)
        ),
        None,
    )
    if corrupted is not None:
        return await _block_draft(
            db,
            row,
            code="attachment_snapshot_corrupt",
            detail=(
                "An immutable collateral snapshot failed its size or SHA-256 integrity check; "
                "nothing was sent."
            ),
        )
    if row.delivery_mode == "attachments" and attachment_bundle_too_large(actual_total):
        return await _block_draft(
            db,
            row,
            code="attachment_bundle_too_large",
            detail=(
                "The complete approved PDF bundle exceeds the email delivery limit. "
                "No files were omitted; use an approved secure bundle link."
            ),
        )
    if row.delivery_mode == "secure_link" and (
        not row.secure_bundle_token_hash
        or row.secure_bundle_expires_at is None
        or row.secure_bundle_expires_at <= now
    ):
        return await _block_draft(
            db,
            row,
            code="secure_bundle_expired",
            detail="The selected secure collateral bundle expired before delivery.",
        )

    # Commit the irreversible claim before touching the provider.  No later
    # scheduler tick is allowed to retry a row left in ``sending``.
    row.status = "sending"
    row.auto_send_at = None
    row.dispatch_started_at = now
    row.approved_at = now if approved_by_user_id else row.approved_at
    row.approved_by_user_id = approved_by_user_id or row.approved_by_user_id
    row.failure_code = None
    row.failure_detail = None
    row.version += 1
    await sync_draft_notifications(db, row, prospect=prospect)
    await db.commit()

    # The irreversible claim is durable. Re-resolve every mutable permission,
    # recipient, policy, and attachment fact in a fresh transaction immediately
    # before the provider call. Keep the draft/prospect locks through delivery,
    # so an administrative change cannot slip between this check and SES.
    current = await load_draft(db, row.id, lock=True)
    if current.status != "sending":
        return current
    if not get_settings().dealer_prospect_pipeline_enabled:
        return await _block_draft(
            db,
            current,
            code="pipeline_disabled",
            detail="Dealer Prospect outreach was disabled before provider delivery.",
        )
    try:
        await validate_current_draft_copy(
            db,
            subject=current.subject,
            editable_body=current.editable_body,
            catalog_snapshot=current.catalog_snapshot or [],
            lock_policy=True,
        )
    except OutreachBlocked as exc:
        return await _block_draft(db, current, code=exc.code, detail=exc.detail)
    if not re.search(r"(?m)^Qualified Commercial · \S.+$", current.locked_footer_text or ""):
        return await _block_draft(
            db,
            current,
            code="compliance_footer_missing",
            detail="Required company mailing address is missing from the locked email footer.",
        )
    latest_prospect = await db.get(DealerProspect, current.prospect_id, with_for_update=True)
    if (
        latest_prospect is None
        or latest_prospect.archived_at is not None
        or latest_prospect.do_not_contact
    ):
        return await _block_draft(
            db,
            current,
            code="do_not_contact" if latest_prospect is not None else "prospect_unavailable",
            detail=(
                latest_prospect.do_not_contact_reason
                if latest_prospect is not None and latest_prospect.do_not_contact_reason
                else "Prospect is marked do not contact, archived, or unavailable."
            ),
        )
    # Suppression writers lock the matching prospect before inserting or
    # changing a suppression row. Holding that same lock before this read
    # closes the final no-row/read race through provider delivery.
    latest_suppression = await is_suppressed(db, current.recipient_email)
    if latest_suppression is not None:
        return await _block_draft(
            db,
            current,
            code="email_suppressed",
            detail=f"Recipient is suppressed ({latest_suppression.reason.replace('_', ' ')}).",
        )
    latest_identity = await prospect_identity(db, latest_prospect)
    if latest_identity.email != normalize_email(current.recipient_email):
        return await _block_draft(
            db,
            current,
            code="recipient_changed",
            detail="Prospect email changed immediately before delivery; nothing was sent.",
        )
    latest_actor = (
        await db.get(User, current.created_by_user_id, with_for_update=True)
        if current.created_by_user_id
        else None
    )
    if (
        latest_actor is None
        or latest_actor.deleted_at is not None
        or latest_actor.account_status != "active"
    ):
        return await _block_draft(
            db,
            current,
            code="creator_not_authorized",
            detail="The draft creator is no longer authorized to send prospect email.",
        )
    try:
        from app.dealer_os.services.prospects import load_visible_prospect

        await load_visible_prospect(db, latest_actor, latest_prospect.id)
    except HTTPException:
        return await _block_draft(
            db,
            current,
            code="creator_not_authorized",
            detail="The draft creator lost prospect access before provider delivery.",
        )
    latest_snapshots = list(
        (
            await db.execute(
                select(DealerProspectEmailDraftAsset)
                .where(DealerProspectEmailDraftAsset.draft_id == current.id)
                .order_by(
                    DealerProspectEmailDraftAsset.sort_order,
                    DealerProspectEmailDraftAsset.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    latest_total = sum(int(item.size_bytes) for item in latest_snapshots)
    if (
        latest_total != int(current.attachment_total_bytes)
        or len(latest_snapshots) != int(current.attachment_count)
        or any(item.validation_status != "passed_antivirus" for item in latest_snapshots)
        or any(
            len(bytes(item.document_bytes)) != int(item.size_bytes)
            or hashlib.sha256(bytes(item.document_bytes)).hexdigest() != item.sha256
            for item in latest_snapshots
        )
    ):
        return await _block_draft(
            db,
            current,
            code="attachment_snapshot_changed",
            detail="The immutable collateral snapshot changed before provider delivery.",
        )
    if current.delivery_mode == "attachments" and attachment_bundle_too_large(latest_total):
        return await _block_draft(
            db,
            current,
            code="attachment_bundle_too_large",
            detail="The complete approved PDF bundle exceeds the delivery limit.",
        )
    if current.delivery_mode == "secure_link" and (
        not current.secure_bundle_token_hash
        or current.secure_bundle_expires_at is None
        or current.secure_bundle_expires_at <= utcnow()
    ):
        return await _block_draft(
            db,
            current,
            code="secure_bundle_expired",
            detail="The selected secure collateral bundle expired before provider delivery.",
        )
    row = current
    prospect = latest_prospect
    identity = latest_identity
    snapshots = latest_snapshots

    from app.services.messaging.outbox import Draft as OutboxDraft
    from app.services.messaging.outbox import Subject as OutboxSubject
    from app.services.messaging.outbox import deliver_email

    unsubscribe_url = _unsubscribe_url_from_footer(row.locked_footer_text)
    headers = {
        "Message-ID": row.rfc_message_id,
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    attachments = (
        []
        if row.delivery_mode == "secure_link"
        else [(item.file_name, bytes(item.document_bytes), item.content_type) for item in snapshots]
    )
    outcome = await deliver_email(
        db,
        OutboxDraft(
            to=row.recipient_email,
            subject=row.subject,
            body_text=row.body_text,
            body_html=row.body_html,
            attachments=attachments,
            from_email=row.from_email,
            from_name=row.from_name,
            reply_to=row.reply_to_email,
            headers=headers,
        ),
        context="dealer_prospect",
        template_key=f"prospect_{row.purpose}",
        subject=OutboxSubject(
            owner_user_id=identity.owner_user_id or row.created_by_user_id,
            prospect_id=row.prospect_id,
            contact_id=identity.contact_id,
            prospect_draft_id=row.id,
        ),
    )
    # Re-lock after the provider call.  ``sending`` is the only state accepted
    # here, so a future reconciliation tool cannot be overwritten accidentally.
    current = await load_draft(db, row.id, lock=True)
    if current.status == "sending":
        current.provider = "ses"
        current.provider_message_id = outcome.message_id
        current.status = "sent" if outcome.ok else "failed"
        current.sent_at = utcnow() if outcome.ok else None
        current.failure_code = None if outcome.ok else "provider_failed"
        current.failure_detail = (
            None if outcome.ok else (outcome.detail or "SES send failed")[:2000]
        )
        if not outcome.ok:
            current.secure_bundle_token_hash = None
            current.secure_bundle_expires_at = None
        current.version += 1
        current_prospect = (
            await _advance_new_prospect_after_delivery(db, current) if outcome.ok else prospect
        )
        db.add(
            DealerProspectActivity(
                prospect_id=current.prospect_id,
                actor_user_id=approved_by_user_id,
                kind="email.sent" if outcome.ok else "email.failed",
                body=(
                    f"Email sent: {current.subject}"
                    if outcome.ok
                    else f"Email delivery failed: {current.subject}"
                ),
                metadata_json={
                    "draft_id": str(current.id),
                    "provider": current.provider,
                    "provider_message_id": current.provider_message_id,
                    "automatic": automatic,
                    "error": current.failure_detail,
                },
            )
        )
        if current_prospect is not None:
            current_prospect.last_activity_at = utcnow()
        await sync_draft_notifications(db, current, prospect=current_prospect)
    await db.commit()
    return current


def _unsubscribe_url_from_footer(footer: str) -> str:
    marker = "Unsubscribe from Dealer Desk email: "
    for line in (footer or "").splitlines():
        if line.startswith(marker):
            value = line[len(marker) :].strip()
            if value.startswith("https://") or value.startswith("http://"):
                return value
    raise OutreachBlocked("unsubscribe_link_missing", "Required unsubscribe link is missing.")


async def dispatch_due_drafts(*, limit: int = 40) -> int:
    """Scheduler entry point.  Each draft receives its own transaction."""
    from app.db import SessionLocal

    async with SessionLocal() as scan_db:
        ids = list(
            (
                await scan_db.execute(
                    select(DealerProspectEmailDraft.id)
                    .where(
                        DealerProspectEmailDraft.status == "pending_review",
                        DealerProspectEmailDraft.auto_send_at.is_not(None),
                        DealerProspectEmailDraft.auto_send_at <= utcnow(),
                    )
                    .order_by(DealerProspectEmailDraft.auto_send_at, DealerProspectEmailDraft.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    sent = 0
    for draft_id in ids:
        async with SessionLocal() as db:
            try:
                row = await dispatch_draft(db, draft_id, automatic=True)
                sent += int(row.status == "sent")
            except Exception:  # noqa: BLE001
                await db.rollback()
                log.exception("prospect outreach scheduler failed draft=%s", draft_id)
    return sent


async def draft_read(db: AsyncSession, row: DealerProspectEmailDraft) -> ProspectEmailDraftRead:
    names = list(
        (
            await db.execute(
                select(DealerProspectEmailDraftAsset.file_name)
                .where(DealerProspectEmailDraftAsset.draft_id == row.id)
                .order_by(
                    DealerProspectEmailDraftAsset.sort_order, DealerProspectEmailDraftAsset.id
                )
            )
        )
        .scalars()
        .all()
    )
    countdown: int | None = None
    if row.status == "pending_review" and row.auto_send_at is not None:
        countdown = max(0, int((row.auto_send_at - utcnow()).total_seconds()))
    error = row.failure_detail
    generation_reason = "ai_generated" if row.draft_source == "ai" else "approved_fallback"
    instruction_disposition = (
        "submitted_to_ai"
        if row.draft_source == "ai" and row.ai_instructions
        else "not_applied_fallback"
        if row.draft_source == "fallback" and row.ai_instructions
        else "none"
    )
    return ProspectEmailDraftRead(
        id=row.id,
        prospect_id=row.prospect_id,
        to_email=row.recipient_email,
        from_email=formataddr((row.from_name, row.from_email)),
        reply_to=row.reply_to_email,
        subject=row.subject,
        body=row.body_text,
        status=row.status,
        send_after=row.auto_send_at,
        review_stopped_at=row.review_stopped_at,
        approved_at=row.approved_at,
        sent_at=row.sent_at,
        cancelled_at=row.cancelled_at,
        countdown_seconds=countdown,
        version=row.version,
        draft_source=row.draft_source,
        generation_reason=generation_reason,
        instruction_disposition=instruction_disposition,
        model_id=row.model_id,
        error=error,
        failure_code=row.failure_code,
        attachment_names=names,
        attachment_count=row.attachment_count,
        attachment_total_bytes=row.attachment_total_bytes,
        delivery_mode=row.delivery_mode,
        secure_bundle_link_required=row.failure_code == "attachment_bundle_too_large",
        secure_bundle_expires_at=row.secure_bundle_expires_at,
        created_at=row.created_at,
    )


def validate_pdf(data: bytes) -> PdfValidation:
    settings = get_settings()
    if not data or len(data) > settings.prospect_email_max_attachment_bytes:
        raise OutreachBlocked(
            "invalid_pdf_size",
            f"PDF must be between 1 and {settings.prospect_email_max_attachment_bytes} bytes.",
        )
    if not data.startswith(b"%PDF-"):
        raise OutreachBlocked("invalid_pdf", "Only structurally valid PDF files are accepted.")
    if _EICAR in data or any(marker in data for marker in _ACTIVE_PDF_MARKERS):
        raise OutreachBlocked(
            "unsafe_pdf", "PDF contains malware-test or active-content markers and was rejected."
        )
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise OutreachBlocked(
                "password_protected_pdf", "Password-protected PDFs are not allowed."
            )
        if len(reader.pages) < 1:
            raise OutreachBlocked("invalid_pdf", "PDF contains no pages.")
        # Force page-tree traversal so truncated xref/page objects fail now,
        # before the asset can be approved or snapshotted.
        for page in reader.pages:
            _ = page.mediabox
    except OutreachBlocked:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OutreachBlocked("invalid_pdf", f"PDF integrity validation failed: {exc}") from exc
    return PdfValidation(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        status="passed_static",
        detail="PDF integrity, encryption, EICAR, and active-content checks passed.",
    )


async def scan_pdf_with_clamd(data: bytes) -> str:
    """Run a fail-closed ClamAV INSTREAM scan and return the engine response."""
    settings = get_settings()
    host = settings.prospect_collateral_clamd_host.strip()
    if not host:
        raise OutreachBlocked(
            "malware_scanner_unavailable",
            "Collateral uploads are disabled until the managed malware scanner is configured.",
        )
    timeout = max(1.0, float(settings.prospect_collateral_clamd_timeout_seconds))
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(settings.prospect_collateral_clamd_port)),
            timeout=timeout,
        )
        writer.write(b"zINSTREAM\0")
        for offset in range(0, len(data), 1024 * 1024):
            chunk = data[offset : offset + 1024 * 1024]
            writer.write(struct.pack("!I", len(chunk)))
            writer.write(chunk)
        writer.write(struct.pack("!I", 0))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        raw = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=timeout)
    except OutreachBlocked:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OutreachBlocked(
            "malware_scanner_unavailable",
            "The managed malware scanner could not verify this PDF. Try again later.",
        ) from exc
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    response = raw.rstrip(b"\0\r\n").decode("utf-8", errors="replace")
    if response == "stream: OK":
        return response
    if " FOUND" in response:
        signature = response.rsplit(":", 1)[-1].replace("FOUND", "").strip()
        raise OutreachBlocked(
            "unsafe_pdf",
            f"The malware scanner rejected this PDF ({signature or 'threat detected'}).",
        )
    raise OutreachBlocked(
        "malware_scanner_unavailable",
        "The managed malware scanner returned an indeterminate result; the PDF was not accepted.",
    )


def logical_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    if not key:
        raise OutreachBlocked("invalid_name", "Collateral name must contain letters or numbers.")
    return key[:120]


def _record_collateral_event(
    db: AsyncSession,
    row: MarketingCollateralAsset,
    *,
    actor_user_id: uuid.UUID | None,
    event_type: str,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        MarketingCollateralAssetEvent(
            asset_id=row.id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            details=details or {},
            created_at=utcnow(),
        )
    )


async def upload_collateral(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    name: str,
    file_name: str,
    data: bytes,
    sort_order: int = 0,
) -> MarketingCollateralAsset:
    static_validation = validate_pdf(data)
    scan_detail = await scan_pdf_with_clamd(data)
    validation = PdfValidation(
        sha256=static_validation.sha256,
        size_bytes=static_validation.size_bytes,
        status="passed_antivirus",
        detail=f"{static_validation.detail} Managed malware scan passed ({scan_detail}).",
    )
    key = logical_key(name)
    next_version = (
        int(
            (
                await db.execute(
                    select(func.coalesce(func.max(MarketingCollateralAsset.version), 0)).where(
                        MarketingCollateralAsset.assignment == COLLATERAL_ASSIGNMENT,
                        MarketingCollateralAsset.logical_key == key,
                    )
                )
            ).scalar_one()
            or 0
        )
        + 1
    )
    safe_file = re.sub(r"[^A-Za-z0-9._ -]+", "_", file_name or "dealer-outreach.pdf")[:240]
    if not safe_file.lower().endswith(".pdf"):
        safe_file += ".pdf"
    row = MarketingCollateralAsset(
        assignment=COLLATERAL_ASSIGNMENT,
        logical_key=key,
        name=_clean_label(name, safe_file),
        version=next_version,
        sort_order=max(0, sort_order),
        status="pending_approval",
        file_name=safe_file,
        content_type="application/pdf",
        size_bytes=validation.size_bytes,
        sha256=validation.sha256,
        document_bytes=data,
        validation_status=validation.status,
        validation_detail=validation.detail,
        uploaded_by_user_id=actor_user_id,
    )
    db.add(row)
    await db.flush()
    _record_collateral_event(
        db,
        row,
        actor_user_id=actor_user_id,
        event_type="uploaded",
        details={
            "status": row.status,
            "version": row.version,
            "file_name": row.file_name,
            "size_bytes": row.size_bytes,
            "sha256": row.sha256,
            "validation_status": row.validation_status,
            "sort_order": row.sort_order,
        },
    )
    await db.flush()
    return row


async def approve_collateral(
    db: AsyncSession,
    row: MarketingCollateralAsset,
    *,
    actor_user_id: uuid.UUID,
    sort_order: int | None = None,
) -> MarketingCollateralAsset:
    if row.validation_status != "passed_antivirus":
        raise OutreachBlocked(
            "collateral_not_validated",
            "Collateral has not passed the required managed antivirus scan.",
        )
    previous_status = row.status
    previous_sort_order = int(row.sort_order)
    active_versions = list(
        (
            await db.execute(
                select(MarketingCollateralAsset)
                .where(
                    MarketingCollateralAsset.assignment == row.assignment,
                    MarketingCollateralAsset.logical_key == row.logical_key,
                    MarketingCollateralAsset.status == "active",
                    MarketingCollateralAsset.id != row.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for old in active_versions:
        old_status = old.status
        old.status = "retired"
        old.retired_at = now
        old.retired_by_user_id = actor_user_id
        _record_collateral_event(
            db,
            old,
            actor_user_id=actor_user_id,
            event_type="retired",
            details={
                "status_before": old_status,
                "status_after": "retired",
                "reason": "new_version_approved",
                "replacement_asset_id": str(row.id),
            },
        )
    row.status = "active"
    row.approved_at = now
    row.approved_by_user_id = actor_user_id
    row.retired_at = None
    row.retired_by_user_id = None
    if sort_order is not None:
        row.sort_order = sort_order
    if previous_status != "active":
        _record_collateral_event(
            db,
            row,
            actor_user_id=actor_user_id,
            event_type="restored" if previous_status == "retired" else "approved",
            details={
                "status_before": previous_status,
                "status_after": "active",
                "sort_order_before": previous_sort_order,
                "sort_order_after": int(row.sort_order),
            },
        )
    elif int(row.sort_order) != previous_sort_order:
        _record_collateral_event(
            db,
            row,
            actor_user_id=actor_user_id,
            event_type="reordered",
            details={
                "sort_order_before": previous_sort_order,
                "sort_order_after": int(row.sort_order),
            },
        )
    await db.flush()
    return row


async def retire_collateral(
    db: AsyncSession, row: MarketingCollateralAsset, *, actor_user_id: uuid.UUID
) -> MarketingCollateralAsset:
    if row.status == "retired":
        return row
    previous_status = row.status
    row.status = "retired"
    row.retired_at = utcnow()
    row.retired_by_user_id = actor_user_id
    _record_collateral_event(
        db,
        row,
        actor_user_id=actor_user_id,
        event_type="retired",
        details={"status_before": previous_status, "status_after": "retired"},
    )
    await db.flush()
    return row


async def update_collateral_order(
    db: AsyncSession,
    row: MarketingCollateralAsset,
    *,
    actor_user_id: uuid.UUID,
    sort_order: int,
) -> MarketingCollateralAsset:
    previous = int(row.sort_order)
    row.sort_order = max(0, int(sort_order))
    if row.sort_order != previous:
        _record_collateral_event(
            db,
            row,
            actor_user_id=actor_user_id,
            event_type="reordered",
            details={
                "sort_order_before": previous,
                "sort_order_after": row.sort_order,
            },
        )
    await db.flush()
    return row


async def reorder_collateral(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    expected_ids: list[uuid.UUID],
    ordered_ids: list[uuid.UUID],
) -> list[MarketingCollateralAsset]:
    """Atomically reorder the complete non-retired Dealer Outreach library."""
    rows = list(
        (
            await db.execute(
                select(MarketingCollateralAsset)
                .where(
                    MarketingCollateralAsset.assignment == COLLATERAL_ASSIGNMENT,
                    MarketingCollateralAsset.status != "retired",
                )
                .order_by(
                    MarketingCollateralAsset.sort_order,
                    MarketingCollateralAsset.logical_key,
                    MarketingCollateralAsset.version.desc(),
                    MarketingCollateralAsset.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    current_ids = [item.id for item in rows]
    if current_ids != expected_ids:
        raise OutreachConflict(
            "Collateral changed in another session. Refresh the library before reordering."
        )
    if len(set(ordered_ids)) != len(ordered_ids) or set(ordered_ids) != set(current_ids):
        raise OutreachConflict("The reorder request must include every non-retired asset once.")
    by_id = {item.id: item for item in rows}
    ordered: list[MarketingCollateralAsset] = []
    for index, asset_id in enumerate(ordered_ids):
        row = by_id[asset_id]
        previous = int(row.sort_order)
        next_order = index * 10
        row.sort_order = next_order
        ordered.append(row)
        if previous != next_order:
            _record_collateral_event(
                db,
                row,
                actor_user_id=actor_user_id,
                event_type="reordered",
                details={
                    "sort_order_before": previous,
                    "sort_order_after": next_order,
                    "position": index,
                    "atomic": True,
                },
            )
    await db.flush()
    return ordered
