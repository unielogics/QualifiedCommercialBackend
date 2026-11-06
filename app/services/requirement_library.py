"""The requirements a desk can add to one file, and how the engine sees them.

A file's checklist came only from its evidence policy and its selected programs.
When the underwriter needs one more document, or one of the online forms, for
one file, there was nowhere to put it that the readiness engine would keep: a
requirement it does not know is marked stale and dropped. This module is that
place.

**The library** is every document and form requirement the firm already defines:
the published funding playbooks (loan products and evidence policies), plus the
code-defined baseline vocabulary (`funding_program_baselines`) for the items no
published playbook carries yet (the P&L and balance sheet, use of proceeds, ...).
Agent CRM playbooks (buyer, seller, cadence, ad hoc) are not funding evidence and
never appear.

**Adding** pins one library row to the file (`application_profiles.
extra_requirements`) and the engine merges it exactly as it merges an evidence
policy's requirements, under the policy key `staff_added`: it counts toward every
selected program's completion, is requestable, is collected, reviewed by the AI
and emailed like the initial checklist. A baseline item that no playbook holds is
first written into a platform playbook the desk owns ("Requirements added by
staff"), and a custom item is written there too, so both join the drop-down for
every file afterwards. The engine always works on a transient copy of the pinned
row, with the file's own required level on top; it never mutates the library row.

A key the file already requires through its policy or a selected program is not
added a second time, and the baseline or program definition wins the merge.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import class_mapper

from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate

log = logging.getLogger(__name__)

#: The policy key a staff-added requirement carries on its state (source_policy_keys).
STAFF_POLICY_KEY = "staff_added"
#: The platform playbook that holds requirements the desk created or pulled from the baseline.
STAFF_PLAYBOOK_PRODUCT_KEY = "staff_custom"
STAFF_PLAYBOOK_NAME = "Requirements added by staff"

#: Funding evidence only. Buyer, seller, cadence and ad hoc playbooks belong to the agent CRM.
LIBRARY_PLAYBOOK_TYPES = ("loan_product", "evidence_policy")
LIBRARY_KINDS = ("document", "form")
#: What a client hands over is never one of these: outreach drafts, calendar items, AI working state.
EXCLUDED_CATEGORIES = frozenset({"communication", "scheduling", "ai_internal"})
LEVELS = ("required", "recommended", "optional")
MAX_ITEMS = 400

#: The categories the drop-down groups by, in the order a desk reaches for them.
CATEGORY_LABELS: dict[str, str] = {
    "financials": "Financials",
    "borrower_info": "Borrower information",
    "credit": "Credit",
    "compliance": "Compliance",
    "agreements": "Agreements",
    "property_data": "Property",
    "insurance": "Insurance",
    "title_and_escrow": "Title and escrow",
    "appraisal_and_inspection": "Appraisal and inspection",
}
CATEGORY_ORDER = list(CATEGORY_LABELS)

#: The online forms a requirement can be filled in through, by requirement key.
FORM_KINDS_BY_KEY: dict[str, tuple[str, ...]] = {
    "owner_personal_financial_statement": ("pfs",),
    "business_debt_schedule": ("debt_schedule",),
    "refinance_debt_schedule": ("debt_schedule",),
    "ytd_p_and_l_balance_sheet": ("p_and_l", "balance_sheet"),
    "ytd_profit_and_loss": ("p_and_l",),
    "real_estate_schedule": ("real_estate_schedule",),
}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " ").capitalize())


def form_kinds_for(key: str, canonical_key: str | None = None) -> tuple[str, ...]:
    return FORM_KINDS_BY_KEY.get(canonical_key or "") or FORM_KINDS_BY_KEY.get(key, ())


# ---------------------------------------------------------------------------
# What a library row is, and which row stands for a key.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryCandidate:
    key: str
    label: str
    category: str
    kind: str
    required_level: str
    description: str
    canonical_key: str | None
    client_visible: bool
    verification_required: bool
    #: "library" (a published playbook row), "baseline" (code-defined, or written
    #: to the staff playbook from the baseline), "custom" (typed by the desk).
    source: str
    #: The row to pin; None for a baseline item no playbook holds yet.
    requirement_id: uuid.UUID | None
    form_kinds: tuple[str, ...] = ()


def _visible_to_a_person(visibility: Any) -> bool:
    return bool({"borrower", "client", "underwriter"}.intersection(set(visibility or [])))


def _client_visible(visibility: Any) -> bool:
    return bool({"borrower", "client"}.intersection(set(visibility or [])))


def _description(objective: str | None, criteria: str | None) -> str:
    text = (objective or "").strip() or (criteria or "").strip()
    return text[:240]


def rank_playbook(playbook: Any) -> tuple:
    """Which playbook's definition of a key wins: the funding team's over the
    platform's, an evidence policy over a loan product, then the newest."""
    published = getattr(playbook, "published_at", None) or getattr(playbook, "created_at", None)
    stamp = published.timestamp() if hasattr(published, "timestamp") else 0.0
    return (
        1 if getattr(playbook, "owner_type", "") == "funding" else 0,
        1 if getattr(playbook, "playbook_type", "") == "evidence_policy" else 0,
        int(getattr(playbook, "version", 0) or 0),
        stamp,
        str(getattr(playbook, "id", "")),
    )


