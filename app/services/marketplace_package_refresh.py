"""Saved-only immutable package preparation; publication remains a guarded caller action."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import inspect, select

from app.capital_partners import enums as E
from app.capital_partners.services import storage
from app.models.public_underwriting_intake import PublicUnderwritingIntakeArtifact
from app.services.financial_workspace import existing_family

SAVED_OPTIONS = {"include_operating_plan": True, "include_working_operating_plan": False}
KINDS = (("executive_summary", E.DocumentKind.EXECUTIVE_SUMMARY, "Executive summary"),
         ("lender_packet", E.DocumentKind.UNDERWRITING_PACKAGE, "Underwriting package"),
         ("investor_presentation", E.DocumentKind.INVESTOR_PRESENTATION, "Investor presentation"))


async def lock_source(db, profile) -> None:
    """Match manual package generation: intake, then bucket/profile, then listing."""
    from app.routers import dealer_ai_intake as core

    if not getattr(profile, "intake_id", None):
        raise HTTPException(409, "This file has no underwriting intake for package PDFs.")
    await db.execute(select(core.PublicUnderwritingIntake.id)
                     .where(core.PublicUnderwritingIntake.id == profile.intake_id).with_for_update())


async def _inputs(db, profile):
    from app.routers import dealer_ai_intake as core

    if not getattr(profile, "intake_id", None):
        raise HTTPException(409, "This file has no underwriting intake for package PDFs.")
    if inspect(profile, raiseerr=False) is not None:
        await db.refresh(profile)
    family = await existing_family(db, profile.id, "operating_plan")
    if family is not None and inspect(family, raiseerr=False) is not None:
        await db.refresh(family)
    intake = await core._load_admin_dealer_lead(db, profile.intake_id, refresh=True)
    values = await core._package_generation_inputs(db, intake, dict(SAVED_OPTIONS))
    financials = values[3]
    if financials.get("package_options") != SAVED_OPTIONS:
        raise HTTPException(409, "The package must use the saved operating-plan version.")
    # The legacy input collector can omit advisory context after an error. An
    # existing saved plan must never silently disappear from an automatic PDF.
    if family is not None and family.current_version_id is not None:
        snapshot = ((financials.get("operating_earnings") or {}).get("improvement_plan") or {}).get("snapshot") or {}
        if not snapshot.get("version_number") or snapshot.get("state") == "working_draft":
            raise HTTPException(409, "The saved operating-plan version could not be included. Retry the package refresh.")
    return intake, values


async def current_input_fingerprint(db, profile) -> str:
    _intake, values = await _inputs(db, profile)
    return values[4]


def _summary(intake, financials):
    """Fresh factual wording, without carrying an earlier AI narrative forward."""
    name = intake.business_name or intake.full_name or "Business"
    readiness = financials.get("package_readiness") or {}
    missing = [row.get("label") for row in readiness.get("requirements") or []
               if row.get("effectively_open") and row.get("label")]
    return {"title": f"{name} financing summary", "executive_summary":
            "Financing summary prepared from the latest saved financials and loan terms. "
            "Historical figures and proposed operating changes are presented separately. "
            "The operating plan describes conditional changes for implementation with underwriting over the coming months; "
            "projected results depend on execution and do not establish completed financial results or funding approval.",
            "recommended_approach": "Review the saved financing structure, evidence conditions and proposed operating actions together.",
            "borrower_profile": name, "key_metrics": [], "documents_reviewed": [],
            "missing_confirmations": missing, "risks": ["Proposed operating results remain conditional on implementation and review."],
            "next_best_action": "Review the current figures, source evidence and offering documents before committing funds.",
            "disclaimer": "Illustrative underwriting information; the approved offering documents govern."}


async def _ready(db, intake, fingerprint, *, exclude=()):
    from app.routers import dealer_ai_intake as core

    found, generation = [], None
    for kind, public_kind, label in KINDS:
        query = select(PublicUnderwritingIntakeArtifact).where(
            PublicUnderwritingIntakeArtifact.intake_id == intake.id,
            PublicUnderwritingIntakeArtifact.artifact_type == kind,
        )
        if exclude:
            query = query.where(PublicUnderwritingIntakeArtifact.id.not_in(exclude))
        artifact = (await db.execute(query.order_by(PublicUnderwritingIntakeArtifact.created_at.desc(),
                                                  PublicUnderwritingIntakeArtifact.id.desc()).limit(1)
                                     .execution_options(populate_existing=True))).scalar_one_or_none()
        if artifact is None or not artifact.s3_key:
            return None
        pdf, package = core._artifact_pdf_metadata(artifact), core._artifact_package_metadata(artifact)
        if (pdf.get("package_input_sha256") != fingerprint or pdf.get("package_options") != SAVED_OPTIONS
                or package.get("status") != "current" or not package.get("generation_id")):
            return None
        if generation is not None and package["generation_id"] != generation:
            return None
        generation = package["generation_id"]
        head = await asyncio.to_thread(storage.head_object, artifact.s3_key)
        if not head or not head.get("size_bytes"):
            return None
        found.append((artifact, public_kind, label))
    return found


async def _actor(db, actor_id):
    from app.models.user import User

    if actor_id is None:
        return None
    user = await db.get(User, actor_id)
    if user is None:
        raise HTTPException(409, "The refresh initiator is no longer available. Start a new refresh.")
    return user


async def discard(prepared: dict[str, Any]) -> None:
    """Call only after a known rollback, never after an ambiguous commit."""
    from app.routers import dealer_ai_intake as core

    for key in prepared.get("created_s3_keys") or []:
        await asyncio.to_thread(core._delete_s3_object, key)


async def prepare(db, profile, *, actor_id=None) -> dict[str, Any]:
    from app.routers import dealer_ai_intake as core

    await lock_source(db, profile)
    intake, (files, metadata, context, financials, fingerprint) = await _inputs(db, profile)
    current = await _ready(db, intake, fingerprint)
    prepared = {"artifacts": current or [], "input_fingerprint": fingerprint, "reused": current is not None,
                "intake_id": intake.id, "profile": profile, "generation_id": uuid4(),
                "generated_at": datetime.now(UTC), "created_s3_keys": [], "published": current is not None}
    if current is not None:
        return prepared
    actor = await _actor(db, actor_id)
    try:
        # Failures roll back only rows created here. The caller owns the outer
        # job transaction and its durable lease/status, including its commit.
        async with db.begin_nested():
            summary = await core._create_executive_summary_artifact(db, intake, actor,
                financials=financials, context_snapshot=context, source_snapshot_metadata=metadata,
                cleanup_s3_keys=prepared["created_s3_keys"], prepared_summary=_summary(intake, financials))
            packet = await core._create_lender_packet_artifact(db, intake, actor, executive_summary=summary,
                files_snapshot=files, financials=financials, source_snapshot_metadata=metadata,
                cleanup_s3_keys=prepared["created_s3_keys"])
            presentation = await core._create_investor_presentation_artifact(db, intake, actor,
                financials=financials, source_snapshot_metadata=metadata,
                cleanup_s3_keys=prepared["created_s3_keys"], generated_at=prepared["generated_at"])
            prepared["artifacts"] = [(artifact, public_kind, label) for artifact, (_, public_kind, label)
                                     in zip((summary, packet, presentation), KINDS, strict=True)]
    except Exception:
        await discard(prepared)
        raise
    return prepared


async def publish_prepared(db, prepared, *, actor_id=None) -> dict[str, Any]:
    """Publish bucket versions only after the caller accepts the listing lease."""
    from app.routers import dealer_ai_intake as core

    if prepared.get("published"):
        return prepared
    # The worker already acquired this intake lock before bucket/profile and
    # listing locks; reacquiring it is a no-op, never an inverted lock order.
    await db.execute(select(core.PublicUnderwritingIntake.id)
                     .where(core.PublicUnderwritingIntake.id == prepared["intake_id"]).with_for_update())
    intake, values = await _inputs(db, prepared["profile"])
    if values[4] != prepared["input_fingerprint"]:
        raise HTTPException(409, "The saved file changed while its PDFs were prepared. Refresh the latest saved version.")
    own_ids = [artifact.id for artifact, _kind, _label in prepared["artifacts"]]
    current = await _ready(db, intake, prepared["input_fingerprint"], exclude=own_ids)
    if current is not None:
        # Another listing/explicit package run already prepared these exact
        # saved facts. Preserve its current version rather than duplicate it.
        for artifact, _kind, _label in prepared["artifacts"]:
            await db.delete(artifact)
        await discard(prepared)
        prepared.update(artifacts=current, reused=True, created_s3_keys=[], published=True)
        return prepared
    actor = await _actor(db, actor_id)
    files = []
    for artifact, _kind, _label in prepared["artifacts"]:
        previous = (await db.execute(select(PublicUnderwritingIntakeArtifact)
            .where(PublicUnderwritingIntakeArtifact.intake_id == intake.id,
                   PublicUnderwritingIntakeArtifact.artifact_type == artifact.artifact_type,
                   PublicUnderwritingIntakeArtifact.id != artifact.id)
            .order_by(PublicUnderwritingIntakeArtifact.created_at.desc()).limit(1))).scalar_one_or_none()
        file, _superseded = await core._publish_package_artifact_to_bucket(db, intake=intake, artifact=artifact,
            previous_artifact=previous, generation_id=prepared["generation_id"], generated_at=prepared["generated_at"], user=actor)
        files.append(file)
    prepared.update(bucket_files=files, published=True)
    return prepared
