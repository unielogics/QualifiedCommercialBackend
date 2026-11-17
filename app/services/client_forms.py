"""The real-estate loan application as a live form on a file.

Reads merge three layers, newest last: what the file already knows (prefill,
re-read on every read so a desk correction flows into every field nobody
typed), what was typed on this form (``client_forms.answers``), and what the
client confirmed ("Looks right" on a value the AI suggested). Saves are
staged against a revision so two tabs cannot overwrite each other silently.
Submitting freezes the merged body into an immutable version with its unsigned
PDF; signing (app/services/signed_forms.py) works on that frozen copy only.

Answers flow back to the file on submission, one home per fact, through the
home's existing writer: occupancy, business purpose, the purpose, property
type, down payment, transaction and payoff go to ``funding_review_details``
(and through the business-purpose rule). Nothing else is written back: the
email is the login identity, the amount is governed by funding requests, and
the desk's own facts stay the desk's.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client_form import ClientForm, ClientFormVersion
from app.services import re_application_prefill as prefill
from app.services import re_application_schema as schema

log = logging.getLogger(__name__)

KIND = schema.KIND
SOURCE_TYPED_CLIENT = "You entered this"
SOURCE_TYPED_DESK = "Entered by your advisor"


def _now() -> datetime:
    return datetime.now(UTC)


def body_sha256(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def intake_for(db: AsyncSession, profile: Any):
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    intake_id = getattr(profile, "intake_id", None)
    return await db.get(PublicUnderwritingIntake, intake_id) if intake_id else None


async def get_form(db: AsyncSession, profile_id: Any, kind: str = KIND, *, lock: bool = False) -> ClientForm | None:
    query = select(ClientForm).where(ClientForm.profile_id == profile_id, ClientForm.kind == kind)
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def current_version(db: AsyncSession, form: ClientForm | None) -> ClientFormVersion | None:
    if form is None or form.current_version_id is None:
        return None
    return await db.get(ClientFormVersion, form.current_version_id)


def compose(pre: prefill.Prefill, form: ClientForm | None, *, audience: str = "client") -> dict[str, Any]:
    """One view of the form: the merged body, where each value came from, and what is left."""
    answers = dict(form.answers or {}) if form is not None else {}
    touched = dict(form.touched or {}) if form is not None else {}
    body = pre.body()
    provenance = {path: dict(item) for path, item in pre.provenance.items()}
    if "has_coborrower" in answers:
        body["has_coborrower"] = bool(answers["has_coborrower"])
    for path, value in answers.items():
        if path == "has_coborrower":
            continue
        if path == "experience.other_properties":
            body["experience"]["other_properties"] = list(value or [])
        else:
            schema.set_at(body, path, value)
        by = (touched.get(path) or {}).get("by", "client")
        provenance[path] = {"tier": prefill.TIER_ANSWERED, "source": SOURCE_TYPED_CLIENT if by == "client" else SOURCE_TYPED_DESK}
    for path, mark in touched.items():
        if isinstance(mark, dict) and mark.get("confirmed") and path in provenance:
            provenance[path]["confirmed"] = True
    confirmed = {
        path for path, item in provenance.items()
        if item.get("tier") in (prefill.TIER_ANSWERED, prefill.TIER_BUREAU) or item.get("confirmed")
    }
    applicable = {path for _, path, _ in schema.applicable_paths(body)}
    please_check = sorted(
        path for path, item in provenance.items()
        if item.get("tier") == prefill.TIER_CHECK and path not in confirmed and path in applicable
        and schema.get_at(body, path) not in (None, "", [])
    )
    view = {
        "kind": KIND,
        "schema_version": schema.SCHEMA_VERSION,
        "body": body,
        "provenance": {path: item for path, item in provenance.items() if path in applicable or path == "experience.other_properties"},
        "completion": schema.completion(body, confirmed=confirmed),
        "please_check": please_check,
        "revision": int(form.revision) if form is not None else 0,
        "status": form.status if form is not None else "draft",
    }
    if audience == "desk":
        view["warnings"] = list(pre.warnings)
    return view


async def read(db: AsyncSession, profile: Any, *, audience: str = "client") -> dict[str, Any]:
    intake = await intake_for(db, profile)
    pre = await prefill.build(db, profile, intake=intake)
    form = await get_form(db, profile.id)
    view = compose(pre, form, audience=audience)
    version = await current_version(db, form)
    view["version"] = (
        {
            "id": str(version.id),
            "number": version.number,
            "submitted_at": version.created_at.isoformat() if version.created_at else None,
            "submitted_by": version.submitted_by,
            "body_sha256": version.body_sha256,
            "pdf_available": bool(version.pdf_s3_key),
        }
        if version is not None
        else None
    )
    if audience == "desk":
        from app.services import re_application_pdf

        letterhead = await _letterhead(db)
        if re_application_pdf.letterhead_is_placeholder(letterhead):
            view["warnings"].append({
                "path": "",
                "label": "Letterhead",
                "source": "Settings",
                "message": "The firm letterhead still holds the placeholder address, so the application PDF prints no street address. Set it in Settings → Letterhead.",
            })
    return view


async def _lock(db: AsyncSession, profile: Any) -> None:
    from app.services.financial_workspace import lock_financial_profile

    if isinstance(db, AsyncSession):
        await lock_financial_profile(db, profile)


async def save(
    db: AsyncSession,
    profile: Any,
    *,
    changes: dict[str, Any] | None,
    confirm: list[str] | None,
    has_coborrower: bool | None,
    revision: int,
    actor: str,
) -> dict[str, Any]:
    """Stage typed answers and confirmations; refuse a stale revision or bad values."""
    await _lock(db, profile)
    form = await get_form(db, profile.id, lock=True)
    if form is None:
        form = ClientForm(profile_id=profile.id, kind=KIND, answers={}, touched={}, revision=0, status="draft")
        db.add(form)
        await db.flush()
    if int(form.revision) != int(revision):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "revision_conflict", "revision": int(form.revision), "message": "This form changed in another window. Reload it to see the latest answers."},
        )
    from app.services import signed_forms_state

    if await signed_forms_state.signing_started(db, form):
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "signing_started", "message": "Signing has started on this application. Ask your advisor to reopen it to make changes."})
    answers = dict(form.answers or {})
    touched = dict(form.touched or {})
    moment = _now().isoformat()
    problems: dict[str, str] = {}
    for path, value in (changes or {}).items():
        if path == "experience.other_properties":
            clean, row_problems = schema.coerce({"experience": {"other_properties": value}})
            if row_problems:
                problems.update(row_problems)
                continue
            answers[path] = clean["experience"]["other_properties"]
            touched[path] = {"by": actor, "at": moment}
            continue
        spec = schema.spec_for(path)
        if spec is None:
            problems[path] = "Unknown field"
            continue
        try:
            answers[path] = schema.coerce_value(spec, value)
        except schema.FieldProblem as exc:
            problems[path] = str(exc)
            continue
        touched[path] = {"by": actor, "at": moment}
    for path in confirm or []:
        if schema.spec_for(path) is None and path != "experience.other_properties":
            continue
        touched[path] = {**(touched.get(path) or {}), "by": actor, "at": moment, "confirmed": True}
    if has_coborrower is not None:
        answers["has_coborrower"] = bool(has_coborrower)
        touched["has_coborrower"] = {"by": actor, "at": moment}
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, {"code": "invalid_fields", "fields": problems, "message": "Some answers need another look."})
    form.answers = answers
    form.touched = touched
    form.revision = int(form.revision) + 1
    if form.status == "submitted":
        # A change after submitting needs a new submission (and a new PDF).
        form.status = "draft"
    await db.flush()
    return await read(db, profile, audience="desk" if actor == "desk" else "client")


async def _letterhead(db: AsyncSession) -> Any:
    from app.models.app_settings import AppSettings
    from app.services.prequal_pdf import _load_letterhead

    if not isinstance(db, AsyncSession):
        return None
    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    return _load_letterhead(row)


def consents_for(signature: dict[str, Any] | None) -> list[Any]:
    from app.services import form_kinds

    kind = form_kinds.get(KIND)
    keys = (signature or {}).get("consents") or ((kind.default_signature or {}).get("consents") if kind else None) or ["esign"]
    return [consent for key in keys if (consent := form_kinds.consent(key)) is not None]


async def render(db: AsyncSession, profile: Any, body: dict[str, Any], *, version_number: int | None, signature: dict[str, Any] | None = None) -> bytes:
    from app.services import re_application_pdf

    reference = f"File {str(profile.id)[:8].upper()}"
    html = re_application_pdf.build_html(
        body,
        reference=reference,
        version_number=version_number,
        letterhead=await _letterhead(db),
        consents=consents_for(signature),
    )
    return re_application_pdf.render_pdf(html)


async def submit(
    db: AsyncSession,
    profile: Any,
    *,
    revision: int,
    actor: str,
    user: Any = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Freeze the merged application, file its unsigned PDF, and write answers back."""
    await _lock(db, profile)
    form = await get_form(db, profile.id, lock=True)
    if form is None:
        form = ClientForm(profile_id=profile.id, kind=KIND, answers={}, touched={}, revision=0, status="draft")
        db.add(form)
        await db.flush()
    if idempotency_key:
        existing = (
            await db.execute(select(ClientFormVersion).where(ClientFormVersion.form_id == form.id, ClientFormVersion.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing is not None:
            return await read(db, profile, audience="desk" if actor == "desk" else "client")
    if int(form.revision) != int(revision):
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "revision_conflict", "revision": int(form.revision), "message": "This form changed in another window. Reload it before submitting."})
    view = await read(db, profile, audience="desk")
    if not view["completion"]["complete"] or view["please_check"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "incomplete", "missing": view["completion"]["missing"], "please_check": view["please_check"], "message": "A few answers are still needed before you can review and sign."},
        )
    body = view["body"]
    number = int((await db.execute(select(func.max(ClientFormVersion.number)).where(ClientFormVersion.form_id == form.id))).scalar() or 0) + 1
    signature = await application_programs_signature(db, profile)
    pdf = await render(db, profile, body, version_number=number, signature=signature)
    from app.routers.dealer_ai_intake import _bucket_storage_config, _put_bucket_object

    _, prefix, _ = _bucket_storage_config()
    key = f"{prefix}/client-forms/{profile.id}/{KIND}/v{number}-{body_sha256(body)[:12]}-unsigned.pdf"
    _put_bucket_object(key, "application/pdf", pdf, prevent_overwrite=True)
    version = ClientFormVersion(
        form_id=form.id,
        profile_id=profile.id,
        number=number,
        schema_version=schema.SCHEMA_VERSION,
        body=body,
        provenance=view["provenance"],
        body_sha256=body_sha256(body),
        pdf_s3_key=key,
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        submitted_by=actor,
        submitted_by_user_id=getattr(user, "id", None),
        idempotency_key=idempotency_key,
    )
    db.add(version)
    await db.flush()
    form.status = "submitted"
    form.current_version_id = version.id
    await write_back(db, profile, body)
    try:
        from app.models.file_event import VISIBILITY_CLIENT
        from app.services import file_events

        await file_events.emit(
            db,
            profile=profile,
            kind="application.submitted",
            visibility=VISIBILITY_CLIENT,
            title=f"Real estate application submitted (version {number})",
            body="The application is ready to review and sign." if signature else "The application was submitted.",
            actor=user,
            actor_label=None if user is not None else "The client",
            target_type="client_form_version",
            target_id=version.id,
            meta={"version": number, "submitted_by": actor},
        )
    except Exception:  # noqa: BLE001 - the timeline line never blocks a submission
        log.exception("application submitted event failed for profile %s", profile.id)
    return await read(db, profile, audience="desk" if actor == "desk" else "client")


