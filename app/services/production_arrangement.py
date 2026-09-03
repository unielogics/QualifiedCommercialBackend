"""Production Arrangement economics, ported one-to-one from the design model.

Field names and covenant rows are taken verbatim from the two agreements:
Production Commitment (Schedules A, B, E) and Program Activation (Addendum A,
Schedules 1-5). The A.3 guideline supplies the derived operative requirements.

This module is pure: no database, no I/O, no clock. The router, the PDF
renderer and the frontend mirror all read the same `compute()` result, so the
number a client signs is never computed in a browser.

Shape of an ``arrangement`` (the editable form, stored as JSONB):

    flat snake_case keys for every text / number field (see FIELD_RULES),
    ``products``: {product_key: {on, cur_rate, cur_premium, rate, premium,
                                 repay, comm, admin, retention, term}},
    ``thresholds``: {threshold_key: operative override or ""} (Addendum A.2),
    ``evidence``: list[str] (multi-select).

``cur_rate`` / ``cur_premium`` are what the dealer runs today, verified on the
onsite review. ``rate`` / ``premium`` are what the program commits to. The
delta between them is the whole case for the arrangement.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

STAGE_ONE_TITLE = "Production Commitment and Capital Engagement Agreement"
STAGE_TWO_TITLE = "Program Activation and Production Agreement"
STAGE_ONE_DOCUMENT_KEY = "production_commitment_v1"
STAGE_TWO_DOCUMENT_KEY = "program_activation_v1"  # reserved: stage two is deferred
DOCUMENT_VERSION = "2026-09-03-1"

PRODUCT_KEYS: tuple[str, ...] = ("vsc", "gap", "theft", "appearance", "key", "tire", "maint", "power")
PRODUCT_LABELS: dict[str, str] = {
    "vsc": "Vehicle service contracts",
    "gap": "GAP products",
    "theft": "Anti-theft products",
    "appearance": "Appearance protection",
    "key": "Key replacement",
    "tire": "Tire and wheel",
    "maint": "Maintenance products",
    "power": "Powertrain products",
}
PRIMARY_PRODUCT = "vsc"
PRODUCT_FIELDS: tuple[str, ...] = (
    "on", "cur_rate", "cur_premium", "rate", "premium", "repay", "comm", "admin", "retention", "term",
)

# Addendum A.3 guideline: 85% monthly floor, 90% rolling three-month, 125%
# remittance coverage, 100% routing, fifth business day reporting.
A3_GUIDELINE: dict[str, Any] = {
    "monthly_floor_pct": 85,
    "rolling_three_month_pct": 90,
    "remittance_pct_of_debt_service": 125,
    "routing_pct": 100,
    "reporting_deadline": "Fifth business day",
}
THRESHOLD_KEYS: tuple[str, ...] = (
    "units", "vsc_count", "vsc_pen", "vsc_pen3", "vsc_gross", "total_gross", "debt_service", "remittance",
)
THRESHOLD_LABELS: dict[str, str] = {
    "units": "Minimum monthly retail units",
    "vsc_count": "Minimum monthly VSC count",
    "vsc_pen": "Minimum single-month VSC penetration",
    "vsc_pen3": "Minimum rolling 3-month VSC penetration",
    "vsc_gross": "Minimum monthly VSC gross",
    "total_gross": "Minimum total monthly Covered Product gross",
    "debt_service": "Monthly Funding Facility debt service",
    "remittance": "Fixed minimum Eligible Net Remittance",
}
SPREAD_FLOOR_POINTS = 3.0

STEPS: tuple[tuple[str, str, str], ...] = (
    ("parties", "Parties to the agreement", "Dealer, sponsor and relationship manager, exactly as they print on both agreements."),
    ("lot", "The lot and the verified baseline", "What the dealer has on the ground today, and the trailing production the thresholds are derived from."),
    ("products", "Covered products and attachment rates", "Which products carry a commitment, how often they attach, and what each contract is worth."),
    ("advance", "Advance and programme cost", "What the dealer is asking for, what the programme actually costs to run, and whether the deal clears."),
    ("buildout", "Policy buildout", "Whether the policies carry the loan payment, and what the dealer is left paying out of pocket."),
    ("thresholds", "Operative thresholds", "The exact figures that become enforceable at activation."),
    ("shortfall", "Shortfall billing and cure", "What happens in a month when production comes in light."),
    ("projection", "Repayment and earnout timeline", "How repayment, commissions and reserves build over the life of the deal."),
    ("preview", "Contract preview", "What prints on the agreement as it stands right now."),
    ("send", "Send and signatures", "Both stages, who has signed, and what is blocking the next one."),
)
STEP_LABELS: dict[str, str] = {
    "parties": "Parties", "lot": "Lot and baseline", "products": "Products and attachment",
    "advance": "Advance and programme cost", "buildout": "Policy buildout", "thresholds": "Operative thresholds",
    "shortfall": "Shortfall and cure", "projection": "Projection", "preview": "Contract preview",
    "send": "Send and signatures",
}

FieldKind = Literal["text", "number", "date", "email", "phone", "select", "multiselect", "textarea"]
RequiredFor = Literal["presentation", "stage_one", "stage_two", "never"]


@dataclass(frozen=True)
class FieldRule:
    key: str
    step: str
    label: str
    kind: FieldKind = "text"
    required_for: RequiredFor = "never"
    non_zero: bool = False
    title: str = ""
    detail: str = ""
    hint: str = ""
    always: str = ""
    options: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return self.required_for != "never"


ENTITY_TYPES: tuple[str, ...] = (
    "Limited liability company", "Corporation", "S corporation", "Limited partnership",
    "Limited liability partnership", "Sole proprietorship", "Trust", "Other",
)
FACILITY_TYPES: tuple[str, ...] = (
    "Dealer capital advance", "Commission advance", "Revolving commission line", "Term advance", "Hybrid",
)
EVIDENCE_OPTIONS: tuple[str, ...] = (
    "DMS unit reports", "F&I production reports", "Sponsor production reports", "Sponsor remittance statements",
    "Bank statements (Plaid)", "Bank statements (uploaded)", "Tax returns", "Dealer attestation",
)
CADENCES: tuple[str, ...] = ("month", "quarter", "balance")
ADJUSTMENTS: tuple[str, ...] = ("none", "bps", "rate")
SIZING_MODES: tuple[str, ...] = ("backsolve", "fixed")
BUILDOUT_MODES: tuple[str, ...] = ("reverse", "forward")
FUNDING_PARTIES: tuple[str, ...] = ("Sponsor", "Qualified Commercial LLC", "Lender")
TERM_OPTIONS: tuple[int, ...] = (12, 18, 24, 36)

# Ported from the design's textField / numField opts. `required_for` "stage_one"
# is the design's `required: true`; "stage_two" is its `required: s.s1.sponsor
# !== ""` (the activation certificate). "presentation" fields gate the client
# PDF before the agreement stage.
FIELD_RULES: tuple[FieldRule, ...] = (
    # ---- parties ----
    FieldRule("dealer_name", "parties", "Dealer full legal name", required_for="presentation",
              title="Dealer legal name is blank", detail="Every schedule prints the dealer's full legal name."),
    FieldRule("dealer_state", "parties", "Dealer state of formation", kind="select", required_for="stage_one",
              title="Dealer state of formation is blank", detail="Needed on the parties block of both agreements."),
    FieldRule("dealer_entity", "parties", "Dealer entity type", kind="select", required_for="stage_one",
              title="Dealer entity type is blank", detail="Needed on the parties block of both agreements.",
              options=ENTITY_TYPES),
    FieldRule("dealer_dba", "parties", "Dealer DBA"),
    FieldRule("dealer_address", "parties", "Dealer address", required_for="stage_one",
              title="Dealer address is blank", detail="Formal notice cannot be served without it."),
    FieldRule("dealer_signer_name", "parties", "Dealer authorized signer", required_for="stage_one",
              title="Dealer authorized signer is blank",
              detail="The signer's full legal name is what the client must type to sign.",
              always="The person who signs for the dealer"),
    FieldRule("dealer_signer_title", "parties", "Dealer signer title", required_for="stage_one",
              title="Dealer signer title is blank", detail="Prints under the dealer's signature block."),
    FieldRule("sponsor_name", "parties", "Sponsor full legal name", kind="select", required_for="presentation",
              title="Sponsor legal name is blank",
              detail="The sponsor is the entity that administers the products, and it must hold a signed "
                     "Strategic Referral, Capital Advisory and Business Relationship Protection Agreement."),
    FieldRule("sponsor_state", "parties", "Sponsor state of formation", kind="select", required_for="stage_one",
              title="Sponsor state of formation is blank", detail="Needed on the parties block."),
    FieldRule("sponsor_entity", "parties", "Sponsor entity type", kind="select", required_for="stage_one",
              title="Sponsor entity type is blank", detail="Needed on the parties block.", options=ENTITY_TYPES),
    FieldRule("sponsor_address", "parties", "Sponsor principal address"),
    FieldRule("sponsor_platform", "parties", "Sponsor platform", required_for="stage_one",
              title="Sponsor platform is blank", detail="Schedule A names the platform the products are administered on.",
              always="The administration platform named on Schedule A"),
    FieldRule("sponsor_email", "parties", "Sponsor notice email", kind="email", required_for="stage_one",
              title="Sponsor notice email is blank", detail="Notice under both agreements is served by confirmed email."),
    FieldRule("rm_name", "parties", "Relationship manager", required_for="presentation",
              title="Relationship manager is blank", detail="Schedule 2 names the manager and their compensation category."),
    FieldRule("rm_employer", "parties", "Relationship manager employer", required_for="stage_one",
              title="Relationship manager employer is blank", detail="Schedule 2 names the manager's employer."),
    FieldRule("rm_email", "parties", "Relationship manager email", kind="email", required_for="stage_one",
              title="Relationship manager email is blank", detail="Schedule 2 needs a notice address for the manager."),
    FieldRule("rm_phone", "parties", "Relationship manager phone", kind="phone", required_for="stage_one",
              title="Relationship manager phone is blank", detail="Schedule 2 needs a phone number for the manager."),
    # ---- lot and baseline ----
    FieldRule("lot_units", "lot", "Vehicles in the lot", kind="number", required_for="presentation", non_zero=True,
              title="Vehicles in the lot is blank", detail="The lot count anchors the whole baseline.",
              always="Counted on the onsite review"),
    FieldRule("avg_cost", "lot", "Average cost of car", kind="number", required_for="presentation", non_zero=True,
              title="Average cost of car is blank", detail="Needed to value the lot and sanity-check the advance.",
              always="Average acquisition cost, not retail price"),
    FieldRule("monthly_units", "lot", "Average monthly retail units", kind="number", required_for="presentation",
              non_zero=True, title="Average monthly retail units is blank",
              detail="Every attachment rate is a percentage of this number.", always="Trailing twelve-month average"),
    FieldRule("cancels", "lot", "Cancellations per month", kind="number"),
    FieldRule("chargebacks", "lot", "Chargebacks per month", kind="number"),
    FieldRule("base_from", "lot", "Baseline from", kind="date", required_for="presentation",
              title="Baseline period is blank", detail="Addendum A.1 requires a baseline from and through date."),
    FieldRule("base_through", "lot", "Baseline through", kind="date", required_for="presentation",
              title="Baseline period end is blank", detail="Addendum A.1 requires a baseline from and through date."),
    FieldRule("evidence", "lot", "Evidence relied upon", kind="multiselect", required_for="presentation",
              title="Evidence relied upon is blank",
              detail="The baseline must be supported by DMS records, sponsor reports, bank records or product reports.",
              always="DMS records, sponsor reports, bank records, product reports", options=EVIDENCE_OPTIONS),
    FieldRule("seasonality", "lot", "Seasonality", kind="textarea"),
    # ---- advance and programme cost ----
    FieldRule("requested", "advance", "Requested amount", kind="number", required_for="presentation", non_zero=True,
              title="Requested amount is blank", detail="Schedule A prints the requested facility amount.",
              always="What the dealer is asking for"),
    FieldRule("min_activation", "advance", "Minimum activation amount", kind="number", required_for="presentation",
              non_zero=True, title="Minimum activation amount is blank",
              detail="No partial advance below this amount activates the agreement.",
              always="No partial advance below this activates"),
    FieldRule("facility_type", "advance", "Requested facility type", kind="select", required_for="presentation",
              title="Requested facility type is blank", detail="Schedule A prints the requested facility type.",
              options=FACILITY_TYPES),
    FieldRule("term", "advance", "Term (months)", kind="number", required_for="presentation", non_zero=True,
              title="Term is blank", detail="The repayment term drives the whole projection."),
    FieldRule("dealer_cof", "advance", "Dealer cost of funds (%)", kind="number", required_for="presentation",
              non_zero=True, title="Dealer cost of funds is blank",
              detail="Priced on the dealer's credit profile and negotiated directly.",
              always="Negotiated with the dealer on their credit profile"),
    FieldRule("exclusivity", "advance", "Exclusivity window (days)", kind="number", required_for="stage_one",
              non_zero=True, title="Exclusivity window is blank",
              detail="Schedule A prints the exclusivity window in days from written approval."),
    FieldRule("bank_cof", "advance", "Bank cost of funds (%)", kind="number",
              always="Near zero — we lend against a bank line"),
    FieldRule("orig_cost", "advance", "Origination and underwriting", kind="number", required_for="presentation",
              non_zero=True, title="Origination and underwriting cost is blank",
              detail="Without the real cost of writing the deal there is no spread to test it against.",
              always="One-time, carried against the whole term"),
    FieldRule("prof_fees", "advance", "Consulting and professional fees", kind="number", required_for="presentation",
              non_zero=True, title="Consulting and professional fees are blank",
              detail="This is the largest cost line on the programme and it cannot be left out of the spread.",
              always="Legal, advisory, onsite review"),
    FieldRule("mgmt_fee", "advance", "Programme management (monthly)", kind="number"),
    FieldRule("loss_prov", "advance", "Loss provision (%)", kind="number"),
    FieldRule("fund_target", "buildout", "Share of payment policies should fund (%)", kind="number"),
    FieldRule("debt_service", "advance", "Monthly facility debt service", kind="number", required_for="presentation",
              non_zero=True, title="Monthly debt service is not set",
              detail="The minimum remittance covenant is 125% of debt service — it cannot be derived until this is filled.",
              always="Sets the 125% remittance covenant"),
    FieldRule("markup", "advance", "Sponsor markup (%)", kind="number", required_for="presentation", non_zero=True,
              title="Sponsor markup is blank", detail="The sponsor's markup on premium is what this arrangement earns them.",
              always="The sponsor's margin on every contract sold"),
    FieldRule("sizing", "advance", "Advance sizing", kind="select", options=SIZING_MODES),
    FieldRule("buildout_mode", "buildout", "Buildout mode", kind="select", options=BUILDOUT_MODES),
    # ---- shortfall and cure ----
    FieldRule("cadence", "shortfall", "Shortfall billing cadence", kind="select", options=CADENCES),
    FieldRule("cure_days", "shortfall", "Shortage cure period (business days)", kind="number",
              required_for="stage_one", non_zero=True, title="Shortage cure period is blank",
              detail="Addendum A.6 needs a number of business days.", always="Business days after notice"),
    FieldRule("corrective", "shortfall", "Corrective period", required_for="stage_one",
              title="Corrective period is blank", detail="Addendum A.6 names the period a shortage must be corrected in."),
    FieldRule("adj", "shortfall", "Program rate adjustment", kind="select", options=ADJUSTMENTS),
    FieldRule("adj_value", "shortfall", "Adjustment value", kind="number"),
    FieldRule("exclusions", "shortfall", "Approved exclusions", kind="textarea"),
    # ---- stage two: funding activation certificate (deferred; rules kept) ----
    FieldRule("funding_party", "send", "Funding party", kind="select", required_for="stage_two",
              title="Funding party is blank",
              detail="The activation certificate has to name the entity that advanced the capital.",
              options=FUNDING_PARTIES),
    FieldRule("funding_date", "send", "Actual funding date", kind="date", required_for="stage_two",
              title="Actual funding date is blank",
              detail="Stage two cannot be executed until actual funding has occurred."),
    FieldRule("funded_amount", "send", "Funded amount", kind="number", required_for="stage_two", non_zero=True,
              title="Funded amount is blank", detail="The certificate records the amount actually disbursed and cleared."),
    FieldRule("commencement", "send", "Production commencement date", kind="date", required_for="stage_two",
              title="Production commencement date is blank", detail="Addendum A names the commencement date."),
    FieldRule("activation_date", "send", "Activation date", kind="date", required_for="stage_two",
              title="Activation date is blank", detail="May not be earlier than actual funding.",
              always="May not be earlier than actual funding"),
    FieldRule("maturity", "send", "Original maturity date", kind="date", required_for="stage_two",
              title="Original maturity date is blank", detail="The activation certificate records the maturity."),
)
FIELD_RULES_BY_KEY: dict[str, FieldRule] = {r.key: r for r in FIELD_RULES}
FIELD_KEYS: frozenset[str] = frozenset(FIELD_RULES_BY_KEY)
NUMBER_KEYS: frozenset[str] = frozenset(r.key for r in FIELD_RULES if r.kind == "number")
SPONSOR_KEYS: frozenset[str] = frozenset(
    {"sponsor_name", "sponsor_state", "sponsor_entity", "sponsor_address", "sponsor_platform", "sponsor_email"}
)
STAGE_ONE_SCOPES: frozenset[str] = frozenset({"presentation", "stage_one"})

DEFAULTS: dict[str, Any] = {
    "rm_employer": "Qualified Commercial LLC",
    "facility_type": "Dealer capital advance",
    "corrective": "Next complete reporting month",
    "sizing": "backsolve",
    "buildout_mode": "reverse",
    "cadence": "quarter",
    "adj": "bps",
    "adj_value": 200,
    "bank_cof": 0.5,
    "loss_prov": 1.5,
    "fund_target": 100,
    "term": 36,
    "exclusivity": 45,
    "cure_days": 5,
}
DEFAULT_PRODUCT: dict[str, Any] = {
    "on": False, "cur_rate": "", "cur_premium": "", "rate": "", "premium": "", "repay": "",
    "comm": "", "admin": "", "retention": "", "term": 36,
}


def empty_arrangement() -> dict[str, Any]:
    """A blank arrangement with every key present, defaults applied."""
    out: dict[str, Any] = {r.key: (list() if r.kind == "multiselect" else "") for r in FIELD_RULES}
    out.update(DEFAULTS)
    out["products"] = {k: {**DEFAULT_PRODUCT, "on": k == PRIMARY_PRODUCT} for k in PRODUCT_KEYS}
    out["thresholds"] = {k: "" for k in THRESHOLD_KEYS}
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _num(value: Any) -> float:
    """`Number(v) || 0` from the design: blanks and junk count as zero."""
    if value is None or value == "" or isinstance(value, bool):
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(out) or math.isinf(out):
        return 0.0
    return out


def jsround(value: float) -> int:
    """JavaScript `Math.round`: halves go toward +infinity, unlike Python's banker's rounding."""
    return int(math.floor(value + 0.5))


def _blank_text(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return not any(str(v).strip() for v in value)
    return value is None or not str(value).strip()


def _blank_num(value: Any, *, non_zero: bool) -> bool:
    if value is None or value == "":
        return True
    try:
        n = float(value)
    except (TypeError, ValueError):
        return True
    return bool(non_zero and n == 0)


def is_blank(rule: FieldRule, value: Any) -> bool:
    if rule.kind == "number":
        return _blank_num(value, non_zero=rule.non_zero)
    return _blank_text(value)


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    return "$" + f"{jsround(n):,}"


def _pct(n: float) -> str:
    return f"{round(n * 10) / 10:g}%"


def _thr_val(overrides: dict[str, Any] | None, key: str, fallback: Any) -> Any:
    if overrides and key in overrides and overrides[key] not in ("", None):
        return overrides[key]
    return fallback


# ---------------------------------------------------------------------------
# per-product economics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductEcon:
    key: str
    label: str
    on: bool
    cur_rate: float
    cur_premium: float
    rate: float
    premium: float
    repay: float
    comm: float          # $ per contract
    comm_pct: float
    admin: float
    retention_pct: float
    reserve: float       # $ per contract
    term: int
    contracts: int
    cur_contracts: int
    gross: float
    cur_gross: float
    repay_m: float
    comm_m: float
    admin_m: float
    reserve_m: float
    uplift: float
    d_contracts: int
    d_gross: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "on": self.on,
            "cur_rate": self.cur_rate, "cur_premium": self.cur_premium, "rate": self.rate, "premium": self.premium,
            "repay": self.repay, "comm": self.comm, "comm_pct": self.comm_pct, "admin": self.admin,
            "retention_pct": self.retention_pct, "reserve": self.reserve, "term": self.term,
            "contracts": self.contracts, "cur_contracts": self.cur_contracts,
            "gross": self.gross, "cur_gross": self.cur_gross,
            "repay_m": self.repay_m, "comm_m": self.comm_m, "admin_m": self.admin_m, "reserve_m": self.reserve_m,
            "uplift": self.uplift, "d_contracts": self.d_contracts, "d_gross": self.d_gross,
        }


@dataclass(frozen=True)
class PortfolioEcon:
    units: int
    rows: tuple[ProductEcon, ...]
    on: tuple[ProductEcon, ...]
    contracts: int
    gross: float
    cur_contracts: int
    cur_gross: float
    d_gross: float
    repay_m: float
    comm_m: float
    admin_m: float
    reserve_m: float
    max_term: int

    @property
    def d_contracts(self) -> int:
        return self.contracts - self.cur_contracts

    def row(self, key: str) -> ProductEcon:
        for r in self.rows:
            if r.key == key:
                return r
        raise KeyError(key)


def product_econ(units: float, key: str, values: dict[str, Any] | None) -> ProductEcon:
    """Per-product monthly economics. Reserve is what remains of premium after
    repayment, commission and the admin fee, reduced for expected claims."""
    v = values or {}
    on = bool(v.get("on"))
    cur_rate = _num(v.get("cur_rate"))
    cur_premium = _num(v.get("cur_premium"))
    rate = _num(v.get("rate"))
    premium = _num(v.get("premium"))
    repay = _num(v.get("repay"))
    comm_pct = _num(v.get("comm"))
    admin = _num(v.get("admin"))
    retention = _num(v.get("retention"))
    term = int(_num(v.get("term"))) or 12
    cur_contracts = jsround(units * cur_rate / 100) if on else 0
    contracts = jsround(units * rate / 100) if on else 0
    comm = premium * (comm_pct / 100)
    residual = max(0.0, premium - repay - comm - admin)
    reserve = residual * (retention / 100)
    gross = contracts * premium
    cur_gross = cur_contracts * cur_premium
    return ProductEcon(
        key=key, label=PRODUCT_LABELS.get(key, key), on=on,
        cur_rate=cur_rate, cur_premium=cur_premium, rate=rate, premium=premium,
        repay=repay, comm=comm, comm_pct=comm_pct, admin=admin, retention_pct=retention, reserve=reserve,
        term=term, contracts=contracts, cur_contracts=cur_contracts,
        gross=gross, cur_gross=cur_gross,
        repay_m=contracts * repay, comm_m=contracts * comm, admin_m=contracts * admin, reserve_m=contracts * reserve,
        uplift=premium - cur_premium, d_contracts=contracts - cur_contracts, d_gross=gross - cur_gross,
    )


def portfolio_econ(units: float, products: dict[str, Any] | None) -> PortfolioEcon:
    products = products or {}
    rows = tuple(product_econ(units, k, products.get(k)) for k in PRODUCT_KEYS)
    on = tuple(r for r in rows if r.on)

    def total(attr: str) -> float:
        return sum(getattr(r, attr) for r in on)

    return PortfolioEcon(
        units=int(units), rows=rows, on=on,
        contracts=int(total("contracts")), gross=total("gross"),
        cur_contracts=int(total("cur_contracts")), cur_gross=total("cur_gross"), d_gross=total("d_gross"),
        repay_m=total("repay_m"), comm_m=total("comm_m"), admin_m=total("admin_m"), reserve_m=total("reserve_m"),
        max_term=max((r.term for r in on), default=12),
    )


# ---------------------------------------------------------------------------
# advance sizing and spread
# ---------------------------------------------------------------------------

def pv_annuity(payment: float, annual_rate_pct: float, n_months: int) -> float:
    """Present value of a level monthly stream at the dealer's rate; at zero rate it is the plain sum."""
    r = annual_rate_pct / 100 / 12
    if r <= 0:
        return payment * n_months
    return payment * ((1 - (1 + r) ** (-n_months)) / r)


