"""Pre-qualification letter approval workflow.

Borrower submits → status=pending → operator reviews/edits → status=approved
(PDF rendered + uploaded to S3) | rejected (admin_notes saved as reason).

Endpoint surface:

  POST   /loans/{loan_id}/prequal-requests       borrower, attaches to existing loan
  POST   /prequal-requests                        borrower, ALSO spawns a Loan stub
  GET    /loans/{loan_id}/prequal-requests       per-loan list (scoped)
  GET    /me/prequal-requests                    borrower's own list
  GET    /admin/prequal-requests                 firm-wide queue (operator-only)
  PUT    /admin/prequal-requests/{id}/approve    render PDF + flip status
  PUT    /admin/prequal-requests/{id}/reject     flip status with required reason

LTV is enforced ONLY on approve (not on submit) — borrowers can ask for
whatever; the underwriter is the one bound by the matrix.
  DSCR:   80% cap → 0.80
  Bridge: 85% cap → 0.85
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.enums import LoanPurpose, LoanStage, LoanType, PropertyType, Role
from app.models.activity import Activity
from app.models.app_settings import AppSettings
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.models.user import User
from app.schemas.prequal import (
    PrequalRequestApprove,
    PrequalRequestCreate,
    PrequalRequestRead,
    PrequalRequestReject,
    PrequalRequestStartCreate,
    PrequalSellerOutcome,
)
from app.services import prequal_pdf

router = APIRouter(tags=["prequal"])
log = logging.getLogger(__name__)


# ── helpers ─────────────────────────────────────────────────────────────

# Per-product LTV ceilings the underwriter is bound by on approve.
# Note: DSCR refi runs tighter than purchase (5pt haircut) — typical
# secondary-market spread for cash-out / rate-and-term seasoning risk.
# Fix & Flip uses LTP (loan-to-purchase) here, not LTC + ARV — the prequal
# letter is for the OFFER stage so we deliberately don't expose ARV /
# rehab leverage that would tip the seller.
LTV_CAPS: dict[str, float] = {
    "dscr_purchase": 0.80,
    "dscr_refi": 0.75,
    "fix_flip": 0.85,
    "bridge": 0.85,
}


def _loan_type_to_enum(prequal_type: str) -> LoanType:
    """Map prequal sub-typed loan_type → canonical LoanType enum used by
    the loans table when the prequal converts on offer-accepted."""
    if prequal_type in ("dscr_purchase", "dscr_refi"):
        return LoanType.DSCR
    if prequal_type == "fix_flip":
        return LoanType.FIX_AND_FLIP
    return LoanType.BRIDGE


def _loan_purpose_for(prequal_type: str) -> LoanPurpose:
    """DSCR Refinance → RATE_TERM_REFI on the spawned loan; everything
    else is a PURCHASE."""
    if prequal_type == "dscr_refi":
        return LoanPurpose.RATE_TERM_REFI
    return LoanPurpose.PURCHASE


def _gen_deal_id() -> str:
    """Match the format used in routers/loans.py:_gen_deal_id."""
    return f"L-{secrets.randbelow(9000) + 1000}"


def _normalize_address(addr: str) -> str:
    """Cheap fuzzy-match key for 'do we already have a loan at this address'.
    Lowercase, strip non-alphanumerics, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", addr.lower())).strip()


def _to_read(req: PrequalRequest) -> PrequalRequestRead:
    """Convert ORM row to API shape, minting a fresh presigned URL on read.
    The URL is never stored — always fresh so it can't go stale in the
    borrower's UI between page loads."""
    settings = get_settings()
    pdf_url: str | None = None
    if req.pdf_s3_key and req.status == "approved":
        pdf_url = prequal_pdf.presign_get(req.pdf_s3_key, settings=settings)
    base = PrequalRequestRead.model_validate(req).model_dump()
    base["pdf_url"] = pdf_url
    return PrequalRequestRead(**base)


def _gen_quote_number() -> str:
    """Q-prefixed 4-digit quote ID — generated when the borrower marks
    the seller's offer as accepted and we promote the prequal to a
    Loan."""
    return f"Q-{secrets.randbelow(9000) + 1000}"


