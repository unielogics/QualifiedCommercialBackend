"""The saved plan document.

Six parts drawn only from what a version stored, printing figures the way the
plan's screens print them, and never what a reviewer wrote for staff.
"""
from __future__ import annotations

import re
import sys
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.schemas import earnings_plan_v2 as schema
from app.services import earnings_improvement as v1
from app.services import earnings_plan_pdf as document
from app.services import earnings_plan_pdf_plain as plain
from app.services import earnings_plan_service as plan_service
from app.services import operating_plan_schema
from app.tests.earnings_plan_fixture import file_body, file_cockpit, item_id
from app.tests.test_earnings_plan_service import STAFF, approve, finished


def saved(*, terms: bool = True, client: bool = False, edit=None) -> tuple[dict, dict]:
    """The design's plan, finished and partly reviewed, with the facts publishing stores beside it."""
    cockpit = file_cockpit(goal=215_000, terms=terms)
    body = finished(approve(file_body(), cockpit))
    if edit:
        edit(body)
    body = schema.validate_body(body)
    if client:
        # A plan the business submits carries no staff approval into its version.
        body = plan_service.protect_reviews(body, schema.empty_body())
    evaluation = v1.evaluate(body, cockpit)
    facts = plan_service.version_facts(operating_plan_schema.key_facts(body), body, cockpit, evaluation, client_submission=client, goal=cockpit["_operating_goal"])
    facts["operating_review"] = {"version_number": 3, "saved_at": "2026-11-14T14:02:00+00:00"}
    return body, facts


