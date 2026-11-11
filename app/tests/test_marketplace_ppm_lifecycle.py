"""Independent synthetic PPM lifecycle, access and immutable-version regressions.

The ASGI routes and database are real. AI, object storage and outbound messages
are replaced; no customer document, account or network is used.
"""

import asyncio
import json
from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capital_partners import enums as E
from app.capital_partners import models as cp
from app.capital_partners.services import ppm, storage
from app.db import get_db
from app.enums import Role
from app.tests.test_capital_partners import Scenario, _patches, harness


@asynccontextmanager
async def ppm_harness():
    h = await harness()
    # Mirror production commit/rollback semantics with a fresh identity map per
    # request; failed storage or stale actions must not leave partial receipts.
    async def request_db():
        await h.db.commit()
        async with AsyncSession(h.db.bind, expire_on_commit=False) as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    h.client._transport.app.dependency_overrides[get_db] = request_db
    try:
        async with h:
            with ExitStack() as stack:
                for replacement in _patches():
                    stack.enter_context(replacement)
                yield h
    finally:
        engine = h.db.bind
        await h.db.close()
        await engine.dispose()


def configure(monkeypatch, listing):
    context = {
        "fingerprint": "a" * 64,
        "listing": {"ref": listing.ref, "name": "Synthetic saved offering", "summary": "Saved business financing facts.",
            "loan_amount": 175000, "offered_amount": 100000, "minimum_investment": 25000,
            "note_rate_pct": 12, "term_months": 12, "payment_frequency": "monthly",
            "loan_dates": {"expected_funding_date": "2027-01-15", "maturity_date": "2028-01-15"},
            "collateral_type": "Equipment", "lien_position": "first", "personal_guarantee": False},
        "financials": {"proposed_terms": {"amount": 175000, "rate_pct": 12, "term_months": 12, "currency": "USD"}},
        "budget": {"currency": "USD", "servicing_mode": "company_managed", "rows": [{"label": "Synthetic servicing charge", "payer": "investor", "timing": "monthly", "total_amount": 600}],
            "calculation": {"net_client_cash": 160000, "borrower_paid_separately": 1000,
                "investor_scenarios": {"six_month_payoff": {"net_income": 5400, "net_roi_pct": 5.4, "principal_returned": 100000},
                    "full_term": {"net_income": 11400, "net_roi_pct": 11.4, "principal_returned": 100000}}}},
        "issuer": "Example Lending LLC", "missing": [], "warnings": ["Staff review is required."],
    }
    async def saved(*_args, **_kwargs):
        return deepcopy(context)
    renderer = Mock(return_value=b"%PDF-1.4\nSynthetic immutable PPM transport\n%%EOF\n")
    stored = Mock()
    monkeypatch.setattr(ppm, "saved_context", saved)
    monkeypatch.setattr(ppm, "get_settings", lambda: SimpleNamespace(bedrock_enabled=False))
    monkeypatch.setattr(ppm, "render_pdf", renderer)
    monkeypatch.setattr(ppm, "_put_pdf", stored)
    return context, renderer, stored


def legal_inputs():
    return {row["key"]: "Example Lending LLC" if row["key"] == "issuer_legal_name" else f"Staff-supplied synthetic disclosure: {row['label']}." for row in ppm.LEGAL_FIELDS if row["required"]}


async def state(h, listing):
    response = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/ppm")
    assert response.status_code == 200, response.text
    return response.json()


async def generate(h, listing, context, key=None, **extra):
    response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/generate", json={
        "expected_source_fingerprint": context["fingerprint"], "idempotency_key": key or str(uuid4()), **extra})
    assert response.status_code == 200, response.text
    return response.json()


async def completed_draft(h, listing, context, *, narrative=None):
    current = await generate(h, listing, context)
    response = await h.client.patch(f"/api/v1/capital/admin/listings/{listing.id}/ppm/draft", json={
        "expected_version": current["current_draft"]["version"], "expected_source_fingerprint": context["fingerprint"],
        "idempotency_key": str(uuid4()), "legal_inputs": legal_inputs(), "narrative_sections": narrative or {}})
    assert response.status_code == 200, response.text
    return response.json()


