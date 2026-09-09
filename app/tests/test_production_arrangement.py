"""The formula port is checked against the design's SEED values, worked by hand."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC
from decimal import Decimal

import pytest

from app.services import production_arrangement as pa

# The design's seed: Delgado Auto Group, 96 retail units a month.
SEED_PRODUCTS = {
    "vsc": {"on": True, "cur_rate": 54, "cur_premium": 2150, "rate": 62, "premium": 2400, "repay": 420, "comm": 14, "admin": 260, "retention": 38, "term": 36, "base": 1300, "other": 120, "markup": 300},
    "gap": {"on": True, "cur_rate": 36, "cur_premium": 795, "rate": 41, "premium": 895, "repay": 150, "comm": 16, "admin": 110, "retention": 46, "term": 36, "base": 480, "other": 55, "markup": 100},
    "theft": {"on": True, "cur_rate": 19, "cur_premium": 545, "rate": 24, "premium": 595, "repay": 95, "comm": 18, "admin": 70, "retention": 52, "term": 24, "base": 330, "other": 40, "markup": 60},
    "appearance": {"on": True, "cur_rate": 14, "cur_premium": 725, "rate": 19, "premium": 795, "repay": 120, "comm": 18, "admin": 95, "retention": 50, "term": 24, "base": 440, "other": 50, "markup": 90},
    "key": {"on": False, "cur_rate": 9, "cur_premium": 329, "rate": 12, "premium": 349, "repay": 55, "comm": 20, "admin": 45, "retention": 55, "term": 24, "base": 190, "other": 20, "markup": 39},
    "tire": {"on": True, "cur_rate": 17, "cur_premium": 645, "rate": 22, "premium": 699, "repay": 110, "comm": 17, "admin": 85, "retention": 48, "term": 24, "base": 380, "other": 44, "markup": 80},
    "maint": {"on": False, "cur_rate": 11, "cur_premium": 849, "rate": 15, "premium": 899, "repay": 0, "comm": 15, "admin": 120, "retention": 44, "term": 12, "base": 600, "other": 60, "markup": 119},
    "power": {"on": False, "cur_rate": 6, "cur_premium": 1425, "rate": 8, "premium": 1495, "repay": 0, "comm": 15, "admin": 180, "retention": 40, "term": 36, "base": 1000, "other": 100, "markup": 215},
}


def seed() -> dict:
    arr = pa.empty_arrangement()
    arr.update({
        "dealer_name": "Delgado Auto Group LLC", "dealer_state": "TX", "dealer_entity": "Limited liability company",
        "dealer_dba": "Delgado Auto Sales", "dealer_address": "4411 Gulf Freeway, Houston TX 77023",
        "dealer_signer_name": "Rafael Delgado", "dealer_signer_title": "Managing member",
        "sponsor_name": "Acme Warranty Administrators Inc", "sponsor_state": "NV", "sponsor_entity": "Corporation",
        "sponsor_platform": "AcmeAdmin", "sponsor_email": "notices@acme.example",
        "rm_name": "Marisol Vega", "rm_email": "mvega@qualifiedcommercial.com", "rm_phone": "(973) 555-0148",
        "lot_units": 142, "avg_cost": 21800, "monthly_units": 96, "cancels": 4, "chargebacks": 2,
        "base_from": "2025-09-01", "base_through": "2026-08-31",
        "evidence": ["DMS unit reports", "Sponsor production reports", "Bank statements (Plaid)"],
        "requested": 1200000, "min_activation": 900000, "term": 36, "dealer_cof": 14.5, "exclusivity": 45,
        "bank_cof": 0.5, "orig_cost": 34000, "prof_fees": 46000, "mgmt_fee": 3200, "loss_prov": 1.5,
        "debt_service": 41300, "fund_target": 100, "cure_days": 5, "adj_value": 200,
        # Deep-copied: SEED_PRODUCTS is module-level, and a test that switches a
        # product off was leaking that into every test that ran after it.
        "products": copy.deepcopy(SEED_PRODUCTS),
    })
    return arr


def test_jsround_matches_javascript_math_round():
    assert pa.jsround(59.52) == 60
    assert pa.jsround(0.5) == 1
    assert pa.jsround(2.5) == 3  # Python's round(2.5) is 2; JS gives 3
    assert pa.jsround(-2.5) == -2


def test_product_econ_seed_values():
    vsc = pa.product_econ(96, "vsc", SEED_PRODUCTS["vsc"])
    assert vsc.contracts == 60          # round(96 * 0.62) = round(59.52)
    assert vsc.cur_contracts == 52      # round(96 * 0.54) = round(51.84)
    assert vsc.gross == 60 * 2400
    assert vsc.comm == pytest.approx(336.0)
    assert vsc.reserve == pytest.approx((2400 - 420 - 336 - 260) * 0.38)
    assert vsc.uplift == 250
    assert vsc.d_contracts == 8
    assert vsc.d_gross == 60 * 2400 - 52 * 2150


def test_reserve_floors_at_zero_when_premium_is_eaten():
    row = pa.product_econ(96, "gap", {"on": True, "rate": 41, "premium": 100, "repay": 150, "comm": 16, "admin": 110, "retention": 46})
    assert row.reserve == 0


def test_portfolio_ignores_off_products_and_sums_repayment():
    e = pa.portfolio_econ(96, SEED_PRODUCTS)
    assert [r.key for r in e.on] == ["vsc", "gap", "theft", "appearance", "tire"]
    # 60*420 + 39*150 + 23*95 + 18*120 + 21*110
    assert e.repay_m == 25200 + 5850 + 2185 + 2160 + 2310
    assert e.max_term == 36
    assert pa.portfolio_econ(96, {}).max_term == 12


def test_pv_annuity_matches_closed_form_and_zero_rate():
    assert pa.pv_annuity(1000, 0, 12) == 12000
    r = 0.12 / 12
    expected = 1000 * ((1 - (1 + r) ** -12) / r)
    assert pa.pv_annuity(1000, 12, 12) == pytest.approx(expected)


def test_irr_recovers_a_known_rate_and_refuses_impossible_streams():
    pv = pa.pv_annuity(1000, 14.5, 36)
    assert pa.irr_annual_pct(1000, 36, pv) == pytest.approx(14.5, abs=1e-6)
    assert pa.irr_annual_pct(0, 36, 100) == 0
    assert pa.irr_annual_pct(100, 10, 5000) == 0  # 1000 repaid against 5000 advanced


def test_advance_backsolve_and_spread_clear_on_seed():
    arr = seed()
    c = pa.compute(arr)
    adv = c["advance"]
    assert adv["sizing"] == "backsolve"
    assert adv["implied_rate"] == 14.5
    assert adv["supported"] == pytest.approx(pa.pv_annuity(37705, 14.5, 36))
    assert adv["advance"] == adv["supported"]
    assert adv["mgmt_total"] == 3200 * 36
    assert adv["total_cost"] == pytest.approx(adv["bank_cost"] + 34000 + 46000 + adv["mgmt_total"] + adv["loss_cost"])
    assert adv["clears"] is True
    assert adv["spread"] >= 3
    shares = [line["share_pct"] for line in adv["cost_lines"]]
    assert sum(shares) == pytest.approx(100)


def test_fixed_sizing_uses_irr_and_flags_a_deal_that_does_not_clear():
    arr = seed()
    arr["sizing"] = "fixed"
    arr["requested"] = 5_000_000  # 37,705 x 36 cannot repay it
    c = pa.compute(arr)
    assert c["advance"]["implied_rate"] == 0
    assert c["advance"]["clears"] is False
    titles = [a["title"] for a in c["attention"]]
    assert "The programme costs more than it returns" in titles


def test_thresholds_follow_the_a3_guideline_and_overrides_win():
    c = pa.compute(seed())
    rows = {r["key"]: r for r in c["thresholds"]["rows"] if r.get("editable")}
    assert rows["units"]["operative"] == pa.jsround(96 * 0.85)
    assert rows["vsc_count"]["operative"] == pa.jsround(60 * 0.85)
    assert rows["vsc_pen"]["operative"] == pa.jsround(62 * 0.85)
    assert rows["vsc_pen3"]["operative"] == pa.jsround(62 * 0.9)
    assert rows["remittance"]["operative"] == pa.jsround(41300 * 1.25)
    assert rows["debt_service"]["operative"] == 41300
    fixed = {r["key"]: r["value"] for r in c["thresholds"]["rows"] if not r.get("editable")}
    assert fixed == {"coverage": "125%", "routing": "100%", "reporting": "Fifth business day", "commencement": "Set at closing"}
    arr = seed()
    arr["thresholds"] = {"units": 90}
    c2 = pa.compute(arr)
    rows2 = {r["key"]: r for r in c2["thresholds"]["rows"] if r.get("editable")}
    assert rows2["units"]["operative"] == 90 and rows2["units"]["overridden"] is True
    rolling = {r["label"]: r["value"] for r in c2["thresholds"]["rolling"]}
    assert rolling["Retail units"] == 270


def test_blank_threshold_override_is_flagged_but_not_debt_service():
    arr = seed()
    arr["thresholds"] = {"units": 0, "remittance": 0}
    c = pa.compute(arr)
    keys = [a["key"] for a in c["attention"]]
    assert "thresholds.units" in keys
    assert "thresholds.remittance" not in keys


def test_remittance_covenant_short_on_seed():
    c = pa.compute(seed())
    assert c["thresholds"]["remittance_req"] == pa.jsround(41300 * 1.25)
    assert any(a["key"] == "remittance_coverage" for a in c["attention"])
    assert c["thresholds"]["coverage_pct"] == pytest.approx(37705 / 51625 * 100)


def test_reverse_solve_sums_to_target_and_dumps_remainder_on_biggest():
    e = pa.portfolio_econ(96, SEED_PRODUCTS)
    rows = pa.reverse_solve(e, 41300)
    total = sum(r["solve_repay"] * r["contracts"] for r in rows)
    assert total >= 41300
    biggest = max(rows, key=lambda r: r["contracts"])
    assert biggest["key"] == "vsc"
    for r in rows:
        assert r["needed"] - r["cur_premium"] == r["uplift"]
    assert pa.reverse_solve(pa.portfolio_econ(96, {}), 1000) == []


def test_buildout_scenarios_and_half_payment_attention():
    c = pa.compute(seed())
    b = c["buildout"]
    assert b["policy_funded"] == 37705
    assert b["loan_free"] is False
    assert b["out_of_pocket"] == 41300 - 37705
    assert b["scenarios"]["with"]["from_operations"] == 41300 - 37705
    assert b["scenarios"]["without"]["from_operations"] == 41300
    assert not any(a["key"] == "buildout" for a in c["attention"])
    arr = seed()
    arr["debt_service"] = 100000
    c2 = pa.compute(arr)
    assert any(a["key"] == "buildout" for a in c2["attention"])


def test_projection_ramp_plateau_rolloff():
    c = pa.compute(seed())
    p = c["projection"]
    assert p["span"] == min(36 + 36, 48)
    assert len(p["bars"]) == p["span"]
    assert p["bars"][0]["repay"] == 37705
    assert p["bars"][-1]["repay"] == 0  # past the term, nothing originates
    assert p["totals"]["repay"] == 37705 * 36
    assert p["totals"]["comm"] == pytest.approx(c["econ"]["comm_m"] * 36)
    assert p["retire_month"] == min(36, -(-int(c["advance"]["advance"]) // 37705)) or p["retire_month"] <= 36


def test_required_rules_by_scope():
    arr = pa.empty_arrangement()
    pres = {a["key"] for a in pa.field_attention(arr, scope="presentation")}
    assert {"dealer_name", "sponsor_name", "monthly_units", "requested", "debt_service", "evidence"} <= pres
    assert "dealer_signer_name" not in pres
    one = {a["key"] for a in pa.field_attention(arr, scope="stage_one")}
    assert pres <= one
    assert {"dealer_signer_name", "sponsor_platform", "sponsor_email"} <= one
    assert "funding_party" not in one
    # cure_days carries a design default (5 days); blanking it flags it. The
    # exclusivity window is never blank now — the tier for the request supplies it.
    assert {"cure_days", "exclusivity"}.isdisjoint(one)
    cleared = {**arr, "cure_days": "", "exclusivity": 0}
    flagged = {a["key"] for a in pa.field_attention(cleared, scope="stage_one")}
    assert "cure_days" in flagged and "exclusivity" not in flagged
    two = {a["key"] for a in pa.field_attention(arr, scope="stage_two")}
    assert {"funding_party", "funded_amount", "maturity"} <= two


def test_non_zero_rule_and_blank_multiselect():
    arr = seed()
    arr["monthly_units"] = 0
    arr["evidence"] = ["", "  "]
    keys = {a["key"] for a in pa.field_attention(arr, scope="presentation")}
    assert "monthly_units" in keys and "evidence" in keys


def test_seed_is_send_ready_except_for_the_covenant_and_the_price():
    """The seed is an old-story deal: every covered product prices above what
    the dealer pays today. The covenant row is the only other thing open."""
    c = pa.compute(seed())
    keys = {a["key"] for a in c["attention"]}
    over = {f"products.{r['key']}.over" for r in c["econ"]["rows"] if r["on"]}
    assert all(r["savings"] < 0 for r in c["econ"]["rows"] if r["on"])
    assert keys == {"remittance_coverage"} | over
    assert all(a.get("owner") != "desk" for a in c["attention"])  # the price is the rep's to fix


def test_products_attention_rules():
    arr = seed()
    arr["products"]["vsc"]["on"] = False
    arr["products"]["gap"]["repay"] = 0
    c = pa.compute(arr)
    keys = {a["key"] for a in c["attention"]}
    assert "products.vsc.on" in keys and "products.gap.repay" in keys
    empty = pa.empty_arrangement()
    for k in pa.PRODUCT_KEYS:
        empty["products"][k]["on"] = False
    assert any(a["key"] == "products" for a in pa.compute(empty)["attention"])


def test_preview_rows_flag_blanks():  # 18 stage-one rows: Schedule A carries the breach fee and the TBD line now
    c = pa.compute(seed())
    one = c["preview"]["one"]
    assert len(one) == 18
    assert not any(r["blank"] for r in one)
    labels = [r["label"] for r in one]
    assert labels[0] == "Dealer legal name" and labels[-1] == "Evidence relied upon"
    two = c["preview"]["two"]
    blank_two = {r["label"] for r in two if r["blank"]}
    assert {"Funding party", "Funding date", "Original maturity date"} <= blank_two
    arr = seed()
    arr["sponsor_platform"] = ""
    row = next(r for r in pa.compute(arr)["preview"]["one"] if r["label"] == "Sponsor platform")
    assert row["blank"] and row["value"] == "Blank"


def test_normalize_and_merge_changes():
    base = pa.empty_arrangement()
    merged = pa.merge_changes(base, {
        "dealer_name": "  Delgado Auto Group LLC ", "lot_units": "142", "avg_cost": "21800.50",
        "evidence": "DMS unit reports, Tax returns", "bogus": "x",
        "products": {"vsc": {"rate": "62", "on": 1}, "nope": {"rate": 1}},
        "thresholds": {"units": "90", "junk": 1},
    })
    assert merged["dealer_name"] == "Delgado Auto Group LLC"
    assert merged["lot_units"] == 142 and merged["avg_cost"] == 21800.5
    assert merged["evidence"] == ["DMS unit reports", "Tax returns"]
    assert "bogus" not in merged
    assert merged["products"]["vsc"]["rate"] == 62 and merged["products"]["vsc"]["on"] is True
    assert merged["products"]["gap"]["term"] == 36  # untouched rows keep defaults
    assert "nope" not in merged["products"]
    assert merged["thresholds"] == {**{k: "" for k in pa.THRESHOLD_KEYS}, "units": 90}
    assert pa.normalize_changes({"lot_units": "abc"}) == {"lot_units": ""}


def test_snapshot_hash_is_stable_across_key_order_and_changes_on_edit():
    a = seed()
    b = json.loads(json.dumps(a))
    b = dict(reversed(list(b.items())))
    assert pa.snapshot_hash(a) == pa.snapshot_hash(b)
    b["dealer_name"] = "Someone else"
    assert pa.snapshot_hash(a) != pa.snapshot_hash(b)
    assert pa.snapshot_hash(a) != pa.snapshot_hash(a, extra={"sponsor": "x"})


def test_jsonable_stringifies_uuid_dates_and_decimals():
    from datetime import date, datetime
    uid = uuid.uuid4()
    out = pa.jsonable({"id": uid, "d": date(2026, 9, 3), "dt": datetime(2026, 9, 3, tzinfo=UTC),
                       "n": Decimal("1.5"), "nan": float("nan"), "list": [uid]})
    assert out["id"] == str(uid) and out["d"] == "2026-09-03" and out["dt"].startswith("2026-09-03")
    assert out["n"] == 1.5 and out["nan"] is None and out["list"] == [str(uid)]
    json.dumps(pa.canonical_snapshot(seed(), pa.compute(seed()), sponsor={"id": uid}, parties=None))


def test_compute_is_json_safe_on_an_empty_arrangement():
    c = pa.compute(None)
    json.dumps(c)
    assert c["econ"]["units"] == 0
    assert c["advance"]["advance"] == 0
    assert c["projection"]["retire_month"] is None



# ---- stage two: terms, funding rules, comparison ---------------------------

def _sheet(**over):
    base = {
        "approved_amount": 1000000, "min_activation_amount": 900000, "rate_pct": 12.5, "term_months": 36,
        "monthly_debt_service": round(pa.level_payment(1000000, 12.5, 36), 2), "funding_party_kind": "Lender",
        "funding_party_name": "First Bank", "facility_type": "Dealer capital advance",
        "expected_funding_date": "2026-09-10", "activation_date": "2026-09-10", "commencement_date": "2026-10-01",
        "maturity_date": "2029-09-10", "use_of_funds": {"inventory": 600000, "debt_payoff": 400000},
    }
    base.update(over)
    return base


def test_level_payment_inverts_pv_annuity_and_handles_zero_rate():
    pmt = pa.level_payment(1000000, 12.5, 36)
    assert pa.pv_annuity(pmt, 12.5, 36) == pytest.approx(1000000)
    assert pa.level_payment(1200, 0, 12) == 100
    assert pa.level_payment(1000, 5, 0) == 0


def test_validate_terms_catches_amounts_dates_and_allocation():
    assert pa.validate_terms(_sheet()) == []
    errs = pa.validate_terms(_sheet(min_activation_amount=2000000, activation_date="2026-09-01", maturity_date="2026-09-10",
                                    use_of_funds={"inventory": 1}, funding_party_name="", facility_type=""))
    joined = " ".join(errs)
    for needle in ("cannot exceed", "Activation date", "Maturity", "Use of funds", "Name the funding party", "facility type"):
        assert needle in joined, needle


def test_apply_term_sheet_fixes_the_advance_and_marks_the_lender_protected():
    arr, applied = pa.apply_term_sheet(seed(), _sheet())
    assert arr["requested"] == 1000000 and arr["sizing"] == "fixed" and arr["funded_amount"] == 1000000
    assert arr["dealer_cof"] == 12.5 and arr["term"] == 36 and arr["funding_party"] == "Lender"
    assert arr["funding_party_name"] == "First Bank" and arr["protected_1_name"] == "First Bank" and arr["protected_source"] == "First Bank"
    assert arr["use_of_funds"]["inventory"] == 600000 and arr["use_of_funds"]["other_label"] == ""
    assert set(pa.TERM_SHEET_KEYS) >= {"requested", "sizing", "funded_amount", "dealer_cof", "term", "debt_service", "use_of_funds"}
    assert "requested" in applied and applied["requested"]["before"] == 1200000
    c = pa.compute(arr, stage=2)
    assert c["advance"]["advance"] == 1000000 and c["advance"]["sizing"] == "fixed"


def test_compute_stage_two_reports_closing_blanks_and_funding_rules():
    arr, _ = pa.apply_term_sheet(seed(), _sheet())
    keys = {a["key"] for a in pa.compute(arr, stage=2)["attention"]}
    assert {"funding_docs_executed_date", "controlled_account", "ach_account", "identity_ein", "owners", "rm_comp_categories"} <= keys
    assert "funding_party" not in keys  # filled by the sheet
    # stage one never sees the closing keys
    assert not ({"controlled_account", "owners"} & {a["key"] for a in pa.compute(arr, stage=1)["attention"]})
    bad = {**arr, "activation_date": "2026-09-01", "funded_amount": 800000, "use_of_funds": {"inventory": 10}, "owners": [{"name": "A", "pct": 60}],
           "identity_naics": "12", "financing_cost_included": "Yes", "audit_discrepancy_threshold": 250}
    keys = {a["key"] for a in pa.compute(bad, stage=2)["attention"]}
    for k in ("activation_date", "funded_amount", "use_of_funds", "owners", "identity_naics", "financing_cost_explain", "audit_discrepancy_threshold"):
        assert k in keys, k


def test_steps_for_stage_and_new_step_keys():
    assert [s[0] for s in pa.steps_for(1)] == ["parties", "lot", "products", "advance", "buildout", "thresholds", "shortfall", "projection", "preview", "send"]
    assert "funding" in [s[0] for s in pa.steps_for(2)] and "disclosures" in [s[0] for s in pa.steps_for(2)]
    assert pa.FIELD_RULES_BY_KEY["funding_party"].step == "funding"


def test_normalize_rows_and_money_groups():
    out = pa.normalize_changes({
        "owners": [{"name": " Ana ", "pct": "60", "email": "a@x.com"}, {"name": "", "pct": ""}, {"name": "Bo", "pct": 40, "title": "CFO"}],
        "use_of_funds": {"inventory": "600000", "other": "", "other_label": " Signage "},
        "program_support": ["capital_health", "other"], "rm_comp_categories": "salary, hourly",
    })
    assert out["owners"] == [{"name": "Ana", "pct": 60, "title": "", "email": "a@x.com", "phone": "", "auth": ""},
                             {"name": "Bo", "pct": 40, "title": "CFO", "email": "", "phone": "", "auth": ""}]
    assert out["use_of_funds"]["inventory"] == 600000 and out["use_of_funds"]["other"] == "" and out["use_of_funds"]["other_label"] == "Signage"
    assert out["program_support"] == ["capital_health", "other"] and out["rm_comp_categories"] == ["salary", "hourly"]
    merged = pa.merge_changes(pa.empty_arrangement(), out)
    assert pa.is_blank(pa.FIELD_RULES_BY_KEY["owners"], merged["owners"]) is False
    assert pa.is_blank(pa.FIELD_RULES_BY_KEY["use_of_funds"], pa.empty_arrangement()["use_of_funds"]) is True


def test_arrangement_diff_marks_changes_and_hides_desk_only_rows_from_the_dealer():
    a = seed()
    c1 = pa.compute(a)
    b, _ = pa.apply_term_sheet(a, _sheet())
    c2 = pa.compute(b, stage=2)
    d = pa.arrangement_diff({"arrangement": a, "computed": c1}, {"arrangement": b, "computed": c2})
    rows = {r["key"]: r for r in d["rows"]}
    assert rows["requested"]["changed"] and rows["requested"]["before"] == "$1,200,000" and rows["requested"]["after"] == "$1,000,000"
    assert rows["dealer_name"]["changed"] is False
    assert rows["advance.spread"]["dealer_visible"] is False and rows["advance.advance"]["dealer_visible"] is True
    assert rows["use_of_funds.inventory"]["original_blank"] is True
    assert d["changed_count"] == sum(1 for r in d["rows"] if r["changed"]) > 0
    import json as _json
    _json.dumps(d)


def test_the_comparison_shows_the_current_figures_moving_too():
    """cur_rate and cur_premium were absent from the diff, so the desk could
    restate the dealer's current production between the executed commitment and
    the final and the comparison said nothing — on the very figures every
    operative threshold is derived from."""
    a = seed()
    c1 = pa.compute(a)
    b = {**a, "products": {**a["products"], "vsc": {**a["products"]["vsc"], "cur_rate": 20, "cur_premium": 999}}}
    c2 = pa.compute(b)
    d = pa.arrangement_diff({"arrangement": a, "computed": c1}, {"arrangement": b, "computed": c2})
    rows = {r["key"]: r for r in d["rows"]}

    assert rows["products.vsc.cur_rate"]["changed"] is True
    assert rows["products.vsc.cur_premium"]["changed"] is True
    assert rows["products.vsc.cur_premium"]["after"] == "$999"
    # And the labels say which side of the table each row came from.
    assert "current" in rows["products.vsc.cur_rate"]["label"]
    assert "new" in rows["products.vsc.rate"]["label"]


# ---------------------------------------------------------------------------
# the fee stack behind premium
# ---------------------------------------------------------------------------

# The design's own worked VSC example: what the dealer pays another provider
# today, our base cost, the admin fee, other fees, our markup, nothing carried
# to the loan yet.
DESIGN_VSC = {"on": True, "cur_rate": 54, "cur_premium": 2150, "rate": 54, "premium": 1715, "repay": 0,
              "comm": 0, "admin": 95, "retention": 0, "term": 36, "base": 1380, "other": 40, "markup": 200}


def test_the_fee_stack_reproduces_the_designs_worked_example():
    r = pa.product_econ(96, "vsc", DESIGN_VSC)
    assert r.stack == 1515 and r.cushion == 635 and r.premium == 1715
    assert r.savings == 435 and r.room == 435
    assert r.stack_known is True
    assert r.uplift == -r.savings  # one number, two names; the sign is stated so nobody flips it


def test_a_legacy_row_admits_it_knows_no_base_cost():
    """_num(None) is 0.0, so a ten-key row would otherwise compute a cushion the
    size of today's whole premium. stack_known is what the UI and the PDF read
    before showing a cushion or a saving as real."""
    legacy = {k: v for k, v in SEED_PRODUCTS["vsc"].items() if k not in ("base", "other", "markup")}
    r = pa.product_econ(96, "vsc", legacy)
    assert r.stack_known is False
    assert r.cushion == r.cur_premium - r.admin  # the fabricated figure, present but flagged
    # And the additive phase changed nothing the old row already reported.
    assert r.reserve == pytest.approx((2400 - 420 - 336 - 260) * 0.38)
    arr = seed()
    arr["products"] = {k: {f: v for f, v in row.items() if f not in ("base", "other", "markup")} for k, row in arr["products"].items()}
    c = pa.compute(arr)
    json.dumps(c)
    assert all(row["stack_known"] is False for row in c["econ"]["rows"])
    # A legacy arrangement is told to enter its base costs, and blamed for nothing else about the stack.
    keys = {a["key"] for a in c["attention"]}
    assert {f"products.{r['key']}.base" for r in c["econ"]["rows"] if r["on"]} <= keys
    assert not any(k.endswith((".over", ".cushion", ".premium")) for k in keys)


def test_savings_hold_volume_constant_and_the_gross_delta_splits_cleanly():
    """`savings_m` compares today's contracts at today's price with today's
    contracts at ours; `d_gross` is split into "sells more" and "charges more"
    so a proposal can say which one moved the number."""
    e = pa.compute(seed())["econ"]
    assert e["savings_m"] == pytest.approx(e["cur_gross"] - e["cost_same"])
    assert e["d_gross_from_attach"] + e["d_gross_from_price"] == pytest.approx(e["d_gross"])
    assert e["cost_same"] != e["gross"]  # the seed lifts attachment, so volume-held and volume-lifted differ
    # On the new story's happy path — price down, attachment up — the total is
    # positive while the price component is negative.
    arr = seed()
    arr["products"]["vsc"].update(DESIGN_VSC | {"rate": 62})
    e2 = pa.compute(arr)["econ"]
    vsc = next(r for r in e2["rows"] if r["key"] == "vsc")
    assert vsc["d_gross_from_price"] < 0 < vsc["d_gross_from_attach"]


def test_normalize_keeps_the_stack_and_still_drops_an_unknown_product_key():
    n = pa.normalize_changes({"products": {"vsc": {"base": 1380, "other": 40, "markup": 200, "bogus": 1}}})
    assert n["products"]["vsc"] == {"base": 1380, "other": 40, "markup": 200}


def test_a_patch_heals_a_legacy_product_row_to_the_full_shape():
    """merge_changes used to copy the stored row when the key existed, so a
    ten-key row stayed ten keys forever. Now a PATCH of any field brings it up
    to DEFAULT_PRODUCT's shape."""
    m = pa.merge_changes({"products": {"vsc": {"on": True, "premium": 2400}}}, {"products": {"vsc": {"rate": 60}}})
    assert sorted(m["products"]["vsc"]) == sorted(pa.DEFAULT_PRODUCT)
    assert m["products"]["vsc"]["premium"] == 2400 and m["products"]["vsc"]["rate"] == 60
    assert m["products"]["vsc"]["base"] == ""


