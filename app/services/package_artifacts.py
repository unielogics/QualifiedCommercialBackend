"""Read-only package metadata and click-time links for immutable staff PDFs."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

KINDS = ("executive_summary", "lender_packet", "investor_presentation")
TTL_SECONDS = 900


def _record(value):
    return value if isinstance(value, dict) else {}


def _version(value):
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _when(value):
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC) if isinstance(value, datetime) else datetime.min.replace(tzinfo=UTC)


def latest_complete(artifacts, active_bucket_file_ids=None):
    """Select one entire stored generation, never a mixture of newer fragments."""
    groups = {}
    for artifact in artifacts:
        body = _record(artifact.body_json)
        package, pdf = _record(body.get("_package")), _record(body.get("_pdf"))
        if package.get("status") != "current":
            continue
        if active_bucket_file_ids is not None and str(package.get("bucket_file_id")) not in {str(value) for value in active_bucket_file_ids}:
            continue
        try:
            generation = str(UUID(str(package.get("generation_id"))))
        except (ValueError, TypeError):
            continue
        if artifact.artifact_type not in KINDS or not artifact.s3_key or not isinstance(pdf.get("package_input_sha256"), str) or not pdf["package_input_sha256"]:
            continue
        rows = groups.setdefault(generation, {})
        prior = rows.get(artifact.artifact_type)
        if prior is None or (_when(artifact.created_at), str(artifact.id)) > (_when(prior.created_at), str(prior.id)):
            rows[artifact.artifact_type] = artifact
    complete = []
    for generation, rows in groups.items():
        if set(rows) != set(KINDS):
            continue
        fingerprints = {_record(_record(row.body_json).get("_pdf")).get("package_input_sha256") for row in rows.values()}
        if len(fingerprints) != 1:
            continue
        stamp = max(_when(row.created_at) for row in rows.values())
        complete.append((stamp, generation, rows, next(iter(fingerprints))))
    if not complete:
        return None
    stamp, generation, rows, fingerprint = max(complete, key=lambda item: (item[0], item[1]))
    return {"generation_id": generation, **{kind + "_id": str(rows[kind].id) for kind in KINDS},
        "versions": {kind: _version(_record(_record(rows[kind].body_json).get("_package")).get("version")) for kind in KINDS},
        "created_at": stamp.isoformat(), "input_fingerprint": fingerprint, "source_freshness": "unchecked"}


async def load_intake(db, intake_id):
    from app.models.bucket import Bucket
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    row = (await db.execute(select(PublicUnderwritingIntake.id, PublicUnderwritingIntake.bucket_id)
        .join(Bucket, Bucket.id == PublicUnderwritingIntake.bucket_id)
        .where(PublicUnderwritingIntake.id == intake_id, Bucket.archived_at.is_(None)))).one_or_none()
    if row is None:
        raise HTTPException(404, "File not found.")
    return SimpleNamespace(id=row.id, bucket_id=row.bucket_id)


async def metadata(db, intake):
    from app.capital_partners.models import CpListing
    from app.capital_partners.services.refresh import state
    from app.models.application_profile import ApplicationProfile
    from app.models.bucket import BucketFile
    from app.models.public_underwriting_intake import PublicUnderwritingIntakeArtifact
    from app.routers import dealer_ai_intake as core

    artifacts = list((await db.execute(select(PublicUnderwritingIntakeArtifact).where(
        PublicUnderwritingIntakeArtifact.intake_id == intake.id,
        PublicUnderwritingIntakeArtifact.artifact_type.in_(KINDS))
        .order_by(PublicUnderwritingIntakeArtifact.created_at.desc(), PublicUnderwritingIntakeArtifact.id.desc()))).scalars())
    listing = (await db.execute(select(CpListing).join(ApplicationProfile, CpListing.profile_id == ApplicationProfile.id)
        .where(ApplicationProfile.intake_id == intake.id)
        .order_by(CpListing.created_at.desc(), CpListing.id.desc()).limit(1))).scalar_one_or_none()
    active = set((await db.execute(select(BucketFile.id).where(BucketFile.bucket_id == intake.bucket_id,
        BucketFile.status == "uploaded", BucketFile.deleted_at.is_(None)))).scalars())
    result = []
    for artifact in artifacts:
        body = _record(artifact.body_json)
        normalized = SimpleNamespace(**{key: getattr(artifact, key, None) for key in (
            "id", "intake_id", "artifact_type", "title", "body_text", "s3_key", "created_by_user_id", "created_at", "updated_at")},
            body_json=body)
        item = core._artifact_read(normalized, active_bucket_file_ids=active, storage_client_factory=lambda: None).model_dump()
        pdf = _record(body.get("_pdf"))
        item.update(body_text=None, body_json={"_pdf": {key: deepcopy(pdf.get(key)) for key in (
            "sha256", "size_bytes", "content_type", "package_input_sha256", "package_options", "package_document_types")},
            **{key: deepcopy(body[key]) for key in ("investor_report_format", "operating_plan_snapshot") if key in body}},
            preview_url=None, download_url=None, is_fresh=None)
        if artifact.artifact_type == "executive_summary" and core._artifact_package_metadata(artifact).get("status") == "current":
            item["body_json"].update({key: deepcopy(body[key]) for key in ("key_metrics", "risks", "mitigants", "next_best_action") if key in body})
        result.append(item)
    return {"intake_id": str(intake.id), "artifacts": result,
        "latest_package": latest_complete(artifacts, active), "refresh": state(listing) if listing else None}


async def fresh_url(db, intake_id, artifact_id, *, download=False):
    from app.models.public_underwriting_intake import PublicUnderwritingIntakeArtifact
    from app.routers import dealer_ai_intake as core

    artifact = (await db.execute(select(PublicUnderwritingIntakeArtifact).where(
        PublicUnderwritingIntakeArtifact.id == artifact_id,
        PublicUnderwritingIntakeArtifact.intake_id == intake_id,
        PublicUnderwritingIntakeArtifact.artifact_type.in_(KINDS)))).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(404, "Package PDF not found in this file.")
    if not artifact.s3_key:
        raise HTTPException(409, {"code": "artifact_pdf_missing", "message": "This saved version has no stored PDF. Refresh the package to prepare one."})
    disposition = "attachment" if download else "inline"

    def presign():
        client = core._s3_client()
        if client is None:
            raise HTTPException(503, "Document storage is temporarily unavailable.")
        bucket, _prefix, _kms = core._bucket_storage_config()
        try:
            head = client.head_object(Bucket=bucket, Key=artifact.s3_key)
            if int(head.get("ContentLength") or 0) <= 0:
                raise HTTPException(409, {"code": "artifact_pdf_missing", "message": "The saved PDF is unavailable. Refresh the package or contact the team."})
            opened = client.get_object(Bucket=bucket, Key=artifact.s3_key, Range="bytes=0-4")
            stream = opened["Body"]
            try:
                if stream.read(5) != b"%PDF-":
                    raise HTTPException(409, {"code": "artifact_pdf_invalid", "message": "The saved object is not a readable PDF. Refresh the package."})
            finally:
                stream.close()
            return client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": artifact.s3_key,
                "ResponseContentDisposition": f'{disposition}; filename="{core._safe_filename(artifact.title or artifact.artifact_type)}.pdf"',
                "ResponseContentType": "application/pdf", "ResponseCacheControl": "no-store"}, ExpiresIn=TTL_SECONDS)
        except HTTPException:
            raise
        except Exception as exc:
            code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise HTTPException(409, {"code": "artifact_pdf_missing", "message": "The saved PDF could not be found. Refresh the package."}) from exc
            raise HTTPException(503, "The saved PDF link could not be prepared. Try again.") from exc

    url = await asyncio.to_thread(presign)
    version = core._artifact_package_metadata(artifact).get("version")
    return {"artifact_id": str(artifact.id), "url": url, "expires_in_seconds": TTL_SECONDS,
        "version": _version(version), "content_type": "application/pdf", "disposition": disposition}
