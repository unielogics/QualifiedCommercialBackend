from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.routers import application_profiles as routes
from app.schemas.application_profile import FinancialStatementWrite
from app.services import client_financial_forms as clients
from app.services import financial_workspace as workspace
from app.tests.test_financial_workspace import Memory, context


def snapshot(kind="debt_schedule", *, revision=4, body=None, base=None):
    return {"kind": kind, "family_id": str(uuid4()), "body": deepcopy(body if body is not None else workspace.empty_body(kind)),
        "draft": {"revision": revision, "base_version_id": str(base) if base else None, "dirty": True},
        "current_version_id": str(base) if base else None, "versions": [], "completion": {"complete": False}}


def row(**changes):
    return {"id": str(uuid4()), "lender": "Imported lender", "balance": "1000", "monthly_payment": "100",
        "origin": "ai_draft", "status": "active", "document_id": str(uuid4()), "vendor_key": "IMPORTED LENDER",
        "count_in_dscr": True, "evidence_links": [{"file_id": str(uuid4()), "role": "agreement"}], **changes}


def result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


@pytest.mark.parametrize("origin", ["admin", "ai_draft", "field_desk", "client_form"])
def test_client_can_correct_any_active_scoped_source_without_taking_source_privileges(origin):
    original = row(origin=origin)
    current = {"debts": [original], "field_sources": {f"{original['id']}.balance": {"page": 2}}}
    before = deepcopy(current)
    corrected = clients.merge_client_body("debt_schedule", current, {"debts": [{"id": original["id"],
        "balance": "800", "payment_amount": "50", "payment_frequency": "weekly", "origin": "admin",
        "vendor_key": "FORGED", "document_id": str(uuid4()), "count_in_dscr": False, "evidence_links": []}]})
    assert current == before
    accepted = corrected["debts"][0]
    assert accepted["balance"] == "800" and accepted["payment_amount"] == "50"
    assert accepted["origin"] == origin and accepted["client_reviewed"] is True
    assert accepted["document_id"] == original["document_id"] and accepted["vendor_key"] == original["vendor_key"]
    assert accepted["evidence_links"] == original["evidence_links"] and accepted["count_in_dscr"] is True
    assert corrected["field_sources"] == current["field_sources"]


def test_client_removal_is_recoverable_and_cannot_restore_excluded_or_duplicate_rows():
    active, dismissed, duplicate = row(), row(status="dismissed"), row(status="duplicate", duplicate_of_id=str(uuid4()))
    current = {"debts": [active, dismissed, duplicate]}
    merged = clients.merge_client_body("debt_schedule", current, {"no_debt": True, "debts": [
        {"id": dismissed["id"], "status": "active", "balance": "0"},
        {"id": duplicate["id"], "status": "active", "duplicate_of_id": None},
    ]})
    assert merged["debts"][0]["status"] == "dismissed"
    assert merged["debts"][0]["origin"] == "ai_draft"
    assert merged["debts"][1:] == current["debts"][1:]
    assert clients.client_body("debt_schedule", merged)["debts"] == []


def test_separate_new_loans_with_the_same_lender_keep_their_stable_ids():
    first = row()
    second = {"id": str(uuid4()), "lender": first["lender"], "balance": "500", "monthly_payment": "50"}
    merged = clients.merge_client_body("debt_schedule", {"debts": [first]}, {"debts": [first, second]})
    assert [item["id"] for item in merged["debts"]] == [first["id"], second["id"]]
    assert [item["balance"] for item in merged["debts"]] == ["1000", "500"]


@pytest.mark.parametrize("debts", [{}, ["bad"], [{"id": "not-an-id"}], [dict(id="x"), dict(id="x")]])
def test_invalid_client_row_shapes_are_rejected(debts):
    with pytest.raises(HTTPException) as error:
        clients.merge_client_body("debt_schedule", {"debts": []}, {"debts": debts})
    assert error.value.status_code == 422


