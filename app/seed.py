"""Seed script — loads design data into the DB so the frontends have real data.

Source of truth: .design/qualified-commercial/project/desktop/data-desk.js (operator)
                  .design/qualified-commercial/project/data.js (mobile)

This script translates the JS shapes into ORM rows. It is idempotent —
re-running drops and reseeds.

Usage:
  python -m uv run python -m app.seed
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete

from app.db import SessionLocal
from app.enums import (
    AITaskPriority,
    AITaskSource,
    BrokerTier,
    CalendarEventKind,
    DocStatus,
    LoanStage,
    LoanType,
    MessageFrom,
    PropertyType,
    Role,
)
from app.models.activity import Activity
from app.models.ai_task import AITask
from app.models.broker import Broker
from app.models.client import Client
from app.models.document import Document
from app.models.event import CalendarEvent
from app.models.hud import HudLineItem
from app.models.loan import Loan
from app.models.message import Message
from app.models.rate_sheet import RateSheetEntry
from app.models.user import User
from app.models.vector_log import VectorLog
from app.services.ai.vector_store import log_event as vector_log
from app.services.hud_template import build_hud_draft
from app.services.math import pricing_quote


def _today() -> date:
    return date.today()


async def seed() -> None:
    async with SessionLocal() as db:
        # Wipe in FK-safe order
        for tbl in [
            VectorLog,
            CalendarEvent,
            AITask,
            Message,
            Activity,
            Document,
            HudLineItem,
            Loan,
            RateSheetEntry,
            Client,
            Broker,
            User,
        ]:
            await db.execute(delete(tbl))
        await db.flush()

        # Users (one per role + a borrower-specific user + the real workspace
        # users that the Gmail relay impersonates).
        admin = User(clerk_id="dev-admin", email="admin@qc.dev", name="Asha Patel", role=Role.SUPER_ADMIN)
        ae = User(clerk_id="dev-ae", email="daniel@qc.dev", name="Daniel Reyes", role=Role.BROKER)
        uw = User(clerk_id="dev-uw", email="priya@qc.dev", name="Priya Singh", role=Role.LOAN_EXEC)
        client_user = User(
            clerk_id="dev-client", email="marcus@qc.dev", name="Marcus Holloway", role=Role.CLIENT
        )
        # Real Workspace users — selectable from the Loan Detail "Participants"
        # super_admin dropdown. Match the GMAIL_DELEGATED_USER + admins-on-BCC.
        franco = User(
            clerk_id="dev-franco",
            email="franco@qualifiedcommercial.com",
            name="Franco",
            role=Role.SUPER_ADMIN,
        )
        denny = User(
            clerk_id="dev-denny",
            email="denny@qualifiedcommercial.com",
            name="Denny",
            role=Role.SUPER_ADMIN,
        )
        db.add_all([admin, ae, uw, client_user, franco, denny])
        await db.flush()

        # Brokers
        b_daniel = Broker(
            user_id=ae.id,
            display_name="Daniel Reyes",
            tier=BrokerTier.GOLD,
            joined=date(2024, 3, 12),
            lifetime_points=18_400_000,
            funded_total=18_400_000,
            funded_count=22,
            balance_points=2_500_000,
        )
        db.add(b_daniel)
        b_jordan = Broker(
            user_id=admin.id,
            display_name="Jordan Lee",
            tier=BrokerTier.SILVER,
            joined=date(2024, 8, 1),
            lifetime_points=8_200_000,
            funded_total=8_200_000,
            funded_count=11,
        )
        db.add(b_jordan)
        await db.flush()

        # Clients (7 per data-desk.js pattern)
        clients_seed = [
            ("Marcus Holloway", "marcus@qc.dev", "Tier II", 712, "Brooklyn, NY", date(2024, 6, 1), b_daniel.id, client_user.id),
            ("Yusuf Adeyemi", "yusuf@qc.dev", "Standard", 698, "Atlanta, GA", date(2024, 9, 14), b_daniel.id, None),
            ("Daniel York", "daniel.y@qc.dev", "Tier II", 745, "Austin, TX", date(2024, 4, 22), b_daniel.id, None),
            ("Sofia Nakamura", "sofia@qc.dev", "Tier I", 762, "San Francisco, CA", date(2024, 1, 30), b_jordan.id, None),
            ("Reza Karimi", "reza@qc.dev", "Standard", 685, "Houston, TX", date(2024, 11, 4), b_jordan.id, None),
            ("Lena Whitfield", "lena@qc.dev", "Tier I", 778, "Miami, FL", date(2023, 12, 8), b_daniel.id, None),
            ("Aisha Carter", "aisha@qc.dev", "Standard", 702, "Charlotte, NC", date(2025, 2, 17), b_jordan.id, None),
        ]
        clients_objs: list[Client] = []
        for name, email, tier, fico, city, since, broker_id, user_id in clients_seed:
            c = Client(
                name=name,
                email=email,
                phone="+1-555-0100",
                tier=tier,
                fico=fico,
                city=city,
                since=since,
                broker_id=broker_id,
                user_id=user_id,
                avatar_color="petrol",
            )
            db.add(c)
            clients_objs.append(c)
        await db.flush()

        # Loans — 15 across 6 stages, mirroring data-desk.js
        loan_specs = [
            # (deal_id, client_idx, address, city, type, amount, stage, ltv, base_rate, points, term, dscr, monthly_rent)
            ("L-2604", 0, "418 Sycamore Ave", "Brooklyn, NY", LoanType.DSCR, 1_240_000, LoanStage.PROCESSING, 0.75, 0.0775, 1.0, 360, 1.32, 9_500),
            ("L-2598", 0, "207 Highline Pl", "Brooklyn, NY", LoanType.DSCR, 3_400_000, LoanStage.PROCESSING, 0.70, 0.0795, 0.5, 360, 1.18, 22_000),
            ("L-2587", 1, "1320 Peach Tree", "Atlanta, GA", LoanType.FIX_AND_FLIP, 425_000, LoanStage.CLOSING, 0.80, 0.0925, 0.0, 12, None, None),
            ("L-2581", 2, "612 Lakeshore Dr", "Austin, TX", LoanType.DSCR, 875_000, LoanStage.LENDER_CONNECTED, 0.78, 0.0780, 1.5, 360, 1.41, 6_800),
            ("L-2575", 3, "44 Marina Blvd", "San Francisco, CA", LoanType.GROUND_UP, 2_100_000, LoanStage.COLLECTING_DOCS, 0.65, 0.1050, 0.0, 18, None, None),
            ("L-2570", 4, "905 Lone Star Rd", "Houston, TX", LoanType.BRIDGE, 540_000, LoanStage.PREQUALIFIED, 0.70, 0.0995, 0.0, 12, None, None),
            ("L-2562", 5, "78 Coastline Ave", "Miami, FL", LoanType.DSCR, 1_750_000, LoanStage.FUNDED, 0.72, 0.0760, 2.0, 360, 1.55, 13_200),
            ("L-2558", 6, "211 Queens Rd", "Charlotte, NC", LoanType.FIX_AND_FLIP, 380_000, LoanStage.COLLECTING_DOCS, 0.82, 0.0935, 0.0, 12, None, None),
            ("L-2550", 0, "55 Atlantic Ter", "Brooklyn, NY", LoanType.DSCR, 920_000, LoanStage.FUNDED, 0.75, 0.0770, 1.0, 360, 1.28, 7_400),
            ("L-2545", 2, "1701 Congress Ave", "Austin, TX", LoanType.DSCR, 1_180_000, LoanStage.CLOSING, 0.74, 0.0775, 1.0, 360, 1.24, 9_100),
            ("L-2540", 3, "98 Pacific Heights", "San Francisco, CA", LoanType.DSCR, 2_600_000, LoanStage.PROCESSING, 0.70, 0.0790, 0.5, 360, 1.36, 18_500),
            ("L-2532", 1, "224 Ponce de Leon", "Atlanta, GA", LoanType.GROUND_UP, 1_400_000, LoanStage.LENDER_CONNECTED, 0.65, 0.1025, 0.0, 18, None, None),
            ("L-2525", 4, "612 Bayou Ln", "Houston, TX", LoanType.FIX_AND_FLIP, 295_000, LoanStage.PREQUALIFIED, 0.85, 0.0950, 0.0, 12, None, None),
            ("L-2517", 5, "11 Ocean Dr", "Miami, FL", LoanType.BRIDGE, 1_900_000, LoanStage.PROCESSING, 0.68, 0.0985, 0.0, 12, None, None),
            ("L-2510", 6, "405 Independence", "Charlotte, NC", LoanType.DSCR, 660_000, LoanStage.COLLECTING_DOCS, 0.77, 0.0790, 0.5, 360, 1.21, 5_100),
        ]
        loans_objs: list[Loan] = []
        for deal_id, ci, addr, city, ltype, amt, stage, ltv, br, pts, term, dscr_v, rent in loan_specs:
            close = _today() + timedelta(days=int(15 + ci * 4))
            qty = pricing_quote(br, amt, pts)
            loan = Loan(
                deal_id=deal_id,
                client_id=clients_objs[ci].id,
                broker_id=clients_objs[ci].broker_id,
                address=addr,
                city=city,
                property_type=PropertyType.SFR,
                annual_taxes=8_400,
                annual_insurance=2_200,
                type=ltype,
                stage=stage,
                amount=amt,
                ltv=ltv,
                arv=(amt / 0.65) if ltype in {LoanType.FIX_AND_FLIP, LoanType.GROUND_UP, LoanType.BRIDGE} else None,
                base_rate=br,
                discount_points=pts,
                final_rate=qty.final_rate,
                term_months=term,
                monthly_rent=rent,
                dscr=dscr_v,
                risk_score=72 if dscr_v else 65,
                close_date=close,
            )
            db.add(loan)
            loans_objs.append(loan)
        await db.flush()

        # Seed HUD per loan
        for loan in loans_objs:
            qty = pricing_quote(float(loan.base_rate), float(loan.amount), float(loan.discount_points))
            hud = build_hud_draft(
                loan_amount=float(loan.amount),
                property_type=PropertyType(loan.property_type),
                loan_type=LoanType(loan.type),
                broker_origination_dollars=qty.broker_origination_dollars,
            )
            for item in hud.items:
                db.add(
                    HudLineItem(
                        loan_id=loan.id,
                        code=item.code,
                        label=item.label,
                        amount=item.amount,
                        category=item.category,
                    )
                )

        # A few documents per loan
        doc_template = [
            ("Insurance Binder", "property", DocStatus.REQUESTED),
            ("Operating Agreement", "entity", DocStatus.RECEIVED),
            ("Personal Financial Statement", "financials", DocStatus.PENDING),
            ("Lease Schedule", "property", DocStatus.VERIFIED),
        ]
        for loan in loans_objs[:8]:
            for name, cat, st in doc_template:
                db.add(
                    Document(
                        loan_id=loan.id,
                        name=name,
                        category=cat,
                        status=st,
                        requested_on=_today() - timedelta(days=12),
                        received_on=_today() - timedelta(days=2) if st == DocStatus.RECEIVED else None,
                    )
                )

        # AI tasks
        ai_specs = [
            (AITaskSource.UNDERWRITING, AITaskPriority.HIGH, "request_doc", "Insurance binder needed for L-2604", "Underwriter flagged missing binder before issuing CTC.", 0.92, "uw-agent"),
            (AITaskSource.MESSAGES, AITaskPriority.HIGH, "draft_reply", "Lender follow-up on rate-lock for L-2598", "Lender asking if borrower wants to extend lock 7 days @ 8 bps.", 0.88, "msg-agent"),
            (AITaskSource.RISK, AITaskPriority.MEDIUM, "flag_inconsistency", "PFS / bank stmt mismatch on L-2587", "Reported liquid assets diverge from bank statement deposits.", 0.81, "risk-agent"),
            (AITaskSource.CALENDAR, AITaskPriority.MEDIUM, "book_appointment", "Schedule appraisal for L-2581", "Appraiser availability narrowed to Wed/Thu next week.", 0.78, "cal-agent"),
            (AITaskSource.DOCUMENTS, AITaskPriority.LOW, "auto_verify", "Auto-verify lease schedule for L-2562", "Confidence above 0.95 → eligible for auto-verify.", 0.95, "doc-agent"),
            (AITaskSource.PIPELINE, AITaskPriority.MEDIUM, "stage_advance", "Advance L-2545 to Closing", "All conditions cleared; CD has been issued.", 0.86, "pipeline-agent"),
            (AITaskSource.RATES, AITaskPriority.LOW, "rate_alert", "DSCR 80% LTV up 12 bps — repricing window", "Lender adjusted base rate sheet this morning.", 0.74, "rates-agent"),
            (AITaskSource.MESSAGES, AITaskPriority.HIGH, "draft_reply", "Borrower asking about reserves on L-2587", "Borrower wants to know reserve requirement clarification.", 0.83, "msg-agent"),
        ]
        for src, pri, action, title, summary, conf, agent in ai_specs:
            db.add(
                AITask(
                    loan_id=loans_objs[0].id if "L-2604" in title else loans_objs[1].id if "L-2598" in title else loans_objs[2].id,
                    source=src,
                    priority=pri,
                    action=action,
                    title=title,
                    summary=summary,
                    confidence=conf,
                    agent=agent,
                )
            )

        # Calendar events for the next 14 days
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for i, (loan_idx, kind, title, who, hour, dur) in enumerate(
            [
                (0, CalendarEventKind.CALL, "UW review — Highline rent roll", "Marisol Vega", 9, 30),
                (1, CalendarEventKind.CALL, "UW call — 418 Sycamore", "Marisol Vega", 10, 30),
                (2, CalendarEventKind.DOC, "Insurance binder due", "Borrower", 14, 60),
                (3, CalendarEventKind.AI, "AI: refinance window opens", "AI", 16, 15),
                (4, CalendarEventKind.LOCK, "Rate lock expires", "Lender", 17, 0),
                (5, CalendarEventKind.CLOSING, "Closing — 78 Coastline", "Title Co.", 12, 90),
            ]
        ):
            db.add(
                CalendarEvent(
                    loan_id=loans_objs[loan_idx].id,
                    kind=kind,
                    title=title,
                    who=who,
                    starts_at=(now + timedelta(days=i, hours=hour - now.hour)).replace(minute=0),
                    duration_min=dur,
                    priority=AITaskPriority.HIGH if kind == CalendarEventKind.CLOSING else AITaskPriority.MEDIUM,
                )
            )

        # A couple of messages on loan #1
        db.add_all(
            [
                Message(
                    loan_id=loans_objs[0].id,
                    from_role=MessageFrom.AI,
                    body="Drafted reply to UW about insurance binder.",
                    is_draft=True,
                ),
                Message(
                    loan_id=loans_objs[0].id,
                    from_role=MessageFrom.LENDER,
                    body="Need binder before we can clear conditions.",
                ),
                Message(
                    loan_id=loans_objs[0].id,
                    from_role=MessageFrom.CLIENT,
                    body="On it — emailing my agent today.",
                ),
            ]
        )

        # Rate sheet
        for sku, ltype, label, rate, ltv in [
            ("FF-90", LoanType.FIX_AND_FLIP, "Fix & Flip 90% LTC", 0.0925, 0.90),
            ("FF-85", LoanType.FIX_AND_FLIP, "Fix & Flip 85% LTC", 0.0895, 0.85),
            ("GU-85", LoanType.GROUND_UP, "Ground Up 85% LTC", 0.1025, 0.85),
            ("GU-80", LoanType.GROUND_UP, "Ground Up 80% LTC", 0.0995, 0.80),
            ("DSCR-80", LoanType.DSCR, "DSCR 80% LTV", 0.0785, 0.80),
            ("DSCR-75", LoanType.DSCR, "DSCR 75% LTV (Cash-Out)", 0.0810, 0.75),
            ("BR-75", LoanType.BRIDGE, "Bridge 75% LTV", 0.0985, 0.75),
            ("BR-65", LoanType.BRIDGE, "Bridge 65% LTV", 0.0935, 0.65),
        ]:
            db.add(
                RateSheetEntry(
                    sku=sku,
                    loan_type=ltype,
                    label=label,
                    base_rate=rate,
                    delta_bps=0,
                    max_ltv=ltv,
                    term_months=360 if ltype == LoanType.DSCR else 12,
                    effective_date=_today(),
                )
            )

        # Vector log entries (one per loan)
        for loan in loans_objs:
            await vector_log(
                db,
                loan_id=loan.id,
                deal_id=loan.deal_id,
                kind="seed.loaded",
                content=f"Seeded {loan.deal_id} ({loan.type}) at stage {loan.stage}, amount ${float(loan.amount):,.0f}.",
            )

        await db.commit()
        print(
            f"Seeded {len(clients_objs)} clients, {len(loans_objs)} loans, "
            f"{len(ai_specs)} AI tasks, 8 rate-sheet SKUs."
        )


if __name__ == "__main__":
    asyncio.run(seed())
