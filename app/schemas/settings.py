"""Settings shape — typed sections that mirror the desktop Settings page."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Section: doc checklists ---------------------------------------------

class DocChecklistItem(BaseModel):
    """One row in the per-loan-type doc checklist.

    Backwards compatible: every new field has a default, so older
    AppSettings JSONB blobs still parse cleanly.
    """

    # Stable internal key — used to match Document.checklist_key,
    # to anchor dependents, and as the de-dup key on retry. Keep
    # short + canonical (e.g. "Bank Statements (2 mo)"). Don't show
    # this raw string to borrowers if `display_name` is set.
    name: str
    # What the borrower / operator sees in the UI. Falls back to
    # `name` when null. Lets us write "Personal Financial Statement
    # (PFS)" without changing the internal key.
    display_name: str | None = None
    # Who owns the item:
    #   external — borrower uploads (Lease, Bank Statements, etc.).
    #              Spawns Document(REQUESTED) + calendar event.
    #   internal — operator orders (Appraisal, Title, Insurance,
    #              PFS). Spawns AITask only — no Document, no
    #              calendar event. Borrower never sees it.
    type: Literal["internal", "external"] = "external"
    required: bool = True
    auto_request: bool = True
    # Days from when this item's anchor event fires until the doc
    # is "due" (drives the calendar event's starts_at). Used by
    # both kickoff + the eager anchor scheduler.
    due_offset_days: int = 3
    # When this item gets created. One of:
    #   "loan_created"               — at kickoff
    #   "doc_received:<doc name>"    — when the named doc flips to
    #                                  RECEIVED (eager scheduler
    #                                  fires this in
    #                                  /documents/upload-complete)
    anchor: str = "loan_created"
    # When True, fan out to N copies on kickoff — one per unit on
    # the property. e.g. a 4-plex with per_unit=True on "Lease"
    # creates 4 Documents named "Lease — Unit 1" through
    # "Lease — Unit 4". Each carries `checklist_key=<name>` so
    # uploads still match the right slot.
    per_unit: bool = False
    # For internal items only — short tag the operator UI uses to
    # render the right CTA on the AITask. Examples:
    #   order_appraisal | order_title | shop_insurance | request_pfs
    internal_action: str | None = None
    # Which side of the transaction this item applies to (alembic
    # 0023). The kickoff cron filters by `loan.side`:
    #   "buyer"  — only materialize when loan.side == buyer
    #   "seller" — only materialize when loan.side == seller
    #   "both"   — fires regardless (default; preserves existing
    #              behavior for un-split firm checklists)
    side: Literal["buyer", "seller", "both"] = "both"


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
    # Firm master switches for the Google-connected automated emails (Phase 4).
    # A per-user toggle (google_accounts.automation_settings) must ALSO be on.
    auto_re_agent_emails: bool = False
    auto_status_change_emails: bool = False


# --- Section: AI spend monitoring ----------------------------------------

class AISpendSettings(BaseModel):
    """Super-admin managed AI spend policy.

    Thresholds are alert-only by default. Manual emergency switches can
    pause categories, but crossing a threshold never shuts down agent
    workflows by itself.
    """
    daily_warning_usd: float = 10.0
    daily_critical_usd: float = 25.0
    avg_client_file_warning_usd: float = 1.50
    avg_client_file_critical_usd: float = 3.00
    master_enabled: bool = True
    chat_enabled: bool = True
    automations_enabled: bool = True
    document_scanning_enabled: bool = True
    summaries_enabled: bool = True
    lender_ai_enabled: bool = True


class PropertyIntelligenceSettings(BaseModel):
    """Provider-backed property intelligence controls.

    Secrets live in provider_secrets; these are safe non-secret toggles
    that can be carried in the normal settings blob.
    """
    ai_report_enabled: bool = True
    cache_ttl_hours: int = Field(default=24, ge=1, le=720)


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

_DEFAULT_TRANSACTION_CHECKLISTS: dict[str, "LoanTypeChecklist"] = {
    "buyer": LoanTypeChecklist(
        docs=[
            DocChecklistItem(name="Government ID", side="buyer"),
            DocChecklistItem(name="Pre-Approval Letter", side="buyer"),
            DocChecklistItem(name="Buyer Agency Agreement", side="buyer"),
            DocChecklistItem(name="Purchase Agreement", side="buyer"),
            DocChecklistItem(name="Earnest Money Receipt", side="buyer"),
            DocChecklistItem(name="Inspection Report", side="buyer"),
            DocChecklistItem(name="Proof of Funds", side="buyer"),
        ],
    ),
    "seller": LoanTypeChecklist(
        docs=[
            DocChecklistItem(name="Government ID", side="seller"),
            DocChecklistItem(name="Listing Agreement", side="seller"),
            DocChecklistItem(name="Property Disclosure", side="seller"),
            DocChecklistItem(name="HOA Documents", side="seller"),
            DocChecklistItem(name="Lead-Based Paint Disclosure", side="seller"),
            DocChecklistItem(name="Title / Deed", side="seller"),
            DocChecklistItem(name="Agency Disclosure", side="seller"),
        ],
    ),
}


class DscrRateTier(BaseModel):
    """One credit tier for DSCR-potential pricing: files at or above min_fico
    (and below the next-higher tier) price from annual_rate (decimal)."""
    min_fico: int = Field(ge=300, le=850)
    annual_rate: float = Field(gt=0.0, lt=0.30)


class DscrPricingSettings(BaseModel):
    """Rate assumptions for the deterministic DSCR-potential screen on
    real-estate leads (app/routers/dealer_ai_intake.py::_compute_dscr_potential).
    Admin-editable so pricing moves without a deploy. The screen shows three
    scenarios: base rate for the file's credit tier ± band_spread."""
    rate_tiers: list[DscrRateTier] = Field(
        default_factory=lambda: [
            DscrRateTier(min_fico=760, annual_rate=0.0675),
            DscrRateTier(min_fico=700, annual_rate=0.075),
            DscrRateTier(min_fico=300, annual_rate=0.0825),
        ]
    )
    band_spread: float = Field(default=0.0075, ge=0.0, lt=0.05)
    amortization_months: int = Field(default=360, ge=60, le=480)
    tax_insurance_annual_pct_of_value: float = Field(default=0.016, gt=0.0, lt=0.10)