async def _spawn_loan_from_approved_request(
    request: PrequalRequest,
    db: AsyncSession,
) -> Loan:
    """Called when a prequal request flips to offer_accepted. Creates the
    actual Loan from the approved_scenario snapshot, attaches the request
    to it, and returns the new Loan. Pre-fills as much from the scenario
    as we have so the underwriter doesn't re-type values they already set."""
    # Resolve numbers — prefer approved_*, fall back to requested.
    purchase = float(request.approved_purchase_price or request.purchase_price)
    loan_amount = float(request.approved_loan_amount or request.requested_loan_amount)
    scenario = request.approved_scenario or {}

    loan_type_enum = _loan_type_to_enum(request.loan_type)
    loan_purpose = _loan_purpose_for(request.loan_type)

    # Look up the requesting user → client.
    user = (
        await db.execute(
            select(User).options(selectinload(User.client)).where(User.id == request.requester_id)
        )
    ).scalar_one_or_none()
    if user is None or user.client is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Cannot promote prequal — requester has no client record.",
        )

    loan = Loan(
        deal_id=_gen_deal_id(),
        client_id=user.client.id,
        broker_id=getattr(user.client, "broker_id", None),
        address=request.target_property_address,
        property_type=PropertyType.SFR,
        type=loan_type_enum,
        purpose=loan_purpose,
        # Stage starts at PREQUALIFIED — this IS the prequalified loan
        # entering the pipeline. Operator moves it forward from there.
        stage=LoanStage.PREQUALIFIED,
        amount=loan_amount,
        # Best-effort fill from scenario; numeric columns are decimals,
        # so leave them None when scenario doesn't have the field.
        ltv=scenario.get("ltv") if isinstance(scenario.get("ltv"), (int, float)) else None,
    )
    db.add(loan)
    await db.flush()
    await db.refresh(loan)
    log.info(
        "prequal.promoted_to_loan request_id=%s deal_id=%s amount=%s ltv=%s",
        request.id, loan.deal_id, loan_amount, loan.ltv,
    )
    return loan


def _scope_loan_for_borrower(loan: Loan, user) -> bool:
    """Borrower can only act on their own loans."""
    if user.role == Role.CLIENT and user.client:
        return loan.client_id == user.client.id
    return user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER}


def _is_operator(user) -> bool:
    return user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER}


async def _create_request(
    loan: Loan | None,
    payload: PrequalRequestCreate,
    user,
    db: AsyncSession,
) -> PrequalRequest:
    """Create a PrequalRequest. loan_id is OPTIONAL — None when this is a
    standalone request that hasn't been converted to a loan yet (which
    is the new default; loan_id is only set after offer_accepted)."""
    req = PrequalRequest(
        loan_id=loan.id if loan is not None else None,
        requester_id=user.id,
        target_property_address=payload.target_property_address,
        purchase_price=payload.purchase_price,
        requested_loan_amount=payload.requested_loan_amount,
        loan_type=payload.loan_type,
        expected_closing_date=payload.expected_closing_date,
        borrower_notes=payload.borrower_notes,
        borrower_entity=payload.borrower_entity,
        status="pending",
    )
    db.add(req)
    # Activity rows must have a loan_id (NOT NULL on activity.loan_id).
    # When the prequal isn't yet attached to a loan we skip the activity
    # log on submission — the prequal_requests row itself IS the audit
    # trail. We log activity again on approve and on offer_accepted,
    # both of which DO have a loan_id by then.
    if loan is not None:
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.role,
                kind="prequal.requested",
                summary=f"Pre-qualification requested for {payload.target_property_address}",
                payload={
                    "loan_type": payload.loan_type,
                    "purchase_price": float(payload.purchase_price),
                    "requested_loan_amount": float(payload.requested_loan_amount),
                    "expected_closing_date": (
                        payload.expected_closing_date.isoformat()
                        if payload.expected_closing_date else None
                    ),
                },
            )
        )
    await db.flush()
    await db.refresh(req)
    return req


# ── borrower endpoints ──────────────────────────────────────────────────


