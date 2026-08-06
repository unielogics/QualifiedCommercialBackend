"""One-time patch: transcribes the real "IN WITNESS WHEREOF... By: / Name: /
Title: / Date:" execution block that exists in all 5 source .docx contracts
but was never carried into contract_templates_data.py.

build_contract_templates.py's SIGNATURE_BLOCK_START regex intentionally
drops everything from "IN WITNESS WHEREOF"/"ACKNOWLEDGMENT" onward (see that
script's docstring), on the premise that the app's own typed-name/
signature-pad UI would render an equivalent block in its place. It never
did -- so every rendered/signed document simply ends after the last
miscellaneous clause, with no signature section, and (for referral_protection)
Schedule A's own separate certifying-officer "By:" line has nothing telling
a reader it needs anything either.

This adds a real "SIGNATURES" section (verbatim wording from the source
.txt, blanks tokenized the same way build_contract_templates.py already
tokenizes every other blank) as the new final body section, before the
first SCHEDULE/EXHIBIT, for the 4 documents that share one shape
(sba_engagement, client_engagement, consulting_addendum, referral_protection
-- all "QUALIFIED COMMERCIAL LLC ... By:/Name:/Title:/Date:" then
"[COUNTERPARTY] ... By:/Name:/Title:/Date:"), plus platform_access's
distinct individual-signer ACKNOWLEDGMENT shape.

New fields, all in_scope_for_initial_signing=False (populated programmatically
at render/sign time -- never shown as a blank input on a fill form):
  qc_signatory_name    default "Jonathan Franco"      (the standing signatory
  qc_signatory_title   default "Executive Partner"     already used elsewhere
  counterparty_signatory_name / _title  -- default "", filled by the frontend
    review step from the signer's typed name/title once they reach it.
The "Date:" line on BOTH sides of the block reuses each document's own
existing effective-date field ($effective_date, or
$underlying_agreement_effective_date for consulting_addendum) rather than
adding new date fields that could drift out of sync with it. The
counterparty-name bracket line reuses each document's own existing
counterparty-name placeholder ($client_legal_name /
$referral_partner_legal_name) rather than a literal bracket string, so it
renders the real company name, not "[CLIENT LEGAL NAME]" verbatim.

Usage: python scripts/patch_signature_blocks.py
Edits app/services/contract_templates_data.py in place.
"""

from __future__ import annotations

import importlib.util
import pprint
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "app" / "services" / "contract_templates_data.py"

QC_FIELDS = {
    "qc_signatory_name": {"label": "Qualified Commercial Signatory Name", "default": "Jonathan Franco", "in_scope_for_initial_signing": False},
    "qc_signatory_title": {"label": "Qualified Commercial Signatory Title", "default": "Executive Partner", "in_scope_for_initial_signing": False},
}

COUNTERPARTY_FIELDS = {
    "counterparty_signatory_name": {"label": "Counterparty Signatory Name", "default": "", "in_scope_for_initial_signing": False},
    "counterparty_signatory_title": {"label": "Counterparty Signatory Title", "default": "", "in_scope_for_initial_signing": False},
}

# The 4 documents sharing the "QUALIFIED COMMERCIAL LLC ... By:/Name:/Title:/Date:"
# then "[COUNTERPARTY] ... By:/Name:/Title:/Date:" shape, each with its own
# preamble line (witness-clause wording differs slightly), its own
# counterparty-name placeholder (matching that document's own cover-block
# usage), and its own effective-date field name.
STANDARD_SHAPE_DOCS = {
    "sba_engagement": {
        "witness_line": "IN WITNESS WHEREOF, the Parties have executed this Agreement as of the Effective Date.",
        "counterparty_name_field": "client_legal_name",
        "date_field": "effective_date",
    },
    "client_engagement": {
        "witness_line": "IN WITNESS WHEREOF, the Parties have executed this Agreement as of the Effective Date.",
        "counterparty_name_field": "client_legal_name",
        "date_field": "effective_date",
    },
    "consulting_addendum": {
        "witness_line": "IN WITNESS WHEREOF, the parties have executed this Addendum as of the Effective Date, to attach to and be incorporated into the Underlying Agreement identified in Section 1.1.",
        "counterparty_name_field": "client_legal_name",
        "date_field": "underlying_agreement_effective_date",
    },
    "referral_protection": {
        "witness_line": "IN WITNESS WHEREOF, the Parties have executed this Agreement as of the Effective Date.",
        "counterparty_name_field": "referral_partner_legal_name",
        "date_field": "effective_date",
    },
}


