"""Source-dated, editable suggestions. Reading a baseline never changes policy.

Marketing ranges are not underwriting thresholds. Only explicitly published
minimums become fit rules; qualitative criteria remain document review checks.
An administrator must save and publish their reviewed configuration separately.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.mca_refinance_policy import MAX_ACTIVE_MCAS, MAX_AMOUNT, MAX_TERM_MONTHS

BASELINE_VERSION = "qcweb-2026-09-27-v1"
SITE = "https://qualifiedcommercial.com"
SBA_SOURCE = "https://www.sba.gov/document/sop-50-10-lender-development-company-loan-programs"
CENSUS_SOURCE = "https://www.census.gov/naics/reference_files_tools/2022_NAICS_Manual.pdf"
# A lender guideline kept in the repository, so the transcription can be checked
# against its source: docs/program_sources/quidity_direct_10_year_term_loan_2026-10.pdf
QUIDITY_SOURCE = "repo://docs/program_sources/quidity_direct_10_year_term_loan_2026-10.pdf"


def check(key: str, label: str, instructions: str, severity: str = "review") -> dict[str, str]:
    return {"key": key, "label": label, "instructions": instructions, "severity": severity}


TAX_CHECKS = [
    check(
        "net_income_nonnegative",
        "No negative earnings in either tax year",
        "Proposed QC policy: identify the same net-income measure on each of the last two filed business returns. Record each year, amount, and source page. Escalate a negative result or missing/ambiguous year; do not substitute revenue for earnings.",
        "block",
    ),
    check(
        "net_income_not_declining",
        "No year-over-year earnings decline",
        "Proposed QC policy: compare comparable full tax years for the same entity and net-income measure. Record both amounts and the change. A lower latest-year amount needs underwriting review; do not treat missing periods as a pass.",
        "block",
    ),
]


def document(
    key: str,
    label: str,
    instructions: str,
    *,
    checks: list[dict] | None = None,
    category: str = "financials",
    stage: str = "underwriting",
    level: str = "required",
    waivable: bool = True,
) -> dict[str, Any]:
    return {
        "requirement_key": key,
        "label": label,
        "category": category,
        "required_level": level,
        "blocks_stage": stage,
        "visibility": ["agent", "borrower", "underwriter"],
        "verification_required": True,
        "can_underwriter_waive": waivable,
        "completion_mode": "requires_human_verify",
        "display_order": 0,
        "objective_text": f"Review {label.lower()} against the approved program requirements.",
        "completion_criteria": instructions,
        "review_checks": checks or [],
    }


def banks(months: int = 6, checks: list[dict] | None = None) -> dict:
    return document(
        f"business_bank_statements_{months}_months",
        f"Last {months} months business bank statements",
        f"Obtain {months} consecutive complete official monthly statements. Check entity/account identity, statement periods, deposits, debt withdrawals, and all pages. Uploaded/readable does not mean financially acceptable.",
        checks=checks,
    )


def taxes(years: int = 2, *, patterns: bool = True, stage: str = "underwriting") -> dict:
    return document(
        f"business_tax_returns_{years}_years",
        f"Last {years} filed business tax return{'s' if years != 1 else ''}",
        f"Obtain {years} complete filed business returns and relevant schedules. Reconcile entity and fiscal years. Explain differences from management accounts. Do not infer a trend from a single year.",
        checks=deepcopy(TAX_CHECKS) if patterns and years >= 2 else [],
        stage=stage,
    )


P_AND_L = document(
    "ytd_p_and_l_balance_sheet",
    "Year-to-date P&L and balance sheet",
    "Reconcile reporting period, entity, revenue, earnings, assets, liabilities, and debt to source records. Compare like-for-like periods, not partial-year totals against full years.",
)
DEBT = document(
    "business_debt_schedule",
    "Business debt schedule and supporting obligations",
    "List every lender, balance, payment, maturity, lien, and MCA/RBF position. Reconcile recurring statement debits to agreements and payoff information; flag omitted obligations.",
)
PFS = document(
    "owner_personal_financial_statement",
    "Owner personal financial statement",
    "Review required owners, completeness, liquidity, liabilities, contingent obligations, signatures, and date.",
)
USE = document(
    "use_of_proceeds",
    "Business use of proceeds",
    "Describe intended business uses and reconcile the requested funding to supporting costs. Escalate personal uses or unsupported refinance assumptions.",
    category="borrower_info",
)
LICENSE = document(
    "business_license",
    "Business / dealer license and ownership",
    "Confirm operating authority, current license, business identity, and ownership where applicable.",
    category="compliance",
)
INVENTORY = document(
    "inventory_and_floorplan",
    "Inventory and floorplan schedule",
    "Reconcile inventory aging, title status, floorplan balances, curtailments, and audit exceptions.",
)
QUOTE = document(
    "equipment_quote",
    "Equipment quote / invoice",
    "Identify vendor, equipment, costs, intended use, useful life, and any existing liens. Verify suitability for the chosen financing program.",
)
ID = document(
    "owner_identification",
    "Guarantor identification",
    "Use the secure identity process for required guarantors. Verify identity; do not infer credit eligibility from personal characteristics.",
    category="compliance",
    stage="closing",
)


def standard_docs() -> list[dict]:
    return deepcopy([taxes(), banks(), P_AND_L, DEBT, PFS])


def rule(field: str, value: float, op: str = "gte") -> dict:
    return {"field": field, "op": op, "value": value}


# Each row is intentionally a draftable suggestion rather than a migration
# changing the customer's published versions or their current file selections.
CATALOG: dict[str, dict[str, Any]] = {
    "sba_7a": {
        "name": "SBA 7(a)",
        "slug": "sba-7a",
        "verticals": ["real_estate", "dealer", "main_street"],
        "rules": [rule("business_age_years", 2), rule("credit_score", 660)],
        "docs": [
            taxes(3),
            deepcopy(P_AND_L),
            deepcopy(DEBT),
            deepcopy(USE),
            deepcopy(PFS),
            document(
                "project_support",
                "Acquisition contract / project quotes and owner resume",
                "Verify transaction costs, management background, and business-purpose use. For Standard 7(a), the QC overview lists 680 guarantor FICO, 1.15 DSCR and 10% acquisition injection; Small 7(a) is a different screen. Staff must identify the variant and apply its requirements.",
            ),
        ],
        "notes": [
            "Website: QC's SBA overview lists two operating years and 660 FICO for Small/Express; Standard 7(a) lists 680 FICO and 1.15 DSCR. This starter does not collapse those variants into one stricter universal screen.",
            "Website: the dedicated 7(a) page requests three years of returns when available. Confirm exceptions and lender requirements before publishing.",
        ],
    },
    "sba_504": {
        "name": "SBA 504",
        "slug": "sba-504",
        "verticals": ["real_estate", "dealer", "main_street"],
        "rules": [rule("business_age_years", 2), rule("credit_score", 680), rule("dscr", 1.2)],
        "recommendation_preferences": [
            {
                "key": "real_estate_equipment_majority",
                "label": "QC preference: at least 51% real estate or equipment",
                # This derived percentage is null until the server has an
                # exact, complete allocation. Keeping the editable policy to
                # one threshold is therefore just as fail-closed as a second
                # explicit completeness condition.
                "when": {"field": "real_estate_equipment_pct", "op": "gte", "value": 51},
                "score": 50,
            }
        ],
        "docs": standard_docs()
        + [
            document(
                "fixed_asset_project",
                "Fixed-asset project and occupancy",
                "Review purchase/construction budget or long-lived equipment, occupancy, and equity sources. QC describes 10% injection, at least 51% owner occupancy for an existing property, and at least 60% for new construction. Staff must verify the applicable property type, occupancy timing, expansion conditions and lender structure; do not apply a property-occupancy percentage to equipment-only financing. Do not use 504 for general working capital or assume MCA debt qualifies.",
            )
        ],
        "notes": [
            "Website: QC's SBA overview lists two operating years, 680 guarantor FICO and 1.20 DSCR for 504; project-specific occupancy and injection require staff verification.",
            "Proposed QC policy: when a complete itemized use-of-funds total is at least 51% real estate and/or equipment, prefer SBA 504 for staff review. This is recommendation ordering only, not eligibility, approval, or an SBA occupancy test. Any remaining working-capital, inventory, refinancing, fee, or other uses need separate program and structure review.",
        ],
    },
    "sba_express": {
        "name": "SBA Express",
        "slug": "sba-express",
        "verticals": ["real_estate", "dealer", "main_street"],
        "rules": [
            rule("business_age_years", 2),
            rule("credit_score", 660),
            rule("requested_amount", 500_000, "lte"),
        ],
        "docs": standard_docs()
        + [
            deepcopy(USE),
            document(
                "sba_forms",
                "Applicable SBA borrower / personal financial forms",
                "Collect and review current required forms and the lender's SBA credit screen. Confirm all required guarantors, eligibility, and use of proceeds.",
                category="compliance",
            ),
        ],
        "notes": [
            "Website: QC's SBA overview lists two operating years and 660 FICO for Small/Express; the lender's SBA credit screen remains a separate review.",
            "Website: SBA currently caps SBA Express and Export Express loans at $500,000. This is not the $350,000 threshold used for other SBA scoring or pricing contexts.",
        ],
    },
    "dscr": {
        "name": "DSCR Rental Loans",
        "slug": "dscr-rental",
        "verticals": ["real_estate"],
        "docs": [
            document(
                "rent_roll",
                "Leases / current rent roll",
                "Verify leases, sustainable rents, occupancy, and operating expenses. Recalculate property DSCR using the proposed full housing payment; a sample calculator value is not the lender's required DSCR.",
            ),
            document(
                "property_insurance",
                "Property insurance quote / binder",
                "Confirm property, coverage and premium used in the payment calculation.",
                category="insurance",
            ),
            document(
                "entity_or_vesting",
                "Entity / vesting documents",
                "Confirm borrowing entity, authority, ownership, and EIN.",
                category="borrower_info",
            ),
            document(
                "transaction_contract_payoff",
                "Purchase contract or refinance payoff",
                "Confirm purchase/refinance structure, property and lien balances.",
                category="property_data",
            ),
            document(
                "operating_account_ach",
                "Property operating-account ACH",
                "Verify account ownership using the secure closing flow.",
                stage="closing",
            ),
        ],
        "notes": [
            "Website: this program relies on property cash flow, not personal tax-return qualification. No personal tax-return requirement is added by this baseline."
        ],
    },
    "fix_flip": {
        "name": "Fix & Flip",
        "slug": "fix-and-flip",
        "verticals": ["real_estate"],
        "docs": [
            document(
                "rehab_scope",
                "Rehab scope, budget and contractor bid",
                "Review complete hard/soft costs, contingencies, contractor ability and draw schedule.",
                category="property_data",
            ),
            document(
                "arv_support",
                "After-repair value support",
                "Compare credible completed-value evidence, not a target sale price alone.",
                category="property_data",
            ),
            document(
                "purchase_entity",
                "Purchase contract and entity documents",
                "Verify acquisition costs and borrowing authority.",
                category="property_data",
            ),
            document(
                "experience_exit",
                "Prior projects and exit strategy",
                "Review completed projects and a credible sale or refinance exit.",
                category="property_data",
            ),
        ],
    },
    "bridge_purchase": {
        "name": "Bridge / Purchase",
        "slug": "bridge",
        "verticals": ["real_estate"],
        "docs": [
            document(
                "purchase_contract",
                "Purchase contract",
                "Verify terms, dates, property and acquisition costs.",
                category="property_data",
            ),
            document(
                "exit_plan",
                "Exit / takeout financing plan",
                "Verify timing and realistic repayment or refinance assumptions.",
                category="property_data",
            ),
            document(
                "asset_title",
                "Asset summary and preliminary title",
                "Review collateral, title, existing liens and material property risks.",
                category="property_data",
            ),
            document(
                "equity_reserves",
                "Equity, reserves and entity authority",
                "Verify cash-to-close, reserves, ownership and signing authority.",
            ),
        ],
    },
    "construction_capital": {
        "name": "Construction Capital",
        "slug": "construction",
        "verticals": ["real_estate"],
        "docs": [
            document(
                "construction_budget",
                "Hard / soft cost budget and contract",
                "Review budget, contingencies, construction contract and contractor credentials.",
                category="property_data",
            ),
            document(
                "plans_permits",
                "Plans and permits",
                "Confirm approvals and the proposed construction scope.",
                category="property_data",
            ),
            document(
                "completed_value",
                "Completed-value support and land documents",
                "Verify land ownership/acquisition and supported completed value.",
                category="property_data",
            ),
            document(
                "sponsor_exit",
                "Sponsor track record and exit",
                "Review execution ability, equity and realistic repayment.",
                category="borrower_info",
            ),
        ],
    },
    "portfolio_lending": {
        "name": "Portfolio Lending",
        "slug": "portfolio",
        "verticals": ["real_estate"],
        "docs": [
            document(
                "real_estate_schedule",
                "Schedule of real estate owned",
                "Identify each property, ownership, value and associated debt.",
                category="property_data",
            ),
            document(
                "portfolio_rents",
                "Property leases and rent rolls",
                "Review occupancy and sustainable rent property by property.",
                category="property_data",
            ),
            document(
                "portfolio_payoffs",
                "Property debt and payoff statements",
                "Reconcile all loans and liens.",
            ),
            document(
                "portfolio_t12",
                "Property trailing-12 operating statements",
                "Reconcile income and operating expenses across the same period; review concentration risks.",
            ),
            document(
                "portfolio_ownership_insurance",
                "Entity ownership and property insurance",
                "Confirm authority, cross-collateral structure and coverage.",
                category="property_data",
            ),
        ],
    },
    "dealer_working_capital": {
        "name": "Dealer Working Capital",
        "slug": "dealer-working-capital",
        "verticals": ["dealer"],
        "docs": [
            banks(),
            deepcopy(P_AND_L),
            deepcopy(LICENSE),
            deepcopy(INVENTORY),
            deepcopy(DEBT),
            document(
                "dealer_fi_production",
                "F&I / warranty production",
                "Verify dealership-specific production and its sustainable contribution to cash flow.",
            ),
        ],
    },
    "dealer_real_estate_capital": {
        "name": "Real-Estate-Backed Dealer Capital",
        "slug": "dealer-real-estate-backed",
        "verticals": ["dealer"],
        "rules": [rule("declared_collateral", True, "eq")],
        "docs": standard_docs()
        + [
            document(
                "dealer_property",
                "Collateral ownership, mortgage and occupancy",
                "Verify property ownership, liens and income or occupancy support.",
                category="property_data",
            ),
            deepcopy(LICENSE),
            deepcopy(INVENTORY),
        ],
    },
    "mca_refinance": {
        "name": "MCA Refinance",
        "path": "/mca-refinance",
        "additional_paths": ["/programs/mca-refinance"],
        "verticals": ["dealer", "main_street", "mca"],
        "rules": [
            rule("requested_amount", 0, "gt"),
            rule("requested_amount", MAX_AMOUNT, "lte"),
            rule("active_mca_count", 0, "gt"),
            rule("active_mca_count", MAX_ACTIVE_MCAS, "lte"),
        ],
        "docs": [
            banks(),
            document(
                "ytd_profit_and_loss",
                "Current-year P&L",
                "The full MCA program page requests current-year P&L. Reconcile entity, period, earnings and recurring obligations to the bank statements; do not add a balance-sheet requirement on the basis of this source alone.",
            ),
            deepcopy(DEBT),
            document(
                "mca_agreements_payoffs",
                "All advance agreements and current payoffs",
                "Verify each active position, remittance, balance and payoff. Compare the proposed replacement payment and total cost against cash flow. This is not an SBA product.",
            ),
            document(
                "business_license",
                "Dealer license, if applicable",
                "Collect a current dealer license when the business is a dealership. This is not a mandatory dealer-license upload for unrelated Main Street businesses; staff must resolve applicability in the associated review.",
                category="compliance",
                level="recommended",
            ),
            document(
                "mca_credit_and_applicability",
                "Authorized credit and applicable-document review",
                "Record the authorized soft-credit review through the approved consent process; the source does not state a minimum FICO. Confirm whether the borrower is a dealer and, when applicable, require and verify the current dealer license before completing this review. Record not applicable for non-dealers rather than requesting an unrelated license.",
                category="credit",
                checks=[
                    check(
                        "custom_mca_license_applicability",
                        "Verify required dealer license when applicable",
                        "Staff must identify whether this is a dealership. If so, a current dealer license is required; missing or invalid evidence blocks this review. Otherwise record the reason it is not applicable.",
                        "block",
                    )
                ],
            ),
        ],
        "notes": [
            "Review: QC MCA refinance policy confirmed October 26, 2026: maximum $350,000, terms up to 120 months (10 years), and no more than two active MCAs for automatic review. These ceilings are not a financing approval or a guaranteed term.",
            "Review: Applications with more than two active MCAs are accepted for manual desk review. Missing count or requested amount cannot pass automatic eligibility; do not assume one position from a document upload. Amounts above $350,000 or requested terms above 120 months also require manual review outside the standard program.",
            "Website: this is a restructuring / refinance workflow, not an SBA program. Review current obligations and actual proposed terms.",
            "Website: the dedicated MCA page describes the initial review as six months of statements, an authorized soft-credit pull and current advance terms. The full program page additionally requests current-year P&L, a debt schedule and a dealer license only when applicable. Do not describe the fuller package as the initial three-item review.",
        ],
    },
    "floorplan_support": {
        "name": "Floorplan Support",
        "slug": "floorplan-support",
        "verticals": ["dealer"],
        "rules": [rule("floorplan_inventory_present", True, "eq")],
        "docs": [
            deepcopy(INVENTORY),
            document(
                "floorplan_statements",
                "Last 6 months floorplan statements",
                "Reconcile borrowing base, curtailments, payoffs and audit exceptions.",
            ),
            banks(),
            deepcopy(P_AND_L),
        ],
    },
    "revenue_based_financing": {
        "name": "Revenue-Based Financing",
        "path": "/revenue-based-financing",
        "verticals": ["dealer", "main_street"],
        "rules": [rule("business_age_years", 5), rule("credit_score", 650)],
        "docs": [
            banks(
                checks=[
                    check(
                        "custom_monthly_deposits",
                        "Average monthly deposits at least $1M",
                        "Website criterion: calculate average qualifying monthly deposits from all six official statement months. This is a monthly bank-deposit test, not annual revenue.",
                        "block",
                    )
                ]
            ),
            taxes(1, patterns=False),
            deepcopy(DEBT),
            {**deepcopy(ID), "blocks_stage": "underwriting"},
            document(
                "rbf_positions",
                "Position and risk review",
                "QC's page allows first or second position, not third position, open bankruptcy, an outstanding confession of judgment, or funding recurring operating losses. Verify with current documents and approved lender policy.",
                category="compliance",
            ),
        ],
        "notes": [
            "Website: five operating years, 650 guarantor FICO and average monthly deposits of at least $1M are published working parameters. Position and deposit tests require staff review."
        ],
    },
    "ez_term": {
        "name": "EZ Term",
        "path": "/ez-term-loan",
        "verticals": ["main_street"],
        "rules": [
            rule("business_age_years", 2),
            rule("credit_score", 660),
            rule("annual_revenue", 50000),
        ],
        "excluded": ["4411", "4412", "4413", "4452", "4561", "48", "49", "6215", "6216", "7223"],
        "docs": [
            banks(
                3,
                [
                    check(
                        "positive_ending_balance",
                        "Three positive month-end balances",
                        "Website criterion: check all three official monthly ending balances, not screenshots or transaction exports.",
                        "block",
                    )
                ],
            ),
            deepcopy(USE),
            document(
                "refinance_debt_schedule",
                "Debt schedule if refinancing",
                "Required for a refinance; not an automatic extra working-capital application requirement.",
                level="recommended",
            ),
            taxes(1, patterns=False, stage="closing"),
            deepcopy(ID),
            document(
                "ez_bank_verification_payoffs",
                "ACH / voided check and applicable payoff letters",
                "At closing, verify the business account using a voided check or ACH form and complete the lender's bank-verification process. For each obligation being refinanced, obtain and reconcile a current payoff letter; otherwise record that payoffs are not applicable.",
                stage="closing",
            ),
            document(
                "ez_mca_and_use",
                "Guarantors, existing MCA and proceeds review",
                "Verify every guarantor meets the credit requirement, no more than one existing MCA, business-purpose uses and no titled-asset inventory purchase. Review the current lender's legal, lien, tax and geographic restrictions; do not infer them from NAICS alone.",
                category="compliance",
                checks=[
                    check(
                        "custom_ez_refinance_schedule",
                        "Require a debt schedule for refinance uses",
                        "Staff must confirm the requested use. If any debt will be refinanced, obtain and reconcile the debt schedule before completing this review. Missing refinance debt information blocks review; for new working capital with no refinance, record not applicable.",
                        "block",
                    )
                ],
            ),
        ],
        "notes": [
            "Website: two operating years, 660 FICO per guarantor, $50K annual revenue, three positive statement endings and at most one MCA. Taxes and identity are closing-stage documents.",
            "Review: the website uses legacy NAICS 4461 for health/personal-care stores; this baseline maps that activity to NAICS 2022 prefix 4561. Other exclusions and geography-specific restrictions require review.",
        ],
    },
    "microcap": {
        "name": "MicroCap",
        "path": "/micro-loan",
        "verticals": ["main_street"],
        "rules": [rule("business_age_years", 2), rule("credit_score", 660), rule("dscr", 1.1)],
        "excluded": ["4411", "441210", "441222", "48", "49", "7225"],
        "docs": [
            banks(
                3,
                [
                    check(
                        "positive_ending_balance",
                        "Positive adjusted endings for all three months",
                        "Website criterion: reconcile adjusted ending balances for each complete month.",
                        "block",
                    ),
                    check(
                        "custom_micro_bank_tolerance",
                        "Check NSF and negative-day limits",
                        "Website criterion: across the three-month screen, no more than two NSF items and no more than five negative-balance days. Missing daily data requires review.",
                        "block",
                    ),
                ],
            ),
            taxes(1, patterns=False),
            document(
                "use_of_proceeds",
                "Working-capital-only use of proceeds",
                "Verify a supported working-capital use and reconcile the request to its assumptions. The MicroCap website excludes equipment, real-estate purchases and refinancing another loan; existing debt does not make refinance an eligible use.",
                category="borrower_info",
                checks=[
                    check(
                        "custom_micro_working_capital_only",
                        "Working capital only; no equipment, real estate or refinance",
                        "Review each proposed use. Block completion if any amount funds equipment, real estate, or debt refinancing, or if the purpose is too ambiguous to verify.",
                        "block",
                    )
                ],
            ),
            document(
                "micro_structure",
                "MicroCap ownership, liens and debt seasoning",
                "Verify no more than five individual owners, no more than four active UCCs, at most two counted MCA/SBA balances with more than 90 days seasoning, and the website's excluded EIDL/PPP/504 balances. Requested funds cannot exceed half annualized sales. Staff must verify all source calculations.",
                category="compliance",
            ),
            document(
                "micro_commitment_package",
                "SBA commitment package and staff review",
                "After the commitment letter, collect and verify the current applicable SBA forms, owner PFS, debt schedule, projections, financial certification and ACH form. These are commitment-stage documents, not additional initial application uploads. Confirm package completeness before final underwriting clearance; do not defer collection until closing.",
                stage="underwriting",
            ),
            document(
                "micro_closing_package",
                "Remaining SBA closing documents",
                "At closing, verify the second year of business returns, required personal returns, guarantor IDs, entity and license documents, bank verification and tax transcripts. Affiliate businesses may require two business-tax years. Reuse the already verified commitment package rather than requesting it again.",
                stage="closing",
            ),
        ],
        "notes": [
            "Website: two operating years, 660 FICO per guarantor and 1.10 DSCR. Detailed bank, ownership, UCC, seasoning and sizing limits require review.",
            "Website: working-capital-only use is distinct from existing MCA exposure. The page permits some seasoned obligations; do not turn that into a blanket no-MCA rule.",
            "Review: the page's dealer, transportation and restaurant exclusions are product/lender restrictions, not a blanket SBA industry prohibition.",
            "Proposed QC policy: the commitment package is a separate underwriting-verification milestone, collected after a commitment rather than at initial intake; the remaining closing package blocks closing. Confirm that these workflow milestones match the lender's process before publishing.",
        ],
    },
    "line_of_credit": {
        "name": "Lines of Credit",
        "slug": "line-of-credit",
        "verticals": ["main_street"],
        "docs": standard_docs()
        + [
            {
                **document(
                    "loc_need",
                    "Staff review of revolving working-capital need",
                    "Proposed QC review check: use the collected financials and conversation to review revenue consistency, cash-flow timing, intended draw/repayment cycle and the amount genuinely needed. This is a staff review record, not a separately published website upload requirement.",
                ),
                "visibility": ["agent", "underwriter"],
            }
        ],
    },
    "equipment_financing": {
        "name": "Equipment Financing",
        "slug": "equipment-financing",
        "verticals": ["main_street", "dealer"],
        "rules": [
            rule("equipment_financing_intent", True, "eq"),
            rule("business_age_years", 1),
            rule("annual_revenue", 100_000),
            rule("credit_score", 640),
            rule("cash_flow", 0, "gt"),
            rule("requested_amount", 25_000),
            rule("requested_amount", 2_000_000, "lte"),
        ],
        "docs": standard_docs()
        + [
            document(
                "equipment_quote",
                "Vendor quote or invoice for the equipment",
                "Obtain the current quote or invoice. Confirm the seller, equipment description, purchase price, delivery timing, intended business use and any existing liens. Missing or unclear details require follow-up rather than an automatic rejection.",
                checks=[
                    check(
                        "custom_equipment_identity",
                        "Identify the equipment",
                        "Record the equipment type, make/model or other identifying detail, and whether it is new, used or refurbished.",
                        "block",
                    ),
                    check(
                        "custom_equipment_seller_and_price",
                        "Verify seller and purchase price",
                        "Record the seller identity and contact information and reconcile the stated purchase price to the quote or invoice.",
                        "block",
                    ),
                    check(
                        "custom_equipment_delivery",
                        "Confirm expected delivery",
                        "Record the expected delivery timing and flag any timing or availability uncertainty for staff review.",
                    ),
                ],
            ),
            document(
                "equipment_transaction_details",
                "Equipment and transaction details",
                "Collect the equipment description, age, condition, remaining useful life, seller details, purchase price, delivery timing and intended business use. Unknown details remain open questions and do not become zero or failed values.",
                checks=[
                    check(
                        "custom_equipment_useful_life",
                        "Review age, condition and useful life",
                        "Compare the equipment's age, condition and remaining useful life with the requested financing term. Escalate an unsupported or unclear useful-life assumption.",
                        "block",
                    ),
                    check(
                        "custom_equipment_business_use",
                        "Confirm business purpose",
                        "Document how the equipment will be used in the business and confirm the request is for a business purpose.",
                        "block",
                    ),
                ],
                category="borrower_info",
            ),
        ],
        "notes": [
            "Website: QC's published Equipment Financing range is $25,000 to $2,000,000 with terms from 24 to 84 months.",
            "Proposed QC policy: the preliminary screen requires an equipment need, one year in business, $100,000 annual revenue, estimated 640 credit, positive cash flow and a request within the published range. Missing facts mean more information is needed, not an automatic rejection.",
            "Review: lender pricing, down payment, private-party eligibility and approval timing remain file-specific and must not be inferred from this baseline.",
        ],
    },
    "jumbo_term": {
        "name": "Jumbo Term",
        "slug": "jumbo-term-loan",
        "verticals": ["main_street"],
        "docs": standard_docs()
        + [
            document(
                "jumbo_project",
                "Project / acquisition and sponsor support",
                "Review complete project costs, sponsor experience, collateral schedule, cash flow and repayment structure.",
            )
        ],
    },
    "hybrid_term_loc": {
        "name": "Hybrid Term / LOC",
        "slug": "hybrid-term-loc",
        "verticals": ["main_street"],
        "docs": standard_docs()
        + [
            {
                **document(
                    "hybrid_uses",
                    "Staff review of term versus revolving capital uses",
                    "Proposed QC review check: use the collected financials and use-of-proceeds information to separate one-time fixed capital from recurring working-capital needs and reconcile exposure, repayment capacity and liquidity. This is a staff review record, not a separately published website upload requirement.",
                ),
                "visibility": ["agent", "underwriter"],
            }
        ],
    },
    "transportation_finance": {
        "name": "Transportation Finance",
        "slug": "transportation-finance",
        "verticals": ["main_street"],
        "include": ["48", "49"],
        "docs": standard_docs()
        + [
            document(
                "fleet_schedule",
                "Fleet, VINs, liens and payoffs",
                "Review vehicles, age, mileage, ownership, values and liens.",
            ),
            document(
                "transport_authority",
                "Operating authority and insurance",
                "Confirm active authority and adequate insurance for the actual operations.",
                category="compliance",
            ),
        ],
    },
    "sba_grocery": {
        "name": "SBA Grocery",
        "slug": "sba-grocery",
        "verticals": ["main_street"],
        "docs": standard_docs()
        + [
            deepcopy(USE),
            document(
                "grocery_activity",
                "Grocery / food activity and premises",
                "Verify grocery, food distribution or qualifying agriculture activity, premises lease/purchase and owner background. Confirm the actual SBA variant and lender screen; this marketing label is not a separate statutory SBA program.",
                category="borrower_info",
            ),
        ],
    },
    "sba_made_in_america": {
        "name": "SBA Made in America",
        "slug": "sba-made-in-america",
        "verticals": ["main_street"],
        "include": ["31", "32", "33"],
        "docs": standard_docs()
        + [
            deepcopy(QUOTE),
            deepcopy(USE),
            document(
                "domestic_production",
                "US manufacturing activity and facility plans",
                "Verify actual domestic manufacturing and intended equipment/facility uses. Confirm the SBA variant and current lender screen; the marketing name alone is not a separate approval standard.",
                category="borrower_info",
            ),
        ],
    },
    "foreclosure_bailout": {
        "name": "Commercial Foreclosure Bailout",
        "path": "/foreclosure-bailout",
        "verticals": ["real_estate"],
        "docs": [
            document(
                "rescue_payoff",
                "Payoff / default notice",
                "Verify exact payoff, deadlines and current lien position.",
                category="property_data",
            ),
            document(
                "rescue_occupancy",
                "Rent roll, occupancy and T12",
                "Confirm commercial/investment property use; primary residences do not fit the website program.",
                category="property_data",
            ),
            document(
                "rescue_court",
                "Court, sale and bankruptcy documents",
                "Have qualified staff review legal deadlines and restrictions. Do not promise a closing before legal/title clearance.",
                category="compliance",
            ),
            document(
                "rescue_closing",
                "Valuation, title, entity, taxes and insurance",
                "Verify valuation support and resolve all closing conditions.",
                category="property_data",
                stage="closing",
            ),
        ],
    },
    # Transcribed from docs/program_sources/quidity_direct_10_year_term_loan_2026-10.pdf
    # (Quidity Direct program guidelines, October 2026; NEWITY LLC is the lender
    # service provider). Every threshold below is the guide's own. Eligibility
    # rules use only what the file already holds — tax return, bank statements,
    # debt schedule, stated facts. The guide's credit, residency, legal and
    # IRS tests are deliberately review items, not fit gates: the owner's
    # process runs the soft credit pull last, and the screen must be able to
    # say "fits" before it. Loan sizing is a staff calculation on the file,
    # written out here so the AI can reason about it.
    "quidity_10_year_term": {
        "name": "10-Year Term Loan",
        "public_slug": "ten-year-term-loan",
        "short_description": (
            "Working capital and payoff of high-interest business debt, including "
            "merchant cash advances, from $50,000 to $350,000 over ten years."
        ),
        "sources": [QUIDITY_SOURCE],
        "source_note": (
            "Lender guideline: Quidity Direct 10 Year Term Loan Program Guidelines, "
            "October 2026, consolidating the September 2026 NEWITY Growth Term Loan "
            "criteria and the October 21, 2026 partner briefing. All terms are subject "
            "to lender confirmation; nothing here is an approval."
        ),
        "description": (
            "Quidity Direct 10 Year Term Loan (lender service provider: NEWITY LLC). "
            "$50,000 to $350,000, 120 months, target rate 14-20% (higher by risk), "
            "5.5% program fee deducted from proceeds, closing 2-4 weeks after an "
            "acceptable file. All 50 states; Puerto Rico excluded. USES: working "
            "capital, and payoff of high-interest business debt including MCAs; any "
            "active MCA must be refinanced in the new loan (up to 2 MCAs, up to 7 "
            "debts); not for business acquisitions or real-estate development, "
            "investment or real-estate-sector businesses. MINIMUMS: 2 years in "
            "business (4 for construction); at least $175,000 revenue and positive "
            "EBITDA on the most recently filed business tax return; soft-pull Equifax "
            "FICO 9 of 680+ for every 20%+ owner (hard minimum, checked last, "
            "recheck after 30 days); guarantors U.S. citizens or lawful permanent "
            "residents and at least 51% U.S. ownership; U.S. operations with "
            "majority domestic income. SIZING: Fast Track up to $200,000 = 40% of "
            "tax-return revenue minus current business debt, no DSCR test, blanket "
            "business-assets UCC up to third position. Cash-flow pathway up to "
            "$350,000 = the lesser of 40% of revenue or 60% of revenue minus current "
            "business debt, minimum 1.10x DSCR, blanket UCC up to fourth position. "
            "Equipment-specific UCC filings do not count as blanket positions. BANK "
            "STATEMENTS (trailing six months): no more than 5 NSF/overdraft items; "
            "annualized qualifying deposits not more than 25% below the tax-return "
            "revenue; average ending balance above 3x the proposed monthly payment. "
            "SECURITY: blanket UCC on all business assets; personal guarantee from "
            "every 20%+ owner; no separately pledged personal collateral. RISK: no "
            "financial-crime felony for any guarantor and no felony within 3 years; "
            "all bankruptcies discharged and none filed within 5 years; aggregate IRS "
            "balances at or below $100,000; no active judgments or liens within 5 "
            "years (program review); no collection litigation active or filed within "
            "3 years. SUBMISSION: most recently filed business tax return, trailing "
            "six months of business bank statements, and a current business debt "
            "schedule including MCA payoff details."
        ),
        "verticals": ["main_street", "mca"],
        # Fit gates are facts, never document counts. In this engine a fact
        # that is not known yet reads as "needs information", which is what a
        # file that has not uploaded its return should look like; a count of
        # zero statements reads as a failure, which is not. The filed return
        # and the six months of statements are therefore requirement rows
        # with review checks, and the gates below are the guide's numbers
        # applied to what those documents yield.
        "rules": [
            rule("business_age_years", 2),
            # Four years for construction. `industry_key` is the main-street
            # slug on an intake-sourced file and the NAICS sector label on a
            # classified one, so both spellings are matched.
            {
                "any": [
                    {"not": rule("industry_key", ["construction_trades", "Construction"], "in")},
                    rule("business_age_years", 4),
                ]
            },
            rule("annual_revenue", 175000),
            rule("cash_flow", 0, "gt"),
            rule("nsf_or_overdraft_count", 5, "lte"),
            rule("requested_amount", 50000),
            rule("requested_amount", 350000, "lte"),
        ],
        "recommendation_preferences": [
            {
                "key": "mca_payoff",
                "label": "Active MCA obligations are refinanced by this program",
                "when": rule("mca_obligations_present", True, "eq"),
                "score": 30,
            },
            {
                "key": "refinance_intent",
                "label": "Stated intent is refinancing business debt",
                "when": rule("intent", "refinance_debt", "eq"),
                "score": 15,
            },
            {
                "key": "dscr_pathway",
                "label": "DSCR at or above 1.10x opens the $350,000 cash-flow pathway",
                "when": rule("dscr", 1.10),
                "score": 20,
            },
        ],
        # Real estate (531) and land subdivision (2372): the guide excludes
        # real-estate development, investment and real-estate-sector businesses.
        "excluded": ["531", "2372"],
        "docs": [
            taxes(
                1,
                patterns=False,
                stage="underwriting",
            )
            | {
                "label": "Most recently filed business tax return",
                "completion_criteria": (
                    "Obtain the most recently filed complete business return with schedules. "
                    "Revenue and EBITDA for this program are read from this return, not from "
                    "bank deposits or management accounts."
                ),
                "review_checks": [
                    check(
                        "custom_quidity_revenue_floor",
                        "Revenue of at least $175,000 on the filed return",
                        "Read gross receipts from the most recently filed business return. Below $175,000 the file does not meet the program minimum.",
                        "block",
                    ),
                    check(
                        "custom_quidity_positive_ebitda",
                        "Positive EBITDA on the filed return",
                        "Compute EBITDA from the filed return (net income plus interest, taxes, depreciation and amortization shown on the return). It must be above zero.",
                        "block",
                    ),
                ],
            },
            banks(
                6,
                [
                    check(
                        "custom_quidity_nsf_limit",
                        "No more than 5 NSF or overdraft items across six months",
                        "Count every non-sufficient-funds and overdraft item on the six official statements. Six or more fails the program screen.",
                        "block",
                    ),
                    check(
                        "custom_quidity_deposit_consistency",
                        "Annualized deposits within 25% of tax-return revenue",
                        "Annualize qualifying deposits from the six statements and compare with gross receipts on the filed return. A decline greater than 25% fails the screen.",
                        "block",
                    ),
                    check(
                        "custom_quidity_liquidity",
                        "Average ending balance above 3x the proposed monthly payment",
                        "Average the six month-end balances and compare with the proposed monthly payment from the loan sizing. The average must exceed three times the payment.",
                        "block",
                    ),
                ],
            ),
            deepcopy(DEBT)
            | {
                "label": "Current business debt schedule with MCA payoff details",
                "review_checks": [
                    check(
                        "custom_quidity_mca_payoff",
                        "Every active MCA is refinanced in the new loan",
                        "List each active merchant cash advance with funder, remaining payback and payoff amount. All of them must be paid off by this loan; the schedule must carry payoff details.",
                        "block",
                    ),
                    check(
                        "custom_quidity_refinance_limits",
                        "No more than 2 MCAs and 7 total debts refinanced",
                        "Count the obligations being refinanced. More than two MCAs or more than seven total debts is outside the program.",
                        "block",
                    ),
                    check(
                        "custom_quidity_ucc_positions",
                        "Blanket UCC position within the pathway limit",
                        "From the schedule and any UCC search, count blanket all-business-assets liens. Fast Track allows up to third position, the cash-flow pathway up to fourth. Equipment-specific UCC filings do not count.",
                        "review",
                    ),
                ],
            },
            deepcopy(USE)
            | {
                "completion_criteria": (
                    "Eligible uses are general working capital and payoff of high-interest "
                    "business debt including MCAs. Business acquisitions and real-estate "
                    "development, investment or real-estate-sector uses are not permitted."
                ),
                "review_checks": [
                    check(
                        "custom_quidity_eligible_use",
                        "Use of funds is working capital or eligible debt payoff",
                        "Confirm the stated use. An acquisition or a real-estate use fails the program regardless of the financials.",
                        "block",
                    )
                ],
            },
            document(
                "quidity_sizing_pathways",
                "Loan sizing: Fast Track and cash-flow pathways",
                "Calculate both pathways from the filed return and the debt schedule. Fast Track (up to $200,000): 40% of annual tax-return revenue minus current business debt, no DSCR test. Cash-flow (up to $350,000): the lesser of 40% of revenue or 60% of revenue minus current business debt, with DSCR of at least 1.10x. Record both results and which pathway the requested amount fits. Sizing is indicative; the lender's underwriting sets the final amount.",
                category="financials",
            )
            | {"requirement_kind": "human_review", "completion_mode": "requires_human_verify"},
            document(
                "quidity_eligibility_review",
                "Program eligibility: geography, ownership, operations and legal standing",
                "Confirm: business located in one of the 50 states (Puerto Rico excluded); every guarantor a U.S. citizen or lawful permanent resident and at least 51% of ownership held by such persons; U.S.-based operations and owners with a majority of income domestic; no financial-crime felony for any guarantor and no felony within 3 years; all bankruptcies discharged and none filed within 5 years; aggregate IRS balances at or below $100,000; no active judgments or liens within 5 years; no collection litigation active or filed within 3 years. Other active litigation is reviewed case by case.",
                category="compliance",
                waivable=False,
                checks=[
                    check(
                        "custom_quidity_legal_standing",
                        "Legal, tax and residency standing confirmed",
                        "Each item above is confirmed from documents or a signed attestation, not inferred. Any failure is outside the program.",
                        "block",
                    )
                ],
            ),
            document(
                "guarantor_credit_review",
                "Soft credit pull: 680+ Equifax FICO 9 for every 20%+ owner",
                "Collected last. Run the soft pull for every owner at 20% or more only once the file otherwise fits; a result under 680 is a hard program minimum. A declined owner may be rechecked after 30 days. Before the pull, treat credit as pending, never as a failure.",
                category="credit",
                stage="closing",
                checks=[
                    check(
                        "custom_quidity_credit_floor",
                        "Every 20%+ owner at 680 or above",
                        "Record each qualifying owner's Equifax FICO 9 from the soft pull. Any owner below 680 fails the program.",
                        "block",
                    )
                ],
            ),
            deepcopy(P_AND_L) | {"required_level": "recommended"},
        ],
        "notes": [
            "Review: the credit, residency, legal and IRS tests are review items on purpose. The fit rules use only tangible file evidence so the screen can say a file fits before the soft credit pull, which the desk runs last.",
            "Review: the guide's revenue and EBITDA tests read the most recently filed business tax return. Revenue and EBITDA are fit gates; the return itself and the six months of statements are requirement rows, because a document the file has not received yet must read as a gap, not as a failed screen.",
            "Review: the 25% deposit-consistency and 3x liquidity tests compare two figures on the file and are review checks on the bank statements, not fit rules; the engine compares a field with a constant only.",
            "Review: Puerto Rico and the acquisition exclusion cannot be expressed as a NAICS prefix; both are compliance review checks.",
        ],
    },
}


def suggested_baseline(
    program_key: str, *, name: str | None = None, current_scopes: list[dict] | None = None
) -> dict[str, Any]:
    """Return a new detached suggestion, including a safe custom-product fallback."""
    spec = deepcopy(CATALOG.get(program_key, {}))
    known = bool(spec)
    program_name = name or spec.get("name") or program_key.replace("_", " ").title()
    # A spec sourced from a lender's own guideline document names its sources
    # outright (a repository path or URL) instead of deriving a site page.
    if spec.get("sources"):
        sources = list(spec["sources"])
    else:
        sources = (
            [SITE + spec.get("path", f"/programs/{spec.get('slug', program_key.replace('_', '-'))}")]
            if known
            else [SITE]
        )
    sources.extend(SITE + path for path in spec.get("additional_paths", []))
    notes = list(spec.get("notes", []))
    if known:
        notes.insert(
            0,
            spec.get("source_note")
            or "Website: source snapshot reviewed on 2026-09-27. Published marketing ranges and closing estimates are not guaranteed terms or automatic eligibility limits.",
        )
    else:
        notes.append(
            "Review: no verified QC product-page mapping exists for this custom program. This is a document-review starter, not sourced product eligibility. Supply approved program facts before publishing."
        )
    notes.append(
        "Proposed QC policy: exclude banking / depository credit intermediation (5221) and insurance including agencies (524). This is your proposed broader house restriction, not a statement that every such business is SBA-ineligible."
    )
    notes.append(
        "Review: selected NAICS prefixes refer to the 2022 US classification. A prefix excludes all descendant activities. Other legal/program exclusions cannot all be represented accurately by a NAICS code."
    )
    notes.append(
        "Proposed QC policy: qualitative underwriting prompts expand the website's descriptions into staff review records, not an assertion that every prompt is a separate borrower upload. Unless the source specifies a stage, the draft uses underwriting verification; confirm collection timing and conditional applicability before publishing. A document waiver cannot waive legal or program eligibility."
    )
    requirements = deepcopy(spec.get("docs", standard_docs()))
    if any(
        row.get("review_checks")
        and any(item["key"].startswith("net_income") for item in row["review_checks"])
        for row in requirements
    ):
        notes.append(
            "Proposed QC policy: the requested no-negative-earnings and no-downtrend tests are explicit tax-document review checks. They are not claimed as universal lender or SBA rules."
        )
    if program_key.startswith("sba_") or program_key == "microcap":
        sources.append(SBA_SOURCE)
        if program_key in {"sba_7a", "sba_504", "sba_express"}:
            sources.append(SITE + "/sba")
        requirements.append(
            document(
                "sba_eligibility_and_proceeds",
                "SBA eligibility, current rules and MCA use",
                "Confirm eligible operating activity and legal eligibility using the applicable current SBA/lender policy, not NAICS alone. Separate outstanding MCA exposure from using proceeds to refinance an MCA. Review the debt schedule, agreements and intended uses; do not assume that existing exposure is a universal borrower prohibition. SBA SOP changes effective October 1, 2026 require a fresh policy review.",
                checks=[
                    check(
                        "custom_sba_mca_review",
                        "Resolve MCA exposure and permitted use of proceeds",
                        "Staff must document the applicable program, current effective policy, proposed use, outstanding MCA balances and any lender-specific exclusions. No automatic pass from a blank debt field.",
                        "block",
                    )
                ],
                category="compliance",
                waivable=False,
            )
        )
        notes.append(
            "Review: confirm the effective SBA SOP and the lender's requirements before approval. This snapshot predates October 1, 2026 policy changes. A blanket ban on all existing MCA exposure has not been assumed."
        )
        notes.append(
            "Review: QC's https://qualifiedcommercial.com/sba/auto currently suggests advances may be refinanced based on payment benefit alone. Do not use that marketing statement as an eligibility rule: SOP 50 10 8 prohibits MCA/factoring refinancing. Effective October 1, 2026, SOP 8.1 retains the active-MCA prohibition and introduces conditions for former sales-based agreements converted to term loans with at least 24 months of amortization and no subsequent agreements. Verify the policy effective for the loan and all additional conditions; existing MCA exposure is not itself the same as an MCA refinance use."
        )
    sources.append(CENSUS_SOURCE)
    criteria = spec.get("rules", [])
    rules: dict[str, Any] = {"fit": {"all": criteria}} if criteria else {}
    if program_key == "mca_refinance":
        # The term is an offer ceiling, not a fabricated requested term. The
        # intrinsic gate applies it whenever an actual term is present.
        rules["intrinsic_limits"] = {"maximum_amount": MAX_AMOUNT, "maximum_term_months": MAX_TERM_MONTHS,
                                    "maximum_active_mcas": MAX_ACTIVE_MCAS, "outside_limits_route": "manual_review"}
    if spec.get("recommendation_preferences"):
        rules["recommendation_preferences"] = deepcopy(spec["recommendation_preferences"])
    if not criteria:
        notes.append(
            "Review: the source page does not state sufficient hard fit thresholds. Document traits are prefilled, but add your approved eligibility checks before publishing; no threshold has been invented."
        )
    elif program_key in {
        "sba_7a",
        "sba_504",
        "sba_express",
        "ez_term",
        "microcap",
        "revenue_based_financing",
    }:
        requirements.append(
            document(
                "guarantor_credit_review",
                "Credit requirements for all required guarantors",
                "The automated file-level score is preliminary. Confirm that every required guarantor meets the applicable program's reviewed credit threshold using authorized credit evidence.",
                category="credit",
            )
        )
    for index, row in enumerate(requirements):
        row["display_order"] = index
    excludes = list(dict.fromkeys(["5221", "524", *spec.get("excluded", [])]))
    if known:
        scopes = [
            {
                "vertical": vertical,
                "scope_key": "default",
                "intake_variants": [],
                "intent_keys": [],
                "naics_prefixes": list(spec.get("include", [])),
                "excluded_naics_prefixes": excludes,
                "industry_keys": [],
                "required_fact_keys": [],
            }
            for vertical in spec["verticals"]
        ]
    else:
        scopes = deepcopy(
            current_scopes
            or [
                {
                    "vertical": "main_street",
                    "scope_key": "default",
                    "intake_variants": [],
                    "intent_keys": [],
                    "naics_prefixes": [],
                    "industry_keys": [],
                    "required_fact_keys": [],
                }
            ]
        )
        for scope in scopes:
            scope["excluded_naics_prefixes"] = list(
                dict.fromkeys([*scope.get("excluded_naics_prefixes", []), *excludes])
            )
    rules["baseline"] = {"version": BASELINE_VERSION, "source_urls": sources, "source_notes": notes}
    return {
        "program_key": program_key,
        "name": program_name,
        "version": BASELINE_VERSION,
        "source_urls": sources,
        "source_notes": notes,
        "rules": rules,
        "requirements": requirements,
        "scopes": scopes,
        "needs_review": True,
        # The program's own words for the underwriting AI, when the spec has
        # them; a draft made from this baseline carries it as its description.
        "description": spec.get("description"),
    }
