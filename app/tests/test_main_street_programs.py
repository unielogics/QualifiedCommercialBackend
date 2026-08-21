"""Unit tests for the Main Street intake service module.

Pure functions over plain mappings — no DB, no fixtures beyond dicts. The two
things worth guarding hardest are (1) that non-lending intents never get dragged
into a lending screen, and (2) that the borrower-safe redaction is an allowlist
that cannot leak a new signal added upstream.
"""

from __future__ import annotations

import pytest

from app.services.main_street_programs import (
    BORROWER_SUGGESTABLE_PROGRAMS,
    MAIN_STREET_INDUSTRIES,
    MAIN_STREET_INTENTS,
    MAIN_STREET_PROGRAM_LABELS,
    MAIN_STREET_REQUIRED_DOCUMENTS,
    borrower_safe_programs,
    compute_main_street_program_fit,
    documents_for,
    intent_kind,
    normalize_industry,
    normalize_intent,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _docs(*names: str) -> list[dict[str, object]]:
    return [{"name": n, "status": "received"} for n in names]


FULL_PACKAGE = _docs(
    "Last 6 months business bank statements",
    "Last 2 years business tax returns",
    "Year-to-date P&L and balance sheet",
    "Business debt schedule",
    "Owner personal financial statement",
)

STRONG_METRICS = {
    "ytd_annualized_revenue": 900_000,
    "estimated_dscr": 1.6,
    "estimated_ebitda_or_cash_flow": 180_000,
    "bank_statement_months": 6,
    "tax_return_years": 2,
    "nsf_or_overdraft_count": 0,
}

STRONG_DETAILS = {"years_in_business": 6, "estimated_credit_score": 720}


def _fit(**over):
    kwargs = {
        "intent": "working_capital",
        "industry": "other",
        "key_metrics": STRONG_METRICS,
        "details": STRONG_DETAILS,
        "documents": FULL_PACKAGE,
    }
    kwargs.update(over)
    return compute_main_street_program_fit(**kwargs)


# ── taxonomies ───────────────────────────────────────────────────────────────


def test_unknown_intent_falls_back_to_not_sure_not_a_product():
    # Guessing a specific product here would seed the wrong document package.
    assert normalize_intent("nonsense") == "not_sure"
    assert normalize_intent(None) == "not_sure"
    assert normalize_intent("") == "not_sure"
    assert normalize_intent("merchant_services") == "merchant_services"


def test_unknown_industry_falls_back_to_other():
    assert normalize_industry("nope") == "other"
    assert normalize_industry(None) == "other"
    assert normalize_industry("trucking_logistics") == "trucking_logistics"


def test_every_industry_carries_both_languages():
    for slug, row in MAIN_STREET_INDUSTRIES.items():
        assert row.get("en"), slug
        assert row.get("es"), slug


def test_every_intent_declares_a_kind_and_both_languages():
    for slug, row in MAIN_STREET_INTENTS.items():
        assert row["kind"] in {"lending", "non_lending", "route_out"}, slug
        assert row.get("en") and row.get("es"), slug
        if row["kind"] == "route_out":
            assert row.get("route"), f"{slug} must name the funnel it hands off to"


def test_intent_kind_classifies_the_non_lending_products():
    # These two decide whether a document package and a fundability verdict
    # apply at all, so the classification is load-bearing.
    assert intent_kind("merchant_services") == "non_lending"
    assert intent_kind("business_systems") == "non_lending"
    assert intent_kind("working_capital") == "lending"
    assert intent_kind("not_sure") == "lending"
    assert intent_kind("property") == "route_out"
    # An unknown intent must never be classified as non-lending, which would
    # silently skip the document package.
    assert intent_kind("garbage") == "lending"


# ── document packages ────────────────────────────────────────────────────────


def test_baseline_names_preserve_the_readiness_keywords():
    """bucket_ai._baseline_key matches on these literal word pairs in the NAME.
    Renaming either row silently breaks lending-readiness scoring."""
    names = [row["name"].lower() for row in MAIN_STREET_REQUIRED_DOCUMENTS]
    assert any("bank" in n and "statement" in n for n in names)
    assert any("tax" in n and "return" in n for n in names)


def test_pfs_is_present_but_not_required_day_one():
    pfs = [r for r in MAIN_STREET_REQUIRED_DOCUMENTS if "personal financial" in r["name"].lower()]
    assert len(pfs) == 1
    assert pfs[0]["required"] is False


def test_merchant_services_asks_only_for_processing_statements():
    package = documents_for("merchant_services")
    assert len(package) == 1
    assert "processing" in package[0]["name"].lower()
    joined = " ".join(r["name"].lower() for r in package)
    assert "tax return" not in joined
    assert "bank statement" not in joined


def test_business_systems_asks_for_nothing():
    assert documents_for("business_systems") == []


def test_route_out_intents_seed_no_documents():
    assert documents_for("property") == []
    assert documents_for("dealership") == []


def test_lending_intent_gets_the_four_item_baseline():
    package = documents_for("working_capital", "other")
    required = [r for r in package if r["required"]]
    assert len(required) == 4


def test_industry_documents_layer_onto_lending_only():
    trucking = {r["name"] for r in documents_for("working_capital", "trucking_logistics")}
    assert any("operating authority" in n.lower() for n in trucking)
    assert any("fleet schedule" in n.lower() for n in trucking)
    # ...and never onto a non-lending file.
    merchant = {r["name"] for r in documents_for("merchant_services", "trucking_logistics")}
    assert not any("fleet schedule" in n.lower() for n in merchant)


def test_restaurant_is_not_shown_trucking_documents():
    names = " ".join(r["name"].lower() for r in documents_for("working_capital", "restaurant_food_service"))
    assert "dot" not in names
    assert "fleet" not in names
    assert "liquor license" in names


def test_intent_extra_documents_are_added():
    equipment = " ".join(r["name"].lower() for r in documents_for("equipment", "manufacturing"))
    assert "vendor quote" in equipment
    refi = " ".join(r["name"].lower() for r in documents_for("refinance_debt", "other"))
    assert "payoff" in refi


def test_document_names_are_unique_within_a_package():
    # restaurant + retail both carry merchant statements; dedupe must hold.
    for industry in MAIN_STREET_INDUSTRIES:
        names = [r["name"] for r in documents_for("working_capital", industry)]
        assert len(names) == len(set(names)), industry


# ── program fit: intent gating ───────────────────────────────────────────────


def test_business_systems_produces_no_program_fit_at_all():
    """No lending question to answer — and the caller keys the fundability
    banner, DSCR and LTV off this being non-empty."""
    assert _fit(intent="business_systems") == {}


def test_route_out_produces_no_program_fit():
    assert _fit(intent="property") == {}
    assert _fit(intent="dealership") == {}


def test_merchant_services_screens_only_merchant_and_never_touches_deposits():
    fit = compute_main_street_program_fit(
        intent="merchant_services",
        key_metrics={"annualized_card_volume": 400_000},
        documents=_docs("Merchant processing statements — last 3 months"),
    )
    assert set(fit) == {"merchant_processing"}
    assert fit["merchant_processing"]["eligible"] is True


def test_merchant_services_without_statements_asks_for_them():
    fit = compute_main_street_program_fit(intent="merchant_services", documents=[])
    row = fit["merchant_processing"]
    assert row["eligible"] is False
    assert any("processing statement" in n.lower() for n in row["needs"])


def test_merchant_screen_does_not_depend_on_bank_deposits():
    """The dealer implementation derives this from annualized deposits, which
    do not exist on a path that never collects bank statements."""
    fit = compute_main_street_program_fit(
        intent="merchant_services",
        key_metrics={"annualized_adjusted_deposits": 5_000_000},  # irrelevant here
        documents=_docs("Merchant processing statements — last 3 months"),
    )
    assert fit["merchant_processing"]["eligible"] is False  # no card volume known


# ── program fit: minimum profile ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "details,expected_blocker",
    [
        ({"years_in_business": 0.5, "estimated_credit_score": 720}, "time in business"),
        ({"years_in_business": 6, "estimated_credit_score": 500}, "credit score"),
    ],
)
def test_minimum_profile_blocks_every_borrower_facing_program(details, expected_blocker):
    fit = _fit(details=details)
    for key in BORROWER_SUGGESTABLE_PROGRAMS:
        assert fit[key]["eligible"] is False, key
        assert any(expected_blocker in b for b in fit[key]["blocked_by"]), key