class AppSettingsData(BaseModel):
    """Full settings blob. Each section has sensible defaults so a bare table
    row still produces usable values for the UI."""
    checklists: dict[str, LoanTypeChecklist] = Field(default_factory=dict)
    # Transaction-side defaults (alembic 0025 / realtor overhaul). Keyed by
    # `"buyer"` | `"seller"`. Used by the agent's lead-stage checklist surfaces
    # (settings page, AddLeadWizard) — distinct from `checklists` above which
    # is keyed by loan type and drives funding-stage doc collection.
    transaction_checklists: dict[str, LoanTypeChecklist] = Field(
        default_factory=lambda: dict(_DEFAULT_TRANSACTION_CHECKLISTS)
    )
    ai_cadence: AICadence = Field(default_factory=AICadence)
    ai_spend: AISpendSettings = Field(default_factory=AISpendSettings)
    referrals: ReferralSettings = Field(default_factory=ReferralSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    simulator: SimulatorSettings = Field(default_factory=SimulatorSettings)
    letterhead: LetterheadSettings = Field(default_factory=LetterheadSettings)
    prequal_auto_approval: PrequalAutoApprovalSettings = Field(default_factory=PrequalAutoApprovalSettings)
    property_intelligence: PropertyIntelligenceSettings = Field(default_factory=PropertyIntelligenceSettings)
    dscr_pricing: DscrPricingSettings = Field(default_factory=DscrPricingSettings)


class AppSettingsRead(BaseModel):
    data: AppSettingsData


class AppSettingsUpdate(BaseModel):
    """PATCH body — every section is optional. We deep-merge keys present in
    the payload onto the persisted JSONB and leave the rest untouched."""
    checklists: dict[str, LoanTypeChecklist] | None = None
    transaction_checklists: dict[str, LoanTypeChecklist] | None = None
    ai_cadence: AICadence | None = None
    ai_spend: AISpendSettings | None = None
    referrals: ReferralSettings | None = None
    pricing: PricingSettings | None = None
    security: SecuritySettings | None = None
    simulator: SimulatorSettings | None = None
    letterhead: LetterheadSettings | None = None
    prequal_auto_approval: PrequalAutoApprovalSettings | None = None
    property_intelligence: PropertyIntelligenceSettings | None = None
    dscr_pricing: DscrPricingSettings | None = None


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
