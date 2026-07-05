from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, with_loader_criteria

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.bucket import Bucket, BucketAIReview, BucketFile, BucketNote, BucketRequestedDocument, BucketUploadLink
from app.models.client import Client
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers.buckets import (
    _bucket_storage_config,
    _generate_passcode,
    _hash_passcode,
    _log,
    _public_url,
    _safe_filename,
    _upload_url,
)
from app.schemas.bucket import (
    BucketAIMessageRead,
    BucketAIReviewRead,
    BucketFileRead,
    BucketFileUploadInitResponse,
    BucketRequestUploadedFileRead,
    BucketRequestedDocumentRead,
)
from app.schemas.common import ORMModel
from app.services.bucket_ai import create_chat_reply, latest_review, run_bucket_ai_review, upload_link_visible_summary
from app.services.payment_authorization import primary_super_admin


router = APIRouter(prefix="/public/dealer-ai-intake", tags=["dealer-ai-intake"])
client_router = APIRouter(prefix="/buckets/client/intakes", tags=["client-bucket-intakes"])


REQUIRED_DOCUMENTS = [
    {
        "name": "Last 2 years tax returns",
        "category": "Financials",
        "description": "Upload business and personal tax returns for the last two years where available.",
        "allow_multiple_files": True,
    },
    {
        "name": "Current year P&L",
        "category": "Financials",
        "description": "Upload the current year profit and loss statement for the dealership/business.",
        "allow_multiple_files": True,
    },
    {
        "name": "Last 3 months bank statements",
        "category": "Bank Statements",
        "description": "Upload the last three months of business bank statements.",
        "allow_multiple_files": True,
    },
    {
        "name": "Asset and real estate schedule",
        "category": "Collateral",
        "description": "List owned real estate and assets, including estimated property values and current loan balances.",
        "allow_multiple_files": True,
    },
    {
        "name": "Mortgage notes or payoff statements",
        "category": "Collateral",
        "description": "Upload mortgage notes, payoff statements, or loan statements for real estate collateral where available.",
        "allow_multiple_files": True,
    },
]


class DealerAssetRow(BaseModel):
    id: str | None = None
    address: str = Field(default="", max_length=320)
    estimated_loan_amount: float | None = Field(default=None, ge=0)
    estimated_property_value: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)


class DealerIntakeStart(BaseModel):
    full_name: str = Field(min_length=1, max_length=180)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=48)
    business_name: str | None = Field(default=None, max_length=180)

    @field_validator("business_name", "phone", mode="before")
    @classmethod
    def empty_to_none(cls, value: object) -> object:
        return None if value == "" else value


class DealerIntakePatch(BaseModel):
    business_name: str | None = Field(default=None, max_length=180)
    phone: str | None = Field(default=None, max_length=48)
    loan_purpose: str | None = Field(default=None, max_length=255)
    requested_loan_amount: float | None = Field(default=None, ge=0)
    estimated_credit_score: int | None = Field(default=None, ge=300, le=850)
    referral_source: str | None = Field(default=None, max_length=180)
    asset_rows: list[DealerAssetRow] | None = None

    @field_validator("business_name", "phone", "loan_purpose", "referral_source", mode="before")
    @classmethod
    def blank_to_none(cls, value: object) -> object:
        return None if value == "" else value


class DealerChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=4000)
    updates: DealerIntakePatch | None = None


class DealerFileUploadInit(BaseModel):
    requested_document_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = "application/octet-stream"
    size_bytes: int = Field(default=0, ge=0)


class DealerUploadComplete(BaseModel):
    file_id: UUID
    note: str | None = Field(default=None, max_length=2000)


class DealerIntakeRead(ORMModel):
    id: UUID
    client_id: UUID | None
    bucket_id: UUID
    bucket_upload_link_id: UUID | None
    latest_review_id: UUID | None
    variant: str
    status: str
    full_name: str
    email: str
    phone: str | None
    business_name: str | None
    loan_purpose: str | None
    requested_loan_amount: float | None
    estimated_credit_score: int | None
    referral_source: str | None
    asset_rows: list[dict[str, Any]] | None
    intake_state: dict[str, Any] | None
    result_snapshot: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class DealerIntakeResponse(BaseModel):
    intake: DealerIntakeRead
    token: str | None = None
    resume_url: str | None = None
    upload_url: str | None = None
    assistant_message: str
    widget: dict[str, Any] | None
    requested_documents: list[BucketRequestedDocumentRead]
    files: list[BucketRequestUploadedFileRead]
    ai_summary: dict[str, Any] | None = None
    latest_review: BucketAIReviewRead | None = None
    messages: list[BucketAIMessageRead] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_public_token() -> str:
    return secrets.token_urlsafe(32)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _active_files(bucket: Bucket) -> list[BucketFile]:
    return [file for file in bucket.files if file.status == "uploaded" and file.deleted_at is None]


