"""Synthetic PPM preset defaults versus the actual marketplace member gates."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.capital_partners import enums as E
from app.capital_partners import models as cp
from app.capital_partners.services import membership_policy, ppm, ppm_presets
from app.enums import Role
from app.tests.test_capital_partners import Scenario
from app.tests.test_marketplace_ppm_lifecycle import (
    configure,
    generate,
    ppm_harness,
    review_payload,
    state,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "wait", "allowed"), [
    (E.MemberStatus.REQUESTED, None, False),
    (E.MemberStatus.QUESTIONNAIRE, None, False),
    (E.MemberStatus.UNDER_REVIEW, None, False),
    (E.MemberStatus.WAITING, None, False),
    (E.MemberStatus.WAITING, "future", False),
    (E.MemberStatus.WAITING, "past", True),
    (E.MemberStatus.APPROVED, None, True),
    (E.MemberStatus.APPROVED, "future", False),
    (E.MemberStatus.APPROVED, "past", True),
    (E.MemberStatus.APPROVED, "zero_days", True),
    (E.MemberStatus.SUSPENDED, None, False),
    (E.MemberStatus.SUSPENDED, "past", False),
])
async def test_actual_member_access_and_subscription_gates_are_not_changed_by_ppm_presets(status, wait, allowed):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, offered="100000")
        user, member = await scenario.member("synthetic-policy-investor@example.test", cleared=False)
        member.status = status
        now = datetime.now(UTC)
        member.waiting_period_ends_at = now + timedelta(days=1) if wait == "future" else now - timedelta(days=1) if wait == "past" else now - timedelta(seconds=1) if wait == "zero_days" else None
        member.waiting_period_days = 0 if wait == "zero_days" else 30
        h.current = user
        base = f"/api/v1/capital/listings/{listing.ref}"
        detail = await h.client.get(base)
        assert detail.status_code == (200 if allowed else 403), detail.text
        subscription = await h.client.post(base + "/subscriptions", json={"amount": "25000", "ack_ppm": True, "ack_accredited": True})
        assert subscription.status_code == (201 if allowed else 403), subscription.text
        count = (await h.db.execute(select(cp.CpSubscription).where(cp.CpSubscription.listing_id == listing.id))).scalars().all()
        assert len(count) == (1 if allowed else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.SUPER_ADMIN, Role.LOAN_EXEC])
async def test_staff_preview_can_read_but_cannot_commit_a_member_subscription(role):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, offered="100000")
        h.current = await h.user("synthetic-policy-staff@example.test", role)
        base = f"/api/v1/capital/listings/{listing.ref}"
        assert (await h.client.get(base)).status_code == 200
        response = await h.client.post(base + "/subscriptions", json={"amount": "25000", "ack_ppm": True, "ack_accredited": True})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "preview_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(("override", "expected_code"), [
    ({"ack_ppm": False}, "acknowledgements_required"),
    ({"ack_accredited": False}, "acknowledgements_required"),
    ({"entity_id": "92000000-0000-0000-0000-000000000001"}, "entity_not_yours"),
    ({"amount": "24999"}, "below_minimum"),
    ({"amount": "100001"}, "exceeds_remaining"),
])
async def test_cleared_member_still_needs_acknowledgements_own_entity_and_available_allocation(override, expected_code):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, offered="100000")
        user, _member = await scenario.member("synthetic-allocation-policy@example.test")
        h.current = user
        response = await h.client.post(f"/api/v1/capital/listings/{listing.ref}/subscriptions", json={"amount": "25000", "ack_ppm": True, "ack_accredited": True, **override})
        assert response.status_code == 409 and response.json()["detail"]["code"] == expected_code, response.text


@pytest.mark.asyncio
async def test_zero_day_staff_review_uses_the_existing_gate_without_approving_a_ppm_or_listing():
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        user, member = await scenario.member("synthetic-zero-day-member@example.test", cleared=False)
        h.current = admin
        response = await h.client.post(f"/api/v1/capital/admin/members/{member.id}/review", json={"decision": "approve", "waiting_period_days": 0})
        assert response.status_code == 200, response.text
        await h.db.refresh(member)
        assert member.status == E.MemberStatus.APPROVED and member.waiting_period_days == 0
        h.current = user
        assert (await h.client.get(f"/api/v1/capital/listings/{listing.ref}")).status_code == 404
        await h.db.refresh(listing)
        assert listing.status == E.ListingStatus.DRAFT
        assert not (await h.db.execute(select(cp.CpListingEvent).where(cp.CpListingEvent.kind == "ppm.reviewed"))).scalars().all()


def preset_context(days=17, issuer="Qualified Commercial LLC"):
    return {"membership_policy": membership_policy.describe(SimpleNamespace(default_waiting_period_days=days)), "issuer": issuer}


def test_preset_catalog_has_finite_stable_field_specific_choices_and_confirmed_issuer_default():
    first = ppm_presets.catalog(preset_context(), ppm.LEGAL_FIELDS)
    changed = ppm_presets.catalog(preset_context(days=0), ppm.LEGAL_FIELDS)
    assert {row["key"] for row in first} == {row["key"] for row in ppm.LEGAL_FIELDS}
    for row, next_row in zip(first, changed, strict=True):
        keys = [option["key"] for option in row["options"]]
        assert len(keys) == len(set(keys)) <= 54
        assert keys.count("custom") == 1
        assert keys == [option["key"] for option in next_row["options"]]
    issuer = next(row for row in first if row["key"] == "issuer_legal_name")
    assert issuer["default_value"] == "Qualified Commercial LLC" and issuer["default_preset"] == "issuer_settings"
    jurisdiction = next(row for row in first if row["key"] == "jurisdiction")
    assert jurisdiction["default_preset"] == "to_be_confirmed"
    assert len([option for option in jurisdiction["options"] if option["key"].startswith("us_state_")]) == 51
    assert next(option for option in jurisdiction["options"] if option["key"] == "to_be_confirmed")["unresolved"] is True


def test_v1_upgrade_fills_empty_fields_once_and_never_replaces_existing_custom_issuer_or_wording():
    original = {"legal_inputs": {"issuer_legal_name": "Existing Example Issuer LLC", "investor_eligibility": "Bespoke investor restriction.\nRetain this staff-authored wording."}}
    before = deepcopy(original)
    legal, selected = ppm_presets.upgrade(preset_context(), ppm.LEGAL_FIELDS, original)
    assert original == before
    assert legal["issuer_legal_name"] == original["legal_inputs"]["issuer_legal_name"]
    assert legal["investor_eligibility"] == original["legal_inputs"]["investor_eligibility"]
    assert selected["issuer_legal_name"] == selected["investor_eligibility"] == "custom"
    assert legal["subscription_process"] and selected["subscription_process"] == "marketplace_allocation_workflow"
    explicit_blank = {"preset_schema": ppm_presets.PRESET_SCHEMA, "legal_inputs": {"issuer_legal_name": ""}, "legal_presets": {"issuer_legal_name": "custom"}}
    legal, selected = ppm_presets.upgrade(preset_context(), ppm.LEGAL_FIELDS, explicit_blank)
    assert legal["issuer_legal_name"] == "" and selected["issuer_legal_name"] == "custom"


def test_selected_membership_preset_follows_current_settings_while_custom_text_is_preserved():
    old_context = preset_context(days=30)
    values, ids = ppm_presets.defaults(old_context, ppm.LEGAL_FIELDS)
    values["jurisdiction"], ids["jurisdiction"] = "Additional bespoke governing-law wording.", "custom"
    previous = {"preset_schema": ppm_presets.PRESET_SCHEMA, "legal_inputs": values, "legal_presets": ids}
    refreshed, selected = ppm_presets.upgrade(preset_context(days=7), ppm.LEGAL_FIELDS, previous)
    assert refreshed["jurisdiction"] == values["jurisdiction"]
    assert selected["jurisdiction"] == "custom"
    assert selected["investor_eligibility"] == "marketplace_membership_policy"
    assert "7 calendar days" in refreshed["investor_eligibility"] and "30 calendar days" not in refreshed["investor_eligibility"]
    # An explicit custom choice must remain custom even if its text happens to
    # match a known preset at the time it was saved.
    previous["legal_presets"]["investor_eligibility"] = "custom"
    custom, selected = ppm_presets.upgrade(preset_context(days=7), ppm.LEGAL_FIELDS, previous)
    assert custom["investor_eligibility"] == values["investor_eligibility"]
    assert selected["investor_eligibility"] == "custom"


@pytest.mark.parametrize("selection", [{"jurisdiction": "arbitrary_rule"}, {"jurisdiction": "marketplace_membership_policy"}, {"unsupported_field": "custom"}])
def test_unknown_and_cross_field_preset_ids_are_rejected(selection):
    with pytest.raises(ValueError):
        ppm_presets.for_save(preset_context(), ppm.LEGAL_FIELDS, {}, selection, {})


def test_a_valid_preset_id_derives_its_text_on_the_server_instead_of_trusting_submitted_wording():
    forged = {"investor_eligibility": "Invented assertion that platform approval establishes legal eligibility."}
    values, ids = ppm_presets.for_save(preset_context(), ppm.LEGAL_FIELDS, forged, {"investor_eligibility": "marketplace_membership_policy"}, {})
    assert ids["investor_eligibility"] == "marketplace_membership_policy"
    assert values["investor_eligibility"] == membership_policy.investor_disclosure(preset_context()["membership_policy"])
    assert "Invented assertion" not in values["investor_eligibility"]


@pytest.mark.asyncio
async def test_api_prefills_new_draft_but_presets_do_not_approve_it_or_its_members(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        context.update(preset_context())
        drafted = await generate(h, listing, context)
        current = drafted["current_draft"]
        assert current["legal_inputs"]["issuer_legal_name"] == "Qualified Commercial LLC"
        assert current["legal_presets"]["issuer_legal_name"] == "issuer_settings"
        assert current["legal_presets"]["investor_eligibility"] == "marketplace_membership_policy"
        assert "17 calendar days" in current["legal_inputs"]["investor_eligibility"]
        assert drafted["membership_policy"] == context["membership_policy"]
        assert drafted["status"] == "draft" and drafted["current_approved"] is None
        assert any(row["key"] == "jurisdiction" for row in drafted["missing"])
        refused = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(drafted, context))
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ppm_incomplete"
        await h.db.refresh(listing)
        assert listing.status == E.ListingStatus.DRAFT
        assert not (await h.db.execute(select(cp.CpMember))).scalars().all()


@pytest.mark.asyncio
async def test_nonempty_unresolved_jurisdiction_choice_remains_missing_and_cannot_be_reviewed(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        context.update(preset_context())
        drafted = await generate(h, listing, context)
        assert drafted["current_draft"]["legal_inputs"]["jurisdiction"].strip()
        assert any(item["key"] == "jurisdiction" for item in drafted["missing"])
        denied = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(drafted, context))
        assert denied.status_code == 409
        assert any(item["key"] == "jurisdiction" for item in denied.json()["detail"]["missing"])


@pytest.mark.asyncio
async def test_api_uses_preset_ids_as_authority_and_custom_blank_survives_source_refresh(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        context.update(preset_context())
        drafted = await generate(h, listing, context)
        current = drafted["current_draft"]
        legal, selected = deepcopy(current["legal_inputs"]), deepcopy(current["legal_presets"])
        legal["investor_eligibility"] = "Invented assurance; this must not replace server-derived policy."
        legal["issuer_legal_name"], selected["issuer_legal_name"] = "", "custom"
        response = await h.client.patch(f"/api/v1/capital/admin/listings/{listing.id}/ppm/draft", json={
            "expected_version": current["version"], "expected_source_fingerprint": context["fingerprint"], "idempotency_key": str(uuid4()),
            "legal_inputs": legal, "legal_presets": selected, "narrative_sections": {}})
        assert response.status_code == 200, response.text
        saved = response.json()["current_draft"]
        assert saved["legal_inputs"]["investor_eligibility"] == membership_policy.investor_disclosure(context["membership_policy"])
        assert saved["legal_inputs"]["issuer_legal_name"] == "" and saved["legal_presets"]["issuer_legal_name"] == "custom"
        context["fingerprint"] = "b" * 64
        context["membership_policy"] = preset_context(days=7)["membership_policy"]
        refreshed = await generate(h, listing, context)
        assert refreshed["current_draft"]["legal_inputs"]["issuer_legal_name"] == ""
        assert refreshed["current_draft"]["legal_presets"]["issuer_legal_name"] == "custom"
        assert "7 calendar days" in refreshed["current_draft"]["legal_inputs"]["investor_eligibility"]


@pytest.mark.asyncio
async def test_custom_additional_eligibility_cannot_remove_the_current_platform_policy_from_document(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        context.update(preset_context())
        drafted = await generate(h, listing, context)
        current = drafted["current_draft"]
        legal, selected = deepcopy(current["legal_inputs"]), deepcopy(current["legal_presets"])
        legal["investor_eligibility"], selected["investor_eligibility"] = "Additional bespoke restrictions for this offering.", "custom"
        response = await h.client.patch(f"/api/v1/capital/admin/listings/{listing.id}/ppm/draft", json={
            "expected_version": current["version"], "expected_source_fingerprint": context["fingerprint"], "idempotency_key": str(uuid4()),
            "legal_inputs": legal, "legal_presets": selected, "narrative_sections": {}})
        assert response.status_code == 200, response.text
        doc = response.json()["current_draft"]["document"]
        content = str(doc)
        assert context["membership_policy"]["summary"] in content
        assert all(requirement["detail"] in content for requirement in context["membership_policy"]["requirements"])
        assert "Additional bespoke restrictions for this offering." in content


@pytest.mark.asyncio
async def test_shared_policy_endpoint_matches_embedded_policy_and_stays_staff_scoped():
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        response = await h.client.get("/api/v1/capital/admin/membership-policy")
        assert response.status_code == 200, response.text
        policy = response.json()
        assert policy == (await state(h, listing))["membership_policy"]
        assert policy["requires_ppm_acknowledgement"] is True and policy["requires_accreditation_acknowledgement"] is True
        assert policy["independent_accreditation_verification"] is False and policy["operator_subscriptions_allowed"] is False
        for role in (Role.LOAN_EXEC, Role.CAPITAL_PARTNER, Role.BROKER):
            h.current = await h.user(f"synthetic-policy-{role.value}@example.test", role)
            assert (await h.client.get("/api/v1/capital/admin/membership-policy")).status_code == 403


def test_canonical_currency_preset_follows_saved_currency_but_manual_override_is_preserved_as_conflict():
    usd = {**preset_context(), "budget": {"currency": "USD"}, "financials": {}, "missing": []}
    values, ids = ppm_presets.defaults(usd, ppm.LEGAL_FIELDS)
    assert ids["offering_currency"] == "saved_financing_currency"
    prior = {"preset_schema": ppm_presets.PRESET_SCHEMA, "legal_inputs": values, "legal_presets": ids}
    eur = {**usd, "budget": {"currency": "EUR"}}
    updated, selected = ppm_presets.upgrade(eur, ppm.LEGAL_FIELDS, prior)
    assert selected["offering_currency"] == "saved_financing_currency" and updated["offering_currency"] == "EUR"
    prior["legal_inputs"]["offering_currency"], prior["legal_presets"]["offering_currency"] = "CAD", "custom"
    custom, selected = ppm_presets.upgrade(eur, ppm.LEGAL_FIELDS, prior)
    assert custom["offering_currency"] == "CAD" and selected["offering_currency"] == "custom"
    assert any(item["key"] == "offering_currency" for item in ppm._missing(eur, custom, selected))


@pytest.mark.asyncio
async def test_real_settings_change_stales_review_and_refreshes_selected_membership_disclosure(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        real_context = ppm.saved_context
        configure(monkeypatch, listing)
        monkeypatch.setattr(ppm, "saved_context", real_context)
        current = await state(h, listing)
        drafted = await generate(h, listing, {"fingerprint": current["source_fingerprint"]})
        version = drafted["current_draft"]
        choices = {**version["legal_presets"], "jurisdiction": "us_state_NJ", "offering_currency": "currency_USD"}
        response = await h.client.patch(f"/api/v1/capital/admin/listings/{listing.id}/ppm/draft", json={
            "expected_version": version["version"], "expected_source_fingerprint": drafted["source_fingerprint"], "idempotency_key": str(uuid4()),
            "legal_inputs": version["legal_inputs"], "legal_presets": choices, "narrative_sections": {}})
        assert response.status_code == 200, response.text
        saved = response.json()
        reviewed = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(saved, {"fingerprint": saved["source_fingerprint"]}))
        assert reviewed.status_code == 200, reviewed.text
        old = deepcopy(reviewed.json()["current_approved"])
        settings = (await h.db.execute(select(cp.CpIssuerSettings).where(cp.CpIssuerSettings.key == "default"))).scalar_one()
        settings.default_waiting_period_days = 7
        stale = await state(h, listing)
        assert stale["source_fingerprint"] != saved["source_fingerprint"]
        assert stale["current_approved"]["stale"] is True
        refreshed = await generate(h, listing, {"fingerprint": stale["source_fingerprint"]})
        assert refreshed["status"] == "draft"
        next_version = refreshed["current_draft"]
        assert next_version["legal_presets"]["investor_eligibility"] == "marketplace_membership_policy"
        assert "7 calendar days" in next_version["legal_inputs"]["investor_eligibility"]
        historical = next(row for row in refreshed["history"] if row["document_id"] == old["document_id"])
        assert historical["legal_inputs"] == old["legal_inputs"]
        assert historical["reviewed_at"] == old["reviewed_at"]
