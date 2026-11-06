"""A current original PDF is not the accepted numeric debt schedule."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import financial_workspace as workspace


def schedule(active=5, excluded=3):
    return {"business_name": "Example business", "debts": [
        {"id": str(uuid4()), "lender": f"Funder {index}", "balance": "100", "monthly_payment": "10", "status": "active"}
        for index in range(active)
    ] + [{"id": str(uuid4()), "lender": f"Excluded {index}", "balance": "100", "monthly_payment": "10", "status": "dismissed"}
         for index in range(excluded)]}


async def snapshot(body, current_body, *, source="original", status="original"):
    stamp = workspace.now()
    profile = SimpleNamespace(id=uuid4())
    family = SimpleNamespace(id=uuid4(), label="Business debt schedule", current_version_id=uuid4())
    current = SimpleNamespace(id=family.current_version_id, body=current_body, body_sha256=workspace.body_hash(current_body),
                              version_number=1, created_at=stamp, actor_name="Client", source=source, status=status,
                              source_file_id=uuid4(), pdf_file_id=uuid4())
    draft = SimpleNamespace(body=body, revision=4, base_version_id=current.id, updated_at=stamp)
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [current]))
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    with patch.object(workspace, "resolve_family", AsyncMock(return_value=(family, draft))), \
         patch.object(workspace, "source_files_for_profile", AsyncMock(return_value=[])), \
         patch.object(workspace, "_families", AsyncMock(return_value=[])):
        result = await workspace.load_workspace(db, profile, kind="debt_schedule")
    return result, current


@pytest.mark.asyncio
async def test_empty_original_does_not_hide_complete_five_row_working_schedule():
    body = schedule()
    before = deepcopy(body)
    result, original = await snapshot(body, {})
    assert result["completion"]["complete"] and result["completion"]["active_rows"] == 5
    assert result["completion"]["missing_fields"] == []
    assert result["accepted_completion"] is None and result["accepted_version_id"] is None
    assert result["current_version_id"] == str(original.id) and result["draft"]["dirty"]
    assert len(result["body"]["debts"]) == 8 and body == before and original.body == {}
    assert result["worksheet"]["computed"]["total_balance"] == 500


@pytest.mark.asyncio
async def test_incomplete_working_changes_do_not_replace_published_completion_metadata():
    published = schedule()
    working = deepcopy(published)
    working["debts"][0]["monthly_payment"] = None
    result, version = await snapshot(working, published, source="form", status="filed")
    assert not result["completion"]["complete"]
    assert result["completion"]["missing_fields"] == [f"{working['debts'][0]['id']}.monthly_payment"]
    assert result["accepted_completion"]["complete"] and result["accepted_completion"]["active_rows"] == 5
    assert result["accepted_version_id"] == str(version.id) and result["draft"]["dirty"]
    assert version.body == published
