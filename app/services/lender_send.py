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
from app.services.ai.anthropic_client import get_client, model_light
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


_BODY_SYSTEM_PROMPT = """You draft short, formal lender-submission emails for a commercial real estate brokerage.

Tone: institutional, polite, concise. No fluff, no marketing copy. The lender is busy.
Length: 3 short paragraphs MAX, plus a bulleted list of file names.

You will be given:
  - deal_id, property address, loan type, loan amount
  - lender's primary contact name (may be null)
  - delivery mode ('links' = individual download links, one per file;
                   'zip' = one archive link)
  - the list of file names being sent

Output ONLY the email body (plain text, no markdown headers, no
subject line, no signature block — the system appends those). Do
NOT invent additional documents or commit to anything beyond what's
in the input.
"""


async def _ai_draft_body(
    *,
    deal_id: str,
    address: str,
    loan_type: str,
    loan_amount: float | None,
    contact_name: str | None,
    delivery: Literal["links", "zip"],
    file_names: list[str],
) -> str:
    """Generates the email body via Haiku. Falls back to a
    deterministic template when the API key is unset or the call
    fails — we never block a send-draft on Anthropic."""
    settings = get_settings()
    file_list = "\n".join(f"  • {name}" for name in file_names) or "  (no files attached)"
    fallback = (
        f"{'Hi ' + contact_name + ',' if contact_name else 'Hello,'}\n\n"
        f"Please find the submission package for {deal_id} — {address} attached "
        f"{'as a single archive' if delivery == 'zip' else 'via the download links below'}. "
        f"Loan type: {loan_type}{f', amount ${loan_amount:,.0f}' if loan_amount else ''}.\n\n"
        f"Files included:\n{file_list}\n\n"
        f"Happy to provide anything else you need to move this forward.\n"
    )
    if not settings.anthropic_api_key:
        return fallback
    try:
        client = get_client()
        result = await client.messages.create(
            model=model_light(),
            max_tokens=400,
            system=_BODY_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"deal_id: {deal_id}\n"
                        f"address: {address}\n"
                        f"loan_type: {loan_type}\n"
                        f"loan_amount: {loan_amount}\n"
                        f"contact_name: {contact_name or '(unknown)'}\n"
                        f"delivery: {delivery}\n"
                        f"files:\n{file_list}\n"
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
    # actually received/verified/approved (we don't want to send
    # request-stubs to the lender).
    valid_statuses = {DocStatus.RECEIVED, DocStatus.VERIFIED, DocStatus.APPROVED}
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

    ai_body = await _ai_draft_body(
        deal_id=loan.deal_id,
        address=loan.address,
        loan_type=str(loan.type.value if hasattr(loan.type, "value") else loan.type),
        loan_amount=float(loan.amount) if loan.amount else None,
        contact_name=lender.contact_name,
        delivery=delivery,
        file_names=file_names,
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