def test_pfs_corrections_are_schema_scoped_and_retain_each_rows_evidence():
    identifier = str(uuid4())
    body = workspace.empty_body("pfs")
    body["schedules"]["notes_payable"] = [{"id": identifier, "current_balance": "100", "source_file_id": "original"}]
    body["owner_ids"] = ["original-owner"]
    body["field_sources"] = {"assets.cash_on_hand": {"page": 3}}
    merged = clients.merge_client_body("pfs", body, {"assets": {"cash_on_hand": "500", "unrecognized_total": "99999"},
        "owner_ids": ["another-owner"], "field_sources": {}, "schedules": {"notes_payable": [
            {"id": identifier, "current_balance": "80", "source_file_id": "forged"}]}})
    assert merged["assets"]["cash_on_hand"] == "500" and "unrecognized_total" not in merged["assets"]
    assert merged["schedules"]["notes_payable"][0] == {"id": identifier, "current_balance": "80", "source_file_id": "original"}
    assert merged["owner_ids"] == body["owner_ids"] and merged["field_sources"] == body["field_sources"]


def test_real_estate_corrections_do_not_replace_evidence_controls():
    prop, mortgage = str(uuid4()), str(uuid4())
    current = {**workspace.empty_body("real_estate_schedule"), "properties": [{"id": prop, "address": "Sample", "source_file_id": "original"}],
        "mortgages": [{"id": mortgage, "property_row_id": prop, "balance": "1000", "evidence": {"statement": "original"}}]}
    merged = clients.merge_client_body("real_estate_schedule", current, {"properties": [{"id": prop, "estimated_value": "5000", "source_file_id": "forged"}],
        "mortgages": [{"id": mortgage, "balance": "800", "evidence": {"statement": "forged"}}]})
    assert merged["properties"][0]["source_file_id"] == "original"
    assert merged["mortgages"][0]["balance"] == "800" and merged["mortgages"][0]["evidence"] == current["mortgages"][0]["evidence"]


@pytest.mark.parametrize("kind", ["p_and_l", "balance_sheet"])
def test_business_form_corrects_schema_cells_without_accepting_forged_totals_or_provenance(kind):
    body = workspace.empty_body(kind)
    section = next(iter(body["sections"]))
    field = next(iter(body["sections"][section]))
    merged = clients.merge_client_body(kind, body, {"header": {"business_name": "Corrected company", "staff_approved": True},
        "sections": {section: {field: "1000", "forged_total": "99999"}}, "field_sources": {"forged": True}})
    assert merged["header"]["business_name"] == "Corrected company" and "staff_approved" not in merged["header"]
    assert merged["sections"][section][field] == "1000" and "forged_total" not in merged["sections"][section]
    assert "field_sources" not in merged


@pytest.mark.asyncio
async def test_pfs_adoption_does_not_follow_another_owners_newer_family():
    profile, held_statement = SimpleNamespace(id=uuid4()), uuid4()
    family = SimpleNamespace(id=uuid4(), identity={"statement_id": str(uuid4())})
    with patch.object(workspace, "lock_financial_profile", AsyncMock()), patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, None))), patch.object(workspace, "load_workspace", AsyncMock()) as read:
        with pytest.raises(HTTPException) as error:
            await clients.ensure_snapshot(None, profile, "pfs", family_id=family.id, statement_id=held_statement)
    assert error.value.status_code == 404
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unbound_pfs_link_cannot_choose_between_separate_owners():
    with patch.object(workspace, "lock_financial_profile", AsyncMock()), patch.object(workspace, "_families", AsyncMock(return_value=[object(), object()])), patch.object(workspace, "resolve_family", AsyncMock()) as resolve:
        with pytest.raises(HTTPException) as error:
            await clients.ensure_snapshot(None, SimpleNamespace(id=uuid4()), "pfs")
    assert error.value.status_code == 409
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_pfs_adoption_rejects_a_statement_from_another_profile_before_loading_it():
    from app.models.financial_statement import FinancialStatement

    profile = SimpleNamespace(id=uuid4())
    statement_id = uuid4()
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(profile_id=uuid4())), execute=AsyncMock())
    with patch.object(workspace, "lock_financial_profile", AsyncMock()), patch.object(workspace, "_legacy_body", AsyncMock()) as legacy:
        with pytest.raises(HTTPException) as error:
            await workspace.resolve_family(db, profile, kind="pfs", statement_id=statement_id)
    assert error.value.status_code == 404
    db.get.assert_awaited_once_with(FinancialStatement, statement_id)
    db.execute.assert_not_awaited()
    legacy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [None, 3, 5])
