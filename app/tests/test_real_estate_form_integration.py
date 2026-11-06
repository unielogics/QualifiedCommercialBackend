from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import application_collection, application_profiles
from app.schemas.application_profile import FinancialStatementWrite
from app.services import (
    chat_form_links,
    financial_publication,
    financial_statements,
    real_estate_forms,
    worksheet_links,
)

KIND = "real_estate_schedule"


def link(**updates):
    return SimpleNamespace(kind=KIND, id=uuid4(), profile_id=uuid4(), statement_id=None,
        financial_family_id=uuid4(), permission="edit", completed_at=None, invitee_email="investor@example.com",
        expires_at=None, **updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,family", [("worksheet", None), ("unknown", None), (KIND, None)])
async def test_form_tokens_cannot_expand_worksheet_scope_or_open_an_unbound_portfolio(kind, family):
    held = SimpleNamespace(kind=kind, financial_family_id=family)
    with patch.object(financial_statements, "link_for_token", AsyncMock(return_value=held)):
        with pytest.raises(HTTPException) as error:
            await application_profiles._open_link_or_404(None, "credential")
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [application_profiles.save_public_financial_form_draft, application_profiles.submit_public_financial_form])
async def test_view_only_form_link_cannot_change_or_publish_portfolio(operation):
    held = link()
    held.permission = "view"
    db = SimpleNamespace(commit=AsyncMock())
    with (patch.object(application_profiles, "_open_link_or_404", AsyncMock(return_value=held)),
          patch.object(application_profiles, "stage_legacy_body", AsyncMock()) as stage):
        with pytest.raises(HTTPException) as error:
            await operation("credential", FinancialStatementWrite(body={}), db)
    assert error.value.status_code == 403
    stage.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_borrower_submission_uses_the_bound_draft_and_publication_pipeline():
    held, profile = link(), SimpleNamespace(id=uuid4())
    snapshot = {"kind": KIND, "family_id": str(held.financial_family_id), "body": {}}
    payload = FinancialStatementWrite(body={"owner_name": "Investor", "no_real_estate": True})
    db = SimpleNamespace(get=AsyncMock(return_value=profile), commit=AsyncMock())
    with (patch.object(application_profiles, "_open_link_or_404", AsyncMock(return_value=held)),
          patch.object(application_profiles, "managed_snapshot", AsyncMock(return_value=snapshot)) as read,
          patch.object(application_profiles, "stage_legacy_body", AsyncMock()) as publish):
        assert await application_profiles.submit_public_financial_form("credential", payload, db) == {"completed": True}
    assert read.await_args.kwargs["family_id"] == held.financial_family_id
    assert publish.await_args.args[2:4] == (snapshot, payload.body)
    assert publish.await_args.kwargs["submit"] and publish.await_args.kwargs["public"]
    assert publish.await_args.kwargs["actor"].id is None
    assert held.completed_at is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_form_share_is_bound_to_the_selected_real_estate_family():
    held = link()
    family_id = uuid4()
    profile = SimpleNamespace(id=held.profile_id)
    user = SimpleNamespace(id=uuid4(), role=Role.LOAN_EXEC)
    snapshot = {"family_id": str(family_id)}
    db = SimpleNamespace(commit=AsyncMock())
    with (patch.object(application_profiles.profiles, "load_profile", AsyncMock(return_value=profile)),
          patch.object(real_estate_forms, "ensure_workspace", AsyncMock(return_value=snapshot)) as prepare,
          patch.object(application_profiles, "managed_snapshot", AsyncMock(return_value=snapshot)),
          patch.object(financial_statements, "mint_link", AsyncMock(return_value=(held, "credential"))),
          patch.object(application_profiles.profiles, "log_profile_action", AsyncMock())):
        result = await application_profiles.mint_financial_form_link(profile.id, KIND, user, db)
    prepare.assert_awaited_once_with(db, profile, actor=user)
    assert held.financial_family_id == family_id
    assert f"/forms/{KIND}/credential" in result["url"]


@pytest.mark.asyncio
async def test_mortgage_collection_is_required_in_the_publication_transaction_not_advisory():
    family, version = SimpleNamespace(kind=KIND), SimpleNamespace(id=uuid4())
    with (patch("app.services.real_estate_mortgage_requests.sync_published_mortgage_requests", AsyncMock(side_effect=HTTPException(403, "Not permitted"))) as collect,
          patch.object(financial_publication, "invalidate_financial_review_caches", AsyncMock()) as invalidate):
        with pytest.raises(HTTPException) as error:
            await financial_publication.after_publication(None, SimpleNamespace(), family=family, version=version, file=SimpleNamespace())
    assert error.value.status_code == 403
    collect.assert_awaited_once()
    invalidate.assert_not_awaited()


