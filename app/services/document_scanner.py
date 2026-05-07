"""Vision-based document scanner.

The scheduler's `job_document_scan_drain` (every 2 min) picks up
`Document.scan_dirty=True` rows and calls `scan_document(...)`
which:

  1. Pulls the file from S3 (capped at 10 MB).
  2. Builds a Claude Sonnet 4 vision/document content block
     (PDFs use Anthropic's native document block; images use the
     image block).
  3. Asks Sonnet to extract structured facts + decide whether the
     file matches the expected checklist item (when one is set).
  4. Writes results back onto the Document row:
       ai_notes / ai_scan_confidence / ai_scan_status='scanned'
       status flips to VERIFIED or FLAGGED based on confidence
       For "is_other" uploads, may auto-link to a known checklist
       item the AI recognizes (high-confidence only).
  5. Drops a one-line AI reaction into the loan's chat thread via
     `post_ai_message` so the borrower sees the verdict in
     Messages.
  6. Marks the loan + client living-profile dirty so the next
     summarizer drain picks up the new state.

Cost: single Sonnet call per scan (~$0.01-$0.30 depending on size).
The 8-doc-per-tick cap in the scheduler bounds peak burn.

Failure modes:
  - File too large / wrong format → ai_scan_status='failed', notes
    explain why. Operator can re-trigger after fixing.
  - Anthropic call fails (rate limit, transient) → 'failed' + the
    next tick won't auto-retry (operator decision).
  - JSON parse failure → 'failed' with raw output stashed in
    ai_notes for debugging.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.enums import DocStatus
from app.models.activity import Activity
from app.models.app_settings import AppSettings
from app.models.document import Document
from app.models.loan import Loan
from app.services.activity_log import mark_loan_dirty
from app.services.ai.anthropic_client import get_client, model_heavy
from app.services.loan_intake_automation import _checklist_for, _coerce_settings

log = logging.getLogger(__name__)

# Hard caps. Larger docs get skipped with a clear note instead of
# silently failing or running up the bill.
MAX_BYTES = 10 * 1024 * 1024  # 10 MB
AUTO_VERIFY_MIN_CONFIDENCE = 0.85

_VISION_SYSTEM = """You are a senior loan-document reviewer at a commercial real estate brokerage.

You receive ONE document at a time and must decide:
  1. What kind of document is this?
  2. Does it match what was REQUESTED (the 'expected_type' the user provides)?
  3. Are there obvious issues a human reviewer should catch?

Output ONLY a single JSON object in this exact shape — no prose, no
code fences:

{
  "document_type": "Tax Returns" | "Bank Statement" | "Rent Roll" | "P&L Statement" | "Operating Agreement" | "Insurance Declaration" | "Driver's License" | "Other" | etc.,
  "named_borrowers": ["Full names that appear in the document"],
  "key_dates": ["YYYY-MM-DD or human-readable dates the document covers"],
  "key_amounts": [12345.67, ...],
  "matches_expected": true | false,
  "confidence": 0.0-1.0,
  "notes": "1-2 sentence summary the operator can read",
  "red_flags": ["list of issues; empty array if none"],
  "suggested_checklist_key": "the closest matching item from the loan's checklist, or null"
}

Strict rules:
  - confidence is your ACTUAL confidence in document_type. 0.85+ means
    you're sure. 0.5-0.85 means the doc is plausible but ambiguous.
    Below 0.5 means you don't recognize it.
  - If 'expected_type' was null (off-checklist upload) leave
    matches_expected=false and put your best guess in
    document_type + suggested_checklist_key.
  - red_flags should be SPECIFIC. "Tax year does not match request"
    is useful. "Looks fishy" is not.
  - If the document is unreadable / encrypted / blank, set
    confidence=0, document_type="Other", notes="Document
    unreadable or encrypted".