def review_payload(current, context, **extra):
    return {"expected_version": current["current_draft"]["version"], "expected_source_fingerprint": context["fingerprint"],
        "idempotency_key": str(uuid4()), "confirmed": True, "reason": "Reviewed synthetic facts and staff-supplied provisions", **extra}


async def legacy_ppm(h, listing, admin):
    """An existing uploaded document, without any generated-PPM adoption event."""
    row = cp.CpListingDocument(
        listing_id=listing.id,
        kind=E.DocumentKind.PPM,
        name="Synthetic existing offering memorandum.pdf",
        s3_key="synthetic-existing/immutable-ppm.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        version=3,
        status=E.DocumentStatus.READY,
        uploaded_by_id=admin.id,
        uploaded_at=datetime.now(UTC) - timedelta(days=10),
    )
    h.db.add(row)
    await h.db.flush()
    await h.db.refresh(listing, attribute_names=["documents"])
    return row


@pytest.mark.asyncio
async def test_legacy_ready_upload_remains_available_before_adoption_and_without_originals_grant():
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False, offered="100000")
        original = await legacy_ppm(h, listing, admin)
        saved = (original.s3_key, original.version, original.uploaded_at)
        current = await state(h, listing)
        assert current["managed"] is False
        assert current["current_approved"]["source"] == "uploaded"
        assert current["current_approved"]["document_id"] == str(original.id)

        published = await h.client.post(
            f"/api/v1/capital/admin/listings/{listing.id}/publish",
            json={"confirmed": True, "reason": "Review of existing synthetic offering"},
        )
        assert published.status_code == 200, published.text
        user, _member = await scenario.member("existing-ppm-investor@example.test")
        h.current = user
        member_path = f"/api/v1/capital/listings/{listing.ref}"
        detail = await h.client.get(member_path)
        assert detail.status_code == 200, detail.text
        assert [row["id"] for row in detail.json()["documents"]] == [str(original.id)]
        link = await h.client.post(member_path + f"/documents/{original.id}/url")
        assert link.status_code == 200, link.text
        assert link.json()["expires_in_seconds"] == 60
        subscribed = await h.client.post(
            member_path + "/subscriptions",
            json={"amount": "25000", "ack_ppm": True, "ack_accredited": True},
        )
        assert subscribed.status_code == 201, subscribed.text
        assert (original.s3_key, original.version, original.uploaded_at) == saved
        grants = (await h.db.execute(select(cp.CpListingDocumentGrant))).scalars().all()
        assert grants == []


