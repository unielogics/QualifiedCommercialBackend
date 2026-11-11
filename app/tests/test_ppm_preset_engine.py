"""Preset intent survives configuration changes without rewriting custom text."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.capital_partners.services import membership_policy, ppm, ppm_presets


def context(days=0, currency="USD"):
    return {"issuer": membership_policy.CONFIRMED_ISSUER_NAME,
        "membership_policy": membership_policy.describe(SimpleNamespace(default_waiting_period_days=days)),
        "budget": {"currency": currency}, "financials": {}, "missing": []}


def previous(ctx):
    legal, selected = ppm_presets.upgrade(ctx, ppm.LEGAL_FIELDS, {})
    return {"legal_inputs": legal, "legal_presets": selected, "preset_schema": ppm_presets.PRESET_SCHEMA}


def test_legacy_blank_upgrade_preserves_nonempty_custom_text():
    original = {"legal_inputs": {"issuer_legal_name": "Existing custom issuer LLC", "investor_eligibility": "Additional offering-specific suitability review.", "jurisdiction": ""}}
    before = deepcopy(original)
    legal, selected = ppm_presets.upgrade(context(), ppm.LEGAL_FIELDS, original)
    assert original == before
    assert legal["issuer_legal_name"] == "Existing custom issuer LLC" and selected["issuer_legal_name"] == "custom"
    assert legal["investor_eligibility"] == original["legal_inputs"]["investor_eligibility"]
    assert selected["subscription_process"] == "marketplace_allocation_workflow"
    assert selected["jurisdiction"] == "to_be_confirmed" and legal["jurisdiction"]
    assert "jurisdiction" in {row["key"] for row in ppm._missing(context(), legal, selected)}


def test_configured_policy_preset_refreshes_while_custom_and_intentional_blank_remain():
    original = previous(context(0))
    original["legal_inputs"]["conflicts_of_interest"] = "Custom affiliation disclosure."
    original["legal_presets"]["conflicts_of_interest"] = "custom"
    original["legal_inputs"]["jurisdiction"] = ""
    original["legal_presets"]["jurisdiction"] = "custom"
    legal, selected = ppm_presets.upgrade(context(17), ppm.LEGAL_FIELDS, original)
    assert "17 calendar days" in legal["investor_eligibility"]
    assert selected["investor_eligibility"] == "marketplace_membership_policy"
    assert legal["conflicts_of_interest"] == "Custom affiliation disclosure."
    assert legal["jurisdiction"] == "" and selected["jurisdiction"] == "custom"
    assert "0 calendar days" in original["legal_inputs"]["investor_eligibility"]


def test_saved_currency_preset_follows_source_but_custom_currency_is_retained_for_review():
    original = previous(context(currency="USD"))
    assert original["legal_presets"]["offering_currency"] == "saved_financing_currency"
    updated, selected = ppm_presets.upgrade(context(currency="EUR"), ppm.LEGAL_FIELDS, original)
    assert updated["offering_currency"] == "EUR" and selected["offering_currency"] == "saved_financing_currency"
    original["legal_presets"]["offering_currency"] = "custom"
    custom, _ = ppm_presets.upgrade(context(currency="EUR"), ppm.LEGAL_FIELDS, original)
    assert custom["offering_currency"] == "USD"
    assert "offering_currency" in {row["key"] for row in ppm._missing(context(currency="EUR"), custom)}


def test_issuer_settings_preset_follows_configured_name_without_overwriting_custom():
    ctx = context()
    original = previous(ctx)
    assert original["legal_presets"]["issuer_legal_name"] == "issuer_settings"
    ctx["issuer"] = "Another configured issuer LLC"
    legal, selected = ppm_presets.upgrade(ctx, ppm.LEGAL_FIELDS, original)
    assert legal["issuer_legal_name"] == ctx["issuer"] and selected["issuer_legal_name"] == "issuer_settings"
    original["legal_presets"]["issuer_legal_name"] = "custom"
    custom, _ = ppm_presets.upgrade(ctx, ppm.LEGAL_FIELDS, original)
    assert custom["issuer_legal_name"] == membership_policy.CONFIRMED_ISSUER_NAME


def test_currency_follow_source_does_not_turn_a_removed_source_into_an_invented_custom_value():
    original = previous(context(currency="USD"))
    unknown, selected = ppm_presets.upgrade(context(currency=None), ppm.LEGAL_FIELDS, original)
    assert unknown["offering_currency"] == "" and selected["offering_currency"] == "currency_not_confirmed"
    restored, selected = ppm_presets.upgrade(context(currency="EUR"), ppm.LEGAL_FIELDS,
        {"legal_inputs": unknown, "legal_presets": selected, "preset_schema": ppm_presets.PRESET_SCHEMA})
    assert restored["offering_currency"] == "EUR" and selected["offering_currency"] == "saved_financing_currency"


def test_old_editor_can_keep_a_preset_or_turn_an_edited_field_into_custom():
    ctx = context()
    original = previous(ctx)
    submitted = deepcopy(original["legal_inputs"])
    submitted["investor_eligibility"] = "Staff's additional investor requirement."
    result, selected = ppm_presets.for_save(ctx, ppm.LEGAL_FIELDS, submitted, None, original)
    assert result["investor_eligibility"] == submitted["investor_eligibility"]
    assert selected["investor_eligibility"] == "custom"
    assert selected["subscription_process"] == "marketplace_allocation_workflow"


def test_selected_preset_uses_server_text_not_a_forged_client_value():
    ctx = context()
    result, selected = ppm_presets.for_save(ctx, ppm.LEGAL_FIELDS,
        {"investor_eligibility": "All visitors can invest without review."},
        {"investor_eligibility": "marketplace_membership_policy"}, {})
    assert "All visitors" not in result["investor_eligibility"]
    assert result["investor_eligibility"] == membership_policy.investor_disclosure(ctx["membership_policy"])
    assert selected["investor_eligibility"] == "marketplace_membership_policy"


@pytest.mark.parametrize("selected", [{"jurisdiction": "intended_506b_review"}, {"invented": "custom"}])
def test_unknown_or_cross_field_preset_is_rejected(selected):
    with pytest.raises(ValueError):
        ppm_presets.for_save(context(), ppm.LEGAL_FIELDS, {}, selected, {})
