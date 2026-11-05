"""Onboard one funding program from its baseline spec: catalog row, review draft, screen, publish.

    python -m app.scripts.onboard_program_from_baseline --program-key quidity_10_year_term \
        --user-email you@qualifiedcommercial.com [--dry-run] [--publish] [--reason "..."]

The baseline (`funding_program_baselines.CATALOG`) is the transcription of the
lender's guideline. This script turns it into the rows the fit engine reads:

1. the catalog row, created from the spec when the program key is new;
2. a draft criteria version, through `funding_program_drafts.ensure_review_drafts`;
3. a screen of the draft's fit rules across the files on the system, printed so
   a bad transcription is seen before it goes live — how many files pass, fail,
   or lack a fact, and which facts and rules are doing the work;
4. with `--publish`, publication of that draft, which is what makes the program
   a candidate on every file in its verticals from the next readiness read.

`--dry-run` does steps 1-3 inside a transaction that is rolled back and prints
what would happen. Publishing is an audited action attributed to the named
super admin; the reason is recorded with it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.enums import Role
from app.models.ai_playbook import AIPlaybookTemplate
from app.models.application_profile import ApplicationProfile
from app.models.funding_program import FundingProgramCatalog
from app.models.user import User
from app.schemas.funding_program import FundingProgramCreate, FundingProgramScopeWrite
from app.services import application_programs, funding_program_drafts, funding_programs
from app.services.funding_program_baselines import CATALOG, suggested_baseline
from app.services.program_rules import evaluate_rules

SCREEN_LIMIT = 400


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--program-key", required=True)
    parser.add_argument("--user-email", required=True, help="super admin the rows are attributed to")
    parser.add_argument("--dry-run", action="store_true", help="plan, screen, write nothing")
    parser.add_argument("--publish", action="store_true", help="publish the draft after creating it")
    parser.add_argument(
        "--reason",
        default=None,
        help="audit reason recorded with the publication (defaults to the spec's source note)",
    )
    return parser


async def _super_admin(db: AsyncSession, email: str) -> User | None:
    user = (
        await db.execute(
            select(User).where(func.lower(User.email) == email.strip().lower(), User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if user is None or user.role != Role.SUPER_ADMIN:
        return None
    return user


async def ensure_catalog_row(
    db: AsyncSession, program_key: str, spec: dict, user: User
) -> tuple[FundingProgramCatalog, bool]:
    """The catalog row for the spec, created when the key is new."""
    existing = (
        await db.execute(select(FundingProgramCatalog).where(FundingProgramCatalog.program_key == program_key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    baseline = suggested_baseline(program_key, name=spec.get("name"))
    payload = FundingProgramCreate(
        program_key=program_key,
        public_slug=spec["public_slug"],
        name=spec["name"],
        short_description=spec.get("short_description"),
        aliases=list(spec.get("aliases", [])),
        display_order=int(spec.get("display_order", 100)),
        scopes=[FundingProgramScopeWrite(**scope) for scope in baseline["scopes"]],
        confirmed=True,
    )
    return await funding_programs.create_program(db, payload, user), True


async def screen(db: AsyncSession, rules: dict, verticals: Sequence[str]) -> dict:
    """Evaluate the draft's fit rules against the files in its verticals.

    Scope (NAICS, variant) is not applied here; this is the eligibility
    arithmetic only, which is where a transcription error shows up as "every
    file fails" or "no file can ever pass".
    """
    profiles = (
        await db.execute(
            select(ApplicationProfile)
            .where(ApplicationProfile.vertical.in_(list(verticals)))
            .order_by(ApplicationProfile.created_at.desc())
            .limit(SCREEN_LIMIT)
        )
    ).scalars().all()
    states: Counter[str] = Counter()
    unknown_fields: Counter[str] = Counter()
    fail_reasons: Counter[str] = Counter()
    for profile in profiles:
        try:
            context = await application_programs.profile_fit_context(db, profile)
            result = evaluate_rules(rules, context)
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the screen
            states["error"] += 1
            fail_reasons[f"error: {type(exc).__name__}"] += 1
            continue
        states[result.state] += 1
        for item in result.results:
            if item.get("status") == "unknown" and item.get("field"):
                unknown_fields[str(item["field"])] += 1
        if result.state == "fail":
            # Only the rules that failed, with what was observed — the
            # evaluation's reason list also carries every rule that passed.
            for item in result.results:
                status = item.get("status")
                if item.get("negated"):
                    status = {"fail": "pass", "pass": "fail"}.get(status, status)
                if status == "fail":
                    fail_reasons[
                        f"{item.get('field')} {item.get('operator')} {item.get('required')} "
                        f"(observed {item.get('observed')!r})"[:110]
                    ] += 1
    return {
        "files": len(profiles),
        "states": dict(states),
        "unknown_fields": unknown_fields.most_common(8),
        "fail_reasons": fail_reasons.most_common(8),
    }


def _print_screen(report: dict) -> None:
    print(f"Screen over {report['files']} file(s): {report['states']}")
    if report["unknown_fields"]:
        print("  facts most often missing:", ", ".join(f"{k} ({n})" for k, n in report["unknown_fields"]))
    if report["fail_reasons"]:
        print("  rules most often failing:")
        for reason, n in report["fail_reasons"]:
            print(f"    {n:3}x {reason}")


async def run(argv: Sequence[str] | None = None, *, session_factory=None) -> int:
    args = build_parser().parse_args(argv)
    spec = CATALOG.get(args.program_key)
    if not spec:
        print(f"No baseline spec for program key {args.program_key!r}.", file=sys.stderr)
        return 2
    if "public_slug" not in spec or "name" not in spec:
        print("The spec needs a name and a public_slug to be onboarded.", file=sys.stderr)
        return 2
    factory = session_factory or SessionLocal
    async with factory() as db:
        user = await _super_admin(db, args.user_email)
        if user is None:
            print(f"No active super admin with email {args.user_email!r}.", file=sys.stderr)
            return 3

        program, created = await ensure_catalog_row(db, args.program_key, spec, user)
        print(f"{'CREATE' if created else 'exists'} catalog row {program.program_key}: {program.name}")

        drafts = await funding_program_drafts.ensure_review_drafts(db, user=user, program_keys=[args.program_key])
        if drafts:
            for row in drafts:
                print(f"CREATE draft version {row['version']} ({row['rules_source']}, {row['requirement_count']} requirement rows)")
        else:
            print("draft: nothing to create (a draft or a published version already exists)")

        draft = (
            await db.execute(
                select(AIPlaybookTemplate)
                .where(
                    AIPlaybookTemplate.funding_program_id == program.id,
                    AIPlaybookTemplate.playbook_type == "loan_product",
                    AIPlaybookTemplate.status == "draft",
                )
                .order_by(AIPlaybookTemplate.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if draft is None:
            print("No draft to screen or publish.", file=sys.stderr)
            await db.rollback()
            return 4

        _print_screen(await screen(db, draft.rules or {}, spec.get("verticals", ["main_street"])))

        if args.dry_run:
            await db.rollback()
            print("Dry run: nothing written.")
            return 0
        if args.publish:
            reason = args.reason or spec.get("source_note") or "Published from the program baseline"
            published = await funding_programs.publish_version(db, program, draft.id, reason, user)
            print(f"PUBLISHED {program.program_key} version {published.version}")
        await db.commit()
        print("Done." if args.publish else "Done (draft only; publish from the editor or re-run with --publish).")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