def irr_annual_pct(payment: float, n_months: int, present_value: float) -> float:
    """Rate the repayment stream actually returns on the advance: bisection on the
    monthly rate where the stream's present value equals the advance. Returns 0
    when the stream cannot repay the advance at any positive rate."""
    if payment <= 0 or present_value <= 0:
        return 0.0
    if payment * n_months <= present_value:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        val = payment * n_months if mid == 0 else payment * ((1 - (1 + mid) ** (-n_months)) / mid)
        if val > present_value:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2) * 12 * 100


@dataclass(frozen=True)
class AdvanceEcon:
    term: int
    requested: float
    supported: float
    advance: float
    sizing: str
    implied_rate: float
    bank_cost: float
    orig_cost: float
    prof_fees: float
    mgmt_total: float
    loss_cost: float
    total_cost: float
    cost_rate: float
    spread: float
    clears: bool
    total_repay: float

    def cost_lines(self, mgmt_fee: float, loss_prov: float, bank_cof: float) -> list[dict[str, Any]]:
        lines = [
            ("prof_fees", "Consulting and professional fees", self.prof_fees, "One-time, at origination"),
            ("orig_cost", "Origination and underwriting", self.orig_cost, "One-time, at origination"),
            ("mgmt", "Programme management", self.mgmt_total, f"{_money(mgmt_fee)} a month for {self.term} months"),
            ("loss", "Loss provision", self.loss_cost, f"{_pct(loss_prov)} of the advance"),
            ("bank", "Bank cost of funds", self.bank_cost, f"{_pct(bank_cof)} a year on the line"),
        ]
        return [
            {"key": k, "label": label, "amount": amount, "when": when,
             "share_pct": (amount / self.total_cost * 100) if self.total_cost > 0 else None}
            for k, label, amount, when in lines
        ]


