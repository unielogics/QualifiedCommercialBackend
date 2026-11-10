from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.capital_partners import enums as E
from app.capital_partners import router_admin as routes
from app.capital_partners.models import CpListingDocument
from app.capital_partners.services import sync
from app.models.application_profile import ApplicationProfile
from app.models.financial_form_version import FinancialFormDraft
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import dealer_ai_intake as intake_routes


def artifact(kind, *, presentation=True, generation="one", sha="a" * 64, choices=None):
    return SimpleNamespace(id=uuid4(), artifact_type=kind, s3_key=f"source/{kind}.pdf", body_json={
        "_package": {"generation_id": generation, "status": "current", "bucket_file_id": str(uuid4())},
        "_pdf": {"package_document_types": ["executive_summary", "lender_packet", *(["investor_presentation"] if presentation else [])],
                 "package_input_sha256": sha, **({"package_options": choices} if choices is not None else {})}})


class PackageDB:
    def __init__(self, profile, intake):
        self.objects = {(ApplicationProfile, profile.id): profile, (PublicUnderwritingIntake, intake.id): intake}
        self.added = []
        self.commits = 0

    async def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, *_args, **_kwargs):
        pass


def fixture(*, presentation=True):
    intake = SimpleNamespace(id=uuid4())
    profile = SimpleNamespace(id=uuid4(), intake_id=intake.id)
    documents = [SimpleNamespace(kind=kind, version=version, status=E.DocumentStatus.READY, superseded_at=None)
                 for kind, version in ((E.DocumentKind.EXECUTIVE_SUMMARY, 3), (E.DocumentKind.UNDERWRITING_PACKAGE, 4),
                                      (E.DocumentKind.INVESTOR_PRESENTATION, 2), (E.DocumentKind.OTHER, 7))]
    listing = SimpleNamespace(id=uuid4(), profile_id=profile.id, status=E.ListingStatus.DRAFT,
        documents=documents, source_snapshot={})
    artifacts = {kind: artifact(kind, presentation=presentation) for kind in ("executive_summary", "lender_packet", "investor_presentation")}
    return listing, profile, intake, artifacts, PackageDB(profile, intake)


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [True, False])
async def test_marketplace_push_versions_the_complete_generation_and_preserves_other_documents(presentation):
    listing, profile, intake, artifacts, db = fixture(presentation=presentation)
    copy = Mock(return_value=True)
    generation = {"generation_id": "one", "presentation_artifact_id": str(artifacts["investor_presentation"].id) if presentation else None}
    fingerprints = {"package": sync._fingerprint(generation)}
    async def latest(_db, _intake_id, kind):
        assert _intake_id == intake.id
        return artifacts[kind]
    with patch.object(routes, "_load_admin_listing", AsyncMock(return_value=listing)), \
         patch.object(routes, "_listing_admin_out", AsyncMock(return_value={"id": listing.id})), \
         patch.object(intake_routes, "_fresh_package_artifact_pair", AsyncMock(return_value=(artifacts["executive_summary"], artifacts["lender_packet"]))) as fresh, \
         patch.object(intake_routes, "_latest_artifact", latest), \
         patch.object(routes.storage, "configured", return_value=True), \
         patch.object(routes.storage, "build_key", side_effect=lambda _listing, name: "listing/" + name), \
         patch.object(routes.storage, "copy_object", copy), \
         patch.object(routes.storage, "head_object", return_value={"ContentLength": 1200}), \
         patch.object(routes.P, "readiness_snapshot", AsyncMock(return_value={})), \
         patch.object(sync, "current_fingerprints", AsyncMock(return_value=(fingerprints, {}, {}))), \
         patch.object(sync, "current_package", AsyncMock(return_value=generation)), \
         patch.object(routes.L, "log_event", AsyncMock()):
        await routes.publish_listing_package(listing.id, SimpleNamespace(id=uuid4()), db)
    fresh.assert_awaited_once_with(db, intake)
    docs = [row for row in db.added if isinstance(row, CpListingDocument)]
    assert copy.call_count == len(docs) == (3 if presentation else 2)
    assert {row.kind: row.version for row in docs} == {
        E.DocumentKind.EXECUTIVE_SUMMARY: 4, E.DocumentKind.UNDERWRITING_PACKAGE: 5,
        **({E.DocumentKind.INVESTOR_PRESENTATION: 3} if presentation else {})}
    assert all(document.status == E.DocumentStatus.SUPERSEDED for document in listing.documents[:3])
    assert listing.documents[3].status == E.DocumentStatus.READY
    published = listing.source_snapshot["published"]["package"]
    assert len(published["documents"]) == len(docs)
    if presentation:
        pushed = next(item for item in published["documents"] if item["kind"] == "investor_presentation")
        assert pushed["artifact_id"] == str(artifacts["investor_presentation"].id)
        assert pushed["version"] == 3
    assert db.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "generation", "fingerprint", "superseded"])