"""


@dataclass
class ScanResult:
    document_type: str | None = None
    named_borrowers: list[str] = field(default_factory=list)
    key_dates: list[str] = field(default_factory=list)
    key_amounts: list[float] = field(default_factory=list)
    matches_expected: bool = False
    confidence: float = 0.0
    notes: str = ""
    red_flags: list[str] = field(default_factory=list)
    suggested_checklist_key: str | None = None
    raw_response: str | None = None  # for debugging when parse fails


def _s3():
    s = get_settings()
    return boto3.client(
        "s3",
        aws_access_key_id=s.aws_access_key_id or None,
        aws_secret_access_key=s.aws_secret_access_key or None,
        region_name=s.aws_region,
    )


def _fetch_object(s3_key: str) -> tuple[bytes, str] | None:
    """Returns (bytes, content_type). None on failure."""
    settings = get_settings()
    if not settings.s3_bucket:
        return None
    try:
        obj = _s3().get_object(Bucket=settings.s3_bucket, Key=s3_key)
        body = obj["Body"].read()
        ct = obj.get("ContentType") or "application/octet-stream"
        return body, ct
    except Exception as exc:  # noqa: BLE001
        log.warning("scanner: s3 fetch failed key=%s: %s", s3_key, exc)
        return None


def _supported_mime(content_type: str) -> str | None:
    """Map S3 content type to the kind of vision block we'll send.
    Returns the canonical media_type string, or None if unsupported."""
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == "application/pdf":
        return "application/pdf"
    if ct in ("image/jpeg", "image/jpg"):
        return "image/jpeg"
    if ct == "image/png":
        return "image/png"
    if ct == "image/gif":
        return "image/gif"
    if ct == "image/webp":
        return "image/webp"
    return None


def _build_content_block(media_type: str, raw: bytes) -> dict[str, Any]:
    """Image → image block. PDF → document block (Anthropic native PDF support)."""
    b64 = base64.b64encode(raw).decode("ascii")
    if media_type == "application/pdf":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
        }
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": b64,
        },
    }


def _strip_code_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl > 0:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _parse_response(text: str) -> ScanResult:
    """Best-effort parse of the JSON the model returned. Anything
    unparseable still gets a ScanResult — callers see confidence=0."""
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return ScanResult(notes="Could not parse model output", raw_response=text[:600])

    def _str_list(key: str) -> list[str]:
        v = data.get(key)
        return [str(x) for x in v if x] if isinstance(v, list) else []

    def _num_list(key: str) -> list[float]:
        v = data.get(key)
        out: list[float] = []
        if isinstance(v, list):
            for x in v:
                try:
                    out.append(float(x))
                except (TypeError, ValueError):
                    continue
        return out

    return ScanResult(
        document_type=str(data.get("document_type") or "") or None,
        named_borrowers=_str_list("named_borrowers"),
        key_dates=_str_list("key_dates"),
        key_amounts=_num_list("key_amounts"),
        matches_expected=bool(data.get("matches_expected")),
        confidence=float(data.get("confidence") or 0.0),
        notes=str(data.get("notes") or "")[:1200],
        red_flags=_str_list("red_flags"),
        suggested_checklist_key=(data.get("suggested_checklist_key") or None),
    )


async def _checklist_keys_for(db: AsyncSession, loan: Loan) -> list[str]:
    """Returns the loan's product checklist keys (item names). Used to
    constrain the model's `suggested_checklist_key`."""
    settings_row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    settings = _coerce_settings(settings_row)
    checklist = _checklist_for(settings, str(loan.type))
    return [item.name for item in checklist.docs]


async def _resolve_existing_request(
    db: AsyncSession, loan_id: UUID, checklist_key: str
) -> Document | None:
    """For is_other auto-link: look up an existing REQUESTED row on
    this loan with this checklist_key so we can supersede it."""
    row = (
        await db.execute(
            select(Document).where(
                Document.loan_id == loan_id,
                Document.checklist_key == checklist_key,
                Document.status == DocStatus.REQUESTED,
            )
        )
    ).scalar_one_or_none()
    return row


