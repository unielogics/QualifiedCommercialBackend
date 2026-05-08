"""Per-broker settings overlay (alembic 0023, reshaped post-codex-PR).

Stored on `Broker.settings_data` as JSONB. Layered on top of the
firm `AppSettingsData` for any loan owned by this broker.

Shape changes from v1 (loan-type axis dropped):

  - `checklists`  was keyed `f"{loan_type}:{side}"` (12 sub-tabs);
                  now keyed by `side` ONLY (`"buyer"` | `"seller"`).
                  Realtors think transaction-side, not DSCR/F&F.
  - `cadence`     was `dict[loan_type, AgentCadenceOverride]`; now a
                  single `AgentCadenceOverride` applied to all loans
                  this broker owns regardless of loan type.
  - `letterhead`  drops the duplicated identity fields (display_name,
                  email, phone, signature_block) — those come from the
                  User row (Clerk-synced) or are not relevant to
                  realtors. Adds `logo_data_url` so each agent can
                  brand docs with their firm's logo + their own
                  headshot.

Endpoints: GET / PUT /me/broker-settings — see app/routers/me.py.

Tolerant parsing: existing JSONB rows from v1 (loan-type-keyed
checklists / cadence) are ignored on read — agents see a clean
slate. v1 cadence values are NOT collapsed automatically; a broker
who configured per-loan-type cadence (rare) re-saves once.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.schemas.settings import DocChecklistItem


class AgentChecklistOverlay(BaseModel):
    """Per-side overlay layered on top of the firm baseline. The
    agent's loans use firm + this overlay; non-agent loans use firm
    only."""

    # Names of FIRM checklist items the agent wants to suppress on
    # their own loans. Match by `DocChecklistItem.name`.
    disabled_firm_items: list[str] = Field(default_factory=list)
    # Agent's own additions. Same `DocChecklistItem` schema as the
    # firm uses, but `internal_action` is ignored (agent items are
    # always external borrower-uploads — internals stay firm-owned).
    extra_items: list[DocChecklistItem] = Field(default_factory=list)


class AgentCadenceOverride(BaseModel):
    """Per-broker reminder cadence override. NULL fields fall back to
    the firm's `LoanTypeChecklist` defaults at evaluation time."""

    first_reminder_days: int | None = None
    second_reminder_days: int | None = None
    escalate_after_days: int | None = None


class AgentLetterhead(BaseModel):
    """Agent-personal letterhead. Populated by the agent's
    /agent-settings page.

    Identity fields (name, email, phone) live on the User row and are
    NOT duplicated here — agent settings reads them from /auth/me.
    Realtors don't sign loan docs, so no signature block.

    Headshot can be supplied as either a base64 data URL
    (`headshot_data_url`, kept for v1 backwards compat) OR an S3 key
    (`headshot_s3_key`). The S3 key path is preferred — production
    PDF rendering pulls from S3 to avoid bloating the JSONB row with
    inline base64. When both are present the S3 key wins."""

    title: str | None = None
    license_number: str | None = None
    brokerage_name: str | None = None
    # Firm logo + realtor headshot — base64 data URLs (v1).
    logo_data_url: str | None = None
    headshot_data_url: str | None = None
    # S3-backed headshot (production path). Set via the
    # /me/broker-settings/headshot/upload-init flow.
    headshot_s3_key: str | None = None


class AgentSettingsData(BaseModel):
    """Full per-broker settings blob. Lives on
    `brokers.settings_data` (JSONB). Every field has sensible
    defaults so an empty record still parses + the resolver
    treats it as a no-op overlay."""

    # Per-side overlay. Keys: `"buyer"` | `"seller"`. Missing keys
    # mean "firm baseline applies as-is for that side."
    checklists: dict[str, AgentChecklistOverlay] = Field(default_factory=dict)
    # Single cadence override applied to ALL loans this broker owns
    # (loan-type axis dropped post-PR).
    cadence: AgentCadenceOverride | None = None
    # Personal identity / letterhead.
    letterhead: AgentLetterhead | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1_shapes(cls, data: Any) -> Any:
        """Tolerantly accept v1 JSONB rows (codex-PR shape).

          - `checklists` keyed `f"{loan_type}:{side}"` → drop those
            keys; only buyer / seller are kept.
          - `cadence` as a `dict[loan_type, ...]` → collapse to a
            single AgentCadenceOverride (first non-null value per
            field across all loan types wins) so old configs aren't
            silently lost.

        New shape passes through unchanged. Empty dict / None also
        passes through as no-op."""
        if not isinstance(data, dict):
            return data

        # 1. checklists — strip loan-type-prefixed keys
        checklists = data.get("checklists")
        if isinstance(checklists, dict):
            cleaned: dict[str, Any] = {}
            for k, v in checklists.items():
                if k in ("buyer", "seller"):
                    cleaned[k] = v
                # legacy "loan_type:side" keys are dropped; the
                # surviving "buyer"/"seller" entries (if any) win.
            data["checklists"] = cleaned

        # 2. cadence — collapse old dict[loan_type, X] to single X.
        cadence = data.get("cadence")
        if isinstance(cadence, dict):
            # Heuristic: if any value is a dict (i.e. dict-of-overrides),
            # we're in v1 shape. Collapse.
            if cadence and any(isinstance(v, dict) for v in cadence.values()):
                merged: dict[str, Any] = {
                    "first_reminder_days": None,
                    "second_reminder_days": None,
                    "escalate_after_days": None,
                }
                for v in cadence.values():
                    if not isinstance(v, dict):
                        continue
                    for key in merged:
                        if merged[key] is None and v.get(key) is not None:
                            merged[key] = v[key]
                # If everything's null, fall back to None
                data["cadence"] = (
                    merged if any(v is not None for v in merged.values()) else None
                )

        return data


class AgentSettingsRead(BaseModel):
    """GET /me/broker-settings response."""
    data: AgentSettingsData