async def test_mismatched_presentation_is_rejected_before_any_listing_copy(fault):
    listing, _profile, _intake, artifacts, db = fixture()
    if fault == "missing":
        artifacts["investor_presentation"] = None
    elif fault == "generation":
        artifacts["investor_presentation"].body_json["_package"]["generation_id"] = "two"
    elif fault == "fingerprint":
        artifacts["investor_presentation"].body_json["_pdf"]["package_input_sha256"] = "b" * 64
    else:
        artifacts["investor_presentation"].body_json["_package"]["status"] = "superseded"
    async def latest(_db, _id, kind):
        return artifacts[kind]
    with patch.object(routes, "_load_admin_listing", AsyncMock(return_value=listing)), \
         patch.object(intake_routes, "_fresh_package_artifact_pair", AsyncMock(return_value=(artifacts["executive_summary"], artifacts["lender_packet"]))), \
         patch.object(intake_routes, "_latest_artifact", latest), patch.object(routes.storage, "copy_object") as copy:
        with pytest.raises(HTTPException) as error:
            await routes.publish_listing_package(listing.id, SimpleNamespace(id=uuid4()), db)
    assert error.value.status_code == 409
    copy.assert_not_called()
    assert not db.added and db.commits == 0


@pytest.mark.asyncio
async def test_source_freshness_rejection_is_not_bypassed_by_presentation_push():
    listing, _profile, _intake, _artifacts, db = fixture()
    with patch.object(routes, "_load_admin_listing", AsyncMock(return_value=listing)), \
         patch.object(intake_routes, "_fresh_package_artifact_pair", AsyncMock(side_effect=HTTPException(409, "The working plan changed"))), \
         patch.object(routes.storage, "copy_object") as copy:
        with pytest.raises(HTTPException) as error:
            await routes.publish_listing_package(listing.id, SimpleNamespace(id=uuid4()), db)
    assert error.value.status_code == 409
    copy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_plan,working", [(True, True), (True, False), (False, True)])
async def test_package_sync_tracks_only_the_operating_snapshot_selected_for_generation(include_plan, working):
    listing, profile, _intake, artifacts, db = fixture()
    choices = {"include_operating_plan": include_plan, "include_working_operating_plan": working}
    for item in artifacts.values():
        item.body_json["_pdf"]["package_options"] = deepcopy(choices)
    family = SimpleNamespace(id=uuid4(), current_version_id=uuid4())
    draft = SimpleNamespace(revision=5)
    db.objects[(FinancialFormDraft, family.id)] = draft
    async def latest(_db, _id, kind):
        return artifacts[kind]
    with patch.object(intake_routes, "_latest_artifact", latest), \
         patch("app.services.financial_workspace.existing_family", AsyncMock(return_value=family)) as find:
        first = await sync.current_package(db, profile)
        sync.mark_published(listing, {"package": sync._fingerprint(first)}, by=None)
        draft.revision += 1
        second = await sync.current_package(db, profile)
        state = sync.sync_state(listing, {"package": sync._fingerprint(second)})
    assert state["up_to_date"] is not (include_plan and working)
    if not include_plan:
        find.assert_not_awaited()
    assert second["presentation_artifact_id"] == str(artifacts["investor_presentation"].id)


@pytest.mark.asyncio
async def test_legacy_pair_sync_does_not_require_a_presentation():
    _listing, profile, _intake, artifacts, db = fixture(presentation=False)
    async def latest(_db, _id, kind):
        assert kind != "investor_presentation"
        return artifacts[kind]
    with patch.object(intake_routes, "_latest_artifact", latest):
        result = await sync.current_package(db, profile)
    assert result == {"generation_id": "one", "summary_artifact_id": str(artifacts["executive_summary"].id),
                      "packet_artifact_id": str(artifacts["lender_packet"].id)}