def test_low_revenue_blocks_on_the_published_minimum():
    fit = _fit(key_metrics={**STRONG_METRICS, "ytd_annualized_revenue": 50_000})
    assert any("annual revenue" in b for b in fit["line_of_credit"]["blocked_by"])


def test_booleans_are_not_accepted_as_numbers():
    """bool is an int in Python; a True revenue must not read as 1."""
    fit = _fit(key_metrics={**STRONG_METRICS, "ytd_annualized_revenue": True})
    assert fit["term_loan_3_5_year"]["eligible"] is False


# ── program fit: per-program rules ───────────────────────────────────────────


def test_strong_file_reaches_term_and_line():
    fit = _fit()
    assert fit["line_of_credit"]["eligible"] is True
    assert fit["term_loan_3_5_year"]["eligible"] is True
    assert fit["term_loan_10_year"]["eligible"] is True


def test_line_of_credit_screens_on_a_lighter_document_bar_than_term():
    """Three months of statements is enough for a line, not for a term loan."""
    fit = _fit(
        key_metrics={
            **STRONG_METRICS,
            "bank_statement_months": 3,
            "tax_return_years": 0,
            "estimated_dscr": None,
        }
    )
    assert fit["line_of_credit"]["eligible"] is True
    assert fit["term_loan_3_5_year"]["eligible"] is False


