"""The formula port is checked against the design's SEED values, worked by hand."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from app.services import production_arrangement as pa

# The design's seed: Delgado Auto Group, 96 retail units a month.
SEED_PRODUCTS = {
    "vsc": {"on": True, "cur_rate": 54, "cur_premium": 2150, "rate": 62, "premium": 2400, "repay": 420, "comm": 14, "admin": 260, "retention": 38, "term": 36},
    "gap": {"on": True, "cur_rate": 36, "cur_premium": 795, "rate": 41, "premium": 895, "repay": 150, "comm": 16, "admin": 110, "retention": 46, "term": 36},
    "theft": {"on": True, "cur_rate": 19, "cur_premium": 545, "rate": 24, "premium": 595, "repay": 95, "comm": 18, "admin": 70, "retention": 52, "term": 24},
    "appearance": {"on": True, "cur_rate": 14, "cur_premium": 725, "rate": 19, "premium": 795, "repay": 120, "comm": 18, "admin": 95, "retention": 50, "term": 24},
    "key": {"on": False, "cur_rate": 9, "cur_premium": 329, "rate": 12, "premium": 349, "repay": 55, "comm": 20, "admin": 45, "retention": 55, "term": 24},
    "tire": {"on": True, "cur_rate": 17, "cur_premium": 645, "rate": 22, "premium": 699, "repay": 110, "comm": 17, "admin": 85, "retention": 48, "term": 24},
    "maint": {"on": False, "cur_rate": 11, "cur_premium": 849, "rate": 15, "premium": 899, "repay": 0, "comm": 15, "admin": 120, "retention": 44, "term": 12},
    "power": {"on": False, "cur_rate": 6, "cur_premium": 1425, "rate": 8, "premium": 1495, "repay": 0, "comm": 15, "admin": 180, "retention": 40, "term": 36},
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
        "debt_service": 41300, "markup": 12, "fund_target": 100, "cure_days": 5, "adj_value": 200,
        "products": SEED_PRODUCTS,
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
    # cure_days and exclusivity carry design defaults (5 days, 45 days); blanking them flags them
    assert {"cure_days", "exclusivity"}.isdisjoint(one)
    cleared = {**arr, "cure_days": "", "exclusivity": 0}
    assert {"cure_days", "exclusivity"} <= {a["key"] for a in pa.field_attention(cleared, scope="stage_one")}
    two = {a["key"] for a in pa.field_attention(arr, scope="stage_two")}
    assert {"funding_party", "funded_amount", "maturity"} <= two


def test_non_zero_rule_and_blank_multiselect():
    arr = seed()
    arr["monthly_units"] = 0
    arr["evidence"] = ["", "  "]
    keys = {a["key"] for a in pa.field_attention(arr, scope="presentation")}
    assert "monthly_units" in keys and "evidence" in keys


def test_seed_is_send_ready_except_for_the_covenant():
    c = pa.compute(seed())
    keys = [a["key"] for a in c["attention"]]
    assert keys == ["remittance_coverage"]


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


def test_preview_rows_flag_blanks():
    c = pa.compute(seed())
    one = c["preview"]["one"]
    assert len(one) == 16
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
    from datetime import date, datetime, timezone
    uid = uuid.uuid4()
    out = pa.jsonable({"id": uid, "d": date(2026, 9, 3), "dt": datetime(2026, 9, 3, tzinfo=timezone.utc),
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
