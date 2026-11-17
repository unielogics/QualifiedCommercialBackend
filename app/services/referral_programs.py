"""Partner-safe view of the active program catalog and intake routing."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import funding_programs

VARIANT_KEYS = {
    "dealer_gatekeeper_v1": "dealer",
    "real_estate_dscr_v1": "real_estate",
    "main_street_v1": "main_street",
    "mca_refi_v1": "mca_refinance",
    "commercial_foreclosure_bailout_v1": "foreclosure_rescue",
}
VERTICAL_KEYS = {"dealer": "dealer", "real_estate": "real_estate", "main_street": "main_street", "mca": "mca_refinance"}


class ReferralProgram(BaseModel):
    program_key: str
    name: str
    short_description: str | None = None
    variants: list[str] = Field(default_factory=list)
    intent_keys_by_variant: dict[str, list[str]] = Field(default_factory=dict)


async def active_catalog(db: AsyncSession) -> list[ReferralProgram]:
    rows = await funding_programs.catalog_rows(db)
    scopes = await funding_programs.scopes_by_program(db, [row.id for row in rows])
    result = []
    for row in rows:
        # Counterparty-specific channels require a separate hosted entitlement.
        # Expose their public business-capital projection instead.
        if row.program_key.startswith("arf_"):
            continue
        variants = []
        intents: dict[str, list[str]] = {}
        for scope in scopes.get(row.id, []):
            if not scope.is_active:
                continue
            routes = [VARIANT_KEYS[key] for key in scope.intake_variants if key in VARIANT_KEYS]
            if not scope.intake_variants and scope.vertical in VERTICAL_KEYS:
                routes = [VERTICAL_KEYS[scope.vertical]]
            if row.program_key == "foreclosure_bailout" and "real_estate" in routes:
                routes = ["foreclosure_rescue"]
            for route in routes:
                intents[route] = list(dict.fromkeys([*intents.get(route, []), *scope.intent_keys]))
            variants.extend(routes)
        result.append(ReferralProgram(
            program_key=row.program_key,
            name=row.name,
            short_description=row.short_description,
            variants=list(dict.fromkeys(variants)),
            intent_keys_by_variant=intents,
        ))
    return result


async def validate_request(db: AsyncSession, program_key: str, variant: str) -> ReferralProgram:
    program = next((row for row in await active_catalog(db) if row.program_key == program_key), None)
    if program is None:
        raise HTTPException(422, "Choose an active funding program")
    if variant not in program.variants:
        raise HTTPException(422, "This application type is not active for the selected program")
    return program
