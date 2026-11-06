"""Accepted row identity, supporting files and exact ZIP delivery remain bound."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import debt_package_evidence as service
from app.services.public_underwriting_packet_pdf import _debt_evidence_section


def file(name="agreement.png", *, state="current", bucket_id=None):
    return SimpleNamespace(id=uuid4(), file_name=name, content_type="image/png", content_hash=hashlib.sha256(name.encode()).hexdigest(),
                           status="uploaded", deleted_at=None, document_version_status=state,
                           bucket_id=bucket_id or uuid4(), size_bytes=len(name), s3_key=f"private/{uuid4()}", source_kind="client_room", source_detail="")


def version(rows):
    return SimpleNamespace(id=uuid4(), version_number=3, created_at=datetime(2026, 11, 6, tzinfo=UTC),
                           body={"debts": rows}, body_sha256="b" * 64, pdf_file_id=uuid4())


def link(source, role="agreement", **fields):
    return {"file_id": str(source.id), "role": role, **fields}


def debt(name="SBA", **fields):
    return {"id": str(uuid4()), "lender": name, "balance": "100", "monthly_payment": "10", "status": "active", **fields}


def manifest(saved, files, *, latest=None, reviewed=(), scoped=None, excluded=()):
    return service.build_manifest(saved, files, scoped_ids=scoped if scoped is not None else [source.id for source in files],
                                  latest_ids=latest if latest is not None else [source.id for source in files],
                                  reviewed_payoff_ids=reviewed, excluded_ids=excluded)


def test_same_lender_loans_remain_separate_and_duplicate_sources_follow_only_survivor():
    image, agreement, extra = file(), file("contract.pdf"), file("alias.png")
    row = debt(evidence_links=[link(image), link(agreement)])
    other = debt(evidence_links=[link(extra)])
    alias = debt(status="duplicate", duplicate_of_id=row["id"], evidence_links=[link(extra), link(image)])
    removed = debt("Removed", status="dismissed", evidence_links=[link(file("private.pdf"))])
    saved = version([alias, row, other, removed, {"id": str(uuid4()), "status": "active"}])
    before = deepcopy(saved.body)
    result = manifest(saved, [image, agreement, extra])
    assert [entry["row_id"] for entry in result["debts"]] == [row["id"], other["id"]]
    assert len(result["debts"][0]["documents"]) == 3
    assert result["debts"][0]["documents"][0]["duplicate_row_ids"] == [alias["id"]]
    assert len(result["debts"][1]["documents"]) == 1
    assert saved.body == before


def test_explicitly_unlinked_sources_do_not_return_from_legacy_documents():
    source = file()
    row = debt(evidence_links=[], documents=[link(source)])
    assert manifest(version([row]), [source])["debts"][0]["documents"] == []
    row.pop("evidence_links")
    assert len(manifest(version([row]), [source])["debts"][0]["documents"]) == 1


@pytest.mark.parametrize("latest,reviewed,state,expected", [(True, True, "current", True), (True, False, "current", False),
    (False, True, "current", False), (True, True, "pending", True), (True, False, "pending", False)])
def test_only_explicit_saved_payoff_selection_and_usable_latest_source_is_current(latest, reviewed, state, expected):
    source = file("payoff.png", state=state)
    row = debt(evidence_links=[link(source, "payoff", is_current=True, payoff_amount="0", payoff_date="2026-11-01")])
    result = manifest(version([row]), [source], latest=[source.id] if latest else [], reviewed=[source.id] if reviewed else [])
    document = result["debts"][0]["documents"][0]
    assert document["current_payoff"] is expected
    assert document["reviewed_payoff_amount"] == "0" and document["reviewed_payoff_date"] == "2026-11-01"
    assert "0" in service.index_text(result)


def test_duplicate_payoff_does_not_overrule_survivor_selection_even_when_alias_precedes_it():
    source = file()
    row = debt(evidence_links=[link(source, "payoff", is_current=True, payoff_amount="50")])
    alias = debt(status="duplicate", duplicate_of_id=row["id"], evidence_links=[link(source, "payoff", is_current=True, payoff_amount="999")])
    document = manifest(version([alias, row]), [source], reviewed=[source.id])["debts"][0]["documents"][0]
    assert document["current_payoff"] and document["reviewed_payoff_amount"] == "50"
    assert document["duplicate_row_ids"] == [alias["id"]]


@pytest.mark.parametrize("case", ["cross_scope", "rejected", "deleted", "missing", "merchant_offer"])
def test_unavailable_or_private_sources_never_become_deliverable(case):
    source = file("DO-NOT-LEAK.png")
    if case == "rejected":
        source.status = "rejected"
    if case == "deleted":
        source.deleted_at = datetime.now(UTC)
    result = manifest(version([debt(evidence_links=[link(source)])]), [] if case == "missing" else [source],
                      scoped=[] if case == "cross_scope" else [source.id], excluded=[source.id] if case == "merchant_offer" else [])
    document = result["debts"][0]["documents"][0]
    assert not document["available"]
    if case in {"cross_scope", "merchant_offer", "missing"}:
        assert document["file_name"] is None and document["content_hash"] is None
        assert "DO-NOT-LEAK" not in json.dumps(result)


def test_indexes_use_actual_collision_names_escape_html_and_do_not_expose_storage_keys():
    source = file('Contract <script>.png')
    value = manifest(version([debt(evidence_links=[link(source)])]), [source])
    archived = {str(source.id): {"path": "documents/contract & proof-2.png", "sha256": "a" * 64}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        service.write_indexes(archive, value, archived)
    with zipfile.ZipFile(output) as archive:
        payload = json.loads(archive.read("debt-evidence.json"))
        html = archive.read("debt-evidence.html").decode()
    document = payload["debts"][0]["documents"][0]
    assert document["archive_path"] == archived[str(source.id)]["path"]
    assert document["delivery_status"] == "archived"
    assert 'href="documents/contract%20%26%20proof-2.png"' in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert source.s3_key not in json.dumps(payload) and "https://" not in html
    failed = service.delivery_manifest(value, {}, [source.id])["debts"][0]["documents"][0]
    assert failed["delivery_status"] == "fetch_failed" and failed["archive_path"] is None


def test_pdf_breakdown_contains_multiple_documents_and_reviewed_payoff_without_urls():
    image, agreement, payoff = file("screenshot.png"), file("contract.pdf"), file("payoff.pdf")
    saved = version([debt(evidence_links=[link(image), link(agreement), link(payoff, "payoff", is_current=True, payoff_amount="75.00", payoff_date="2026-11-01")])])
    value = manifest(saved, [image, agreement, payoff], reviewed=[payoff.id])
    html = _debt_evidence_section({"debt_evidence": value})
    assert all(source.file_name in html for source in [image, agreement, payoff])
    assert "Current reviewed payoff" in html and "$75" in html and "2026-11-01" in html
    assert "1. SBA" in html and "Nov 06, 2026 00:00 UTC" in html
    assert saved.body["debts"][0]["id"] not in html
    assert saved.body["debts"][0]["id"] not in service.index_text(value)
    assert saved.body["debts"][0]["id"] not in service.index_html(value)
    assert "https://" not in html and "private/" not in html
    assert _debt_evidence_section({}) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "hash", "fetch"])
async def test_zip_delivery_matches_actual_files_and_flags_linked_failures(failure):
    from app.routers import dealer_ai_intake as core

    a, b = file("same.png"), file("same.png")
    a.content_hash = hashlib.sha256(b"first").hexdigest()
    b.content_hash = hashlib.sha256(b"second").hexdigest()
    value = manifest(version([debt(evidence_links=[link(a), link(b)])]), [a, b])
    summary = SimpleNamespace(s3_key="summary", body_json={}, body_text="Summary")
    packet = SimpleNamespace(s3_key="packet", title="Lender packet", body_json={"debt_evidence": value})
    intake = SimpleNamespace(id=uuid4(), business_name="Example", full_name="Operator", email="example@example.invalid", bucket=object())
    async def contents(key):
        if key == b.s3_key and failure == "fetch":
            raise OSError("unavailable")
        return {a.s3_key: b"first", b.s3_key: b"wrong" if failure == "hash" else b"second"}.get(key, b"PDF")
    with patch.object(core, "_fresh_package_artifact_pair", AsyncMock(return_value=(summary, packet))), patch.object(core, "_generate_management_json", AsyncMock(return_value={})), patch.object(core, "_active_files", return_value=[a, b]), patch.object(core, "_s3_bytes", side_effect=contents), patch.object(service, "delivery_files", AsyncMock(return_value=[])):
        _name, raw = await core.build_package_zip_bytes(SimpleNamespace(), intake, SimpleNamespace())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        payload = json.loads(archive.read("debt-evidence.json"))
        docs = payload["debts"][0]["documents"]
        for document in docs:
            path = document["archive_path"]
            if path:
                assert hashlib.sha256(archive.read(path)).hexdigest() == document["archived_sha256"] == document["content_hash"]
        if failure:
            assert next(document for document in docs if document["file_id"] == str(b.id))["delivery_status"] == "fetch_failed"
            assert b"NOT INCLUDED" in archive.read("README.txt")
        else:
            assert len({document["archive_path"] for document in docs}) == 2
            assert {document["archive_path"] for document in docs} == {"documents/same.png", "documents/same-2.png"}


def test_manifest_changes_invalidate_package_fingerprint_but_untouched_source_does_not():
    from app.routers.dealer_ai_intake import _package_input_fingerprint

    source = file()
    saved = version([debt(evidence_links=[link(source)])])
    before = manifest(saved, [source])
    def fingerprint(value):
        return _package_input_fingerprint(source_metadata={}, context_snapshot={}, financials={"debt_evidence": value})
    assert fingerprint(before) == fingerprint(deepcopy(before))
    source.content_hash = "c" * 64
    assert fingerprint(before) != fingerprint(manifest(saved, [source]))
    saved.id = uuid4()
    assert fingerprint(before) != fingerprint(manifest(saved, [source]))


@pytest.mark.asyncio
async def test_unpublished_draft_cannot_supply_a_package_manifest():
    with patch.object(service, "accepted_debt_schedule_snapshot", AsyncMock(return_value=None)), patch("app.services.financial_workspace.source_files_for_profile", AsyncMock()) as sources:
        result = await service.load_manifest(SimpleNamespace(), SimpleNamespace(id=uuid4()))
    assert result == {"status": "no_accepted_schedule", "schedule": None, "debts": []}
    sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_intake_profile_binding_must_match_room_before_exposing_saved_sources():
    class Rows:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4())
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows()))
    intake = SimpleNamespace(id=uuid4(), bucket_id=uuid4())
    with patch.object(service, "load_manifest", AsyncMock()) as load:
        result = await service.manifest_for_intake(db, intake)
    assert result["status"] == "no_accepted_schedule"
    assert db.execute.await_args.args[0].compile().params["intake_id_1"] == intake.id
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_loader_rechecks_cross_room_scope_and_merchant_exclusion_before_delivery():
    class Rows:
        def scalars(self):
            return self
        def all(self):
            return [source, private, offer]
    bucket = uuid4()
    source, private, offer = file(bucket_id=bucket), file("private.png"), file("offer.pdf", bucket_id=bucket)
    saved = version([debt(evidence_links=[link(source), link(private), link(offer)])])
    db = SimpleNamespace(info={}, execute=AsyncMock(return_value=Rows()))
    with patch.object(service, "accepted_debt_schedule_snapshot", AsyncMock(return_value=saved)), patch("app.services.financial_workspace.source_files_for_profile", AsyncMock(return_value=[source, offer])), patch("app.services.document_version_selection.document_file_ids", AsyncMock(return_value={source.id, private.id, offer.id})), patch("app.services.financial_debt_actions.reviewed_payoff_source_ids", AsyncMock(return_value=set())), patch("app.services.merchant_processing.is_offer_document", side_effect=lambda candidate: candidate.id == offer.id):
        result = await service.load_manifest(db, SimpleNamespace(id=uuid4(), primary_bucket_id=bucket))
    documents = {document["file_id"]: document for document in result["debts"][0]["documents"]}
    assert service.available_file_ids(result) == {source.id}
    assert documents[str(private.id)]["status"] == "out_of_scope" and documents[str(private.id)]["file_name"] is None
    assert documents[str(offer.id)]["status"] == "excluded" and documents[str(offer.id)]["file_name"] is None


@pytest.mark.asyncio
async def test_linked_heic_keeps_original_and_separately_hashed_readable_preview():
    from app.routers import dealer_ai_intake as core
    from app.tests.test_debt_image_imports import heic_bytes

    original = heic_bytes()
    source = file("Loan screenshot.heic")
    source.content_type, source.content_hash = "image/heic", hashlib.sha256(original).hexdigest()
    value = manifest(version([debt(evidence_links=[link(source, "supporting")])]), [source])
    summary = SimpleNamespace(s3_key="summary", body_json={}, body_text="Summary")
    packet = SimpleNamespace(s3_key="packet", title="Lender packet", body_json={"debt_evidence": value})
    intake = SimpleNamespace(id=uuid4(), business_name="Example", full_name="Operator", email="example@example.invalid", bucket=object())
    with patch.object(core, "_fresh_package_artifact_pair", AsyncMock(return_value=(summary, packet))), patch.object(core, "_generate_management_json", AsyncMock(return_value={})), patch.object(core, "_active_files", return_value=[source]), patch.object(core, "_s3_bytes", AsyncMock(side_effect=lambda key: original if key == source.s3_key else b"PDF")), patch.object(service, "delivery_files", AsyncMock(return_value=[])):
        _name, raw = await core.build_package_zip_bytes(SimpleNamespace(), intake, SimpleNamespace())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        document = json.loads(archive.read("debt-evidence.json"))["debts"][0]["documents"][0]
        assert archive.read(document["archive_path"]) == original
        assert document["archived_sha256"] == source.content_hash
        preview = archive.read(document["preview_path"])
        assert preview.startswith(b"\xff\xd8\xff") and document["preview_sha256"] == hashlib.sha256(preview).hexdigest()
        assert document["preview_status"] == "available" and not document["current_payoff"]
        assert b"JPEG preview" in archive.read("debt-evidence.html")