def _uploaded_doc_ids(bucket: Bucket) -> set[UUID]:
    return {file.requested_document_id for file in _active_files(bucket) if file.requested_document_id is not None}


def _missing_required_docs(bucket: Bucket) -> list[BucketRequestedDocument]:
    uploaded = _uploaded_doc_ids(bucket)
    return [doc for doc in bucket.requested_documents if doc.required and doc.id not in uploaded]


def _asset_rows(intake: PublicUnderwritingIntake) -> list[dict[str, Any]]:
    rows = intake.asset_rows if isinstance(intake.asset_rows, list) else []
    return [row for row in rows if isinstance(row, dict)]


def _next_widget(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    if not intake.loan_purpose or intake.requested_loan_amount is None or intake.estimated_credit_score is None:
        return {
            "type": "deal_profile",
            "title": "Dealer financing profile",
            "description": "Tell us the purpose, rough loan amount, and estimated credit score. We will validate credit during the intro call.",
            "fields": ["loan_purpose", "requested_loan_amount", "estimated_credit_score"],
        }
    if not _asset_rows(intake):
        return {
            "type": "asset_table",
            "title": "Real estate and asset schedule",
            "description": "Add each property or major asset with the address, estimated current loan amount, and estimated value.",
        }
    missing = _missing_required_docs(intake.bucket)
    if missing:
        return {
            "type": "upload_files",
            "title": "Upload required documents",
            "description": "Upload the required files so the AI can screen bankability.",
            "missing_document_ids": [str(doc.id) for doc in missing],
        }
    if not intake.referral_source:
        return {
            "type": "referral",
            "title": "Referral credit",
            "description": "Who referred you to this link? If nobody did, enter self.",
        }
    if not intake.result_snapshot:
        return {
            "type": "run_review",
            "title": "Run preliminary AI screen",
            "description": "The AI will review the uploaded file room and give a strict preliminary bankability screen.",
        }
    return {
        "type": "bankability_result",
        "title": "Preliminary bankability screen",
        "description": "Review the AI summary, missing items, product fit, and next steps.",
    }


def _message_for_widget(widget: dict[str, Any], intake: PublicUnderwritingIntake) -> str:
    kind = widget.get("type")
    if kind == "deal_profile":
        return "I have your contact info. Next I need the loan purpose, rough requested amount, and your estimated credit score. This is self-reported for now and will be validated during the intro call."
    if kind == "asset_table":
        return "Now add the real estate collateral and major assets. If you have mortgage notes or payoff statements, you can upload those too."
    if kind == "upload_files":
        return "The file is not ready for a bankability answer yet. Upload the required tax returns, P&L, bank statements, asset schedule, and mortgage-note documents."
    if kind == "referral":
        return "Before I run the final screen, tell us who referred you so we can credit the right person."
    if kind == "run_review":
        return "Everything needed for the first screen is captured. Run the AI review and I will tell you whether the file appears bankable, incomplete, or not bankable from the current evidence."
    if kind == "bankability_result":
        status_label = (intake.result_snapshot or {}).get("bankability_assessment", {}).get("status")
        return f"The preliminary screen is ready{f': {status_label}' if status_label else ''}. Review the summary and next steps below."
    return "How can I help with this dealer financing file?"


def _dealer_context(intake: PublicUnderwritingIntake) -> dict[str, Any]:
    return {
        "review_type": "dealer_gatekeeper_v1",
        "deal_type": "dealer financing with real estate collateral",
        "documentation_level": "full doc",
        "collateral_type": "real estate collateral and business assets",
        "loan_purpose": intake.loan_purpose,
        "requested_loan_amount": float(intake.requested_loan_amount) if intake.requested_loan_amount is not None else None,
        "estimated_credit_score": intake.estimated_credit_score,
        "business_name": intake.business_name,
        "referral_source": intake.referral_source,
        "asset_rows": _asset_rows(intake),
        "underwriting_focus": (
            "Strictly screen bankability for DSCR, full-doc commercial/dealer financing, and real estate collateral. "
            "Do not mark bankable if required core documents are missing. Flag proof-of-funds, financial, collateral, "
            "credit, and ownership gaps."
        ),
        "custom_instructions": (
            "This is a public lead-magnet gatekeeper for car dealers. Ask for last 2 years tax returns, current-year P&L, "
            "last 3 months bank statements, asset/real estate schedule, and mortgage notes/payoff statements. "
            "Return a preliminary bankability screen, not a commitment to lend."
        ),
    }


async def _find_or_create_client(db: AsyncSession, payload: DealerIntakeStart) -> Client:
    email = _normalize_email(str(payload.email))
    client = (await db.execute(select(Client).where(Client.email == email).order_by(Client.created_at.desc()))).scalars().first()
    owner = await primary_super_admin(db)
    if client is None:
        client = Client(
            name=payload.full_name.strip(),
            email=email,
            phone=payload.phone,
            referral_source="dealer_ai_intake",
            originating_agent_id=owner.id if owner else None,
            current_agent_id=owner.id if owner else None,
            source_channel="dealer_ai_intake",
            lead_source="other",
            lead_temperature="warm",
            financing_support_needed="yes",
            relationship_context="new_lead",
            client_experience_mode="self_directed",
            client_experience_mode_reason="dealer_ai_intake",
            client_experience_mode_locked_by="firm",
            lead_intake={
                "source": "dealer_ai_intake",
                "business_name": payload.business_name,
            },
        )
        db.add(client)
        await db.flush()
        return client
    if not client.phone and payload.phone:
        client.phone = payload.phone
    if payload.full_name and (not client.name or client.name.lower() == email):
        client.name = payload.full_name.strip()
    if client.current_agent_id is None and owner is not None:
        client.current_agent_id = owner.id
    intake = dict(client.lead_intake or {})
    intake.update({"source": "dealer_ai_intake", "business_name": payload.business_name or intake.get("business_name")})
    client.lead_intake = intake
    await db.flush()
    return client


async def _create_bucket_for_intake(db: AsyncSession, client: Client, payload: DealerIntakeStart, request: Request) -> tuple[Bucket, BucketUploadLink]:
    owner = await primary_super_admin(db)
    bucket = Bucket(
        name=f"{payload.business_name or payload.full_name} Dealer AI Intake",
        bucket_type="dealer_ai_intake",
        client_name=payload.business_name or payload.full_name,
        purpose="Dealer financing AI intake",
        description="Public AI gatekeeper intake for dealer financing with real estate collateral.",
        ai_context={
            "review_type": "dealer_gatekeeper_v1",
            "deal_type": "dealer financing with real estate collateral",
            "documentation_level": "full doc",
            "collateral_type": "real estate collateral and business assets",
            "client_email": client.email,
        },
        created_by_id=owner.id if owner else None,
    )
    db.add(bucket)
    await db.flush()
    for doc in REQUIRED_DOCUMENTS:
        db.add(
            BucketRequestedDocument(
                bucket_id=bucket.id,
                name=doc["name"],
                category=doc["category"],
                description=doc["description"],
                required=True,
                allow_multiple_files=bool(doc["allow_multiple_files"]),
                status="requested",
                is_custom=False,
            )
        )
    passcode = _generate_passcode()
    link = BucketUploadLink(
        bucket_id=bucket.id,
        token=secrets.token_urlsafe(32),
        recipient_name=payload.full_name.strip(),
        recipient_email=client.email,
        allow_notes=True,
        allow_multiple_sessions=True,
        can_use_ai_chat=True,
        can_view_ai_tasks=True,
        passcode_hash=_hash_passcode(passcode),
    )
    db.add(link)
    await _log(
        db,
        bucket.id,
        "dealer_ai_intake_created",
        request=request,
        actor_name=payload.full_name,
        actor_email=client.email,
        actor_role="public_lead",
        target_type="bucket",
        target_id=str(bucket.id),
        detail="Public dealer AI intake created",
    )
    await db.flush()
    return bucket, link


async def _load_public_intake(db: AsyncSession, token: str) -> PublicUnderwritingIntake:
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.token_hash == _hash_token(token))
            .options(
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI intake not found")
    return intake


async def _load_client_intake(db: AsyncSession, user: CurrentUser, intake_id: UUID) -> PublicUnderwritingIntake:
    if user.client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Current user has no linked client record")
    intake = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.id == intake_id, PublicUnderwritingIntake.client_id == user.client.id)
            .options(
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.requested_documents),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.files),
                selectinload(PublicUnderwritingIntake.bucket).selectinload(Bucket.notes),
                selectinload(PublicUnderwritingIntake.bucket_upload_link),
                selectinload(PublicUnderwritingIntake.latest_review),
                with_loader_criteria(BucketFile, BucketFile.deleted_at.is_(None), include_aliases=True),
            )
        )
    ).scalar_one_or_none()
    if intake is None or intake.bucket.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dealer AI intake not found")
    return intake


