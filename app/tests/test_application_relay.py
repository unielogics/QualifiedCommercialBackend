"""The signed application relayed: lender sharing answered from it, packages carry it, partner links."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.application_profile import (
    ApplicationCollectionTask,
    ApplicationRequirementResponse,
    ApplicationRequirementState,
)
from app.models.bucket import BucketShare, bucket_share_files
from app.models.file_event import FileEvent
from app.models.form_signing import FormSigningSession
from app.services import application_relay, signed_forms, submission_batches
from app.tests._sqlite_schema import sqlite_session, sqlite_template
from app.tests.test_signed_forms import _sign, _submitted

LENDER = SimpleNamespace(id=uuid.uuid4(), name="Acme Capital")
DESK = SimpleNamespace(id=None, name="Desk Underwriter", email="desk@example.com", role="loan_exec")


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "application_relay")


@pytest.fixture
async def db(template_database, tmp_path):
    async with sqlite_session(template_database, tmp_path) as session:
        yield session


@pytest.fixture
def storage():
    objects: dict[str, bytes] = {}

    def put(key, content_type, data, **kwargs):
        objects[key] = data
        return "v1"

    with (
        patch("app.routers.dealer_ai_intake._bucket_storage_config", lambda: ("bucket", "prefix", "kms")),
        patch("app.routers.dealer_ai_intake._put_bucket_object", put),
        patch("app.routers.dealer_ai_intake._read_bucket_object", lambda key, **kwargs: objects.get(key)),
        patch("app.routers.dealer_ai_intake._send_signed_document_copy_email", lambda **kwargs: None),
        patch("app.services.document_signature.render_signature_certificate_pdf", lambda **kwargs: b"%PDF-1.4 certificate"),
        patch("app.services.credit_pull_core.auto_pull_after_signature", AsyncMock(return_value={"status": "completed"})),
        patch("app.services.email.ses_client.send_email", lambda **kwargs: SimpleNamespace(ok=True)),
    ):
        yield objects


async def _executed(db):
    _, profile, view = await _submitted(db)
    signing = await signed_forms.view(db, profile, role="borrower")
    await _sign(db, profile, view, signing)
    return profile


async def _sharing_state(db, profile, lender=LENDER) -> ApplicationRequirementState:
    key = submission_batches.sharing_requirement(lender.id)["requirement_key"]
    return (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id, ApplicationRequirementState.requirement_key == key
            )
        )
    ).scalar_one()


async def _responses(db, state) -> list[ApplicationRequirementResponse]:
    return list((await db.execute(select(ApplicationRequirementResponse).where(ApplicationRequirementResponse.requirement_state_id == state.id))).scalars().all())


async def test_the_signed_application_answers_each_lenders_sharing_consent(db, storage) -> None:
    profile = await _executed(db)
    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    state = await _sharing_state(db, profile)
    assert state.status == "verified"
    [response] = await _responses(db, state)
    assert response.source == "signed_form" and response.review_state == "accepted"
    assert response.value["answer"]["granted"] is True
    # The record holds the words the client signed and names the disclosure they satisfy.
    assert response.provenance["disclosure_version"] == "re-app-sharing-v1"
    assert response.provenance["disclosure_text"].startswith("I authorize Qualified Commercial")
    assert response.provenance["satisfies_disclosure_version"] == "lender-sharing-v1"
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    assert response.provenance["basis"]["signing_session_id"] == str(session.id)
    assert response.provenance["basis"]["executed_file_id"] == str(session.executed_file_id)
    assert await submission_batches.sharing_is_authorized(db, {"states": {state.requirement_key: state}}, LENDER.id)
    tasks = (await db.execute(select(ApplicationCollectionTask).where(ApplicationCollectionTask.profile_id == profile.id))).scalars().all()
    sharing_tasks = [task for task in tasks if state.requirement_key in (task.requirement_keys or [])]
    assert sharing_tasks and {task.status for task in sharing_tasks} == {"completed"}

    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    assert len(await _responses(db, state)) == 1


async def test_a_changed_lender_disclosure_is_answered_again(db, storage) -> None:
    profile = await _executed(db)
    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    state = await _sharing_state(db, profile)
    [first] = await _responses(db, state)
    first.created_at = datetime.now(UTC) - timedelta(minutes=5)
    await db.flush()
    await submission_batches.materialize_lender_requirements(db, profile, None, SimpleNamespace(id=LENDER.id, name="Acme Capital Partners"))
    assert len(await _responses(db, state)) == 2
    assert state.status == "verified"


async def test_the_clients_own_refusal_is_never_overridden(db, storage) -> None:
    _, profile, view = await _submitted(db)
    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    state = await _sharing_state(db, profile)
    db.add(ApplicationRequirementResponse(
        profile_id=profile.id, requirement_state_id=state.id, value={"unavailable_reason": "Not with this lender"},
        source="borrower_room", source_version="1", review_state="unavailable",
        provenance={"definition_id": (state.provenance or {}).get("definition_id")},
    ))
    await db.flush()
    signing = await signed_forms.view(db, profile, role="borrower")
    await _sign(db, profile, view, signing)

    assert await application_relay.answer_lender_sharing(db, profile, state.requirement_key) is None
    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    assert [row.source for row in await _responses(db, state)] == ["borrower_room"]
    assert not await submission_batches.sharing_is_authorized(db, {"states": {state.requirement_key: state}}, LENDER.id)


async def test_without_the_sharing_consent_nothing_is_relayed(db, storage) -> None:
    profile = await _executed(db)
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    snapshot = [item for item in session.signature_config["consent_snapshot"] if item["key"] != "information_sharing"]
    session.signature_config = {**session.signature_config, "consent_snapshot": snapshot}
    await db.flush()
    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    state = await _sharing_state(db, profile)
    assert state.status != "verified" and not await _responses(db, state)
    with pytest.raises(HTTPException) as refused:
        await application_relay.share_with_partner(db, profile, actor=DESK, recipient_name="Pat Lender")
    assert refused.value.detail["code"] == "sharing_not_authorized"


async def test_every_lender_package_carries_the_signed_application(db, storage) -> None:
    profile = await _executed(db)
    await submission_batches.materialize_lender_requirements(db, profile, None, LENDER)
    state = await _sharing_state(db, profile)
    [response] = await _responses(db, state)
    source = {
        "readiness": SimpleNamespace(selections=[], programs=[], requirements=[]), "files": {}, "analyses": {}, "context": {},
        "states": {state.requirement_key: state}, "responses": {state.id: response},
    }
    lender = SimpleNamespace(id=LENDER.id, name=LENDER.name, is_active=True, submission_email="review@acme.example")
    material = await submission_batches.package_material(db, profile, lender, None, source, [])
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    assert str(session.executed_file_id) in [item["id"] for item in material["manifest"]["files"]]
    assert "sharing_consent_required" not in {item["code"] for item in material["blockers"]}
    [consent] = [item for item in material["manifest"]["responses"] if item["kind"] == "consent"]
    assert consent["provenance"]["disclosure_version"] == "re-app-sharing-v1"
    assert consent["provenance"]["basis"]["kind"] == "signed_application"
    assert "signing_session_id" not in consent["provenance"]["basis"]


async def test_a_partner_link_opens_only_the_signed_application_for_seven_days(db, storage) -> None:
    _, profile, view = await _submitted(db)
    with pytest.raises(HTTPException) as early:
        await application_relay.share_with_partner(db, profile, actor=DESK, recipient_name="Pat Lender")
    assert early.value.detail["code"] == "not_signed"
    signing = await signed_forms.view(db, profile, role="borrower")
    await _sign(db, profile, view, signing)

    result = await application_relay.share_with_partner(
        db, profile, actor=DESK, recipient_name="Pat  Lender", organization="Bridge Partners", recipient_email="Pat@Bridge.example"
    )
    share = await db.get(BucketShare, uuid.UUID(result["share_id"]))
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    shared = (await db.execute(select(bucket_share_files.c.file_id).where(bucket_share_files.c.share_id == share.id))).scalars().all()
    assert list(shared) == [session.executed_file_id]
    assert share.can_preview and share.can_download
    assert not (share.can_add_notes or share.can_upload or share.can_use_ai_chat or share.can_view_ai_summary or share.can_view_ai_tasks or share.can_see_internal_notes)
    assert share.recipient_name == "Pat Lender, Bridge Partners" and share.recipient_email == "pat@bridge.example"
    ends = datetime.fromisoformat(result["expires_at"])
    assert timedelta(days=6, hours=23) < ends - datetime.now(UTC) <= timedelta(days=7)
    assert result["passcode"] and result["passcode"] not in share.passcode_hash
    assert result["share_url"].endswith(f"/buckets/share/{share.token}")
    event = (await db.execute(select(FileEvent).where(FileEvent.kind == "application.shared"))).scalar_one()
    assert event.visibility == "client" and "Bridge Partners" in event.title

    relay = await application_relay.relay_read(db, profile.id)
    assert relay["signed"]["sharing_consent"] is True
    assert [(item["recipient"], item["active"]) for item in relay["shares"]] == [("Pat Lender, Bridge Partners", True)]
