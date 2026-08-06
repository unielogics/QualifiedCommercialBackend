"""One-time patch: wires Schedule A's 3 disclosure tables (Article 6.1's
existing-capital-relationship disclosure) to real signer-submitted rows,
instead of the flat column-header paragraphs left by the mechanical
transcription with no way to actually collect the disclosure data.

Adds 3 new "disclosure_rows"-type fields to referral_protection's
field_schema, each carrying a static table_columns definition (see
ContractField.table_columns / TableColumn in contract_templates.py), and
sets disclosure_field on each of Schedule A's 3 part-sections so
render_contract_document() replaces their static rows with whatever the
signer actually submits for that field (or a "None disclosed" placeholder
row if nothing was submitted).

Institutional Relationships' "program_category" column uses Schedule B's
own product-category list (the categories of loan types this contract
already defines), per the confirmed design -- not the unrelated real-estate
LoanType enum.

Usage: python scripts/patch_schedule_a_disclosure_fields.py
Edits app/services/contract_templates_data.py in place.
"""

from __future__ import annotations

import importlib.util
import pprint
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "app" / "services" / "contract_templates_data.py"

# Schedule B's own product-category list (SCHEDULE Part 1 — Standard Success
# Fee Ranges by Product), stripped of the "(program-capped)" qualifier for a
# clean dropdown label -- the categories of loan types this contract itself
# defines, not the unrelated real-estate LoanType enum used elsewhere.
PROGRAM_CATEGORIES = [
    "SBA 7(a) / Express",
    "SBA 504",
    "USDA B&I",
    "Commercial Real Estate / DSCR",
    "Dealer Floorplan / Dealer LOC",
    "Warranty / Reinsurance Receivable",
    "Asset-Based Lending",
    "Bridge / Private Credit",
    "Working Capital / LOC",
    "Other",
]

DISCLOSURE_FIELDS = {
    "schedule_a_institutional_rows": {
        "label": "Schedule A Part 1 — Institutional Relationships",
        "section_heading": "SCHEDULE Part 1 — Institutional Relationships",
        "table_columns": [
            {"key": "institution", "label": "Institution", "input_type": "text"},
            {"key": "program_category", "label": "Program / Category", "input_type": "select", "options": PROGRAM_CATEGORIES},
            {"key": "division", "label": "Division / Group", "input_type": "text"},
            {"key": "relationship_manager", "label": "Relationship Manager", "input_type": "text"},
            {"key": "start_date", "label": "Start Date", "input_type": "date"},
            {"key": "active", "label": "Active?", "input_type": "checkbox"},
        ],
    },
    "schedule_a_other_capital_rows": {
        "label": "Schedule A Part 2 — Other Capital Relationships",
        "section_heading": (
            "SCHEDULE Part 2 — Other Capital Relationships. Disclose every non-institutional capital "
            "relationship, whether or not it relates to a product Qualified Commercial places, including "
            "without limitation private credit funds, family offices and individual investors, factors, "
            "lessors, floorplan providers, warehouse lines, and any other source of capital or credit."
        ),
        "table_columns": [
            {"key": "counterparty", "label": "Counterparty", "input_type": "text"},
            {"key": "type", "label": "Type", "input_type": "text"},
            {"key": "program_facility", "label": "Program / Facility", "input_type": "text"},
            {"key": "contact", "label": "Contact", "input_type": "text"},
            {"key": "start_date", "label": "Start Date", "input_type": "date"},
        ],
    },
    "schedule_a_pending_rows": {
        "label": "Schedule A Part 3 — Pending Applications and Active Pursuits",
        "section_heading": (
            "SCHEDULE Part 3 — Pending Applications and Active Pursuits. List each Capital Source and "
            "program to which the Referral Partner has submitted an application, or is actively and "
            "demonstrably pursuing, as of the Effective Date. Attach contemporaneous documentation for each."
        ),
        "table_columns": [
            {"key": "capital_source", "label": "Capital Source", "input_type": "text"},
            {"key": "program_division", "label": "Program / Division", "input_type": "text"},
            {"key": "date_submitted", "label": "Date Submitted or Initiated", "input_type": "date"},
            {"key": "documentation_attached", "label": "Documentation Attached", "input_type": "checkbox"},
        ],
    },
}


def _find_section(sections: list[dict], heading: str) -> dict:
    for s in sections:
        if s["heading"] == heading:
            return s
    raise KeyError(heading)


if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location("contract_templates_data", DATA_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    data = mod.CONTRACT_RAW_DATA

    doc = data["referral_protection"]
    fs = doc["field_schema"]
    sections = doc["sections"]

    for field_name, info in DISCLOSURE_FIELDS.items():
        fs[field_name] = {
            "label": info["label"],
            "default": "",
            "raw_token": "",
            "field_type": "disclosure_rows",
            "row_group": None,
            "in_scope_for_initial_signing": True,
            "table_columns": info["table_columns"],
        }
        sec = _find_section(sections, info["section_heading"])
        # Every paragraph in these 3 sections up to and including the
        # certification/signature lines is either a bare column-header
        # string (now superseded by table_columns' own labels) or, for
        # Part 3 only, the trailing CERTIFICATION/By:/Name:/Title:/Date:
        # block, which must be preserved verbatim.
        header_count = len(info["table_columns"]) if field_name != "schedule_a_institutional_rows" else 6
        sec["paragraphs"] = sec["paragraphs"][header_count:]
        sec["disclosure_field"] = field_name
        print(f"referral_protection: wired {sec['heading']!r} -> disclosure_field={field_name!r}, kept {len(sec['paragraphs'])} trailing paragraph(s)")

    with DATA_FILE.open("w", encoding="utf-8") as f:
        f.write('"""Auto-generated by scripts/build_contract_templates.py + \n')
        f.write("scripts/apply_contract_field_renames.py + \n")
        f.write("scripts/patch_qc_own_field_defaults.py + \n")
        f.write("scripts/patch_venue_county_default.py + \n")
        f.write("scripts/patch_schedule_b_retainer_defaults.py + \n")
        f.write("scripts/patch_qc_own_fields_out_of_scope.py + \n")
        f.write("scripts/patch_signature_blocks.py + \n")
        f.write("scripts/patch_table_sections.py + \n")
        f.write("scripts/patch_schedule_a_disclosure_fields.py from the source .docx\n")
        f.write("contract text. Do not hand-edit generated dict literals directly here --\n")
        f.write("change contract_templates.py's post-processing instead, or re-run the\n")
        f.write('generator against a corrected source .txt.\n"""\n\n')
        f.write("from __future__ import annotations\n\n")
        f.write("CONTRACT_RAW_DATA: dict = ")
        f.write(pprint.pformat(data, indent=2, width=100, sort_dicts=False))
        f.write("\n")
    print(f"Wrote {DATA_FILE}")
