"""Review drafts for unpublished funding programs.

Ten catalog programs are published; the rest route nothing because they have
no approved criteria version. This module generates ONE draft version per
active, unpublished, non-ARF program so the desk can review and publish it
from the funding programs editor.

Rules of the road:

* Never publishes. ``publish_version`` is not called from here.
* Never touches a draft a person authored: any program whose latest draft
  already carries requirement rows is skipped.
* Rules come from the latest legacy-imported draft when it has a ``fit``
  block, otherwise from the 2026-09-27 site baseline. Requirements always
  come from the baseline, with the program's existing scopes passed through.
* Invalid rules or baseline requirements are reported in the plan, never
  raised to the caller.
* ``arf_*`` programs are hand-authored and protected.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.funding_program import FundingProgramCatalog, FundingProgramScope
from app.models.user import User
from app.schemas.funding_program import FundingProgramScopeWrite, FundingProgramVersionCreate
from app.services import funding_programs
from app.services.funding_program_baselines import suggested_baseline

log = logging.getLogger(__name__)

PROTECTED_PREFIXES: tuple[str, ...] = ("arf_",)
REVIEW_DRAFT_DESCRIPTION = (
    "Generated for review from the legacy deterministic rules and the 2026-09-27 site "
    "baseline. Not published — publish from the funding programs editor after review."
)
REVIEW_DRAFT_REASON = "automated review draft"
RULES_SOURCE_LEGACY = "legacy_draft"
RULES_SOURCE_BASELINE = "baseline"
_NAME_MAX_LENGTH = 160


def review_draft_name(program_name: str) -> str:
    """``"{name} review draft"`` bounded to the version name column."""
    suffix = " review draft"
    base = (program_name or "").strip()
    room = _NAME_MAX_LENGTH - len(suffix)
    return f"{base[:room].rstrip()}{suffix}"


@dataclass
class ReviewDraftPlan:
    """What ``ensure_review_drafts`` will do for one catalog program."""

    program_key: str
    program_name: str
    action: str  # "create" | "skip"
    reason: str | None = None
    rules_source: str | None = None
    requirement_count: int = 0
    payload: FundingProgramVersionCreate | None = None
    program: FundingProgramCatalog | None = None

    @property
    def will_create(self) -> bool:
        return self.action == "create" and self.payload is not None and self.program is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "program_key": self.program_key,
            "program_name": self.program_name,
            "action": self.action,
            "reason": self.reason,
            "rules_source": self.rules_source,
            "requirement_count": self.requirement_count,
        }


def is_protected_program(program_key: str) -> bool:
    return any(program_key.startswith(prefix) for prefix in PROTECTED_PREFIXES)


async def latest_drafts_by_program(
    db: AsyncSession, program_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AIPlaybookTemplate]:
    """Highest-version ``draft`` criteria row per program, if any."""
    if not program_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(AIPlaybookTemplate)
                .where(
                    AIPlaybookTemplate.playbook_type == "loan_product",
                    AIPlaybookTemplate.status == "draft",
                    AIPlaybookTemplate.funding_program_id.in_(program_ids),
                )
                .order_by(
                    AIPlaybookTemplate.funding_program_id,
                    AIPlaybookTemplate.version.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    latest: dict[uuid.UUID, AIPlaybookTemplate] = {}
    for row in rows:
        if row.funding_program_id is None:
            continue
        current = latest.get(row.funding_program_id)
        if current is None or int(row.version or 0) > int(current.version or 0):
            latest[row.funding_program_id] = row
    return latest


async def requirement_counts(
    db: AsyncSession, playbook_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not playbook_ids:
        return {}
    rows = (
        await db.execute(
            select(AICollectionRequirement.playbook_id, func.count(AICollectionRequirement.id))
            .where(AICollectionRequirement.playbook_id.in_(playbook_ids))
            .group_by(AICollectionRequirement.playbook_id)
        )
    ).all()
    counts: dict[uuid.UUID, int] = defaultdict(int)
    for playbook_id, count in rows:
        counts[playbook_id] = int(count or 0)
    return counts


def scope_dicts(rows: list[FundingProgramScope]) -> list[dict[str, Any]]:
    """Existing scope rows as the plain dicts ``suggested_baseline`` accepts.

    Mirrors the baseline router. A legacy row that no longer passes the write
    schema is passed through field by field instead of aborting the plan.
    """
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            result.append(FundingProgramScopeWrite.model_validate(row, from_attributes=True).model_dump())
        except ValidationError:
            result.append(
                {
                    "vertical": getattr(row, "vertical", None),
                    "scope_key": getattr(row, "scope_key", "default"),
                    "intake_variants": list(getattr(row, "intake_variants", None) or []),
                    "intent_keys": list(getattr(row, "intent_keys", None) or []),
                    "naics_prefixes": list(getattr(row, "naics_prefixes", None) or []),
                    "excluded_naics_prefixes": list(
                        getattr(row, "excluded_naics_prefixes", None) or []
                    ),
                    "industry_keys": list(getattr(row, "industry_keys", None) or []),
                    "required_fact_keys": list(getattr(row, "required_fact_keys", None) or []),
                }
            )
    return result


def _http_detail_text(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        first = detail[0]
        loc = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        return f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))
    return str(detail)


def _validation_text(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", []))
    return f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))


def build_review_draft_payload(
    program: FundingProgramCatalog,
    *,
    legacy_draft: AIPlaybookTemplate | None,
    scopes: list[FundingProgramScope],
) -> tuple[FundingProgramVersionCreate | None, str, str | None]:
    """Return ``(payload, rules_source, error)`` for one program.

    ``payload`` is ``None`` when the chosen rules or the baseline requirements
    do not validate; ``error`` then says why so the operator can fix the
    source rather than the script crashing mid-run.
    """
    baseline = suggested_baseline(
        program.program_key,
        name=program.name,
        current_scopes=scope_dicts(scopes),
    )
    legacy_rules = dict(getattr(legacy_draft, "rules", None) or {}) if legacy_draft else {}
    if legacy_rules.get("fit"):
        rules = deepcopy(legacy_rules)
        rules_source = RULES_SOURCE_LEGACY
    else:
        rules = deepcopy(baseline.get("rules") or {})
        rules_source = RULES_SOURCE_BASELINE

    try:
        funding_programs._validate_program_rules_or_422(rules)
    except HTTPException as exc:
        return None, rules_source, f"{rules_source} rules failed validation: {_http_detail_text(exc)}"

    try:
        payload = FundingProgramVersionCreate(
            name=review_draft_name(program.name),
            description=REVIEW_DRAFT_DESCRIPTION,
            rules=rules,
            requirements=deepcopy(baseline.get("requirements") or []),
            reason=REVIEW_DRAFT_REASON,
            confirmed=True,
        )
    except ValidationError as exc:
        return None, rules_source, f"baseline requirements failed validation: {_validation_text(exc)}"
    return payload, rules_source, None


async def plan_review_drafts(
    db: AsyncSession,
    *,
    program_keys: list[str] | None = None,
) -> list[ReviewDraftPlan]:
    """Read-only plan: one entry per considered program, nothing written."""
    wanted = (
        list(dict.fromkeys(key.strip() for key in program_keys if key and key.strip()))
        if program_keys
        else None
    )
    catalog = await funding_programs.catalog_rows(db)
    if wanted is not None:
        by_key = {row.program_key: row for row in catalog}
        rows = [by_key[key] for key in wanted if key in by_key]
        unknown = [key for key in wanted if key not in by_key]
    else:
        rows = list(catalog)
        unknown = []

    program_ids = [row.id for row in rows]
    published = await funding_programs.published_versions_by_program(db, program_ids)
    scopes = await funding_programs.scopes_by_program(db, program_ids)
    drafts = await latest_drafts_by_program(db, program_ids)
    counts = await requirement_counts(db, [draft.id for draft in drafts.values()])

    plan: list[ReviewDraftPlan] = []
    for program in rows:
        entry = ReviewDraftPlan(
            program_key=program.program_key,
            program_name=program.name,
            action="skip",
            program=program,
        )
        plan.append(entry)
        if program.id in published:
            entry.reason = f"published version {published[program.id].version} exists"
            continue
        if is_protected_program(program.program_key):
            entry.reason = "protected hand-authored program (arf_*)"
            continue
        draft = drafts.get(program.id)
        existing = counts.get(draft.id, 0) if draft is not None else 0
        if draft is not None and existing > 0:
            entry.reason = (
                f"latest draft version {draft.version} already has {existing} requirement "
                "row(s); a person authored it"
            )
            continue
        payload, rules_source, error = build_review_draft_payload(
            program, legacy_draft=draft, scopes=scopes.get(program.id, [])
        )
        entry.rules_source = rules_source
        if payload is None:
            entry.reason = error
            continue
        entry.action = "create"
        entry.payload = payload
        entry.requirement_count = len(payload.requirements)
    for key in unknown:
        plan.append(
            ReviewDraftPlan(
                program_key=key,
                program_name=key,
                action="skip",
                reason="not an active catalog program",
            )
        )
    return plan


async def ensure_review_drafts(
    db: AsyncSession,
    *,
    user: User,
    program_keys: list[str] | None = None,
    plan: list[ReviewDraftPlan] | None = None,
) -> list[dict[str, Any]]:
    """Create one review draft per eligible unpublished program.

    Returns ``[{program_key, version, rules_source, requirement_count}]`` for
    the drafts created. Programs skipped or failing validation are reported in
    the plan (``plan_review_drafts``) and logged; they never raise. Nothing is
    published and nothing is committed here; the caller owns the transaction.
    """
    entries = plan if plan is not None else await plan_review_drafts(db, program_keys=program_keys)
    created: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.will_create:
            if entry.rules_source is not None and entry.reason:
                log.warning(
                    "review draft skipped for %s: %s", entry.program_key, entry.reason
                )
            continue
        assert entry.payload is not None and entry.program is not None
        try:
            playbook = await funding_programs.create_version(db, entry.program, entry.payload, user)
        except (HTTPException, ValidationError) as exc:
            text = _http_detail_text(exc) if isinstance(exc, HTTPException) else _validation_text(exc)
            entry.action = "skip"
            entry.reason = f"create_version rejected the draft: {text}"
            log.warning("review draft rejected for %s: %s", entry.program_key, text)
            continue
        created.append(
            {
                "program_key": entry.program_key,
                "version": int(playbook.version),
                "rules_source": entry.rules_source,
                "requirement_count": len(entry.payload.requirements),
            }
        )
    return created
