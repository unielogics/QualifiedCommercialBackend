"""Settings shape — typed sections that mirror the desktop Settings page."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --- Section: doc checklists ---------------------------------------------

class DocChecklistItem(BaseModel):
    name: str
    required: bool = True
    auto_request: bool = True


class LoanTypeChecklist(BaseModel):
    docs: list[DocChecklistItem] = Field(default_factory=list)
    first_reminder_days: int = 3
    second_reminder_days: int = 7
    escalate_after_days: int = 14
    auto_approve_risk_score: int = 90


# --- Section: AI cadence -------------------------------------------------

class AICadence(BaseModel):
    morning_digest: str = "08:00"
    evening_summary: str = "17:30"
    auto_nudge_borrower: bool = True
    auto_escalate_overdue: bool = True
    auto_draft_replies: bool = True
    anomaly_alerts: bool = True
    weekend_ops: bool = False
    confidence_floor_default: float = 0.80  # 0..1


# --- Section: referrals --------------------------------------------------

class ReferralSettings(BaseModel):
    require_approval: bool = True
    auto_link_from_url: bool = True
    block_re_attribution: bool = True
    notify_broker_on_signup: bool = True
    points_per_dollar: float = 1.0
    refi_multiplier: float = 1.25
    expiry_days: int = 365
    dispute_sla_business_days: int = 5


# --- Section: pricing ----------------------------------------------------

class PricingSettings(BaseModel):
    daily_pull_time: str = "07:00"
    auto_publish_threshold_bps: int = 25
    notify_clients_on_change: bool = True
    lock_window_business_days: int = 5


# --- Section: security --------------------------------------------------

class SecuritySettings(BaseModel):
    sso_enabled: bool = True
    mfa_enforced: bool = True
    mfa_renewal_days: int = 14
    borrower_portal_mfa: bool = False
    session_timeout_minutes: int = 30
    ip_allowlist: list[str] = Field(default_factory=list)


# --- Section: simulator -------------------------------------------------

class SimulatorSettings(BaseModel):
    """Bounds + toggles for the borrower-facing Simulator screen.

    Super-admins set the ranges; the screen renders sliders / chips clamped
    to these limits. `advanced_mode_enabled` exposes the taxes/insurance/HOA
    inputs in the UI (the recalc endpoint always accepts them).
    """
    points_min: float = 0.0
    points_max: float = 3.0
    points_step: float = 0.5
    amount_min: float = 100_000
    amount_max: float = 5_000_000
    amount_step: float = 25_000
    ltv_min: float = 0.50  # 0..1
    ltv_max: float = 0.90
    ltv_step: float = 0.05
    advanced_mode_enabled: bool = True
    show_taxes: bool = True
    show_insurance: bool = True
    show_hoa: bool = True
    show_ltv_toggle: bool = True


# --- Section: prequal auto-approval ------------------------------------

class PrequalAutoApprovalSettings(BaseModel):
    """Deterministic gate for auto-approving prequalification requests.
    See app/services/prequal_auto_approve.py — only when ALL conditions
    pass does the system approve without admin review. Anything that
    falls through stays in `status='pending'` for manual review.

    The kill-switch: set `enabled=False` to revert to 100% manual
    approval if the auto path ever causes a problem in prod."""
    enabled: bool = True

    # Borrowers below this score never auto-approve. Tighter than the
    # tier_max_ltv gate because we want a margin of safety even
    # within the "basic" tier.
    fico_floor: int = 660

    # Hard ceiling on auto-approved loan size. Anything above this
    # always lands on the admin's desk no matter how clean the math.
    safety_loan_ceiling_usd: int = 1_000_000

    # Block auto-approve when the borrower has no credit pull on
    # file. Prevents the system from issuing a letter against a
    # borrower whose risk profile we haven't actually verified.
    require_credit_pull: bool = True


# --- Section: letterhead ------------------------------------------------

class LetterheadSettings(BaseModel):
    """Configurable letterhead values that get rendered into every
    pre-qualification PDF — office address, signing officer's name and
    title, and the officer's saved signature image (stored in S3, fetched
    + base64-embedded by the PDF renderer at render time).

    Edited by SUPER_ADMIN only via the firm-letterhead settings page.
    Used by app/services/prequal_pdf.py:render_letter.
    """
    # Officer / signer identity (full first + last name).
    officer_name: str = "Franco Pellegrino"
    officer_title: str = "Managing Director | Qualified Commercial LLC"

    # Three address lines on the letterhead header (top-right block).
    # Empty string = blank that line.
    office_address_line_1: str = "123 Financial Way, Suite 400"
    office_address_line_2: str = "Garfield, NJ 07026"
    office_address_line_3: str = "www.qualifiedcommercial.com"

    # S3 key of the uploaded signature image (transparent PNG ideal).
    # None until the super admin uploads one — templates fall back to a
    # plain underline + typed name in that case.
    signature_s3_key: str | None = None


# --- Aggregate ----------------------------------------------------------

class AppSettingsData(BaseModel):
    """Full settings blob. Each section has sensible defaults so a bare table
    row still produces usable values for the UI."""
    checklists: dict[str, LoanTypeChecklist] = Field(default_factory=dict)
    ai_cadence: AICadence = Field(default_factory=AICadence)
    referrals: ReferralSettings = Field(default_factory=ReferralSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    simulator: SimulatorSettings = Field(default_factory=SimulatorSettings)
    letterhead: LetterheadSettings = Field(default_factory=LetterheadSettings)
    prequal_auto_approval: PrequalAutoApprovalSettings = Field(default_factory=PrequalAutoApprovalSettings)


class AppSettingsRead(BaseModel):
    data: AppSettingsData


class AppSettingsUpdate(BaseModel):
    """PATCH body — every section is optional. We deep-merge keys present in
    the payload onto the persisted JSONB and leave the rest untouched."""
    checklists: dict[str, LoanTypeChecklist] | None = None
    ai_cadence: AICadence | None = None
    referrals: ReferralSettings | None = None
    pricing: PricingSettings | None = None
    security: SecuritySettings | None = None
    simulator: SimulatorSettings | None = None
    letterhead: LetterheadSettings | None = None
    prequal_auto_approval: PrequalAutoApprovalSettings | None = None


# --- Signature image upload ---------------------------------------------

class SignatureUploadInitResponse(BaseModel):
    """Response from POST /settings/letterhead/signature/upload-init.

    s3_key      — caller PUTs the bytes to upload_url and then PATCHes
                  /settings with letterhead.signature_s3_key set to this.
    upload_url  — presigned PUT URL (expires in 5 min). None when the
                  backend is running without S3 credentials (local dev).
    """
    s3_key: str
    upload_url: str | None