def test_the_comparison_never_shows_our_cost_to_the_dealer_and_never_invents_a_change():
    """A stage-two package drafted from a commitment executed before the stack
    existed: the new rows are blank on the original, not changed — and our base
    cost, markup and commission never reach the dealer's signing gate."""
    a = seed()
    a["products"] = {k: {f: v for f, v in row.items() if f not in ("base", "other", "markup")} for k, row in a["products"].items()}  # as every executed commitment today
    c1 = pa.compute(a)
    b, _ = pa.apply_term_sheet(a, _sheet())
    b["products"]["vsc"].update({"base": 1380, "other": 40, "markup": 200})
    c2 = pa.compute(b, stage=2)
    rows = {r["key"]: r for r in pa.arrangement_diff({"arrangement": a, "computed": c1}, {"arrangement": b, "computed": c2})["rows"]}
    # c1 was computed by this code, so its rows carry the keys; simulate a
    # frozen pre-deploy snapshot by removing them from the original's rows.
    for r in c1["econ"]["rows"]:
        for k in ("base", "other", "markup", "stack"):
            r.pop(k, None)
    rows = {r["key"]: r for r in pa.arrangement_diff({"arrangement": a, "computed": c1}, {"arrangement": b, "computed": c2})["rows"]}
    for fld in ("base", "markup", "comm_pct"):
        assert rows[f"products.vsc.{fld}"]["dealer_visible"] is False, fld
    for fld in ("base", "other", "markup", "stack"):
        assert rows[f"products.vsc.{fld}"]["original_blank"] is True, fld
    assert rows["products.vsc.other"]["dealer_visible"] is True
    assert rows["products.vsc.premium"]["original_blank"] is False



