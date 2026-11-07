"""Content-bound supporting documents for one accepted debt schedule.

This is a delivery read model, never a writer or a source of debt figures.
The saved row UUID owns its documents; a duplicate contributes its source
documents to the surviving obligation without becoming another loan.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from html import escape
from urllib.parse import quote
from uuid import UUID

from sqlalchemy import select

from app.models.bucket import BucketFile
from app.services.financial_version_selection import (
    accepted_debt_schedule_snapshot,
    active_debt_schedule_rows,
)
from app.services.loan_progress import finite_amount, format_paid_down_pct, loan_progress_fields


def reviewed_loan_reference(row):
    """Display only a bounded reference explicitly retained on the saved row."""
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    raw = row.get("loan_reference") or evidence.get("loan_reference")
    if not isinstance(raw, str) or len(raw) > 80:
        return None
    value = " ".join(raw.split())
    if not value or not re.fullmatch(r"[A-Za-z0-9#*/ ._-]+", value) or re.search(r"\bssn\b|\b\d{3}-\d{2}-\d{4}\b", value, re.I):
        return None
    return value


def debt_heading(debt, ordinal):
    label = f"{ordinal}. {debt.get('lender') or 'Unnamed obligation'}"
    reference = reviewed_loan_reference(debt)
    return f"{label} - loan {reference}" if reference else label


def _links(body):
    active = {str(row["id"]): row for row in active_debt_schedule_rows(body)}
    result = {row_id: [] for row_id in active}
    for row in (body or {}).get("debts") or []:
        row_id = str(row.get("id"))
        survivor = str(row.get("duplicate_of_id"))
        target = row_id if row_id in active else survivor if row.get("status") == "duplicate" and survivor in active else None
        if target is None:
            continue
        links = row.get("evidence_links") if "evidence_links" in row else row.get("documents")
        for link in links or []:
            if isinstance(link, dict) and link.get("role") in {"agreement", "payoff", "supporting"}:
                result[target].append((link, row_id if target != row_id else None))
    return active, result


def build_manifest(version, files, *, scoped_ids, latest_ids, reviewed_payoff_ids, excluded_ids=()):
    """Pure, redacted projection; never serialize a file outside proven scope."""
    if version is None:
        return {"status": "no_accepted_schedule", "schedule": None, "debts": []}
    scoped, latest, reviewed, excluded = (set(map(str, group)) for group in (scoped_ids, latest_ids, reviewed_payoff_ids, excluded_ids))
    files = {str(file.id): file for file in files}
    active, links = _links(version.body)
    debts = []
    for row_id, row in active.items():
        documents = {}
        for link, duplicate_id in sorted(links[row_id], key=lambda entry: entry[1] is not None):
            file_id = str(link.get("file_id") or "")
            key = (file_id, link["role"])
            if key in documents:
                if duplicate_id:
                    documents[key]["duplicate_row_ids"].append(duplicate_id)
                continue
            file = files.get(file_id) if file_id in scoped and file_id not in excluded else None
            status = "missing" if file is None else "deleted" if getattr(file, "deleted_at", None) else "rejected" if file.status == "rejected" else "unavailable" if file.status != "uploaded" else "superseded" if file_id not in latest else "pending_review" if getattr(file, "document_version_status", "current") != "current" else "current"
            if file_id in excluded:
                status = "excluded"
            elif file_id not in scoped:
                status = "out_of_scope"
            selected = link["role"] == "payoff" and link.get("is_current") is True and duplicate_id is None
            current_payoff = bool(selected and file_id in reviewed and status in {"current", "pending_review"})
            documents[key] = {
                "file_id": file_id, "role": link["role"], "status": status,
                "file_name": file.file_name if file else None,
                "content_type": file.content_type if file else None,
                "content_hash": file.content_hash if file else None,
                "available": bool(file and status in {"current", "pending_review", "superseded"}),
                "selected_current_payoff": selected, "current_payoff": current_payoff,
                "reviewed_payoff_amount": str(link["payoff_amount"]) if link.get("payoff_amount") is not None else None,
                "reviewed_payoff_date": str(link["payoff_date"]) if link.get("payoff_date") else None,
                "duplicate_row_ids": [duplicate_id] if duplicate_id else [],
            }
        reference = reviewed_loan_reference(row)
        balance = row.get("balance") if row.get("balance") is not None else row.get("current_balance")
        debts.append({"row_id": row_id, "lender": row.get("lender") or "Unnamed obligation",
                      **({"loan_reference": reference} if reference else {}),
                      **loan_progress_fields(row),
                      "balance": str(balance) if balance is not None else None,
                      "monthly_payment": str(row["monthly_payment"]) if row.get("monthly_payment") is not None else None,
                      "documents": list(documents.values())})
    stamp = getattr(version, "created_at", None)
    return {"status": "accepted", "schedule": {
        "version_id": str(version.id), "version_number": version.version_number,
        "saved_at": stamp.isoformat() if stamp else None,
        "body_sha256": getattr(version, "body_sha256", None),
        "pdf_file_id": str(version.pdf_file_id) if getattr(version, "pdf_file_id", None) else None,
    }, "debts": debts}


async def load_manifest(db, profile):
    from app.services import merchant_processing, provenance
    from app.services.document_version_selection import document_file_ids
    from app.services.financial_debt_actions import reviewed_payoff_source_ids
    from app.services.financial_workspace import source_files_for_profile

    version = await accepted_debt_schedule_snapshot(db, profile.id)
    if version is None:
        return build_manifest(None, [], scoped_ids=[], latest_ids=[], reviewed_payoff_ids=[])
    _, groups = _links(version.body)
    ids = set()
    for entries in groups.values():
        for link, _duplicate in entries:
            try:
                ids.add(UUID(str(link.get("file_id"))))
            except (ValueError, TypeError):
                continue
    files = list((await db.execute(select(BucketFile).where(BucketFile.id.in_(ids)))).scalars().all()) if ids else []
    allowed = {file.id for file in await source_files_for_profile(db, profile)}
    # Deleted same-room sources may still be named as unavailable. Never widen
    # a selected cross-room source's scope merely because an ID was submitted.
    allowed.update(file.id for file in files if file.bucket_id == profile.primary_bucket_id)
    excluded = {file.id for file in files if merchant_processing.is_offer_document(file) or provenance.is_internal_package_output(file)}
    latest = await document_file_ids(db, ids & allowed, usable=False)
    reviewed = await reviewed_payoff_source_ids(db, (ids & allowed) - excluded)
    return build_manifest(version, files, scoped_ids=allowed, latest_ids=latest,
                          reviewed_payoff_ids=reviewed, excluded_ids=excluded)


async def profile_for_intake(db, intake):
    from app.models.application_profile import ApplicationProfile

    if not isinstance(getattr(db, "info", None), dict):
        return None
    profile = (await db.execute(select(ApplicationProfile).where(ApplicationProfile.intake_id == intake.id))).scalar_one_or_none()
    return profile if profile is not None and profile.primary_bucket_id == intake.bucket_id else None


async def manifest_for_intake(db, intake):
    profile = await profile_for_intake(db, intake)
    return await load_manifest(db, profile) if profile else build_manifest(None, [], scoped_ids=[], latest_ids=[], reviewed_payoff_ids=[])


async def delivery_files(db, intake, manifest):
    from app.services.financial_workspace import source_files_for_profile

    profile = await profile_for_intake(db, intake)
    if profile is None:
        return []
    ids = available_file_ids(manifest)
    return [file for file in await source_files_for_profile(db, profile)
            if file.id in ids and file.status == "uploaded" and file.deleted_at is None]


def available_file_ids(manifest):
    return {UUID(document["file_id"]) for debt in manifest.get("debts") or []
            for document in debt["documents"] if document["available"]}


def delivery_manifest(manifest, archived, failed_ids=()):
    """Use the actual successful ZIP writes, after collision-safe naming."""
    result = copy.deepcopy(manifest)
    failed = set(map(str, failed_ids))
    for debt in result.get("debts") or []:
        for document in debt["documents"]:
            entry = archived.get(document["file_id"]) if document["available"] else None
            document["archive_path"] = entry["path"] if entry else None
            document["archived_sha256"] = entry["sha256"] if entry else None
            document["preview_path"] = entry.get("preview_path") if entry else None
            document["preview_sha256"] = entry.get("preview_sha256") if entry else None
            document["preview_status"] = entry.get("preview_status", "not_applicable") if entry else "not_in_package"
            document["delivery_status"] = "archived" if entry else "fetch_failed" if document["file_id"] in failed else "not_in_package"
    return result


def saved_at_label(value):
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC).strftime("%b %d, %Y %H:%M UTC")
    except (TypeError, ValueError):
        return "Date unavailable"


def loan_summary_text(debt):
    """Figures from this exact accepted row, never from one of its payoff documents."""
    progress = loan_progress_fields(debt)

    def money(value):
        amount = finite_amount(value)
        return f"${amount:,.2f}" if amount is not None else "Not provided"

    return (f"Origination date: {progress['originated_on'] or 'Not provided'}; "
            f"original amount: {money(progress['original_amount'])}; "
            f"current balance: {money(debt.get('balance'))}; "
            f"paid down: {format_paid_down_pct(progress['paid_down_pct'])}; "
            f"monthly payment: {money(debt.get('monthly_payment'))}")


def index_text(manifest):
    schedule = manifest.get("schedule") or {}
    lines = ["Debt supporting documents", f"Accepted schedule v{schedule.get('version_number', '-')} - {saved_at_label(schedule.get('saved_at'))}",
             "Only the accepted schedule supplies debt figures. Linked images do not establish a current payoff."]
    for number, debt in enumerate(manifest.get("debts") or [], 1):
        lines += ["", debt_heading(debt, number), loan_summary_text(debt)]
        for document in debt["documents"]:
            label = "Current reviewed payoff" if document["current_payoff"] else "Previously selected payoff; review latest" if document["selected_current_payoff"] else document["role"]
            lines.append(f"  {label}: {document.get('file_name') or 'Unavailable linked source'} - {document['status']}; {document.get('archive_path') or 'NOT INCLUDED'}")
            if document.get("preview_path"):
                lines.append(f"    Readable JPEG preview (derivative): {document['preview_path']}")
            elif document.get("preview_status") == "unavailable":
                lines.append("    JPEG preview unavailable; original included.")
            if document["role"] == "payoff":
                lines.append(f"    Reviewed amount: {document.get('reviewed_payoff_amount') or 'unknown'}; date: {document.get('reviewed_payoff_date') or 'unknown'}")
        if not debt["documents"]:
            lines.append("  No supporting documents linked to this saved obligation.")
    return "\n".join(lines) + "\n"


def index_html(manifest):
    sections = []
    for number, debt in enumerate(manifest.get("debts") or [], 1):
        items = []
        for document in debt["documents"]:
            name = escape(str(document.get("file_name") or "Unavailable linked source"))
            path = document.get("archive_path")
            name = f'<a href="{escape(quote(path, safe="/"), quote=True)}">{name}</a>' if path else name + " <strong>NOT INCLUDED</strong>"
            preview = document.get("preview_path")
            if preview:
                name += f' (<a href="{escape(quote(preview, safe="/"), quote=True)}">JPEG preview</a>; derivative)'
            elif document.get("preview_status") == "unavailable":
                name += " (JPEG preview unavailable; original included)"
            label = "Current reviewed payoff" if document["current_payoff"] else "Previously selected payoff; review latest" if document["selected_current_payoff"] else document["role"]
            payoff = f"; reviewed amount {escape(str(document.get('reviewed_payoff_amount') or 'unknown'))}; date {escape(str(document.get('reviewed_payoff_date') or 'unknown'))}" if document["role"] == "payoff" else ""
            items.append(f"<li>{escape(label)}: {name} - {escape(document['status'])}{payoff}</li>")
        sections.append(f"<section><h2>{escape(debt_heading(debt, number))}</h2><p>{escape(loan_summary_text(debt))}</p><ul>{''.join(items) or '<li>No linked supporting documents.</li>'}</ul></section>")
    return '<!doctype html><html><head><meta charset="utf-8"><title>Debt supporting documents</title></head><body><h1>Debt supporting documents</h1><p>Sources belong to the accepted saved schedule. Images and older payoff letters are supporting evidence, not an automatic current payoff.</p>' + "".join(sections) + "</body></html>"


def write_indexes(archive, manifest, archived, failed_ids=()):
    result = delivery_manifest(manifest, archived, failed_ids)
    archive.writestr("debt-evidence.json", json.dumps(result, ensure_ascii=False, indent=2))
    archive.writestr("debt-evidence.txt", index_text(result))
    archive.writestr("debt-evidence.html", index_html(result))
    return result