def advance_econ(arr: dict[str, Any], e: PortfolioEcon) -> AdvanceEcon:
    term = int(_num(arr.get("term"))) or 1
    requested = _num(arr.get("requested"))
    dealer_cof = _num(arr.get("dealer_cof"))
    total_repay = e.repay_m * term
    supported = pv_annuity(e.repay_m, dealer_cof, term)
    sizing = arr.get("sizing") if arr.get("sizing") in SIZING_MODES else "backsolve"
    advance = supported if sizing == "backsolve" else requested
    implied = dealer_cof if sizing == "backsolve" else irr_annual_pct(e.repay_m, term, requested)
    # Bank capital is close to free; the programme's real cost is the one-time
    # consulting, underwriting and professional work, plus running management.
    bank_cost = advance * (_num(arr.get("bank_cof")) / 100) * (term / 12)
    orig_cost = _num(arr.get("orig_cost"))
    prof_fees = _num(arr.get("prof_fees"))
    mgmt_total = _num(arr.get("mgmt_fee")) * term
    loss_cost = advance * (_num(arr.get("loss_prov")) / 100)
    total_cost = bank_cost + orig_cost + prof_fees + mgmt_total + loss_cost
    cost_rate = (total_cost / advance) * (12 / term) * 100 if advance > 0 else 0.0
    spread = implied - cost_rate
    return AdvanceEcon(
        term=term, requested=requested, supported=supported, advance=advance, sizing=sizing,
        implied_rate=implied, bank_cost=bank_cost, orig_cost=orig_cost, prof_fees=prof_fees,
        mgmt_total=mgmt_total, loss_cost=loss_cost, total_cost=total_cost, cost_rate=cost_rate,
        spread=spread, clears=spread >= SPREAD_FLOOR_POINTS, total_repay=total_repay,
    )


