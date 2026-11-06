"""Selected-loan image originals stay scoped, reviewed, and separate by row UUID."""
import base64
import copy
import hashlib
import io
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from PIL import Image

from app.models.bucket import BucketFile
from app.services import bucket_ai
from app.services import financial_debt_document_imports as imports
from app.services import financial_source_images as images
from app.services import financial_workspace as workspace
from app.tests.test_debt_document_imports import context, debt, extracted


def image_bytes(format="PNG", color="white", size=(120, 80)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=format)
    return output.getvalue()


def image_context(*rows, format="PNG", classification="other"):
    db, profile, family, draft, file, _raw = context(*rows, classification=classification)
    raw = image_bytes(format)
    media = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}[format]
    file.content_type, file.file_name = media, f"Loan screenshot.{format.lower()}"
    file.content_hash = hashlib.sha256(raw).hexdigest()
    db.analyses[0].content_hash = file.content_hash
    return db, profile, family, draft, file, raw


@pytest.mark.parametrize("format,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")])
def test_actual_image_bytes_decode_for_supported_still_formats(format, media):
    assert images.validated_image_media(image_bytes(format)) == media


@pytest.mark.parametrize("raw", [b"\x89PNG\r\n\x1a\nnot-an-image", b"\xff\xd8\xfftruncated-jpeg", b"GIF89acorrupt"])
def test_corrupt_claimed_image_headers_are_rejected_before_model(raw):
    with pytest.raises(HTTPException) as error:
        images.validated_image_media(raw)
    assert error.value.status_code == 422


def test_oversized_and_excessive_dimension_images_do_not_reach_model(monkeypatch):
    raw = image_bytes()
    monkeypatch.setattr(bucket_ai, "MAX_FILE_BYTES", len(raw) - 1)
    with pytest.raises(HTTPException, match="too large"):
        images.validated_image_media(raw)
    monkeypatch.setattr(bucket_ai, "MAX_FILE_BYTES", len(raw) + 1)
    monkeypatch.setattr(images, "MAX_IMAGE_EDGE", 100)
    with pytest.raises(HTTPException, match="dimensions"):
        images.validated_image_media(raw)


def test_animated_images_require_explicit_still_frames():
    output = io.BytesIO()
    Image.new("RGB", (50, 50), "red").save(output, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (50, 50), "blue")], duration=100, loop=0)
    with pytest.raises(HTTPException, match="multi-frame"):
        images.validated_image_media(output.getvalue())


def test_missing_heic_decoder_gives_actionable_retained_source_error():
    import builtins

    original = builtins.__import__

    def no_decoder(name, *args, **kwargs):
        if name == "pillow_heif":
            raise ImportError("Decoder unavailable in this test")
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=no_decoder), pytest.raises(HTTPException, match="HEIC review is unavailable"):
        images.validated_image_media(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 30)


