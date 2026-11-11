"""Finite, versioned draft wording; custom text never silently becomes a preset."""
from __future__ import annotations

from copy import deepcopy

from . import membership_policy

PRESET_SCHEMA = "ppm-presets-v1"
CUSTOM = "custom"
STATES = dict(pair.split(":", 1) for pair in (
    "AL:Alabama|AK:Alaska|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|CT:Connecticut|DE:Delaware|DC:District of Columbia|"
    "FL:Florida|GA:Georgia|HI:Hawaii|ID:Idaho|IL:Illinois|IN:Indiana|IA:Iowa|KS:Kansas|KY:Kentucky|LA:Louisiana|ME:Maine|"
    "MD:Maryland|MA:Massachusetts|MI:Michigan|MN:Minnesota|MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|"
    "NH:New Hampshire|NJ:New Jersey|NM:New Mexico|NY:New York|NC:North Carolina|ND:North Dakota|OH:Ohio|OK:Oklahoma|"
    "OR:Oregon|PA:Pennsylvania|RI:Rhode Island|SC:South Carolina|SD:South Dakota|TN:Tennessee|TX:Texas|UT:Utah|"
    "VT:Vermont|VA:Virginia|WA:Washington|WV:West Virginia|WI:Wisconsin|WY:Wyoming"
).split("|"))


def catalog(context, fields):
    policy = context.get("membership_policy") or membership_policy.describe()
    issuer = context.get("issuer") or membership_policy.CONFIRMED_ISSUER_NAME

    def option(key, label, text, unresolved=False):
        return {"key": key, "label": label, "text": text, "unresolved": unresolved}

    options = {
        "issuer_legal_name": [option("issuer_settings", issuer, issuer)],
        "offering_structure": [option("platform_loan_participation", "Loan participation — draft", "The platform records a funded investor allocation as a loan participation. The final subscription, participation, note and security documents establish the investor's legal rights and payment terms.")],
        "offering_exemption": [option("intended_506b_review", "Intended Rule 506(b) — confirm before approval", "Draft intended offering basis: Rule 506(b) private offering, subject to staff or counsel confirmation. No general solicitation is contemplated. The final offering terms, investor requirements and applicable limits must be confirmed before acceptance."),
            option("not_confirmed", "To be confirmed", "Offering exemption and restrictions remain to be confirmed before approval.", True)],
        "jurisdiction": [option("to_be_confirmed", "To be confirmed", "Governing law and offering jurisdictions remain to be confirmed before approval.", True), *[
            option("us_state_" + key, name, f"Intended governing law: {name}, subject to confirmation in the definitive offering documents. Offering jurisdictions and applicable filing requirements require separate review.")
            for key, name in STATES.items()]],
        "investor_eligibility": [option("marketplace_membership_policy", "Current marketplace member requirements", membership_policy.investor_disclosure(policy))],
        "subscription_process": [option("marketplace_allocation_workflow", "Marketplace subscription process", "A cleared member may request an allocation subject to the stated minimum and available capital. The investor acknowledges the PPM and provides accredited-investor self-confirmation. The desk then processes subscription documents and records signing and funding. A request alone does not guarantee acceptance or funding.")],
        "transfer_restrictions": [option("issuer_consent_workflow", "Issuer-consent transfer process", "The platform supports requests to transfer a participation. Completion requires recorded issuer consent and settlement. The definitive offering documents govern transfer rights and restrictions; no liquid secondary market is assured.")],
        "conflicts_of_interest": [option("recorded_fees_and_conflicts", "Recorded fees and conflicts disclosure", "Company, manager or servicer compensation may create conflicts of interest. The fee disclosure identifies currently recorded charges, payers and applicability. Any additional affiliation, referral or compensation arrangement must be disclosed and reviewed before approval.")],
        "offering_currency": [option("currency_not_confirmed", "To be confirmed", "", True), *[
            option("currency_" + code, code, code) for code in ("USD", "CAD", "EUR", "GBP", "CHF", "AUD", "JPY")]],
    }
    defaults = {key: rows[0]["key"] for key, rows in options.items() if rows}
    currency = (context.get("budget") or {}).get("currency") or ((context.get("financials") or {}).get("proposed_terms") or {}).get("currency")
    if currency:
        options["offering_currency"] = [option("saved_financing_currency", currency + " — saved financing", currency)]
        defaults["offering_currency"] = "saved_financing_currency"
    output = []
    for source in fields:
        key = source["key"]
        rows = options[key]
        selected = defaults.get(key, CUSTOM)
        value = next((row["text"] for row in rows if row["key"] == selected), "")
        help_text = "Draft wording for staff review. Custom wording is preserved when saved facts change."
        if key == "offering_currency":
            help_text = ("Uses the saved financing currency. Select it to clear a previous conflicting custom value." if currency
                else "Choose a confirmed currency when the saved financing currency is unknown.")
        output.append({**source, "options": [*rows, option(CUSTOM, "Custom wording", "")],
            "default_preset": selected, "default_value": value,
            "help": help_text, "read_only": False})
    return output


def unresolved_fields(context, fields, values, selected=None):
    unresolved = set()
    for field in catalog(context, fields):
        for choice in field["options"]:
            if choice.get("unresolved") and ((selected or {}).get(field["key"]) == choice["key"]
                or values.get(field["key"], "").strip() == choice["text"].strip()):
                unresolved.add(field["key"])
    return unresolved


def defaults(context, fields):
    rows = catalog(context, fields)
    return ({row["key"]: row["default_value"] for row in rows}, {row["key"]: row["default_preset"] for row in rows})


def _options(context, fields):
    return {row["key"]: {option["key"]: option for option in row["options"]} for row in catalog(context, fields)}


def upgrade(context, fields, previous):
    """Refresh selected presets; fill v1 blanks once, preserving all custom text."""
    legal = deepcopy(previous.get("legal_inputs") or {})
    selected = deepcopy(previous.get("legal_presets") or {})
    old = previous.get("preset_schema") != PRESET_SCHEMA
    default_values, default_keys = defaults(context, fields)
    choices = _options(context, fields)
    for row in fields:
        key = row["key"]
        choice = selected.get(key)
        if key == "offering_currency" and choice in {"saved_financing_currency", "currency_not_confirmed"} and choice not in choices[key]:
            # Following the source is distinct from manually choosing a currency.
            legal[key], selected[key] = default_values[key], default_keys[key]
        elif choice and choice != CUSTOM and choice in choices[key]:
            legal[key] = choices[key][choice]["text"]
        elif choice == CUSTOM or not old or legal.get(key, "").strip():
            selected[key] = CUSTOM
        else:
            legal[key], selected[key] = default_values[key], default_keys[key]
    return legal, selected


def for_save(context, fields, values, explicit, previous):
    choices = _options(context, fields)
    if explicit is not None and set(explicit) - set(choices):
        raise ValueError("Use supported PPM preset fields.")
    result, selected = deepcopy(values), {}
    for key in choices:
        supplied = explicit.get(key) if explicit is not None else None
        if supplied is not None:
            if supplied not in choices[key]:
                raise ValueError("Choose a supported PPM preset or custom wording.")
            choice = supplied
        else:
            # Older editors can still change a selected field into custom text.
            prior = (previous.get("legal_presets") or {}).get(key, CUSTOM)
            choice = prior if values.get(key) == (previous.get("legal_inputs") or {}).get(key) and prior in choices[key] else CUSTOM
        selected[key] = choice
        if choice != CUSTOM:
            result[key] = choices[key][choice]["text"]
    return result, selected
