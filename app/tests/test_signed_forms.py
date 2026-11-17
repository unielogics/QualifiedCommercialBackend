"""Signing the frozen real-estate application, and what the signature also counts as."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile, BucketRequestedDocument
from app.models.client_form import ClientFormVersion
from app.models.form_signing import FormSigningSession
from app.services import client_forms, signed_forms
from app.services import re_application_schema as schema
from app.tests._sqlite_schema import sqlite_session, sqlite_template
from app.tests.test_re_application import _complete, _heloc_file

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
REQUEST = SimpleNamespace(headers={"user-agent": "pytest"}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "signed_forms")


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
        patch("app.services.credit_pull_core.auto_pull_after_signature", AsyncMock(return_value={"status": "completed"})) as pull,
        patch("app.services.email.ses_client.send_email", lambda **kwargs: SimpleNamespace(ok=True)),
    ):
        yield SimpleNamespace(objects=objects, pull=pull)


async def _submitted(db, *, coborrower: bool = False):
    intake, profile = await _heloc_file(db)
    view = await client_forms.read(db, profile, audience="desk")
    if coborrower:
        view = await client_forms.save(db, profile, changes={}, confirm=[], has_coborrower=True, revision=view["revision"], actor="desk")
    view = await _complete(db, profile)
    view = await client_forms.submit(db, profile, revision=view["revision"], actor="client")
    return intake, profile, view


def _consents(signing):
    return [{"key": item["key"], "sha256": item["sha256"]} for item in signing["consents"]]


async def _sign(db, profile, view, signing, *, role="borrower", typed=None, consents=None, link=None):
    person = schema.get_at(view["body"], f"borrowers.{role}")
    return await signed_forms.sign(
        db, profile, role=role, link=link or SimpleNamespace(id=uuid.uuid4(), label=None),
        typed_name=typed or f"{person['first_name']} {person['last_name']}", signature_data_url=PNG,
        consents=consents if consents is not None else _consents(signing), esign_consent=True,
        version_id=view["version"]["id"], body_sha256=view["version"]["body_sha256"], request=REQUEST,
    )


async def test_one_signer_executes_the_application_and_counts_as_the_credit_authorization(db, storage) -> None:
    intake, profile, view = await _submitted(db)
    signing = await signed_forms.view(db, profile, role="borrower")
    assert signing["can_sign"] and signing["required_roles"] == ["borrower"]
    assert {item["key"] for item in signing["consents"]} >= {"business_purpose", "credit_authorization", "information_sharing", "esign"}

    result = await _sign(db, profile, view, signing)
    assert result["status"] == "executed" and result["executed_file_id"]
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    assert [link["role"] for link in session.chain] == ["borrower"]
    assert session.chain[0]["input_sha256"] == session.unsigned_sha256
    executed = await db.get(BucketFile, session.executed_file_id)
    assert executed.source_kind == "generated" and executed.source_detail == "signed_document"
    assert storage.objects[executed.s3_key].startswith(b"%PDF")
    slot = await db.get(BucketRequestedDocument, session.requested_document_id)
    assert slot.status == "uploaded" and slot.signature_kind == "signed_form"

    credit = (await db.execute(select(BucketRequestedDocument).where(BucketRequestedDocument.signature_kind == "credit_authorization"))).scalar_one()
    assert credit.status == "uploaded"
    storage.pull.assert_awaited_once()
    assert storage.pull.await_args.kwargs["applicant_data"]["legal_last_name"] == schema.get_at(view["body"], "borrowers.borrower.last_name")

    with pytest.raises(HTTPException) as again:
        await _sign(db, profile, view, signing)
    assert again.value.detail["code"] == "already_executed"


async def test_a_co_borrower_signs_from_their_own_link_before_it_executes(db, storage) -> None:
    _, profile, view = await _submitted(db, coborrower=True)
    signing = await signed_forms.view(db, profile, role="borrower")
    assert signing["required_roles"] == ["borrower", "coborrower"]
    first = await _sign(db, profile, view, signing)
    assert first["status"] == "in_progress"
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    slot = await db.get(BucketRequestedDocument, session.requested_document_id)
    assert slot.status != "uploaded"
    with pytest.raises(HTTPException) as edit:
        await client_forms.save(db, profile, changes={"notes.notes": "late change"}, confirm=[], has_coborrower=None, revision=view["revision"], actor="client")
    assert edit.value.detail["code"] == "signing_started"

    # A second click by the borrower records nothing new.
    again = await _sign(db, profile, view, signing)
    assert again["status"] == "in_progress" and len(session.chain) == 1
    # A link emailed for another signing pass cannot sign this one.
    with pytest.raises(HTTPException) as outdated:
        await _sign(db, profile, view, signing, role="coborrower", link=SimpleNamespace(id=uuid.uuid4(), label=f"coborrower:{uuid.uuid4()}"))
    assert outdated.value.detail["code"] == "link_outdated"
    # The invitation waits for the save, and the co-borrower's address is the same each time the desk copies it.
    assert db.info.get("client_forms_after_commit")
    found = await signed_forms.coborrower_link(db, profile, session, await db.get(ClientFormVersion, session.version_id))
    assert found is not None and found[0].label == f"coborrower:{session.id}"
    assert (await signed_forms.coborrower_link(db, profile, session, await db.get(ClientFormVersion, session.version_id)))[1] == found[1]

    with patch("app.services.signed_forms._pull_credit", AsyncMock()):
        done = await _sign(db, profile, view, signing, role="coborrower", link=found[0])
    assert done["status"] == "executed"
    assert [link["role"] for link in session.chain] == ["borrower", "coborrower"]
    assert session.chain[1]["input_sha256"] == session.chain[0]["output_sha256"]


async def test_changed_terms_a_changed_application_and_a_wrong_name_are_refused(db, storage) -> None:
    _, profile, view = await _submitted(db)
    signing = await signed_forms.view(db, profile, role="borrower")
    altered = [dict(item) for item in _consents(signing)]
    altered[0]["sha256"] = "0" * 64
    with pytest.raises(HTTPException) as consent:
        await _sign(db, profile, view, signing, consents=altered)
    assert consent.value.detail["code"] == "consent_changed"
    stale = {**view, "version": {**view["version"], "body_sha256": "f" * 64}}
    with pytest.raises(HTTPException) as changed:
        await _sign(db, profile, stale, signing)
    assert changed.value.detail["code"] == "form_changed"
    with pytest.raises(HTTPException) as name:
        await _sign(db, profile, view, signing, typed="Someone Else")
    assert name.value.status_code == 422


async def test_a_personal_use_hold_and_unapproved_wording_stop_signing(db, storage) -> None:
    intake, profile, view = await _submitted(db)
    signing = await signed_forms.view(db, profile, role="borrower")
    intake.intake_state = {**intake.intake_state, "personal_use_review": {"status": "held"}}
    with pytest.raises(HTTPException) as held:
        await _sign(db, profile, view, signing)
    assert held.value.detail["code"] == "personal_use_review"
    intake.intake_state = {key: value for key, value in intake.intake_state.items() if key != "personal_use_review"}
    with patch("app.services.signed_forms.signing_open", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as closed:
            await _sign(db, profile, view, signing)
    assert closed.value.detail["code"] == "signing_not_open"


async def test_the_desk_can_reopen_an_unfinished_signing_and_the_room_engine_refuses_signed_forms(db, storage) -> None:
    _, profile, view = await _submitted(db, coborrower=True)
    signing = await signed_forms.view(db, profile, role="borrower")
    await _sign(db, profile, view, signing)
    reopened = await signed_forms.reopen(db, profile, actor=SimpleNamespace(id=None, name="Desk"), reason="Client asked to fix the rent")
    assert reopened["status"] == "draft"
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    assert session.status == "void" and session.void_reason == "Client asked to fix the rent"

    import app.routers.dealer_ai_intake as core

    slot = await db.get(BucketRequestedDocument, session.requested_document_id)
    result = SimpleNamespace(scalar_one_or_none=lambda: slot)
    fake = SimpleNamespace(execute=AsyncMock(return_value=result))
    with pytest.raises(HTTPException) as refused:
        await core._sign_requested_document(
            fake, SimpleNamespace(bucket_id=slot.bucket_id), core.DealerDocumentSignRequest(requested_document_id=slot.id, typed_name="Jordan Sample", esign_consent=True, signature_data_url=PNG),
            REQUEST, actor_name="Jordan", actor_email="jordan@example.com",
        )
    assert refused.value.detail["code"] == "sign_on_form_page"


async def test_wording_approval_is_recorded_and_opens_signing_in_production(db) -> None:
    assert (await signed_forms.approval(db))["legal_approved"] is False
    with (
        patch("app.config.get_settings", lambda: SimpleNamespace(app_env="production")),
        patch("app.services.app_settings_lock.lock_app_settings", AsyncMock()),
    ):
        assert await signed_forms.signing_open(db) is False
        await signed_forms.set_approval(db, approved=True, actor=SimpleNamespace(name="Denny Matos"), note="Approved by the CEO")
        assert await signed_forms.signing_open(db) is True
    record = await signed_forms.approval(db)
    assert record["approved_by"] == "Denny Matos" and record["note"] == "Approved by the CEO"


async def test_a_signed_version_stays_on_file_and_a_reopen_starts_a_new_version(db, storage) -> None:
    _, profile, view = await _submitted(db)
    signing = await signed_forms.view(db, profile, role="borrower")
    await _sign(db, profile, view, signing)
    first = (await db.execute(select(FormSigningSession))).scalar_one()
    assert first.status == "executed"

    reopened = await signed_forms.reopen(db, profile, actor=SimpleNamespace(id=None, name="Desk"), reason="The loan amount changed")
    assert reopened["status"] == "draft"
    assert first.status == "executed" and first.void_reason is None
    view = await client_forms.save(db, profile, changes={"notes.notes": "Amount updated"}, confirm=[], has_coborrower=None, revision=reopened["revision"], actor="desk")
    view = await client_forms.submit(db, profile, revision=view["revision"], actor="client")
    assert view["version"]["number"] == 2
    signing = await signed_forms.view(db, profile, role="borrower")
    assert signing["status"] == "ready" and signing["can_sign"]
    assert (await _sign(db, profile, view, signing))["status"] == "executed"
    sessions = (await db.execute(select(FormSigningSession))).scalars().all()
    assert sorted(item.status for item in sessions) == ["executed", "executed"]
    assert len({item.executed_file_id for item in sessions}) == 2


async def test_what_was_submitted_is_what_is_read_and_signed(db, storage) -> None:
    intake, profile, view = await _submitted(db)
    frozen = view["body"]
    # The file moves on after the client submitted: the desk corrects the phone.
    intake.phone = "+17045550199"
    await db.flush()
    client_view = await client_forms.read(db, profile, audience="client")
    assert client_view["status"] == "submitted" and client_view["body"] == frozen and client_view["please_check"] == []
    desk = await client_forms.read(db, profile, audience="desk")
    assert [item["path"] for item in desk["changed_since_submission"]] == ["borrowers.borrower.mobile_phone"]
    # The document offered to read before signing is the stored copy that gets signed, not a new rendering.
    version = (await db.execute(select(ClientFormVersion))).scalar_one()
    assert await client_forms.preview_pdf(db, profile) == storage.objects[version.pdf_s3_key]
    # Submitting again without a change makes no new version.
    same = await client_forms.submit(db, profile, revision=client_view["revision"], actor="client")
    assert same["version"]["number"] == 1


async def test_a_bureau_failure_never_undoes_the_signature_or_the_authorization(db, storage) -> None:
    _, profile, view = await _submitted(db)
    signing = await signed_forms.view(db, profile, role="borrower")
    storage.pull.side_effect = RuntimeError("the bureau is down")
    result = await _sign(db, profile, view, signing)
    assert result["status"] == "executed"
    credit = (await db.execute(select(BucketRequestedDocument).where(BucketRequestedDocument.signature_kind == "credit_authorization"))).scalar_one()
    assert credit.status == "uploaded"
    # The signed copies are queued for after the save, not sent during it.
    assert db.info.get("client_forms_after_commit")
    await client_forms.run_deferred(db)
    assert not db.info.get("client_forms_after_commit")


async def test_a_signing_that_failed_to_save_can_be_tried_again(db, storage) -> None:
    _, profile, view = await _submitted(db)
    profile_id = profile.id
    await db.commit()
    signing = await signed_forms.view(db, profile, role="borrower")
    real, failures = client_forms.store, []

    def flaky(key, content_type, data):
        if "/signed-" in key and not failures:
            failures.append(key)
            raise OSError("storage hiccup")
        return real(key, content_type, data)

    with patch.object(client_forms, "store", flaky):
        with pytest.raises(HTTPException) as failed:
            await _sign(db, profile, view, signing)
        assert failed.value.status_code == 502
        await db.rollback()
        profile = await db.get(ApplicationProfile, profile_id)
        # Nothing from the failed attempt stands in the way: the same signer signs again.
        result = await _sign(db, profile, view, signing)
    assert result["status"] == "executed"
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    assert len(session.chain) == 1