@pytest.mark.asyncio
async def test_real_image_extractor_skips_acroform_uses_vision_and_sanitizes_all_sba_fields():
    raw = image_bytes()
    file = SimpleNamespace(id=uuid4(), file_name="SBA account.png", content_type="image/png")
    body = {"business_name": "Example borrower", "debts": [{"id": "model-owned-id", "lender": "SBA", "debt_type": "sba_loan",
        "original_amount": "10000", "balance": "8000", "rate": "9.5", "monthly_payment": "250",
        "originated_on": "2022-01-01", "maturity_on": "2032-01-01", "secured": "secured", "payment_status": "current",
        "collateral": "Business assets", "notes": "Printed loan detail", "factor_rate": None,
        "status": "dismissed", "duplicate_of_id": str(uuid4()), "count_in_dscr": False,
        "evidence_links": [{"file_id": str(uuid4()), "role": "payoff"}]}]}
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": body, "references": {"r1.balance": {"page": 9, "quote": "Balance $8000"}}}))])
    with patch.object(workspace, "_extract_acroform", side_effect=AssertionError("Images have no AcroForm")), patch(
        "app.services.ai.bedrock_client.get_client", return_value=object()
    ), patch("app.services.ai.bedrock_client.model_heavy", return_value="test"), patch(
        "app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)
    ) as model:
        proposed, _refs, warnings, pages = await workspace._extract_full_form(AsyncMock(), "debt_schedule", file, raw, extra_instructions=imports.CONTRACT_INSTRUCTIONS)
    row = proposed["debts"][0]
    assert row["id"] != "model-owned-id" and row["status"] == "active" and row["origin"] == "ai_draft"
    assert "duplicate_of_id" not in row and "evidence_links" not in row and "count_in_dscr" not in row
    assert row["debt_type"] == "sba_loan" and Decimal(row["original_amount"]) == 10000 and Decimal(row["balance"]) == 8000
    assert row["rate"] == "9.500" and row["monthly_payment"] == "250.00" and row["factor_rate"] is None
    assert row["originated_on"] == "2022-01-01" and row["maturity_on"] == "2032-01-01"
    assert row["secured"] == "secured" and row["payment_status"] == "current" and row["collateral"] == "Business assets"
    assert pages == {} and any("visual review" in warning for warning in warnings)
    blocks = model.await_args.kwargs["messages"][0]["content"]
    image = next(block for block in blocks if block["type"] == "image")
    assert image["source"]["media_type"] == "image/png" and base64.b64decode(image["source"]["data"]) == raw
    assert "bank deposit/account balance is not a loan balance" in blocks[0]["text"]


@pytest.mark.asyncio
async def test_image_preview_uses_original_hash_no_fake_pdf_page_and_changes_no_draft():
    selected = debt("SBA", monthly_payment="250")
    db, profile, family, draft, file, raw = image_context(selected, classification="bank_statement")
    before = copy.deepcopy(draft.body)
    source = debt("SBA", balance="7900", rate="9.5")
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", return_value=raw), patch.object(
        workspace, "_extract_full_form", AsyncMock(return_value=({"debts": [source]}, {"r1.balance": {"page": 1, "quote": "Balance 7900"}}, [], {1: "Balance 7900"}))
    ):
        proposal = await imports.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4, role="supporting")
    assert draft.body == before and draft.revision == 4 and not db.versions
    assert proposal["target_row_id"] == selected["id"] and proposal["role"] == "supporting"
    assert all(mapping["key"].startswith(selected["id"] + ".") for mapping in proposal["mappings"])
    for mapping in proposal["mappings"]:
        assert mapping["source"]["kind"] == "image" and mapping["source"]["content_type"] == "image/png"
        assert mapping["source"]["content_hash"] == hashlib.sha256(raw).hexdigest()
        assert "page" not in mapping["source"] and mapping["confidence"] == "needs_review"
    assert next(mapping for mapping in proposal["mappings"] if mapping["key"].endswith(".balance"))["conflict"]


