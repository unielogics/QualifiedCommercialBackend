"""Create review drafts for the unpublished funding programs.

Run via:
    python -m app.scripts.ensure_program_review_drafts --user-email <email> [--dry-run]
    docker exec qcbackend python -m app.scripts.ensure_program_review_drafts \
        --user-email <email> --dry-run

For every active catalog program with no published criteria version (except
the hand-authored ``arf_*`` programs and any program whose latest draft already
has requirement rows) this creates ONE new draft version attributed to the
named super admin, built from the legacy deterministic ``fit`` rules when the
latest draft has them and otherwise from the 2026-09-27 site baseline, with
the baseline's requirement rows. Nothing is ever published: review and publish
from the funding programs editor.

``--dry-run`` prints the same plan and writes nothing. Re-running without it
is idempotent because the created draft then carries requirement rows and is
skipped as human-reviewable.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from sqlalchemy import func, select

from app.db import SessionLocal
from app.enums import Role
from app.models.user import User
from app.services import funding_program_drafts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.ensure_program_review_drafts",
        description="Create review drafts for unpublished funding programs (never publishes).",
    )
    parser.add_argument(
        "--user-email",
        required=True,
        help="Super admin the drafts and their audit entries are attributed to.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and write nothing.",
    )
    parser.add_argument(
        "--program-key",
        action="append",
        default=[],
        metavar="KEY",
        help="Restrict to one program key (repeatable). Default: every active program.",
    )
    return parser


def _print_plan(plan: list[funding_program_drafts.ReviewDraftPlan]) -> None:
    creates = [entry for entry in plan if entry.action == "create"]
    skips = [entry for entry in plan if entry.action != "create"]
    print(f"Plan: {len(creates)} draft(s) to create, {len(skips)} program(s) skipped.")
    for entry in creates:
        print(
            f"  CREATE {entry.program_key}: rules from {entry.rules_source}, "
            f"{entry.requirement_count} requirement row(s)"
        )
    for entry in skips:
        print(f"  skip   {entry.program_key}: {entry.reason}")


async def run(argv: Sequence[str] | None = None, *, session_factory=None) -> int:
    args = build_parser().parse_args(argv)
    email = args.user_email.strip().lower()
    factory = session_factory or SessionLocal
    async with factory() as db:
        user = (
            await db.execute(
                select(User).where(func.lower(User.email) == email, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if user is None:
            print(f"ERROR: no active user found with email={email!r}", file=sys.stderr)
            return 2
        if user.role != Role.SUPER_ADMIN:
            print(
                f"ERROR: {email} is not a super admin; drafts are attributed as super_admin actions",
                file=sys.stderr,
            )
            return 3

        plan = await funding_program_drafts.plan_review_drafts(
            db, program_keys=args.program_key or None
        )
        _print_plan(plan)
        if args.dry_run:
            print("Dry run: nothing written.")
            return 0

        created = await funding_program_drafts.ensure_review_drafts(
            db, user=user, program_keys=args.program_key or None, plan=plan
        )
        await db.commit()
        print(f"Created {len(created)} review draft(s).")
        for item in created:
            print(
                f"  {item['program_key']}: version {item['version']} "
                f"({item['rules_source']}, {item['requirement_count']} requirement row(s))"
            )
        rejected = [entry for entry in plan if entry.rules_source and entry.action != "create"]
        if rejected:
            print(f"{len(rejected)} program(s) need attention before a draft can be generated:")
            for entry in rejected:
                print(f"  {entry.program_key}: {entry.reason}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
