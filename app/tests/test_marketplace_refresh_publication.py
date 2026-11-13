"""Explicit publication selects economics and PDFs together, never background work."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.capital_partners import enums as E
from app.capital_partners.models import CpListingDocument
from app.capital_partners.services import listings, package_documents, refresh, sync
from app.tests.test_capital_partners import Scenario
from app.tests.test_marketplace_ppm_lifecycle import (
    completed_draft,
    configure,
    ppm_harness,
    review_payload,
    state,
)


@pytest.mark.asyncio
async def test_pending_figures_pdfs_and_review_status_follow_explicit_publish_only(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _, _ = configure(monkeypatch, listing)
        await h.db.refresh(listing, attribute_names=["documents"])
        old_doc = CpListingDocument(listing_id=listing.id, kind=E.DocumentKind.INVESTOR_PRESENTATION,
            name="Synthetic old presentation.pdf", s3_key="old.pdf", version=1, status="ready", content_type="application/pdf")
        listing.documents.append(old_doc)
        h.db.add(old_doc)
        listing.source_snapshot = {"financials": {"net_profit": -10000}, "published": {"package": {"documents": []}}}
        initial = await completed_draft(h, listing, context)
        assert (await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review",
            json=review_payload(initial, context))).status_code == 200
        publish_url = f"/api/v1/capital/admin/listings/{listing.id}/publish"
        published = await h.client.post(publish_url, json={"reason": "Initial synthetic offer", "confirmed": True})
        assert published.status_code == 200, published.text
        await h.db.refresh(listing)
        await h.db.refresh(listing, attribute_names=["documents"])
        before = deepcopy(listing.source_snapshot)
        old_rate = listing.note_rate_pct
        listing.note_rate_pct = 27
        listing.source_snapshot = {**listing.source_snapshot, "financials": {"net_profit": 50000,
            "operating_projection": {"qualification": "proposed", "saved_version": {"version_number": 4}}}}
        monkeypatch.setattr(package_documents.storage, "copy_object", Mock(return_value=True))
        receipts = await package_documents.attach_generated_documents(h.db, listing,
            [(SimpleNamespace(id=uuid4(), s3_key="new.pdf"), E.DocumentKind.INVESTOR_PRESENTATION, "Presentation")],
            actor_id=None, now=listings.now_utc())
        sync.mark_published(listing, {}, by=None, package={"documents": receipts})
        new_doc = next(row for row in listing.documents if str(row.id) == receipts[0]["document_id"])
        assert old_doc.status == "ready"
        assert listings.member_view(listing).note_rate_pct == str(old_rate)
        assert listings.partner_financials(listing)["net_profit"] == -10000
        assert listings.partner_financials(listing)["pending_operating_projection"]["saved_version"]["version_number"] == 4
        # Restore wording creates a new unapproved candidate, without activation.
        current = await state(h, listing)
        restored = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/versions/{initial['current_draft']['document_id']}/reinstate",
            json={"expected_version": current["latest_version"], "expected_source_fingerprint": context["fingerprint"], "idempotency_key": str(uuid4())})
        assert restored.status_code == 200, restored.text
        refresh._put(listing, {"job_id": str(uuid4()), "status": "succeeded", "review_required": True, "publication_required": True})
        await h.db.commit()
        investor, _ = await scenario.member("active-refresh@example.test")
        h.current = investor
        detail = await h.client.get(f"/api/v1/capital/listings/{listing.ref}")
        assert detail.status_code == 200
        assert detail.json()["note_rate_pct"] == str(old_rate)
        assert detail.json()["financials"]["net_profit"] == -10000
        assert detail.json()["financials"]["financial_update_pending"] is True
        doc_url = f"/api/v1/capital/listings/{listing.ref}/documents"
        assert (await h.client.post(doc_url + f"/{old_doc.id}/url")).status_code == 200
        assert (await h.client.post(doc_url + f"/{new_doc.id}/url")).status_code == 404
        h.current = admin
        status_url = f"/api/v1/capital/admin/listings/{listing.id}/refresh"
        assert (await h.client.get(status_url)).json()["review_required"] is True
        reviewed = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review",
            json=review_payload(restored.json(), context))
        assert reviewed.status_code == 200, reviewed.text
        flags = (await h.client.get(status_url)).json()
        assert flags["review_required"] is False and flags["publication_required"] is True
        updated = await h.client.post(publish_url, json={"reason": "Publish synthetic update explicitly", "confirmed": True})
        assert updated.status_code == 200, updated.text
        await h.db.refresh(listing)
        await h.db.refresh(listing, attribute_names=["documents"])
        flags = (await h.client.get(status_url)).json()
        assert flags["review_required"] is False and flags["publication_required"] is False
        assert listing.source_snapshot["active_financials"]["net_profit"] == 50000
        assert before["active_financials"]["net_profit"] == -10000
        assert old_doc.status == "superseded" and new_doc.status == "ready"
        h.current = investor
        assert (await h.client.post(doc_url + f"/{old_doc.id}/url")).status_code == 404
        assert (await h.client.post(doc_url + f"/{new_doc.id}/url")).status_code == 200
