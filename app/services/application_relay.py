"""The signed application as the relay copy for lenders and partners.

The real-estate application's information-sharing consent authorizes Qualified
Commercial to share the application and the file with its lending partners and
associates and with any prospective lender. Once every signer has signed, that
consent:

- answers each lender's own sharing requirement (lender_sharing_{id}) with an
  accepted response whose basis is the signed application, so
  submission_batches.sharing_is_authorized stays the single gate. The response
  records the words the client actually signed (not the lender requirement's
  own disclosure) and names the disclosure it satisfies, which
  collection_tasks.materialize_response_state accepts from a signed form only.
  The client's own answer to that requirement stands: a refusal is never
  overridden, and a current grant needs nothing more;
- travels in every lender package (submission_batches.package_material);
- lets the desk share that one file with a partner who is not on the lender
  roster, through a seven-day passcode link logged on the client's timeline.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationRequirementResponse, ApplicationRequirementState
from app.models.bucket import BucketFile
from app.models.client_form import ClientFormVersion
from app.models.form_signing import FormSigningSession
from app.services import form_kinds

log = logging.getLogger(__name__)

#: ApplicationRequirementResponse.source for answers that come from a signed form.
SOURCE = "signed_form"
SHARING_CONSENT = "information_sharing"
SHARE_DAYS = 7
#: What a lender reads about the basis (internal record ids stay in our provenance).
LENDER_BASIS_KEYS = ("kind", "form_version", "signers", "signed_at", "executed_file_sha256", "consent_version")


@dataclass(frozen=True)
class SignedApplication:
    session: FormSigningSession
    version: ClientFormVersion | None
    file: BucketFile
    #: The information-sharing consent as signed ({key, label, version, sha256}),
    #: or None when the signature did not include it.
    sharing: dict[str, Any] | None

    @property
    def signers(self) -> list[dict[str, Any]]:
        recorded = self.session.signers or {}
        return [recorded[role] for role in self.session.required_roles if isinstance(recorded.get(role), dict)]

    @property
    def signer_names(self) -> list[str]:
        return [str(item.get("name")).strip() for item in self.signers if str(item.get("name") or "").strip()]


async def executed_application(db: AsyncSession, profile_id: Any) -> SignedApplication | None:
    """The latest fully signed application on the file whose signed copy is still on file."""
    sessions = (
        await db.execute(
            select(FormSigningSession)
            .where(
                FormSigningSession.profile_id == profile_id,
                FormSigningSession.status == "executed",
                FormSigningSession.executed_file_id.is_not(None),
            )
            .order_by(FormSigningSession.executed_at.desc(), FormSigningSession.created_at.desc())
        )
    ).scalars().all()
    for session in sessions:
        file = await db.get(BucketFile, session.executed_file_id)
        if file is None or file.deleted_at is not None or file.status != "uploaded":
            continue
        snapshot = (session.signature_config or {}).get("consent_snapshot") or []
        sharing = next((dict(item) for item in snapshot if isinstance(item, dict) and item.get("key") == SHARING_CONSENT), None)
        return SignedApplication(session=session, version=await db.get(ClientFormVersion, session.version_id), file=file, sharing=sharing)
    return None


def signed_sharing_text(sharing: dict[str, Any] | None) -> str | None:
    """The consent words as signed, when today's registry still holds that exact text."""
    consent = form_kinds.consent(SHARING_CONSENT)
    if sharing and consent is not None and consent.sha256 == sharing.get("sha256"):
        return consent.text
    return None


def basis(signed: SignedApplication) -> dict[str, Any]:
    session = signed.session
    return {
        "kind": "signed_application",
        "form_kind": session.kind,
        "form_version": signed.version.number if signed.version else None,
        "signing_session_id": str(session.id),
        "signature_ids": [str(item["signature_id"]) for item in signed.signers if item.get("signature_id")],
        "signers": signed.signer_names,
        "signed_at": session.executed_at.isoformat() if session.executed_at else None,
        "executed_file_id": str(signed.file.id),
        "executed_file_sha256": signed.file.content_hash,
        "consent_key": SHARING_CONSENT,
        "consent_version": (signed.sharing or {}).get("version"),
        "consent_sha256": (signed.sharing or {}).get("sha256"),
    }