# ---------------------------------------------------------------------------
# thresholds (Addendum A.2 / A.3)
# ---------------------------------------------------------------------------

def derived_thresholds(arr: dict[str, Any], e: PortfolioEcon) -> dict[str, dict[str, Any]]:
    vsc = e.row(PRIMARY_PRODUCT)
    ds = _num(arr.get("debt_service"))
    floor = A3_GUIDELINE["monthly_floor_pct"] / 100
    rolling = A3_GUIDELINE["rolling_three_month_pct"] / 100
    remit = A3_GUIDELINE["remittance_pct_of_debt_service"] / 100
    return {
        "units": {"base": e.units, "req": jsround(e.units * floor)},
        "vsc_count": {"base": vsc.contracts, "req": jsround(vsc.contracts * floor)},
        "vsc_pen": {"base": vsc.rate, "req": jsround(vsc.rate * floor)},
        "vsc_gross": {"base": vsc.gross, "req": jsround(vsc.gross * floor)},
        "total_gross": {"base": e.gross, "req": jsround(e.gross * floor)},
        "debt_service": {"base": None, "req": ds},
        "remittance": {"base": None, "req": jsround(ds * remit)},
        "vsc_pen3": {"base": None, "req": jsround(vsc.rate * rolling)},
    }


def threshold_rows(arr: dict[str, Any], e: PortfolioEcon) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Editable covenant rows (with the operative value resolved from overrides
    or the A.3 guideline) and the fixed rows. Returns (rows, attention)."""
    dt = derived_thresholds(arr, e)
    overrides = arr.get("thresholds") or {}
    specs = [
        ("units", dt["units"]["base"], dt["units"]["req"], "count"),
        ("vsc_count", dt["vsc_count"]["base"], dt["vsc_count"]["req"], "count"),
        ("vsc_pen", dt["vsc_pen"]["base"], dt["vsc_pen"]["req"], "pct"),
        ("vsc_pen3", dt["vsc_pen"]["base"], dt["vsc_pen3"]["req"], "pct"),
        ("vsc_gross", dt["vsc_gross"]["base"], dt["vsc_gross"]["req"], "money"),
        ("total_gross", dt["total_gross"]["base"], dt["total_gross"]["req"], "money"),
        ("debt_service", None, dt["debt_service"]["req"], "money"),
        ("remittance", None, dt["remittance"]["req"], "money"),
    ]
    rows: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for key, base, fallback, fmt in specs:
        raw = _thr_val(overrides, key, fallback)
        blank = raw in ("", None) or _num(raw) == 0
        # debt service and the remittance floor derive from the advance step; flagging
        # them here as well would report the same missing number twice.
        derives_from_advance = key in ("debt_service", "remittance")
        if blank and not derives_from_advance:
            attention.append({
                "step": "thresholds", "key": f"thresholds.{key}",
                "title": f"{THRESHOLD_LABELS[key]} is blank",
                "detail": "Addendum A.2: a blank field is not enforceable.",
            })
        rows.append({
            "key": key, "label": THRESHOLD_LABELS[key], "format": fmt,
            "baseline": base, "guideline": fallback, "operative": _num(raw) if not blank else None,
            "overridden": key in overrides and overrides.get(key) not in ("", None),
            "blank": blank, "editable": True,
        })
    fixed = [
        {"key": "coverage", "label": "Minimum remittance coverage", "value": "125%"},
        {"key": "routing", "label": "Covered Product routing compliance", "value": "100%"},
        {"key": "reporting", "label": "Monthly reporting deadline", "value": A3_GUIDELINE["reporting_deadline"]},
        {"key": "commencement", "label": "Production Commencement Date",
         "value": str(arr.get("commencement") or "").strip() or "Set at closing"},
    ]
    return rows + fixed, attention


def rolling_three_month(rows: list[dict[str, Any]], remittance_req: float) -> list[dict[str, Any]]:
    by_key = {r["key"]: r for r in rows if r.get("editable")}

    def op(key: str) -> float:
        return _num(by_key.get(key, {}).get("operative"))

    return [
        {"label": "Retail units", "value": op("units") * 3, "format": "count"},
        {"label": "VSC contracts", "value": op("vsc_count") * 3, "format": "count"},
        {"label": "VSC gross production", "value": op("vsc_gross") * 3, "format": "money"},
        {"label": "Total Covered Product gross", "value": op("total_gross") * 3, "format": "money"},
        {"label": "Aggregate Eligible Net Remittance", "value": remittance_req * 3, "format": "money"},
        {"label": "VSC penetration", "value": op("vsc_pen3"), "format": "pct"},
    ]


# ---------------------------------------------------------------------------
# policy buildout
# ---------------------------------------------------------------------------

def reverse_solve(e: PortfolioEcon, need_monthly: float) -> list[dict[str, Any]]:
    """Spread the required monthly repayment across covered products in
    proportion to what each one already earns. Rounding each per-contract
    figure leaves the monthly total a few dollars short, which would keep the
    arrangement from ever reading as fully funded, so the remainder goes on
    the product carrying the most contracts."""
    basis = sum(r.contracts * r.premium for r in e.on)
    rows: list[dict[str, Any]] = []
    for r in e.on:
        share = (r.contracts * r.premium) / basis if basis > 0 else 0.0
        per_contract = (need_monthly * share) / r.contracts if r.contracts > 0 else 0.0
        rows.append({
            "key": r.key, "label": r.label, "contracts": r.contracts, "cur_premium": r.cur_premium,
            "solve_repay": jsround(per_contract), "needed": jsround(r.cur_premium + per_contract),
        })
    if rows:
        rounded = sum(r["solve_repay"] * r["contracts"] for r in rows)
        gap = need_monthly - rounded
        if gap > 0:
            biggest = max(rows, key=lambda r: r["contracts"])
            if biggest["contracts"] > 0:
                add = math.ceil(gap / biggest["contracts"])
                biggest["solve_repay"] += add
                biggest["needed"] += add
    for r in rows:
        r["uplift"] = r["needed"] - r["cur_premium"]
        r["steep"] = r["uplift"] > r["cur_premium"] * 0.25
    return rows


def buildout(arr: dict[str, Any], e: PortfolioEcon, adv: AdvanceEcon) -> dict[str, Any]:
    ds = _num(arr.get("debt_service"))
    target = _num(arr.get("fund_target"))
    policy_funded = e.repay_m
    funded_pct = (policy_funded / ds) * 100 if ds > 0 else 0.0
    out_of_pocket = max(0.0, ds - policy_funded)
    loan_free = ds > 0 and policy_funded >= ds
    need_monthly = ds * (target / 100)
    solve_rows = reverse_solve(e, need_monthly)
    required_per_contract = need_monthly / e.contracts if e.contracts > 0 else 0.0
    avg_cur_premium = (
        sum(r.cur_contracts * r.cur_premium for r in e.on) / max(1, e.cur_contracts) if e.contracts > 0 else 0.0
    )
    required_uplift_pct = (required_per_contract / avg_cur_premium) * 100 if avg_cur_premium > 0 else 0.0

    def scenario(with_build: bool) -> dict[str, Any]:
        funded = min(policy_funded, ds) if with_build else 0.0
        ops = max(0.0, ds - funded)
        free = with_build and ds > 0 and funded >= ds
        return {
            "key": "with" if with_build else "without",
            "title": "With policy buildout" if with_build else "Without policy buildout",
            "sub": ("The repayment is built into every contract the dealer already sells." if with_build
                    else "The dealer services the loan out of operating cash, the way any other note works."),
            "tag": "No cost to the dealer" if free else ("Partly funded" if with_build else "Full payment"),
            "free": free, "payment": ds, "funded": funded, "from_operations": ops,
            "total_from_operations": ops * adv.term,
            "funded_pct": (funded / ds * 100) if ds > 0 else 0.0,
            "gross": e.gross if with_build else e.cur_gross,
        }

    return {
        "debt_service": ds, "fund_target_pct": target, "policy_funded": policy_funded,
        "funded_pct": funded_pct, "out_of_pocket": out_of_pocket, "loan_free": loan_free,
        "need_monthly": need_monthly, "solve_rows": solve_rows,
        "required_per_contract": required_per_contract, "required_uplift_pct": required_uplift_pct,
        "scenarios": {"with": scenario(True), "without": scenario(False)},
    }


# ---------------------------------------------------------------------------
# projection: ramp, plateau, roll-off
# ---------------------------------------------------------------------------

def projection(e: PortfolioEcon, adv: AdvanceEcon) -> dict[str, Any]:
    term = adv.term
    span = min(term + e.max_term, 48)
    bars: list[dict[str, Any]] = []
    for m in range(1, span + 1):
        originating = m <= term
        repay = e.repay_m if originating else 0.0
        comm = e.comm_m if originating else 0.0
        # Each cohort earns its reserve out over its own product term.
        reserve = 0.0
        for row in e.on:
            cohorts = min(m, row.term, term)
            active = cohorts if m <= term else max(0, row.term - (m - term))
            reserve += (row.reserve_m / row.term) * active if row.term else 0.0
        bars.append({"m": m, "repay": repay, "comm": comm, "reserve": reserve, "total": repay + comm + reserve})
    peak = max([1.0] + [b["total"] for b in bars])
    steady = next((b["m"] for b in bars if b["reserve"] >= e.reserve_m * 0.98), 0)
    total_reserve = sum(b["reserve"] for b in bars)
    retire_month = min(term, math.ceil(adv.advance / e.repay_m)) if e.repay_m > 0 else None
    return {
        "span": span, "term": term, "bars": bars, "peak": peak,
        "steady_from_month": steady or None,
        "plateau_monthly": e.repay_m + e.comm_m + e.reserve_m,
        "retire_month": retire_month, "roll_off_months": e.max_term,
        "first_month_total": bars[0]["total"] if bars else 0.0,
        "totals": {"repay": adv.total_repay, "comm": e.comm_m * term, "reserve": total_reserve},
    }


# ---------------------------------------------------------------------------
# required-field rules ("a blank field is not enforceable")
# ---------------------------------------------------------------------------

def field_attention(arr: dict[str, Any], *, scope: str) -> list[dict[str, Any]]:
    """Blank required fields for a scope: presentation | stage_one | stage_two.
    stage_one includes presentation fields; stage_two includes everything."""
    if scope == "presentation":
        wanted = {"presentation"}
    elif scope == "stage_one":
        wanted = set(STAGE_ONE_SCOPES)
    else:
        wanted = set(STAGE_ONE_SCOPES) | {"stage_two"}
    out: list[dict[str, Any]] = []
    for rule in FIELD_RULES:
        if rule.required_for not in wanted:
            continue
        if is_blank(rule, arr.get(rule.key)):
            out.append({
                "step": rule.step, "key": rule.key,
                "title": rule.title or f"{rule.label} is blank",
                "detail": rule.detail or "Required — a blank field is not enforceable",
            })
    return out


def econ_attention(arr: dict[str, Any], e: PortfolioEcon, adv: AdvanceEcon, remittance_req: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not adv.clears:
        out.append({
            "step": "advance", "key": "spread",
            "title": ("The programme costs more than it returns" if adv.spread < 0
                      else "Spread is under the 3 point floor"),
            "detail": (f"Projected return is {_pct(adv.implied_rate)} against an all-in programme cost of "
                       f"{_pct(adv.cost_rate)}. Underwriting will not clear it."),
        })
    vsc = e.row(PRIMARY_PRODUCT)
    if not vsc.on:
        out.append({
            "step": "products", "key": f"products.{PRIMARY_PRODUCT}.on",
            "title": "Vehicle service contracts are not covered",
            "detail": "VSC is the primary repayment product. With it unchecked there is no production commitment on it.",
        })
    for row in e.on:
        if not row.repay:
            out.append({
                "step": "products", "key": f"products.{row.key}.repay",
                "title": f"{row.label} has no repayment amount",
                "detail": "A covered product with no per-contract withholding contributes nothing to repayment.",
            })
    if not e.on:
        out.append({
            "step": "products", "key": "products",
            "title": "No covered products selected",
            "detail": "An unchecked product carries no production commitment, so there is nothing to repay against.",
        })
    if remittance_req > 0 and e.repay_m < remittance_req:
        coverage = (e.repay_m / remittance_req) * 100
        out.append({
            "step": "products", "key": "remittance_coverage",
            "title": "Repayment does not meet the remittance covenant",
            "detail": (f"{_money(e.repay_m)} a month against a {_money(remittance_req)} floor — {_pct(coverage)} of it. "
                       "Every month would open short unless attachment or the withheld amount goes up."),
        })
    return out


# ---------------------------------------------------------------------------
# contract preview rows
# ---------------------------------------------------------------------------

def _pv(label: str, value: Any, *, schedule: str) -> dict[str, Any]:
    blank = value in ("", None, "—") or (isinstance(value, (list, tuple)) and not value)
    shown = "Blank" if blank else (", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value))
    return {"schedule": schedule, "label": label, "value": shown, "blank": blank}


def preview_rows(arr: dict[str, Any], computed: dict[str, Any], *, stage: int = 1) -> list[dict[str, Any]]:
    e = computed["econ"]
    vsc = next(r for r in e["rows"] if r["key"] == PRIMARY_PRODUCT)
    thr = {r["key"]: r for r in computed["thresholds"]["rows"] if r.get("editable")}
    if stage == 1:
        return [
            _pv("Dealer legal name", arr.get("dealer_name"), schedule="A"),
            _pv("Requested facility type", arr.get("facility_type"), schedule="A"),
            _pv("Requested amount", _money(_num(arr.get("requested"))) if _num(arr.get("requested")) else "", schedule="A"),
            _pv("Minimum activation amount", _money(_num(arr.get("min_activation"))) if _num(arr.get("min_activation")) else "", schedule="A"),
            _pv("Exclusivity window (days)", arr.get("exclusivity"), schedule="A"),
            _pv("Sponsor platform", arr.get("sponsor_platform"), schedule="A"),
            _pv("Sponsor legal name", arr.get("sponsor_name"), schedule="A"),
            _pv("Relationship manager", arr.get("rm_name"), schedule="A"),
            _pv("Baseline from", arr.get("base_from"), schedule="E"),
            _pv("Baseline through", arr.get("base_through"), schedule="E"),
            _pv("Average monthly retail units", e["units"], schedule="E"),
            _pv("Average monthly VSC count", vsc["contracts"], schedule="E"),
            _pv("Baseline VSC penetration", _pct(vsc["rate"]), schedule="E"),
            _pv("Average monthly VSC gross", _money(vsc["gross"]), schedule="E"),
            _pv("Average total monthly Covered Product gross", _money(e["gross"]), schedule="E"),
            _pv("Evidence relied upon", arr.get("evidence"), schedule="E"),
        ]
    remittance_req = computed["thresholds"]["remittance_req"]
    adj = arr.get("adj") or "none"
    adj_value = _num(arr.get("adj_value"))
    adj_text = "None" if adj == "none" else (f"{jsround(adj_value)} basis points" if adj == "bps" else f"{_pct(adj_value)} adjusted rate")

    def op(key: str, fmt: str) -> str:
        v = thr.get(key, {}).get("operative")
        if v is None:
            return ""
        return _money(v) if fmt == "money" else (f"{v:g}%" if fmt == "pct" else f"{v:g}")

    return [
        _pv("Dealer legal name", arr.get("dealer_name"), schedule="Addendum A"),
        _pv("Funding party", arr.get("funding_party"), schedule="Certificate"),
        _pv("Actual funding amount", _money(_num(arr.get("funded_amount"))) if _num(arr.get("funded_amount")) else "", schedule="Certificate"),
        _pv("Funding date", arr.get("funding_date"), schedule="Certificate"),
        _pv("Activation date", arr.get("activation_date"), schedule="Certificate"),
        _pv("Original maturity date", arr.get("maturity"), schedule="Certificate"),
        _pv("Production commencement date", arr.get("commencement"), schedule="Addendum A"),
        _pv("Monthly scheduled debt service", _money(_num(arr.get("debt_service"))) if _num(arr.get("debt_service")) else "", schedule="Schedule 1"),
        _pv("Minimum monthly retail units", op("units", "count"), schedule="Schedule 1"),
        _pv("Minimum monthly VSC count", op("vsc_count", "count"), schedule="Schedule 1"),
        _pv("Minimum single-month VSC penetration", op("vsc_pen", "pct"), schedule="Schedule 1"),
        _pv("Minimum rolling 3-month VSC penetration", op("vsc_pen3", "pct"), schedule="Schedule 1"),
        _pv("Minimum monthly VSC gross", op("vsc_gross", "money"), schedule="Schedule 1"),
        _pv("Minimum total monthly Covered Product gross", op("total_gross", "money"), schedule="Schedule 1"),
        _pv("Fixed minimum Eligible Net Remittance", _money(remittance_req) if remittance_req else "", schedule="Schedule 1"),
        _pv("Minimum remittance coverage", "125%", schedule="Schedule 1"),
        _pv("Covered Product routing compliance", "100%", schedule="Schedule 1"),
        _pv("Monthly reporting deadline", A3_GUIDELINE["reporting_deadline"], schedule="Schedule 1"),
        _pv("Remittance shortage cure", f"{jsround(_num(arr.get('cure_days')))} business days" if _num(arr.get("cure_days")) else "", schedule="Addendum A"),
        _pv("Program rate adjustment", adj_text, schedule="Addendum A"),
    ]


# ---------------------------------------------------------------------------
# the whole thing
# ---------------------------------------------------------------------------

def compute(arrangement: dict[str, Any] | None) -> dict[str, Any]:
    """Everything the UI, the PDF and the send gate need, in one JSON-safe dict."""
    arr = {**empty_arrangement(), **(arrangement or {})}
    units = _num(arr.get("monthly_units"))
    e = portfolio_econ(units, arr.get("products"))
    adv = advance_econ(arr, e)
    thr_rows, thr_attention = threshold_rows(arr, e)
    dt = derived_thresholds(arr, e)
    remittance_req = max(_num(_thr_val(arr.get("thresholds"), "remittance", dt["remittance"]["req"])),
                         _num(arr.get("debt_service")) * (A3_GUIDELINE["remittance_pct_of_debt_service"] / 100))
    coverage = (e.repay_m / remittance_req) * 100 if remittance_req > 0 else 0.0
    build = buildout(arr, e, adv)
    proj = projection(e, adv)

    lot_units = _num(arr.get("lot_units"))
    lot = {
        "lot_value": lot_units * _num(arr.get("avg_cost")),
        "months_of_inventory": (lot_units / e.units) if lot_units and e.units else None,
        "sell_through_pct": (e.units / lot_units * 100) if lot_units else None,
    }
    markup_m = e.gross * (_num(arr.get("markup")) / 100)
    mgmt_m = _num(arr.get("mgmt_fee"))
    vsc = e.row(PRIMARY_PRODUCT)

    attention = field_attention(arr, scope="stage_one")
    attention += thr_attention
    attention += econ_attention(arr, e, adv, remittance_req)
    if build["debt_service"] > 0 and build["policy_funded"] < build["debt_service"] * 0.5:
        attention.append({
            "step": "buildout", "key": "buildout",
            "title": "Policies carry less than half the payment",
            "detail": (f"{_money(build['policy_funded'])} against a {_money(build['debt_service'])} payment. "
                       f"The dealer would fund {_money(build['out_of_pocket'])} a month out of operations."),
        })

    computed: dict[str, Any] = {
        "document_version": DOCUMENT_VERSION,
        "econ": {
            "units": e.units, "rows": [r.as_dict() for r in e.rows], "on": [r.key for r in e.on],
            "covered_labels": [r.label for r in e.on],
            "contracts": e.contracts, "gross": e.gross, "cur_contracts": e.cur_contracts, "cur_gross": e.cur_gross,
            "d_contracts": e.d_contracts, "d_gross": e.d_gross, "d_gross_term": e.d_gross * adv.term,
            "repay_m": e.repay_m, "comm_m": e.comm_m, "admin_m": e.admin_m, "reserve_m": e.reserve_m,
            "max_term": e.max_term,
            "blended_attach": (e.contracts / e.units) if e.units else None,
            "cur_per_vehicle": (e.cur_contracts / e.units) if e.units else None,
            "waterfall": [
                {"label": "VSC premium the customer pays", "value": vsc.premium},
                {"label": "Withheld toward repayment", "value": vsc.repay},
                {"label": "Agency commission", "value": vsc.comm},
                {"label": "Administrator fee", "value": vsc.admin},
                {"label": "Reserve after expected claims", "value": vsc.reserve},
            ],
        },
        "lot": lot,
        "advance": {
            "term": adv.term, "requested": adv.requested, "supported": adv.supported, "advance": adv.advance,
            "sizing": adv.sizing, "implied_rate": adv.implied_rate, "cost_rate": adv.cost_rate,
            "spread": adv.spread, "clears": adv.clears, "floor_points": SPREAD_FLOOR_POINTS,
            "bank_cost": adv.bank_cost, "orig_cost": adv.orig_cost, "prof_fees": adv.prof_fees,
            "mgmt_total": adv.mgmt_total, "loss_cost": adv.loss_cost, "total_cost": adv.total_cost,
            "total_repay": adv.total_repay,
            "cost_lines": adv.cost_lines(mgmt_m, _num(arr.get("loss_prov")), _num(arr.get("bank_cof"))),
        },
        "thresholds": {
            "rows": thr_rows, "guideline": A3_GUIDELINE,
            "remittance_req": remittance_req, "coverage_pct": coverage,
            "rolling": rolling_three_month(thr_rows, remittance_req),
        },
        "buildout": build,
        "sponsor": {"markup_pct": _num(arr.get("markup")), "markup_m": markup_m, "mgmt_m": mgmt_m,
                    "total_over_term": (markup_m + mgmt_m) * adv.term},
        "projection": proj,
        "attention": attention,
        "attention_presentation": field_attention(arr, scope="presentation"),
        "attention_stage_two": field_attention(arr, scope="stage_two"),
    }
    computed["preview"] = {"one": preview_rows(arr, computed, stage=1), "two": preview_rows(arr, computed, stage=2)}
    return computed


def attention(arrangement: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The send gate: every reason stage one cannot go out yet."""
    return compute(arrangement)["attention"]