@pytest.mark.asyncio
async def test_multiple_images_attach_to_one_sba_row_without_replacing_payoff_or_other_loans():
    payoff_id = uuid4()
    selected = debt("SBA", monthly_payment="250", evidence_links=[{"file_id": str(payoff_id), "role": "payoff", "is_current": True, "payoff_amount": "8000", "payoff_date": "2026-11-01"}])
    other = debt("SBA", monthly_payment="750", balance="30000")
    db, profile, family, draft, first, raw = image_context(selected, other)
    db.objects[(BucketFile, payoff_id)] = SimpleNamespace(id=payoff_id, bucket_id=profile.primary_bucket_id)
    second = SimpleNamespace(**vars(first))
    second.id, second.file_name = uuid4(), "SBA interest detail.jpeg"
    second_raw = image_bytes("JPEG", "blue")
    second.content_type, second.content_hash = "image/jpeg", hashlib.sha256(second_raw).hexdigest()
    db.objects[(BucketFile, second.id)] = second
    sources = {first.id: raw, second.id: second_raw}
    candidates = [extracted(debt("SBA", balance="7900")), extracted(debt("SBA", rate="8.5"))]
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", side_effect=lambda file: sources[file.id]), patch.object(
        workspace, "_extract_full_form", AsyncMock(side_effect=candidates)
    ), patch.object(workspace, "load_workspace", AsyncMock(return_value={})):
        for file, field in ((first, "balance"), (second, "rate")):
            proposal = await imports.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=draft.revision, role="supporting")
            await workspace.apply_import(db, profile, UUID(proposal["id"]), expected_revision=draft.revision, selected_keys=[f"{selected['id']}.{field}"])
    chosen, unchanged = draft.body["debts"]
    assert chosen["id"] == selected["id"] and Decimal(chosen["balance"]) == 7900 and chosen["rate"] == "8.500"
    assert chosen["monthly_payment"] == "250.00" and len(chosen["evidence_links"]) == 3
    assert next(link for link in chosen["evidence_links"] if link["file_id"] == str(payoff_id)) == selected["evidence_links"][0]
    assert unchanged["id"] == other["id"] and Decimal(unchanged["balance"]) == 30000 and unchanged["evidence_links"] == []
    assert len(draft.body["debts"]) == 2 and not db.versions


@pytest.mark.asyncio
async def test_same_lender_multi_account_image_remains_ambiguous_without_any_row_or_link_changes():
    rows = [debt("SBA", balance=str(amount)) for amount in (10000, 20000, 30000)]
    db, profile, family, draft, file, raw = image_context(*rows)
    before = copy.deepcopy(draft.body)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", return_value=raw), patch.object(
        workspace, "_extract_full_form", AsyncMock(return_value=extracted(*[debt("SBA", balance=str(amount)) for amount in (9000, 19000, 29000)]))
    ):
        with pytest.raises(HTTPException) as error:
            await imports.propose_document_import(db, profile, row_id=UUID(rows[0]["id"]), file_id=file.id, expected_revision=4)
    assert error.value.status_code == 409 and error.value.detail["code"] == "ambiguous_contract_obligation"
    assert draft.body == before and not db.imports and not db.versions


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["scope", "changed_bytes", "svg", "spoofed_image"])
async def test_image_scope_hash_and_signature_guards_precede_model(bad):
    selected = debt("SBA")
    db, profile, family, draft, file, raw = image_context(selected)
    if bad == "scope":
        file.bucket_id = uuid4()
    elif bad == "changed_bytes":
        raw = image_bytes(color="blue")
    elif bad == "svg":
        raw = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Balance 9000</text></svg>'
        file.content_type, file.file_name = "image/svg+xml", "loan.svg"
        file.content_hash = hashlib.sha256(raw).hexdigest()
    else:
        raw = b'not a real image'
        file.content_hash = hashlib.sha256(raw).hexdigest()
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", return_value=raw), patch.object(workspace, "_extract_full_form", AsyncMock()) as model:
        with pytest.raises(HTTPException) as error:
            await imports.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
    assert error.value.status_code in {404, 409, 422}
    model.assert_not_awaited()
    assert not db.imports and not db.versions


@pytest.mark.asyncio
async def test_image_apply_rechecks_original_bytes_and_keeps_link_unaccepted_when_changed():
    selected = debt("SBA")
    db, profile, family, draft, file, raw = image_context(selected)
    before = copy.deepcopy(draft.body)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", return_value=raw) as read, patch.object(
        workspace, "_extract_full_form", AsyncMock(return_value=extracted(debt("SBA", balance="7900")))
    ):
        proposal = await imports.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
        read.return_value = image_bytes(color="blue")
        with pytest.raises(HTTPException) as error:
            await workspace.apply_import(db, profile, UUID(proposal["id"]), expected_revision=4)
    assert error.value.status_code == 409 and draft.body == before and db.imports[-1].status == "proposed"