def _build_ai_message(doc: Document, scan: ScanResult, *, auto_linked: bool) -> str:
    """One-liner reaction the borrower sees in Messages."""
    label = doc.checklist_key or scan.document_type or doc.name
    if doc.status == DocStatus.VERIFIED:
        body = f"Got your {label} — thanks. {scan.notes}".strip()
        if auto_linked:
            body = (
                f"Got your upload — looks like a {label}, so I've matched it "
                f"to that requirement. {scan.notes}".strip()
            )
        return body
    if doc.status == DocStatus.FLAGGED:
        flag_str = "; ".join(scan.red_flags) if scan.red_flags else (
            "I'm not sure this is the right document."
        )
        return (
            f"I had a look at your upload for {label} but flagged it for review: {flag_str} "
            f"An underwriter will follow up shortly."
        )
    return (
        f"Got your upload for {label}. An underwriter will take a look "
        f"and get back to you."
    )


async def scan_document(db: AsyncSession, document_id: UUID) -> ScanResult:
    """Run a single document through the vision pipeline and persist
    the outcome onto the Document row. Returns the ScanResult so
    callers (the scheduler tick) can log + decide.

    Raises only on programming errors. Caller-fixable failures (S3
    miss, unsupported format, vision call exception) are caught and
    persisted as `ai_scan_status='failed'`.
    """
    settings = get_settings()

    doc = (
        await db.execute(
            select(Document)
            .options(selectinload(Document.loan).selectinload(Loan.client))
            .where(Document.id == document_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        log.warning("scanner: document %s vanished", document_id)
        return ScanResult(notes="Document row not found")

    loan = doc.loan
    if loan is None:
        log.warning("scanner: document %s has no parent loan", document_id)
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = "Document has no parent loan"
        return ScanResult(notes="Document has no parent loan")

    # Mark in-flight so concurrent ticks don't double-grab
    doc.ai_scan_status = "scanning"
    await db.flush()

    # 1. Pull bytes from S3
    if not doc.s3_key:
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = "No s3_key on document — nothing to scan."
        await db.flush()
        return ScanResult(notes=doc.ai_notes)

    fetched = _fetch_object(doc.s3_key)
    if fetched is None:
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = "Couldn't fetch the file from S3."
        await db.flush()
        return ScanResult(notes=doc.ai_notes)

    raw, ct = fetched
    if len(raw) > MAX_BYTES:
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = (
            f"File is larger than the {MAX_BYTES // (1024 * 1024)} MB scan cap; "
            "operator review required."
        )
        await db.flush()
        return ScanResult(notes=doc.ai_notes)

    media_type = _supported_mime(ct)
    if media_type is None:
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = (
            f"Unsupported content type {ct!r}. Vision scanner accepts PDF + JPEG/PNG/GIF/WEBP."
        )
        await db.flush()
        return ScanResult(notes=doc.ai_notes)

    # 2. Build context for the model
    checklist_keys = await _checklist_keys_for(db, loan)
    expected_type = doc.checklist_key  # may be None for is_other
    user_prompt_parts: list[str] = []
    user_prompt_parts.append(
        f"Loan: {loan.deal_id} ({str(loan.type)}) at {loan.address}"
    )
    if expected_type:
        user_prompt_parts.append(f"expected_type: {expected_type}")
    else:
        user_prompt_parts.append("expected_type: null (borrower picked 'Other')")
    if checklist_keys:
        user_prompt_parts.append(
            "Checklist keys for this loan (use one of these or null for "
            "suggested_checklist_key): "
            + ", ".join(repr(k) for k in checklist_keys)
        )
    user_prompt_parts.append(
        "Now look at the attached document and produce the JSON object."
    )
    user_text = "\n".join(user_prompt_parts)

    # 3. Vision call
    if not settings.anthropic_api_key:
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = "ANTHROPIC_API_KEY not configured — vision scan skipped."
        await db.flush()
        return ScanResult(notes=doc.ai_notes)

    try:
        client = get_client()
        result = await client.messages.create(
            model=model_heavy(),
            max_tokens=900,
            system=_VISION_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        _build_content_block(media_type, raw),
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
        text = "".join(
            b.text for b in result.content if getattr(b, "type", None) == "text"
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("scanner: vision call failed doc=%s", doc.id)
        doc.ai_scan_status = "failed"
        doc.scan_dirty = False
        doc.ai_notes = f"Vision call failed: {exc}"
        await db.flush()
        return ScanResult(notes=doc.ai_notes)

    scan = _parse_response(text)
    log.info(
        "scanner: doc=%s type=%r match=%s conf=%.2f",
        doc.id, scan.document_type, scan.matches_expected, scan.confidence,
    )

    # 4. Decide outcome + write back
    auto_linked = False
    if doc.is_other and scan.confidence >= AUTO_VERIFY_MIN_CONFIDENCE and scan.suggested_checklist_key:
        # Try to auto-link. If a REQUESTED row for that key exists
        # on this loan, supersede it; the new file becomes the
        # canonical fulfillment.
        if scan.suggested_checklist_key in checklist_keys:
            superseded = await _resolve_existing_request(
                db, loan.id, scan.suggested_checklist_key
            )
            doc.checklist_key = scan.suggested_checklist_key
            doc.is_other = False
            auto_linked = True
            if superseded is not None and superseded.id != doc.id:
                # Old REQUESTED row gets dropped — its info now
                # lives on the new RECEIVED+scanned row.
                await db.delete(superseded)

    if doc.checklist_key is not None:
        # Categorized: agreement + confidence drives verify/flag.
        if scan.matches_expected and scan.confidence >= AUTO_VERIFY_MIN_CONFIDENCE:
            doc.status = DocStatus.VERIFIED
            doc.verified_at = datetime.now(timezone.utc)
            doc.verified_by = "ai"
        else:
            doc.status = DocStatus.FLAGGED
    elif doc.is_other:
        # Uncategorized "Other" with low confidence — operator triages.
        doc.status = DocStatus.PENDING

    # Persist scan state
    notes_lines: list[str] = []
    if scan.document_type:
        notes_lines.append(f"Type: {scan.document_type}")
    if scan.notes:
        notes_lines.append(scan.notes)
    if scan.named_borrowers:
        notes_lines.append("Names: " + ", ".join(scan.named_borrowers))
    if scan.key_dates:
        notes_lines.append("Dates: " + ", ".join(scan.key_dates))
    if scan.key_amounts:
        notes_lines.append("Amounts: " + ", ".join(f"${a:,.0f}" for a in scan.key_amounts))
    if scan.red_flags:
        notes_lines.append("Flags: " + "; ".join(scan.red_flags))
    if not notes_lines and scan.raw_response:
        notes_lines.append(f"(unparseable model output) {scan.raw_response}")
    doc.ai_notes = "\n".join(notes_lines) or "Vision scan complete."
    doc.ai_scan_confidence = Decimal(f"{scan.confidence:.2f}")
    doc.ai_scan_status = "scanned"
    doc.scan_dirty = False

    # 5. Activity + AI message
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label="ai",
            kind="doc.scanned" if doc.status == DocStatus.VERIFIED else "doc.flagged",
            summary=f"AI scanned: {doc.name}",
            payload={
                "doc_id": str(doc.id),
                "document_type": scan.document_type,
                "confidence": scan.confidence,
                "matches_expected": scan.matches_expected,
                "auto_linked": auto_linked,
                "status": str(doc.status),
            },
        )
    )

    # 6. Drop a one-liner into the loan's chat thread (Phase E).
    # Local import to avoid a circular at module load.
    try:
        from app.services.ai_messaging import post_ai_message
        if loan.client and loan.client.user_id:
            await post_ai_message(
                db,
                user_id=loan.client.user_id,
                loan_id=loan.id,
                body=_build_ai_message(doc, scan, auto_linked=auto_linked),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("scanner: post_ai_message failed doc=%s: %s", doc.id, exc)

    # 7. Mark loan + client living-profile dirty so the per-loan +
    # account summarizers pick up the new state.
    await mark_loan_dirty(db, loan.id)
    if loan.client is not None:
        loan.client.living_refreshed_at = None  # forces next refresh

    await db.flush()
    return scan