def test_excess_overdraft_activity_blocks_the_line():
    fit = _fit(key_metrics={**STRONG_METRICS, "nsf_or_overdraft_count": 9})
    assert fit["line_of_credit"]["eligible"] is False
    assert any("overdraft" in b for b in fit["line_of_credit"]["blocked_by"])


def test_jumbo_needs_scale_and_a_large_request():
    assert _fit()["jumbo_term_loan"]["eligible"] is False  # strong, but not jumbo scale
    fit = _fit(
        key_metrics={
            **STRONG_METRICS,
            "ytd_annualized_revenue": 9_000_000,
            "requested_amount": 4_000_000,
        }
    )
    assert fit["jumbo_term_loan"]["eligible"] is True


def test_equipment_requires_a_stated_equipment_purchase():
    assert _fit()["equipment_financing"]["eligible"] is False
    fit = _fit(intent="equipment", documents=[*FULL_PACKAGE, *_docs("Vendor quote for the equipment")])
    assert fit["equipment_financing"]["eligible"] is True
    assert fit["equipment_financing"]["needs"] == []


def test_transportation_finance_is_industry_gated():
    assert _fit(industry="restaurant_food_service")["transportation_finance"]["eligible"] is False
    fit = _fit(
        industry="trucking_logistics",
        documents=[*FULL_PACKAGE, *_docs("MC or DOT operating authority letter")],
    )
    assert fit["transportation_finance"]["eligible"] is True


def test_sba_does_not_flip_eligible_without_a_pfs():
    """The dealer shortcut is 'no missing REQUIRED docs', and the Main Street
    PFS is optional — so SBA must carry its own explicit document test."""
    without_pfs = _docs(
        "Last 6 months business bank statements",
        "Last 2 years business tax returns",
        "Year-to-date P&L and balance sheet",
        "Business debt schedule",
    )
    fit = _fit(documents=without_pfs)
    assert fit["sba"]["eligible"] is False
    assert any("personal financial statement" in n.lower() for n in fit["sba"]["needs"])
    assert _fit()["sba"]["eligible"] is True


def test_sba_sector_programs_are_industry_gated():
    grocery = _fit(industry="grocery_commodities")
    assert grocery["sba_grocery"]["eligible"] is True
    assert grocery["sba_made_in_america"]["eligible"] is False

    mfg = _fit(industry="manufacturing")
    assert mfg["sba_made_in_america"]["eligible"] is True
    assert mfg["sba_grocery"]["eligible"] is False


def test_reinsurance_is_never_computed_for_main_street():
    assert "reinsurance_backed" not in _fit()


def test_every_industry_screens_without_raising():
    for industry in MAIN_STREET_INDUSTRIES:
        fit = _fit(industry=industry)
        assert fit, industry
        for key, row in fit.items():
            assert isinstance(row["eligible"], bool), (industry, key)


def test_empty_inputs_do_not_raise():
    fit = compute_main_street_program_fit(intent="working_capital")
    assert all(row["eligible"] is False for row in fit.values() if "eligible" in row)