async def test_stale_or_unversioned_whole_body_saves_cannot_overwrite_the_working_draft(revision):
    with patch.object(workspace, "save_draft", AsyncMock()) as save:
        with pytest.raises(HTTPException) as error:
            await clients.stage_client_draft(None, None, snapshot(), body={"debts": []}, expected_revision=revision)
    assert error.value.status_code == 409
    save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", workspace.KINDS)
async def test_all_fillable_forms_publish_through_the_same_version_pipeline(kind):
    original = snapshot(kind)
    staged = {**original, "draft": {**original["draft"], "revision": 5}}
    db = SimpleNamespace(execute=AsyncMock(return_value=result(None)), get=AsyncMock(return_value=None))
    with patch.object(clients, "stage_client_draft", AsyncMock(return_value=staged)), patch.object(workspace, "publish_version", AsyncMock(return_value={"saved_version": {"version_number": 2}})) as publish:
        accepted = await clients.save_submission(db, None, original, body=original["body"], expected_revision=4,
            base_version_id=None, idempotency_key="client:scoped:retry-1")
    assert accepted["saved_version"]["version_number"] == 2
    assert publish.await_args.kwargs == {"kind": kind, "family_id": UUID(original["family_id"]),
        "expected_revision": 5, "base_version_id": None, "idempotency_key": "client:scoped:retry-1", "actor": None, "submit": True}


