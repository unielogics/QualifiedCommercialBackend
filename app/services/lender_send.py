"""Send-to-Lender — package the loan's documents and draft a
broker-approval email to the connected lender.

Flow (operator triggers from the Connected card on the loan page):

  1. Operator multi-selects the docs and picks delivery mode
     (links | zip).
  2. Backend resolves the lender (via `loan.lender_id` first, falling
     back to the LENDER participant for older loans), validates the
     document set, and either:
       links → generates a presigned GET per doc (24h)
       zip   → archives the docs into one S3 ZIP via document_zip
               and presigns the archive (7d)
  3. AI drafts a short, formal email body (Haiku) referencing the
     deal_id + property address + the doc list / archive link.
  4. Inserts an `EmailDraft(status=PENDING)` with To = lender's
     submission email, Cc/Bcc = the loan participants whose
     `cc_outbound` / `bcc_outbound` flags are set (i.e. the notify
     list set at connect time). This is the same recipient
     calculation the orchestrator uses for inbound→broker
     drafts — symmetry by design.
  5. Returns the draft so the desktop can route the operator to the
     existing review screen for approve / send.

The actual send happens through the existing
`POST /email-drafts/{id}/approve` route + Gmail-DWD pipeline. Nothing
new on the wire side.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import boto3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.enums import DocStatus, EmailDraftStatus, ParticipantRole
from app.models.activity import Activity
from app.models.document import Document
from app.models.email_draft import EmailDraft
from app.models.lender import Lender
from app.models.loan import Loan
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import tracked_messages_create
from app.services.document_zip import DocumentZipError, package_documents

log = logging.getLogger(__name__)

DOC_PRESIGN_TTL_SECONDS = 86400  # 24h for individual links


class LenderSendError(ValueError):
    """Caller-fixable error (no lender connected, no docs, oversize
    package, etc.). Routers translate to 400 / 409."""


@dataclass
class SendResult:
    draft: EmailDraft
    lender: Lender
    delivery: Literal["links", "zip"]
    document_count: int
    zip_s3_key: str | None


def _s3():
    s = get_settings()
    return boto3.client(
        "s3",
        aws_access_key_id=s.aws_access_key_id or None,
        aws_secret_access_key=s.aws_secret_access_key or None,
        region_name=s.aws_region,
    )


def _presign_doc(doc: Document) -> str | None:
    settings = get_settings()
    if not settings.s3_bucket or not doc.s3_key:
        return None
    try:
        return _s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": doc.s3_key},
            ExpiresIn=DOC_PRESIGN_TTL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("presign failed doc=%s: %s", doc.id, exc)
        return None


_BODY_SYSTEM_PROMPT = """You draft the introduction for a lender-submission email from a commercial real estate brokerage. The lender is seeing this file for the FIRST time and needs to catch up fast.

Tone: institutional, polite, concise. No fluff, no marketing copy, no hype. The lender is busy.

Write EXACTLY 2 to 3 short paragraphs that introduce the deal:
  - ¶1: the asset (property type / address), the borrower's ask
        (loan type + requested amount), and the headline figures.
  - ¶2: the underwriting posture (LTV/LTC, DSCR, ARV, rate, risk /
        deal health as provided) and the current status — what this
        package establishes / where the file stands.
  - ¶3 (only if there is genuine content): open items or the next
        step. Omit this paragraph rather than padding it.

After the paragraphs, add a single line "Files included:" followed by
a bulleted list of the file names provided.

You will be given a structured CONTEXT block (deal facts, metrics,
status narrative, per-document notes), the lender contact name (may be
null), the delivery mode, and the file list.

Hard rules:
  - Use ONLY figures and facts present in the CONTEXT. Do NOT invent
    or estimate documents, numbers, or commitments. If a metric is
    absent, simply don't mention it.
  - Do NOT include borrower PII (SSN, full credit detail / scores),
    do NOT name any competing or other lenders, and do NOT relay
    internal-only commentary. Keep it lender-appropriate.
  - Output ONLY the email body (plain text, no markdown headers, no
    subject line, no signature block — the system appends links and a
    signature after your text).
