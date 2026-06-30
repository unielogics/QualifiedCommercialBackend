from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import boto3
from pypdf import PdfReader
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models.bucket import (
    Bucket,
    BucketActivityLog,
    BucketAIActionItem,
    BucketAIMessage,
    BucketAIReview,
    BucketDocumentTemplate,
    BucketFile,
    BucketNote,
    BucketRequestedDocument,
    BucketShare,
    BucketUploadLink,
)
from app.models.user import User
from app.services.ai.bedrock_client import get_client, model_heavy, model_light
from app.services.ai.usage import json_safe_metadata, tracked_messages_create

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REVIEW_ATTACHMENTS = 8
MAX_PDF_PAGES = 100

REVIEW_SYSTEM = """You are a senior commercial lending underwriter reviewing a secure document bucket.

Return ONLY JSON in this shape:
{
  "executive_summary": "short plain-English summary",
  "available_documents": [{"file_name": "...", "document_type": "...", "summary": "..."}],
  "missing_or_incomplete_items": [{"title": "...", "detail": "...", "priority": "high|medium|low"}],
  "discrepancies": [{"title": "...", "detail": "...", "files": ["..."]}],
  "underwriter_questions": [{"question": "...", "route": "admin|uploader|share", "reason": "..."}],
  "proof_of_funds_financial_collateral_gaps": [{"title": "...", "detail": "..."}],
  "per_file_summaries": [{"file_id": "...", "file_name": "...", "summary": "...", "red_flags": ["..."]}],
  "recommended_next_document_requests": [{"title": "...", "instructions": "...", "route": "admin|uploader|share", "rationale": "..."}]
}

Be specific. Flag missing proof of funds, unclear financials, mismatched names/dates/amounts, missing collateral documents, unreadable files, stale documents, and any question an underwriter would ask before approval.
"""

