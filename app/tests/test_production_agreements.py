"""Production Package agreement templates: the extracted inventories, the fill
helpers and the field mapping for both stages, checked against the seed."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date

import pytest
from bs4 import BeautifulSoup, Tag

from app.services import production_agreements as tpl
from app.services import production_arrangement as pa
from app.services import production_fields as pf

sys.path.insert(0, "app/tests")
from test_production_arrangement import seed  # noqa: E402

DESIGN_SLOTS = {"commitment_v1": 147, "activation_v1": 150}
DESIGN_CHECKS = {"commitment_v1": 28, "activation_v1": 19}
EXPECTED_ANCHORS = {
    "commitment_v1": {"qc": 1, "dealer": 1, "sponsor": 1, "fp": 0, "rm": 1},
    "activation_v1": {"qc": 2, "dealer": 2, "sponsor": 2, "fp": 1, "rm": 1},
}
EXPECTED_INITIALS = {"qc": 1, "dealer": 2, "sponsor": 1, "fp": 0, "rm": 0}
CHECK_GROUPS = {
    "commitment_v1": {"products": 9, "support": 9, "rm_comp": 6, "financing_cost": 2, "sba": 2},
    "activation_v1": {"support": 9, "rm_comp": 6, "financing_cost": 2, "sba": 2},
}
# signature-date slots the stamper writes on the DATE anchors; never filled by the builders
COMMITMENT_STAMPED = {"sig_qc_date", "sig_dealer_date", "sig_sponsor_date", "s2_ack_date"}
ACTIVATION_STAMPED = {
    "s2_ack_date", "s5_qc_date", "s5_dealer_date", "s5_sponsor_date", "s5_fp_date",
    "ms_qc_date", "ms_dealer_date", "ms_sponsor_date",
}

SPONSOR = {
    "name": "Acme Warranty Administrators Inc", "entity_type": "Corporation", "state_of_formation": "NV",
    "principal_address": "100 Casino Center Blvd, Las Vegas NV 89101", "platform": "AcmeAdmin",
    "notice_email": "notices@acme.example", "signer_name": "Jane Sponsor", "signer_title": "CEO",
}
PARTIES = {
    "dealer": {"name": "Delgado Auto Group LLC", "signer_name": "Rafael Delgado", "signer_title": "Managing member",
               "email": "rafael@delgado.example", "phone": "+15555550100"},
    "qc": {"name": "Qualified Commercial LLC"},
    "relationship_manager": {"name": "Marisol Vega", "email": "mvega@qualifiedcommercial.com"},
}
FILE_CTX = {
    "identity": {"legal_name": "Delgado Auto Group LLC", "dba": "Delgado Auto Sales", "entity_type": "Limited liability company",
                 "state": "TX", "formation_date": "2014-03-12", "ein": "45-1234567", "naics": "441120",
                 "address": "4411 Gulf Freeway, Houston TX 77023", "license": "P-123456", "website": "delgadoauto.example"},
    "owners": [
        {"name": "Rafael Delgado", "pct": 60, "title": "Managing member", "email": "rafael@delgado.example", "phone": "(713) 555-0100", "auth": True},
        {"name": "Lucia Delgado", "pct": 40, "title": "Member", "email": "lucia@delgado.example", "phone": "(713) 555-0101", "auth": False},
    ],
    "qc": {"notice_email": "notices@qualifiedcommercial.com", "notice_address": "1 Commerce Way, Newark NJ 07102",
           "signer_name": "Denny Matos", "signer_title": "Chief Executive Officer", "address": "1 Commerce Way, Newark NJ 07102"},
    "dealer_notice": {"email": "office@delgado.example", "address": "4411 Gulf Freeway, Houston TX 77023"},
    "sponsor_notice": {"email": "legal@acme.example", "address": "100 Casino Center Blvd, Las Vegas NV 89101"},
}
META = {
    "agreement_no": "QC-PA-1A2B3C4D-R1", "effective_date": "", "written_approval_date": "2026-09-01",
    "outside_funding_date": "2026-10-16", "commitment_agreement_date": "2026-09-03", "revision_no": 1,
    "generated_on": date(2026, 9, 3),
}


def _stage_two_arrangement() -> dict:
    arr = seed()
    arr.update({
        "identity_formation_date": "2014-03-12", "identity_ein": "45-1234567", "identity_naics": "441120",
        "owners": [
            {"name": "Rafael Delgado", "pct": 60, "title": "Managing member", "email": "rafael@delgado.example", "phone": "(713) 555-0100", "auth": True},
            {"name": "Lucia Delgado", "pct": 40, "title": "Member", "email": "lucia@delgado.example", "phone": "(713) 555-0101", "auth": False},
        ],
        "dealer_notice_email": "office@delgado.example",
        "funding_party": "Lender", "funding_party_name": "First Gulf Bank N.A.", "funded_amount": 1150000,
        "funding_date": "2026-09-20", "activation_date": "2026-09-21", "commencement": "2026-10-01", "maturity": "2029-09-20",
        "funding_docs_executed_date": "2026-09-19", "controlled_account": "FGB ****4411", "ach_account": "FGB ****4412",
        "use_of_funds": {"inventory": 800000, "debt_payoff": 250000, "working_capital": 100000, "other": 0, "other_label": "Signage"},
        "program_support": ["application_packaging", "reporting_technology", "Ongoing monitoring"],
        "program_support_other": "", "fp_joinder": "no",
        "audit_discrepancy_threshold": 5, "review_threshold": 250000,
        "exclusion_1": "Hurricane closure, documented by the county", "seasonality": "Retail units run 20% under the average in January and February every year; the parties will consider that when setting thresholds for the winter months.",
        "rm_comp_categories": ["salary"], "rm_comp_other": "",
        "comp_dealer_qc_amount": "$3,200 per month", "comp_dealer_qc_purpose": "Programme management",
        "program_economics_1": "Sponsor markup of 12% on every Covered Product premium.",
        "financing_cost_included": "No", "sba_status": "Not an SBA transaction",
        "protected_1_name": "First Gulf Bank N.A.", "protected_1_rel": "Introduced lender", "protected_1_date": "2026-08-15",
        "protected_1_txn": "Dealer capital advance", "existing_1_name": "Texas Floorplan Co", "existing_1_rel": "Floorplan lender",
        "existing_1_info": "Since 2019", "protected_source": "First Gulf Bank N.A.",
    })
    return arr


def _manifest_entry(key: str) -> dict:
    return tpl.manifest()["templates"][key]


# ---------------------------------------------------------------------------
# manifest + templates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", tpl.TEMPLATE_KEYS)
def test_manifest_inventories(key: str):
    entry = _manifest_entry(key)
    # the design's slot inventory plus the sponsor logo text slot the extraction adds
    assert len(entry["fields"]) == DESIGN_SLOTS[key] + 1
    assert entry["fields"].count("sponsor_logo_text") == 1
    assert len(set(entry["fields"])) == len(entry["fields"])
    assert len(entry["checks"]) == DESIGN_CHECKS[key]
    groups = {}
    for check in entry["checks"]:
        group, _, slug = check.partition(".")
        assert slug, check
        groups[group] = groups.get(group, 0) + 1
    assert groups == CHECK_GROUPS[key]
    assert entry["anchors"] == EXPECTED_ANCHORS[key]
    assert entry["initials"] == EXPECTED_INITIALS
    assert entry["source_artifact"].startswith("artifact-")
    assert tpl.manifest()["version"] == "2026-09-03-1"
    html, sha = tpl.load_template(key)
    assert sha == entry["sha256"] == hashlib.sha256(html.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("key", tpl.TEMPLATE_KEYS)
def test_template_is_print_ready(key: str):
    html, _ = tpl.load_template(key)
    for forbidden in ("sc-raw-", "@font-face", "<img", "<helmet", "<doc-page", "<script", "<link", "sc-camel-view-box", "'Archivo'", "'IBM Plex Sans'"):
        assert forbidden not in html, forbidden
    assert html.startswith("<!doctype html>")
    assert "@page { size: Letter" in html and "{{FOOTER}}" in html and 'viewBox="0 0 512 512"' in html
    assert '"DejaVu Sans"' in html
    assert ".chk.on::after" in html and ".anc{" in html and "h2,h3{break-after:avoid}" in html
    soup = BeautifulSoup(html, "html.parser")
    entry = _manifest_entry(key)
    assert tpl.template_field_keys(key) == entry["fields"]
    assert tpl.template_check_keys(key) == entry["checks"]
    for slot in soup.find_all(attrs={"data-field": True}):
        stray = [c for c in slot.contents if not (isinstance(c, Tag) and c.get("class") == ["anc"])]
        assert not stray, f"{slot['data-field']} is not an empty leaf"
    for box in soup.find_all(attrs={"data-check": True}):
        assert box.get("class") == ["chk"] and box.name == "span" and not box.contents
    # every anchor the manifest promises is in the markup exactly once, and nothing else is
    found = [m.group(0) for m in tpl.ANCHOR_RE.finditer(html)]
    expected = [t for tokens in tpl.anchor_tokens(key).values() for t in tokens]
    assert sorted(found) == sorted(expected)
    # DATE anchors live inside their data-field slot so the stamp lands on the date line
    for span in soup.select("span.anc"):
        token = span.get_text()
        if token.startswith("[[DATE:"):
            assert span.parent.get("data-field", "").endswith("_date"), token
        else:
            assert not span.parent.get("data-field"), token
    captions = {"commitment_v1": 6, "activation_v1": 7}[key]
    assert len(soup.select("div.pb")) == captions
    assert len(soup.select(".keep")) >= 4
    assert "sc-raw" not in html


def test_anchor_tokens_and_strip():
    tokens = tpl.anchor_tokens("activation_v1")
    assert tokens["dealer"] == ["[[SIG:dealer:1]]", "[[DATE:dealer:1]]", "[[SIG:dealer:2]]", "[[DATE:dealer:2]]", "[[INI:dealer:1]]", "[[INI:dealer:2]]"]
    assert tokens["fp"] == ["[[SIG:fp:1]]", "[[DATE:fp:1]]"]
    assert tokens["rm"] == ["[[SIG:rm:1]]", "[[DATE:rm:1]]"]
    assert "fp" not in tpl.anchor_tokens("commitment_v1")
    assert tpl.anchor("INI", "qc", 3) == "[[INI:qc:3]]"
    with pytest.raises(ValueError):
        tpl.anchor("SEAL", "qc", 1)
    text = "By (signature) [[SIG:dealer:1]] Name [[DATE:dealer:1]]\nQC initials [[INI:qc:1]] done"
    assert tpl.strip_anchors(text) == "By (signature)  Name \nQC initials  done"
    assert tpl.strip_anchors("") == "" and tpl.strip_anchors(None) == ""


def test_load_template_refuses_a_tampered_file(monkeypatch):
    forged = json.loads(json.dumps(tpl._manifest()))
    forged["templates"]["commitment_v1"]["sha256"] = "0" * 64
    monkeypatch.setattr(tpl, "_manifest", lambda: forged)
    tpl._load.cache_clear()
    try:
        with pytest.raises(tpl.TemplateIntegrityError):
            tpl.load_template("commitment_v1")
        with pytest.raises(KeyError):
            tpl.template_entry("nope_v9")
    finally:
        tpl._load.cache_clear()


# ---------------------------------------------------------------------------
# fill
# ---------------------------------------------------------------------------

def test_fill_template_escapes_blanks_ticks_and_strips():
    out = tpl.fill_template(
        "commitment_v1",
        {"dealer_legal_name": "Delgado <b>Auto</b> & Sons", "dealer_address": "4411 Gulf Freeway\nHouston TX 77023",
         "sig_dealer_date": "", "owner_1_auth": True},
        {"products.vsc", "sba.not_sba"},
        footer='Commitment "R1"',
    )
    assert "Delgado &lt;b&gt;Auto&lt;/b&gt; &amp; Sons" in out
    assert "4411 Gulf Freeway<br/>Houston TX 77023" in out
    assert ">Yes<" in out
    assert "data-field=" not in out and "data-check=" not in out
    assert out.count('class="chk on"') == 2 and out.count('class="chk"') == 26
    assert 'content: "Qualified Commercial | Commitment \\"R1\\""' in out
    assert "{{FOOTER}}" not in out
    # anchors survive inside the date slot that was filled blank, and beside it
    assert "[[DATE:dealer:1]]" in out and "[[SIG:dealer:1]]" in out
    soup = BeautifulSoup(out, "html.parser")
    span = next(s for s in soup.select("span.anc") if s.get_text() == "[[DATE:dealer:1]]")
    assert span.parent.get_text(strip=True) == "[[DATE:dealer:1]]"
    assert sorted(m.group(0) for m in tpl.ANCHOR_RE.finditer(out)) == sorted(t for ts in tpl.anchor_tokens("commitment_v1").values() for t in ts)


def test_fill_template_rejects_unknown_keys():
    with pytest.raises(KeyError, match="a2_avg_units_op"):
        tpl.fill_template("commitment_v1", {"a2_avg_units_op": "96"}, footer="x")
    with pytest.raises(KeyError, match="products.vsc"):
        tpl.fill_template("activation_v1", {}, {"products.vsc"}, footer="x")
    with pytest.raises(KeyError):
        tpl.fill_template("nope_v9", {}, footer="x")


def test_content_sha_tracks_values_and_checks():
    a = tpl.content_sha256("commitment_v1", {"agreement_no": "QC-1"}, set())
    assert a == tpl.content_sha256("commitment_v1", {"agreement_no": "QC-1"}, [])
    assert a != tpl.content_sha256("commitment_v1", {"agreement_no": "QC-2"}, set())
    assert a != tpl.content_sha256("commitment_v1", {"agreement_no": "QC-1"}, {"products.vsc"})
    assert a != tpl.content_sha256("activation_v1", {"agreement_no": "QC-1"}, set())


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def test_formatters():
    assert pf.money(1234567) == "$1,234,567"
    assert pf.money("1234567.5") == "$1,234,567.50"
    assert pf.money("$1,200,000") == "$1,200,000"
    assert pf.money(-42) == "-$42"
    assert pf.money("") == "" and pf.money(None) == "" and pf.money("abc") == ""
    assert pf.pct(62) == "62%" and pf.pct(62.5) == "62.5%" and pf.pct(52.73) == "52.7%" and pf.pct("") == ""
    assert pf.count(96) == "96" and pf.count(1234) == "1,234" and pf.count(0) == "0" and pf.count("x") == ""
    assert pf.metric(pf.count, 0) == "" and pf.metric(pf.count, None) == "" and pf.metric(pf.money, 41300) == "$41,300"
    assert pf.fmt_date("2026-09-03") == "September 03, 2026"
    assert pf.fmt_date(date(2026, 9, 3)) == "September 03, 2026"
    assert pf.fmt_date("2026-09-03T12:00:00+00:00") == "September 03, 2026"
    assert pf.fmt_date("upon funding") == "upon funding" and pf.fmt_date("") == ""
    assert pf.yes_no(True) == "Yes" and pf.yes_no("sent") == "Yes" and pf.yes_no(None) == "" and pf.yes_no("Pending") == "Pending"
    assert pf.name_and_title("Rafael Delgado", "Managing member") == "Rafael Delgado, Managing member"
    assert pf.name_and_title("", "CEO") == "CEO"
    one, two = pf.split_notes("word " * 30)
    assert len(one) <= 90 and one.endswith("word") and two.startswith("word")
    assert pf.split_notes("short") == ("short", "")
    assert pf.adjustment_text({"adj": "bps", "adj_value": 200}) == "200 basis points"
    assert pf.adjustment_text({"adj": "rate", "adj_value": 1.5}) == "1.5% adjusted rate"
    assert pf.adjustment_text({"adj": "none"}) == "None" and pf.adjustment_text({}) == "None"


# ---------------------------------------------------------------------------
# stage one values
# ---------------------------------------------------------------------------

def test_commitment_values_cover_the_manifest_on_the_seed():
    arr = seed()
    computed = pa.compute(arr)
    values, checks = pf.commitment_values(arr, computed, SPONSOR, PARTIES, FILE_CTX, META)
    entry = _manifest_entry("commitment_v1")
    assert set(values) == set(entry["fields"]) - COMMITMENT_STAMPED
    assert COMMITMENT_STAMPED == pf.STAMPED_SLOTS["commitment_v1"]
    assert checks <= set(entry["checks"])
    # the arrangement defaults pre-select the disclosure answers (production_arrangement.DEFAULTS)
    assert pa.DEFAULTS["financing_cost_included"] == "No" and pa.DEFAULTS["sba_status"] == "Not an SBA transaction"
    assert checks == {"products.vsc", "products.gap", "products.theft", "products.appearance", "products.tire",
                      "financing_cost.no", "sba.not_sba"}
    assert all(isinstance(v, str) for v in values.values())

    assert values["agreement_no"] == "QC-PA-1A2B3C4D-R1"
    assert values["effective_date"] == ""
    assert values["written_approval_date"] == "September 01, 2026"
    assert values["outside_funding_date"] == "October 16, 2026"
    assert values["dealer_legal_name"] == "Delgado Auto Group LLC" and values["dealer_entity_type"] == "Limited liability company"
    assert values["sponsor_legal_name"] == "Acme Warranty Administrators Inc" == values["sponsor_logo_text"]
    assert values["sponsor_address"] == SPONSOR["principal_address"]
    assert values["qc_address"] == FILE_CTX["qc"]["address"]
    assert values["minimum_activation_amount"] == "$900,000" and values["exclusivity_days"] == "45"
    # §9.1 falls back to the file for what the seed does not carry
    assert values["identity_legal_name"] == "Delgado Auto Group LLC" and values["identity_ein"] == "45-1234567"
    assert values["identity_formation_date"] == "March 12, 2014" and values["identity_naics"] == "441120"
    assert values["owner_1_name"] == "Rafael Delgado" and values["owner_1_pct"] == "60%" and values["owner_1_auth"] == "Yes"
    assert values["owner_2_auth"] == "No" and values["owner_3_name"] == "" and values["owner_total_pct"] == "100%"
    # Schedule A
    assert values["sa_requested_amount"] == "$1,200,000" and values["sa_facility_type"] == "Dealer capital advance"
    assert values["sa_sponsor_platform"] == "AcmeAdmin"
    assert values["sa_notice_qc_email"] == "notices@qualifiedcommercial.com"
    assert values["sa_notice_dealer_email"] == "office@delgado.example"
    assert values["sa_notice_sponsor_email"] == "notices@acme.example"
    assert values["sa_notice_sponsor_address"] == SPONSOR["principal_address"]
    # Schedule B
    assert values["s2_rm_name"] == "Marisol Vega" == values["s2_ack_name"] and values["s2_rm_employer"] == "Qualified Commercial LLC"
    # Schedule C defaults
    assert values["s3_fp_qc_amount"] == "$0" and values["s3_fp_qc_purpose"] == "None"
    assert values["s3_dealer_sponsor_post_amount"] == "$0" and values["s3_conflict_1"] == ""
    # Schedule D blank at stage one
    assert values["s4_protected_1_name"] == "" and values["s4_existing_4_info"] == ""
    # Schedule E from the computed economics
    assert values["se_baseline_from"] == "September 01, 2025" and values["se_baseline_through"] == "August 31, 2026"
    assert values["se_evidence"] == "DMS unit reports, Sponsor production reports, Bank statements (Plaid)"
    assert values["se_units_baseline"] == "96" and values["se_units_source"] == values["se_evidence"]
    assert values["se_vsc_baseline"] == "60" and values["se_pen_baseline"] == "62%"
    assert values["se_vsc_gross_baseline"] == "$144,000"
    assert values["se_cp_gross_baseline"] == pf.money(computed["econ"]["gross"])
    assert values["se_cancel_baseline"] == "4" and values["se_chargeback_baseline"] == "2"
    assert values["se_notes_1"] == "" and values["se_notes_2"] == ""
    assert values["se_verified_by"] == "Marisol Vega" and values["se_dealer_confirm"] == "Rafael Delgado"
    assert values["se_date"] == "September 03, 2026"
    # signature page
    assert values["sig_qc_name"] == "Denny Matos" and values["sig_qc_title"] == "Chief Executive Officer"
    assert values["sig_dealer_name"] == "Rafael Delgado, Managing member"
    assert values["sig_sponsor_name"] == "Jane Sponsor, CEO" and values["sig_sponsor_legal_name"] == SPONSOR["name"]

    out = tpl.fill_template("commitment_v1", values, checks, footer="Production Commitment and Capital Engagement Agreement")
    assert "Delgado Auto Group LLC" in out and out.count('class="chk on"') == 7


def test_commitment_values_blank_when_nothing_is_known():
    arr = pa.empty_arrangement()
    values, checks = pf.commitment_values(arr, pa.compute(arr), None, {}, {}, {})
    entry = _manifest_entry("commitment_v1")
    assert set(values) == set(entry["fields"]) - COMMITMENT_STAMPED
    # the primary product is on by default; the disclosure defaults come with the arrangement
    assert checks == {"products.vsc", "financing_cost.no", "sba.not_sba"}
    arr["financing_cost_included"] = ""
    arr["sba_status"] = ""
    assert pf.commitment_values(arr, pa.compute(arr), None, {}, {}, {})[1] == {"products.vsc"}
    assert values["se_units_baseline"] == "" and values["se_units_source"] == ""
    assert values["owner_total_pct"] == "" and values["s3_fp_qc_amount"] == "$0"
    assert values["sa_notice_qc_email"] == "" and values["sig_dealer_name"] == ""
    tpl.fill_template("commitment_v1", values, checks, footer="x")


def test_commitment_checks_and_notes_from_stage_two_keys():
    arr = _stage_two_arrangement()
    values, checks = pf.commitment_values(arr, pa.compute(arr), SPONSOR, PARTIES, FILE_CTX, META)
    assert {"support.application_packaging", "support.reporting_technology", "support.ongoing_monitoring",
            "rm_comp.salary", "financing_cost.no", "sba.not_sba"} <= checks
    assert "support.other" not in checks and "rm_comp.other" not in checks
    assert values["s3_dealer_qc_post_amount"] == "$3,200 per month" and values["s3_dealer_qc_post_purpose"] == "Programme management"
    assert values["s4_protected_1_date"] == "August 15, 2026"
    assert values["owner_1_name"] == "Rafael Delgado" and values["owner_total_pct"] == "100%"
    assert values["se_notes_1"] and values["se_notes_2"] and len(values["se_notes_1"]) <= 90
    assert " ".join([values["se_notes_1"], values["se_notes_2"]]) == arr["seasonality"]
    arr["program_support_other"] = "Onsite F&I training"
    arr["rm_comp_other"] = "Documented travel reimbursement"
    arr["financing_cost_included"] = "Yes"
    arr["sba_status"] = "SBA transaction; required SBA compensation documentation attached"
    values, checks = pf.commitment_values(arr, pa.compute(arr), SPONSOR, PARTIES, FILE_CTX, META)
    assert {"support.other", "rm_comp.other", "financing_cost.yes", "sba.sba"} <= checks
    assert values["sa_support_other"] == "Onsite F&I training" and values["s2_comp_other"] == "Documented travel reimbursement"


# ---------------------------------------------------------------------------
# stage two values
# ---------------------------------------------------------------------------

def test_activation_values_cover_the_manifest_on_the_seed():
    arr = _stage_two_arrangement()
    computed = pa.compute(arr)
    values, checks = pf.activation_values(arr, computed, SPONSOR, PARTIES, FILE_CTX, META, original=seed())
    entry = _manifest_entry("activation_v1")
    assert set(values) == set(entry["fields"]) - ACTIVATION_STAMPED
    assert ACTIVATION_STAMPED == pf.STAMPED_SLOTS["activation_v1"]
    assert checks <= set(entry["checks"])
    assert checks == {"support.application_packaging", "support.reporting_technology", "support.ongoing_monitoring",
                      "rm_comp.salary", "financing_cost.no", "sba.not_sba"}
    assert all(isinstance(v, str) for v in values.values())

    assert values["activation_date"] == "September 21, 2026" and values["actual_funding_date"] == "September 20, 2026"
    assert values["commitment_agreement_date"] == "September 03, 2026"
    assert values["minimum_activation_amount"] == "$900,000"
    assert values["audit_discrepancy_threshold"] == "5%" and values["review_threshold"] == "$250,000"
    # Addendum A from the computed thresholds (A.3 guideline on the seed)
    rows = {r["key"]: r for r in computed["thresholds"]["rows"] if r.get("editable")}
    assert values["baseline_from"] == "September 01, 2025"
    assert values["a2_avg_units_baseline"] == "96" and values["a2_avg_units_op"] == "96" and values["a2_min_units_op"] == "82"
    assert values["a2_avg_vsc_baseline"] == "60" and values["a2_min_vsc_op"] == "51"
    assert values["a2_pen_baseline"] == "62%" and values["a2_min_pen_op"] == "53%" and values["a2_roll_pen_op"] == "56%"
    assert values["a2_avg_vsc_gross_baseline"] == "$144,000" and values["a2_min_vsc_gross_op"] == pf.money(rows["vsc_gross"]["operative"])
    assert values["a2_min_cp_gross_op"] == pf.money(rows["total_gross"]["operative"])
    assert values["a2_debt_service_op"] == "$41,300" and values["a2_min_remittance_op"] == "$51,625"
    assert values["a2_production_commencement"] == "October 01, 2026"
    assert values["a4_units"] == "246" and values["a4_vsc"] == "153" and values["a4_remittance"] == "$154,875"
    assert values["a4_penetration"] == "56%" and values["a4_vsc_gross"] == pf.money(rows["vsc_gross"]["operative"] * 3)
    assert values["a5_exclusion_1"] == "Hurricane closure, documented by the county" and values["a5_exclusion_3"] == ""
    assert values["a6_cure_days"] == "5" and values["a8_pricing_adjustment"] == "200 basis points"
    # Schedule 1
    assert values["s1_funding_party"] == "First Gulf Bank N.A." == values["s5_funding_party"]
    assert values["s1_actual_funding_amount"] == "$1,150,000" == values["s5_amount_funded"]
    assert values["s1_maturity_date"] == "September 20, 2029" and values["s1_monthly_debt_service"] == "$41,300"
    assert values["s1_controlled_account"] == "FGB ****4411" and values["s1_ach_account"] == "FGB ****4412"
    assert values["s1_use_inventory"] == "$800,000" and values["s1_use_debt_payoff"] == "$250,000"
    assert values["s1_use_other"] == "$0 (Signage)" and values["s1_use_equipment"] == "" and values["s1_use_total"] == "$1,150,000"
    assert values["s1_notice_dealer_email"] == "office@delgado.example" and values["s1_notice_qc_address"] == FILE_CTX["qc"]["notice_address"]
    # Schedules 2-4 as stage one
    assert values["s2_ack_name"] == "Marisol Vega" and values["s3_fp_qc_amount"] == "$0"
    assert values["s4_protected_1_name"] == "First Gulf Bank N.A." and values["s4_existing_1_rel"] == "Floorplan lender"
    # Schedule 5 certificate
    assert values["s5_docs_executed_date"] == "September 19, 2026" and values["s5_funding_date"] == "September 20, 2026"
    assert values["s5_production_commencement"] == "October 01, 2026" and values["s5_activation_date"] == "September 21, 2026"
    assert values["s5_pricing_adjustment"] == "200 basis points" and values["s5_protected_source"] == "First Gulf Bank N.A."
    assert values["s5_qc_name"] == "Denny Matos" and values["s5_dealer_name"] == "Rafael Delgado, Managing member"
    assert values["s5_sponsor_name"] == "Jane Sponsor, CEO" and values["s5_dealer_legal_name"] == "Delgado Auto Group LLC"
    assert values["s5_fp_legal_name"] == "" and values["s5_fp_name"] == ""  # no joinder expected
    assert values["ms_qc_title"] == "Chief Executive Officer" and values["ms_sponsor_legal_name"] == SPONSOR["name"]

    out = tpl.fill_template("activation_v1", values, checks, footer="Program Activation and Production Agreement")
    assert "First Gulf Bank N.A." in out and out.count('class="chk on"') == 6


def test_activation_values_funding_override_joinder_and_original_backfill():
    arr = _stage_two_arrangement()
    arr["fp_joinder"] = "yes"
    arr["dealer_notice_email"] = ""
    original = {**seed(), "dealer_notice_email": "old@delgado.example"}
    funding = {"funded_amount": 1100000, "funding_date": "2026-09-22", "activation_date": None}
    values, _ = pf.activation_values(arr, pa.compute(arr), SPONSOR, PARTIES, {}, META, original=original, funding=funding)
    assert values["s5_fp_legal_name"] == "First Gulf Bank N.A."
    assert values["s5_amount_funded"] == "$1,100,000" == values["s1_actual_funding_amount"]
    assert values["actual_funding_date"] == "September 22, 2026" and values["activation_date"] == "September 21, 2026"
    # the Activation carries no §9.1 / §9.2 section: identity and owners print on the Commitment only
    assert not any(k.startswith(("identity_", "owner_")) for k in values)
    assert values["s1_notice_dealer_email"] == "old@delgado.example"
    assert values["s1_notice_qc_email"] == "" and values["qc_address"] == ""  # no file context
    # a sponsor-funded deal names the sponsor; an unnamed lender prints blank
    arr2 = {**_stage_two_arrangement(), "funding_party": "Sponsor", "funding_party_name": ""}
    values2, _ = pf.activation_values(arr2, pa.compute(arr2), SPONSOR, PARTIES, FILE_CTX, META)
    assert values2["s1_funding_party"] == SPONSOR["name"]
    arr3 = {**_stage_two_arrangement(), "funding_party_name": ""}
    values3, _ = pf.activation_values(arr3, pa.compute(arr3), SPONSOR, PARTIES, FILE_CTX, META)
    assert values3["s1_funding_party"] == ""


def test_activation_values_blank_on_an_empty_arrangement():
    arr = pa.empty_arrangement()
    values, checks = pf.activation_values(arr, pa.compute(arr), None, {}, {}, {})
    assert set(values) == set(_manifest_entry("activation_v1")["fields"]) - ACTIVATION_STAMPED
    assert checks == {"financing_cost.no", "sba.not_sba"}  # arrangement defaults; nothing else is known
    assert values["audit_discrepancy_threshold"] == "5%" and values["a8_pricing_adjustment"] == "200 basis points"
    assert values["a2_min_units_op"] == "" and values["a2_avg_units_baseline"] == "" and values["a4_units"] == ""
    assert values["a2_min_remittance_op"] == "" and values["s1_use_total"] == ""
    tpl.fill_template("activation_v1", values, checks, footer="x")


# ---------------------------------------------------------------------------
# rendering (prod image only: WeasyPrint natives are absent on the dev box)
# ---------------------------------------------------------------------------

def test_rendered_pdfs_carry_every_anchor():
    try:
        import weasyprint  # noqa: PLC0415 -- native libraries; absent on the dev box
    except Exception as exc:  # noqa: BLE001 -- weasyprint raises OSError, not ImportError, without pango
        pytest.skip(f"weasyprint unavailable here: {exc}")
    fitz = pytest.importorskip("fitz")
    arr = _stage_two_arrangement()
    computed = pa.compute(arr)
    for key, builder in (("commitment_v1", pf.commitment_values), ("activation_v1", pf.activation_values)):
        values, checks = builder(arr, computed, SPONSOR, PARTIES, FILE_CTX, META)
        html = tpl.fill_template(key, values, checks, footer="render test")
        pdf = weasyprint.HTML(string=html).write_pdf()
        doc = fitz.open(stream=pdf, filetype="pdf")
        assert doc.page_count > 0
        text = "\n".join(page.get_text() for page in doc)
        for tokens in tpl.anchor_tokens(key).values():
            for token in tokens:
                hits = [rect for page in doc for rect in page.search_for(token)]
                assert len(hits) == 1, f"{key}: {token} found {len(hits)} times"
        assert "Delgado Auto Group LLC" in text
        assert not re.search(r"\[\[(SIG|DATE|INI):", tpl.strip_anchors(text))