@router.post(
    "/loans/{loan_id}/prequal-requests",
    response_model=PrequalRequestRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_prequal_for_loan(
    loan_id: UUID,
    payload: PrequalRequestCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalRequestRead:
    """Borrower submits a pre-qual request against an existing loan."""
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    if not _scope_loan_for_borrower(loan, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot submit for this loan")
    req = await _create_request(loan, payload, user, db)
    return _to_read(req)


@router.post(
    "/prequal-requests",
    response_model=PrequalRequestRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_prequal_spawn(
    payload: PrequalRequestStartCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalRequestRead:
    """Borrower submits a standalone pre-qual request — NO loan record is
    created at this stage. Under the new lifecycle a Loan is only spawned
    after the borrower marks the seller's offer as accepted (see
    /me/prequal-requests/{id}/accept-offer)."""
    req = await _create_request(None, payload, user, db)
    return _to_read(req)


@router.get("/loans/{loan_id}/prequal-requests", response_model=list[PrequalRequestRead])
async def list_loan_prequal_requests(
    loan_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PrequalRequestRead]:
    loan = await db.get(Loan, loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    if not _scope_loan_for_borrower(loan, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot view requests for this loan")
    rows = (
        await db.execute(
            select(PrequalRequest)
            .where(PrequalRequest.loan_id == loan_id)
            .order_by(PrequalRequest.created_at.desc())
        )
    ).scalars().all()
    return [_to_read(r) for r in rows]


@router.get("/me/prequal-requests", response_model=list[PrequalRequestRead])
async def list_my_prequal_requests(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PrequalRequestRead]:
    """Borrower's own list across all their loans. Drives the badges in the
    simulator's My Loans tab."""
    rows = (
        await db.execute(
            select(PrequalRequest)
            .where(PrequalRequest.requester_id == user.id)
            .order_by(PrequalRequest.created_at.desc())
        )
    ).scalars().all()
    return [_to_read(r) for r in rows]


# ── admin endpoints ─────────────────────────────────────────────────────


@router.get("/admin/prequal-requests", response_model=list[PrequalRequestRead])
async def list_admin_prequal_queue(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(None, alias="status"),
) -> list[PrequalRequestRead]:
    """Firm-wide queue. Default sort: PENDING first, then by closing date
    (NULLS-last) so urgent ones float to the top."""
    if not _is_operator(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    stmt = select(PrequalRequest).options(selectinload(PrequalRequest.loan))
    if status_filter in {"pending", "approved", "rejected"}:
        stmt = stmt.where(PrequalRequest.status == status_filter)
    # Pending floats first; within a status group, oldest-closing-first
    # so the rush deals get attention.
    stmt = stmt.order_by(
        # pending first via a CASE expression keyed off status
        # (simpler than re-sorting in Python)
        PrequalRequest.status.asc(),  # 'approved' < 'pending' < 'rejected' alphabetically — fix below
        PrequalRequest.expected_closing_date.asc().nullslast(),
        PrequalRequest.created_at.desc(),
    )
    rows = (await db.execute(stmt)).scalars().all()
    # Stable Python-side sort to put pending on top regardless of alpha.
    rows = sorted(rows, key=lambda r: {"pending": 0, "approved": 1, "rejected": 2}.get(r.status, 9))
    return [_to_read(r) for r in rows]


@router.put(
    "/admin/prequal-requests/{request_id}/approve",
    response_model=PrequalRequestRead,
)
async def approve_prequal_request(
    request_id: UUID,
    payload: PrequalRequestApprove,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalRequestRead:
    if not _is_operator(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")

    req = await db.get(PrequalRequest, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    # Allowed statuses for re-issue:
    #   pending        — first approval, status flips to approved
    #   approved       — re-approval (admin tweaked numbers), letter regenerates
    #   offer_accepted — borrower already accepted; admin still wants to
    #                    correct the letter. Status stays as is, the
    #                    spawned Loan is left untouched (its values were
    #                    pre-filled from the original approved_scenario;
    #                    the operator updates the loan separately if it
    #                    needs to track new numbers).
    if req.status not in {"pending", "approved", "offer_accepted"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Request is {req.status}; cannot approve. Submit a new request instead.",
        )

    # LTV cap enforcement against the matrix.
    cap = LTV_CAPS.get(req.loan_type)
    if cap is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown loan_type: {req.loan_type}")
    if payload.approved_purchase_price <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_input", "message": "Approved purchase price must be > 0."},
        )
    ltv = payload.approved_loan_amount / payload.approved_purchase_price
    if ltv > cap + 1e-6:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ltv_exceeded",
                "message": (
                    f"Approved LTV is {ltv * 100:.1f}% but the {req.loan_type.upper()} "
                    f"matrix caps at {cap * 100:.0f}%. Lower the approved loan amount."
                ),
            },
        )

    # Snapshot the approved figures + scenario + reviewer + qualification#.
    req.approved_purchase_price = payload.approved_purchase_price
    req.approved_loan_amount = payload.approved_loan_amount
    req.admin_notes = payload.admin_notes
    req.approved_scenario = payload.approved_scenario
    # Admin may have corrected the LLC / entity name (e.g. borrower
    # submitted "TBD" originally and now has it). Override only when the
    # admin actually supplied a value — None means "leave whatever the
    # borrower submitted".
    if payload.borrower_entity is not None:
        req.borrower_entity = payload.borrower_entity
    # Don't downgrade a forward-state status. If the borrower already
    # marked the seller's offer as accepted, the prequal lives in
    # offer_accepted and editing the letter shouldn't bounce it back to
    # approved (which would un-link the spawned Loan from a UX
    # perspective and re-prompt the borrower to confirm acceptance).
    if req.status == "pending":
        req.status = "approved"
    req.reviewed_by = user.id
    req.reviewed_at = datetime.now(timezone.utc)
    # Generate the qualification # (Q-XXXX) on first approval. Re-approval
    # keeps the same number so the borrower's downloaded letter stays
    # consistent across edits.
    if not req.quote_number:
        req.quote_number = _gen_quote_number()
    await db.flush()

    # Render + upload the PDF. Under the new lifecycle the prequal
    # doesn't have a loan_id at approve time — that comes later, on
    # offer-accepted. render_letter handles both cases.
    settings = get_settings()
    loan = None
    if req.loan_id is not None:
        loan = await db.get(Loan, req.loan_id)
    # Load the firm-letterhead settings once; render_letter uses
    # officer_name / office_address / signature image from this row.
    settings_row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    # Pre-fetch the requester's individual legal name so the letter
    # can fall back to "Jane Doe and/or Assignee LLC (TBD)" when no
    # entity has been typed in. Pre-loan we have no loan.client to
    # follow; threading the name explicitly is the cleanest path.
    requester = (
        await db.execute(
            select(User).options(selectinload(User.client)).where(User.id == req.requester_id)
        )
    ).scalar_one_or_none()
    fallback_individual_name = (
        requester.client.name if requester and requester.client else None
    )
    try:
        s3_key = prequal_pdf.render_letter(
            req,
            loan,
            settings=settings,
            expiration_days=payload.expiration_days,
            quote_number=req.quote_number,
            settings_row=settings_row,
            fallback_individual_name=fallback_individual_name,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("prequal_pdf render/upload failed for request %s", req.id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Approval saved but PDF rendering failed. Re-click Approve to retry.",
        ) from exc

    req.pdf_s3_key = s3_key
    await db.flush()

    # Activity log lives on the loan (NOT NULL FK). Skip when no loan
    # is attached yet — the prequal_requests row is the audit until the
    # borrower accepts and we spawn one.
    if loan is not None:
        db.add(
            Activity(
                loan_id=loan.id,
                actor_id=user.id,
                actor_label=user.role,
                kind="prequal.approved",
                summary=f"Pre-qualification approved at {ltv * 100:.0f}% LTV — {req.target_property_address}",
                payload={
                    "request_id": str(req.id),
                    "quote_number": req.quote_number,
                    "s3_key": s3_key,
                    "approved_purchase_price": float(req.approved_purchase_price),
                    "approved_loan_amount": float(req.approved_loan_amount),
                    "ltv": round(ltv, 4),
                    "loan_type": req.loan_type,
                    "admin_notes": req.admin_notes,
                    "approved_scenario": req.approved_scenario,
                },
            )
        )
    await db.flush()
    await db.refresh(req)
    return _to_read(req)


@router.put(
    "/admin/prequal-requests/{request_id}/reject",
    response_model=PrequalRequestRead,
)
async def reject_prequal_request(
    request_id: UUID,
    payload: PrequalRequestReject,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalRequestRead:
    if not _is_operator(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")

    req = await db.get(PrequalRequest, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    if req.status not in {"pending", "rejected"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Request is {req.status}; cannot reject.",
        )

    req.status = "rejected"
    req.admin_notes = payload.admin_notes
    req.reviewed_by = user.id
    req.reviewed_at = datetime.now(timezone.utc)
    await db.flush()

    # Activity log only when a loan is attached. Pre-loan rejections
    # are audited via the prequal_requests row itself.
    if req.loan_id is not None:
        db.add(
            Activity(
                loan_id=req.loan_id,
                actor_id=user.id,
                actor_label=user.role,
                kind="prequal.rejected",
                summary=f"Pre-qualification rejected — {req.target_property_address}",
                payload={
                    "request_id": str(req.id),
                    "admin_notes": req.admin_notes,
                    "loan_type": req.loan_type,
                },
            )
        )
    await db.flush()
    await db.refresh(req)
    return _to_read(req)


# ── borrower offer-outcome endpoints ────────────────────────────────────
#
# Once approved, the borrower hands the letter to the seller. The seller
# either accepts the offer (we create a Loan + quote#) or declines (the
# prequal closes without a loan). The borrower reports back through these
# endpoints; super-admin can do it on the borrower's behalf.


async def _load_request_for_outcome(
    request_id: UUID, user, db: AsyncSession
) -> PrequalRequest:
    req = await db.get(PrequalRequest, request_id)
    if req is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    # Borrower can only act on their own; operators on any.
    if user.role == Role.CLIENT and req.requester_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot act on this request")
    if not (user.role == Role.CLIENT or _is_operator(user)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized")
    if req.status != "approved":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Request is {req.status}; only approved requests can be marked accepted/declined.",
        )
    return req


@router.put(
    "/me/prequal-requests/{request_id}/accept-offer",
    response_model=PrequalRequestRead,
)
async def borrower_accept_offer(
    request_id: UUID,
    payload: PrequalSellerOutcome,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalRequestRead:
    """Borrower confirms the seller accepted their offer. This is the
    moment we promote the prequal into a real Loan record in the
    pipeline, mint a Q-XXXX number on the loan, and surface it to ops."""
    req = await _load_request_for_outcome(request_id, user, db)

    loan = await _spawn_loan_from_approved_request(req, db)
    req.loan_id = loan.id
    req.status = "offer_accepted"
    if not req.quote_number:
        req.quote_number = _gen_quote_number()
    await db.flush()

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="prequal.offer_accepted",
            summary=(
                f"Seller accepted offer — Loan {loan.deal_id} created from prequal "
                f"{req.quote_number} at {req.target_property_address}"
            ),
            payload={
                "request_id": str(req.id),
                "quote_number": req.quote_number,
                "deal_id": loan.deal_id,
                "approved_purchase_price": float(req.approved_purchase_price or 0),
                "approved_loan_amount": float(req.approved_loan_amount or 0),
                "note": payload.note,
            },
        )
    )
    await db.flush()
    await db.refresh(req)
    return _to_read(req)


@router.put(
    "/me/prequal-requests/{request_id}/decline-offer",
    response_model=PrequalRequestRead,
)
async def borrower_decline_offer(
    request_id: UUID,
    payload: PrequalSellerOutcome,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PrequalRequestRead:
    """Borrower reports the seller declined / they walked away. The
    prequal closes; no loan is ever created from this request. Borrower
    can submit a new prequal for a different property."""
    req = await _load_request_for_outcome(request_id, user, db)
    req.status = "offer_declined"
    await db.flush()
    # Pre-loan, no Activity log target — the prequal_requests row IS the
    # audit trail. Just refresh and return.
    await db.refresh(req)
    return _to_read(req)
