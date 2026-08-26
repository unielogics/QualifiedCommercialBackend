from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.dealer_os.router import patch_application_finalization
from app.dealer_os.schemas import ApplicationFinalizationPatch
from app.enums import Role


def test_finalization_patch_requires_a_status_or_funded_amount() -> None:
    with pytest.raises(ValidationError):
        ApplicationFinalizationPatch()


def test_finalization_patch_accepts_funded_status_and_amount() -> None:
    payload = ApplicationFinalizationPatch(status="complete", funded_amount=325_000)

    assert payload.status == "complete"
    assert payload.funded_amount == 325_000


@pytest.mark.asyncio
async def test_finalization_endpoint_is_super_admin_only() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await patch_application_finalization(
            uuid4(),
            ApplicationFinalizationPatch(status="declined"),
            SimpleNamespace(role=Role.FIELD_REP),
            SimpleNamespace(),
        )

    assert exc_info.value.status_code == 403