def presentation_attention(arrangement: dict[str, Any] | None) -> list[dict[str, Any]]:
    return field_attention({**empty_arrangement(), **(arrangement or {})}, scope="presentation")


# ---------------------------------------------------------------------------
# normalisation, snapshots and hashing
# ---------------------------------------------------------------------------

def normalize_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Coerce a PATCH body onto the arrangement shape. Unknown keys are dropped;
    numbers are stored as numbers or ""; products/thresholds are merged per key."""
    out: dict[str, Any] = {}
    for key, value in (changes or {}).items():
        if key == "products" and isinstance(value, dict):
            prods: dict[str, Any] = {}
            for pk, pv_ in value.items():
                if pk not in PRODUCT_KEYS or not isinstance(pv_, dict):
                    continue
                row: dict[str, Any] = {}
                for f in PRODUCT_FIELDS:
                    if f not in pv_:
                        continue
                    row[f] = bool(pv_[f]) if f == "on" else _coerce_number(pv_[f])
                prods[pk] = row
            out["products"] = prods
        elif key == "thresholds" and isinstance(value, dict):
            out["thresholds"] = {tk: _coerce_number(tv) for tk, tv in value.items() if tk in THRESHOLD_KEYS}
        elif key in FIELD_KEYS:
            rule = FIELD_RULES_BY_KEY[key]
            if rule.kind == "number":
                out[key] = _coerce_number(value)
            elif rule.kind == "multiselect":
                out[key] = [str(v).strip() for v in (value or []) if str(v).strip()] if isinstance(value, (list, tuple)) else (
                    [s.strip() for s in str(value).split(",") if s.strip()] if value else []
                )
            else:
                out[key] = "" if value is None else str(value).strip()
    return out


def _coerce_number(value: Any) -> float | int | str:
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(n) or math.isinf(n):
        return ""
    return int(n) if n.is_integer() else n


def merge_changes(arrangement: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    base = {**empty_arrangement(), **(arrangement or {})}
    normalized = normalize_changes(changes)
    for key, value in normalized.items():
        if key == "products":
            merged = {k: dict(base["products"].get(k, DEFAULT_PRODUCT)) for k in PRODUCT_KEYS}
            for pk, row in value.items():
                merged[pk].update(row)
            base["products"] = merged
        elif key == "thresholds":
            base["thresholds"] = {**(base.get("thresholds") or {}), **value}
        else:
            base[key] = value
    return base


def jsonable(value: Any) -> Any:
    """Stringify UUIDs, dates and Decimals before a JSONB write."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (uuid.UUID, datetime, date)):
        return value.isoformat() if not isinstance(value, uuid.UUID) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_hash(arrangement: dict[str, Any], *, extra: dict[str, Any] | None = None) -> str:
    payload = {"document_version": DOCUMENT_VERSION, "arrangement": arrangement, "extra": extra or {}}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_snapshot(arrangement: dict[str, Any], computed: dict[str, Any], *,
                       sponsor: dict[str, Any] | None, parties: dict[str, Any] | None) -> dict[str, Any]:
    return jsonable({
        "document_version": DOCUMENT_VERSION,
        "arrangement": arrangement,
        "computed": computed,
        "sponsor": sponsor or {},
        "parties": parties or {},
    })


def money(n: float | None) -> str:
    return _money(n)


def pct(n: float) -> str:
    return _pct(n)
