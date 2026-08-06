"""One-time patch: converts the flat-paragraph fee/registry/specimen-form
sections in all 5 contracts into real {heading, columns, rows} tables (see
ContractSection's columns/rows fields in contract_templates.py). Every table
in the source contracts (Schedule A/B/C-equivalent fee schedules, capital-
source registries, and Exhibit 1's Field/Detail rows) was mechanically
transcribed by build_contract_templates.py as one paragraph per column
header / cell, with no row grouping -- this reshapes those exact paragraphs
into columns + rows using the already-transcribed text verbatim (no wording
changes), so the frontend can render an actual <table> instead of a wall of
unrelated-looking single lines.

Schedule A's own disclosure-table sections (the signer-submitted capital-
relationship rows) are NOT touched here -- see
patch_schedule_a_disclosure_fields.py, since those need `disclosure_field`
wiring to a real input, not a static rows conversion.

Usage: python scripts/patch_table_sections.py
Edits app/services/contract_templates_data.py in place.
"""

from __future__ import annotations

import importlib.util
import pprint
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "app" / "services" / "contract_templates_data.py"


def _find_section(sections: list[dict], heading: str) -> dict:
    for s in sections:
        if s["heading"] == heading:
            return s
    raise KeyError(heading)


def patch_referral_protection(data: dict) -> None:
    sections = data["referral_protection"]["sections"]

    # Schedule B Part 1 -- Standard Success Fee Ranges by Product. 4 columns
    # x 10 rows (9 named products + "Other"), no intro prose before the
    # table (the section's own paragraphs[0]/[1] are prose, kept), 2 trailing
    # prose paragraphs (program-capped note + retainer/milestone note).
    sec = _find_section(sections, "SCHEDULE Part 1 — Standard Success Fee Ranges by Product")
    intro = sec["paragraphs"][:1]
    trailing = sec["paragraphs"][-2:]
    columns = ["Product / Program", "Standard Fee Range", "Retainer", "Paid By"]
    rows = [
        ["SBA 7(a) / Express (program-capped)", "1% – 3%", "$schedule_b_retainer_sba_7a_express", "Lender"],
        ["SBA 504 (program-capped)", "1% – 3%", "$schedule_b_retainer_sba_504", "Lender"],
        ["USDA B&I (program-capped)", "1% – 3%", "$schedule_b_retainer_usda_bi", "Lender"],
        ["Commercial Real Estate / DSCR", "3.5% – 5%", "$schedule_b_retainer_cre_dscr", "Lender"],
        ["Dealer Floorplan / Dealer LOC", "3% – 5%", "$schedule_b_retainer_dealer_floorplan", "☐ Lender ☐ Client"],
        ["Warranty / Reinsurance Receivable", "3.5% – 5%", "$schedule_b_retainer_warranty_reinsurance", "☐ Lender ☐ Client"],
        ["Asset-Based Lending", "3% – 5%", "$schedule_b_retainer_asset_based_lending", "☐ Lender ☐ Client"],
        ["Bridge / Private Credit", "5% – 10%", "$schedule_b_retainer_bridge_private_credit", "Client"],
        ["Working Capital / LOC", "3.5% – 5%", "$schedule_b_retainer_working_capital_loc", "Client"],
        [
            "Other: $schedule_b_other_product_name",
            "$schedule_b_other_fee_rate_low – $schedule_b_other_fee_rate_high",
            "$schedule_b_other_retainer",
            "☐ Lender ☐ Client",
        ],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows

    # Schedule B Part 2 -- Advisory and Work-Product Fees. 4 columns x 6 rows.
    sec = _find_section(sections, "SCHEDULE Part 2 — Advisory and Work-Product Fees (earned upon performance per Section 16.5)")
    intro = sec["paragraphs"][:2]
    trailing = sec["paragraphs"][-1:]
    columns = ["Fee Type", "Standard Low", "Standard High", "When Earned"]
    rows = [
        ["Advisory / engagement fee", "$2,500", "$5,000", "Upon engagement (retainer)"],
        ["Underwriting & packaging fee", "$7,500", "$12,500", "Upon submission to Capital Source"],
        ["Financial modeling fee", "$2,000", "$5,000", "Upon delivery of model"],
        ["Due diligence / document prep", "$3,000", "$6,000", "Upon performance"],
        ["Technology fee", "$250", "$500", "Upon engagement"],
        ["Third-party cost reimbursement", "At cost", "At cost", "Appraisal, environmental, title, search, filing"],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows

    # Schedule B Part 3 -- Payment Mechanics. Only clause (g)'s milestone
    # table is tabular; (a)-(f) stay as paragraphs.
    sec = _find_section(sections, "SCHEDULE Part 3 — Payment Mechanics")
    paras = sec["paragraphs"]
    milestone_intro_idx = next(i for i, p in enumerate(paras) if p.startswith("(g)"))
    intro = paras[: milestone_intro_idx + 1]
    trailing = paras[-1:]
    columns = ["Milestone", "Fee Earned", "Cumulative"]
    rows = [
        ["Engagement and file intake", "Advisory + technology fee", "$2,750 – $5,500"],
        ["Financial model delivered", "Financial modeling fee", "$4,750 – $10,500"],
        ["Diligence and document package assembled", "Due diligence / doc prep fee", "$7,750 – $16,500"],
        ["Submission to Capital Source", "Underwriting & packaging fee", "$15,250 – $29,000"],
        ["Initial funding or first advance", "Success fee less amounts earned above", "Per Part 1"],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows

    # Schedule C -- Registry of Protected Capital Sources. 6 columns, no
    # static rows (populated by admins over time via a separate registry
    # feature) -- render an explanatory placeholder row.
    sec = _find_section(sections, "SCHEDULE C")
    intro = sec["paragraphs"][:2]
    columns = ["Reg. No.", "Capital Source", "Program / Division", "Introduced Contact", "Client / Deal", "Date"]
    rows = [["—", "No entries yet.", "", "", "", ""]]
    sec["paragraphs"] = intro
    sec["columns"] = columns
    sec["rows"] = rows

    # Exhibit 1 -- Deal Registration and Introduction Confirmation. Field/
    # Detail rows; Registration Number's detail cell is the real per-deal
    # number (see patch: dynamic issuance happens at render time via the
    # deal_registration_number_prefix/_suffix fields, populated by the
    # deal-registrations feature -- Part 5 of the referral-protection plan).
    sec = _find_section(sections, "EXHIBIT 1")
    intro = sec["paragraphs"][:2]
    columns = ["Field", "Detail"]
    rows = [
        ["Registration Number", "QC-$deal_registration_number_prefix-$deal_registration_number_suffix"],
        ["Date and Time of Introduction", "$deal_registration_introduced_at"],
        ["Referral Partner", "$referral_partner_legal_name"],
        ["Client / Borrower", "$deal_registration_client_borrower"],
        ["Financing Opportunity (type, amount, use of proceeds)", "$deal_registration_financing_opportunity"],
        ["Introduced Capital Source", "$deal_registration_introduced_capital_source"],
        ["Introduced Program / Division", "$deal_registration_introduced_program"],
        ["Introduced Contact (name, title)", "$deal_registration_introduced_contact"],
        ["Method of Introduction", "$deal_registration_method_of_introduction"],
        ["Documents Transmitted", "$deal_registration_documents_transmitted"],
        ["Coded Designation (if staged disclosure)", "$deal_registration_coded_designation"],
        ["Capital Source No.", "$deal_registration_coded_capital_source_number"],
        ["Date Identity Disclosed", "$deal_registration_date_identity_disclosed"],
    ]
    sec["paragraphs"] = intro
    sec["columns"] = columns
    sec["rows"] = rows

    # New scalar fields backing Exhibit 1's rows (the old
    # deal_registration_method_other_description/coded_capital_source_number
    # fields already existed; add the rest, all out-of-scope-for-signing --
    # populated only when an admin issues a Deal Registration, never on the
    # main contract fill form).
    fs = data["referral_protection"]["field_schema"]
    new_exhibit1_fields = {
        "deal_registration_introduced_at": {"label": "Deal Registration — Date/Time of Introduction", "default": ""},
        "deal_registration_client_borrower": {"label": "Deal Registration — Client / Borrower", "default": ""},
        "deal_registration_financing_opportunity": {"label": "Deal Registration — Financing Opportunity", "default": ""},
        "deal_registration_introduced_capital_source": {"label": "Deal Registration — Introduced Capital Source", "default": ""},
        "deal_registration_introduced_program": {"label": "Deal Registration — Introduced Program / Division", "default": ""},
        "deal_registration_introduced_contact": {"label": "Deal Registration — Introduced Contact", "default": ""},
        "deal_registration_method_of_introduction": {"label": "Deal Registration — Method of Introduction", "default": ""},
        "deal_registration_documents_transmitted": {"label": "Deal Registration — Documents Transmitted", "default": ""},
        "deal_registration_coded_designation": {"label": "Deal Registration — Coded Designation", "default": ""},
        "deal_registration_date_identity_disclosed": {"label": "Deal Registration — Date Identity Disclosed", "default": ""},
    }
    for name, info in new_exhibit1_fields.items():
        fs[name] = {**info, "raw_token": "", "field_type": "text", "row_group": None, "in_scope_for_initial_signing": False}


def patch_sba_engagement(data: dict) -> None:
    sections = data["sba_engagement"]["sections"]

    sec = _find_section(sections, "SCHEDULE Part 1 — The Loan Request")
    columns = ["Item", "Detail"]
    rows = [
        ["Client legal name", "$client_legal_name"],
        ["Principals / guarantors", "$loan_request_principals_guarantors"],
        ["Program (7(a) / 504 / USDA B&I / other)", "$loan_request_program"],
        ["Requested amount", "$loan_request_amount"],
        ["Use of proceeds", "$loan_request_use_of_proceeds"],
        ["Target closing date", "$loan_request_target_closing_date"],
    ]
    sec["paragraphs"] = []
    sec["columns"] = columns
    sec["rows"] = rows

    fs = data["sba_engagement"]["field_schema"]
    for name, label in {
        "loan_request_principals_guarantors": "Loan Request — Principals / Guarantors",
        "loan_request_program": "Loan Request — Program",
        "loan_request_amount": "Loan Request — Requested Amount",
        "loan_request_use_of_proceeds": "Loan Request — Use of Proceeds",
        "loan_request_target_closing_date": "Loan Request — Target Closing Date",
    }.items():
        fs[name] = {"label": label, "default": "", "raw_token": "", "field_type": "text", "row_group": None, "in_scope_for_initial_signing": True}

    sec = _find_section(sections, "SCHEDULE Part 2 — Fees for Work Performed (earned upon performance per Section 5.5)")
    intro = sec["paragraphs"][:1]
    trailing = sec["paragraphs"][-1:]
    columns = ["Fee Type", "Standard Low", "Standard High", "Amount for This File"]
    rows = [
        ["Advisory / engagement fee (retainer)", "$2,500", "$5,000", "$retainer_fee_amount"],
        ["Underwriting & packaging fee", "$7,500", "$12,500", "$underwriting_packaging_fee_amount"],
        ["Financial modeling fee", "$2,000", "$5,000", "$financial_modeling_fee_amount"],
        ["Due diligence / document prep", "$3,000", "$6,000", "$due_diligence_doc_prep_fee_amount"],
        ["Technology fee", "$250", "$500", "$technology_fee_amount"],
        ["Third-party cost reimbursement", "At cost", "At cost", "At cost"],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "SCHEDULE Part 3 — Success Fee Component, if any")
    intro = sec["paragraphs"][:1]
    trailing = sec["paragraphs"][-2:]
    columns = ["Program", "Standard Range", "Rate for This File", "Paid By"]
    rows = [
        ["SBA 7(a) / Express", "1% – 3%, subject to SOP cap", "$success_fee_rate_7a_express", "Lender"],
        ["SBA 504", "1% – 3%, subject to SOP cap", "$success_fee_rate_504", "Lender"],
        ["USDA B&I", "1% – 3%, subject to SOP cap", "$success_fee_rate_usda_bi", "Lender"],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "EXHIBIT B")
    intro = sec["paragraphs"][:2]
    columns = ["No.", "Program Lender", "Program / Division", "Contact Introduced", "Date Introduced"]
    rows = [["—", "No entries yet.", "", "", ""]]
    sec["paragraphs"] = intro
    sec["columns"] = columns
    sec["rows"] = rows


def patch_client_engagement(data: dict) -> None:
    sections = data["client_engagement"]["sections"]

    sec = _find_section(sections, "SCHEDULE Part 1 — The Financing Request")
    columns = ["Item", "Detail"]
    rows = [
        ["Client legal name", "$client_legal_name"],
        ["Additional borrower entities", "$financing_request_additional_borrower_entities"],
        ["Principals / guarantors", "$financing_request_principals_guarantors"],
        ["Product / facility type", "$financing_request_product_type"],
        ["Requested amount", "$financing_request_amount"],
        ["Purpose / use of proceeds", "$financing_request_use_of_proceeds"],
        ["Collateral / property", "$financing_request_collateral"],
        ["Target closing date", "$financing_request_target_closing_date"],
    ]
    sec["paragraphs"] = []
    sec["columns"] = columns
    sec["rows"] = rows

    fs = data["client_engagement"]["field_schema"]
    for name, label in {
        "financing_request_additional_borrower_entities": "Financing Request — Additional Borrower Entities",
        "financing_request_principals_guarantors": "Financing Request — Principals / Guarantors",
        "financing_request_product_type": "Financing Request — Product / Facility Type",
        "financing_request_amount": "Financing Request — Requested Amount",
        "financing_request_use_of_proceeds": "Financing Request — Purpose / Use of Proceeds",
        "financing_request_collateral": "Financing Request — Collateral / Property",
        "financing_request_target_closing_date": "Financing Request — Target Closing Date",
    }.items():
        fs[name] = {"label": label, "default": "", "raw_token": "", "field_type": "text", "row_group": None, "in_scope_for_initial_signing": True}

    sec = _find_section(sections, "SCHEDULE Part 2 — Success Fee")
    intro = sec["paragraphs"][:1]
    trailing = sec["paragraphs"][-4:]
    columns = ["Product", "Standard Range", "Rate for This File", "Minimum Fee"]
    rows = [
        ["Commercial Real Estate / DSCR", "3.5% – 5%", "$success_fee_rate_cre_dscr", "$success_fee_min_fee_cre_dscr"],
        ["Dealer Floorplan / Dealer LOC", "3% – 5%", "$success_fee_rate_dealer_floorplan", "$success_fee_min_fee_dealer_floorplan"],
        ["Warranty / Reinsurance Receivable", "3.5% – 5%", "$success_fee_rate_warranty_reinsurance", "$success_fee_min_fee_warranty_reinsurance"],
        ["Asset-Based Lending", "3% – 5%", "$success_fee_rate_asset_based_lending", "$success_fee_min_fee_asset_based_lending"],
        ["Bridge / Private Credit", "5% – 10%", "$success_fee_rate_bridge_private_credit", "$success_fee_min_fee_bridge_private_credit"],
        ["Working Capital / Line of Credit", "3.5% – 5%", "$success_fee_rate_working_capital_loc", "$success_fee_min_fee_working_capital_loc"],
        [
            "Other: $success_fee_other_product_name",
            "$success_fee_other_standard_rate_low – $success_fee_other_standard_rate_high",
            "$success_fee_rate_other",
            "$success_fee_min_fee_other",
        ],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "SCHEDULE Part 3 — Flat and Milestone Fees (earned upon performance per Section 5.4)")
    trailing = sec["paragraphs"][-1:]
    columns = ["Fee Type", "Standard Low", "Standard High", "Amount for This File"]
    rows = [
        ["Advisory / engagement fee (retainer)", "$2,500", "$5,000", "$flat_fee_advisory_engagement_retainer"],
        ["Underwriting & packaging fee", "$7,500", "$12,500", "$flat_fee_underwriting_packaging"],
        ["Financial modeling fee", "$2,000", "$5,000", "$flat_fee_financial_modeling"],
        ["Due diligence / document prep", "$3,000", "$6,000", "$flat_fee_due_diligence_doc_prep"],
        ["Technology fee", "$250", "$500", "$flat_fee_technology"],
        ["Third-party cost reimbursement", "At cost", "At cost", "At cost"],
    ]
    sec["paragraphs"] = trailing
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "SCHEDULE Part 4 — Milestones")
    trailing = sec["paragraphs"][-1:]
    columns = ["Milestone", "Fee Earned", "Due"]
    rows = [
        ["Execution of this Agreement", "Advisory + technology fee (retainer)", "At execution"],
        ["Financial model delivered", "Financial modeling fee", "On delivery"],
        ["Diligence and document package assembled", "Due diligence / doc prep fee", "On assembly"],
        ["Submission to Capital Source", "Underwriting & packaging fee", "On submission"],
        ["Initial funding or first advance", "Success fee less amounts previously earned", "At closing"],
    ]
    sec["paragraphs"] = trailing
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "EXHIBIT B")
    intro = sec["paragraphs"][:2]
    columns = ["No.", "Capital Source", "Program / Division", "Contact Introduced", "Date Introduced"]
    rows = [["—", "No entries yet.", "", "", ""]]
    sec["paragraphs"] = intro
    sec["columns"] = columns
    sec["rows"] = rows


def patch_consulting_addendum(data: dict) -> None:
    sections = data["consulting_addendum"]["sections"]

    sec = _find_section(
        sections,
        "SCHEDULE Part 1 — Success Fee Ranges by Product (percentage of gross funded/committed amount, unless "
        "Underlying Agreement is the SBA agreement, in which case subject to the SOP cap)",
    )
    columns = ["Product / Program", "Minimum", "Maximum", "Rate for This File"]
    rows = [
        ["SBA 7(a) / 504 / USDA B&I", "1%", "3% (SOP cap governs)", "$success_fee_rate_sba_7a_504_usda"],
        ["Commercial Real Estate / DSCR", "3.5%", "5%", "$success_fee_rate_cre_dscr"],
        ["Dealer Floorplan / Dealer LOC", "3%", "5%", "$success_fee_rate_dealer_floorplan_loc"],
        ["Warranty / Reinsurance Receivable", "3.5%", "5%", "$success_fee_rate_warranty_reinsurance"],
        ["Asset-Based Lending", "3%", "5%", "$success_fee_rate_asset_based_lending"],
        ["Bridge / Private Credit", "5%", "10%", "$success_fee_rate_bridge_private_credit"],
        ["Working Capital / Line of Credit", "3.5%", "5%", "$success_fee_rate_working_capital_loc"],
        [
            "Other: $success_fee_other_product_name",
            "$success_fee_other_product_min_rate",
            "$success_fee_other_product_max_rate",
            "$success_fee_other_product_rate_this_file",
        ],
    ]
    sec["paragraphs"] = []
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "SCHEDULE Part 2 — Flat and Milestone Fee Types")
    columns = ["Fee Type", "Minimum", "Maximum", "Fee for This File"]
    rows = [
        ["Advisory / engagement fee", "$2,500", "$5,000", "$flat_fee_this_file_advisory_engagement"],
        ["Underwriting & packaging fee", "$7,500", "$12,500", "$flat_fee_this_file_underwriting_packaging"],
        ["Financial modeling fee", "$2,000", "$5,000", "$flat_fee_this_file_financial_modeling"],
        ["Due diligence / document prep", "$3,000", "$6,000", "$flat_fee_this_file_due_diligence_doc_prep"],
        ["Technology fee", "$250", "$500", "$flat_fee_this_file_technology"],
        [
            "Consulting services under Article 2",
            "$consulting_services_article2_fee_min",
            "$consulting_services_article2_fee_max",
            "$consulting_services_article2_fee_this_file",
        ],
        ["Third-party cost reimbursement", "At cost", "At cost", "At cost"],
    ]
    sec["paragraphs"] = []
    sec["columns"] = columns
    sec["rows"] = rows

    sec = _find_section(sections, "SCHEDULE Part 3 — Retainer and Milestones")
    intro = sec["paragraphs"][:1]
    trailing = sec["paragraphs"][-2:]
    columns = ["Milestone", "Fee Earned", "Cumulative"]
    rows = [
        ["Engagement and file intake", "Advisory + technology fee", "$2,750 – $5,500"],
        ["Financial model delivered", "Financial modeling fee", "$4,750 – $10,500"],
        ["Diligence and document package assembled", "Due diligence / doc prep fee", "$7,750 – $16,500"],
        ["Submission to Capital Source / Program Lender", "Underwriting & packaging fee", "$15,250 – $29,000"],
        ["Initial funding or first advance", "Success fee less amounts earned above", "Per Part 1"],
    ]
    sec["paragraphs"] = intro + trailing
    sec["columns"] = columns
    sec["rows"] = rows