@pytest.mark.asyncio
async def test_adoption_keeps_the_legacy_upload_in_staff_history_without_member_access(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        original = await legacy_ppm(h, listing, admin)
        context, _renderer, _stored = configure(monkeypatch, listing)
        drafted = await generate(h, listing, context)
        assert any(row["document_id"] == str(original.id) for row in drafted["history"])
        historical = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/versions/{original.id}/url?download=true")
        assert historical.status_code == 200, historical.text
        assert drafted["current_approved"] is None or drafted["current_approved"]["stale"]
        await h.db.refresh(original)
        assert original.s3_key == "synthetic-existing/immutable-ppm.pdf" and original.version == 3


@pytest.mark.asyncio
async def test_generated_draft_review_publication_and_source_staleness_are_separate(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False, offered="100000")
        context, renderer, stored = configure(monkeypatch, listing)
        before = await state(h, listing)
        assert before["status"] == "not_started" and before["history"] == []
        drafted = await generate(h, listing, context)
        first = drafted["current_draft"]
        assert drafted["status"] == "draft" and first["version"] == 1
        assert drafted["current_approved"] is None and drafted["missing"]
        assert first["source"] == "generated_ppm" and renderer.call_count == stored.call_count == 1
        refused = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(drafted, context))
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ppm_incomplete"
        current = await completed_draft(h, listing, context, narrative={"overview": "Retained staff description."})
        saved_draft = deepcopy(current["current_draft"])
        approved = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(current, context))
        assert approved.status_code == 200, approved.text
        approval = approved.json()["current_approved"]
        assert approval["version"] == saved_draft["version"] + 1
        assert approval["document_id"] != saved_draft["document_id"]
        assert approved.json()["status"] == "approved"
        assert any(row["document_id"] == first["document_id"] for row in approved.json()["history"])
        detail = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}")
        assert detail.json()["status"] == "draft"  # Review does not publish.
        published = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/publish", json={"confirmed": True, "reason": "Explicit synthetic publication"})
        assert published.status_code == 200, published.text
        member, _ = await scenario.member("current-ppm-investor@example.test")
        h.current = member
        member_path = f"/api/v1/capital/listings/{listing.ref}"
        visible = await h.client.get(member_path)
        assert [row["id"] for row in visible.json()["documents"]] == [approval["document_id"]]
        assert (await h.client.post(member_path + f"/documents/{first['document_id']}/url")).status_code == 404
        assert (await h.client.post(member_path + f"/documents/{approval['document_id']}/url")).status_code == 200
        context["fingerprint"] = "b" * 64
        context["financials"]["proposed_terms"]["amount"] = 225000
        stale = await h.client.get(member_path)
        assert stale.json()["documents"] == []
        assert stale.json()["can_subscribe"] is False
        assert (await h.client.post(member_path + f"/documents/{approval['document_id']}/url")).status_code == 404
        refused = await h.client.post(member_path + "/subscriptions", json={"amount": "25000", "ack_ppm": True, "ack_accredited": True})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ppm_review_required"
        h.current = admin
        refreshed = await generate(h, listing, context)
        assert refreshed["status"] == "draft" and refreshed["current_draft"]["version"] > approval["version"]
        assert refreshed["current_draft"]["narrative_sections"]["overview"] == "Retained staff description."
        assert refreshed["current_approved"]["stale"] is True


@pytest.mark.asyncio
async def test_same_generation_intent_replays_and_changed_intent_or_actor_conflicts(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, renderer, stored = configure(monkeypatch, listing)
        key = str(uuid4())
        first = await generate(h, listing, context, key)
        replay = await generate(h, listing, context, key)
        assert replay["current_draft"]["document_id"] == first["current_draft"]["document_id"]
        assert len(replay["history"]) == renderer.call_count == stored.call_count == 1
        path = f"/api/v1/capital/admin/listings/{listing.id}/ppm/generate"
        changed = await h.client.post(path, json={"expected_source_fingerprint": context["fingerprint"], "idempotency_key": key, "force": True})
        assert changed.status_code == 409 and changed.json()["detail"]["code"] == "ppm_idempotency_conflict"
        h.current = await h.user("other-ppm-staff@example.test", Role.SUPER_ADMIN)
        different_actor = await h.client.post(path, json={"expected_source_fingerprint": context["fingerprint"], "idempotency_key": key})
        assert different_actor.status_code == 409
        assert renderer.call_count == stored.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["render", "storage", "source_race"])
async def test_failed_generation_preserves_existing_upload_and_leaves_no_partial_receipts(monkeypatch, failure):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        original = await legacy_ppm(h, listing, admin)
        context, renderer, stored = configure(monkeypatch, listing)
        if failure == "render":
            renderer.return_value = b"not a PDF"
        elif failure == "storage":
            from fastapi import HTTPException
            stored.side_effect = HTTPException(503, "Synthetic storage unavailable")
        else:
            calls = 0
            async def raced(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                return {**deepcopy(context), "fingerprint": context["fingerprint"] if calls == 1 else "b" * 64}
            monkeypatch.setattr(ppm, "saved_context", raced)
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/generate", json={
            "expected_source_fingerprint": context["fingerprint"], "idempotency_key": str(uuid4())})
        assert response.status_code == (409 if failure == "source_race" else 503), response.text
        await h.db.refresh(original)
        assert original.status == E.DocumentStatus.READY and original.version == 3
        receipts = (await h.db.execute(select(cp.CpListingEvent).where(cp.CpListingEvent.kind.like("ppm.%")))).scalars().all()
        assert receipts == []
        docs = (await h.db.execute(select(cp.CpListingDocument).where(cp.CpListingDocument.listing_id == listing.id))).scalars().all()
        assert [row.id for row in docs] == [original.id]