def source_of(requirement: Any, playbook: Any) -> str:
    if str(requirement.requirement_key).startswith("custom_"):
        return "custom"
    if getattr(playbook, "product_key", None) == STAFF_PLAYBOOK_PRODUCT_KEY:
        return "baseline"
    return "library"


def candidate_from_row(requirement: Any, playbook: Any) -> LibraryCandidate:
    return LibraryCandidate(
        key=requirement.requirement_key,
        label=requirement.label,
        category=requirement.category,
        kind=requirement.requirement_kind or "document",
        required_level=requirement.required_level,
        description=_description(requirement.objective_text, requirement.completion_criteria),
        canonical_key=requirement.canonical_key,
        client_visible=_client_visible(requirement.visibility),
        verification_required=bool(
            requirement.verification_required or requirement.completion_mode == "requires_human_verify"
        ),
        source=source_of(requirement, playbook),
        requirement_id=requirement.id,
        form_kinds=form_kinds_for(requirement.requirement_key, requirement.canonical_key),
    )


def best_rows(pairs: list[tuple[Any, Any]]) -> dict[str, tuple[Any, Any]]:
    """One (requirement, playbook) per key: the best-ranked playbook's row."""
    best: dict[str, tuple[Any, Any]] = {}
    for requirement, playbook in pairs:
        if requirement.category in EXCLUDED_CATEGORIES:
            continue
        if (requirement.requirement_kind or "document") not in LIBRARY_KINDS:
            continue
        if not _visible_to_a_person(requirement.visibility):
            continue
        current = best.get(requirement.requirement_key)
        if current is None or rank_playbook(playbook) > rank_playbook(current[1]):
            best[requirement.requirement_key] = (requirement, playbook)
    return best


def baseline_definitions() -> dict[str, dict[str, Any]]:
    """The code-defined baseline documents by key, first definition winning."""
    from app.services import funding_program_baselines

    found: dict[str, dict[str, Any]] = {}
    for baseline in funding_program_baselines.CATALOG.values():
        for item in baseline.get("docs") or []:
            key = item.get("requirement_key")
            if key and key not in found and item.get("category") not in EXCLUDED_CATEGORIES:
                found[key] = item
    return found


def candidate_from_baseline(item: dict[str, Any]) -> LibraryCandidate:
    key = item["requirement_key"]
    return LibraryCandidate(
        key=key,
        label=item["label"],
        category=item["category"],
        kind="document",
        required_level=item.get("required_level", "required"),
        description=_description(item.get("objective_text"), item.get("completion_criteria")),
        canonical_key=None,
        client_visible=_client_visible(item.get("visibility")),
        verification_required=bool(item.get("verification_required", True)),
        source="baseline",
        requirement_id=None,
        form_kinds=form_kinds_for(key),
    )


