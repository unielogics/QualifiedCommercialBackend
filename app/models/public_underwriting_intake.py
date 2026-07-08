from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.bucket import Bucket, BucketAIReview, BucketUploadLink
    from app.models.client import Client
    from app.models.user import User


class PublicUnderwritingIntake(TimestampMixin, Base):
    __tablename__ = "public_underwriting_intakes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bucket_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bucket_upload_link_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_upload_links.id", ondelete="SET NULL"), nullable=True, index=True
    )
    latest_review_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bucket_ai_reviews.id", ondelete="SET NULL"), nullable=True, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    variant: Mapped[str] = mapped_column(String(64), nullable=False, default="dealer_financing_v1", server_default="dealer_financing_v1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting", server_default="collecting")

    full_name: Mapped[str] = mapped_column(String(180), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(48), nullable=True)
    business_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    loan_purpose: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_loan_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    estimated_credit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    referral_source: Mapped[str | None] = mapped_column(String(180), nullable=True)

    asset_rows: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    intake_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client | None] = relationship()
    bucket: Mapped[Bucket] = relationship()
    bucket_upload_link: Mapped[BucketUploadLink | None] = relationship()
    latest_review: Mapped[BucketAIReview | None] = relationship()


class PublicUnderwritingIntakeArtifact(TimestampMixin, Base):
    __tablename__ = "public_underwriting_intake_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intake_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    s3_key: Mapped[str | None] = mapped_column(String(700), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    intake: Mapped[PublicUnderwritingIntake] = relationship()
    created_by_user: Mapped[User | None] = relationship()


class PublicUnderwritingIntakeEmailSend(TimestampMixin, Base):
    __tablename__ = "public_underwriting_intake_email_sends"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    intake_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intakes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    executive_summary_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intake_artifacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lender_packet_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public_underwriting_intake_artifacts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    to_emails: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    cc_emails: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_access_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    ses_status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending", server_default="pending")
    ses_message_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    ses_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    intake: Mapped[PublicUnderwritingIntake] = relationship()
    executive_summary_artifact: Mapped[PublicUnderwritingIntakeArtifact | None] = relationship(
        foreign_keys=[executive_summary_artifact_id]
    )
    lender_packet_artifact: Mapped[PublicUnderwritingIntakeArtifact | None] = relationship(
        foreign_keys=[lender_packet_artifact_id]
    )
    sent_by_user: Mapped[User | None] = relationship()