def test_real_estate_is_explicitly_shareable_without_expanding_existing_four_form_packets():
    assert worksheet_links.pin_required_for("view", [KIND])
    assert not worksheet_links.pin_required_for("view", ["p_and_l"])
    assert KIND not in application_profiles.PACKET_KINDS
    assert KIND not in chat_form_links.target_for("open_worksheet", None)[1]
    assert chat_form_links.target_for("complete_now", KIND) == (KIND, ())


@pytest.mark.asyncio
@pytest.mark.parametrize("client_safe", [False, True])
async def test_current_portfolio_context_is_visible_to_staff_but_not_unscoped_business_room(client_safe):
    family = SimpleNamespace(id=uuid4(), kind=KIND, label="Owner portfolio")
    version = SimpleNamespace(id=uuid4(), version_number=2, created_at=datetime.now(UTC), pdf_file_id=uuid4(), source_file_id=None,
        key_facts={"estimated_value_total": 500000})
    result = SimpleNamespace(all=lambda: [(family, version)])
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=result))
    context = await financial_publication.current_version_context(db, profile_id=uuid4(), client_safe=client_safe)
    assert len(context) == (0 if client_safe else 1)
    if context:
        assert context[0]["version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_real_estate_form_request_is_persisted_in_requirements_and_repeated_requests_do_not_duplicate():
    from app.services import application_programs, requirement_library

    profile = SimpleNamespace(extra_requirements=[], id=uuid4(), primary_bucket_id=uuid4())
    actor = SimpleNamespace(id=uuid4(), name="Staff")
    slot = SimpleNamespace(id=uuid4(), bucket_id=profile.primary_bucket_id, status="requested", requirement_key=KIND)
    entry = {"key": KIND, "requirement_id": str(uuid4()), "required_level": "required"}
    plan = SimpleNamespace(entry=entry, already=False)
    states = []
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: states))
    db = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=slot))

    async def materialize(_db, _profile):
        states.append(SimpleNamespace(profile_id=profile.id, requested_document_id=slot.id, requirement_key=KIND,
            status="requested", provenance={"requirement_kind": "form", "collection_config": {"form_kind": KIND}}))

    with (patch.object(real_estate_forms.financial_workspace, "lock_financial_profile", AsyncMock()),
          patch.object(real_estate_forms, "requested_slot", AsyncMock(side_effect=[None, slot])),
          patch.object(requirement_library, "plan_add", AsyncMock(return_value=plan)) as pin,
          patch.object(application_programs, "get_program_readiness", AsyncMock(side_effect=materialize)) as refresh):
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (slot, False)
        assert await real_estate_forms.ensure_request(db, profile, actor=actor) == (slot, True)
    assert [item["key"] for item in profile.extra_requirements] == [KIND]
    pin.assert_awaited_once()
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_hosted_form_action_checks_document_type_not_the_internal_request_identifier():
    from app.services import arf_channel

    held = link()
    profile = SimpleNamespace(id=held.profile_id)
    task = SimpleNamespace(id=uuid4(), automation_approved=True, action_type="complete_form", status="open", assignee="borrower",
        requirement_keys=["staff_request_private_identifier_0"], config={"form_kind": KIND, "document_type": KIND})
    db = SimpleNamespace(commit=AsyncMock())
    with (patch.object(application_collection, "_public_application_room", AsyncMock(return_value=(None, profile))),
          patch.object(application_collection, "_task", AsyncMock(return_value=task)),
          patch.object(arf_channel, "profile_uses_arf", AsyncMock(return_value=True)),
          patch.object(arf_channel, "require_hosted_permission", AsyncMock()) as allow,
          patch.object(real_estate_forms, "ensure_workspace", AsyncMock(return_value={"family_id": str(held.financial_family_id)})),
          patch("app.services.financial_workspace_legacy.managed_snapshot", AsyncMock(return_value={"family_id": str(held.financial_family_id)})),
          patch.object(financial_statements, "mint_link", AsyncMock(return_value=(held, "credential"))),
          patch.object(application_collection.profiles, "log_profile_action", AsyncMock())):
        result = await application_collection.open_public_collection_form("room", task.id, SimpleNamespace(passcode="123"), None, db)
    allow.assert_awaited_once_with(db, requested_documents=[KIND])
    assert f"/forms/{KIND}/credential" in result["url"]