# ---------------------------------------------------------------------------
# the story: the payment comes out of the cushion
# ---------------------------------------------------------------------------

def new_story() -> dict:
    """Every covered product priced below today with room to spare."""
    arr = seed()
    for row in arr["products"].values():
        row.update({"base": round(row["cur_premium"] * 0.55), "other": 20, "markup": round(row["cur_premium"] * 0.08), "repay": 0})
        row["premium"] = row["base"] + row["admin"] + row["other"] + row["markup"]
    return arr


def send_ready(debt_service: float = 30000) -> dict:
    """A new-story arrangement with the loan built into the policies out of
    the room, so nothing is open but what a test chooses to open."""
    arr = new_story()
    arr["thresholds"] = {}
    arr["debt_service"] = debt_service
    rows, shortfall = pa.room_solve(pa.portfolio_econ(96, arr["products"]), debt_service * 1.25)
    assert shortfall == 0, "the seed's room must carry the covenant"
    for r in rows:
        row = arr["products"][r["key"]]
        row["repay"] = r["solve_repay"]
        row["premium"] = row["base"] + row["admin"] + row["other"] + row["markup"] + row["repay"]
    return arr


def test_send_ready_means_nothing_open():
    assert pa.compute(send_ready())["attention"] == []


def test_room_solve_fills_the_room_first_and_never_hides_a_shortfall():
    arr = new_story()
    e = pa.portfolio_econ(96, arr["products"])
    rows, shortfall = pa.room_solve(e, 5000)
    assert shortfall == 0
    assert sum(r["solve_repay"] * r["contracts"] for r in rows) >= 5000
    assert not any(r["over_room"] for r in rows)
    assert all(r["savings_after"] >= 0 for r in rows)  # nobody pays more than today
    # The rounding remainder lands on the product with the most room.
    assert max(rows, key=lambda r: r["room_m"])["solve_repay"] >= rows[0]["solve_repay"] or len(rows) == 1
    # Short: the rest is spread evenly on top, reported, and flagged per row.
    big, short = pa.room_solve(e, 500_000)
    assert short > 0 and any(r["over_room"] for r in big)
    assert sum(r["solve_repay"] * r["contracts"] for r in big) >= 500_000
    assert pa.room_solve(pa.portfolio_econ(96, {}), 1000) == ([], 1000)


