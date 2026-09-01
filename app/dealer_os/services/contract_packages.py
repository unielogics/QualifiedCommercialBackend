"""Versioned program packages and one-signature multi-document envelopes.

The blank program application is immutable once uploaded.  A package freezes
the exact template version, populated source values and document hashes before
delivery.  One signature may execute every explicitly acknowledged document,
but each resulting PDF keeps its own signature, certificate and SHA-256.
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.models.user import User

from ..models import (
    ContractDocument,
    ContractEnvelope,
    ContractEnvelopeDocument,
    ContractPackage,
    ContractPackageItem,
    ContractTemplate,
    ContractTemplateVersion,
    DealerApplicationProfile,
    DealerBusiness,
    DealerOwner,
    DealerProgramRuleResolution,
)
from . import contract_fill, contract_sign, qc_master_application, storage

PROGRAM_APPLICATION_KEY = "qc_program_application"
PROGRAM_APPLICATION_TITLE = "Business Loan Application"
EZ_PROGRAM_KEY = "term_loan_3_5_year"
MICRO_PROGRAM_KEY = "term_loan_10_year"
SUPPORTED_PROGRAMS = frozenset({EZ_PROGRAM_KEY, MICRO_PROGRAM_KEY})
PROGRAM_ORDER = (EZ_PROGRAM_KEY, MICRO_PROGRAM_KEY)
PROGRAM_LABELS = {
    EZ_PROGRAM_KEY: "EZ Term",
    MICRO_PROGRAM_KEY: "MicroCap",
}
SOURCE_ASSET = Path(__file__).resolve().parents[1] / "templates" / "qc-program-application-v1.pdf"

# Normalized locations are retained with the immutable template version.  The
# renderer still locates labels by text where possible, while these anchors
# make signature/program placement explicit and auditable.
DEFAULT_OVERLAY_MAP: dict[str, Any] = {
    "coordinate_space": "normalized_top_left",
    "page": {"width": 612, "height": 792},
    "program": {"ez_term": [0.243, 0.127], "microcap": [0.433, 0.127]},
    "signature": [0.453, 0.955],
    "signature_date": [0.707, 0.955],
    "ssn_policy": "provider_only_notice",
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


async def ensure_defaults(db: AsyncSession, actor_user_id: UUID | None = None) -> None:
    """Ensure the bundled v1 paper has an immutable S3-backed version."""
    template = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == PROGRAM_APPLICATION_KEY))
    ).scalar_one_or_none()
    if template is None:
        template = ContractTemplate(
            key=PROGRAM_APPLICATION_KEY,
            title=PROGRAM_APPLICATION_TITLE,
            render_kind="uploaded_pdf",
            revision=1,
            active=True,
            has_acroform=False,
            field_names=[],
            field_map={},
        )
        db.add(template)
        await db.flush()

    version = (
        await db.execute(
            select(ContractTemplateVersion)
            .where(ContractTemplateVersion.template_id == template.id)
            .order_by(ContractTemplateVersion.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None:
        raw = SOURCE_ASSET.read_bytes()
        digest = _sha(raw)
        key = f"contract-templates/{PROGRAM_APPLICATION_KEY}/r1-{digest[:16]}.pdf"
        if not storage.put_bytes(key, raw, "application/pdf"):
            raise RuntimeError("The program application template could not be stored.")
        version = ContractTemplateVersion(
            template_id=template.id,
            revision=1,
            s3_key=key,
            sha256=digest,
            page_count=1,
            has_acroform=False,
            field_names=[],
            overlay_map=DEFAULT_OVERLAY_MAP,
            uploaded_by_user_id=actor_user_id,
            active=True,
        )
        db.add(version)
        template.s3_key = key
        template.page_count = 1
        template.revision = 1
        await db.flush()

    items = list(
        (
            await db.execute(
                select(ContractPackageItem).where(
                    ContractPackageItem.template_key == PROGRAM_APPLICATION_KEY,
                    ContractPackageItem.template_version_id.is_(None),
                )
            )
        ).scalars().all()
    )
    for item in items:
        item.template_version_id = version.id
    await db.flush()


async def create_template_version(
    db: AsyncSession,
    *,
    template_key: str,
    title: str,
    raw: bytes,
    actor_user_id: UUID,
    overlay_map: dict[str, Any] | None = None,
) -> ContractTemplateVersion:
    if not raw.startswith(b"%PDF"):
        raise ValueError("Upload a PDF template.")
    template = (
        await db.execute(select(ContractTemplate).where(ContractTemplate.key == template_key))
    ).scalar_one_or_none()
    if template is None:
        template = ContractTemplate(
            key=template_key,
            title=title,
            render_kind="uploaded_pdf",
            revision=1,
            active=True,
            has_acroform=False,
            field_names=[],
            field_map={},
        )
        db.add(template)
        await db.flush()
    elif template.render_kind != "uploaded_pdf":
        raise ValueError("Only uploaded-PDF templates can be versioned in Forms and Packages.")
    template.title = title

    import fitz

    source = fitz.open(stream=raw, filetype="pdf")
    if source.page_count < 1:
        raise ValueError("The PDF template has no pages.")
    page = source[0]
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    selected_overlay = overlay_map or (
        DEFAULT_OVERLAY_MAP
        if template_key == PROGRAM_APPLICATION_KEY
        else {
            "coordinate_space": "pdf_points_top_left",
            "page": {"width": page_width, "height": page_height},
            "signature": [72.0, max(72.0, page_height - 54.0)],
            "signature_date": [max(330.0, page_width - 180.0), max(72.0, page_height - 54.0)],
            "static_supporting_document": True,
        }
    )
    latest = int(
        (
            await db.execute(
                select(ContractTemplateVersion.revision)
                .where(ContractTemplateVersion.template_id == template.id)
                .order_by(ContractTemplateVersion.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        or 0
    )
    revision = latest + 1
    digest = _sha(raw)
    key = f"contract-templates/{template_key}/r{revision}-{digest[:16]}.pdf"
    if not storage.put_bytes(key, raw, "application/pdf"):
        raise RuntimeError("The template PDF could not be stored.")
    version = ContractTemplateVersion(
        template_id=template.id,
        revision=revision,
        s3_key=key,
        sha256=digest,
        page_count=source.page_count,
        has_acroform=False,
        field_names=[],
        overlay_map=selected_overlay,
        uploaded_by_user_id=actor_user_id,
        active=True,
    )
    db.add(version)
    template.s3_key = key
    template.revision = revision
    template.active = True
    await db.flush()
    return version


def signature_spots(overlay_map: dict[str, Any] | None) -> dict[str, list[float]] | None:
    overlay = overlay_map or {}
    signature = overlay.get("signature")
    signature_date = overlay.get("signature_date")
    if not (
        isinstance(signature, list)
        and len(signature) == 2
        and isinstance(signature_date, list)
        and len(signature_date) == 2
    ):
        return None
    if overlay.get("coordinate_space") == "normalized_top_left":
        page = overlay.get("page") or {}
        width = float(page.get("width") or 612)
        height = float(page.get("height") or 792)
        return {
            "signature": [float(signature[0]) * width, float(signature[1]) * height],
            "date": [float(signature_date[0]) * width, float(signature_date[1]) * height],
        }
    return {
        "signature": [float(signature[0]), float(signature[1])],
        "date": [float(signature_date[0]), float(signature_date[1])],
    }


async def packages(db: AsyncSession) -> list[tuple[ContractPackage, list[ContractPackageItem]]]:
    await ensure_defaults(db)
    rows = list(
        (
            await db.execute(
                select(ContractPackage)
                .where(ContractPackage.program_key.in_(SUPPORTED_PROGRAMS))
                .order_by(ContractPackage.program_key, ContractPackage.version.desc())
            )
        ).scalars().all()
    )
    out = []
    for package in rows:
        items = list(
            (
                await db.execute(
                    select(ContractPackageItem)
                    .where(ContractPackageItem.package_id == package.id)
                    .order_by(ContractPackageItem.sort_order)
                )
            ).scalars().all()
        )
        out.append((package, items))
    return out


def _program_viable(context: dict[str, Any], program_key: str) -> bool:
    programs = (context.get("routing") or {}).get("programs") or []
    row = next((item for item in programs if item.get("program_key") == program_key), None)
    return bool(row and row.get("status") in {"recommended", "potential"})


def ordered_program_keys(program_keys: list[str]) -> list[str]:
    selected = set(program_keys)
    if not selected or not selected.issubset(SUPPORTED_PROGRAMS):
        raise ValueError("Choose EZ Term, MicroCap, or both.")
    return [key for key in PROGRAM_ORDER if key in selected]


def envelope_program_keys(envelope: ContractEnvelope) -> list[str]:
    selected = [
        key
        for key in list(getattr(envelope, "program_keys", None) or [])
        if key in SUPPORTED_PROGRAMS
    ]
    return ordered_program_keys(selected or [envelope.program_key])


def envelope_document_key(template_key: str, program_key: str, *, multiple: bool) -> str:
    """Keep same-template program forms distinct inside one envelope.

    The legacy schema uniquely keys an envelope document by template key. A
    short program suffix preserves that invariant without changing historical
    rows or the immutable template-version reference.
    """
    if not multiple:
        return template_key
    suffix = "ez" if program_key == EZ_PROGRAM_KEY else "micro"
    return f"{template_key[: 48 - len(suffix) - 2]}__{suffix}"


def _program_result(context: dict[str, Any], program_key: str) -> dict[str, Any]:
    programs = (context.get("routing") or {}).get("programs") or []
    return next(
        (item for item in programs if item.get("program_key") == program_key),
        {},
    )


def _program_conditions(context: dict[str, Any], program_key: str) -> list[str]:
    row = _program_result(context, program_key)
    conditions = list(row.get("borrower_safe_reasons") or [])
    conditions.extend(
        item for item in list(row.get("unresolved") or []) if item not in conditions
    )
    return conditions


def _missing_for_program(values: dict[str, str], base_missing: list[str], program_key: str) -> list[str]:
    required = {
        "principal name": "owner_full",
        "principal email": "owner_email",
        "principal phone": "owner_phone",
        "principal home address": "owner_street",
        "guaranty type": "guaranty",
        "business legal name": "biz_legal",
        "business entity type": "biz_entity",
        "business formation state": "biz_formation_state",
        "business start date": "biz_start",
        "physical business address": "biz_address",
        "annual sales": "annual_sales",
        "funding amount requested": "amount_requested",
        "detailed use of funds": "use_of_funds",
        "existing MCA balance (enter zero when none)": "mca_balance",
        "existing SBA balance (enter zero when none)": "sba_balance",
        "authorized signer title": "signer_title",
    }
    # `build_values` is shared with older standalone agreements and therefore
    # reports requirements that are not present on this program application
    # (for example, the assigned rep).  A package is blocked only by fields
    # its configured PDF actually needs.
    missing: set[str] = set()
    for label, key in required.items():
        if not str(values.get(key) or "").strip():
            missing.add(label)
    if program_key == MICRO_PROGRAM_KEY:
        for label, key in (
            ("business DSCR inputs", "business_dscr"),
            ("owner count", "owner_count"),
            ("active UCC disclosure", "ucc_filings"),
            ("affiliate-business disclosure", "affiliates"),
        ):
            if values.get(key) in {None, "", "N/A"}:
                missing.add(label)
    return sorted(missing)


async def generate_envelope(
    db: AsyncSession,
    dealer: DealerBusiness,
    *,
    program_keys: list[str],
    actor: User,
    override_confirmations: dict[str, str | None] | None = None,
    override_reason: str | None = None,
) -> ContractEnvelope:
    selected_programs = ordered_program_keys(program_keys)
    confirmations = override_confirmations or {}
    await ensure_defaults(db, actor.id)
    context = await qc_master_application.build_context(db, dealer)
    routing = context.get("routing") or {}
    rules_version = routing.get("rules_version") or context.get("rules_version")
    now = datetime.now(UTC)

    manual_rows = list(
        (
            await db.execute(
                select(DealerProgramRuleResolution).where(
                    DealerProgramRuleResolution.dealer_id == dealer.id,
                    DealerProgramRuleResolution.rule_key == "program_selection.manual",
                    DealerProgramRuleResolution.status == "active",
                )
            )
        ).scalars().all()
    )
    manual_by_program = {row.program_key: row for row in manual_rows}
    package_override_rows = list(
        (
            await db.execute(
                select(DealerProgramRuleResolution).where(
                    DealerProgramRuleResolution.dealer_id == dealer.id,
                    DealerProgramRuleResolution.rule_key
                    == "program_package.selection_override",
                    DealerProgramRuleResolution.status == "active",
                )
            )
        ).scalars().all()
    )
    package_override_by_program = {
        row.program_key: row for row in package_override_rows
    }
    resolved_override_reasons: dict[str, str] = {}

    for row in package_override_rows:
        if row.program_key not in selected_programs or _program_viable(
            context, row.program_key
        ):
            row.status = "cleared"
            row.resolved_by_user_id = actor.id
            row.resolved_at = now
            row.resolution_note = (
                "Program was removed from the package selection."
                if row.program_key not in selected_programs
                else "The current routing result no longer requires an override."
            )

    for program_key in selected_programs:
        if _program_viable(context, program_key):
            continue
        reason = (confirmations.get(program_key) or "").strip()
        manual = manual_by_program.get(program_key)
        existing_override = package_override_by_program.get(program_key)
        legacy_super_admin_reason = (
            (override_reason or "").strip()
            if len(selected_programs) == 1 and actor.role == Role.SUPER_ADMIN
            else ""
        )
        if reason:
            if existing_override is not None and existing_override.status == "active":
                existing_override.status = "superseded"
                existing_override.resolved_by_user_id = actor.id
                existing_override.resolved_at = now
                existing_override.resolution_note = (
                    "Reconfirmed while regenerating the selected package."
                )
            result = _program_result(context, program_key)
            row = DealerProgramRuleResolution(
                dealer_id=dealer.id,
                program_key=program_key,
                rule_key="program_package.selection_override",
                kind="alternative_program",
                source="Field Desk Step 4 package selection",
                current_value={
                    "system_status": result.get("status") or "blocked",
                    "system_blockers": _program_conditions(context, program_key),
                    "selected_program_key": program_key,
                    "selected_package_programs": selected_programs,
                    "rules_version": rules_version,
                },
                recommended_action=(
                    "Generate the staff-selected application form while preserving "
                    "all system conditions for underwriting."
                ),
                status="active",
                rep_note=reason,
                requested_by_user_id=actor.id,
                requested_at=now,
            )
            db.add(row)
            resolved_override_reasons[program_key] = reason
        elif manual is not None:
            resolved_override_reasons[program_key] = (
                manual.rep_note
                or "Authorized staff selected this submission path with system blockers preserved."
            )
        elif existing_override is not None and existing_override.status == "active":
            resolved_override_reasons[program_key] = (
                existing_override.rep_note
                or "Authorized staff confirmed this blocked package selection."
            )
        elif legacy_super_admin_reason:
            resolved_override_reasons[program_key] = legacy_super_admin_reason
        else:
            label = PROGRAM_LABELS[program_key]
            raise ValueError(
                f"{label} is blocked. Hold its program card for three seconds "
                "and confirm the documented override before generating forms."
            )

    package_rows = list(
        (
            await db.execute(
                select(ContractPackage)
                .where(
                    ContractPackage.program_key.in_(selected_programs),
                    ContractPackage.active.is_(True),
                )
                .order_by(ContractPackage.version.desc())
            )
        ).scalars().all()
    )
    packages_by_program: dict[str, ContractPackage] = {}
    for package in package_rows:
        packages_by_program.setdefault(package.program_key, package)
    missing_packages = [
        PROGRAM_LABELS[key]
        for key in selected_programs
        if key not in packages_by_program
    ]
    if missing_packages:
        raise ValueError(
            f"No active forms package is configured for {', '.join(missing_packages)}."
        )
    items_by_program: dict[str, list[ContractPackageItem]] = {}
    for program_key in selected_programs:
        package = packages_by_program[program_key]
        items = list(
            (
                await db.execute(
                    select(ContractPackageItem)
                    .where(ContractPackageItem.package_id == package.id)
                    .order_by(ContractPackageItem.sort_order)
                )
            ).scalars().all()
        )
        if not items:
            raise ValueError(
                f"The {PROGRAM_LABELS[program_key]} package has no documents."
            )
        items_by_program[program_key] = items

    active = (
        await db.execute(
            select(ContractEnvelope)
            .where(
                ContractEnvelope.dealer_id == dealer.id,
                ContractEnvelope.status.notin_(["void", "executed"]),
            )
            .order_by(ContractEnvelope.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active and active.status == "out_for_signature":
        raise ValueError("Void the sent package before changing or regenerating it.")

    primary = (
        await db.execute(
            select(DealerOwner)
            .where(DealerOwner.dealer_id == dealer.id)
            .order_by(DealerOwner.is_primary.desc(), DealerOwner.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if primary is None:
        raise ValueError("Add a designated primary owner before generating forms.")

    primary_package = packages_by_program[selected_programs[0]]
    combined_package_key = "__".join(
        packages_by_program[key].key for key in selected_programs
    )
    package_key = (
        combined_package_key
        if len(combined_package_key) <= 80
        else f"combined-{_sha(combined_package_key.encode())[:16]}"
    )
    package_title = (
        primary_package.title
        if len(selected_programs) == 1
        else " + ".join(PROGRAM_LABELS[key] for key in selected_programs)
        + " Application Package"
    )
    envelope = active or ContractEnvelope(
        dealer_id=dealer.id,
        package_id=primary_package.id,
        package_key=package_key,
        package_version=max(
            packages_by_program[key].version for key in selected_programs
        ),
        program_key=selected_programs[0],
        program_keys=selected_programs,
        title=package_title,
        recipient_owner_id=primary.id,
        created_by_user_id=actor.id,
        delivery_history=[],
    )
    if active is None:
        db.add(envelope)
        await db.flush()
    else:
        await db.execute(delete(ContractEnvelopeDocument).where(
            ContractEnvelopeDocument.envelope_id == envelope.id
        ))
        await db.execute(delete(ContractDocument).where(
            ContractDocument.envelope_id == envelope.id
        ))
        await db.flush()

    envelope.package_id = primary_package.id
    envelope.package_key = package_key
    envelope.package_version = max(
        packages_by_program[key].version for key in selected_programs
    )
    envelope.program_key = selected_programs[0]
    envelope.program_keys = selected_programs
    envelope.title = package_title
    envelope.recipient_owner_id = primary.id

    profile = context.get("profile")
    if profile is None:
        profile = DealerApplicationProfile(dealer_id=dealer.id)
        db.add(profile)
    if len(selected_programs) == 1 or profile.selected_program not in selected_programs:
        profile.selected_program = selected_programs[0]
    profile.updated_by_user_id = actor.id
    base_values, base_missing = await contract_fill.build_values(db, dealer)
    package_snapshot = [
        {
            "program_key": key,
            "package_key": packages_by_program[key].key,
            "package_version": packages_by_program[key].version,
        }
        for key in selected_programs
    ]
    funding_profile = {
        "original_requested_amount": context["request"].get("original_amount"),
        "working_funding_goal": context["request"].get("amount"),
        "program_key": selected_programs[0],
        "program_keys": selected_programs,
        "system_status": context.get("route_status"),
        "annual_sales": context["financial"].get("annual_sales"),
        "annual_cash_flow_available_for_debt": context["financial"].get(
            "annual_cash_flow_available_for_debt"
        ),
        "monthly_debt_payments": context["financial"].get("monthly_debt_payments"),
        "dscr": context["financial"].get("dscr"),
        "verified_bank_months": context["financial"].get("statement_months") or [],
        "bank_evidence_target": context["financial"].get("accepted_statement_target", 6),
        "credit": [
            {
                "owner": owner.get("name"),
                "status": owner.get("credit_status"),
                "quality": owner.get("credit_quality"),
            }
            for owner in context.get("owners") or []
            if owner.get("credit_required")
        ],
        "debt_count": len(context.get("debts") or []),
        "unresolved_conditions": list(
            dict.fromkeys(
                condition
                for key in selected_programs
                for condition in _program_conditions(context, key)
            )
        ),
        "package_programs": package_snapshot,
    }
    values_by_program: dict[str, dict[str, Any]] = {}
    for program_key in selected_programs:
        values = dict(base_values)
        values["selected_program"] = program_key
        values["signer_title"] = profile.signer_title or ""
        values["_funding_profile"] = {
            **funding_profile,
            "program_key": program_key,
            "system_status": _program_result(context, program_key).get("status"),
        }
        missing = _missing_for_program(values, base_missing, program_key)
        values["_missing_data"] = missing
        values["_override_reason"] = resolved_override_reasons.get(program_key)
        values_by_program[program_key] = values

    envelope.source_sha256 = _sha(
        json.dumps(
            {
                "programs": selected_programs,
                "packages": package_snapshot,
                "values": values_by_program,
                "overrides": resolved_override_reasons,
            },
            sort_keys=True,
            default=str,
        ).encode()
    )
    envelope_missing: set[str] = set()
    document_specs: list[dict[str, Any]] = []
    seen_supporting: dict[tuple[str, str], int] = {}
    for program_key in selected_programs:
        for item in items_by_program[program_key]:
            if item.template_key != PROGRAM_APPLICATION_KEY:
                identity = (item.template_key, str(item.template_version_id or ""))
                existing_index = seen_supporting.get(identity)
                if existing_index is not None:
                    document_specs[existing_index]["required"] = bool(
                        document_specs[existing_index]["required"] or item.required
                    )
                    continue
                seen_supporting[identity] = len(document_specs)
            document_specs.append(
                {
                    "program_key": program_key,
                    "item": item,
                    "required": item.required,
                }
            )

    multiple = len(selected_programs) > 1
    for sort_order, spec in enumerate(document_specs):
        program_key = spec["program_key"]
        item = spec["item"]
        values = values_by_program[program_key]
        missing = list(values["_missing_data"])
        version = await db.get(ContractTemplateVersion, item.template_version_id) if item.template_version_id else None
        if version is None or not version.active:
            raise ValueError(f"{item.title_snapshot} has no active immutable template version.")
        raw = storage.get_bytes(version.s3_key)
        if raw is None:
            raise RuntimeError(f"The template for {item.title_snapshot} could not be read.")
        result = contract_fill.fill_pdf(
            item.template_key,
            raw,
            values,
            overlay_map=version.overlay_map,
        )
        document_template_key = envelope_document_key(
            item.template_key,
            program_key,
            multiple=multiple,
        )
        document_key = (
            f"contract-fills/{dealer.id}/envelopes/{envelope.id}/"
            f"{sort_order:02d}-{document_template_key}-{result.sha256[:16]}.pdf"
        )
        if not storage.put_bytes(document_key, result.pdf, "application/pdf"):
            raise RuntimeError(f"The populated {item.title_snapshot} could not be stored.")
        document_missing = sorted(set(missing) | set(result.missing))
        if spec["required"]:
            envelope_missing.update(document_missing)
        doc = ContractDocument(
            dealer_id=dealer.id,
            template_key=document_template_key,
            envelope_id=envelope.id,
            template_version_id=version.id,
            template_revision=version.revision,
            status="draft" if document_missing else "ready",
            field_values={
                **result.placed,
                "signer_title": profile.signer_title or "",
                "_funding_profile": funding_profile,
                "_missing_data": document_missing,
                "_overlay_problems": result.missing,
                "_signature_spots": signature_spots(version.overlay_map),
                "_base_template_key": item.template_key,
                "_program_key": program_key,
            },
            filled_s3_key=document_key,
            filled_sha256=result.sha256,
        )
        db.add(doc)
        await db.flush()
        title = item.title_snapshot
        if multiple and item.template_key == PROGRAM_APPLICATION_KEY:
            title = f"{PROGRAM_LABELS[program_key]} {item.title_snapshot}"
        db.add(ContractEnvelopeDocument(
            envelope_id=envelope.id,
            contract_document_id=doc.id,
            title_snapshot=title,
            sort_order=sort_order,
            required=bool(spec["required"]),
        ))
    envelope.status = "draft" if envelope_missing else "ready"
    await db.flush()
    return envelope


async def execute_envelope(
    db: AsyncSession,
    dealer: DealerBusiness,
    envelope: ContractEnvelope,
    *,
    typed_name: str,
    signature_png: bytes | None,
    signature_sha256: str | None,
    ip: str | None,
    user_agent: str | None,
) -> tuple[bytes, str]:
    if not signature_png or not signature_sha256:
        raise ValueError("A drawn signature is required for this application package.")
    if envelope.status == "executed" and envelope.bundle_s3_key and envelope.bundle_sha256:
        existing = storage.get_bytes(envelope.bundle_s3_key)
        if existing is None:
            raise RuntimeError("The executed package could not be read.")
        return existing, envelope.bundle_sha256
    if envelope.status != "out_for_signature":
        raise ValueError("This package is not out for signature.")
    rows = list(
        (
            await db.execute(
                select(ContractEnvelopeDocument, ContractDocument)
                .join(ContractDocument, ContractDocument.id == ContractEnvelopeDocument.contract_document_id)
                .where(ContractEnvelopeDocument.envelope_id == envelope.id)
                .order_by(ContractEnvelopeDocument.sort_order)
            )
        ).all()
    )
    if not rows or any(link.required and not link.acknowledged_at for link, _ in rows):
        raise ValueError("Review and acknowledge every required document before signing.")

    import fitz

    bundle = fitz.open()
    evidence: list[tuple[str, str]] = []
    for link, doc in rows:
        executed, digest = await contract_sign.execute(
            db,
            dealer,
            doc,
            typed_name=typed_name,
            signature_png=signature_png,
            signature_sha256=signature_sha256,
            ip=ip,
            user_agent=user_agent,
            title=link.title_snapshot,
        )
        child = fitz.open(stream=executed, filetype="pdf")
        bundle.insert_pdf(child)
        evidence.append((link.title_snapshot, digest))

    now = datetime.now(UTC)
    rows_html = "".join(
        f"<tr><td>{html.escape(title)}</td><td>{html.escape(digest)}</td></tr>"
        for title, digest in evidence
    )
    from weasyprint import HTML

    cert = HTML(string=f"""
      <html><head><style>
      body{{font-family:Arial,sans-serif;margin:44px;color:#111827}}
      h1{{font-size:21px}} p,td,th{{font-size:11px;line-height:1.5}}
      table{{width:100%;border-collapse:collapse}}td,th{{border:1px solid #d1d5db;padding:8px;text-align:left}}
      </style></head><body><h1>Package Certificate of Completion</h1>
      <p><b>{html.escape(envelope.title)}</b><br>Case: {html.escape(dealer.case_ref or str(dealer.id))}<br>
      Signer: {html.escape(typed_name)}<br>Signed: {now.isoformat()}<br>
      Programs: {html.escape(', '.join(PROGRAM_LABELS[key] for key in envelope_program_keys(envelope)))}<br>
      Package version: {envelope.package_version}<br>
      Source SHA-256: {html.escape(envelope.source_sha256 or '')}<br>
      Signature SHA-256: {html.escape(signature_sha256)}<br>
      Signing IP: {html.escape(ip or '')}<br>
      Device: {html.escape((user_agent or '')[:220])}<br>
      The signer reviewed and acknowledged every required document and affirmed that one electronic signature applies to each listed document.</p>
      <table><tr><th>Executed document</th><th>SHA-256</th></tr>{rows_html}</table></body></html>
    """).write_pdf()
    cert_doc = fitz.open(stream=cert, filetype="pdf")
    bundle.insert_pdf(cert_doc)
    output = bundle.tobytes(deflate=True)
    digest = _sha(output)
    key = f"contract-executed/{dealer.id}/envelopes/{envelope.id}/{digest[:16]}-package.pdf"
    if not storage.put_bytes(key, output, "application/pdf"):
        raise RuntimeError("The executed package could not be stored.")
    envelope.status = "executed"
    envelope.completed_at = now
    envelope.signer_name = typed_name
    envelope.signer_title = str((rows[0][1].field_values or {}).get("signer_title") or "") or None
    envelope.signature_sha256 = signature_sha256
    envelope.signer_ip = ip
    envelope.signer_user_agent = (user_agent or "")[:400]
    envelope.bundle_s3_key = key
    envelope.bundle_sha256 = digest
    await db.flush()
    return output, digest
