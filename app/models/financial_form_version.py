"""Financial document families, editable drafts, and immutable dated versions.

Published figures still project into the existing financial tables. Drafts are
kept apart so typing cannot change underwriting or overwrite a filed PDF.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FinancialFormFamily(Base):
    __tablename__ = "financial_form_families"
    __table_args__ = (
        UniqueConstraint("profile_id", "kind", "identity_key", name="uq_financial_form_family_identity"),
        CheckConstraint("kind in ('pfs','debt_schedule','p_and_l','balance_sheet','real_estate_schedule')", name="ck_financial_form_family_kind"),
        Index("ix_financial_form_family_profile_kind", "profile_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("application_profiles.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_form_versions.id", ondelete="SET NULL", use_alter=True, name="fk_financial_family_current_version"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FinancialFormDraft(Base):
    __tablename__ = "financial_form_drafts"

    family_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_form_families.id", ondelete="CASCADE"), primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_form_versions.id", ondelete="SET NULL", use_alter=True, name="fk_financial_draft_base_version"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone())
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))


class FinancialFormVersion(Base):
    __tablename__ = "financial_form_versions"
    __table_args__ = (
        UniqueConstraint("family_id", "version_number", name="uq_financial_form_version_number"),
        UniqueConstraint("family_id", "idempotency_key", name="uq_financial_form_version_request"),
        Index("ix_financial_form_version_source", "source_file_id"),
        Index("ix_financial_form_version_pdf", "pdf_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    family_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_form_families.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    key_facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="published")
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_form_versions.id", ondelete="SET NULL"))
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL"))
    pdf_file_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="SET NULL"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    actor_name: Mapped[str | None] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone())


class FinancialFormImport(Base):
    __tablename__ = "financial_form_imports"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    family_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_form_families.id", ondelete="CASCADE"), nullable=False, index=True)
    source_file_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bucket_files.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    proposed_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    mappings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone())
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