def _apply_updates(intake: PublicUnderwritingIntake, updates: DealerIntakePatch | None) -> None:
    if updates is None:
        return
    data = updates.model_dump(exclude_unset=True)
    for key in ("business_name", "phone", "loan_purpose", "requested_loan_amount", "estimated_credit_score", "referral_source"):
        if key in data:
            setattr(intake, key, data[key])
    if "asset_rows" in data:
        intake.asset_rows = [row.model_dump() if isinstance(row, DealerAssetRow) else row for row in updates.asset_rows or []]
    state = dict(intake.intake_state or {})
    state["last_updates"] = data
    intake.intake_state = state
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **_dealer_context(intake)}


def _response(
    intake: PublicUnderwritingIntake,
    *,
    token: str | None,
    assistant_message: str | None = None,
    messages: list[Any] | None = None,
) -> DealerIntakeResponse:
    review = intake.latest_review if intake.latest_review else None
    widget = _next_widget(intake)
    files = sorted(_active_files(intake.bucket), key=lambda file: file.created_at, reverse=True)
    summary = upload_link_visible_summary(review, intake.bucket)
    return DealerIntakeResponse(
        intake=DealerIntakeRead.model_validate(intake),
        token=token,
        resume_url=_public_url(f"/dealer-ai-underwriter?token={token}") if token else None,
        upload_url=_public_url(f"/buckets/request/{intake.bucket_upload_link.token}") if intake.bucket_upload_link else None,
        assistant_message=assistant_message or _message_for_widget(widget, intake),
        widget=widget,
        requested_documents=[BucketRequestedDocumentRead.model_validate(doc) for doc in intake.bucket.requested_documents],
        files=[BucketRequestUploadedFileRead.model_validate(file) for file in files],
        ai_summary=summary,
        latest_review=BucketAIReviewRead.model_validate(review) if review else None,
        messages=[BucketAIMessageRead.model_validate(message) for message in (messages or [])],
    )


