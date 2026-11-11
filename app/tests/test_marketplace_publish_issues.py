"""Publish guidance identifies the owner while retaining the existing rules."""

from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.capital_partners import enums as E
from app.capital_partners.models import CpListing, CpListingDocument
from app.capital_partners.schemas import PublishIssue
from app.capital_partners.services import emails, listings, storage
from app.enums import Role
from app.tests.test_capital_partners import Scenario, harness


def listing(**changes):
    values = dict(name="Synthetic financing", profile_id=None, note_rate_pct=9,
                  term_months=24, loan_amount=100000, offered_amount=100000,
                  minimum_investment=10000, source_snapshot={}, documents=[
                      CpListingDocument(kind=E.DocumentKind.PPM, status=E.DocumentStatus.READY)])
    return CpListing(**(values | changes))


def issues_by_code(row):
    issues = listings.publish_issues(row)
    assert listings.publish_blockers(row) == [item["message"] for item in issues]
    for issue in issues:
        PublishIssue.model_validate(issue)
    return {issue["code"]: issue for issue in issues}


@asynccontextmanager
async def api_harness():
    h = await harness()
    try:
        async with h:
            yield h
    finally:
        engine = h.db.bind
        await h.db.close()
        await engine.dispose()


@pytest.mark.parametrize(("change", "code", "message", "field", "linked_owner"), [
    ({"name": ""}, "name_required", "name", "name", "terms"),
    ({"note_rate_pct": None}, "note_rate_required", "note_rate_pct", "note_rate_pct", "coverage_plan"),
    ({"term_months": None}, "term_required", "term_months", "term_months", "coverage_plan"),
    ({"offered_amount": 0}, "offered_amount_required", "offered_amount", "offered_amount", "budget"),
    ({"minimum_investment": 0}, "minimum_investment_required", "minimum_investment", "minimum_investment", "budget"),
    ({"minimum_investment": 100001}, "minimum_exceeds_offered", "minimum_investment exceeds offered_amount", "minimum_investment", "budget"),
])
@pytest.mark.parametrize("linked", [False, True])
def test_missing_fields_target_editable_owner_and_keep_legacy_text(change, code, message, field, linked_owner, linked):
    row = listing(**change, profile_id=uuid4() if linked else None)
    issue = issues_by_code(row)[code]
    expected_owner = linked_owner if linked else "terms"
    assert issue == {"code": code, "message": message, "section": expected_owner,
                     **({"field": field} if expected_owner == "terms" else {})}


@pytest.mark.parametrize(("terms", "budget", "changes", "code", "message", "owner"), [
    ({"active_proposal": False}, None, {}, "proposal_inactive", "Restore or synchronize the working financing proposal before publishing; issued terms are a historical reference", "coverage_plan"),
    ({"recorded_terms_ref": {"version": 2}, "collateral_stale": True}, None, {}, "terms_outdated", "Save an updated terms version for the changed financing or collateral before publishing", "client_terms"),
    ({"recorded_terms_ref": {"version": 2}, "terms_state": "differs"}, None, {}, "terms_outdated", "Save an updated terms version for the changed financing or collateral before publishing", "client_terms"),
    ({}, {"stale": True, "calculation": {"status": "ready"}}, {}, "terms_budget_outdated", "Save an updated terms version for the changed cost budget before publishing", "client_terms"),
    ({"deal_budget_stale": True}, {"calculation": {"status": "ready"}}, {}, "terms_budget_outdated", "Save an updated terms version for the changed cost budget before publishing", "client_terms"),
    ({}, {"calculation": {"status": "incomplete"}}, {}, "budget_incomplete", "Confirm the closing cost budget, funding mode and investor charges before publishing", "budget"),
    ({}, {"calculation": {"status": "ready", "principal_solution": {"status": "calculated", "requires_apply": True}}}, {}, "principal_not_applied", "Apply the solved gross principal to the shared loan before publishing", "budget"),
    ({}, {"calculation": {"status": "ready", "principal_solution": {"status": "incomplete"}}}, {}, "principal_not_applied", "Apply the solved gross principal to the shared loan before publishing", "budget"),
    ({}, {"calculation": {"status": "ready", "allocation_reconciliation": {"status": "unallocated"}}}, {}, "allocation_unreconciled", "Reconcile the current use-of-funds allocations with net loan proceeds at funding before publishing", "use_of_funds"),
    ({}, {"current_allocation_reconciliation": {"status": "overallocated"}, "calculation": {"status": "ready"}}, {}, "allocation_unreconciled", "Reconcile the current use-of-funds allocations with net loan proceeds at funding before publishing", "use_of_funds"),
    ({"amount": 100000, "rate_pct": 9, "term_months": 24}, None, {"offered_amount": 100001}, "offered_exceeds_financing", "offered_amount exceeds the canonical financing amount", "budget"),
    ({"amount": 200000, "rate_pct": 9, "term_months": 24}, None, {}, "financing_outdated", "Refresh financing from the file before publishing", "snapshot"),
])
def test_saved_source_requirements_target_their_real_editor_without_mutation(terms, budget, changes, code, message, owner):
    financials = {"proposed_terms": terms}
    if budget is not None:
        financials["deal_budget"] = budget
    snapshot = {"financials": financials}
    original = deepcopy(snapshot)
    row = listing(profile_id=uuid4(), source_snapshot=snapshot, **changes)
    issue = issues_by_code(row)[code]
    assert issue == {"code": code, "message": message, "section": owner}
    assert row.source_snapshot == original