@pytest.mark.asyncio
async def test_lost_submit_response_retries_the_saved_version_without_staging_or_rendering_again():
    initial = {"debts": [row(document_id=None, evidence_links=[])]}
    body = workspace.validate_body("debt_schedule", clients.merge_client_body("debt_schedule", initial, initial))
    previous = SimpleNamespace(id=uuid4(), body=body, body_sha256=workspace.body_hash(body), version_number=2,
        created_at=workspace.now(), actor_name="Borrower", source="form", source_file_id=None, pdf_file_id=uuid4(), status="filed")
    current = snapshot(body=body, revision=8)
    db = SimpleNamespace(execute=AsyncMock(return_value=result(previous)))
    with patch.object(clients, "stage_client_draft", AsyncMock()) as stage, patch.object(workspace, "publish_version", AsyncMock()) as publish:
        retried = await clients.save_submission(db, None, current, body=body, expected_revision=4,
            base_version_id=None, idempotency_key="same-request")
    assert retried["saved_version"]["id"] == str(previous.id)
    stage.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_reusing_a_save_identifier_with_changed_answers_is_rejected():
    initial = {"debts": [row(document_id=None, evidence_links=[])]}
    body = workspace.validate_body("debt_schedule", clients.merge_client_body("debt_schedule", initial, initial))
    previous = SimpleNamespace(body=body, body_sha256=workspace.body_hash(body))
    db = SimpleNamespace(execute=AsyncMock(return_value=result(previous)))
    with pytest.raises(HTTPException) as error:
        await clients.save_submission(db, None, snapshot(body=body), body={"debts": [{**body["debts"][0], "balance": "2"}]},
            expected_revision=4, base_version_id=None, idempotency_key="same-request")
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_public_read_unlocks_imported_rows_but_hides_excluded_history_and_returns_version_metadata():
    imported, excluded = row(), row(status="dismissed")
    version_id = str(uuid4())
    held = SimpleNamespace(id=uuid4(), kind="debt_schedule", profile_id=uuid4(), statement_id=None,
        financial_family_id=None, permission="edit", invitee_email=None, completed_at=None)
    value = snapshot(body={"debts": [imported, excluded]})
    value.update(current_version_id=version_id, versions=[{"id": version_id, "version_number": 1,
        "created_at": "2026-11-06T12:00:00+00:00", "pdf_file_id": str(uuid4()), "actor_name": "Internal staff", "status": "original", "source": "original"}])
    db = SimpleNamespace(info={}, get=AsyncMock(return_value=SimpleNamespace(id=held.profile_id)), commit=AsyncMock())
    with patch.object(routes, "_open_link_or_404", AsyncMock(return_value=held)), patch.object(clients, "ensure_snapshot", AsyncMock(return_value=value)), patch.object(routes.financial_statements, "form_prefill", AsyncMock(return_value={})), patch.object(routes.financial_statements, "debt_body_for_profile", AsyncMock(return_value={})):
        response = await routes.public_financial_form("held-token", db)
    assert len(response["body"]["debts"]) == 1 and response["body"]["debts"][0]["editable"] is True
    assert response["body"]["debts"][0]["origin"] == "ai_draft"
    assert held.financial_family_id == UUID(value["family_id"])
    assert response["workspace"]["current_version"]["version_number"] == 1
    assert response["workspace"]["current_version"]["pdf_url"].endswith(f"/{version_id}/pdf")
    assert "actor_name" not in response["workspace"]["current_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [routes.save_public_financial_form_draft, routes.submit_public_financial_form])
async def test_read_only_form_link_never_enters_the_mutation_pipeline(operation):
    link = SimpleNamespace(permission="view")
    with patch.object(routes, "_open_link_or_404", AsyncMock(return_value=link)), patch.object(routes, "_client_link_snapshot", AsyncMock()) as read:
        with pytest.raises(HTTPException) as error:
            await operation("token", FinancialStatementWrite(body={}), None)
    assert error.value.status_code == 403
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_pdf_link_cannot_open_a_different_owner_or_form_family_on_the_same_profile():
    profile = SimpleNamespace(id=uuid4())
    held = SimpleNamespace(kind="pfs", profile_id=profile.id, financial_family_id=uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=profile), commit=AsyncMock())
    with patch.object(routes, "_open_link_or_404", AsyncMock(return_value=held)), patch.object(routes, "_client_link_snapshot", AsyncMock(return_value={"family_id": str(held.financial_family_id)})), patch.object(workspace, "version_for_profile", AsyncMock(return_value=(SimpleNamespace(id=uuid4(), kind="pfs"), None))), patch.object(workspace, "version_pdf", AsyncMock()) as pdf:
        with pytest.raises(HTTPException) as error:
            await routes.public_financial_version_pdf("token", uuid4(), db)
    assert error.value.status_code == 404
    pdf.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_save_identifiers_are_link_scoped_and_fit_the_version_storage_column():
    a, b = uuid4(), uuid4()
    assert clients.submission_key(a, "request-123") != clients.submission_key(b, "request-123")
    assert len(clients.submission_key(a, "x" * 128)) <= 128