# ── borrower-safe redaction ──────────────────────────────────────────────────


def test_redaction_emits_only_the_four_safe_keys():
    safe = borrower_safe_programs(_fit())
    assert safe, "a strong file should surface something"
    for row in safe:
        assert set(row) == {"key", "label", "why", "still_needed"}


def test_redaction_drops_a_signal_added_upstream():
    """Allowlist on the way out — a new internal field on a program row must
    not leak just because nobody updated a denylist."""
    fit = _fit()
    fit["line_of_credit"]["indicative_rate"] = 9.99
    fit["line_of_credit"]["approval_probability"] = 0.87
    dumped = repr(borrower_safe_programs(fit))
    assert "9.99" not in dumped
    assert "0.87" not in dumped
    assert "indicative_rate" not in dumped


def test_internal_only_programs_are_never_suggestable():
    for key in ("debt_consulting", "merchant_processing", "transportation_factoring", "real_estate_backed"):
        assert key not in BORROWER_SUGGESTABLE_PROGRAMS
    fit = _fit(key_metrics={**STRONG_METRICS, "estimated_dscr": 0.4})
    assert fit["debt_consulting"]["eligible"] is True
    assert all(row["key"] != "debt_consulting" for row in borrower_safe_programs(fit))


def test_ineligible_programs_are_not_suggested():
    safe = borrower_safe_programs(_fit(details={"years_in_business": 0.2, "estimated_credit_score": 500}))
    assert safe == []


def test_the_two_term_bands_collapse_into_one_card():
    """They are separate products with separate names, so the old dedupe on
    label text would no longer catch them. The collapse is by key group now, and
    this asserts the behaviour rather than the label coincidence that used to
    imply it: a file eligible for both must still spend only one card on them."""
    fit = _fit()
    assert fit["term_loan_3_5_year"]["eligible"] is True
    assert fit["term_loan_10_year"]["eligible"] is True

    keys = [row["key"] for row in borrower_safe_programs(fit)]
    assert keys.count("term_loan_3_5_year") + keys.count("term_loan_10_year") == 1

    labels = [row["label"] for row in borrower_safe_programs(fit)]
    assert len(labels) == len(set(labels))


def test_the_two_term_bands_are_named_separately():
    """A shared label was what let the old collapse work; if these ever merge
    again the test above stops proving anything."""
    assert (
        MAIN_STREET_PROGRAM_LABELS["term_loan_3_5_year"]
        != MAIN_STREET_PROGRAM_LABELS["term_loan_10_year"]
    )


# ── program boundaries: the lender's published sheet ─────────────────────────


@pytest.mark.parametrize("fico,eligible", [(659, False), (660, True)])
def test_term_bands_require_660_where_the_house_floor_is_640(fico, eligible):
    """The per-program FICO minimum must bite without dragging the shared floor
    up with it — a 650 file still reaches a line of credit."""
    fit = _fit(details={**STRONG_DETAILS, "estimated_credit_score": fico})
    assert fit["term_loan_3_5_year"]["eligible"] is eligible
    assert fit["term_loan_10_year"]["eligible"] is eligible
    assert fit["line_of_credit"]["eligible"] is True


@pytest.mark.parametrize("tib,eligible", [(1.99, False), (2.0, True)])
def test_term_bands_require_two_filed_years(tib, eligible):
    fit = _fit(details={**STRONG_DETAILS, "years_in_business": tib})
    assert fit["term_loan_3_5_year"]["eligible"] is eligible
    assert fit["term_loan_10_year"]["eligible"] is eligible


@pytest.mark.parametrize("dscr,eligible", [(1.09, False), (1.10, True)])
def test_ten_year_band_screens_coverage_at_one_point_one(dscr, eligible):
    fit = _fit(key_metrics={**STRONG_METRICS, "estimated_dscr": dscr})
    assert fit["term_loan_10_year"]["eligible"] is eligible


@pytest.mark.parametrize("revenue,eligible", [(49_999, False), (50_000, True)])
def test_ez_term_funds_from_fifty_thousand_of_revenue(revenue, eligible):
    """The old screen sat at $150K and the house floor at $100K, so a $60K file
    saw nothing at all. Revenue this low still reaches the 3-5 year band."""
    fit = _fit(key_metrics={**STRONG_METRICS, "ytd_annualized_revenue": revenue})
    assert fit["term_loan_3_5_year"]["eligible"] is eligible