@pytest.mark.parametrize(("kind", "state", "blocked"), [
    (E.DocumentKind.PPM, E.DocumentStatus.PENDING, True),
    (E.DocumentKind.PPM, E.DocumentStatus.SUPERSEDED, True),
    (E.DocumentKind.NOTE, E.DocumentStatus.READY, True),
    (E.DocumentKind.PPM, E.DocumentStatus.READY, False),
])
def test_only_ready_ppm_satisfies_the_document_requirement(kind, state, blocked):
    row = listing()
    row.documents = [CpListingDocument(kind=kind, status=state)]
    issues = issues_by_code(row)
    assert ("ppm_required" in issues) is blocked
    if blocked:
        assert issues["ppm_required"] == {"code": "ppm_required", "message": "private placement memorandum not uploaded",
                                          "section": "documents", "document_kind": "ppm"}


@pytest.mark.asyncio
async def test_real_routes_return_matching_guidance_and_require_ready_upload_then_explicit_publish():
    async with api_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        with patch.multiple(storage, configured=lambda: True,
                            presign_put=lambda key, **_kwargs: {"upload_url": "https://example.com/upload", "headers": {}, "s3_key": key},
                            head_object=lambda _key: {"size_bytes": 123, "content_type": "application/pdf"}), \
                patch.object(emails, "_send", AsyncMock()) as send:
            row = await scenario.listing(admin, publish=False)
            path = f"/api/v1/capital/admin/listings/{row.id}"
            before = (await h.client.get(path)).json()
            assert before["publish_blockers"] == ["private placement memorandum not uploaded"]
            assert before["publish_issues"][0] == {"code": "ppm_required", "message": before["publish_blockers"][0],
                                                   "section": "documents", "field": None, "document_kind": "ppm"}
            blocked = await h.client.post(path + "/publish", json={"reason": "Reviewed synthetic listing", "confirmed": True})
            assert blocked.status_code == 409
            detail = blocked.json()["detail"]
            assert detail["code"] == "incomplete"
            assert detail["message"] == "Fill in: private placement memorandum not uploaded"
            assert detail["publish_issues"] == [{"code": "ppm_required", "message": before["publish_blockers"][0],
                                                "section": "documents", "document_kind": "ppm"}]
            assert row.status == "draft" and row.published_at is None
            init = await h.client.post(path + "/documents/upload-init", json={"kind": "ppm", "name": "Synthetic PPM.pdf"})
            assert init.status_code == 200
            # Production uses a new session per request; this shared-session
            # harness must reload the collection populated before upload-init.
            h.db.expire(row, ["documents"])
            assert (await h.client.get(path)).json()["publish_issues"]
            done = await h.client.post(path + "/documents/upload-complete", json={"document_id": init.json()["document_id"]})
            assert done.status_code == 200
            ready = (await h.client.get(path)).json()
            assert ready["publish_issues"] == ready["publish_blockers"] == []
            assert row.status == "draft" and row.published_at is None
            published = await h.client.post(path + "/publish", json={"reason": "Reviewed synthetic listing", "confirmed": True})
            assert published.status_code == 200
            assert published.json()["status"] == "open"
            assert published.json()["publish_issues"] == []
            send.assert_not_awaited()


@pytest.mark.asyncio
async def test_guidance_does_not_bypass_admin_acl_or_replace_field_validation_locations():
    async with api_harness() as h:
        scenario = Scenario(h)
        row = await scenario.listing(await scenario.admin(), publish=False)
        path = f"/api/v1/capital/admin/listings/{row.id}"
        invalid = await h.client.patch(path, json={"state": "invalid"})
        assert invalid.status_code == 422
        assert invalid.json()["detail"][0]["loc"] == ["body", "state"]
        nested = await h.client.patch(path, json={"products": [{"label": "Synthetic alternative", "rate_pct": 101}]})
        assert nested.status_code == 422
        assert nested.json()["detail"][0]["loc"] == ["body", "products", 0, "rate_pct"]
        h.current = await h.user("loan-exec@example.com", Role.LOAN_EXEC)
        for method, suffix, body in [("GET", "", None), ("POST", "/publish", {"confirmed": True, "reason": "Not authorized"})]:
            result = await h.client.request(method, path + suffix, json=body)
            assert result.status_code == 403
        assert row.status == "draft" and row.published_at is None