def test_a_legacy_row_takes_only_the_even_share():
    """A row with no base cost has no room it can vouch for."""
    arr = new_story()
    for f in ("base", "other", "markup"):
        arr["products"]["gap"].pop(f)
    e = pa.portfolio_econ(96, arr["products"])
    rows, _ = pa.room_solve(e, 5000)
    gap = next(r for r in rows if r["key"] == "gap")
    assert gap["room"] == 0 and gap["solve_repay"] == 0


def test_the_dealer_can_carry_the_loan_directly():
    """buildout_mode was declared, defaulted and never read. `forward` now means
    the dealer pays from operations: nothing is carried, whatever the products
    say, and the half-payment rule stays quiet because nothing was meant to be."""
    arr = seed()
    c = pa.compute(arr)
    assert c["buildout"]["mode"] == "reverse" and c["buildout"]["build"] is True
    assert c["buildout"]["policy_funded"] == c["econ"]["repay_m"] > 0
    arr["buildout_mode"] = "forward"
    d = pa.compute(arr)
    assert d["buildout"]["build"] is False and d["buildout"]["policy_funded"] == 0
    assert d["buildout"]["out_of_pocket"] == d["buildout"]["debt_service"]
    assert not any(a["key"] == "buildout" for a in d["attention"])
    assert not any(a["key"].endswith(".repay") for a in d["attention"])
    arr["buildout_mode"] = "sideways"  # stored without a membership check; not trusted
    assert pa.compute(arr)["buildout"]["mode"] == "reverse"


