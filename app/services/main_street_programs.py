"""Main Street intake — taxonomies, document packages, and deterministic
program-fit screening.

"Main Street" is the third public intake vertical, after the dealer
(``dealer_gatekeeper_v1``) and real-estate funnels. It serves ordinary operating
businesses: restaurants, mechanic shops, grocery and commodities, trucking,
manufacturing, retail, construction, and professional practices.

Two axes drive everything here, and the order matters:

**Intent first.** Not every lead wants a loan. ``merchant_services`` and
``business_systems`` are non-lending products, and forcing them through an
underwriting package is both wrong and a conversion killer — a business asking
for lower card-processing rates should never be told to upload two years of tax
returns. Intent decides the document package, whether program fit runs at all,
and whether a fundability verdict is meaningful.

**Industry second.** It layers conditional documents on top, and only for
lending intents. It affects 3 of the 8 borrower-facing programs (the two SBA
sector programs and transportation); the rest are gated on cash flow and
documentation, not sector.

Everything in this module is a pure function over plain mappings. No DB session,
no ORM models, no imports from ``app.routers`` — so it is unit-testable in
isolation and the router stays a thin caller. Mirrors the deterministic,
non-AI screening approach of ``_compute_loan_program_fit`` in
``app/routers/dealer_ai_intake.py`` without inheriting its dealer coupling.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

__all__ = [
    "MAIN_STREET_VARIANT",
    "MAIN_STREET_INDUSTRIES",
    "MAIN_STREET_INTENTS",
    "IntentKind",
    "intent_kind",
    "normalize_intent",
    "normalize_industry",
    "MAIN_STREET_REQUIRED_DOCUMENTS",
    "documents_for",
    "MAIN_STREET_PROGRAM_LABELS",
    "BORROWER_SUGGESTABLE_PROGRAMS",
    "compute_main_street_program_fit",
    "borrower_safe_programs",
]

MAIN_STREET_VARIANT = "main_street_v1"

# ══════════════════════════════════════════════════════════════════════════
# Intent — what brought this business here
# ══════════════════════════════════════════════════════════════════════════

IntentKind = Literal["lending", "non_lending", "route_out"]

# ``route_out`` intents are answered by a funnel that already exists. They are
# offered in the picker so a mis-arrived lead is handed off deliberately rather
# than screened against the wrong package — and so the dealer and real-estate
# funnels stop being polluted by Main Street traffic.
MAIN_STREET_INTENTS: dict[str, dict[str, Any]] = {
    "working_capital": {
        "kind": "lending",
        "en": "Working capital or a business loan",
        "es": "Capital de trabajo o un préstamo comercial",
    },
    "equipment": {
        "kind": "lending",
        "en": "Financing equipment or a vehicle",
        "es": "Financiar equipo o un vehículo",
    },
    "refinance_debt": {
        "kind": "lending",
        "en": "Refinancing existing debt or advances",
        "es": "Refinanciar deuda o adelantos existentes",
    },
    "merchant_services": {
        "kind": "non_lending",
        "en": "Lower card processing rates",
        "es": "Tasas más bajas de procesamiento de tarjetas",
    },
    "business_systems": {
        "kind": "non_lending",
        "en": "Point-of-sale or business management systems",
        "es": "Sistemas de punto de venta o de gestión",
    },
    "not_sure": {
        "kind": "lending",
        "en": "Not sure yet — help me figure it out",
        "es": "Aún no estoy seguro — ayúdenme a decidir",
    },
    "property": {
        "kind": "route_out",
        "en": "Buying or refinancing property",
        "es": "Comprar o refinanciar una propiedad",
        "route": "/funding-review",
    },
    "dealership": {
        "kind": "route_out",
        "en": "Financing a car dealership",
        "es": "Financiar un concesionario de autos",
        "route": "/dealer-ai-underwriter",
    },
}

_DEFAULT_INTENT = "not_sure"


def normalize_intent(value: Any) -> str:
    """Map an arbitrary inbound value to a known intent slug.

    Unrecognized input falls back to ``not_sure`` rather than to a specific
    product — guessing wrong here would seed the wrong document package.
    """
    if isinstance(value, str) and value.strip() in MAIN_STREET_INTENTS:
        return value.strip()
    return _DEFAULT_INTENT


def intent_kind(intent: Any) -> IntentKind:
    return MAIN_STREET_INTENTS[normalize_intent(intent)]["kind"]  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════════════
# Industry
# ══════════════════════════════════════════════════════════════════════════

# CROSS-REPO CONTRACT. These nine slugs must stay identical to
# ``VERTICALS[].industries[].slug`` in QCWeb/src/lib/programs.ts and to
# ``MainStreetIndustry`` in QCDashboard/src/lib/intakeIndustries.ts. Nothing
# enforces the match at build time, so an unrecognized slug normalizes to
# ``other`` rather than being mis-routed.
MAIN_STREET_INDUSTRIES: dict[str, dict[str, str]] = {
    "restaurant_food_service": {
        "en": "Restaurants and food service",
        "es": "Restaurantes y servicio de alimentos",
    },
    "auto_service": {
        "en": "Mechanic and auto service shops",
        "es": "Talleres mecánicos y de servicio automotriz",
    },
    "grocery_commodities": {
        "en": "Grocery, produce and commodities",
        "es": "Supermercados, productos agrícolas y materias primas",
    },
    "trucking_logistics": {
        "en": "Trucking, freight and logistics",
        "es": "Transporte de carga y logística",
    },
    "manufacturing": {
        "en": "Manufacturing and fabrication",
        "es": "Manufactura y fabricación",
    },
    "retail_ecommerce": {
        "en": "Retail and e-commerce",
        "es": "Comercio minorista y comercio electrónico",
    },
    "construction_trades": {
        "en": "Construction and trades",
        "es": "Construcción y oficios",
    },
    "professional_practice": {
        "en": "Professional and medical practices",
        "es": "Prácticas profesionales y médicas",
    },
    "other": {
        "en": "Other operating business",
        "es": "Otro negocio en operación",
    },
}

_DEFAULT_INDUSTRY = "other"


def normalize_industry(value: Any) -> str:
    if isinstance(value, str) and value.strip() in MAIN_STREET_INDUSTRIES:
        return value.strip()
    return _DEFAULT_INDUSTRY


# ══════════════════════════════════════════════════════════════════════════
# Documents
# ══════════════════════════════════════════════════════════════════════════

# Two load-bearing naming constraints, both verified against the existing
# pipeline. ``_baseline_key`` in app/services/bucket_ai.py keys off the literal
# substrings "bank"+"statement" and "tax"+"return" in the document NAME, and
# ``_apply_lending_readiness`` uses that mapping for its >=6-months / >=2-years
# minimum-evidence check. Renaming either of the first two rows silently breaks
# readiness scoring, so keep those word pairs intact.
MAIN_STREET_REQUIRED_DOCUMENTS: list[dict[str, Any]] = [
    {
        "name": "Last 6 months business bank statements",
        "category": "Bank Statements",
        "description": (
            "Upload the last six months from your main business operating account. "
            "This is the single most useful document — it establishes deposits, cash "
            "flow and stability."
        ),
        "allow_multiple_files": True,
        "required": True,
    },
    {
        "name": "Last 2 years business tax returns",
        "category": "Financials",
        "description": (
            "Upload the business tax returns for the last two years. If the current "
            "year is on extension, upload the extension filing instead."
        ),
        "allow_multiple_files": True,
        "required": True,
    },
    {
        "name": "Year-to-date P&L and balance sheet",
        "category": "Financials",
        "description": (
            "Upload the current year-to-date profit and loss statement and balance "
            "sheet. An export from your accounting software is fine."
        ),
        "allow_multiple_files": True,
        "required": True,
    },
    {
        "name": "Business debt schedule",
        "category": "Debts",
        "description": (
            "List every outstanding business debt: lender, balance and monthly "
            "payment. You can fill this out online here instead of uploading a file."
        ),
        "allow_multiple_files": False,
        "required": True,
    },
    # Present so the borrower can volunteer it, but NOT required day one. The PFS
    # is the highest-friction item in the package and it drives no Main Street
    # threshold on its own — it earns its place on an SBA or jumbo file and
    # nowhere else. Promoted to required by the router when the file heads down
    # one of those paths.
    {
        "name": "Owner personal financial statement",
        "category": "Personal Financials",
        "description": (
            "A personal financial statement for each owner with 20% or more "
            "ownership. Needed for SBA and larger term requests — you can fill it "
            "out online here."
        ),
        "allow_multiple_files": True,
        "required": False,
    },
]

_MERCHANT_SERVICES_DOCUMENTS: list[dict[str, Any]] = [
    {
        "name": "Merchant processing statements — last 3 months",
        "category": "Merchant Processing",
        "description": (
            "Upload the last three monthly statements from your current card "
            "processor. They show your effective rate and monthly volume, which is "
            "all we need to tell you whether we can beat it."
        ),
        "allow_multiple_files": True,
        "required": True,
    },
]

_INTENT_EXTRA_DOCUMENTS: dict[str, list[dict[str, Any]]] = {
    "equipment": [
        {
            "name": "Vendor quote or invoice for the equipment",
            "category": "Equipment",
            "description": "The quote or invoice for the equipment or vehicle being financed.",
            "allow_multiple_files": True,
            "required": True,
        },
    ],
    "refinance_debt": [
        {
            "name": "Existing advance and loan agreements with payoff letters",
            "category": "Debts",
            "description": (
                "Every current advance or loan agreement plus a payoff letter for each. "
                "We need the payoff amounts and draw frequency to model whether a single "
                "structured payment actually clears the stack."
            ),
            "allow_multiple_files": True,
            "required": True,
        },
    ],
}

# Layered on top of the baseline once industry is known, and only for lending
# intents. Added dynamically rather than shown up front: a restaurant owner who
# sees "DOT operating authority" on turn one concludes the form is not for them
# and leaves. The router rate-limits this to at most two new documents per turn.
_INDUSTRY_DOCUMENTS: dict[str, list[dict[str, Any]]] = {
    "restaurant_food_service": [
        {"name": "Merchant processing statements — last 3 months", "category": "Merchant Processing"},
        {"name": "Food service or health permit", "category": "Licenses"},
        {"name": "Liquor license, if applicable", "category": "Licenses"},
        {"name": "Signed lease for the operating location", "category": "Property"},
    ],
    "auto_service": [
        {"name": "Repair or dealer license, or state registration", "category": "Licenses"},
        {"name": "Merchant processing statements — last 3 months", "category": "Merchant Processing"},
        {"name": "Signed lease for the operating location", "category": "Property"},
        {"name": "Equipment schedule — lifts, alignment, diagnostics", "category": "Equipment"},
    ],
    "grocery_commodities": [
        {"name": "Merchant processing statements — last 3 months", "category": "Merchant Processing"},
        {"name": "Signed lease or purchase contract for the operating location", "category": "Property"},
        {"name": "Supplier or purchase ledger", "category": "Inventory"},
    ],
    "trucking_logistics": [
        {"name": "MC or DOT operating authority letter", "category": "Licenses"},
        {"name": "IFTA filings — last 4 quarters", "category": "Financials"},
        {"name": "Fleet schedule — VIN, year, mileage, lienholder", "category": "Equipment"},
        {"name": "Certificates of insurance", "category": "Insurance"},
        {"name": "Accounts receivable aging", "category": "Receivables"},
    ],
    "manufacturing": [
        {"name": "Equipment schedule", "category": "Equipment"},
        {"name": "Accounts receivable aging", "category": "Receivables"},
        {"name": "Facility lease or deed", "category": "Property"},
        {"name": "Major customer or purchase-order summary", "category": "Receivables"},
    ],
    "construction_trades": [
        {"name": "Contractor license", "category": "Licenses"},
        {"name": "Work-in-progress schedule", "category": "Financials"},
        {"name": "Accounts receivable aging", "category": "Receivables"},
        {"name": "Bonding capacity letter, if bonded", "category": "Financials"},
    ],
    "professional_practice": [
        {"name": "Professional license(s)", "category": "Licenses"},
        {"name": "Practice management or production report", "category": "Financials"},
        {"name": "Signed lease for the operating location", "category": "Property"},
    ],
    "retail_ecommerce": [
        {"name": "Merchant processing statements — last 3 months", "category": "Merchant Processing"},
        {"name": "Signed lease or platform payout statements", "category": "Property"},
    ],
    "other": [],
}


def documents_for(intent: Any, industry: Any = None) -> list[dict[str, Any]]:
    """The document package to seed at intake creation.

    Intent decides the shape; industry only layers extras onto a lending file.
    ``business_systems`` deliberately returns an empty list — it is a
    qualification conversation, not a document review, and asking for paperwork
    would misrepresent what we need.
    """
    slug = normalize_intent(intent)
    kind = intent_kind(slug)

    if slug == "business_systems":
        return []
    if slug == "merchant_services":
        return [dict(row) for row in _MERCHANT_SERVICES_DOCUMENTS]
    if kind == "route_out":
        return []

    package = [dict(row) for row in MAIN_STREET_REQUIRED_DOCUMENTS]
    package.extend(dict(row) for row in _INTENT_EXTRA_DOCUMENTS.get(slug, ()))

    seen = {row["name"] for row in package}
    for row in _INDUSTRY_DOCUMENTS.get(normalize_industry(industry), ()):
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        package.append(
            {
                "name": row["name"],
                "category": row["category"],
                "description": row.get("description", ""),
                "allow_multiple_files": True,
                "required": False,
            }
        )
    return package


# ══════════════════════════════════════════════════════════════════════════
# Screening thresholds
# ══════════════════════════════════════════════════════════════════════════

# PROVISIONAL, pending lending-desk sign-off — the same status the dealer
# constants in dealer_ai_intake.py carried when first added. Derived by analogy
# to those, not approved by an underwriter. Do not quote these to a borrower.

# Minimum profile. Mirrors ELIGIBILITY_RULE in QCWeb/src/lib/programs.ts, which
# is what the marketing site publishes. Every borrower-facing program is gated
# on all three.
_MS_MIN_YEARS_IN_BUSINESS = 1.0
_MS_MIN_ANNUAL_REVENUE = 100_000
_MS_MIN_FICO = 640

# Evidence minimums, counted from per-file facts rather than model claims.
_MS_MIN_BANK_MONTHS = 6
_MS_MIN_TAX_YEARS = 2

_MS_LOC_MIN_REVENUE = 100_000
_MS_LOC_MIN_BANK_MONTHS = 3  # a line screens on deposits alone — lighter doc bar
_MS_LOC_MAX_NSF_COUNT = 3  # six-month aggregate

_MS_TERM_3_5_MIN_REVENUE = 150_000
_MS_TERM_3_5_MIN_DSCR = 1.00
_MS_TERM_10YR_MIN_REVENUE = 300_000
_MS_TERM_10YR_MIN_DSCR = 1.15
_MS_TERM_10YR_MIN_TIB = 2.0
_MS_HYBRID_MIN_REVENUE = 200_000
_MS_HYBRID_MIN_DSCR = 1.10

_MS_JUMBO_MIN_REVENUE = 5_000_000
_MS_JUMBO_MIN_DSCR = 1.25
_MS_JUMBO_MIN_REQUEST = 1_000_000  # below this it is a term loan, not a jumbo

_MS_EQUIPMENT_MIN_CASH_FLOW = 0  # must clear zero

_MS_MERCHANT_MIN_VOLUME = 120_000  # annualized card volume

_MS_TRANSPORT_MIN_REVENUE = 150_000
_MS_FACTORING_MIN_RECEIVABLES = 50_000

_MS_SBA_MIN_TIB = 2.0  # SBA wants two filed years
_MS_SBA_MIN_DSCR = 1.15

_MS_DEBT_CONSULTING_MAX_DSCR = 1.00

_MS_SBA_GROCERY_INDUSTRIES = frozenset({"grocery_commodities", "restaurant_food_service"})


# ══════════════════════════════════════════════════════════════════════════
# Programs
# ══════════════════════════════════════════════════════════════════════════

MAIN_STREET_PROGRAM_LABELS: dict[str, str] = {
    # Borrower-facing, mapped 1:1 to the Main Street programs on the marketing site.
    "line_of_credit": "Lines of Credit",
    "term_loan_3_5_year": "Term Loans",
    "term_loan_10_year": "Term Loans",
    "term_loan_loc_hybrid": "Hybrid Term / LOC",
    "equipment_financing": "Equipment Financing",
    "jumbo_term_loan": "Jumbo Term Loan",
    "transportation_finance": "Transportation Finance",
    "sba": "SBA 7(a)",
    "sba_grocery": "SBA Grocery",
    "sba_made_in_america": "SBA Made in America",
    # Internal routing signals — never shown to a borrower.
    "real_estate_backed": "Real-estate-backed",
    "merchant_processing": "Merchant Processing",
    "transportation_factoring": "Transportation Factoring",
    "debt_consulting": "Debt Consulting",
}

# Programs a borrower may ever be told about. The four omitted keys are
# routing or distress signals for the underwriter, not products we offer this
# borrower in conversation — telling someone unprompted that they look like a
# debt-consolidation case is a bad experience and a bad sales motion.
BORROWER_SUGGESTABLE_PROGRAMS = frozenset(
    {
        "line_of_credit",
        "term_loan_3_5_year",
        "term_loan_10_year",
        "term_loan_loc_hybrid",
        "equipment_financing",
        "jumbo_term_loan",
        "transportation_finance",
        "sba",
        "sba_grocery",
        "sba_made_in_america",
    }
)

# Hand-written, never model-authored. The assistant relays these verbatim rather
# than composing its own rationale, which removes the largest hallucination
# surface in the whole feature. Each names the evidence, not an outcome — no
# "you qualify", no rate, no amount.
_WHY_TEMPLATES: dict[str, str] = {
    "line_of_credit": "Your deposit activity supports a revolving line you can draw on as needed.",
    "term_loan_3_5_year": "Filed returns and current cash flow support a fixed monthly payment.",
    "term_loan_10_year": "Two or more filed years and steady coverage support a longer amortization.",
    "term_loan_loc_hybrid": (
        "Your file supports both a fixed piece and a revolving piece, which suits a known "
        "need plus a variable one."
    ),
    "equipment_financing": (
        "The equipment secures the financing, so approval leans on the asset as much as the "
        "balance sheet."
    ),
    "jumbo_term_loan": "Revenue and coverage at this scale open the large-balance structures.",
    "transportation_finance": (
        "Your fleet and haul revenue support financing structured around settlement timing."
    ),
    "sba": "Two filed years and owner-occupied use point toward government-backed terms.",
    "sba_grocery": "Your sector qualifies for the government-backed food and agriculture program.",
    "sba_made_in_america": (
        "Domestic manufacturing qualifies for the government-backed production program."
    ),
}


def _num(value: Any) -> float | None:
    """Coerce to a real number. Rejects bool, which is an int in Python and
    would otherwise sail through every threshold check as 0 or 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _meets(value: Any, threshold: float) -> bool:
    n = _num(value)
    return n is not None and n >= threshold