"""


def _fmt_pct(v: float | None) -> str | None:
    """Stored as a 0–1 ratio (Numeric(6,4)) — render as a percent."""
    if v is None:
        return None
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return None


# Internal-only / borrower-sensitive vocabulary that must never reach a
# lender-facing email. The cached `living_profile` (status + bottlenecks)
# is operator-internal narrative and routinely embeds fraud/compliance
# flags and PII cues — the LLM faithfully relays whatever it's given, so
# we scrub deterministically at the single context chokepoint (this also
# protects the no-AI fallback body, which embeds the same context).
_SENSITIVE_RE = re.compile(
    r"\b("
    r"fraud|synthetic\s+identity|identity\s+theft|fcra|\bmla\b|ofac|"
    r"sanction|sanctions|watchlist|blacklist|debarred|"
    r"compliance\s+review|credit\s+lock|credit\s+score|credit\s+detail|"
    r"\bfico\b|\bssn\b|social\s+security|date\s+of\s+birth|\bdob\b|"
    r"\bkyc\b|\baml\b|\bsar\b|suspicious\s+activity|"
    r"bankruptc|litigation|lawsuit|judgment\s+lien|broker\s+action"
    r")\b",
    re.IGNORECASE,
)


def _scrub_sensitive(text: str) -> str:
    """Drop any segment (sentence / clause / line) that mentions
    internal-only or borrower-sensitive terms. Conservative by design —
    if a unit is even partly sensitive we remove the whole unit rather
    than risk a partial leak. Returns '' when nothing survives."""
    if not text:
        return ""
    # "Broker action:" begins an internal directive — cut it and
    # everything after it before segmenting.
    text = re.split(r"broker\s+action\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0]
    segments = re.split(r"(?<=[.;!?])\s+|\n+", text)
    kept = [
        s.strip()
        for s in segments
        if s.strip() and not _SENSITIVE_RE.search(s)
    ]
    return " ".join(kept).strip()


def _build_lender_context(loan: Loan, selected: list[Document]) -> str:
    """Compact, plain-text catch-up context for the lender intro.

    Pure (no I/O): `loan` and `loan.documents` are already loaded by
    the caller. Sources only already-curated fields — loan financials,
    the cached `living_profile`, and per-document AI scan notes — never
    raw borrower documents/credit, so no borrower PII leaks. Every
    field is emitted only when present, so a sparse loan still yields
    coherent input.
    """
    lines: list[str] = []

    loan_type = str(loan.type.value if hasattr(loan.type, "value") else loan.type)
    prop_type = str(
        loan.property_type.value
        if hasattr(loan.property_type, "value")
        else loan.property_type
    )
    lines.append("=== DEAL ===")
    lines.append(f"deal_id: {loan.deal_id}")
    lines.append(f"address: {loan.address}")
    if prop_type:
        lines.append(f"property_type: {prop_type}")
    lines.append(f"loan_type: {loan_type}")
    if loan.amount:
        lines.append(f"requested_amount: ${float(loan.amount):,.0f}")

    metrics: list[str] = []
    if (ltv := _fmt_pct(loan.ltv)) is not None:
        metrics.append(f"LTV {ltv}")
    if (ltc := _fmt_pct(loan.ltc)) is not None:
        metrics.append(f"LTC {ltc}")
    if loan.arv:
        metrics.append(f"ARV ${float(loan.arv):,.0f}")
    if loan.dscr is not None:
        metrics.append(f"DSCR {float(loan.dscr):.2f}x")
    rate = loan.final_rate if loan.final_rate is not None else loan.base_rate
    if rate is not None:
        # Stored as a 0–1 ratio (Numeric(9,6)) — render as a percent.
        metrics.append(f"rate {float(rate) * 100:.3f}%")
    if loan.risk_score is not None:
        metrics.append(f"risk_score {loan.risk_score}")
    dh = loan.deal_health
    dh = str(dh.value if hasattr(dh, "value") else dh) if dh is not None else None
    if dh:
        metrics.append(f"deal_health {dh}")
    if metrics:
        lines.append("=== UNDERWRITING METRICS ===")
        lines.append("; ".join(metrics))

    status_bits: list[str] = []
    if loan.status_summary:
        status_bits.append(str(loan.status_summary).strip())
    lp = loan.living_profile if isinstance(loan.living_profile, dict) else {}
    cur = lp.get("current_status")
    if isinstance(cur, str) and cur.strip():
        status_bits.append(cur.strip())
    mkt = lp.get("market_context")
    if isinstance(mkt, dict):
        narr = mkt.get("narrative")
        if isinstance(narr, str) and narr.strip():
            status_bits.append(narr.strip())
    if status_bits:
        status_clean = _scrub_sensitive(" ".join(status_bits))
        if status_clean:
            lines.append("=== STATUS ===")
            lines.append(status_clean[:1200])
    bottlenecks = lp.get("bottlenecks")
    if isinstance(bottlenecks, list) and bottlenecks:
        open_items: list[str] = []
        for b in bottlenecks:
            if isinstance(b, str):
                raw = b
            elif isinstance(b, dict):
                raw = b.get("label") or b.get("title") or b.get("detail") or ""
            else:
                raw = ""
            cleaned = _scrub_sensitive(str(raw))
            if cleaned:
                open_items.append(f"  - {cleaned[:200]}")
            if len(open_items) >= 3:
                break
        if open_items:
            lines.append("=== OPEN ITEMS ===")
            lines.extend(open_items)

    doc_lines: list[str] = []
    for d in selected[:20]:
        name = d.name or f"doc-{str(d.id)[:8]}"
        note = ""
        if d.ai_scan_status == "scanned" and d.ai_notes:
            note = _scrub_sensitive(" ".join(str(d.ai_notes).split()))[:240]
        doc_lines.append(f"  - {name}: {note}" if note else f"  - {name}")
    if doc_lines:
        lines.append("=== DOCUMENTS IN THIS PACKAGE ===")
        lines.extend(doc_lines)

    return "\n".join(lines)


async def _ai_draft_body(
    db: AsyncSession,
    *,
    loan_id: UUID | None,
    client_id: UUID | None,
    deal_id: str,
    address: str,
    loan_type: str,
    loan_amount: float | None,
    contact_name: str | None,
    delivery: Literal["links", "zip"],
    file_names: list[str],
    context: str,
) -> str:
    """Generates the lender-intro email body via Haiku from the
    structured catch-up `context`. Falls back to a deterministic,
    metric-aware template when the API key is unset or the call
    fails — we never block a send-draft on Anthropic."""
    settings = get_settings()
    file_list = "\n".join(f"  • {name}" for name in file_names) or "  (no files attached)"
    fallback = (
        f"{'Hi ' + contact_name + ',' if contact_name else 'Hello,'}\n\n"
        f"Please find the submission package for {deal_id} — {address} "
        f"{'as a single archive' if delivery == 'zip' else 'via the download links below'}. "
        f"Loan type: {loan_type}{f', requested amount ${loan_amount:,.0f}' if loan_amount else ''}.\n\n"
        f"Deal context for your review:\n{context}\n\n"
        f"Files included:\n{file_list}\n\n"
        f"Happy to provide anything else you need to move this forward.\n"
    )
    if not settings.ai_provider_enabled:
        return fallback
    try:
        client = get_client()
        result = await tracked_messages_create(
            db,
            feature="lender_send",
            client=client,
            model=model_light(),
            loan_id=loan_id,
            client_id=client_id,
            max_tokens=700,
            system=_BODY_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"contact_name: {contact_name or '(unknown)'}\n"
                        f"delivery: {delivery}\n\n"
                        f"CONTEXT:\n{context}\n\n"
                        f"FILES (use these exact names in the bulleted list):\n{file_list}\n"
                    ),
                }
            ],
        )
        text = "".join(
            b.text for b in result.content if getattr(b, "type", None) == "text"
        ).strip()
        return text or fallback
    except Exception as exc:  # noqa: BLE001
        log.warning("lender_send: AI draft failed: %s", exc)
        return fallback


async def _resolve_lender(db: AsyncSession, loan: Loan) -> Lender | None:
    """Prefer the FK; fall back to the participant table for legacy
    loans that pre-date the FK."""
    if loan.lender_id is not None:
        lender = (
            await db.execute(select(Lender).where(Lender.id == loan.lender_id))
        ).scalar_one_or_none()
        if lender is not None:
            return lender
    # Fall back: first LENDER participant + matching submission_email
    for p in loan.participants:
        if p.role == ParticipantRole.LENDER:
            lender = (
                await db.execute(
                    select(Lender).where(Lender.submission_email == p.email)
                )
            ).scalar_one_or_none()
            if lender is not None:
                return lender
    return None


async def draft_lender_send(
    db: AsyncSession,
    *,
    loan_id: UUID,
    document_ids: list[UUID],
    delivery: Literal["links", "zip"],
    actor_user_id: UUID | None,
) -> SendResult:
    if delivery not in ("links", "zip"):
        raise LenderSendError(f"Unknown delivery mode: {delivery!r}")

    loan = (
        await db.execute(
            select(Loan)
            .options(
                selectinload(Loan.participants),
                selectinload(Loan.documents),
            )
            .where(Loan.id == loan_id)
        )
    ).scalar_one_or_none()
    if loan is None:
        raise LenderSendError("Loan not found")

    lender = await _resolve_lender(db, loan)
    if lender is None:
        raise LenderSendError(
            "No lender is connected to this loan. Connect a lender first."
        )
    to_email = lender.submission_email or lender.contact_email
    if not to_email:
        raise LenderSendError(
            f"Lender '{lender.name}' has no submission_email or contact_email set."
        )

    # Filter requested doc_ids → only this loan's docs that are
    # actually received/verified (we don't want to send request-stubs
    # to the lender). VERIFIED is the terminal good state — DocStatus
    # has no APPROVED member.
    valid_statuses = {DocStatus.RECEIVED, DocStatus.VERIFIED}
    docs_by_id = {d.id: d for d in loan.documents}
    selected: list[Document] = []
    for did in document_ids:
        d = docs_by_id.get(did)
        if d is None:
            continue  # silently drop strangers
        if d.status not in valid_statuses:
            continue
        selected.append(d)
    if not selected:
        raise LenderSendError(
            "Pick at least one received/verified document to send."
        )

    # Build the body inputs
    file_names = [d.name or f"doc-{str(d.id)[:8]}" for d in selected]
    cc_emails = [
        p.email for p in loan.participants
        if p.cc_outbound and p.role != ParticipantRole.LENDER
    ]
    bcc_emails = [
        p.email for p in loan.participants
        if p.bcc_outbound and p.role != ParticipantRole.LENDER
    ]

    zip_s3_key: str | None = None
    body_links_block: str
    if delivery == "links":
        link_lines: list[str] = []
        for d in selected:
            url = _presign_doc(d)
            if url:
                link_lines.append(f"  • {d.name}\n    {url}")
            else:
                link_lines.append(f"  • {d.name}  (link unavailable)")
        body_links_block = "Download links (valid 24h):\n" + "\n".join(link_lines)
    else:
        try:
            zip_result = package_documents(deal_id=loan.deal_id, documents=selected)
        except DocumentZipError as exc:
            raise LenderSendError(str(exc)) from exc
        zip_s3_key = zip_result.s3_key
        body_links_block = (
            f"Submission package (single archive, {zip_result.files_packaged} files, "
            f"{zip_result.bytes_total // 1024} KB, link valid 7 days):\n"
            f"  {zip_result.download_url}"
        )

    lender_context = _build_lender_context(loan, selected)
    ai_body = await _ai_draft_body(
        db,
        loan_id=loan.id,
        client_id=loan.client_id,
        deal_id=loan.deal_id,
        address=loan.address,
        loan_type=str(loan.type.value if hasattr(loan.type, "value") else loan.type),
        loan_amount=float(loan.amount) if loan.amount else None,
        contact_name=lender.contact_name,
        delivery=delivery,
        file_names=file_names,
        context=lender_context,
    )
    full_body = f"{ai_body}\n\n{body_links_block}\n"

    subject = f"[QC-{loan.deal_id}] Submission package — {loan.address}"

    draft = EmailDraft(
        loan_id=loan.id,
        to_email=to_email,
        cc_emails=cc_emails or None,
        bcc_emails=bcc_emails or None,
        subject=subject,
        body=full_body,
        status=EmailDraftStatus.PENDING,
        # Send-as-user: prefer the acting user, else the loan's operational owner.
        # send_as_user() falls back to firm SES when this user has no Gmail grant.
        sender_user_id=actor_user_id or loan.assigned_owner_id,
        triggered_by_kind="lender_submission",
        triggered_by_payload={
            "delivery": delivery,
            "document_ids": [str(d.id) for d in selected],
            "lender_id": str(lender.id),
            "zip_s3_key": zip_s3_key,
        },
    )
    db.add(draft)

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=actor_user_id,
            actor_label="super_admin",
            kind="lender.send_drafted",
            summary=(
                f"Drafted submission package to {lender.name} "
                f"({len(selected)} files, delivery={delivery})"
            ),
            payload={
                "lender_id": str(lender.id),
                "lender_name": lender.name,
                "delivery": delivery,
                "document_count": len(selected),
                "zip_s3_key": zip_s3_key,
            },
        )
    )

    await db.flush()
    await db.refresh(draft)
    log.info(
        "lender_send: draft=%s loan=%s lender=%s delivery=%s docs=%d",
        draft.id, loan.id, lender.id, delivery, len(selected),
    )
    return SendResult(
        draft=draft,
        lender=lender,
        delivery=delivery,
        document_count=len(selected),
        zip_s3_key=zip_s3_key,
    )