def words(markup: str) -> str:
    """The document as a reader sees it: no tags, no stylesheet, single spaces."""
    text = re.sub(r"<style>.*?</style>", " ", markup, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, character in (("&amp;", "&"), ("&#x27;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(entity, character)
    # A figure in bold is followed by its comma or full stop, not by a space.
    return re.sub(r"\s+([,.;])", r"\1", re.sub(r"\s+", " ", text))


def weasyprint_or_skip():
    try:
        pytest.importorskip("weasyprint")
    except OSError:
        pytest.skip("WeasyPrint native Pango runtime is unavailable")


@pytest.mark.parametrize("printed,expected", [
    (document._usd(-22377), "−$22,377"), (document._usd(-0.4), "$0"), (document._usd(2.5), "$3"), (document._usd(None), "—"),
    (document._usd(105, 2), "$105.00"), (document._signed_usd(205273.64), "+$205,274"), (document._signed_usd(-14400), "−$14,400"),
    (document._signed_usd(0.4), "$0"), (document._times(-0.028777), "−0.03x"), (document._times(-0.001), "0.00x"),
    (document._signed_times(1.0586), "+1.06x"), (document._signed_times(0.001), "0.00x"), (document._percent(0.116257), "11.6%"),
    (document._percent(-0.0249), "−2.5%"), (document._points(0.141157), "+14.1 pts"), (document._points(-0.0001), "0.0 pts"),
    (document._plain(1250.5), "1,250.5"), (document._plain(340.0), "340"), (document._factor_value({"unit": "$"}, 262), "$262.00"),
    (document._factor_value({"unit": "%"}, 95), "95%"), (document._factor_value({"unit": "units / mo"}, 340), "340 units / mo"),
    (document._month("2026-11", 2), "Jan 2027"), (document._month("2027-01", 11, short=True), "Dec"), (document._month(None, 0), "Month 1"),
    (document._period("2025-01-01", "2025-12-31"), "Jan 1 – Dec 31, 2025"), (document._period("2024-07-01", "2025-06-30"), "Jul 1, 2024 – Jun 30, 2025"),
])
def test_figures_print_as_the_plan_screens_print_them(printed, expected):
    # A true minus, halves rounded away from zero, and never a signed zero: the same rules as src/lib/financialsOverview.ts.
    assert printed == expected


def test_the_document_has_six_parts_and_the_six_figures_before_and_after():
    body, facts = saved()
    markup = document.build_html(body=body, financial_totals=facts)
    text = words(markup)
    # Six parts in order, each under its own heading.
    places = [text.index("Operating Expenses and Earnings Plan")] + [text.index(f"Part {number} of 6 {title}") for number, title in enumerate(document.PARTS[1:], 2)]
    assert places == sorted(places) and markup.count("<section class='part") == 6
    for line in ("Harbor Auto Detail LLC", "Version 3 · Saved Nov 14, 2026 · Saved by staff", "IRS 1120-S · 2025", "Tax year 2025 · calendar year assumed", "Jan 2027"):
        assert line in text, line
    # Before, after, the change and the reviewed figure of each of the six metrics, as the File page shows them.
    for label, before, after, change, reviewed in (
            ("EBITDA", "−$22,377", "$182,897", "+$205,274", "$52,164"), ("NOI", "−$30,777", "$174,497", "+$205,274", "$43,764"),
            ("DSCR", "−0.03x", "1.03x", "+1.06x", "0.29x"), ("Free cash flow", "−$799,977", "$5,297", "+$805,274", "−$125,436"),
            ("Operating margin", "−2.5%", "11.6%", "+14.1 pts", "3.4%"), ("Debt service", "$777,600", "$177,600", "−$600,000", "$177,600")):
        assert f"{label} {change} {before} → {after} Reviewed changes only {reviewed}" in text, label
    assert "Covers debt" in text and "Needs $39,103 more EBITDA a year for 1.25x" in text
    assert "81% of the $215,000 goal with the plan" in text and "Still to find $40,503" in text
    # The bridge: the baseline, the steps in their order, the plan.
    bridge = "Baseline EBITDA −$22,377 Pricing $0 Revenue drivers +$89,616 Growth +$36,845 Cost of goods sold +$21,420 Operating expenses +$71,793 New activities −$14,400 Plan EBITDA $182,897"
    assert bridge in text
    stored = facts["plan_result"]
    assert round(stored["baseline"]["ebitda"] + sum(step["value"] for step in stored["bridge"]["proposed"]), 2) == round(stored["proposed"]["ebitda"], 2)


def test_each_part_prints_the_source_the_model_the_changes_and_the_handoff():
    body, facts = saved()
    text = words(document.build_html(body=body, financial_totals=facts))
    for line in (
            # 2 · the ladder, the groups that add up, the loan terms
            "Revenue $1,236,000", "− Cost of goods sold $601,886", "− Operating expenses $664,891", "= NOI −$30,777",
            "+ Other income, depreciation and amortization $8,400", "= EBITDA −$22,377", "10 of 10 confirmed by the business",
            "Costs on the source $1,266,777", "These groups add up to cost of goods sold plus operating expenses.",
            "Current payments $64,800 / mo New payment $14,800 / mo Cash released $50,000 / mo DSCR from terms alone −0.03x → −0.13x",
            "Payroll taxes Included in another line", "Transportation Not applicable",
            # 3 · the revenue model and how each cost behaves
            "Reconciles to IRS revenue", "Units sold 300 units / mo Price per unit $250.00 Units sold 340 units / mo Price per unit $262.00 $900,000 $1,068,960",
            "Revenue model $1,236,000 $1,500,960", "$105.00 per unit of Detail packages", "55.0% of Fleet maintenance contracts revenue", "2.0% of all revenue",
            "Cost of goods sold $601,886 $677,666 +$75,780", "Growth spending and new activities", "$0 $50,400 +$50,400", "Operating expenses $664,891 $648,797 −$16,094",
            # 4 · every change with its logic
            "Revenue drivers EBITDA +$95,841 a year", "Detail packages · Price per unit Increase by $12.00 · $250.00 → $262.00 Price list moves to $262.",
            "Moves Revenue +$48,960 · Card processing +$783 Feb 2027 +$48,177 +$44,162 +0.27x Reviewed",
            "Spend $3,000 a month to add 40 units sold a month in Detail packages · setup $1,500 one time",
            "Rent Decrease by $28,805 a year · $196,805 → $168,000 a year Lease amendment on bay 2 signed in October 2026. Apr 2027 +$28,805",
            "Decrease by 5% · $406,980 a year in the plan, with the volume it follows",
            "Weekend detailer, part time New cost of $1,200 a month ($14,400 a year)", "$35,800 / mo stays as cash",
            "Operating reserve · Reserve $10,000 / mo",
            # 5 · before and after, the bridge, the ramp
            "Revenue $1,236,000 $1,500,960 +$264,960 $1,279,200", "= EBITDA −$22,377 $182,897 +$205,274 $52,164", "First-year EBITDA −$22,377 $145,663 +$168,040 $41,435",
            "Revenue drivers +$89,616 +$42,336", "Monthly debt payment $14,800", "First year $145,663 · full-year run rate $182,897",
            # 6 · review, confirmation, handoff
            "3 reviewed · 8 proposed", "Counting reviewed changes only, EBITDA is $52,164 a year and DSCR is 0.29x, against $182,897 and 1.03x with every proposed change.",
            "Confirmed The business confirmed these disclosures and proposals.", "No historical differences recorded.", "Source figures reference"):
        assert line in text, line
    # Twelve months along the ramp, starting where the plan starts.
    assert "−$1,581 Jan $4,463 Feb $6,174 Mar" in text and "$14,959 Apr $14,959 May $15,241 Jun" in text and text.count("$15,241") == 7


def test_nothing_a_reviewer_wrote_for_staff_is_printed():
    body, facts = saved()
    assert body["changes"][0]["review_reason"] == "Lease and price list checked."
    for markup in (document.build_html(body=body, financial_totals=facts), plain.build_html(body=body, financial_totals=facts)):
        for private in ("Lease and price list checked.", str(STAFF.id), "Not in the 2025 return.", "incremental_to_baseline", "review_stamp"):
            assert private not in markup, private
        assert "Reviewed" in markup


def test_what_the_business_typed_is_text_and_never_markup_or_style():
    def edit(body):
        body["business_name"] = "</style><script>alert('x')</script> & Sons"
        body["changes"][0]["note"] = "<img src=x onerror=alert(1)> \"quoted\""
        body["notes"] = "Line one\n<b>line two</b>"

    body, facts = saved(edit=edit)
    markup = document.build_html(body=body, financial_totals=facts)
    assert "<script" not in markup and "<img" not in markup and "<b>line two" not in markup
    assert "&lt;/style&gt;&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; Sons" in markup
    # The stylesheet is constant: names reach the running header and footer through the page, not through CSS text.
    assert markup.count("<style>") == 1 and re.search(r"<style>(.*?)</style>", markup, flags=re.S).group(1) == document.CSS
    assert "string-set: owner content(text)" in document.CSS and "Sons" not in document.CSS


def test_a_client_submission_says_so_and_shows_no_review():
    body, facts = saved(client=True)
    text = words(document.build_html(body=body, financial_totals=facts))
    assert "Client submission · awaiting staff review" in text and "Saved by staff" not in text
    assert "11 proposed" in text and "This version was submitted by the business and is awaiting staff review" in text
    # Nothing reviewed: the reviewed figures are the baseline's.
    assert "EBITDA +$205,274 −$22,377 → $182,897 Reviewed changes only −$22,377" in text


def test_without_saved_loan_terms_after_is_measured_against_todays_payments():
    body, facts = saved(terms=False)
    text = words(document.build_html(body=body, financial_totals=facts))
    assert "No loan terms are saved, so after figures are measured against today's debt payments." in text
    assert "DSCR +0.26x −0.03x → 0.24x" in text and "Debt service $0 $777,600 → $777,600" in text and "Below 1.00x" in text
    assert "Loan terms on file" not in text and "The saved loan terms release" not in text
    # Uses were entered against released cash that these figures do not have: said plainly, not as "$0 released".
    assert "Over-assigned by $14,200 / mo No loan terms are saved on this file, so no cash is released to assign; $14,200 a month is listed below." in text
    assert "No loan terms are saved, so debt service, DSCR and free cash flow use today's debt payments." in text


def test_corrections_and_carried_over_items_print_where_they_belong():
    def edit(body):
        body["source"]["corrections"] = {"rent": {"amount": 190000, "reason": "The return includes a one-time deposit that will not recur."}}
        body["source"]["confirmations"]["rent"] = "corrected"
        body["carried_over"] = [{"label": "Google Ads", "detail": "Re-enter it as a growth activity.", "annual_amount": 12000, "kind": "change"},
                                {"label": "Old payroll cut", "detail": "Already entered.", "annual_amount": -5000, "kind": "change", "resolved": True}]
        body["notes"] = "Second bay lease amendment is signed."

    body, facts = saved(edit=edit)
    text = words(document.build_html(body=body, financial_totals=facts))
    # The source figure stays as filed; the difference is the preparer's to read.
    assert "Rent and premises $196,805 Correction noted" in text and "1 correction noted by the business is listed under the tax preparer handoff in part 6" in text
    assert "Rent and premises The return includes a one-time deposit that will not recur. $196,805 $190,000" in text
    assert "Carried over from the earlier plan" in text and "Google Ads Re-enter it as a growth activity. +$12,000" in text and "Old payroll cut" not in text
    assert "Second bay lease amendment is signed." in text
    # A correction never moves a figure.
    assert "= EBITDA −$22,377" in text and "Costs on the source $1,266,777" in text


def test_a_version_without_figures_still_prints_its_plan():
    markup = document.build_html(body=schema.empty_body(), financial_totals={})
    text = words(markup)
    assert markup.count("<section class='part") == 6 and "No figures were stored with this version." in text
    assert "No changes proposed." in text and "No revenue stream is described in this plan." in text and "Business name not supplied" in text
    # The plan as written is still there when only its figures are missing.
    body, _facts = saved()
    text = words(document.build_html(body=body, financial_totals={}))
    assert "Detail packages" in text and "Detail supplies and chemicals" in text and "The business confirmed these disclosures and proposals." in text


def test_a_long_plan_keeps_every_change_and_every_cost():
    def edit(body):
        other = next(line for line in body["lines"] if line["group"] == "other_deductions")
        for index in range(30):
            body["lines"].append({"id": f"x{index}", "group": "other_deductions", "label": f"Equipment lease {index + 1}", "base": 50, "behavior": "fixed", "link": "all", "added": True})
            other["base"] -= 50
            body["changes"].append({"id": str(uuid4()), "target": {"kind": "line", "line_id": f"x{index}"}, "direction": "down", "mode": "percent", "value": 10,
                                    "start_month": 1 + index % 12, "note": f"Renegotiated contract {index + 1}."})

    body, facts = saved(edit=edit)
    markup = document.build_html(body=body, financial_totals=facts)
    text = words(markup)
    assert all(f"Renegotiated contract {index + 1}." in text for index in range(30)) and "41 changes" in text
    # A cost split out of a group says which group it belongs to; the group still adds up.
    assert text.count("Part of Other") == 30 and "Costs on the source $1,266,777" in text
    # Every band heads at least one row, and is grouped with it so a page never ends on a heading.
    assert markup.count("<tbody class='keep'><tr class='band'>") == markup.count("<tr class='band'>") > 0


def test_the_fallback_engine_prints_the_plain_document():
    import fitz

    body, facts = saved()
    with patch.dict(sys.modules, {"weasyprint": None}):
        pdf = document.render_pdf(body=body, financial_totals=facts)
    pages = fitz.open(stream=pdf, filetype="pdf")
    text = " ".join(page.get_text() for page in pages)
    assert pdf.startswith(b"%PDF") and "Operating expenses and earnings plan - Page 1 of" in text
    assert "Harbor Auto Detail LLC" in text and "$182,897" in text and "1.25x coverage" in text and "$39,103" in text
    # The plain document keeps the same facts in tables, with nothing a reviewer wrote.
    markup = plain.build_html(body=body, financial_totals=facts)
    for line in ("IRS 1120-S · 2025", "Plan starts Jan 2027", "-$22,377", "$182,897", "1.03x", "$177,600", "Still to find for 1.25x coverage: <b>$39,103</b>",
                 "Revenue drivers", "+$89,616", "Lease amendment on bay 2 signed in October 2026.", "First-year EBITDA $145,663", "Goal $215,000", "release $50,000 a month"):
        assert line in markup, line
    assert "No changes proposed." in plain.build_html(body=schema.empty_body(), financial_totals={})


def test_the_designed_document_gives_each_part_its_own_page():
    weasyprint_or_skip()
    import fitz

    body, facts = saved()
    pdf = document.render_pdf(body=body, financial_totals=facts)
    pages = fitz.open(stream=pdf, filetype="pdf")
    texts = [page.get_text() for page in pages]
    assert pages.metadata["producer"].startswith("WeasyPrint") and pages.metadata["title"] == "Operating expenses and earnings plan · Harbor Auto Detail LLC · Version 3"
    starts = [next(index for index, text in enumerate(texts) if f"PART {number} OF 6" in text.upper()) for number in range(2, 7)]
    assert starts == sorted(set(starts)) and starts[0] >= 1
    assert "Operating Expenses and Earnings Plan" in texts[0] and f"Page 1 of {len(pages)}" in texts[0]
    # The running footer names the business and the version on every page; the header names the part after the cover.
    assert all("Harbor Auto Detail LLC · Operating expenses and earnings plan · Version 3" in text for text in texts)
    assert "QUALIFIED COMMERCIAL" in texts[starts[0]] and "Source figures" in texts[starts[0]]
    fonts = {font[3].split("+")[-1] for page in pages for font in page.get_fonts()}
    if fonts <= {"DejaVu-Sans", "DejaVu-Sans-Bold"}:
        # With the fonts the service image has, the design's sample plan is exactly six pages.
        assert len(pages) == 6 and starts == [1, 2, 3, 4, 5]


def test_a_long_designed_document_flows_over_pages_without_losing_rows():
    weasyprint_or_skip()
    import fitz

    def edit(body):
        other = next(line for line in body["lines"] if line["group"] == "other_deductions")
        for index in range(30):
            body["lines"].append({"id": f"x{index}", "group": "other_deductions", "label": f"Equipment lease {index + 1}", "base": 50, "behavior": "fixed", "link": "all", "added": True})
            other["base"] -= 50
            body["changes"].append({"id": str(uuid4()), "target": {"kind": "line", "line_id": f"x{index}"}, "direction": "down", "mode": "percent", "value": 10,
                                    "start_month": 1 + index % 12, "note": f"Renegotiated contract {index + 1}. " + "The signed amendment is on file. " * 6})

    body, facts = saved(edit=edit)
    pages = fitz.open(stream=document.render_pdf(body=body, financial_totals=facts), filetype="pdf")
    text = " ".join(page.get_text() for page in pages)
    assert len(pages) > 6 and all(f"Renegotiated contract {index + 1}." in text for index in range(30))
    # The table heading repeats on each page the changes run over.
    assert text.count("CHANGE AND WHY IT WILL WORK") >= 3


def test_a_document_is_drawn_from_the_version_and_not_from_the_file():
    body, facts = saved()
    before = deepcopy((body, facts))
    first = document.build_html(body=body, financial_totals=facts)
    # Drawing changes nothing it was given, and reads no file, so the same version always gives the same document.
    assert (body, facts) == before and first == document.build_html(body=deepcopy(body), financial_totals=deepcopy(facts))
    assert item_id("l:rent") not in first


def test_a_change_in_dollars_a_year_reads_as_dollars_a_year():
    def edit(body):
        card = next(row for row in body["changes"] if row["id"] == item_id("l:o1"))
        card.update(mode="annual", value=4_720)

    body, facts = saved(edit=edit)
    text = words(document.build_html(body=body, financial_totals=facts))
    # The cost follows revenue, so the dollars are measured on the source's revenue and the plan figure carries the plan's.
    assert "Card processing Decrease by $4,720 a year at today's volume · $24,287 a year in the plan, with the volume it follows" in text
    # A fixed cost says its dollars plainly, and a rate change keeps its own unit.
    assert "Rent Decrease by $28,805 a year · $196,805 → $168,000 a year" in text
    untouched, plain_facts = saved()
    assert "Card processing Decrease by 0.4 pts" in words(document.build_html(body=untouched, financial_totals=plain_facts))
