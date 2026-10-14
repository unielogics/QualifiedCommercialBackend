"""ARF public catalog/referral and internal source/permission administration."""
from __future__ import annotations

# ruff: noqa: B008
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.application_profile import ApplicationProfile
from app.models.arf_channel import ARFReferral
from app.models.bucket import Bucket
from app.models.client import Client
from app.models.user import User
from app.services import application_profiles, arf_channel

public_router = APIRouter(prefix="/public/arf", tags=["arf"])
admin_router = APIRouter(prefix="/admin/arf", tags=["arf"])
profile_router = APIRouter(prefix="/application-profiles", tags=["arf"])


class Approval(BaseModel):
    approved: bool = False
    evidence_reference: str | None = Field(default=None, max_length=2000)
    scope: str | None = Field(default=None, max_length=4000)
    @model_validator(mode="after")
    def evidence_required(self):
        if self.approved and not (self.evidence_reference or "").strip():
            raise ValueError("Written evidence reference is required for an approval")
        return self


class ChannelPatch(BaseModel):
    intake_mode: Literal["referral_only", "partner_hosted_authorized"]
    publication_state: Literal["draft_pending_approval", "published", "paused"]
    approvals: dict[str, Approval] = Field(default_factory=dict, max_length=20)
    permitted_fields: list[str] = Field(default_factory=list, max_length=100)
    permitted_documents: list[str] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=3, max_length=2000)
    confirmed: Literal[True]
    @model_validator(mode="after")
    def valid_scope(self):
        if set(self.approvals) - arf_channel.APPROVAL_KEYS:
            raise ValueError("Unknown ARF approval gate")
        if self.intake_mode == "partner_hosted_authorized" and not (self.approvals.get("hosted_intake") and self.approvals["hosted_intake"].approved and self.permitted_fields):
            raise ValueError("Hosted intake requires written approval and an explicit permitted-field scope")
        if self.publication_state == "published" and not all(self.approvals.get(key) and self.approvals[key].approved for key in ("public_copy", "rule_pack")):
            raise ValueError("Publishing requires approved copy and a current rule pack")
        return self


class ReferralCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=32)
    business_name: str | None = Field(default=None, max_length=180)
    program_key: str | None = Field(default=None, max_length=64)
    industry_key: Literal["plumbing", "electrical", "hvac"] | None = None
    source: str | None = Field(default=None, max_length=160)
    campaign: str | None = Field(default=None, max_length=160)
    partner: str | None = Field(default=None, max_length=160)
    consent: Literal[True]
    website: str = Field(default="", max_length=0)
    @model_validator(mode="after")
    def known_product(self):
        if self.program_key and self.program_key not in arf_channel.PRODUCT_BY_KEY:
            raise ValueError("Unknown ARF product")
        return self


def config_read(config):
    return {"channel_key": config.channel_key, "version": config.version, "intake_mode": config.intake_mode, "publication_state": config.publication_state, "approvals": config.approvals or {}, "permitted_fields": config.permitted_fields or [], "permitted_documents": config.permitted_documents or [], "source_version": arf_channel.SOURCE_VERSION, "source_references": arf_channel.SOURCE_REFERENCES, "missing_source_artifacts": ["Complete approved industry taxonomy", "Official lender forms and submission checklist", "Current verified product rule pack"]}


@public_router.get("/catalog")
async def catalog(db: AsyncSession = Depends(get_db)):
    return await arf_channel.public_catalog(db)


@public_router.post("/referrals", status_code=status.HTTP_201_CREATED)
async def referral(payload: ReferralCreate, db: AsyncSession = Depends(get_db)):
    catalog = await arf_channel.public_catalog(db)
    if not catalog["products"]:
        raise HTTPException(409, "This channel is awaiting approved public information")
    if payload.program_key and not any(item["program_key"] == payload.program_key for item in catalog["products"]):
        raise HTTPException(409, "This product is not currently published")
    return await save_contact_referral(payload, db)