def _clears(value: Any, threshold: float) -> bool:
    n = _num(value)
    return n is not None and n > threshold


def _uploaded_names(documents: Sequence[Mapping[str, Any]]) -> set[str]:
    """Names of documents that actually have a file against them."""
    return {
        str(doc.get("name", "")).lower()
        for doc in documents
        if doc.get("status") in {"received", "complete", "uploaded"}
    }


def _has(documents: Sequence[Mapping[str, Any]], *needles: str) -> bool:
    """True when some uploaded document name contains every needle."""
    lowered = [n.lower() for n in needles]
    return any(all(n in name for n in lowered) for name in _uploaded_names(documents))


def _minimum_profile(
    revenue: float | None, tib: float | None, fico: float | None
) -> list[str]:
    """Blockers against the published minimum profile. Internal-only strings."""
    blocked: list[str] = []
    if not _meets(revenue, _MS_MIN_ANNUAL_REVENUE):
        blocked.append("annual revenue below the minimum profile")
    if not _meets(tib, _MS_MIN_YEARS_IN_BUSINESS):
        blocked.append("time in business below the minimum profile")
    if not _meets(fico, _MS_MIN_FICO):
        blocked.append("credit score below the minimum profile")
    return blocked


def compute_main_street_program_fit(
    *,
    intent: Any,
    industry: Any = None,
    key_metrics: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
    documents: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic, non-AI screen of which programs this file trends toward.

    Returns a mapping of program key to ``{"eligible": bool, "blocked_by":
    [...], "needs": [...], <signals>}``. ``blocked_by`` is internal;
    ``needs`` is borrower-safe.

    Gated on intent kind. A ``business_systems`` file gets ``{}`` — there is no
    lending question to answer. A ``merchant_services`` file gets only the
    merchant screen, read from processing statements rather than bank deposits,
    because that path never collects bank statements at all. Everything the
    caller does downstream (fundability banner, DSCR, LTV) should be suppressed
    when this returns empty or merchant-only.
    """
    km = key_metrics or {}
    det = details or {}
    docs = documents or []

    slug = normalize_intent(intent)
    kind = intent_kind(slug)
    sector = normalize_industry(industry)

    if kind == "route_out" or slug == "business_systems":
        return {}

    # ── Merchant services: its own screen, its own evidence ──
    # The dealer implementation derives this from annualized bank deposits,
    # which only exist after a lending package. On this path there are no bank
    # statements, so the question is answered from the processing statements
    # themselves: what is the current effective rate and monthly volume.
    if slug == "merchant_services":
        volume = _num(km.get("annualized_card_volume"))
        has_statements = _has(docs, "merchant processing")
        needs: list[str] = []
        if not has_statements:
            needs.append("Your last 3 monthly processing statements")
        elif volume is None:
            needs.append("A processing statement that shows monthly volume and effective rate")
        return {
            "merchant_processing": {
                "eligible": bool(has_statements and _meets(volume, _MS_MERCHANT_MIN_VOLUME)),
                "blocked_by": (
                    [] if _meets(volume, _MS_MERCHANT_MIN_VOLUME) else ["card volume below screen"]
                ),
                "needs": needs,
                "annualized_card_volume": volume,
                "current_effective_rate": _num(km.get("current_effective_rate")),
            }
        }

    # ── Lending intents ──
    revenue = _num(km.get("ytd_annualized_revenue")) or _num(km.get("annualized_adjusted_deposits"))
    dscr = _num(km.get("estimated_dscr"))
    cash_flow = _num(km.get("estimated_ebitda_or_cash_flow"))
    requested = _num(km.get("requested_amount"))
    bank_months = _num(km.get("bank_statement_months")) or 0.0
    tax_years = _num(km.get("tax_return_years")) or 0.0
    nsf_count = _num(km.get("nsf_or_overdraft_count"))
    receivables = _num(km.get("total_receivables"))
    tib = _num(det.get("years_in_business"))
    fico = _num(det.get("estimated_credit_score"))

    profile_blockers = _minimum_profile(revenue, tib, fico)

    def program(
        eligible: bool,
        *,
        blocked: Sequence[str] = (),
        needs: Sequence[str] = (),
        **signals: Any,
    ) -> dict[str, Any]:
        all_blockers = [*profile_blockers, *blocked]
        return {
            "eligible": bool(eligible and not all_blockers),
            "blocked_by": all_blockers,
            "needs": list(needs),
            **signals,
        }

    evidence_needs: list[str] = []
    if bank_months < _MS_MIN_BANK_MONTHS:
        evidence_needs.append("Six months of business bank statements")
    if tax_years < _MS_MIN_TAX_YEARS:
        evidence_needs.append("Two years of business tax returns")

    line_of_credit = program(
        _meets(revenue, _MS_LOC_MIN_REVENUE)
        and bank_months >= _MS_LOC_MIN_BANK_MONTHS
        and (nsf_count is None or nsf_count <= _MS_LOC_MAX_NSF_COUNT),
        blocked=(
            []
            if nsf_count is None or nsf_count <= _MS_LOC_MAX_NSF_COUNT
            else ["overdraft activity above screen"]
        ),
        needs=(
            []
            if bank_months >= _MS_LOC_MIN_BANK_MONTHS
            else ["At least three months of business bank statements"]
        ),
        revenue=revenue,
        bank_months=bank_months,
    )

    term_loan_3_5_year = program(
        _meets(revenue, _MS_TERM_3_5_MIN_REVENUE) and _meets(dscr, _MS_TERM_3_5_MIN_DSCR),
        needs=evidence_needs,
        revenue=revenue,
        dscr=dscr,
    )

    term_loan_10_year = program(
        _meets(revenue, _MS_TERM_10YR_MIN_REVENUE)
        and _meets(dscr, _MS_TERM_10YR_MIN_DSCR)
        and _meets(tib, _MS_TERM_10YR_MIN_TIB),
        needs=evidence_needs,
        revenue=revenue,
        dscr=dscr,
        years_in_business=tib,
    )

    term_loan_loc_hybrid = program(
        _meets(revenue, _MS_HYBRID_MIN_REVENUE) and _meets(dscr, _MS_HYBRID_MIN_DSCR),
        needs=evidence_needs,
        revenue=revenue,
        dscr=dscr,
    )

    jumbo_term_loan = program(
        _meets(revenue, _MS_JUMBO_MIN_REVENUE)
        and _meets(dscr, _MS_JUMBO_MIN_DSCR)
        and _meets(requested, _MS_JUMBO_MIN_REQUEST),
        needs=evidence_needs,
        revenue=revenue,
        dscr=dscr,
        requested_amount=requested,
    )

    wants_equipment = bool(det.get("financing_equipment_or_vehicle")) or slug == "equipment"
    has_quote = _has(docs, "vendor quote") or _has(docs, "invoice")
    equipment_financing = program(
        wants_equipment and _clears(cash_flow, _MS_EQUIPMENT_MIN_CASH_FLOW),
        blocked=[] if wants_equipment else ["no equipment purchase stated"],
        needs=[] if has_quote else ["A vendor quote or invoice for the equipment"],
        cash_flow=cash_flow,
    )

    is_trucking = sector == "trucking_logistics"
    has_authority = _has(docs, "authority")
    transportation_finance = program(
        is_trucking and _meets(revenue, _MS_TRANSPORT_MIN_REVENUE),
        blocked=[] if is_trucking else ["not a transportation file"],
        needs=(
            []
            if has_authority
            else ["Your MC or DOT operating authority letter", "A fleet schedule with VINs"]
        ),
        revenue=revenue,
    )

    # SBA does not reuse the dealer "no missing required docs" shortcut: the PFS
    # is optional in the Main Street baseline, so that predicate would flip SBA
    # eligible without one. It carries its own explicit document test.
    sba_docs_complete = (
        bank_months >= _MS_MIN_BANK_MONTHS
        and tax_years >= _MS_MIN_TAX_YEARS
        and _has(docs, "personal financial statement")
        and _has(docs, "debt schedule")
    )
    sba_needs = list(evidence_needs)
    if not _has(docs, "personal financial statement"):
        sba_needs.append("A personal financial statement for each 20%+ owner")
    if not _has(docs, "debt schedule"):
        sba_needs.append("Your business debt schedule")

    sba_core = (
        _meets(tib, _MS_SBA_MIN_TIB) and _meets(dscr, _MS_SBA_MIN_DSCR) and sba_docs_complete
    )
    sba = program(sba_core, needs=sba_needs, dscr=dscr, years_in_business=tib)
    sba_grocery = program(
        sba_core and sector in _MS_SBA_GROCERY_INDUSTRIES,
        blocked=[] if sector in _MS_SBA_GROCERY_INDUSTRIES else ["sector not eligible"],
        needs=sba_needs,
    )
    sba_made_in_america = program(
        sba_core and sector == "manufacturing",
        blocked=[] if sector == "manufacturing" else ["sector not eligible"],
        needs=sba_needs,
    )

    # ── Internal-only signals ──
    real_estate_backed = {
        "eligible": bool(det.get("owns_operating_property")),
        "blocked_by": [],
        "needs": [],
    }
    merchant_processing = {
        "eligible": _meets(km.get("annualized_card_volume"), _MS_MERCHANT_MIN_VOLUME),
        "blocked_by": [],
        "needs": [],
        "annualized_card_volume": _num(km.get("annualized_card_volume")),
    }
    transportation_factoring = {
        "eligible": is_trucking and _meets(receivables, _MS_FACTORING_MIN_RECEIVABLES),
        "blocked_by": [] if is_trucking else ["not a transportation file"],
        "needs": [],
        "total_receivables": receivables,
    }
    debt_consulting = {
        "eligible": dscr is not None and dscr <= _MS_DEBT_CONSULTING_MAX_DSCR,
        "blocked_by": [],
        "needs": [],
        "dscr": dscr,
    }

    return {
        "line_of_credit": line_of_credit,
        "term_loan_3_5_year": term_loan_3_5_year,
        "term_loan_10_year": term_loan_10_year,
        "term_loan_loc_hybrid": term_loan_loc_hybrid,
        "jumbo_term_loan": jumbo_term_loan,
        "equipment_financing": equipment_financing,
        "transportation_finance": transportation_finance,
        "sba": sba,
        "sba_grocery": sba_grocery,
        "sba_made_in_america": sba_made_in_america,
        "real_estate_backed": real_estate_backed,
        "merchant_processing": merchant_processing,
        "transportation_factoring": transportation_factoring,
        "debt_consulting": debt_consulting,
    }


def borrower_safe_programs(
    fit: Mapping[str, Any] | None,
    labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Reduce the internal fit dict to the borrower-visible shape.

    An allowlist on the way out, not a denylist on the way in: only the four
    keys below survive, so a signal added to a program row later cannot leak by
    default. Never pricing, never approval odds, never a raw metric that reads
    as an offer.

    NOTE: nothing calls this while ``features.suggestedPrograms`` is off — Main
    Street currently inherits the dealer policy that program names are internal.
    It exists so that flipping the policy is a one-line change rather than a
    rewrite, and so the redaction boundary is written and tested before it is
    ever needed.
    """
    table = labels or MAIN_STREET_PROGRAM_LABELS
    out: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for key, row in (fit or {}).items():
        if key not in BORROWER_SUGGESTABLE_PROGRAMS or not isinstance(row, Mapping):
            continue
        if not row.get("eligible"):
            continue
        label = table.get(key, key)
        # The two term bands share a borrower-facing label; collapse to one card.
        if label in seen_labels:
            continue
        seen_labels.add(label)
        out.append(
            {
                "key": key,
                "label": label,
                "why": _WHY_TEMPLATES.get(key, ""),
                "still_needed": [str(n)[:120] for n in (row.get("needs") or [])][:3],
            }
        )
    return out[:3]