if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location("contract_templates_data", DATA_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    data = mod.CONTRACT_RAW_DATA

    patch_referral_protection(data)
    patch_sba_engagement(data)
    patch_client_engagement(data)
    patch_consulting_addendum(data)
    print("Patched table sections for referral_protection, sba_engagement, client_engagement, consulting_addendum")

    with DATA_FILE.open("w", encoding="utf-8") as f:
        f.write('"""Auto-generated by scripts/build_contract_templates.py + \n')
        f.write("scripts/apply_contract_field_renames.py + \n")
        f.write("scripts/patch_qc_own_field_defaults.py + \n")
        f.write("scripts/patch_venue_county_default.py + \n")
        f.write("scripts/patch_schedule_b_retainer_defaults.py + \n")
        f.write("scripts/patch_qc_own_fields_out_of_scope.py + \n")
        f.write("scripts/patch_signature_blocks.py + \n")
        f.write("scripts/patch_table_sections.py from the source .docx contract\n")
        f.write("text. Do not hand-edit generated dict literals directly here -- change\n")
        f.write("contract_templates.py's post-processing instead, or re-run the generator\n")
        f.write('against a corrected source .txt.\n"""\n\n')
        f.write("from __future__ import annotations\n\n")
        f.write("CONTRACT_RAW_DATA: dict = ")
        f.write(pprint.pformat(data, indent=2, width=100, sort_dicts=False))
        f.write("\n")
    print(f"Wrote {DATA_FILE}")
