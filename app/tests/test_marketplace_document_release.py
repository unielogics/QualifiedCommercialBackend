"""Synthetic package attachment, document transport and investor release tests."""

from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.capital_partners import enums as E
from app.capital_partners import models as cp
from app.capital_partners.services import document_catalog as catalog
from app.capital_partners.services import listings, storage, sync
from app.capital_partners.services.package_documents import attach_generated_documents
from app.enums import Role
from app.tests.test_capital_partners import Scenario, _patches, harness


@asynccontextmanager
async def document_harness():
    h = await harness()
    try:
        async with h:
            yield h
    finally:
        engine = h.db.bind
        await h.db.close()
        await engine.dispose()


def source_row(*, category="bank_statement", releasable=True, protected=False):
    file = SimpleNamespace(
        id=uuid4(),
        file_name="Private original.pdf",
        s3_key="private/never-in-catalog.pdf",
        content_hash="a" * 64,
        s3_version_id="immutable-version",
        document_version_number=1,
        content_type="application/pdf",
        size_bytes=1200,
    )
    return {
        "file": file,
        "category": category,
        "year": 2025,
        "releasable": releasable,
        "previewable": not protected,
    }


def package_artifacts():
    return [
        (SimpleNamespace(id=uuid4(), s3_key=f"source/{kind}.pdf"), kind, name)
        for kind, name in (
            (E.DocumentKind.EXECUTIVE_SUMMARY, "Executive credit brief"),
            (E.DocumentKind.UNDERWRITING_PACKAGE, "Underwriting package"),
            (E.DocumentKind.INVESTOR_PRESENTATION, "Investor presentation"),
        )
    ]


@pytest.mark.asyncio
async def test_generated_documents_are_idempotent_visible_and_never_satisfy_ppm():
    async with document_harness() as h:
        with ExitStack() as stack:
            for mocked in _patches():
                stack.enter_context(mocked)
            copy = stack.enter_context(patch.object(storage, "copy_object", return_value=True))
            scenario = Scenario(h)
            admin = await scenario.admin()
            listing = await scenario.listing(admin, publish=False)
            await h.db.refresh(listing, attribute_names=["documents"])
            artifacts = package_artifacts()
            first = await attach_generated_documents(
                h.db, listing, artifacts, actor_id=admin.id, now=datetime.now(UTC)
            )
            sync.mark_published(
                listing, {}, by=admin.id, package={"generation_id": "synthetic", "documents": first}
            )
            await h.db.flush()
            second = await attach_generated_documents(
                h.db, listing, artifacts, actor_id=admin.id, now=datetime.now(UTC)
            )
            assert first == second and copy.call_count == 3
            assert len(listing.documents) == 3 and all(
                doc.size_bytes == 1234 for doc in listing.documents
            )
            detail = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}")
            assert detail.status_code == 200, detail.text
            assert {doc["source"] for doc in detail.json()["documents"]} == {"generated_package"}
            assert len(detail.json()["documents"]) == 3
            refused = await h.client.post(
                f"/api/v1/capital/admin/listings/{listing.id}/publish",
                json={"confirmed": True, "reason": "Synthetic review"},
            )
            assert refused.status_code == 409 and "private placement memorandum" in refused.text
            init = await h.client.post(
                f"/api/v1/capital/admin/listings/{listing.id}/documents/upload-init",
                json={"kind": "ppm", "name": "Synthetic PPM.pdf"},
            )
            # The common ASGI harness shares one session across requests; reload
            # the collection as a real next-request session would do.
            await h.db.refresh(listing, attribute_names=["documents"])
            done = await h.client.post(
                f"/api/v1/capital/admin/listings/{listing.id}/documents/upload-complete",
                json={"document_id": init.json()["document_id"]},
            )
            assert done.status_code == 200, done.text
            assert done.json()["source"] == "uploaded"
            assert (
                await h.client.post(
                    f"/api/v1/capital/admin/listings/{listing.id}/publish",
                    json={"confirmed": True, "reason": "Synthetic review"},
                )
            ).status_code == 200
            user, _member = await scenario.member()
            h.current = user
            member_detail = await h.client.get(f"/api/v1/capital/listings/{listing.ref}")
            assert member_detail.status_code == 200 and len(member_detail.json()["documents"]) == 4
            assert (
                await h.client.post(
                    f"/api/v1/capital/listings/{listing.ref}/documents/{first[0]['document_id']}/url"
                )
            ).json()["expires_in_seconds"] == 60
            h.current = admin
            artifacts[0][0].id = uuid4()
            third = await attach_generated_documents(
                h.db, listing, artifacts, actor_id=admin.id, now=datetime.now(UTC)
            )
            assert third[0]["version"] == 2 and third[1:] == first[1:] and copy.call_count == 4
            assert len(listings.ready_documents(listing)) == 4  # three generated + actual PPM


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["copy", "missing_head", "empty_head", "source_key"])
async def test_failed_package_preflight_does_not_change_document_rows(failure):
    listing = SimpleNamespace(id=uuid4(), source_snapshot={}, documents=[])
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    artifacts = package_artifacts()
    if failure == "source_key":
        artifacts[-1][0].s3_key = None
    with (
        patch.object(storage, "copy_object", return_value=failure != "copy") as copy,
        patch.object(
            storage,
            "head_object",
            return_value=None
            if failure == "missing_head"
            else {"size_bytes": 0 if failure == "empty_head" else 1200},
        ),
    ):
        with pytest.raises(HTTPException):
            await attach_generated_documents(
                db, listing, artifacts, actor_id=uuid4(), now=datetime.now(UTC)
            )
    assert listing.documents == [] and db.add.call_count == 0
    if failure == "source_key":
        copy.assert_not_called()