@pytest.mark.asyncio
async def test_review_requires_current_version_facts_confirmation_and_available_pdf(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        path = f"/api/v1/capital/admin/listings/{listing.id}/ppm/review"
        for override, expected in [({"confirmed": False}, 422), ({"expected_version": 1}, 409), ({"expected_source_fingerprint": "old"}, 409)]:
            response = await h.client.post(path, json=review_payload(current, context, **override))
            assert response.status_code == expected, response.text
        with patch.object(storage, "head_object", return_value=None):
            missing_pdf = await h.client.post(path, json=review_payload(current, context))
        assert missing_pdf.status_code == 409 and missing_pdf.json()["detail"]["code"] == "ppm_pdf_missing"
        assert (await state(h, listing))["current_approved"] is None


@pytest.mark.asyncio
async def test_forged_snapshot_cannot_approve_and_draft_history_access_is_scoped(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        current = await generate(h, listing, context)
        draft_id = current["current_draft"]["document_id"]
        listing.source_snapshot = {"ppm": {"status": "approved", "source_fingerprint": context["fingerprint"], "document_id": draft_id}, "ppm_approved": True}
        await h.db.flush()
        assert (await state(h, listing))["current_approved"] is None
        url = f"/api/v1/capital/admin/listings/{listing.id}/ppm/versions/{draft_id}/url?download=true"
        assert (await h.client.post(url)).status_code == 200
        other = await scenario.listing(admin, publish=False)
        wrong = await h.client.post(f"/api/v1/capital/admin/listings/{other.id}/ppm/versions/{draft_id}/url")
        assert wrong.status_code == 404
        for role in (Role.LOAN_EXEC, Role.CAPITAL_PARTNER):
            h.current = await h.user(f"no-ppm-{role.value}@example.test", role)
            assert (await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/ppm")).status_code == 403
            assert (await h.client.post(url)).status_code == 403


@pytest.mark.asyncio
async def test_superseded_approved_ppm_needs_new_draft_even_when_source_facts_are_unchanged(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        drafted = await completed_draft(h, listing, context)
        reviewed = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(drafted, context))
        assert reviewed.status_code == 200, reviewed.text
        approved = reviewed.json()["current_approved"]
        removed = await h.client.delete(f"/api/v1/capital/admin/listings/{listing.id}/documents/{approved['document_id']}")
        assert removed.status_code == 200, removed.text
        stale = await state(h, listing)
        assert stale["status"] == "stale" and stale["current_approved"]["stale"] is True
        fresh = await generate(h, listing, context)
        assert fresh["status"] == "draft" and fresh["current_draft"]["version"] > approved["version"]
        assert fresh["current_draft"]["document_id"] != approved["document_id"]


@pytest.mark.parametrize("profit", [0, -1200])
def test_rendered_facts_separate_principal_net_profit_currency_and_private_source_data(monkeypatch, profit):
    context, _renderer, _stored = configure(monkeypatch, SimpleNamespace(ref="CP-FACTS-QA"))
    context["private_source"] = {"account_number": "PRIVATE_ACCOUNT_SENTINEL", "tax_id": "PRIVATE_TAX_SENTINEL", "s3_key": "private/never-in-ppm"}
    context["budget"]["rows"][0]["file_name"] = "PRIVATE_FILENAME_SENTINEL"
    context["budget"]["calculation"]["investor_scenarios"]["six_month_payoff"]["net_income"] = profit
    document = ppm._document(context, legal_inputs(), {})
    facts = {fact["label"]: fact["value"] for section in document["sections"] for fact in section["facts"]}
    assert facts["Loan principal"] == "175,000.00 USD"
    assert facts["Capital offered"] == "100,000.00 USD"
    assert facts["Net borrower cash at funding"] == "160,000.00 USD"
    assert facts["Payoff at six months, capped at maturity: net profit"] == f"{profit:,.2f} USD"
    assert facts["Payoff at six months, capped at maturity: principal returned"] == "100,000.00 USD"
    assert facts["Expected funding"] == "2027-01-15" and facts["Maturity"] == "2028-01-15"
    assert "PRIVATE_" not in json.dumps(document) and "private/never-in-ppm" not in json.dumps(document)
    context["budget"]["currency"] = None
    context["financials"]["proposed_terms"]["currency"] = None
    unknown = ppm._document(context, legal_inputs(), {})
    assert "currency unconfirmed" in json.dumps(unknown) and " USD" not in json.dumps(unknown)
    explicit = ppm._document(context, {**legal_inputs(), "offering_currency": "CAD"}, {})
    assert "175,000.00 CAD" in json.dumps(explicit)


@pytest.mark.asyncio
async def test_manual_upload_is_frozen_and_metadata_review_preserves_its_bytes(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        original = await legacy_ppm(h, listing, admin)
        context, renderer, stored = configure(monkeypatch, listing)
        objects = {original.s3_key: b"%PDF-1.4\nSynthetic original memorandum\n%%EOF"}
        copies = []
        def copy(source, target, **_kwargs):
            copies.append((source, target))
            objects[target] = objects[source]
            return True
        def head(key):
            return {"size_bytes": len(objects[key]), "content_type": "application/pdf"} if key in objects else None
        with patch.object(storage, "copy_object", copy), patch.object(storage, "head_object", head):
            base = f"/api/v1/capital/admin/listings/{listing.id}"
            initialized = await h.client.post(base + "/documents/upload-init", json={"kind": "ppm", "name": "Synthetic staff-authored PPM.pdf"})
            assert initialized.status_code == 200, initialized.text
            upload = initialized.json()
            supplied = b"%PDF-1.4\nSynthetic external staff-authored provisions\n%%EOF"
            objects[upload["s3_key"]] = supplied
            complete = await h.client.post(base + "/documents/upload-complete", json={"document_id": upload["document_id"]})
            assert complete.status_code == 200 and complete.json()["status"] == "pending", complete.text
            current = await state(h, listing)
            assert current["current_draft"]["source"] == "uploaded"
            draft = current["current_draft"]
            payload = {"expected_version": draft["version"], "expected_source_fingerprint": context["fingerprint"],
                "idempotency_key": str(uuid4()), "legal_inputs": legal_inputs(), "narrative_sections": {"overview": "Staff review metadata for the uploaded memorandum."}}
            saved = await h.client.patch(base + "/ppm/draft", json=payload)
            assert saved.status_code == 200, saved.text
            reviewed = await h.client.post(base + "/ppm/review", json=review_payload(saved.json(), context))
            assert reviewed.status_code == 200, reviewed.text
            approved = reviewed.json()["current_approved"]
            assert approved["source"] == "uploaded" and approved["document_id"] == saved.json()["current_draft"]["document_id"]
            rows = (await h.db.execute(select(cp.CpListingDocument).where(cp.CpListingDocument.listing_id == listing.id).execution_options(populate_existing=True))).scalars().all()
            active = next(row for row in rows if str(row.id) == approved["document_id"])
            assert objects[active.s3_key] == supplied and active.s3_key != upload["s3_key"]
            assert all(objects[target] == supplied for _source, target in copies)
            objects[upload["s3_key"]] = b"changed after the upload link was issued"
            assert objects[active.s3_key] == supplied
            assert renderer.call_count == stored.call_count == 0
            assert original.s3_key in objects and objects[original.s3_key].startswith(b"%PDF-1.4")


@pytest.mark.asyncio
async def test_ai_unavailable_falls_back_to_a_nonready_saved_fact_draft(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        monkeypatch.setattr(ppm, "get_settings", lambda: SimpleNamespace(bedrock_enabled=True))
        monkeypatch.setattr(ppm, "_select_sections", AsyncMock(side_effect=TimeoutError("Synthetic AI timeout")))
        result = await generate(h, listing, context)
        assert result["generation_status"] == "source_based" and result["status"] == "draft"
        assert result["current_approved"] is None and result["missing"]
        assert any("unavailable" in warning for warning in result["current_draft"]["document"]["warnings"])


@pytest.mark.asyncio
async def test_blank_manual_override_restores_saved_ai_wording_without_erasing_the_suggestion(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        sentence = "This offering relates to financing an operating business."
        async def selected(_db, document, *_args, **_kwargs):
            return {"section_order": [section["key"] for section in document["sections"]], "ai_sections": {"overview": sentence}, "citations": [{"section": "overview", "source_keys": ["business_context"]}]}
        monkeypatch.setattr(ppm, "get_settings", lambda: SimpleNamespace(bedrock_enabled=True))
        monkeypatch.setattr(ppm, "_select_sections", selected)
        current = await completed_draft(h, listing, context, narrative={"overview": "Staff customized wording."})
        response = await h.client.patch(f"/api/v1/capital/admin/listings/{listing.id}/ppm/draft", json={
            "expected_version": current["current_draft"]["version"], "expected_source_fingerprint": context["fingerprint"],
            "idempotency_key": str(uuid4()), "legal_inputs": legal_inputs(), "narrative_sections": {"overview": "   "}})
        assert response.status_code == 200, response.text
        current = response.json()["current_draft"]
        assert "overview" not in current["narrative_sections"]
        assert current["ai_sections"]["overview"] == sentence
        overview = next(section for section in current["document"]["sections"] if section["key"] == "overview")
        assert sentence in overview["paragraphs"] and "Staff customized wording." not in overview["paragraphs"]


@pytest.mark.asyncio
async def test_saved_fact_authority_ignores_own_receipts_but_changes_with_issuer_or_offering_facts():
    issuer = SimpleNamespace(issuer_name="Example Lending LLC")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: issuer)))
    listing = cp.CpListing(ref="CP-AUTHORITY-QA", name="Synthetic saved offering", vertical="business",
        note_rate_pct=12, term_months=12, loan_amount=175000, offered_amount=100000, minimum_investment=25000,
        source_snapshot={}, documents=[], products=[], payment_frequency="monthly", lien_position="first")
    first = await ppm.saved_context(db, listing)
    listing.source_snapshot = {"ppm": {"status": "approved", "at": "2026-11-11T15:00:00Z"},
        "published": {"at": "2026-11-11T16:00:00Z"}, "document_access": ["PRIVATE_ACCOUNT_SENTINEL"], "grant_reason": "PRIVATE_TAX_SENTINEL"}
    unchanged = await ppm.saved_context(db, listing)
    assert first["fingerprint"] == unchanged["fingerprint"]
    assert "PRIVATE_" not in json.dumps(unchanged["ai_facts"])
    assert any(row["key"] == "currency" for row in unchanged["missing"])
    assert not any(row["key"] == "currency" for row in ppm._missing(unchanged, {**legal_inputs(), "offering_currency": "CAD"}))
    issuer.issuer_name = "Another Example Issuer LLC"
    changed_issuer = await ppm.saved_context(db, listing)
    assert changed_issuer["fingerprint"] != first["fingerprint"]
    listing.loan_amount = 225000
    changed_amount = await ppm.saved_context(db, listing)
    assert changed_amount["fingerprint"] != changed_issuer["fingerprint"]


@pytest.mark.asyncio
async def test_paused_model_releases_transaction_and_cannot_overwrite_a_newer_staff_draft(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        initial = await generate(h, listing, context)
        entered, release = asyncio.Event(), asyncio.Event()
        transaction_free = []
        async def paused(db, document, *_args, **_kwargs):
            transaction_free.append(not db.in_transaction())
            entered.set()
            await release.wait()
            return [row["key"] for row in document["sections"]]
        monkeypatch.setattr(ppm, "get_settings", lambda: SimpleNamespace(bedrock_enabled=True))
        monkeypatch.setattr(ppm, "_select_sections", paused)
        base = f"/api/v1/capital/admin/listings/{listing.id}/ppm"
        generating = asyncio.create_task(h.client.post(base + "/generate", json={
            "expected_source_fingerprint": context["fingerprint"], "idempotency_key": str(uuid4()), "force": True}))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            manual = await asyncio.wait_for(h.client.patch(base + "/draft", json={
                "expected_version": initial["current_draft"]["version"], "expected_source_fingerprint": context["fingerprint"],
                "idempotency_key": str(uuid4()), "legal_inputs": legal_inputs(),
                "narrative_sections": {"overview": "Newer staff entries while the model is paused."}}), timeout=3)
        finally:
            release.set()
            result = await generating
        assert manual.status_code == 200, manual.text
        assert transaction_free == [True], "Do not retain a database transaction across a model request"
        assert result.status_code == 409, result.text
        latest = await state(h, listing)
        assert latest["current_draft"]["document_id"] == manual.json()["current_draft"]["document_id"]
        assert latest["current_draft"]["narrative_sections"]["overview"] == "Newer staff entries while the model is paused."
        assert len(latest["history"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "number", "legal", "name", "unknown_source", "extra_field", "omitted_section"])
async def test_actual_ai_selector_receives_no_private_document_data_and_rejects_invented_claims(monkeypatch, fault):
    from app.services.ai import bedrock_client, usage
    document = {"sections": [
        {"key": "overview", "title": "Overview", "paragraphs": ["PRIVATE_ACCOUNT_SENTINEL"], "facts": [{"value": "PRIVATE_TAX_ID_SENTINEL"}]},
        {"key": "terms", "title": "Terms", "facts": [{"value": "175000"}]},
    ]}
    fact = "This offering relates to financing an operating business."
    paragraph = {"section": "overview", "paragraph": fact, "source_keys": ["business_context"]}
    result = {"section_order": ["overview", "terms"], "paragraphs": [paragraph]}
    if fault == "number":
        paragraph["paragraph"] = "This offering provides a 12 percent return."
    elif fault == "legal":
        paragraph["paragraph"] = "This offering is approved and exempt from all restrictions."
    elif fault == "name":
        paragraph["paragraph"] = "This offering relates to Acme Unicorn Corporation."
    elif fault == "unknown_source":
        paragraph["source_keys"] = ["private_account"]
    elif fault == "extra_field":
        result["approved"] = True
    elif fault == "omitted_section":
        result["section_order"] = ["overview"]
    response = SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", name="order_ppm_sections", input=result)])
    tracked = AsyncMock(return_value=response)
    monkeypatch.setattr(usage, "tracked_messages_create", tracked)
    monkeypatch.setattr(bedrock_client, "get_client", lambda: object())
    monkeypatch.setattr(bedrock_client, "model_light", lambda: "synthetic-model")
    if fault:
        with pytest.raises(ValueError):
            await ppm._select_sections(None, document, None, {"business_context": fact})
    else:
        selected = await ppm._select_sections(None, document, None, {"business_context": fact})
        assert selected["ai_sections"] == {"overview": fact}
    sent = json.dumps(tracked.call_args.kwargs["messages"])
    assert "PRIVATE_ACCOUNT_SENTINEL" not in sent and "PRIVATE_TAX_ID_SENTINEL" not in sent and "175000" not in sent
    assert "business_context" in sent
