"""Contact-gated resource reports; capabilities grant only submitted resource data."""
# FastAPI evaluates dependency markers, not Python's default-call lifecycle.
# ruff: noqa: B008
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.phone import RequiredPhone
from app.services import resource_assessments as service


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(prefix="/public/resources", tags=["public-resources"], dependencies=[Depends(no_store)])
Goal = Literal["mca_refinance", "merchant_cash_advance", "term_loan"]


class ResourceContact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=160)
    business_name: str = Field(min_length=2, max_length=180)
    email: EmailStr
    phone: RequiredPhone

    @field_validator("email", mode="after")
    @classmethod
    def normalize_email(cls, value):
        return str(value).casefold()


class ResourceLead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    resource_id: Literal["business_funding_check"]
    goal: Goal
    locale: Literal["en", "es"] = "en"
    answers: dict[str, Any]
    contact: ResourceContact
    email_marketing_consent: bool = Field(default=False, strict=True)
    lending_updates_consent: bool = Field(default=False, strict=True)
    privacy_acknowledged: Literal[True]
    attribution: dict[str, Any] = Field(default_factory=dict)
    website: str = Field(default="", max_length=0)

    @field_validator("answers", "attribution")
    @classmethod
    def bounded_json(cls, value):
        return service.validate_json_object(value)


class ResourceAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_token: str = Field(min_length=64, max_length=180, repr=False)


class ResourceReport(ResourceAccess):
    assumptions: dict[str, Any] | None = None

    @field_validator("assumptions")
    @classmethod
    def bounded_json(cls, value):
        return service.validate_json_object(value) if value is not None else None


@router.post("/leads", status_code=201)
async def capture_resource(payload: ResourceLead, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        return await service.capture(db, payload, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/handoff")
async def resource_handoff(payload: ResourceAccess, db: AsyncSession = Depends(get_db)):
    capture = await service.resolve(db, payload.resource_token)
    return {"goal": capture.goal, "locale": capture.locale, "contact": capture.contact,
            "answers": capture.answers, "attribution": capture.attribution}


@router.post("/report")
async def resource_report(payload: ResourceReport, db: AsyncSession = Depends(get_db)):
    from app.services.funding_resource_calculations import calculate_funding_report

    capture = await service.resolve(db, payload.resource_token)
    try:
        assumptions = payload.assumptions if payload.assumptions is not None else capture.assumptions or None
        report = calculate_funding_report(capture.goal, capture.answers, assumptions)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    capture.assumptions = assumptions or {}
    capture.report = report
    await db.commit()
    return {"report": report}