def test_summary_allowlist_strips_identifiers_and_nonfinite_or_untyped_values():
    value = {
        "bank": {
            "months": [
                {
                    "month": "2025-01",
                    "deposits": "10000",
                    "withdrawals": 5000,
                    "net": 5000,
                    "currency": "USD",
                    "account_number": "SECRET-ACCOUNT",
                    "merchant": "SECRET-MERCHANT",
                    "file_id": str(uuid4()),
                }
            ]
        },
        "tax": {
            "years": [
                {
                    "year": "2025",
                    "gross_receipts": 100000,
                    "income": -100,
                    "currency": "USD",
                    "tin": "SECRET-TIN",
                    "entity": "PRIVATE-NAME",
                    "source_refs": [{"secret": True}],
                    "income_change_pct": float("nan"),
                }
            ]
        },
    }
    safe = catalog.safe_document_summaries(value)
    assert safe["bank_activity"][0]["deposits"] == 10000 and safe["tax_years"][0]["income"] == -100
    assert safe["tax_years"][0]["income_change_pct"] is None
    assert all(
        secret not in str(safe)
        for secret in ("SECRET", "PRIVATE", "file_id", "source_refs", "account_number")
    )
    assert catalog.safe_document_summaries(safe) == safe
    assert catalog.safe_document_summaries({"bank": "PRIVATE", "tax": []}) == {
        "bank_activity": [],
        "tax_years": [],
    }


@pytest.mark.asyncio
async def test_legacy_nonobject_financials_never_escape_or_break_locked_catalog():
    listing = SimpleNamespace(profile_id=None, source_snapshot={"financials": ["PRIVATE"]})
    with patch.object(catalog, "source_inventory", AsyncMock(return_value=[])):
        result = await catalog.catalog(SimpleNamespace(), listing)
    assert result == {
        "access": {"state": "locked"},
        "groups": [],
        "originals": [],
        "summaries": {"bank_activity": [], "tax_years": []},
    }