@pytest.mark.asyncio
async def test_automated_extraction_cannot_replace_a_clients_corrected_imported_values():
    from app.dealer_os.services import financial_debt_drafts

    original = row(document_id=None, evidence_links=[])
    body = clients.merge_client_body("debt_schedule", {"debts": [original]},
        {"debts": [{"id": original["id"], "balance": "800", "monthly_payment": "80"}]})
    draft = snapshot(body=body)
    managed = financial_debt_drafts.ManagedDebtDraft(SimpleNamespace(id=uuid4()), draft)
    with patch.object(financial_debt_drafts.financial_version_selection, "accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), patch.object(workspace, "save_draft", AsyncMock()) as save:
        proposed = await financial_debt_drafts.stage_proposals(None, managed,
            [{"lender": original["lender"], "balance": 1000, "monthly_payment": 100}], fields=("balance", "monthly_payment"))
    assert proposed["updated"] == 0 and body["debts"][0]["balance"] == "800"
    assert body["debts"][0]["origin"] == "ai_draft"
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_pfs_correction_creates_v2_and_preserves_v1_pdf_and_source_snapshot():
    from app.models.financial_form_version import FinancialFormVersion

    db = Memory()
    profile, family, draft = context("pfs")
    source_id = uuid4()
    original = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=1, schema_version="sba413.v1",
        body={}, key_facts={"net_worth": 100}, body_sha256=workspace.body_hash({}), pdf_sha256="original-hash",
        source="original", status="original", source_file_id=source_id, pdf_file_id=source_id, created_at=workspace.now())
    db.versions.append(original)
    db.objects[(FinancialFormVersion, original.id)] = original
    family.current_version_id = draft.base_version_id = original.id
    body = deepcopy(draft.body)
    body["assets"]["cash_on_hand"] = "1200"
    submitted = snapshot("pfs", revision=4, body=draft.body, base=original.id)
    submitted["family_id"] = str(family.id)
    async def read_snapshot(*_args, **_kwargs):
        return {**submitted, "body": deepcopy(draft.body), "draft": {"revision": draft.revision,
            "base_version_id": str(draft.base_version_id), "dirty": False},
            "current_version_id": str(family.current_version_id), "versions": [workspace._version_read(v) for v in db.versions]}
    actor = SimpleNamespace(id=None, name="Client", email="client@example.test")
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "load_workspace", read_snapshot), patch.object(workspace, "_render", return_value=b"%PDF-new-client-version"), patch.object(workspace, "_write_pdf") as storage, patch.object(workspace, "_project", AsyncMock()) as project, patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch.object(routes, "_requested_slot", AsyncMock(return_value=SimpleNamespace(id=uuid4(), status="requested"))), patch("app.services.financial_publication.after_publication", AsyncMock()):
        saved = await clients.save_submission(db, profile, submitted, body=body, expected_revision=4,
            base_version_id=original.id, idempotency_key="client-save-v2", actor=actor)
    assert saved["saved_version"]["version_number"] == 2
    assert len(db.versions) == 2 and db.versions[1].body["assets"]["cash_on_hand"] == "1200"
    assert family.current_version_id == db.versions[1].id and draft.base_version_id == db.versions[1].id
    assert original.body == {} and original.pdf_file_id == original.source_file_id == source_id
    assert db.versions[1].actor_name == "Client" and db.versions[1].created_at is not None
    assert f"/v2-{db.versions[1].pdf_sha256}.pdf" in storage.call_args.args[0]
    assert project.await_args.args[3]["assets"]["cash_on_hand"] == "1200"