def test_the_story_rules():
    arr = new_story()
    c = pa.compute(arr)
    keys = {a["key"] for a in c["attention"]}
    assert not any(k.endswith((".over", ".cushion", ".base", ".premium")) for k in keys)
    # Our base cost above today's price: no cushion, and the dealer pays more.
    arr["products"]["vsc"]["base"] = 2500
    arr["products"]["vsc"]["premium"] = 2500 + 260 + 20 + arr["products"]["vsc"]["markup"]
    keys = {a["key"]: a for a in pa.compute(arr)["attention"]}
    assert "products.vsc.cushion" in keys and keys["products.vsc.cushion"]["owner"] == "desk"
    assert "products.vsc.over" in keys and "markup" in keys["products.vsc.over"]["detail"]
    # The premium and the stack disagree by more than a dollar.
    arr = new_story()
    arr["products"]["vsc"]["premium"] += 2
    assert "products.vsc.premium" in {a["key"] for a in pa.compute(arr)["attention"]}
    arr["products"]["vsc"]["premium"] -= 2.5  # within rounding
    assert "products.vsc.premium" not in {a["key"] for a in pa.compute(arr)["attention"]}
    # The loan pushing the ticket over today names the repayment as the lever.
    arr = new_story()
    vsc = arr["products"]["vsc"]
    vsc["repay"] = vsc["cur_premium"] - vsc["base"] - vsc["admin"] - vsc["other"] - vsc["markup"] + 50
    vsc["premium"] = vsc["base"] + vsc["admin"] + vsc["other"] + vsc["markup"] + vsc["repay"]
    over = next(a for a in pa.compute(arr)["attention"] if a["key"] == "products.vsc.over")
    assert "carried to the loan" in over["detail"]


