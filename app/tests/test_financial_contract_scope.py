from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import financial_workspace as workspace


def source(bucket_id):
    return SimpleNamespace(id=uuid4(), bucket_id=bucket_id, deleted_at=None, status="uploaded",
                           source_kind=None, source_detail=None, source_entity_type=None)


@pytest.mark.asyncio
async def test_uploaded_contract_owned_by_file_is_available_without_other_bucket_reads():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=None)
    file = source(profile.primary_bucket_id)
    db = SimpleNamespace(get=AsyncMock(return_value=file))
    with patch.object(workspace, "source_files_for_profile", AsyncMock()) as selected:
        assert await workspace.source_file_for_profile(db, profile, file.id) is file
        selected.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicitly_selected_supporting_contract_can_be_linked_to_loan():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=uuid4())
    file = source(uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=file))
    with patch.object(workspace, "source_files_for_profile", AsyncMock(return_value=[file])):
        assert await workspace.source_file_for_profile(db, profile, file.id) is file


@pytest.mark.asyncio
async def test_other_clients_contract_is_rejected_even_if_file_id_is_known():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=uuid4())
    file = source(uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=file))
    with patch.object(workspace, "source_files_for_profile", AsyncMock(return_value=[])):
        with pytest.raises(HTTPException) as error:
            await workspace.source_file_for_profile(db, profile, file.id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_removed_contract_is_not_available_as_new_loan_evidence():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=None)
    file = source(profile.primary_bucket_id)
    file.deleted_at = datetime.now(UTC)
    with pytest.raises(HTTPException) as error:
        await workspace.source_file_for_profile(SimpleNamespace(get=AsyncMock(return_value=file)), profile, file.id)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_source_picker_combines_selected_documents_without_duplicates_or_removed_files():
    profile = SimpleNamespace(id=uuid4(), primary_bucket_id=uuid4(), intake_id=uuid4())
    primary = source(profile.primary_bucket_id)
    supporting = source(uuid4())
    removed = source(uuid4())
    removed.deleted_at = datetime.now(UTC)
    pending = source(uuid4())
    pending.status = "pending"
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [primary]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    with patch("app.services.operator_file_links.selected_files_for_intake", AsyncMock(
        return_value=[primary, supporting, removed, pending],
    )):
        files = await workspace.source_files_for_profile(db, profile)
    assert [file.id for file in files] == [primary.id, supporting.id]
