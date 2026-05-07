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
import re
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
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.schemas.prequal import (
    PrequalRequestApprove,
    PrequalRequestCreate,
    PrequalRequestRead,
    PrequalRequestReject,
    PrequalRequestStartCreate,
)
from app.services import prequal_pdf

router = APIRouter(tags=["prequal"])
log = logging.getLogger(__name__)


# ── helpers ─────────────────────────────────────────────────────────────

# Per-product LTV ceilings the underwriter is bound by on approve.
LTV_CAPS: dict[str, float] = {"dscr": 0.80, "bridge": 0.85}


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


async def _find_or_spawn_loan_for_request(
    payload: PrequalRequestStartCreate,
    user,
    db: AsyncSession,
) -> Loan:
    """Used by the no-loan-yet POST. If the borrower already has a loan
    at the same property, attach to that one. Otherwise spawn a new
    Loan in stage=PREQUALIFIED so the operator pipeline picks it up."""
    if user.client is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only borrowers with a client record can submit a pre-qualification request.",
        )

    # Try to attach to an existing loan at the same property.
    target_norm = _normalize_address(payload.target_property_address)
    if target_norm:
        existing = (
            await db.execute(
                select(Loan).where(Loan.client_id == user.client.id)
            )
        ).scalars().all()
        for loan in existing:
            if _normalize_address(loan.address) == target_norm:
                return loan

    # No match — spawn a new stub. Stage PREQUALIFIED is the default and
    # also the perfect semantic match for this flow.
    loan_type = (
        LoanType.DSCR if payload.loan_type == "dscr" else LoanType.BRIDGE
    )
    loan = Loan(
        deal_id=_gen_deal_id(),
        client_id=user.client.id,
        broker_id=getattr(user.client, "broker_id", None),
        address=payload.target_property_address,
        property_type=PropertyType.SFR,
        type=loan_type,
        purpose=LoanPurpose.PURCHASE,
        stage=LoanStage.PREQUALIFIED,
        amount=payload.requested_loan_amount,
    )
    db.add(loan)
    await db.flush()
    await db.refresh(loan)
    log.info(
        "prequal.spawned_loan deal_id=%s client_id=%s address=%s",
        loan.deal_id, user.client.id, payload.target_property_address,
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
    loan: Loan,
    payload: PrequalRequestCreate,
    user,
    db: AsyncSession,
) -> PrequalRequest:
    req = PrequalRequest(
        loan_id=loan.id,
        requester_id=user.id,
        target_property_address=payload.target_property_address,
        purchase_price=payload.purchase_price,
        requested_loan_amount=payload.requested_loan_amount,
        loan_type=payload.loan_type,
        expected_closing_date=payload.expected_closing_date,
        borrower_notes=payload.borrower_notes,
        status="pending",
    )
    db.add(req)
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
    """Borrower submits a pre-qual request for a property they don't yet have
    a loan record for. Backend spawns or attaches to a Loan."""
    loan = await _find_or_spawn_loan_for_request(payload, user, db)
    req = await _create_request(loan, payload, user, db)
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
    if req.status not in {"pending", "approved"}:
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

    # Snapshot the approved figures + reviewer.
    req.approved_purchase_price = payload.approved_purchase_price
    req.approved_loan_amount = payload.approved_loan_amount
    req.admin_notes = payload.admin_notes
    req.status = "approved"
    req.reviewed_by = user.id
    req.reviewed_at = datetime.now(timezone.utc)
    await db.flush()

    # Render + upload the PDF. If this throws we want to leave status as
    # approved already — the admin can re-approve to re-render — but
    # surface the failure to the caller.
    loan = await db.get(Loan, req.loan_id)
    if loan is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Loan disappeared")

    settings = get_settings()
    try:
        s3_key = prequal_pdf.render_letter(req, loan, settings=settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("prequal_pdf render/upload failed for request %s", req.id)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Approval saved but PDF rendering failed. Re-click Approve to retry.",
        ) from exc

    req.pdf_s3_key = s3_key
    await db.flush()

    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=user.id,
            actor_label=user.role,
            kind="prequal.approved",
            summary=f"Pre-qualification approved at {ltv * 100:.0f}% LTV — {req.target_property_address}",
            payload={
                "request_id": str(req.id),
                "s3_key": s3_key,
                "approved_purchase_price": float(req.approved_purchase_price),
                "approved_loan_amount": float(req.approved_loan_amount),
                "ltv": round(ltv, 4),
                "loan_type": req.loan_type,
                "admin_notes": req.admin_notes,
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
