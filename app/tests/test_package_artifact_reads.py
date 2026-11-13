"""Real read-only API metadata and fresh, scoped immutable PDF links."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.capital_partners.models import CpListing
from app.db import get_db
from app.deps import get_current_user
from app.enums import Role
from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketFile
from app.models.public_underwriting_intake import (
    PublicUnderwritingIntake,
    PublicUnderwritingIntakeArtifact,
)
from app.routers import dealer_ai_intake as core
from app.services import package_artifacts as service
from app.tests import test_capital_partners as capital


def artifact(intake_id, kind, generation, version=1, status="current", **patch):
    return PublicUnderwritingIntakeArtifact(id=uuid4(), intake_id=intake_id, artifact_type=kind,
        title="Synthetic saved package", s3_key=f"synthetic/{uuid4()}.pdf", created_at=datetime(2026, 11, 13, 12, tzinfo=UTC),
        body_json={"_package": {"generation_id": str(generation), "status": status, "version": version,
            "bucket_file_id": str(uuid4())}, "_pdf": {"package_input_sha256": "a" * 64, "sha256": "b" * 64},
            "key_metrics": [{"label": "Synthetic coverage", "value": "1.25x"}], "risks": ["Execution assumption"],
            "mitigants": ["Known saved structure"], "next_best_action": "Review saved facts",
            "private_narrative": "Do not include full narrative on metadata polls"}, **patch)


@asynccontextmanager
async def harness(monkeypatch):
    monkeypatch.setattr(capital, "MODELS", [*capital.MODELS, Bucket, BucketFile, PublicUnderwritingIntake,
        PublicUnderwritingIntakeArtifact, ApplicationProfile])
    engine = await capital.make_engine()
    async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as db:
        app = FastAPI()
        app.include_router(core.admin_router, prefix="/api/v1")
        ctx = SimpleNamespace(db=db, current=SimpleNamespace(id=uuid4(), role=Role.SUPER_ADMIN))

        async def user():
            if ctx.current is None:
                raise HTTPException(401)
            return ctx.current

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = user
        bucket = Bucket(name="Synthetic package room")
        db.add(bucket)
        await db.flush()
        intake = PublicUnderwritingIntake(bucket_id=bucket.id, token_hash=str(uuid4()), full_name="Synthetic applicant", email="synthetic@example.test")
        db.add(intake)
        await db.flush()
        ctx.bucket, ctx.intake = bucket, intake
        ctx.path = f"/api/v1/admin/ai-underwriter-leads/{intake.id}/artifacts"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ctx.client = client
            yield ctx
    await engine.dispose()


async def save_generation(h, *, version=1, status="current", at=None):
    generation = uuid4()
    rows = [artifact(h.intake.id, kind, generation, version, status) for kind in service.KINDS]
    for row in rows:
        if at:
            row.created_at = at
        key = row.body_json["_package"]["bucket_file_id"]
        file = BucketFile(id=UUID(key), bucket_id=h.bucket.id, file_name="Synthetic package.pdf",
            s3_key=row.s3_key, status="uploaded", source_kind="generated")
        h.db.add_all([row, file])
    await h.db.flush()
    return generation, rows


def test_latest_complete_requires_current_coherent_generation_and_ignores_newer_history():
    ident, live, history = uuid4(), uuid4(), uuid4()
    current = [artifact(ident, kind, live, 7) for kind in service.KINDS]
    # A newer retained historical generation must never become the current bundle.
    historical = [artifact(ident, kind, history, 8, "superseded") for kind in service.KINDS]
    for row in historical:
        row.created_at += timedelta(days=1)
    selected = service.latest_complete([*historical, *current])
    assert selected["generation_id"] == str(live)
    assert set(selected["versions"].values()) == {7}
    partial = [artifact(ident, service.KINDS[0], uuid4(), 9)]
    assert service.latest_complete(partial) is None
    current[2].body_json["_pdf"]["package_input_sha256"] = "different"
    assert service.latest_complete([*current, *historical]) is None


def test_generation_requires_all_current_bucket_links_when_linkage_is_known():
    generation = uuid4()
    rows = [artifact(uuid4(), kind, generation) for kind in service.KINDS]
    ids = {row.body_json["_package"]["bucket_file_id"] for row in rows}
    assert service.latest_complete(rows, ids)["generation_id"] == str(generation)
    ids.remove(rows[1].body_json["_package"]["bucket_file_id"])
    assert service.latest_complete(rows, ids) is None


def test_malformed_legacy_metadata_never_crashes_latest_selection():
    row = artifact(uuid4(), "executive_summary", uuid4())
    for malformed in (None, [], {"_package": []}, {"_package": {"generation_id": "bad"}}):
        row.body_json = malformed
        assert service.latest_complete([row]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.SUPER_ADMIN, Role.LOAN_EXEC])
async def test_metadata_is_read_only_no_presigning_no_collectors_and_preserves_current_digest(monkeypatch, role):
    async with harness(monkeypatch) as h:
        h.current.role = role
        generation, rows = await save_generation(h, version=7)
        await h.db.commit()
        queries = []
        event.listen(h.db.bind.sync_engine, "before_cursor_execute", lambda _c, _cu, statement, *_: queries.append(statement))
        collector = AsyncMock(side_effect=AssertionError("No readiness collector in metadata GET"))
        monkeypatch.setattr(core, "_current_package_input_fingerprint", collector)
        storage = Mock(side_effect=AssertionError("No storage client on metadata polls"))
        monkeypatch.setattr(core, "_s3_client", storage)
        h.db.flush = AsyncMock(side_effect=AssertionError("No metadata flush"))
        before = deepcopy([row.body_json for row in rows])
        response = await h.client.get(h.path)
        assert response.status_code == 200, response.text
        value = response.json()
        assert "no-store" in response.headers["cache-control"]
        assert value["latest_package"]["generation_id"] == str(generation)
        assert value["latest_package"]["source_freshness"] == "unchecked"
        assert value["refresh"] is None
        assert len(value["artifacts"]) == 3
        assert all(row["is_fresh"] is None and row["preview_url"] is None and row["download_url"] is None for row in value["artifacts"])
        summary = next(row for row in value["artifacts"] if row["artifact_type"] == "executive_summary")
        assert summary["body_json"]["key_metrics"][0]["value"] == "1.25x"
        assert "private_narrative" not in str(value)
        assert before == [row.body_json for row in rows]
        assert not h.db.new and not h.db.dirty and not h.db.deleted
        assert all(statement.lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in statement.upper() for statement in queries)
        collector.assert_not_awaited()
        storage.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_reads_durable_status_without_private_job_fields_or_malformed_history_failure(monkeypatch):
    async with harness(monkeypatch) as h:
        generation, _ = await save_generation(h, version=7)
        malformed = artifact(h.intake.id, "executive_summary", uuid4(), 1, "superseded")
        malformed.body_json = []
        profile = ApplicationProfile(vertical="main_street", funding_category="general", industry="Synthetic", intake_id=h.intake.id)
        h.db.add_all([profile, malformed])
        await h.db.flush()
        listing = CpListing(ref="QC-SYNTH-PDF", name="Synthetic", vertical="business", profile_id=profile.id,
            source_snapshot={"refresh_job": {"job_id": str(uuid4()), "status": "queued", "actor_id": "private-actor", "requests": {"private-key": True}}})
        h.db.add(listing)
        await h.db.commit()
        result = await h.client.get(h.path)
        assert result.status_code == 200, result.text
        assert result.json()["latest_package"]["generation_id"] == str(generation)
        assert result.json()["refresh"]["status"] == "queued"
        assert "private-actor" not in result.text and "private-key" not in result.text


def storage(monkeypatch, *, prefix=b"%PDF-", missing=False):
    client = Mock()
    if missing:
        client.head_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
    else:
        client.head_object.return_value = {"ContentLength": 1234, "ContentType": "application/pdf"}
    client.get_object.side_effect = lambda **kw: {"Body": BytesIO(prefix)}
    client.generate_presigned_url.side_effect = lambda *a, **kw: f"https://synthetic.example/pdf?fresh={client.generate_presigned_url.call_count}"
    monkeypatch.setattr(core, "_s3_client", lambda: client)
    monkeypatch.setattr(core, "_bucket_storage_config", lambda: ("synthetic", "", None))
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.SUPER_ADMIN, Role.LOAN_EXEC])
async def test_fresh_links_pin_current_or_historical_id_inline_or_download(monkeypatch, role):
    async with harness(monkeypatch) as h:
        h.current.role = role
        _, rows = await save_generation(h, status="superseded")
        await h.db.commit()
        client = storage(monkeypatch)
        path = h.path + f"/{rows[0].id}/url"
        first = await h.client.get(path)
        second = await h.client.get(path + "?download=true")
        assert first.status_code == second.status_code == 200
        assert first.json()["artifact_id"] == str(rows[0].id)
        assert first.json()["url"] != second.json()["url"]
        assert first.json()["disposition"] == "inline" and second.json()["disposition"] == "attachment"
        assert "no-store" in first.headers["cache-control"]
        calls = client.generate_presigned_url.call_args_list
        assert calls[0].kwargs["Params"]["ResponseContentDisposition"].startswith("inline;")
        assert calls[1].kwargs["Params"]["ResponseContentDisposition"].startswith("attachment;")
        assert calls[0].kwargs["Params"]["ResponseCacheControl"] == "no-store"
        assert calls[0].kwargs["ExpiresIn"] == 900
        assert not h.db.dirty and not h.db.new and not h.db.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.CLIENT, Role.BROKER, Role.CAPITAL_PARTNER])
async def test_nonoperators_cannot_load_metadata_or_mint_links(monkeypatch, role):
    async with harness(monkeypatch) as h:
        h.current.role = role
        client = storage(monkeypatch)
        assert (await h.client.get(h.path)).status_code == 403
        assert (await h.client.get(h.path + f"/{uuid4()}/url")).status_code == 403
        client.head_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "invalid", "cross_file", "archived", "other_kind"])
async def test_missing_invalid_or_foreign_pdf_cannot_be_previewed(monkeypatch, failure):
    async with harness(monkeypatch) as h:
        _, rows = await save_generation(h)
        if failure == "cross_file":
            rows[0].intake_id = uuid4()
        if failure == "archived":
            h.bucket.archived_at = datetime.now(UTC)
        if failure == "other_kind":
            rows[0].artifact_type = "vendor_email"
        await h.db.commit()
        client = storage(monkeypatch, missing=failure == "missing", prefix=b"wrong" if failure == "invalid" else b"%PDF-")
        response = await h.client.get(h.path + f"/{rows[0].id}/url")
        assert response.status_code == (404 if failure in {"cross_file", "archived", "other_kind"} else 409)
        client.generate_presigned_url.assert_not_called()
