"""The application's link, route by route: the code gate, sessions, and who may do what with which link."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.client_form import ClientFormVersion
from app.models.form_signing import FormSigningSession
from app.routers import client_forms as routes
from app.services import client_forms, form_link_codes, signed_forms
from app.tests._sqlite_schema import sqlite_session, sqlite_template
from app.tests.test_re_application import _heloc_file
from app.tests.test_signed_forms import _sign, _submitted

REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "client_form_routes")


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


async def test_the_link_asks_for_a_code_and_then_opens_with_a_session(db) -> None:
    _, profile = await _heloc_file(db)
    _, token = await client_forms.standing_link(db, profile)
    gate = await routes.open_client_form(token, REQUEST, None, db)
    # The standing link names no address of its own: the code goes to the file's current email.
    assert gate["needs_code"] is True and gate["email_hint"] == "j*****@example.com" and "body" not in gate

    sent: dict = {}
    with patch.object(form_link_codes, "send_code_email", lambda **kwargs: sent.update(kwargs) or True):
        assert (await routes.send_client_form_code(token, REQUEST, db))["sent"] is True
    assert sent["to_email"] == "jordan@example.com"
    with pytest.raises(HTTPException) as wrong:
        await routes.verify_client_form_code(token, routes.CodeVerify(code="000000" if sent["code"] != "000000" else "111111"), REQUEST, db)
    assert wrong.value.status_code == 403
    reply = await routes.verify_client_form_code(token, routes.CodeVerify(code=sent["code"]), REQUEST, db)

    view = await routes.open_client_form(token, REQUEST, reply["session"], db)
    assert view["needs_code"] is False and view["status"] == "draft" and view["schema"]["kind"] == "re_application"
    for bad in (None, "not-a-session"):
        with pytest.raises(HTTPException) as denied:
            await routes.save_client_form_draft(token, routes.FormSave(changes={}, confirm=[], revision=0), REQUEST, bad, db)
        assert denied.value.status_code == 401
    with pytest.raises(HTTPException) as missing:
        await routes.open_client_form("no-such-token", REQUEST, None, db)
    assert missing.value.status_code == 404


async def test_the_room_hands_over_the_files_own_link_with_a_session(db) -> None:
    _, profile = await _heloc_file(db)
    first = await routes.hand_off(db, profile, email=None)
    second = await routes.hand_off(db, profile, email=None)
    address, session = first.split("#s=")
    token = address.rsplit("/", 1)[-1]
    # One address per file, however often the client comes through the room.
    assert second.split("#s=")[0] == address
    assert (await client_forms.current_standing_link(db, profile))[1] == token
    assert (await routes.open_client_form(token, REQUEST, session, db))["needs_code"] is False


async def test_a_co_borrower_link_reads_and_signs_its_own_signing_pass_and_nothing_else(db, storage) -> None:
    _, profile, view = await _submitted(db, coborrower=True)
    signing = await signed_forms.view(db, profile, role="borrower")
    await _sign(db, profile, view, signing)
    session = (await db.execute(select(FormSigningSession))).scalar_one()
    version = await db.get(ClientFormVersion, session.version_id)
    link, token, email = await signed_forms.coborrower_link(db, profile, session, version)
    assert email == view["body"]["borrowers"]["coborrower"]["email"]
    theirs, _ = form_link_codes.issue_session(link)

    read = await routes.open_client_form(token, REQUEST, theirs, db)
    assert read["status"] == "submitted"
    with pytest.raises(HTTPException) as edit:
        await routes.save_client_form_draft(token, routes.FormSave(changes={"notes.notes": "mine"}, confirm=[], revision=read["revision"]), REQUEST, theirs, db)
    assert edit.value.status_code == 403
    waiting = await routes.client_form_signing(token, REQUEST, theirs, db)
    assert waiting["role"] == "coborrower" and waiting["can_sign"] is True
    # What they are offered to read is the copy the borrower already signed.
    assert await client_forms.preview_pdf(db, profile) == storage[session.current_pdf_key]

    # Reopened for changes: the pass is void and its co-borrower link stops working.
    await signed_forms.reopen(db, profile, actor=SimpleNamespace(id=None, name="Desk"), reason="A different co-borrower")
    await db.flush()
    with pytest.raises(HTTPException) as gone:
        await routes.open_client_form(token, REQUEST, theirs, db)
    assert gone.value.status_code == 404