def test_the_sponsor_block_reads_the_per_product_markup():
    c = pa.compute(seed())
    e = c["econ"]
    expected = sum(r["contracts"] * r["markup"] for r in e["rows"] if r["on"])
    assert c["sponsor"]["markup_m"] == pytest.approx(expected) and expected > 0
    assert c["sponsor"]["markup_pct"] == pytest.approx(expected / e["gross"] * 100)
    assert "markup" not in pa.FIELD_RULES_BY_KEY
    assert "markup" not in pa.DESK_ONLY_KEYS and "markup" in pa.DESK_ONLY_PRODUCT_FIELDS


def test_the_waterfall_tells_today_versus_with_us():
    w = {row["label"]: row for row in pa.compute(seed())["econ"]["waterfall"]}
    assert w["What the dealer pays today"]["value"] == 2150
    assert w["What the dealer pays with us"]["value"] == 2400
    assert w["The dealer saves"]["value"] == -250
    assert w["Base product cost"]["value"] + w["Administrator fee"]["value"] + w["Other fees"]["value"] + w["Our markup"]["value"] + w["Carried to the loan"]["value"] == 2400


def test_the_exclusivity_window_follows_the_size_of_the_request():
    """Over $350,000: sixty days. At or under: thirty or less. The desk may
    shorten under the tier, never lengthen, and the number that prints is the
    number that governs."""
    arr = seed()
    arr["requested"] = 350_000
    arr.pop("exclusivity", None)
    assert pa.exclusivity_days(arr) == 30
    arr["requested"] = 350_001
    assert pa.exclusivity_days(arr) == 60
    arr["exclusivity"] = 20
    assert pa.exclusivity_days(arr) == 20
    arr["exclusivity"] = 45
    assert pa.exclusivity_days(arr) == 45  # shorter than the tier: the desk's number governs
    assert not any(a["key"] == "exclusivity" for a in pa.compute(arr)["attention"])
    arr["exclusivity"] = 90
    assert pa.exclusivity_days(arr) == 60  # past the tier: the tier governs, and the desk is told
    c = pa.compute(arr)
    row = next(a for a in c["attention"] if a["key"] == "exclusivity")
    assert row["owner"] == "desk" and "60" in row["detail"]
    assert c["advance"]["exclusivity_days"] == 60 and c["advance"]["exclusivity_tier"] == 60
    assert next(r for r in c["preview"]["one"] if r["label"] == "Exclusivity window (days)")["value"] == "60"
    arr["exclusivity"] = ""
    assert not any(a["key"] == "exclusivity" for a in pa.compute(arr)["attention"])


