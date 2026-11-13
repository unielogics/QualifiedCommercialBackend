"""Independent token scope, read-only evaluation, and public evidence contracts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.models.application_profile import ApplicationProfile
from app.models.bucket import PROTECTED_RETENTION_CLASSES, BucketFile
from app.models.file_event import FileEvent
from app.models.financial_form_link import FinancialFormLink
from app.models.financial_form_version import (
    FinancialFormDraft,
    FinancialFormFamily,
    FinancialFormVersion,
)
from app.routers import application_profiles as routes
from app.schemas.earnings_improvement import empty_body
from app.services import financial_statements, operating_plan_service
from app.tests.test_financial_workspace import Result, context
from app.tests.test_operating_plan_service import approved, cockpit, proposal

TOKEN = "synthetic-operating-form-credential"


class ReadOnlyMemory:
    """Reads resolve real model keys; every persistence entry point is observed."""

    def __init__(self):
        self.profile, self.family, self.draft = context("operating_plan")
        self.draft.body = proposal()
        self.link = FinancialFormLink(id=uuid4(), profile_id=self.profile.id,
            financial_family_id=self.family.id, kind="operating_plan", permission="edit",
            token_hash=financial_statements.hash_token(TOKEN), revoked_at=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1), last_used_at=None)
        self.objects = {(ApplicationProfile, self.profile.id): self.profile,
            (FinancialFormFamily, self.family.id): self.family,
            (FinancialFormDraft, self.family.id): self.draft}
        self.commit, self.flush, self.add = AsyncMock(), AsyncMock(), Mock()
        self.info = {}
        self.goal = FileEvent(profile_id=self.profile.id, kind="operating_plan.goal_set",
            meta={"amount": "18000", "currency": "USD", "revision": 1})

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def execute(self, statement):
        if statement.column_descriptions[0]["entity"] is FileEvent:
            return Result([self.goal] if self.goal is not None else [])
        assert financial_statements.hash_token(TOKEN) in statement.compile().params.values()
        return Result(scalar=self.link)

    def assert_unchanged(self, original):
        assert self.draft.body == original
        assert self.draft.revision == 4
        assert self.family.current_version_id is None
        assert self.link.last_used_at is None
        self.commit.assert_not_awaited()
        self.flush.assert_not_awaited()
        self.add.assert_not_called()


def source_context(db):
    source, unrelated = str(uuid4()), str(uuid4())
    result = cockpit()
    result["noi"] = {"status": "reviewed", "headline": {"annualized_value": -10000}}
    result["operating_evidence"] = {
        "files": [
            {"file_id": source, "file_name": "Synthetic business return.pdf", "classification": "business_tax_return",
             "eligible_for_guidance": True, "eligible_for_review": True, "content_hash": "private-file-hash",
             "analysis_id": "private-analysis", "association_stamp": "private-association"},
            {"file_id": unrelated, "file_name": "Private owner credit.pdf", "classification": "credit_report"},
        ],
        "guidance": {"expense_groups": [{"key": "source-rent", "label": "Rent", "category": "rent",
            "baseline_annual_amount": 24000, "source_file_ids": [source]}]},
        "expense_candidates": [{"key": "rent", "label": "Rent", "category": "rent", "source_file_ids": [source]}],
        "merchant_sources": [], "merchant_offers": [{"internal_offer_id": "private-offer"}], "warnings": [],
    }
    db.objects[(BucketFile, UUID(source))] = SimpleNamespace(
        id=UUID(source), bucket_id=db.profile.primary_bucket_id,
        file_name="Synthetic business return.pdf", content_type="application/pdf", status="uploaded",
        deleted_at=None, retention_class="ordinary", s3_key="private/key.pdf", s3_version_id="private-version")
    return result, source, unrelated


async def evaluate(db, body=None):
    return await routes.public_operating_plan_evaluate(TOKEN,
        routes.PublicOperatingPlanEvaluate(body=body if body is not None else db.draft.body), db)


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["expired", "revoked", "unknown"])
@pytest.mark.parametrize("operation", ["evaluate", "evidence"])
async def test_closed_tokens_are_indistinguishable_and_never_read_financial_context(closed, operation):
    db = ReadOnlyMemory()
    original = deepcopy(db.draft.body)
    if closed == "expired":
        db.link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif closed == "revoked":
        db.link.revoked_at = datetime.now(UTC)
    else:
        db.link = None
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock()) as load:
        with pytest.raises(HTTPException) as error:
            if operation == "evaluate":
                await evaluate(db)
            else:
                await routes.public_operating_plan_evidence(TOKEN, uuid4(), db)
    assert error.value.status_code == 404
    assert error.value.detail == "This link is no longer available"
    load.assert_not_awaited()
    if db.link is not None:
        db.assert_unchanged(original)
    else:
        db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong_kind", "unbound", "missing_profile", "missing_family",
    "other_profile_family", "other_kind_family", "other_family_version"])
async def test_token_cannot_cross_its_bound_profile_kind_or_version_family(invalid):
    db = ReadOnlyMemory()
    if invalid == "wrong_kind":
        db.link.kind = "p_and_l"
    elif invalid == "unbound":
        db.link.financial_family_id = None
    elif invalid == "missing_profile":
        del db.objects[(ApplicationProfile, db.profile.id)]
    elif invalid == "missing_family":
        del db.objects[(FinancialFormFamily, db.family.id)]
    elif invalid == "other_profile_family":
        db.family.profile_id = uuid4()
    elif invalid == "other_kind_family":
        db.family.kind = "p_and_l"
    else:
        db.family.current_version_id = uuid4()
        db.objects[(FinancialFormVersion, db.family.current_version_id)] = SimpleNamespace(family_id=uuid4())
    with patch.object(operating_plan_service, "client_preview", AsyncMock()) as preview:
        with pytest.raises(HTTPException) as error:
            await evaluate(db)
    assert error.value.status_code == 404
    preview.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_token_cannot_evaluate_but_can_read_its_existing_source():
    db = ReadOnlyMemory()
    db.link.permission = "view"
    current, source, _ = source_context(db)
    with pytest.raises(HTTPException) as error:
        await evaluate(db)
    assert error.value.status_code == 403
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch("app.routers.buckets._download_url", return_value="https://storage.test/short-lived"):
        result = await routes.public_operating_plan_evidence(TOKEN, UUID(source), db)
    assert result["expires_in"] == 300
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_discards_forged_approval_strips_private_context_and_never_saves():
    db = ReadOnlyMemory()
    current, source, unrelated = source_context(db)
    original = deepcopy(db.draft.body)
    incoming = deepcopy(original)
    incoming["adjustments"][0].update(status="approved", reviewed_by_user_id=str(uuid4()),
        reviewed_at=datetime.now(UTC).isoformat(), review_reason="Forged private approval",
        review_source_stamp="f" * 64, baseline_treatment="included_in_baseline",
        baseline_reconciliation_note="Forged private reconciliation", source_file_ids=[source])
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        result = await evaluate(db, incoming)
    action = result["evaluation"]["actions"][0]
    assert action["review_state"] == "proposed"
    assert not action["included_in_approved_projection"]
    assert result["evaluation"]["totals"]["approved"]["count"] == 0
    assert result["evaluation"]["projection"]["approved"]["status"] == "awaiting_review"
    assert set(result) == {"evaluation", "evidence", "guidance", "goal"}
    assert [row["file_id"] for row in result["evidence"]["files"]] == [source]
    assert result["evidence"]["merchant_offers"] == []
    exposed = repr(result)
    for private in (unrelated, "private-file-hash", "private-analysis", "private-association", "private-offer",
                    "reviewed_by_user_id", "review_source_stamp", "authority_stamp", "context_stamp", "Forged private"):
        assert private not in exposed
    db.assert_unchanged(original)


@pytest.mark.asyncio
async def test_current_server_review_is_retained_without_disclosing_staff_receipt():
    db = ReadOnlyMemory()
    db.draft.body = approved()
    original = deepcopy(db.draft.body)
    current, _, _ = source_context(db)
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        result = await evaluate(db)
    assert "review_reason" not in repr(result)
    assert "Private review reason" not in repr(result)
    assert "reviewed_by_user_id" not in repr(result)
    db.assert_unchanged(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [False, True])
async def test_new_unavailable_sources_are_rejected_but_saved_historical_references_remain_readable(retained):
    db = ReadOnlyMemory()
    current, _, private_source = source_context(db)
    body = deepcopy(db.draft.body)
    body["expenses"][0]["source_file_ids"] = [private_source]
    if retained:
        db.draft.body = deepcopy(body)
    original = deepcopy(db.draft.body)
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        if retained:
            result = await evaluate(db, body)
            assert private_source not in {row["file_id"] for row in result["evidence"]["files"]}
        else:
            with pytest.raises(HTTPException) as error:
                await evaluate(db, body)
            assert error.value.status_code == 422
    db.assert_unchanged(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["not_scoped", "missing", "cross_bucket", "deleted", "rejected", "protected"])
async def test_document_preview_refuses_private_missing_or_unusable_sources(denied):
    db = ReadOnlyMemory()
    current, source, private_source = source_context(db)
    file_id = UUID(source)
    file = db.objects[(BucketFile, file_id)]
    if denied == "not_scoped":
        file_id = UUID(private_source)
    elif denied == "missing":
        del db.objects[(BucketFile, file_id)]
    elif denied == "cross_bucket":
        file.bucket_id = uuid4()
    elif denied == "deleted":
        file.deleted_at = datetime.now(UTC)
    elif denied == "rejected":
        file.status = "rejected"
    else:
        file.retention_class = next(iter(PROTECTED_RETENTION_CLASSES))
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch("app.routers.buckets._download_url") as sign:
        with pytest.raises(HTTPException) as error:
            await routes.public_operating_plan_evidence(TOKEN, file_id, db)
    assert error.value.status_code == 404
    assert error.value.detail == "Source document not found"
    sign.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_response_exposes_only_the_scoped_file_and_300_second_preview_url():
    db = ReadOnlyMemory()
    current, source, _ = source_context(db)
    original = deepcopy(db.draft.body)
    with patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)), \
         patch("app.routers.buckets._download_url", return_value="https://storage.test/short-lived") as sign:
        result = await routes.public_operating_plan_evidence(TOKEN, UUID(source), db)
    assert result == {"file": {"id": source, "file_name": "Synthetic business return.pdf", "content_type": "application/pdf"},
        "preview_url": "https://storage.test/short-lived", "expires_in": 300}
    sign.assert_called_once_with("private/key.pdf", file_name="Synthetic business return.pdf",
        content_type="application/pdf", version_id="private-version", ttl=300)
    db.assert_unchanged(original)


@pytest.mark.asyncio
async def test_public_evaluate_http_contract_requires_only_body_and_no_staff_identity():
    db = ReadOnlyMemory()
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.get_db] = lambda: db
    path = f"/api/v1/application-profiles/public/financial-forms/{TOKEN}/evaluate"
    with patch.object(operating_plan_service, "client_preview", AsyncMock(return_value={"evaluation": {}})) as preview:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(path, json={"body": empty_body()})
            assert response.status_code == 200
            for extra in ({"approved": True}, {"context_stamp": "forged"}, {"actor_id": str(uuid4())}):
                response = await client.post(path, json={"body": empty_body(), **extra})
                assert response.status_code == 422
    preview.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_get_includes_sanitized_operating_context_for_the_exact_form():
    db = ReadOnlyMemory()
    current, source, private_source = source_context(db)
    snapshot = {"family_id": str(db.family.id), "body": db.draft.body,
        "draft": {"revision": 4, "base_version_id": None}, "versions": []}
    with patch.object(routes, "_client_link_snapshot", AsyncMock(return_value=snapshot)), \
         patch.object(financial_statements, "form_prefill", AsyncMock(return_value={"business_name": "Synthetic Studio"})), \
         patch("app.services.financial_cockpit.load_cockpit", AsyncMock(return_value=current)):
        result = await routes.public_financial_form(TOKEN, db)
    assert result["kind"] == "operating_plan"
    assert result["workspace"]["family_id"] == str(db.family.id)
    assert [row["file_id"] for row in result["operating_plan"]["evidence"]["files"]] == [source]
    assert private_source not in repr(result["operating_plan"])
    # Existing link-open tracking remains separate from the read-only preview endpoints.
    db.commit.assert_awaited_once()
