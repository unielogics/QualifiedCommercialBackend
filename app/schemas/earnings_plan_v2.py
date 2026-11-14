"""The earnings plan as the Earnings Plan design models it (`operating-plan-v2`).

One body holds what the business confirmed about its source figures, how its
revenue is made, what each cost follows, and the changes it proposes. The
arithmetic lives in app/services/earnings_plan_math.py; this module only says
what a well-formed plan is.

Staff review decisions sit on each change (`REVIEW_FIELDS`). An ordinary writer
may keep an unchanged decision but never author one; the service enforces that.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, get_args
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "operating-plan-v2"
StreamType = Literal["units", "hourly", "services", "recurring", "rental", "commission", "custom"]
LineBehavior = Literal["fixed", "unit", "pct"]
ReviewDecision = Literal["proposed", "approved", "rejected"]
BaselineTreatment = Literal["unreconciled", "included_in_baseline", "incremental_to_baseline", "outside_operating_earnings"]
CategoryAnswer = Literal["included", "not_applicable", "help"]
Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]

REVIEW_FIELDS = ("status", "reviewed_by_user_id", "reviewed_at", "review_reason", "review_stamp",
                 "baseline_treatment", "baseline_reconciliation_note")
REVIEW_DEFAULTS: dict[str, Any] = {"status": "proposed", "reviewed_by_user_id": None, "reviewed_at": None, "review_reason": "",
                                   "review_stamp": None, "baseline_treatment": "unreconciled", "baseline_reconciliation_note": ""}
ITEM_BLOCKS = ("changes", "growth", "activities")
ALL = "all"

# How each kind of revenue stream is usually described: (label, unit, counts as volume).
STREAM_TEMPLATES: dict[str, dict[str, Any]] = {
    "units": {"label": "Units sold", "unit_word": "unit", "factors": [("Units sold", "units / mo", True), ("Price per unit", "$", False)]},
    "hourly": {"label": "Hourly fees", "unit_word": "billable hour",
               "factors": [("Billable staff", "people", True), ("Hours each", "hrs / mo", True), ("Utilization", "%", True), ("Hourly rate", "$", False)]},
    "services": {"label": "Professional services", "unit_word": "engagement", "factors": [("Engagements", "per mo", True), ("Average fee", "$", False)]},
    "recurring": {"label": "Recurring contracts", "unit_word": "contract-month",
                  "factors": [("Active clients", "clients", True), ("Retention", "%", True), ("Monthly fee", "$", False)]},
    "rental": {"label": "Rental or occupancy", "unit_word": "occupied unit-month",
               "factors": [("Rentable units", "units", True), ("Occupancy", "%", True), ("Monthly rent", "$", False)]},
    "commission": {"label": "Commission or % of volume", "unit_word": "dollar processed",
                   "factors": [("Processed volume", "dollars / mo", True), ("Take rate", "%", False)]},
    "custom": {"label": "Custom metrics", "unit_word": "unit", "factors": [("Metric", "count", True), ("Value per metric", "$", False)]},
}


class _Form(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_unknown(cls, value, info):
        # A cleared amount is unknown, never zero; a boolean is never a number.
        annotation = cls.model_fields[info.field_name].annotation
        if isinstance(value, bool) and annotation is not bool:
            raise ValueError("Use a number or leave the field empty, not a boolean")
        if isinstance(value, str) and not value.strip() and type(None) in get_args(annotation):
            return None
        return value


class Factor(_Form):
    id: Key
    label: str = Field(default="", max_length=120)
    value: float | None = Field(default=None, ge=0, le=1e15)
    unit: str = Field(default="", max_length=40)
    volume: bool = False
    custom: bool = False


class Stream(_Form):
    id: Key
    name: str = Field(default="", max_length=240)
    type: StreamType = "custom"
    added: bool = False
    factors: list[Factor] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def distinct_factors(self):
        if len({row.id for row in self.factors}) != len(self.factors):
            raise ValueError("Each metric of a revenue stream needs its own ID")
        return self


class Line(_Form):
    id: Key
    group: Key
    label: str = Field(default="", max_length=240)
    base: float = Field(default=0, ge=0, le=1e15)
    behavior: LineBehavior = "fixed"
    link: Key = ALL
    added: bool = False


class Target(_Form):
    kind: Literal["pricing", "factor", "line", "group"]
    stream_id: Key | None = None
    factor_id: Key | None = None
    line_id: Key | None = None
    group: Key | None = None

    @model_validator(mode="after")
    def complete(self):
        needed = {"pricing": (), "factor": ("stream_id", "factor_id"), "line": ("line_id",), "group": ("group",)}[self.kind]
        if any(getattr(self, key) is None for key in needed):
            raise ValueError("A change must name what it changes")
        for key in ("stream_id", "factor_id", "line_id", "group"):
            if key not in needed:
                setattr(self, key, None)
        return self


class _Item(_Form):
    id: UUID = Field(default_factory=uuid4)
    start_month: int = Field(default=1, ge=1, le=12)
    setup_cost: float | None = Field(default=None, ge=0, le=1e15)
    setup_cadence: Literal["one_time", "monthly"] = "one_time"
    note: str = Field(default="", max_length=4000)
    evidence_note: str = Field(default="", max_length=4000)
    owner: str = Field(default="", max_length=180)
    included: bool = True
    source_file_ids: list[UUID] = Field(default_factory=list, max_length=100)
    status: ReviewDecision = "proposed"
    reviewed_by_user_id: UUID | None = None
    reviewed_at: datetime | None = None
    review_reason: str = Field(default="", max_length=2000)
    review_stamp: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    baseline_treatment: BaselineTreatment = "unreconciled"
    baseline_reconciliation_note: str = Field(default="", max_length=2000)

    @field_validator("reviewed_at")
    @classmethod
    def aware_review(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Review timestamp must include a timezone")
        return value


class Change(_Item):
    target: Target
    direction: Literal["up", "down"] = "down"
    mode: Literal["amount", "percent"] = "percent"
    value: float | None = Field(default=None, ge=0, le=1e15)


class Growth(_Item):
    name: str = Field(default="", max_length=240)
    monthly_spend: float | None = Field(default=None, ge=0, le=1e15)
    stream_id: Key
    factor_id: Key
    added_per_month: float | None = Field(default=None, ge=0, le=1e15)


class Activity(_Item):
    name: str = Field(default="", max_length=240)
    monthly_amount: float | None = Field(default=None, ge=0, le=1e15)


class Allocation(_Form):
    id: UUID = Field(default_factory=uuid4)
    label: str = Field(default="", max_length=240)
    purpose: Literal["existing", "planned", "reserve", "other"] = "other"
    monthly_amount: float | None = Field(default=None, ge=0, le=1e15)
    notes: str = Field(default="", max_length=4000)


class Correction(_Form):
    amount: float = Field(ge=0, le=1e15)
    reason: str = Field(min_length=3, max_length=4000)


class Source(_Form):
    """What the business confirmed about the figures the plan starts from."""
    stamp: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    confirmations: dict[Key, Literal["confirmed", "corrected"]] = Field(default_factory=dict, max_length=100)
    corrections: dict[Key, Correction] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def corrections_are_marked(self):
        for key in self.corrections:
            self.confirmations[key] = "corrected"
        # A correction that was withdrawn leaves the amount unconfirmed again.
        for key, state in list(self.confirmations.items()):
            if state == "corrected" and key not in self.corrections:
                del self.confirmations[key]
        return self


class CarriedOver(_Form):
    """Something an earlier plan held that this model cannot express; kept for re-entry, never counted."""
    id: UUID = Field(default_factory=uuid4)
    kind: str = Field(default="", max_length=80)
    label: str = Field(default="", max_length=240)
    detail: str = Field(default="", max_length=4000)
    annual_amount: float | None = Field(default=None, ge=-1e15, le=1e15)
    resolved: bool = False


class EarningsPlanBody(_Form):
    schema_version: Literal["operating-plan-v2"] = SCHEMA_VERSION
    business_name: str = Field(default="", max_length=255)
    period_start: date | None = None
    period_end: date | None = None
    currency: str = Field(default="USD", pattern=r"^(?:[A-Z]{3})?$")
    plan_start: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    source: Source = Field(default_factory=Source)
    streams: list[Stream] = Field(default_factory=list, max_length=40)
    lines: list[Line] = Field(default_factory=list, max_length=500)
    category_answers: dict[Key, CategoryAnswer] = Field(default_factory=dict, max_length=50)
    changes: list[Change] = Field(default_factory=list, max_length=300)
    growth: list[Growth] = Field(default_factory=list, max_length=100)
    activities: list[Activity] = Field(default_factory=list, max_length=100)
    allocations: list[Allocation] = Field(default_factory=list, max_length=200)
    carried_over: list[CarriedOver] = Field(default_factory=list, max_length=500)
    attestation: bool = False
    notes: str = Field(default="", max_length=10000)

    @field_validator("period_start", "period_end", mode="before")
    @classmethod
    def empty_dates(cls, value):
        return None if value == "" else value

    @field_validator("currency", mode="before")
    @classmethod
    def normalized_currency(cls, value):
        return str(value or "").strip().upper()

    @model_validator(mode="after")
    def coherent(self):
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("Reporting end must follow reporting start")
        streams = {row.id: row for row in self.streams}
        if len(streams) != len(self.streams):
            raise ValueError("Each revenue stream needs its own ID")
        lines = {row.id: row for row in self.lines}
        if len(lines) != len(self.lines):
            raise ValueError("Each cost line needs its own ID")
        for line in self.lines:
            if line.link != ALL and line.link not in streams:
                raise ValueError("A cost line must follow one of this plan's revenue streams, or all revenue")
        items = [row.id for block in (self.changes, self.growth, self.activities) for row in block]
        if len(set(items)) != len(items):
            raise ValueError("Each change needs its own ID")
        if len({row.id for row in self.allocations}) != len(self.allocations):
            raise ValueError("Each cash allocation needs its own ID")
        places = set()
        for change in self.changes:
            target = change.target
            if target.kind == "factor":
                stream = streams.get(target.stream_id)
                if stream is None or all(factor.id != target.factor_id for factor in stream.factors):
                    raise ValueError("A change must point at a metric of one of this plan's revenue streams")
                place = f"f:{target.stream_id}:{target.factor_id}"
            elif target.kind == "line":
                if target.line_id not in lines:
                    raise ValueError("A change must point at one of this plan's cost lines")
                place = f"l:{target.line_id}"
            elif target.kind == "group":
                if all(line.group != target.group for line in self.lines):
                    raise ValueError("A whole-group change must name a group that has cost lines")
                place = f"g:{target.group}"
            else:
                place = "pricing"
            if place in places:
                raise ValueError("Only one change can apply to the same price, metric, cost or group")
            places.add(place)
        for activity in self.growth:
            stream = streams.get(activity.stream_id)
            factor = next((row for row in stream.factors if row.id == activity.factor_id), None) if stream else None
            if factor is None or not factor.volume:
                raise ValueError("A growth activity must feed a volume metric of one of this plan's revenue streams")
        return self


def empty_body() -> dict:
    return EarningsPlanBody().model_dump(mode="json")


def is_v2(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION


def validate_body(raw: Any) -> dict:
    try:
        return EarningsPlanBody.model_validate(raw).model_dump(mode="json")
    except ValidationError as exc:
        errors = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        raise HTTPException(422, detail={"message": "Review the earnings plan fields", "errors": errors}) from exc


def items(body: dict) -> list[tuple[str, dict]]:
    """Every change, growth activity and new activity with the block it lives in."""
    return [(block, row) for block in ITEM_BLOCKS for row in body.get(block) or []]


def without_changes(body: dict) -> dict:
    """The same plan with nothing proposed: today's figures through the model."""
    return {**body, "changes": [], "growth": [], "activities": []}


def key_facts(raw: Any) -> dict:
    """What a saved version records about itself; never a private review reason or reviewer."""
    body = validate_body(raw)
    private = set(REVIEW_FIELDS) - {"status"}
    safe = {**body, **{block: [{key: value for key, value in row.items() if key not in private} for row in body[block]] for block in ITEM_BLOCKS}}
    return {"source_form": "operating_plan", "document_type": "operating_expenses_and_earnings_plan",
            "business_name": body["business_name"], "period_start": body["period_start"], "period_end": body["period_end"],
            "currency": body["currency"], "basis": None, "operating_plan": safe,
            "financial_fact_policy": "Expense disclosure and operating proposals; not a P&L, current earnings or program qualification."}