# ---- where the loan goes ----

def test_where_the_loan_goes_is_seeded_and_blank_until_an_amount_is_entered():
    arr = pa.empty_arrangement()
    assert [r["label"] for r in arr["proceeds"]] == ["New working capital", "Previous contract repayment"]
    assert all(r["amount"] == "" and r["note"] == "" for r in arr["proceeds"])
    rule = pa.FIELD_RULES_BY_KEY["proceeds"]
    assert pa.is_blank(rule, arr["proceeds"]) is True
    assert pa.is_blank(rule, [{"label": "Signage", "amount": 1, "note": ""}]) is False
    # A legacy package with no key gets the seed; a desk that removed both lines keeps them removed.
    assert pa.compute({})["advance"]["proceeds"] == [{"label": "New working capital", "amount": 0.0, "note": "", "entered": False},
                                                     {"label": "Previous contract repayment", "amount": 0.0, "note": "", "entered": False}]
    assert pa.compute({"proceeds": []})["advance"]["proceeds"] == []
    # It is the desk's: the advance step, so DESK_ONLY_KEYS picks it up by derivation.
    assert "proceeds" in pa.DESK_ONLY_KEYS and rule.step == "advance" and rule.required is False


def test_where_the_loan_goes_normalises_like_the_owners_table():
    out = pa.normalize_changes({"proceeds": [
        {"label": " New working capital ", "amount": "250000", "note": ""},
        {"label": "", "amount": "", "note": "  "},
        {"label": "Previous contract repayment", "amount": 100000.5, "note": " payoff of the Acme advance "},
        "junk",
        {"label": "x", "amount": "abc"},
    ]})
    assert out["proceeds"] == [
        {"label": "New working capital", "amount": 250000, "note": ""},
        {"label": "Previous contract repayment", "amount": 100000.5, "note": "payoff of the Acme advance"},
        {"label": "x", "amount": "", "note": ""},
    ]
    capped = pa.normalize_changes({"proceeds": [{"label": f"l{i}", "amount": i + 1} for i in range(12)]})
    assert len(capped["proceeds"]) == pa.MAX_PROCEEDS
    # The owners table still normalises byte for byte the way it did before the two kinds shared a path.
    assert pa.OWNER_COLUMNS == (("name", "text"), ("pct", "number"), ("title", "text"), ("email", "text"), ("phone", "text"), ("auth", "text"))