def lender_basis(provenance: dict[str, Any] | None) -> dict[str, Any] | None:
    """The part of a signed-form answer's basis a lender reads in the package manifest."""
    found = (provenance or {}).get("basis")
    if not isinstance(found, dict):
        return None
    return {key: found.get(key) for key in LENDER_BASIS_KEYS}


async def answer_lender_sharing(db: AsyncSession, profile: Any, requirement_key: str) -> ApplicationRequirementResponse | None:
    """Answer one lender's sharing requirement from the signed application, when it can.

    Returns the new response, or None when nothing was recorded: no signed
    application with the sharing consent, a requirement that is not an
    approved consent, the client's own answer already standing, or this
    signed application already answering the current disclosure.
    """
    from app.services.collection_tasks import disclosure_hash

    signed = await executed_application(db, profile.id)
    if signed is None or signed.sharing is None:
        return None
    state = (
        await db.execute(
            select(ApplicationRequirementState).where(
                ApplicationRequirementState.profile_id == profile.id,
                ApplicationRequirementState.requirement_key == requirement_key,
            )
        )
    ).scalar_one_or_none()
    if state is None or state.status in {"waived", "not_applicable"}:
        return None
    metadata = state.provenance or {}
    config = metadata.get("collection_config") or {}
    if metadata.get("requirement_kind") != "consent" or not config.get("disclosure_text") or not config.get("disclosure_version"):
        return None
    satisfies = disclosure_hash(config)
    definition_version = str(metadata.get("definition_version") or "1")
    latest = (
        await db.execute(
            select(ApplicationRequirementResponse)
            .where(ApplicationRequirementResponse.requirement_state_id == state.id)
            .order_by(ApplicationRequirementResponse.created_at.desc(), ApplicationRequirementResponse.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        recorded = latest.provenance or {}
        if latest.source != SOURCE:
            answer = latest.value.get("answer") if isinstance(latest.value, dict) else None
            granted = isinstance(answer, dict) and answer.get("granted") is True
            if not granted or state.status == "verified":
                return None
        elif (
            recorded.get("satisfies_disclosure_hash") == satisfies
            and recorded.get("definition_id") == metadata.get("definition_id")
            and latest.source_version == definition_version
            and (recorded.get("basis") or {}).get("signing_session_id") == str(signed.session.id)
        ):
            return None
    now = datetime.now(UTC)
    response = ApplicationRequirementResponse(
        profile_id=profile.id,
        requirement_state_id=state.id,
        value={"answer": {"granted": True, "consenter_name": (", ".join(signed.signer_names) or "Signed application")[:160], "basis": "signed_application"}},
        source=SOURCE,
        source_version=definition_version,
        review_state="accepted",
        reviewed_at=now,
        provenance={
            "definition_id": metadata.get("definition_id"),
            # What the client signed: the information-sharing consent's own words.
            "disclosure_text": signed_sharing_text(signed.sharing),
            "disclosure_version": signed.sharing.get("version"),
            "disclosure_hash": signed.sharing.get("sha256"),
            "scope": config.get("scope"),
            # The lender requirement's disclosure this answer satisfies.
            "satisfies_disclosure_hash": satisfies,
            "satisfies_disclosure_version": config.get("disclosure_version"),
            "basis": basis(signed),
        },
    )
    db.add(response)
    await db.flush()
    return response


async def share_with_partner(
    db: AsyncSession,
    profile: Any,
    *,
    actor: Any,
    recipient_name: str,
    organization: str | None = None,
    recipient_email: str | None = None,
    request: Any = None,
) -> dict[str, Any]:
    """A seven-day passcode link to the signed application only, for a partner off the roster.

    The passcode is returned once (only its hash is stored); the client's
    timeline says who received it.
    """
    from app.models.bucket import BucketShare
    from app.models.file_event import VISIBILITY_CLIENT
    from app.routers.buckets import _generate_passcode, _hash_passcode, _log, _public_url
    from app.services import file_events
    from app.services.local_time import local_stamp

    signed = await executed_application(db, profile.id)
    if signed is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "not_signed", "message": "Everyone has to sign the application before it can be shared."})
    if signed.sharing is None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "sharing_not_authorized", "message": "The signed application does not include the information-sharing consent, so it cannot be shared from here."})
    name = " ".join(str(recipient_name or "").split())[:180]
    org = " ".join(str(organization or "").split())[:180] or None
    if len(name) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Name the person receiving the application.")
    who = f"{name}, {org}"[:180] if org else name
    passcode = _generate_passcode()
    share = BucketShare(
        bucket_id=signed.file.bucket_id,
        token=secrets.token_urlsafe(32),
        recipient_name=who,
        recipient_email=str(recipient_email).strip().lower()[:320] if recipient_email else None,
        passcode_hash=_hash_passcode(passcode),
        can_preview=True,
        can_download=True,
        can_add_notes=False,
        can_upload=False,
        can_see_internal_notes=False,
        can_use_ai_chat=False,
        can_view_ai_summary=False,
        can_view_ai_tasks=False,
        can_propose_tasks=False,
        expires_at=datetime.now(UTC) + timedelta(days=SHARE_DAYS),
    )
    share.files = [signed.file]
    db.add(share)
    await db.flush()
    await _log(
        db, share.bucket_id, "share_created", request=request, user=actor,
        target_type="share", target_id=str(share.id), detail=f"Signed real estate application to {who}"[:500],
    )
    ends = local_stamp(share.expires_at, fmt="%b %-d", label=False)
    await file_events.emit(
        db, profile=profile, kind="application.shared", visibility=VISIBILITY_CLIENT,
        title=f"Your application was shared with {org or name}"[:200],
        body=f"We shared your signed real estate loan application with {who} for review, under the information-sharing consent you signed. Their access ends {ends}.",
        actor=actor, target_type="bucket_share", target_id=share.id,
        meta={"recipient": who, "file_id": str(signed.file.id), "form_version": signed.version.number if signed.version else None},
    )
    return {
        "share_id": str(share.id),
        "share_url": _public_url(f"/buckets/share/{share.token}"),
        "passcode": passcode,
        "expires_at": share.expires_at.isoformat(),
        "recipient": who,
        "file_name": signed.file.file_name,
        "form_version": signed.version.number if signed.version else None,
    }