def standard_signature_section(witness_line: str, counterparty_name_field: str, date_field: str) -> dict:
    return {
        "heading": "SIGNATURES",
        "paragraphs": [
            witness_line,
            "QUALIFIED COMMERCIAL LLC",
            "By: $qc_signatory_name",
            "Name: $qc_signatory_name",
            "Title: $qc_signatory_title",
            f"Date: ${date_field}",
            f"${counterparty_name_field}",
            "By: $counterparty_signatory_name",
            "Name: $counterparty_signatory_name",
            "Title: $counterparty_signatory_title",
            f"Date: ${date_field}",
        ],
    }


# platform_access is signed by an individual User (not a company), using
# "Signature:"/"Print Name:"/"Title / Role at Referral Partner:" wording --
# see qc_contracts/platform_text.txt lines 49-61. Reuses the doc's own
# existing individual_name/referral_partner_legal_name/effective_date fields
# rather than the generic counterparty_name_field pattern, since those are
# already collected under different names in this specific document.
PLATFORM_ACCESS_SECTION = {
    "heading": "ACKNOWLEDGMENT",
    "paragraphs": [
        "By signing below, User acknowledges having read this Agreement, understands that Platform "
        "access will not be granted or will be revoked absent a signed copy on file with Qualified "
        "Commercial, and agrees to be bound by its terms.",
        "USER",
        "Signature: $individual_name",
        "Print Name: $individual_name",
        "Title / Role at Referral Partner: $counterparty_signatory_title",
        "Referral Partner: $referral_partner_legal_name",
        "Date: $effective_date",
        "QUALIFIED COMMERCIAL LLC",
        "By: $qc_signatory_name",
        "Name: $qc_signatory_name",
        "Title: $qc_signatory_title",
        "Date: $effective_date",
    ],
}


def insert_before_first_schedule_or_exhibit(sections: list[dict], new_section: dict) -> None:
    for i, sec in enumerate(sections):
        heading = sec["heading"]
        if heading.startswith("SCHEDULE") or heading.startswith("EXHIBIT"):
            sections.insert(i, new_section)
            return
    sections.append(new_section)


if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location("contract_templates_data", DATA_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    data = mod.CONTRACT_RAW_DATA

    for doc_key, shape in STANDARD_SHAPE_DOCS.items():
        fs = data[doc_key]["field_schema"]
        for name, info in {**QC_FIELDS, **COUNTERPARTY_FIELDS}.items():
            fs[name] = {**info, "raw_token": "", "field_type": "text", "row_group": None}
        section = standard_signature_section(shape["witness_line"], shape["counterparty_name_field"], shape["date_field"])
        insert_before_first_schedule_or_exhibit(data[doc_key]["sections"], section)
        print(f"{doc_key}: added SIGNATURES section ({len(data[doc_key]['sections'])} sections total)")

    fs = data["platform_access"]["field_schema"]
    for name, info in {**QC_FIELDS, **COUNTERPARTY_FIELDS}.items():
        fs[name] = {**info, "raw_token": "", "field_type": "text", "row_group": None}
    data["platform_access"]["sections"].append(PLATFORM_ACCESS_SECTION)
    print(f"platform_access: added ACKNOWLEDGMENT section ({len(data['platform_access']['sections'])} sections total)")

    with DATA_FILE.open("w", encoding="utf-8") as f:
        f.write('"""Auto-generated by scripts/build_contract_templates.py + \n')
        f.write("scripts/apply_contract_field_renames.py + \n")
        f.write("scripts/patch_qc_own_field_defaults.py + \n")
        f.write("scripts/patch_venue_county_default.py + \n")
        f.write("scripts/patch_schedule_b_retainer_defaults.py + \n")
        f.write("scripts/patch_qc_own_fields_out_of_scope.py + \n")
        f.write("scripts/patch_signature_blocks.py from the source .docx contract\n")
        f.write("text. Do not hand-edit generated dict literals directly here -- change\n")
        f.write("contract_templates.py's post-processing instead, or re-run the generator\n")
        f.write('against a corrected source .txt.\n"""\n\n')
        f.write("from __future__ import annotations\n\n")
        f.write("CONTRACT_RAW_DATA: dict = ")
        f.write(pprint.pformat(data, indent=2, width=100, sort_dicts=False))
        f.write("\n")
    print(f"Wrote {DATA_FILE}")