@pytest.mark.asyncio
async def test_mislabeled_image_original_is_preserved_without_a_pdf_claim():
    selected = debt("SBA")
    db, profile, family, draft, file, raw = image_context(selected, classification="debt_schedule")
    file.file_name, file.content_type = "Original.png.pdf", "application/pdf"
    before = copy.deepcopy(draft.body)
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", return_value=raw), patch.object(
        workspace, "_extract_full_form", AsyncMock(return_value=extracted(debt("SBA", balance="7900")))
    ), patch.object(workspace, "_write_pdf") as store:
        await workspace.propose_import(db, profile, kind="debt_schedule", file_id=file.id, family_id=family.id, analysis=db.analyses[0])
    original = db.versions[0]
    assert original.source == "original" and original.source_file_id == file.id and original.pdf_file_id is None
    assert original.pdf_sha256 == hashlib.sha256(raw).hexdigest() and original.body == {}
    assert workspace._version_read(original)["source_content_type"] == "image/png"
    assert family.current_version_id is None and draft.body == before
    store.assert_not_called()
    with patch.object(workspace, "version_for_profile", AsyncMock(return_value=(family, original))), patch.object(workspace, "_read_file", return_value=raw):
        with pytest.raises(HTTPException) as error:
            await workspace.version_pdf(db, profile, original.id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_real_saved_pdf_download_still_returns_its_original_verified_bytes():
    from pypdf import PdfWriter

    db, profile, family, _draft, file, _raw = image_context()
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    raw = output.getvalue()
    file.content_type, file.file_name = "application/pdf", "Saved version.pdf"
    version = SimpleNamespace(id=uuid4(), pdf_file_id=file.id, source_file_id=None, pdf_sha256=hashlib.sha256(raw).hexdigest())
    with patch.object(workspace, "version_for_profile", AsyncMock(return_value=(family, version))), patch.object(workspace, "_read_file", return_value=raw):
        assert await workspace.version_pdf(db, profile, version.id) == (raw, file.file_name)



def heic_bytes():
    import pillow_heif
    from PIL import ImageDraw

    image = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(image).text((12, 12), "SBA LOAN ACCOUNT 1234\nBalance $8,000\nPayment $250", fill="black")
    output = io.BytesIO()
    pillow_heif.from_pillow(image).save(output, quality=90)
    return output.getvalue()


def test_real_heic_original_decodes_to_readable_jpeg_without_changing_original():
    raw = heic_bytes()
    before = hashlib.sha256(raw).hexdigest()
    assert images.validated_image_media(raw) == "image/heic"
    preview = images.jpeg_preview(raw)
    assert preview.startswith(b"\xff\xd8\xff") and preview != raw
    with Image.open(io.BytesIO(preview)) as image:
        assert image.format == "JPEG" and image.size == (320, 180)
        assert image.convert("L").getextrema()[0] < 100  # Printed text survives.
    assert hashlib.sha256(raw).hexdigest() == before


@pytest.mark.asyncio
async def test_real_heic_reaches_vision_as_jpeg_and_review_keeps_original_hash():
    selected = debt("SBA", monthly_payment="250")
    db, profile, family, draft, file, _png = image_context(selected)
    raw = heic_bytes()
    file.file_name, file.content_type, file.content_hash = "SBA loan.heic", "image/heic", hashlib.sha256(raw).hexdigest()
    db.analyses[0].content_hash = file.content_hash
    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"body": {
        "debts": [{"lender": "SBA", "debt_type": "sba_loan", "balance": "8000", "monthly_payment": "250"}]}}))])
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), patch.object(workspace, "_read_file", return_value=raw), patch.object(
        workspace, "_extract_acroform", side_effect=AssertionError("HEIC is not a PDF")
    ), patch("app.services.ai.bedrock_client.get_client", return_value=object()), patch(
        "app.services.ai.bedrock_client.model_heavy", return_value="test"
    ), patch("app.services.ai.usage.tracked_messages_create", AsyncMock(return_value=response)) as model:
        proposal = await imports.propose_document_import(db, profile, row_id=UUID(selected["id"]), file_id=file.id, expected_revision=4)
    block = next(block for block in model.await_args.kwargs["messages"][0]["content"] if block["type"] == "image")
    assert block["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(block["source"]["data"]).startswith(b"\xff\xd8\xff")
    assert all(mapping["source"]["content_hash"] == hashlib.sha256(raw).hexdigest() for mapping in proposal["mappings"])
    assert all(mapping["source"]["content_type"] == "image/heic" and mapping["confidence"] == "needs_review" for mapping in proposal["mappings"])
    assert draft.revision == 4 and not db.versions and draft.body["debts"][0]["evidence_links"] == []


def test_image_preview_s3_read_is_bounded_and_uses_retained_version(monkeypatch):
    from unittest.mock import Mock

    raw = heic_bytes()
    stream = io.BytesIO(raw)
    client = Mock()
    client.get_object.return_value = {"Body": stream, "ContentLength": len(raw)}
    file = SimpleNamespace(s3_key="private/source.heic", s3_version_id="retained-version")
    with patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch(
        "app.routers.buckets._s3_client", return_value=client
    ):
        assert images._read_preview_source(file) == raw
    assert stream.closed
    client.get_object.assert_called_once_with(Bucket="bucket", Key="private/source.heic", VersionId="retained-version")
    stream = io.BytesIO(raw)
    client.get_object.return_value = {"Body": stream}
    monkeypatch.setattr(bucket_ai, "MAX_FILE_BYTES", 10)
    with patch("app.routers.buckets._bucket_storage_config", return_value=("bucket", "prefix", None)), patch(
        "app.routers.buckets._s3_client", return_value=client
    ), pytest.raises(HTTPException, match="size limit"):
        images._read_preview_source(file)
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["other_profile", "expected_hash", "stored_bytes_changed"])
async def test_image_preview_enforces_existing_scope_and_original_hash(failure):
    db, profile, _family, _draft, file, raw = image_context()
    expected = file.content_hash
    if failure == "other_profile":
        file.bucket_id = uuid4()
    elif failure == "expected_hash":
        expected = "a" * 64
    else:
        raw = image_bytes(color="blue")
    with patch.object(images, "_read_preview_source", return_value=raw) as read:
        with pytest.raises(HTTPException) as error:
            await images.scoped_image_preview(db, profile, file.id, expected_content_hash=expected)
    assert error.value.status_code == (404 if failure == "other_profile" else 409)
    if failure != "stored_bytes_changed":
        read.assert_not_called()


@pytest.mark.asyncio
async def test_heic_preview_route_returns_jpeg_under_existing_review_role_and_scope():
    from app.enums import Role
    from app.routers import financial_workspace as routes

    db, profile, _family, _draft, file, _png = image_context()
    raw = heic_bytes()
    file.content_hash = hashlib.sha256(raw).hexdigest()
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)), patch.object(images, "_read_preview_source", return_value=raw):
        response = await routes.preview_source_image(profile.id, file.id, SimpleNamespace(role=Role.LOAN_EXEC), db,
                                                     expected_content_hash=file.content_hash)
    assert response.media_type == "image/jpeg" and response.body.startswith(b"\xff\xd8\xff")
    assert response.headers["cache-control"] == "no-store" and response.headers["x-source-content-hash"] == file.content_hash
    assert file.content_hash == hashlib.sha256(raw).hexdigest() and not db.versions and not db.imports
    with patch.object(routes.profiles, "load_profile", AsyncMock()) as scope:
        with pytest.raises(HTTPException) as error:
            await routes.preview_source_image(profile.id, file.id, SimpleNamespace(role=Role.CLIENT), db,
                                              expected_content_hash=file.content_hash)
    assert error.value.status_code == 403
    scope.assert_not_awaited()