@pytest.mark.asyncio
async def test_release_is_explicit_scoped_version_bound_audited_and_revocable():
    async with document_harness() as h:
        with ExitStack() as stack:
            for mocked in _patches():
                stack.enter_context(mocked)
            scenario = Scenario(h)
            admin = await scenario.admin()
            listing = await scenario.listing(admin)
            listing.profile_id = uuid4()
            await h.db.flush()
            user, member = await scenario.member()
            other_user, _other_member = await scenario.member("other@example.test")
            row = source_row()
            rows = [row]
            stack.enter_context(
                patch.object(catalog, "source_inventory", AsyncMock(side_effect=lambda *_: rows))
            )
            base = f"/api/v1/capital/listings/{listing.ref}/file-documents"
            admin_base = f"/api/v1/capital/admin/listings/{listing.id}"
            h.current = user
            locked = (await h.client.get(base)).json()
            assert locked["access"]["state"] == "locked" and locked["originals"] == []
            assert (
                str(row["file"].id) not in str(locked)
                and row["file"].file_name not in str(locked)
                and row["file"].s3_key not in str(locked)
            )
            assert (await h.client.post(f"{base}/{row['file'].id}/url")).status_code == 403
            assert (
                await h.client.post(
                    f"/api/v1/capital/listings/{listing.ref}/documents/{row['file'].id}/url"
                )
            ).status_code == 404
            h.current = admin
            endpoint = f"{admin_base}/document-grants/{member.id}"
            assert (
                await h.client.put(
                    endpoint, json={"status": "active", "reason": "Confirmed engagement"}
                )
            ).status_code == 422
            approved = await h.client.put(
                endpoint,
                json={
                    "status": "active",
                    "reason": "Confirmed engagement",
                    "engagement_confirmed": True,
                },
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["file_count"] == 1
            assert (await h.client.get(f"{admin_base}/document-grants")).json()[0][
                "member_id"
            ] == str(member.id)
            assert (
                await h.client.post(
                    f"{admin_base}/file-documents/{row['file'].id}/url?download=true"
                )
            ).status_code == 200
            h.current = user
            assert (await h.client.get(base)).json()["originals"][0]["file_id"] == str(
                row["file"].id
            )
            assert (await h.client.post(f"{base}/{row['file'].id}/url?download=true")).json()[
                "expires_in_seconds"
            ] == 60
            h.current = other_user
            assert (await h.client.get(base)).json()["originals"] == []
            assert (await h.client.post(f"{base}/{row['file'].id}/url")).status_code == 403
            h.current = user
            later = source_row()
            rows.append(later)
            assert (await h.client.post(f"{base}/{later['file'].id}/url")).status_code == 403
            row["file"].content_hash = "b" * 64
            assert (await h.client.post(f"{base}/{row['file'].id}/url")).status_code == 403
            assert (await h.client.get(base)).json()["originals"] == []
            h.current = admin
            assert (
                await h.client.put(
                    endpoint, json={"status": "revoked", "reason": "Engagement ended"}
                )
            ).status_code == 200
            h.current = user
            assert (await h.client.get(base)).json()["access"]["state"] == "locked"
            assert (await h.client.post(f"{base}/{row['file'].id}/url")).status_code == 403
            events = (
                (
                    await h.db.execute(
                        select(cp.CpListingEvent).where(cp.CpListingEvent.listing_id == listing.id)
                    )
                )
                .scalars()
                .all()
            )
            assert {
                "source_documents.granted",
                "source_documents.revoked",
                "source_document.accessed",
            }.issubset({event.kind for event in events})


@pytest.mark.asyncio
async def test_admin_document_url_is_ready_scoped_and_staff_only():
    async with document_harness() as h:
        with ExitStack() as stack:
            for mocked in _patches():
                stack.enter_context(mocked)
            presign = stack.enter_context(
                patch.object(
                    storage, "presign_get", return_value="https://storage.example.test/document"
                )
            )
            scenario = Scenario(h)
            admin = await scenario.admin()
            listing = await scenario.listing(admin)
            await h.db.refresh(listing, attribute_names=["documents"])
            document = listing.documents[0]
            endpoint = f"/api/v1/capital/admin/listings/{listing.id}/documents/{document.id}/url"
            result = await h.client.post(endpoint + "?download=true")
            assert result.status_code == 200 and result.json()["expires_in_seconds"] == 60
            assert presign.call_args.kwargs["disposition"] == "attachment"
            assert (
                await h.client.post(
                    f"/api/v1/capital/admin/listings/{listing.id}/documents/{uuid4()}/url"
                )
            ).status_code == 404
            document.status = E.DocumentStatus.SUPERSEDED
            await h.db.flush()
            assert (await h.client.post(endpoint)).status_code == 404
            h.current = await h.user("loan-exec@example.test", Role.LOAN_EXEC)
            assert (await h.client.post(endpoint)).status_code == 403


@pytest.mark.asyncio
async def test_signed_subscription_does_not_release_sources_and_grants_cannot_cross_listing_or_role():
    async with document_harness() as h:
        with ExitStack() as stack:
            for mocked in _patches():
                stack.enter_context(mocked)
            scenario = Scenario(h)
            admin = await scenario.admin()
            first = await scenario.listing(admin)
            second = await scenario.listing(admin)
            first.profile_id, second.profile_id = uuid4(), uuid4()
            user, member = await scenario.member()
            h.db.add(
                cp.CpSubscription(
                    listing_id=first.id,
                    member_id=member.id,
                    entity_id=member.entities[0].id,
                    amount=10000,
                    status=E.SubscriptionStatus.SIGNED,
                    ack_ppm=True,
                    ack_accredited=True,
                )
            )
            await h.db.flush()
            row = source_row()
            stack.enter_context(
                patch.object(catalog, "source_inventory", AsyncMock(return_value=[row]))
            )
            h.current = user
            first_base = f"/api/v1/capital/listings/{first.ref}/file-documents"
            second_base = f"/api/v1/capital/listings/{second.ref}/file-documents"
            assert (await h.client.get(first_base)).json()["access"]["state"] == "locked"
            payload = {
                "status": "active",
                "reason": "Staff confirmed engagement",
                "engagement_confirmed": True,
            }
            endpoint = f"/api/v1/capital/admin/listings/{first.id}/document-grants/{member.id}"
            assert (await h.client.put(endpoint, json=payload)).status_code == 403
            h.current = admin
            assert (
                await h.client.put(endpoint, json={**payload, "file_ids": [str(uuid4())]})
            ).status_code == 422
            assert (await h.client.put(endpoint, json=payload)).status_code == 200
            h.current = user
            assert len((await h.client.get(first_base)).json()["originals"]) == 1
            assert (await h.client.get(second_base + f"?member_id={member.id}")).json()[
                "originals"
            ] == []
            assert (await h.client.post(f"{second_base}/{row['file'].id}/url")).status_code == 403
            member.status = E.MemberStatus.SUSPENDED
            await h.db.flush()
            assert (await h.client.get(first_base)).status_code == 403
            assert (await h.client.post(f"{first_base}/{row['file'].id}/url")).status_code == 403
            h.current = admin
            assert (await h.client.put(endpoint, json=payload)).status_code == 409


@pytest.mark.asyncio
async def test_source_inventory_excludes_nonbusiness_and_denies_unreviewed_identity_or_protected_originals():
    from app.models.application_profile import ApplicationProfile
    from app.services import application_programs, document_associations, locked_file_requests

    profile = SimpleNamespace(id=uuid4())
    listing = SimpleNamespace(profile_id=profile.id)
    db = SimpleNamespace(get=AsyncMock(return_value=profile))
    files, analyses, accepted, identities = [], {}, set(), {}
    cases = [
        ("bank_statement", True, True, False, True),
        ("business_tax_return", True, True, False, True),
        ("personal_tax_return", True, True, False, False),
        ("bank_statement", False, True, False, False),
        ("business_tax_return", True, False, False, False),
        ("bank_statement", True, True, True, False),
    ]
    expected = {}
    for category, reviewed, matched, protected, allowed in cases:
        file = source_row(category=category)["file"]
        file.statement_period = "2025-01"
        files.append(file)
        analyses[file.id] = SimpleNamespace(
            classification=category,
            status="completed",
            analysis={"key_facts": {"tax_year": 2025}},
            protected=protected,
        )
        if reviewed:
            accepted.add(file.id)
        identities[str(file.id)] = {"allows_financial_use": matched}
        if category in catalog.CATEGORIES:
            expected[file.id] = allowed
    with (
        patch.object(
            application_programs,
            "_evidence_inventory",
            AsyncMock(return_value=(files, analyses, set())),
        ) as inventory,
        patch.object(
            application_programs, "accepted_evidence_file_ids", AsyncMock(return_value=accepted)
        ),
        patch.object(document_associations, "load_context", AsyncMock(return_value=identities)),
        patch.object(
            locked_file_requests,
            "is_password_protected_file",
            side_effect=lambda _file, analysis: analysis.protected,
        ),
    ):
        rows = await catalog.source_inventory(db, listing)
    assert {row["file"].id: row["releasable"] for row in rows} == expected
    inventory.assert_awaited_once_with(db, profile)
    db.get.assert_awaited_once_with(ApplicationProfile, profile.id)


def test_original_presign_binds_the_source_s3_version():
    client = Mock()
    client.generate_presigned_url.return_value = "https://storage.example.test/versioned"
    with (
        patch.object(
            storage, "get_settings", return_value=SimpleNamespace(s3_bucket="synthetic-only")
        ),
        patch.object(storage, "_s3_client", return_value=client),
    ):
        assert storage.presign_get(
            "private/source.pdf", version_id="approved-version", download_filename="Original.pdf"
        )
    assert (
        client.generate_presigned_url.call_args.kwargs["Params"]["VersionId"] == "approved-version"
    )
    assert client.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 60
