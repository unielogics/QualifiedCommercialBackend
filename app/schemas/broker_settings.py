"""Per-broker settings overlay (alembic 0023).

Stored on `Broker.settings_data` as JSONB. Layered on top of the
firm `AppSettingsData` for any loan owned by this broker:
  - `checklists` overlays per (loan_type, side) — agent can disable
    specific firm items and add their own custom rows.
  - `cadence` overrides the firm's reminder windows for the agent's
    loans (NULL fields fall back to firm).
  - `letterhead` is the agent's personal signing identity for any
    docs they generate (replaces the localStorage-only v1 store).

Endpoints: GET / PUT /me/broker-settings — see app/routers/me.py.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.settings import DocChecklistItem


class AgentChecklistOverlay(BaseModel):
    """Per-(loan_type, side) overlay layered on top of the firm
    baseline. The agent's loans use firm + this overlay; non-agent
    loans use firm only."""

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
    /agent-settings page. Replaces the v1 localStorage store."""

    display_name: str | None = None
    title: str | None = None
    phone: str | None = None
    email: str | None = None
    license_number: str | None = None
    brokerage_name: str | None = None
    # Base64 data URL — fine for ~50KB headshots. v2 follow-up:
    # same S3 upload flow as the firm's `signature_s3_key`.
    headshot_data_url: str | None = None
    signature_block: str | None = None


class AgentSettingsData(BaseModel):
    """Full per-broker settings blob. Lives on
    `brokers.settings_data` (JSONB). Every field has sensible
    defaults so an empty record still parses + the resolver
    treats it as a no-op overlay."""

    # Per-(loan_type, side) overlay. Keys: f"{loan_type}:{side}"
    # e.g. "dscr:buyer", "fix_and_flip:seller". Missing keys mean
    # "firm baseline applies as-is for that combination."
    checklists: dict[str, AgentChecklistOverlay] = Field(default_factory=dict)
    # Per-loan-type cadence override (one entry per loan_type).
    cadence: dict[str, AgentCadenceOverride] = Field(default_factory=dict)
    # Personal identity / letterhead.
    letterhead: AgentLetterhead | None = None


class AgentSettingsRead(BaseModel):
    """GET /me/broker-settings response."""
    data: AgentSettingsData