def test_lowering_the_floor_did_not_loosen_the_programs_that_leaned_on_it():
    """Equipment, transportation and the SBA rows had no revenue test of their
    own and were gated entirely by the shared floor. At $60K they must still be
    ineligible, and must say why."""
    fit = _fit(
        key_metrics={**STRONG_METRICS, "ytd_annualized_revenue": 60_000},
        details={**STRONG_DETAILS, "financing_equipment_or_vehicle": True},
        industry="trucking_logistics",
    )
    for key in ("equipment_financing", "transportation_finance", "sba", "sba_grocery"):
        assert fit[key]["eligible"] is False, key
        assert any("annual revenue" in b for b in fit[key]["blocked_by"]), key


@pytest.mark.parametrize(
    "requested,eligible",
    [(24_999, False), (25_000, True), (500_000, True), (500_001, False)],
)
def test_ez_term_publishes_an_amount_band(requested, eligible):
    fit = _fit(key_metrics={**STRONG_METRICS, "requested_amount": requested})
    assert fit["term_loan_3_5_year"]["eligible"] is eligible


@pytest.mark.parametrize(
    "requested,eligible",
    [(14_999, False), (15_000, True), (50_000, True), (50_001, False)],
)
def test_microcap_publishes_an_amount_band(requested, eligible):
    fit = _fit(key_metrics={**STRONG_METRICS, "requested_amount": requested})
    assert fit["term_loan_10_year"]["eligible"] is eligible


def test_an_unnamed_amount_does_not_disqualify():
    """A fresh intake has not stated a number yet. It must still see the
    programs it could reach, the same call the NSF screen makes."""
    metrics = {k: v for k, v in STRONG_METRICS.items() if k != "requested_amount"}
    fit = _fit(key_metrics=metrics)
    assert fit["term_loan_3_5_year"]["eligible"] is True
    assert fit["term_loan_10_year"]["eligible"] is True


@pytest.mark.parametrize("requested,eligible", [(50_000, True), (50_001, False)])
def test_microcap_caps_the_loan_at_half_of_revenue(requested, eligible):
    """Revenue sizes this loan rather than gating it: at $100K of sales the cap
    is $50K, and a dollar more is outside the program."""
    fit = _fit(
        key_metrics={
            **STRONG_METRICS,
            "ytd_annualized_revenue": 100_000,
            "requested_amount": requested,
        }
    )
    assert fit["term_loan_10_year"]["eligible"] is eligible


@pytest.mark.parametrize("nsf,eligible", [(2, True), (3, False)])
def test_microcap_screens_overdrafts_tighter_than_the_line(nsf, eligible):
    fit = _fit(key_metrics={**STRONG_METRICS, "nsf_or_overdraft_count": nsf})
    assert fit["term_loan_10_year"]["eligible"] is eligible
    assert fit["line_of_credit"]["eligible"] is True


@pytest.mark.parametrize("sector", ["trucking_logistics", "restaurant_food_service"])
def test_microcap_excludes_sectors_the_sba_sheet_excludes(sector):
    fit = _fit(industry=sector)
    assert fit["term_loan_10_year"]["eligible"] is False
    assert any("sector" in b for b in fit["term_loan_10_year"]["blocked_by"])
    # The 3-5 year band has no such restriction.
    assert fit["term_loan_3_5_year"]["eligible"] is True


def test_never_more_than_three_suggestions():
    assert len(borrower_safe_programs(_fit(industry="grocery_commodities"))) <= 3


def test_every_suggestable_program_has_a_hand_written_rationale():
    """The model relays these; it never authors them."""
    for key in BORROWER_SUGGESTABLE_PROGRAMS:
        assert key in MAIN_STREET_PROGRAM_LABELS
    fit = {k: {"eligible": True, "needs": []} for k in BORROWER_SUGGESTABLE_PROGRAMS}
    for row in borrower_safe_programs(fit):
        assert row["why"], row["key"]


def test_redaction_handles_none_and_garbage():
    assert borrower_safe_programs(None) == []
    assert borrower_safe_programs({}) == []
    assert borrower_safe_programs({"line_of_credit": "not a mapping"}) == []