async def _published_pairs(db: AsyncSession) -> list[tuple[AICollectionRequirement, AIPlaybookTemplate]]:
    rows = await db.execute(
        select(AICollectionRequirement, AIPlaybookTemplate)
        .join(AIPlaybookTemplate, AIPlaybookTemplate.id == AICollectionRequirement.playbook_id)
        .where(
            AIPlaybookTemplate.status == "published",
            AIPlaybookTemplate.is_active.is_(True),
            AIPlaybookTemplate.playbook_type.in_(LIBRARY_PLAYBOOK_TYPES),
            AICollectionRequirement.requirement_kind.in_(LIBRARY_KINDS),
            AICollectionRequirement.category.not_in(EXCLUDED_CATEGORIES),
        )
    )
    return [(row[0], row[1]) for row in rows.all()]


async def library_candidates(db: AsyncSession) -> list[LibraryCandidate]:
    """Every addable requirement, one per key, grouped the way the drop-down is."""
    best = best_rows(await _published_pairs(db))
    items = {key: candidate_from_row(req, playbook) for key, (req, playbook) in best.items()}
    for key, definition in baseline_definitions().items():
        if key not in items:
            items[key] = candidate_from_baseline(definition)
    order = {category: index for index, category in enumerate(CATEGORY_ORDER)}
    ranked = sorted(
        items.values(),
        key=lambda item: (order.get(item.category, len(order)), item.label.casefold(), item.key),
    )
    return ranked[:MAX_ITEMS]


# ---------------------------------------------------------------------------
# A file's added requirements.
# ---------------------------------------------------------------------------


def clean_entries(raw: Any) -> list[dict[str, Any]]:
    """The profile's `extra_requirements`, minus anything malformed."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append({**item, "key": key})
    return entries


def find_entry(profile: Any, key: str) -> dict[str, Any] | None:
    return next((item for item in clean_entries(getattr(profile, "extra_requirements", None)) if item["key"] == key), None)


def transient_copy(row: Any, *, required_level: str | None = None) -> AICollectionRequirement:
    """The definition the engine works on: every column of the library row, the
    file's own required level on top, and no connection to the session. The
    library row itself is never touched."""
    values = {attribute.key: getattr(row, attribute.key) for attribute in class_mapper(AICollectionRequirement).column_attrs}
    if required_level in LEVELS:
        values["required_level"] = required_level
    return AICollectionRequirement(**values)


@dataclass(frozen=True)
class ExtraDefinition:
    requirement: AICollectionRequirement
    #: The source playbook's version, for the evidence decisions' criteria version.
    version: int
    entry: dict[str, Any]


async def extra_definitions(db: AsyncSession, profile: Any) -> list[ExtraDefinition]:
    """The file's added requirements as definitions the engine can merge."""
    entries = clean_entries(getattr(profile, "extra_requirements", None))
    if not entries:
        return []
    ids: list[uuid.UUID] = []
    for entry in entries:
        try:
            ids.append(uuid.UUID(str(entry.get("source_requirement_id"))))
        except ValueError:
            continue
    found: dict[uuid.UUID, tuple[AICollectionRequirement, int]] = {}
    if ids:
        rows = await db.execute(
            select(AICollectionRequirement, AIPlaybookTemplate.version)
            .join(AIPlaybookTemplate, AIPlaybookTemplate.id == AICollectionRequirement.playbook_id)
            .where(AICollectionRequirement.id.in_(ids))
        )
        for requirement, version in rows.all():
            found[requirement.id] = (requirement, int(version or 1))
    definitions: list[ExtraDefinition] = []
    for entry in entries:
        try:
            wanted = uuid.UUID(str(entry.get("source_requirement_id")))
        except ValueError:
            wanted = None
        match = found.get(wanted) if wanted else None
        if match is None:
            log.warning("added requirement %s has no library row any more; it is skipped", entry.get("key"))
            continue
        requirement, version = match
        definitions.append(
            ExtraDefinition(
                requirement=transient_copy(requirement, required_level=entry.get("required_level")),
                version=version,
                entry=entry,
            )
        )
    return definitions


def provenance_note(entry: dict[str, Any], form_kinds: tuple[str, ...] = ()) -> dict[str, Any]:
    """What the requirement's state records about who added it, for the desk's row."""
    return {
        "added_by": entry.get("added_by"),
        "added_by_name": entry.get("added_by_name"),
        "added_at": entry.get("added_at"),
        "note": entry.get("note"),
        "custom": bool(entry.get("custom")),
        "form_kinds": list(form_kinds),
    }


