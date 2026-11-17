# ruff: noqa: B008
"""The online forms a requirement can collect, for the desk's editors.

Read-only: the program editor and the Add a requirement drawer list these kinds,
their consents and what a signature can also count as. The registry itself is
code (app/services/form_kinds.py); turning a form on for a program or a file is
configuration on the requirement row.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import require_role
from app.enums import Role
from app.models.user import User
from app.services import form_kinds

router = APIRouter(prefix="/form-kinds", tags=["form-kinds"])


@router.get("")
async def list_form_kinds(
    _: User = Depends(require_role(Role.SUPER_ADMIN, Role.LOAN_EXEC)),
) -> dict:
    return form_kinds.catalog()