async def relay_read(db: AsyncSession, profile_id: Any) -> dict[str, Any]:
    """For the desk: the signed copy that is relayed, and every partner link made to a signed version."""
    from app.models.bucket import BucketShare, bucket_share_files

    signed = await executed_application(db, profile_id)
    executed_ids = select(FormSigningSession.executed_file_id).where(
        FormSigningSession.profile_id == profile_id, FormSigningSession.executed_file_id.is_not(None)
    )
    shares = (
        await db.execute(
            select(BucketShare)
            .join(bucket_share_files, bucket_share_files.c.share_id == BucketShare.id)
            .where(bucket_share_files.c.file_id.in_(executed_ids))
            .order_by(BucketShare.created_at.desc())
            .limit(20)
        )
    ).scalars().unique().all()
    now = datetime.now(UTC)

    def active(share: Any) -> bool:
        ends = share.expires_at
        if ends is not None and ends.tzinfo is None:
            ends = ends.replace(tzinfo=UTC)
        return share.status == "active" and (ends is None or ends > now)

    return {
        "signed": {
            "form_version": signed.version.number if signed.version else None,
            "signed_at": signed.session.executed_at.isoformat() if signed.session.executed_at else None,
            "file_name": signed.file.file_name,
            "sharing_consent": signed.sharing is not None,
        } if signed else None,
        "shares": [
            {
                "id": str(share.id),
                "recipient": share.recipient_name,
                "created_at": share.created_at.isoformat() if share.created_at else None,
                "expires_at": share.expires_at.isoformat() if share.expires_at else None,
                "active": active(share),
                "views": share.view_count,
                "downloads": share.download_count,
            }
            for share in shares
        ],
    }