# ---------------------------------------------------------------------------
# Adding and removing.
# ---------------------------------------------------------------------------


def slugify(label: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (label or "").casefold()).strip("_")
    return text[:56] or "requirement"


def next_free_key(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


async def ensure_staff_playbook(db: AsyncSession) -> AIPlaybookTemplate:
    playbook = (
        await db.execute(
            select(AIPlaybookTemplate)
            .where(
                AIPlaybookTemplate.owner_type == "platform",
                AIPlaybookTemplate.playbook_type == "evidence_policy",
                AIPlaybookTemplate.product_key == STAFF_PLAYBOOK_PRODUCT_KEY,
                AIPlaybookTemplate.status == "published",
                AIPlaybookTemplate.is_active.is_(True),
            )
            .order_by(AIPlaybookTemplate.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if playbook is not None:
        return playbook
    playbook = AIPlaybookTemplate(
        owner_type="platform",
        owner_id=None,
        playbook_type="evidence_policy",
        product_key=STAFF_PLAYBOOK_PRODUCT_KEY,
        name=STAFF_PLAYBOOK_NAME,
        description=(
            "Requirements the desk added to a file that no program or evidence policy carries: "
            "custom items and baseline items pulled into the library. Never a file's own policy."
        ),
        rules={},
        version=1,
        status="published",
        published_at=datetime.now(UTC),
        is_active=True,
    )
    db.add(playbook)
    await db.flush()
    return playbook


async def _existing_keys(db: AsyncSession, like: str) -> set[str]:
    rows = await db.execute(
        select(AICollectionRequirement.requirement_key).where(AICollectionRequirement.requirement_key.like(f"{like}%"))
    )
    return {row[0] for row in rows.all()}


async def create_custom_row(
    db: AsyncSession, *, label: str, category: str, instructions: str | None
) -> AICollectionRequirement:
    """A requirement the desk typed, written into the staff playbook so it joins
    the drop-down for every file."""
    playbook = await ensure_staff_playbook(db)
    base = f"custom_{slugify(label)}"
    key = next_free_key(base, await _existing_keys(db, base))
    text = (instructions or "").strip()
    row = AICollectionRequirement(
        playbook_id=playbook.id,
        requirement_key=key,
        requirement_kind="document",
        label=label.strip(),
        category=category,
        required_level="required",
        visibility=["borrower", "underwriter"],
        blocks_stage="underwriting",
        verification_required=True,
        can_underwriter_waive=True,
        completion_mode="requires_human_verify",
        display_order=0,
        objective_text=f"Collect {label.strip()}.",
        completion_criteria=text or "Confirm the document is complete, current and for the right entity.",
        ai_request_message_template=(
            f"Please provide {label.strip()}." + (f" {text}" if text else "")
        ),
        review_checks=[],
        collection_config={},
    )
    db.add(row)
    await db.flush()
    return row


async def materialize_baseline_row(db: AsyncSession, definition: dict[str, Any]) -> AICollectionRequirement:
    """A baseline document no published playbook carries, written into the staff
    playbook (once) so it can be pinned like any other row."""
    playbook = await ensure_staff_playbook(db)
    existing = (
        await db.execute(
            select(AICollectionRequirement).where(
                AICollectionRequirement.playbook_id == playbook.id,
                AICollectionRequirement.requirement_key == definition["requirement_key"],
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = AICollectionRequirement(
        playbook_id=playbook.id,
        requirement_key=definition["requirement_key"],
        requirement_kind="document",
        label=definition["label"],
        category=definition["category"],
        required_level=definition.get("required_level", "required"),
        visibility=list(definition.get("visibility") or ["borrower", "underwriter"]),
        blocks_stage=definition.get("blocks_stage"),
        verification_required=bool(definition.get("verification_required", True)),
        can_underwriter_waive=bool(definition.get("can_underwriter_waive", True)),
        completion_mode=definition.get("completion_mode", "requires_human_verify"),
        display_order=int(definition.get("display_order", 0) or 0),
        objective_text=definition.get("objective_text") or "",
        completion_criteria=definition.get("completion_criteria") or "",
        review_checks=list(definition.get("review_checks") or []),
        collection_config={},
    )
    db.add(row)
    await db.flush()
    return row


async def best_row_for_key(db: AsyncSession, key: str) -> tuple[Any, Any] | None:
    rows = await db.execute(
        select(AICollectionRequirement, AIPlaybookTemplate)
        .join(AIPlaybookTemplate, AIPlaybookTemplate.id == AICollectionRequirement.playbook_id)
        .where(
            AICollectionRequirement.requirement_key == key,
            AIPlaybookTemplate.status == "published",
            AIPlaybookTemplate.is_active.is_(True),
            AIPlaybookTemplate.playbook_type.in_(LIBRARY_PLAYBOOK_TYPES),
        )
    )
    return best_rows([(row[0], row[1]) for row in rows.all()]).get(key)


@dataclass
class AddPlan:
    entry: dict[str, Any]
    label: str
    #: True when the key was already added to this file; nothing is written.
    already: bool = False


def build_entry(
    *,
    key: str,
    requirement_id: uuid.UUID,
    required_level: str,
    note: str | None,
    user_id: Any,
    custom: bool,
    user_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "source_requirement_id": str(requirement_id),
        "required_level": required_level,
        "note": (note or "").strip() or None,
        "added_by": str(user_id) if user_id else None,
        "added_by_name": (user_name or "").strip() or None,
        "added_at": (now or datetime.now(UTC)).isoformat(),
        "custom": custom,
    }


async def plan_add(
    db: AsyncSession,
    profile: Any,
    *,
    requirement_key: str | None,
    custom: dict[str, Any] | None,
    required_level: str | None,
    note: str | None,
    user_id: Any,
    on_file: dict[str, dict[str, list[str]]],
    user_name: str | None = None,
) -> AddPlan:
    """Decide what adding this requirement means, writing a library row when the
    item is a custom one or a baseline document no playbook holds. Raises 404 for
    an unknown key and 409 when the file already requires it another way."""
    if bool(requirement_key) == bool(custom):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose one library item or describe a custom one")
    if required_level is not None and required_level not in LEVELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown required level")

    if custom:
        row = await create_custom_row(
            db, label=custom["label"], category=custom["category"], instructions=custom.get("instructions")
        )
        level = required_level or row.required_level
        return AddPlan(
            entry=build_entry(key=row.requirement_key, requirement_id=row.id, required_level=level, note=note, user_id=user_id, custom=True, user_name=user_name),
            label=row.label,
        )

    key = str(requirement_key).strip()
    from app.services.requirement_lifecycle import debt_identity
    shared_key = debt_identity(key)
    existing = find_entry(profile, key)
    if existing is None and shared_key:
        existing = next((entry for entry in clean_entries(profile.extra_requirements) if debt_identity(entry["key"]) == shared_key), None)
    if existing is not None:
        return AddPlan(entry=existing, label=key, already=True)
    sources = on_file.get(shared_key or key)
    if sources and (sources.get("program_keys") or [item for item in sources.get("policy_keys", []) if item != STAFF_POLICY_KEY]):
        raise HTTPException(status.HTTP_409_CONFLICT, "This file already requires that item")

    found = await best_row_for_key(db, key)
    if found is not None:
        row = found[0]
    else:
        definition = baseline_definitions().get(key)
        if definition is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That requirement is not in the library")
        row = await materialize_baseline_row(db, definition)
    level = required_level or row.required_level
    return AddPlan(
        entry=build_entry(key=row.requirement_key, requirement_id=row.id, required_level=level, note=note, user_id=user_id, custom=False, user_name=user_name),
        label=row.label,
    )


def with_entry(profile: Any, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The profile's entries with this one appended (a new list, so the JSONB change is seen)."""
    return [*clean_entries(getattr(profile, "extra_requirements", None)), entry]


def without_entry(profile: Any, key: str) -> list[dict[str, Any]]:
    return [item for item in clean_entries(getattr(profile, "extra_requirements", None)) if item["key"] != key]