def test_where_the_loan_goes_flags_only_when_both_sides_are_filled_and_disagree():
    arr = seed()
    arr["proceeds"] = [{"label": "New working capital", "amount": 700000}, {"label": "Previous contract repayment", "amount": 500000}]
    c = pa.compute(arr)
    assert c["advance"]["proceeds_total"] == 1200000 and c["advance"]["proceeds_gap"] == 0
    assert not [a for a in c["attention"] if a["key"] == "proceeds"]
    arr["proceeds"][1]["amount"] = 400000
    rows = [a for a in pa.compute(arr)["attention"] if a["key"] == "proceeds"]
    assert len(rows) == 1 and rows[0]["owner"] == "desk" and "$100,000 unallocated" in rows[0]["detail"]
    arr["proceeds"][1]["amount"] = 600000
    assert "$100,000 over the request" in [a for a in pa.compute(arr)["attention"] if a["key"] == "proceeds"][0]["detail"]
    # Labels alone, or no request, never flag.
    arr["proceeds"] = [{"label": "New working capital", "amount": ""}]
    assert not [a for a in pa.compute(arr)["attention"] if a["key"] == "proceeds"]
    arr["proceeds"] = [{"label": "New working capital", "amount": 5}]
    arr["requested"] = ""
    assert not [a for a in pa.compute(arr)["attention"] if a["key"] == "proceeds"]


def test_where_the_loan_goes_never_invents_a_change_in_the_comparison():
    a = seed()
    a["proceeds"] = [{"label": "New working capital", "amount": 700000}, {"label": "Previous contract repayment", "amount": 500000}]
    b = copy.deepcopy(a)
    d = pa.arrangement_diff({"arrangement": a, "computed": pa.compute(a)}, {"arrangement": b, "computed": pa.compute(b, stage=2)})
    rows = {r["key"]: r for r in d["rows"]}
    assert rows["proceeds.0"]["changed"] is False and rows["proceeds.total"]["changed"] is False
    assert rows["proceeds.0"]["dealer_visible"] is True and rows["proceeds.0"]["original_blank"] is False
    # A commitment executed before the lines existed reads as blank on the original side, not as a change from nothing.
    old = seed()
    old.pop("proceeds", None)
    d2 = pa.arrangement_diff({"arrangement": old, "computed": pa.compute(old)}, {"arrangement": b, "computed": pa.compute(b, stage=2)})
    r0 = {r["key"]: r for r in d2["rows"]}["proceeds.0"]
    assert r0["original_blank"] is True and r0["before"] == "—" and r0["after"] == "$700,000"


def test_the_proposal_prints_where_the_loan_goes_only_once_an_amount_is_entered():
    from app.services import production_presentation as pp
    arr = seed()
    meta = {"generated_at": "2026-09-09", "package_id": "x", "business_name": "Delgado"}
    html = pp.build_presentation_html(arr, pa.compute(arr), meta=meta)
    assert "Where the loan goes" not in html
    arr["proceeds"] = [{"label": "New working capital", "amount": 700000, "note": "floorplan relief"}, {"label": "Previous contract repayment", "amount": ""}]
    html = pp.build_presentation_html(arr, pa.compute(arr), meta=meta)
    assert "Where the loan goes" in html and "floorplan relief" in html and "$700,000" in html
    assert "$500,000 of the request unallocated" in html
    assert "Previous contract repayment" not in html.split("Where the loan goes", 1)[1].split("</table>", 1)[0]