async def _start_upload(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerFileUploadInit,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFileUploadInitResponse:
    req = None
    if payload.requested_document_id:
        req = await db.get(BucketRequestedDocument, payload.requested_document_id)
        if req is None or req.bucket_id != intake.bucket_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Requested document does not belong to this intake")
    existing_conditions = [
        BucketFile.bucket_id == intake.bucket_id,
        BucketFile.upload_link_id == intake.bucket_upload_link_id,
        BucketFile.file_name == payload.file_name,
        BucketFile.size_bytes == payload.size_bytes,
        BucketFile.status.in_(("uploading", "uploaded")),
        BucketFile.deleted_at.is_(None),
    ]
    if payload.requested_document_id:
        existing_conditions.append(BucketFile.requested_document_id == payload.requested_document_id)
    else:
        existing_conditions.append(BucketFile.requested_document_id.is_(None))
    existing = (await db.execute(select(BucketFile).where(*existing_conditions).order_by(BucketFile.created_at.desc()))).scalars().first()
    if existing:
        upload_url, headers = _upload_url(existing.s3_key, payload.content_type)
        return BucketFileUploadInitResponse(file_id=existing.id, upload_url=upload_url, s3_key=existing.s3_key, required_headers=headers)
    if req is not None and not req.allow_multiple_files:
        existing_for_doc = (
            await db.execute(
                select(BucketFile).where(
                    BucketFile.bucket_id == intake.bucket_id,
                    BucketFile.requested_document_id == req.id,
                    BucketFile.status.in_(("uploading", "uploaded")),
                    BucketFile.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if existing_for_doc is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "This requested document only allows one file")
    _, prefix, _ = _bucket_storage_config()
    file_id = uuid4()
    safe = _safe_filename(payload.file_name)
    s3_key = f"{prefix}/uploads/{intake.bucket_id}/{file_id}-{safe}"
    file = BucketFile(
        id=file_id,
        bucket_id=intake.bucket_id,
        requested_document_id=payload.requested_document_id,
        upload_link_id=intake.bucket_upload_link_id,
        file_name=payload.file_name,
        s3_key=s3_key,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        uploaded_by_name=actor_name,
        uploaded_by_email=actor_email,
        status="uploading",
    )
    db.add(file)
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_file_upload_started",
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role="public_lead",
        target_type="file",
        target_id=str(file.id),
        detail=payload.file_name,
    )
    await db.commit()
    upload_url, headers = _upload_url(s3_key, payload.content_type)
    return BucketFileUploadInitResponse(file_id=file.id, upload_url=upload_url, s3_key=s3_key, required_headers=headers)


async def _complete_upload(
    db: AsyncSession,
    intake: PublicUnderwritingIntake,
    payload: DealerUploadComplete,
    request: Request,
    *,
    actor_name: str,
    actor_email: str,
) -> BucketFile:
    file = await db.get(BucketFile, payload.file_id)
    if file is None or file.bucket_id != intake.bucket_id or file.upload_link_id != intake.bucket_upload_link_id or file.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    if file.status != "uploaded":
        file.status = "uploaded"
        if file.requested_document_id:
            req = await db.get(BucketRequestedDocument, file.requested_document_id)
            if req is not None:
                req.status = "uploaded"
        if intake.bucket_upload_link is not None:
            intake.bucket_upload_link.completed_at = _now()
    if payload.note:
        db.add(
            BucketNote(
                bucket_id=intake.bucket_id,
                author_name=actor_name,
                author_role="public_lead",
                visibility="shared",
                content=payload.note,
            )
        )
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_file_uploaded",
        request=request,
        actor_name=actor_name,
        actor_email=actor_email,
        actor_role="public_lead",
        target_type="file",
        target_id=str(file.id),
        detail=file.file_name,
    )
    await db.commit()
    await db.refresh(file)
    return file


@router.post("/start", response_model=DealerIntakeResponse, status_code=status.HTTP_201_CREATED)
async def start_dealer_intake(
    payload: DealerIntakeStart,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    client = await _find_or_create_client(db, payload)
    bucket, link = await _create_bucket_for_intake(db, client, payload, request)
    token = _new_public_token()
    intake = PublicUnderwritingIntake(
        client_id=client.id,
        bucket_id=bucket.id,
        bucket_upload_link_id=link.id,
        token_hash=_hash_token(token),
        full_name=payload.full_name.strip(),
        email=client.email or _normalize_email(str(payload.email)),
        phone=payload.phone,
        business_name=payload.business_name,
        intake_state={"messages": [], "source": "dealer_ai_intake"},
    )
    db.add(intake)
    await db.commit()
    intake = await _load_public_intake(db, token)
    return _response(
        intake,
        token=token,
        assistant_message="I can screen this dealer financing file. I will collect the core facts, upload the documents into a secure bucket, then give a strict preliminary bankability answer.",
    )


@router.get("/{token}", response_model=DealerIntakeResponse)
async def get_dealer_intake(token: str, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    return _response(intake, token=token)


@router.patch("/{token}", response_model=DealerIntakeResponse)
async def update_dealer_intake(
    token: str,
    payload: DealerIntakePatch,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _apply_updates(intake, payload)
    await db.commit()
    intake = await _load_public_intake(db, token)
    return _response(intake, token=token)


@router.post("/{token}/chat", response_model=DealerIntakeResponse)
async def dealer_intake_chat(
    token: str,
    payload: DealerChatRequest,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    _apply_updates(intake, payload.updates)
    intake.last_message_at = _now()
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="uploader",
            message=payload.message.strip(),
            actor_name=intake.full_name,
            upload_link=intake.bucket_upload_link,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
    await db.commit()
    intake = await _load_public_intake(db, token)
    return _response(intake, token=token, assistant_message=assistant_message, messages=messages)


@router.post("/{token}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def dealer_upload_init(
    token: str,
    payload: DealerFileUploadInit,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_public_intake(db, token)
    return await _start_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/files/complete", response_model=BucketFileRead)
async def dealer_upload_complete(
    token: str,
    payload: DealerUploadComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_public_intake(db, token)
    return await _complete_upload(db, intake, payload, request, actor_name=intake.full_name, actor_email=intake.email)


@router.post("/{token}/run-review", response_model=DealerIntakeResponse)
async def run_dealer_review(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_public_intake(db, token)
    intake.bucket.ai_context = {**(intake.bucket.ai_context or {}), **_dealer_context(intake)}
    review = BucketAIReview(
        bucket_id=intake.bucket_id,
        requested_by_user_id=None,
        status="queued",
        context_snapshot=intake.bucket.ai_context or {},
        file_ids=[str(file.id) for file in _active_files(intake.bucket)],
        provider="bedrock",
    )
    db.add(review)
    await db.flush()
    await _log(
        db,
        intake.bucket_id,
        "dealer_ai_review_queued",
        request=request,
        actor_name=intake.full_name,
        actor_email=intake.email,
        actor_role="public_lead",
        target_type="ai_review",
        target_id=str(review.id),
        detail="Public dealer AI screen",
    )
    await run_bucket_ai_review(db, review.id)
    fresh_review = await latest_review(db, intake.bucket_id)
    intake.latest_review_id = fresh_review.id if fresh_review else review.id
    if fresh_review and isinstance(fresh_review.result, dict):
        intake.result_snapshot = fresh_review.result
        intake.status = "reviewed"
        intake.completed_at = _now()
    await db.commit()
    intake = await _load_public_intake(db, token)
    return _response(intake, token=token)


@client_router.get("", response_model=list[DealerIntakeRead])
async def list_my_dealer_intakes(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> list[DealerIntakeRead]:
    if user.role != Role.CLIENT or user.client is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client account required")
    rows = (
        await db.execute(
            select(PublicUnderwritingIntake)
            .where(PublicUnderwritingIntake.client_id == user.client.id)
            .order_by(PublicUnderwritingIntake.updated_at.desc())
        )
    ).scalars().all()
    return [DealerIntakeRead.model_validate(row) for row in rows]


@client_router.get("/{intake_id}", response_model=DealerIntakeResponse)
async def get_my_dealer_intake(intake_id: UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)) -> DealerIntakeResponse:
    intake = await _load_client_intake(db, user, intake_id)
    return _response(intake, token=None)


@client_router.post("/{intake_id}/chat", response_model=DealerIntakeResponse)
async def my_dealer_intake_chat(
    intake_id: UUID,
    payload: DealerChatRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DealerIntakeResponse:
    intake = await _load_client_intake(db, user, intake_id)
    _apply_updates(intake, payload.updates)
    messages = []
    assistant_message = None
    if payload.message and payload.message.strip():
        chat_messages, _ = await create_chat_reply(
            db,
            bucket=intake.bucket,
            audience="uploader",
            message=payload.message.strip(),
            actor_name=user.name or intake.full_name,
            user=user,
            upload_link=intake.bucket_upload_link,
        )
        messages = chat_messages
        if chat_messages:
            assistant_message = chat_messages[-1].content
    await db.commit()
    intake = await _load_client_intake(db, user, intake_id)
    return _response(intake, token=None, assistant_message=assistant_message, messages=messages)


@client_router.post("/{intake_id}/files/upload-init", response_model=BucketFileUploadInitResponse)
async def my_dealer_upload_init(
    intake_id: UUID,
    payload: DealerFileUploadInit,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFileUploadInitResponse:
    intake = await _load_client_intake(db, user, intake_id)
    return await _start_upload(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)


@client_router.post("/{intake_id}/files/complete", response_model=BucketFileRead)
async def my_dealer_upload_complete(
    intake_id: UUID,
    payload: DealerUploadComplete,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> BucketFile:
    intake = await _load_client_intake(db, user, intake_id)
    return await _complete_upload(db, intake, payload, request, actor_name=user.name or intake.full_name, actor_email=user.email)