async def save_contact_referral(payload: ReferralCreate, db: AsyncSession):
    """Save contact-only intake; this never authorizes hosted financial intake."""
    existing = (await db.execute(select(ARFReferral).where(ARFReferral.idempotency_key == str(payload.idempotency_key)).with_for_update())).scalar_one_or_none()
    if existing:
        if existing.email.casefold() != str(payload.email).casefold():
            raise HTTPException(409, "Start a new contact request for another recipient")
        return {"status": "received", "message": "Your financing review request is already with our team."}
    client = Client(name=payload.full_name.strip(), email=str(payload.email), phone=payload.phone, referral_source="arf_external")
    bucket = Bucket(name=(payload.business_name or payload.full_name).strip(), client_name=payload.full_name.strip(), bucket_type="business_capital_referral", status="collecting_documents", purpose="Business financing review request")
    db.add_all([client, bucket])
    await db.flush()
    profile = ApplicationProfile(client_id=client.id, primary_bucket_id=bucket.id, vertical="main_street", funding_category="arf_external", industry=payload.industry_key, is_draft=True)
    db.add(profile)
    await db.flush()
    db.add(ARFReferral(idempotency_key=str(payload.idempotency_key), profile_id=profile.id, full_name=payload.full_name.strip(), email=str(payload.email), phone=payload.phone, business_name=payload.business_name, program_key=payload.program_key, industry_key=payload.industry_key, attribution={key: value for key, value in {"source": payload.source, "campaign": payload.campaign, "partner": payload.partner}.items() if value}, consent_version="arf-contact-referral-v1", consented_at=datetime.now(UTC)))
    db.add(Activity(actor_label="public_referral", kind="arf.referral_captured", summary="ARF financing review contact request received", payload={"profile_id": str(profile.id), "channel": arf_channel.CHANNEL_KEY, "program_key": payload.program_key}))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(select(ARFReferral).where(ARFReferral.idempotency_key == str(payload.idempotency_key)))).scalar_one_or_none()
        if existing is None or existing.email.casefold() != str(payload.email).casefold():
            raise HTTPException(409, "This contact request could not be saved; please start a new request") from None
    return {"status": "received", "message": "Your financing review request is with our team."}


@admin_router.get("/configuration")
async def configuration(_: User = Depends(require_role(Role.SUPER_ADMIN)), db: AsyncSession = Depends(get_db)):
    return config_read(await arf_channel.get_configuration(db))


@admin_router.put("/configuration")
async def configure(payload: ChannelPatch, user: User = Depends(require_role(Role.SUPER_ADMIN)), db: AsyncSession = Depends(get_db)):
    config = await arf_channel.get_configuration(db)
    config.version += 1
    config.intake_mode = payload.intake_mode
    config.publication_state = payload.publication_state
    config.approvals = {key: item.model_dump() | {"recorded_by": str(user.id), "recorded_at": datetime.now(UTC).isoformat()} for key, item in payload.approvals.items()}
    config.permitted_fields = list(dict.fromkeys(payload.permitted_fields))
    config.permitted_documents = list(dict.fromkeys(payload.permitted_documents))
    config.updated_by_user_id = user.id
    db.add(config)
    db.add(Activity(actor_id=user.id, actor_label="super_admin", kind="arf.configuration_updated", summary="Updated ARF channel permission evidence", payload={"version": config.version, "reason": payload.reason, "intake_mode": config.intake_mode, "publication_state": config.publication_state}))
    await db.commit()
    return config_read(config)


@admin_router.post("/import-snapshot")
async def import_snapshot(user: User = Depends(require_role(Role.SUPER_ADMIN)), db: AsyncSession = Depends(get_db)):
    created = await arf_channel.ensure_catalog(db, user.id)
    db.add(Activity(actor_id=user.id, actor_label="super_admin", kind="arf.snapshot_imported", summary="Imported supplied ARF snapshot as draft products", payload={"source_version": arf_channel.SOURCE_VERSION, "created_program_keys": created}))
    await db.commit()
    return {"created_program_keys": created, "status": "draft_pending_approval", "source_version": arf_channel.SOURCE_VERSION}


@profile_router.get("/{profile_id}/assessment")
async def get_assessment(profile_id: UUID, user: User = Depends(require_role(Role.SUPER_ADMIN, Role.LOAN_EXEC)), db: AsyncSession = Depends(get_db)):
    profile = await application_profiles.load_profile(db, profile_id, user)
    result = await arf_channel.assessment(db, profile, persist=True)
    await db.commit()
    return result


@profile_router.post("/{profile_id}/assessment")
async def save_assessment(profile_id: UUID, user: User = Depends(require_role(Role.SUPER_ADMIN, Role.LOAN_EXEC)), db: AsyncSession = Depends(get_db)):
    profile = await application_profiles.load_profile(db, profile_id, user)
    result = await arf_channel.assessment(db, profile, persist=True)
    await db.commit()
    return result