async def application_programs_signature(db: AsyncSession, profile: Any) -> dict[str, Any] | None:
    """The signature the file's requirements configure for this form (merged), if any."""
    from app.models.bucket import BucketRequestedDocument
    from app.services import form_kinds

    bucket_id = getattr(profile, "primary_bucket_id", None)
    if bucket_id is None or not isinstance(db, AsyncSession):
        return None
    rows = (
        await db.execute(
            select(BucketRequestedDocument).where(
                BucketRequestedDocument.bucket_id == bucket_id,
                BucketRequestedDocument.signature_kind == "signed_form",
                BucketRequestedDocument.status.notin_(["closed", "not_applicable", "superseded"]),
            )
        )
    ).scalars().all()
    blocks = []
    for row in rows:
        source = row.requirement_source if isinstance(row.requirement_source, dict) else {}
        if source.get("form_kind") == KIND and isinstance(source.get("signature"), dict):
            blocks.append(source["signature"])
    return form_kinds.merge_signatures(blocks)


async def write_back(db: AsyncSession, profile: Any, body: dict[str, Any]) -> None:
    """Answers that have a home elsewhere on the file go there, through its own writer."""
    intake = await intake_for(db, profile)
    if intake is None:
        return
    from app.routers import dealer_ai_intake as core
    from app.services import business_purpose

    if getattr(intake, "variant", None) != core.FUNDING_VARIANT:
        return
    request = body.get("request") or {}
    property_ = body.get("property") or {}
    purpose_label = {value: key for key, value in schema.PURPOSE_BY_TRANSACTION.items()}.get(request.get("loan_purpose"))
    proposed = {
        "occupancy": request.get("occupancy"),
        "business_purpose": request.get("business_purpose"),
        "purpose_description": request.get("purpose_description"),
        "property_type": property_.get("property_type"),
        "down_payment_amount": request.get("down_payment"),
        "transaction_type": purpose_label,
        "existing_payoff_amount": property_.get("payoff_amount") if property_.get("payoff_amount") is not None else property_.get("first_lien_balance"),
    }
    accepted = core._merge_funding_review_details(intake, {key: value for key, value in proposed.items() if value is not None}, source=business_purpose.SOURCE_FORM)
    if accepted.keys() & {"occupancy", "business_purpose"}:
        await business_purpose.after_answers(db, intake, source=business_purpose.SOURCE_FORM, answers=accepted)


async def version_pdf(db: AsyncSession, profile: Any, version_id: Any) -> bytes:
    version = await db.get(ClientFormVersion, version_id)
    if version is None or version.profile_id != profile.id or not version.pdf_s3_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    from app.routers.dealer_ai_intake import _read_bucket_object

    data = _read_bucket_object(version.pdf_s3_key)
    if not data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The PDF is not available")
    return data


async def preview_pdf(db: AsyncSession, profile: Any) -> bytes:
    """The application as it stands right now, unsigned, never stored."""
    view = await read(db, profile, audience="client")
    return await render(db, profile, view["body"], version_number=None, signature=await application_programs_signature(db, profile))


async def mint_link(db: AsyncSession, profile: Any, *, invitee_email: str | None, created_by: Any = None, label: str | None = None) -> tuple[Any, str]:
    from app.services import financial_statements

    return await financial_statements.mint_link(
        db, profile, kind=KIND, label=label or "Real estate loan application", invitee_email=invitee_email, created_by=created_by
    )


def form_url(token: str) -> str:
    from app.config import get_settings

    return f"{get_settings().frontend_app_url.rstrip('/')}/forms/{KIND}/{token}"