@pytest.mark.asyncio
async def test_client_pdf_failure_does_not_publish_or_lose_the_already_autosaved_draft():
    db = Memory()
    profile, family, draft = context("pfs")
    draft.body["assets"]["cash_on_hand"] = "1200"
    current = snapshot("pfs", revision=4, body=draft.body)
    current["family_id"] = str(family.id)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_render", return_value=b"%PDF-new-client-version"), patch.object(workspace, "_write_pdf", side_effect=RuntimeError("storage unavailable")), patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch.object(workspace, "_project", AsyncMock()) as project:
        with pytest.raises(HTTPException) as error:
            await clients.save_submission(db, profile, current, body=deepcopy(draft.body), expected_revision=4,
                base_version_id=None, idempotency_key="retry-after-storage")
    assert error.value.status_code == 502
    assert draft.body["assets"]["cash_on_hand"] == "1200" and family.current_version_id is None
    assert db.versions == []
    project.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_changed_body_submit_retains_draft_before_pdf_retry_without_advancing_original():
    from app.models.financial_form_version import FinancialFormVersion

    db = Memory()
    profile, family, draft = context("pfs")
    original = FinancialFormVersion(id=uuid4(), family_id=family.id, version_number=1, schema_version="sba413.v1",
        body={}, key_facts={}, body_sha256=workspace.body_hash({}), pdf_sha256="original-hash",
        source="original", status="original", source_file_id=uuid4(), pdf_file_id=uuid4(), created_at=workspace.now())
    db.versions.append(original)
    db.objects[(FinancialFormVersion, original.id)] = original
    family.current_version_id = draft.base_version_id = original.id
    first_read = snapshot("pfs", revision=4, body=draft.body, base=original.id)
    first_read["family_id"] = str(family.id)
    changed = deepcopy(draft.body)
    changed["assets"]["cash_on_hand"] = "1200"
    events = []
    class PublicationSavepoint:
        async def __aenter__(self):
            assert draft.body["assets"]["cash_on_hand"] == "1200" and draft.revision == 5
            events.append("publication_started")
        async def __aexit__(self, error_type, error, traceback):
            events.append("publication_rolled_back" if error_type else "publication_succeeded")
    db.begin_nested = lambda: PublicationSavepoint()
    async def commit_draft():
        assert events[-1] == "publication_rolled_back"
        assert family.current_version_id == original.id and db.versions == [original]
        events.append("working_draft_committed")
    db.commit = AsyncMock(side_effect=commit_draft)
    async def read_snapshot(*_args, **_kwargs):
        return {**first_read, "body": deepcopy(draft.body), "draft": {"revision": draft.revision,
            "base_version_id": str(draft.base_version_id), "dirty": True}, "current_version_id": str(family.current_version_id),
            "versions": [workspace._version_read(v) for v in db.versions]}
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "load_workspace", read_snapshot), patch.object(workspace, "_render", return_value=b"%PDF-client-correction"), patch.object(workspace, "_write_pdf", side_effect=[RuntimeError("storage unavailable"), None]), patch.object(workspace, "_project", AsyncMock()) as project, patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch.object(routes, "_requested_slot", AsyncMock(return_value=SimpleNamespace(id=uuid4(), status="requested"))), patch("app.services.financial_publication.after_publication", AsyncMock()):
        with pytest.raises(HTTPException) as failure:
            await clients.save_submission(db, profile, first_read, body=changed, expected_revision=4,
                base_version_id=original.id, idempotency_key="retry-retained-draft")
        assert failure.value.status_code == 502
        assert failure.value.detail["workspace"]["revision"] == 5
        assert failure.value.detail["workspace"]["base_version_id"] == str(original.id)
        assert "Reload" in failure.value.detail["message"]
        assert events == ["publication_started", "publication_rolled_back", "working_draft_committed"]
        assert draft.body["assets"]["cash_on_hand"] == "1200" and draft.revision == 5
        assert draft.base_version_id == family.current_version_id == original.id
        project.assert_not_awaited()
        retained = await read_snapshot()
        with pytest.raises(HTTPException) as stale:
            await clients.save_submission(db, profile, retained, body=changed, expected_revision=4,
                base_version_id=original.id, idempotency_key="retry-retained-draft")
        assert stale.value.status_code == 409
        saved = await clients.save_submission(db, profile, retained, body=changed, expected_revision=5,
            base_version_id=original.id, idempotency_key="retry-retained-draft")
    assert saved["saved_version"]["version_number"] == 2
    assert len(db.versions) == 2 and db.versions[1].idempotency_key == "retry-retained-draft"
    assert original.body == {} and original.pdf_sha256 == "original-hash"
    assert events[-1] == "publication_succeeded"
    db.commit.assert_awaited_once()