CHAT_SYSTEM = """You are the Bucket AI assistant for a secure Qualified Commercial document room.

Return ONLY JSON in this shape:
{
  "answer": "helpful answer scoped to the user's permitted context",
  "proposed_context_patch": null,
  "proposed_action_items": [
    {"title": "...", "instructions": "...", "route": "admin|uploader|share", "rationale": "..."}
  ]
}

Rules:
- Never mention or infer files outside the provided context.
- External users cannot update saved bucket instructions directly.
- If an external user asks for something actionable, create proposed_action_items for super-admin approval.
- For admin users, proposed_context_patch may include deal_type, documentation_level, collateral_type, loan_purpose, underwriting_focus, or custom_instructions when the admin asks to update instructions.
- For admin users, when they ask you to create to-dos, missing-file requests, clarification requests, or follow-up actions, return those as proposed_action_items. Use route "uploader" for client upload tasks, "share" for shared-reviewer tasks, and "admin" for internal Qualified Commercial tasks.
- When suggesting document requests, prefer the provided document template names/categories when they fit. If none fit, create a clear custom task title and instructions.
- Keep answers concise and operational.
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text_from_response(resp: Any) -> str:
    return "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")


def _strip_code_fence(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        if first_nl > 0:
            cleaned = cleaned[first_nl + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _json_or_fallback(text: str, fallback_key: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_strip_code_fence(text))
        return parsed if isinstance(parsed, dict) else {fallback_key: text}
    except json.JSONDecodeError:
        return {fallback_key: text}


def _s3_client():
    settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _fetch_file(file: BucketFile) -> tuple[bytes, str] | None:
    settings = get_settings()
    if not settings.s3_bucket:
        return None
    try:
        obj = _s3_client().get_object(Bucket=settings.s3_bucket, Key=file.s3_key)
        return obj["Body"].read(), obj.get("ContentType") or file.content_type
    except Exception as exc:  # noqa: BLE001
        log.warning("bucket_ai: S3 fetch failed file=%s key=%s: %s", file.id, file.s3_key, exc)
        return None


def _media_type(content_type: str, file_name: str) -> str | None:
    lower = f"{content_type} {file_name}".lower()
    if "application/pdf" in lower or file_name.lower().endswith(".pdf"):
        return "application/pdf"
    if "image/jpeg" in lower or file_name.lower().endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if "image/png" in lower or file_name.lower().endswith(".png"):
        return "image/png"
    if "image/gif" in lower or file_name.lower().endswith(".gif"):
        return "image/gif"
    if "image/webp" in lower or file_name.lower().endswith(".webp"):
        return "image/webp"
    return None


def _content_block(media_type: str, raw: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(raw).decode("ascii")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": encoded}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}


def _pdf_skip_reason(raw: bytes) -> tuple[str, str] | None:
    try:
        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            return (
                "password_protected",
                "This PDF requires a password before AI can read it. Upload an unlocked copy or provide a readable replacement.",
            )
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            return (
                "too_many_pdf_pages",
                f"This PDF has {page_count} pages. Bedrock accepts PDFs up to {MAX_PDF_PAGES} pages, so this file was reviewed by metadata only.",
            )
        return None
    except Exception as exc:  # noqa: BLE001
        message = str(exc).lower()
        if "encrypt" in message or "password" in message:
            return (
                "password_protected",
                "This PDF requires a password before AI can read it. Upload an unlocked copy or provide a readable replacement.",
            )
        return (
            "pdf_parse_failed",
            "The system could not inspect this PDF safely before AI review, so it was reviewed by metadata only.",
        )


def _skip_file(file: BucketFile, reason: str, explanation: str) -> dict[str, str]:
    return {
        "file_id": str(file.id),
        "file_name": file.file_name,
        "reason": reason,
        "explanation": explanation,
    }


async def log_bucket_ai_activity(
    db: AsyncSession,
    bucket_id: UUID,
    action: str,
    *,
    user: User | None = None,
    actor_name: str | None = None,
    actor_email: str | None = None,
    actor_role: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        BucketActivityLog(
            bucket_id=bucket_id,
            actor_user_id=user.id if user else None,
            actor_name=user.name if user else actor_name,
            actor_email=user.email if user else actor_email,
            actor_role=user.role if user else actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            created_at=_now(),
        )
    )


async def run_bucket_ai_review(db: AsyncSession, review_id: UUID) -> BucketAIReview | None:
    review = (
        await db.execute(
            select(BucketAIReview)
            .where(BucketAIReview.id == review_id)
            .options(
                selectinload(BucketAIReview.bucket).selectinload(Bucket.requested_documents),
                selectinload(BucketAIReview.bucket).selectinload(Bucket.files),
            )
        )
    ).scalar_one_or_none()
    if review is None or review.status not in {"queued", "failed"}:
        return review

    bucket = review.bucket
    review.status = "running"
    review.started_at = _now()
    review.error = None
    await log_bucket_ai_activity(db, bucket.id, "ai_review_started", target_type="ai_review", target_id=str(review.id), detail=bucket.name)
    await db.flush()

    files = [file for file in bucket.files if file.status == "uploaded" and file.deleted_at is None]
    review.file_ids = [str(file.id) for file in files]

    requested = [
        {
            "id": str(doc.id),
            "name": doc.name,
            "category": doc.category,
            "description": doc.description,
            "required": doc.required,
            "status": doc.status,
        }
        for doc in bucket.requested_documents
    ]
    metadata = [
        {
            "id": str(file.id),
            "file_name": file.file_name,
            "content_type": file.content_type,
            "size_bytes": file.size_bytes,
            "requested_document_id": str(file.requested_document_id) if file.requested_document_id else None,
            "uploaded_by": file.uploaded_by_name,
        }
        for file in files
    ]
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "bucket": {
                        "name": bucket.name,
                        "client_name": bucket.client_name,
                        "purpose": bucket.purpose,
                        "bucket_type": bucket.bucket_type,
                        "description": bucket.description,
                    },
                    "ai_context": review.context_snapshot or bucket.ai_context or {},
                    "requested_documents": requested,
                    "uploaded_files": metadata,
                    "instruction": "Review the attached/readable files and the metadata. Identify what is available, missing, discrepant, unclear, or likely to be questioned by an underwriter.",
                },
                default=str,
            ),
        }
    ]

    attached = 0
    skipped: list[dict[str, str]] = []
    blocked_files: list[dict[str, str]] = []
    for file in files:
        if attached >= MAX_REVIEW_ATTACHMENTS:
            skipped.append(_skip_file(file, "attachment_limit", "The AI review reached the attachment limit, so this file was reviewed by metadata only."))
            continue
        fetched = _fetch_file(file)
        if fetched is None:
            skipped.append(_skip_file(file, "fetch_failed", "The system could not retrieve this file from storage for AI review."))
            continue
        raw, content_type = fetched
        if len(raw) > MAX_FILE_BYTES:
            skipped.append(_skip_file(file, "too_large", "The file is larger than the current AI review limit and was reviewed by metadata only."))
            continue
        media = _media_type(content_type, file.file_name)
        if media:
            if media == "application/pdf":
                pdf_skip = _pdf_skip_reason(raw)
                if pdf_skip:
                    reason, explanation = pdf_skip
                    skipped_file = _skip_file(file, reason, explanation)
                    if reason == "password_protected":
                        blocked_files.append(skipped_file)
                    skipped.append(skipped_file)
                    continue
            content.append({"type": "text", "text": f"File {file.id}: {file.file_name}"})
            content.append(_content_block(media, raw))
            attached += 1
            continue
        lower = f"{content_type} {file.file_name}".lower()
        if "text/" in lower or file.file_name.lower().endswith((".txt", ".csv", ".md", ".log")):
            snippet = raw[:16000].decode("utf-8", errors="replace")
            content.append({"type": "text", "text": f"File {file.id}: {file.file_name}\n\n{snippet}"})
            attached += 1
            continue
        skipped.append(_skip_file(file, "unsupported_content_type", "This file type is not directly attached to the AI model yet and was reviewed by metadata only."))

    if skipped:
        content.append({"type": "text", "text": "Files not attached to model: " + json.dumps(skipped)})
    if blocked_files:
        content.append({"type": "text", "text": "Files requiring action before AI can read them: " + json.dumps(blocked_files)})

    try:
        model = model_heavy()
        resp = await tracked_messages_create(
            db,
            feature="document_scan",
            client=get_client(),
            model=model,
            metadata={"bucket_id": str(bucket.id), "bucket_ai_review_id": str(review.id)},
            max_tokens=2500,
            system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        review.provider = "bedrock"
        review.model = getattr(resp, "model", None) or model
        result = _json_or_fallback(_text_from_response(resp), "executive_summary")
        if blocked_files:
            result["blocked_files"] = blocked_files
        if skipped:
            result["skipped_files"] = skipped
        review.result = result
        review.status = "completed"
        review.completed_at = _now()
        await _create_review_recommendation_actions(db, bucket=bucket, review=review, result=result)
        await log_bucket_ai_activity(db, bucket.id, "ai_review_completed", target_type="ai_review", target_id=str(review.id), detail=bucket.name)
    except Exception as exc:  # noqa: BLE001
        log.exception("bucket_ai: review failed review=%s", review.id)
        review.status = "failed"
        review.error = str(exc)[:2000]
        review.completed_at = _now()
        await log_bucket_ai_activity(db, bucket.id, "ai_review_failed", target_type="ai_review", target_id=str(review.id), detail=review.error)
    await db.flush()
    return review


async def drain_bucket_ai_reviews(db: AsyncSession, *, limit: int = 3) -> int:
    rows = (
        await db.execute(
            select(BucketAIReview.id)
            .where(BucketAIReview.status == "queued")
            .order_by(BucketAIReview.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    for review_id in rows:
        await run_bucket_ai_review(db, review_id)
        await db.commit()
    return len(rows)


async def latest_review(db: AsyncSession, bucket_id: UUID) -> BucketAIReview | None:
    completed = (
        await db.execute(
            select(BucketAIReview)
            .where(BucketAIReview.bucket_id == bucket_id, BucketAIReview.status == "completed")
            .order_by(BucketAIReview.completed_at.desc().nulls_last(), BucketAIReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if completed is not None:
        return completed
    return (
        await db.execute(
            select(BucketAIReview)
            .where(BucketAIReview.bucket_id == bucket_id, BucketAIReview.result.is_not(None))
            .order_by(BucketAIReview.completed_at.desc().nulls_last(), BucketAIReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def share_visible_summary(review: BucketAIReview | None, share: BucketShare) -> dict[str, Any] | None:
    if review is None or not isinstance(review.result, dict):
        return None
    visible_names = {file.file_name for file in share.files if file.status == "uploaded" and file.deleted_at is None}
    per_file = [
        item for item in review.result.get("per_file_summaries", []) or []
        if isinstance(item, dict) and item.get("file_name") in visible_names
    ]
    return {
        "summary": f"{len(visible_names)} shared file{'' if len(visible_names) == 1 else 's'} available for review.",
        "per_file_summaries": per_file,
        "missing_or_incomplete_items": _visible_review_items(review.result.get("missing_or_incomplete_items") or [], visible_names),
        "discrepancies": _visible_review_items(review.result.get("discrepancies") or [], visible_names),
        "blocked_files": _visible_review_items(review.result.get("blocked_files") or [], visible_names),
    }


def _visible_review_items(items: Any, visible_names: set[str]) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    visible: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_files = item.get("files")
        named_files = {str(value) for value in raw_files} if isinstance(raw_files, list) else set()
        file_name = str(item.get("file_name") or "")
        if named_files and not named_files.intersection(visible_names):
            continue
        if file_name and file_name not in visible_names:
            continue
        visible.append(item)
    return visible


def upload_link_visible_summary(review: BucketAIReview | None, bucket: Bucket) -> dict[str, Any] | None:
    active_files = [file for file in bucket.files if file.status == "uploaded" and file.deleted_at is None]
    active_names = {file.file_name for file in active_files}
    active_ids = {str(file.id) for file in active_files}
    if review is None or not isinstance(review.result, dict):
        missing_docs = [
            {
                "title": doc.name,
                "detail": doc.description or "This requested document has not been marked received yet.",
                "priority": "high" if doc.required else "medium",
                "category": doc.category,
            }
            for doc in bucket.requested_documents
            if doc.status != "uploaded"
        ]
        return {
            "summary": "Qualified Commercial has prepared this file room. AI analysis uses the super-admin bucket inputs, uploaded files, and any completed underwriting review.",
            "review_status": review.status if review else "not_started",
            "review_error": review.error if review else None,
            "ai_context": bucket.ai_context or {},
            "available_documents": [
                {
                    "file_id": str(file.id),
                    "file_name": file.file_name,
                    "document_type": file.content_type,
                    "summary": f"Uploaded by {file.uploaded_by_name or file.uploaded_by_email or 'Qualified Commercial'}.",
                }
                for file in active_files
            ],
            "missing_or_incomplete_items": missing_docs,
            "discrepancies": [],
            "underwriter_questions": [],
            "proof_of_funds_financial_collateral_gaps": [],
            "per_file_summaries": [],
            "blocked_files": [],
            "skipped_files": [],
        }
    result = review.result
    per_file = [
        item for item in result.get("per_file_summaries", []) or []
        if isinstance(item, dict)
        and (str(item.get("file_id") or "") in active_ids or str(item.get("file_name") or "") in active_names)
    ]
    available = [
        item for item in result.get("available_documents", []) or []
        if isinstance(item, dict) and (not item.get("file_name") or str(item.get("file_name")) in active_names)
    ]
    return {
        "summary": result.get("executive_summary") or result.get("summary") or f"{len(active_files)} uploaded file{'' if len(active_files) == 1 else 's'} available.",
        "ai_context": bucket.ai_context or {},
        "review_completed_at": review.completed_at.isoformat() if review.completed_at else None,
        "available_documents": available,
        "missing_or_incomplete_items": result.get("missing_or_incomplete_items") or [],
        "discrepancies": result.get("discrepancies") or [],
        "underwriter_questions": result.get("underwriter_questions") or [],
        "proof_of_funds_financial_collateral_gaps": result.get("proof_of_funds_financial_collateral_gaps") or [],
        "per_file_summaries": per_file,
        "blocked_files": result.get("blocked_files") or [],
        "skipped_files": result.get("skipped_files") or [],
    }


async def visible_action_items(
    db: AsyncSession,
    bucket_id: UUID,
    *,
    route: str | None = None,
    upload_link_id: UUID | None = None,
    share_id: UUID | None = None,
    approved_only: bool = False,
) -> list[BucketAIActionItem]:
    stmt = select(BucketAIActionItem).where(BucketAIActionItem.bucket_id == bucket_id)
    if approved_only:
        stmt = stmt.where(BucketAIActionItem.status.in_(("approved", "completed")))
    if route:
        stmt = stmt.where(BucketAIActionItem.route == route)
    if upload_link_id:
        stmt = stmt.where(
            or_(
                BucketAIActionItem.upload_link_id == upload_link_id,
                BucketAIActionItem.upload_link_id.is_(None),
            )
            if route == "uploader"
            else BucketAIActionItem.upload_link_id == upload_link_id
        )
    if share_id:
        stmt = stmt.where(BucketAIActionItem.share_id == share_id)
    return (await db.execute(stmt.order_by(BucketAIActionItem.created_at.desc()))).scalars().all()


async def create_chat_reply(
    db: AsyncSession,
    *,
    bucket: Bucket,
    audience: str,
    message: str,
    actor_name: str,
    user: User | None = None,
    upload_link: BucketUploadLink | None = None,
    share: BucketShare | None = None,
) -> tuple[list[BucketAIMessage], list[BucketAIActionItem]]:
    user_row = BucketAIMessage(
        bucket_id=bucket.id,
        upload_link_id=upload_link.id if upload_link else None,
        share_id=share.id if share else None,
        user_id=user.id if user else None,
        audience=audience,
        role="user",
        author_name=actor_name,
        content=message,
    )
    db.add(user_row)
    await db.flush()

    context = await _chat_context(db, bucket=bucket, audience=audience, upload_link=upload_link, share=share)
    model = model_light()
    try:
        resp = await tracked_messages_create(
            db,
            feature="chat",
            client=get_client(),
            model=model,
            user_id=user.id if user else None,
            metadata={"bucket_id": str(bucket.id), "audience": audience, "share_id": str(share.id) if share else None, "upload_link_id": str(upload_link.id) if upload_link else None},
            max_tokens=1200,
            system=CHAT_SYSTEM,
            messages=[{"role": "user", "content": json.dumps({"context": context, "message": message}, default=str)}],
        )
        parsed = _json_or_fallback(_text_from_response(resp), "answer")
        answer = str(parsed.get("answer") or parsed.get("summary") or _text_from_response(resp))[:5000]
        patch = parsed.get("proposed_context_patch") if isinstance(parsed.get("proposed_context_patch"), dict) else None
        assistant = BucketAIMessage(
            bucket_id=bucket.id,
            upload_link_id=upload_link.id if upload_link else None,
            share_id=share.id if share else None,
            audience=audience,
            role="assistant",
            author_name="Bucket AI",
            content=answer,
            proposed_context_patch=json_safe_metadata(patch),
            provider="bedrock",
            model=getattr(resp, "model", None) or model,
            metadata_json=json_safe_metadata({"raw": parsed}),
        )
        db.add(assistant)
        await db.flush()
        proposals = await _create_proposals(db, bucket=bucket, source_message=assistant, parsed=parsed, audience=audience, upload_link=upload_link, share=share, user=user)
        await log_bucket_ai_activity(db, bucket.id, "ai_chat_message_created", user=user, actor_name=actor_name, actor_role=audience, target_type="ai_message", target_id=str(user_row.id), detail=message[:180])
        return [user_row, assistant], proposals
    except Exception as exc:  # noqa: BLE001
        log.exception("bucket_ai: chat failed bucket=%s", bucket.id)
        assistant = BucketAIMessage(
            bucket_id=bucket.id,
            upload_link_id=upload_link.id if upload_link else None,
            share_id=share.id if share else None,
            audience=audience,
            role="assistant",
            author_name="Bucket AI",
            content=f"AI is unavailable right now: {exc}",
        )
        db.add(assistant)
        await db.flush()
        await log_bucket_ai_activity(db, bucket.id, "ai_chat_failed", user=user, actor_name=actor_name, actor_role=audience, target_type="ai_message", target_id=str(user_row.id), detail=str(exc)[:180])
        return [user_row, assistant], []


async def _chat_context(
    db: AsyncSession,
    *,
    bucket: Bucket,
    audience: str,
    upload_link: BucketUploadLink | None,
    share: BucketShare | None,
) -> dict[str, Any]:
    base = {
        "bucket": {
            "name": bucket.name,
            "client_name": bucket.client_name,
            "purpose": bucket.purpose,
            "bucket_type": bucket.bucket_type,
        },
        "audience": audience,
    }
    if audience == "admin":
        review = await latest_review(db, bucket.id)
        tasks = await visible_action_items(db, bucket.id)
        templates = (
            await db.execute(
                select(BucketDocumentTemplate)
                .where(BucketDocumentTemplate.is_active.is_(True))
                .order_by(BucketDocumentTemplate.category, BucketDocumentTemplate.name)
                .limit(250)
            )
        ).scalars().all()
        return {
            **base,
            "ai_context": bucket.ai_context or {},
            "requested_documents": [_doc_context(doc) for doc in bucket.requested_documents],
            "document_template_library": [_template_context(template) for template in templates],
            "files": [_file_context(file) for file in bucket.files if file.status == "uploaded" and file.deleted_at is None],
            "notes": [_note_context(note) for note in bucket.notes],
            "latest_review": review.result if review else None,
            "action_items": [_task_context(task) for task in tasks],
            "instructions": "When the admin asks for tasks or document requests, use the template library where it fits. If no template matches, create a custom action item with route uploader/admin/share.",
        }
    if upload_link is not None:
        review = await latest_review(db, bucket.id)
        tasks = await visible_action_items(db, bucket.id, route="uploader", upload_link_id=upload_link.id, approved_only=True)
        return {
            **base,
            "recipient_name": upload_link.recipient_name,
            "requested_documents": [_doc_context(doc) for doc in bucket.requested_documents],
            "uploaded_files": [_file_context(file) for file in bucket.files if file.status == "uploaded" and file.deleted_at is None],
            "visible_summary": upload_link_visible_summary(review, bucket),
            "instructions": "Help the uploader understand what is already uploaded, what is still needed, and how to submit files. Do not discuss admin notes or shares. External users cannot change saved AI instructions.",
            "approved_tasks": [_task_context(task) for task in tasks],
        }
    if share is not None:
        review = await latest_review(db, bucket.id)
        tasks = await visible_action_items(db, bucket.id, route="share", share_id=share.id, approved_only=True)
        notes = [note for note in bucket.notes if note.visibility == "shared" or share.can_see_internal_notes]
        return {
            **base,
            "recipient_name": share.recipient_name,
            "visible_files": [_file_context(file) for file in share.files if file.status == "uploaded" and file.deleted_at is None],
            "visible_notes": [_note_context(note) for note in notes],
            "visible_summary": share_visible_summary(review, share) if share.can_view_ai_summary else None,
            "approved_tasks": [_task_context(task) for task in tasks],
            "instructions": "Answer only from the files and notes visible to this share link.",
        }
    return base


async def _create_proposals(
    db: AsyncSession,
    *,
    bucket: Bucket,
    source_message: BucketAIMessage,
    parsed: dict[str, Any],
    audience: str,
    upload_link: BucketUploadLink | None,
    share: BucketShare | None,
    user: User | None = None,
) -> list[BucketAIActionItem]:
    raw_items = parsed.get("proposed_action_items")
    if not isinstance(raw_items, list):
        return []
    created: list[BucketAIActionItem] = []
    auto_approve = audience == "admin"
    for raw in raw_items[:5]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        instructions = str(raw.get("instructions") or "").strip()
        if not title or not instructions:
            continue
        route = str(raw.get("route") or ("share" if share else "uploader" if upload_link else "admin")).strip()
        if route not in {"admin", "uploader", "share"}:
            route = "admin"
        if route == "share" and share is None and audience == "admin":
            route = "admin"
        approved_at = _now() if auto_approve else None
        item = BucketAIActionItem(
            bucket_id=bucket.id,
            source_message_id=source_message.id,
            upload_link_id=upload_link.id if route == "uploader" and upload_link else None,
            share_id=share.id if route == "share" and share else None,
            status="approved" if auto_approve else "proposed",
            route=route,
            title=title[:220],
            instructions=instructions,
            rationale=str(raw.get("rationale") or "")[:2000] or None,
            created_by="ai",
            created_by_user_id=user.id if user else None,
            approved_by_user_id=user.id if approved_at and user else None,
            approved_at=approved_at,
        )
        db.add(item)
        created.append(item)
    if created:
        await db.flush()
        await log_bucket_ai_activity(
            db,
            bucket.id,
            "ai_action_created" if auto_approve else "ai_action_proposed",
            user=user if auto_approve else None,
            actor_name="Bucket AI" if not auto_approve else None,
            actor_role=audience,
            target_type="ai_action_item",
            detail=f"{len(created)} {'created' if auto_approve else 'proposed'}",
        )
    return created


async def _create_review_recommendation_actions(
    db: AsyncSession,
    *,
    bucket: Bucket,
    review: BucketAIReview,
    result: dict[str, Any],
) -> list[BucketAIActionItem]:
    raw_items = result.get("recommended_next_document_requests")
    if not isinstance(raw_items, list):
        return []
    existing_titles = {
        title.lower()
        for title in (
            await db.execute(
                select(BucketAIActionItem.title).where(
                    BucketAIActionItem.bucket_id == bucket.id,
                    BucketAIActionItem.status.in_(("proposed", "approved")),
                )
            )
        ).scalars().all()
    }
    created: list[BucketAIActionItem] = []
    for raw in raw_items[:8]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        instructions = str(raw.get("instructions") or raw.get("detail") or "").strip()
        if not title or not instructions or title.lower() in existing_titles:
            continue
        route = str(raw.get("route") or "admin").strip()
        if route not in {"admin", "uploader", "share"}:
            route = "admin"
        if route == "share":
            route = "admin"
        item = BucketAIActionItem(
            bucket_id=bucket.id,
            source_message_id=None,
            status="proposed",
            route=route,
            title=title[:220],
            instructions=instructions,
            rationale=str(raw.get("rationale") or "Recommended by the latest AI underwriting review.")[:2000],
            created_by="ai",
        )
        db.add(item)
        created.append(item)
        existing_titles.add(title.lower())
    if created:
        await db.flush()
        await log_bucket_ai_activity(
            db,
            bucket.id,
            "ai_action_proposed",
            actor_name="Bucket AI",
            actor_role="system",
            target_type="ai_review",
            target_id=str(review.id),
            detail=f"{len(created)} review recommendations",
        )
    return created


def _doc_context(doc: BucketRequestedDocument) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "name": doc.name,
        "category": doc.category,
        "description": doc.description,
        "required": doc.required,
        "allow_multiple_files": doc.allow_multiple_files,
        "status": doc.status,
    }


def _file_context(file: BucketFile) -> dict[str, Any]:
    return {
        "id": str(file.id),
        "file_name": file.file_name,
        "content_type": file.content_type,
        "size_bytes": file.size_bytes,
        "requested_document_id": str(file.requested_document_id) if file.requested_document_id else None,
        "uploaded_by_name": file.uploaded_by_name,
    }


def _template_context(template: BucketDocumentTemplate) -> dict[str, Any]:
    return {
        "id": str(template.id),
        "name": template.name,
        "category": template.category,
        "description": template.description,
        "required": template.required,
        "allow_multiple_files": template.allow_multiple_files,
    }


def _note_context(note: BucketNote) -> dict[str, Any]:
    return {"author_name": note.author_name, "visibility": note.visibility, "content": note.content, "created_at": note.created_at.isoformat()}


def _task_context(task: BucketAIActionItem) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "status": task.status,
        "route": task.route,
        "title": task.title,
        "instructions": task.instructions,
        "rationale": task.rationale,
    }
