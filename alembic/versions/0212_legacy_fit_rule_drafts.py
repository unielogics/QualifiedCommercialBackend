"""Import legacy deterministic fit rules as inactive review drafts.

Revision ID: 0212_legacy_fit_rule_drafts
Revises: 0211_program_catalog_ai_evidence

Only legacy rules with a direct canonical-program mapping are imported. These
versions remain drafts until a super admin reviews and publishes them. Rules
that cannot be represented exactly by the bounded DSL carry explicit unresolved
review items and are rejected by the publish service until a revised version is
created.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0212_legacy_fit_rule_drafts"
down_revision = "0211_program_catalog_ai_evidence"
branch_labels = None
depends_on = None


def _rule(field: str, operator: str, value: object | None = None) -> dict:
    item = {"field": field, "op": operator}
    if value is not None:
        item["value"] = value
    return item


def _all(*rules: dict) -> dict:
    return {"all": list(rules)}


def _any(*rules: dict) -> dict:
    return {"any": list(rules)}


def _optional_threshold(field: str, operator: str, value: float) -> dict:
    return _any({"not": _rule(field, "present")}, _rule(field, operator, value))


def _main_street_profile(*rules: dict) -> dict:
    return _all(
        _rule("vertical", "eq", "main_street"),
        _rule("business_age_years", "gte", 1),
        _rule("annual_revenue", "gte", 50_000),
        _rule("estimated_credit_score", "gte", 640),
        *rules,
    )


def _main_street_sba_fit() -> dict:
    return _main_street_profile(
        _rule("business_age_years", "gte", 2),
        _rule("annual_revenue", "gte", 100_000),
        _rule("dscr", "gte", 1.15),
        _rule("bank_statement_months", "gte", 6),
        _rule("tax_return_years", "gte", 2),
        _rule("evidence_available", "evidence_available", "personal_financial_statement"),
        _rule("evidence_available", "evidence_available", "debt_schedule"),
    )


def _imported(fit: dict, *, source: str, unresolved: list[str] | None = None) -> dict:
    return {
        "priority": 0,
        "fit": fit,
        "import": {
            "migration": revision,
            "source": source,
            "status": "provisional",
            "review_required": True,
        },
        "unresolved_review_items": unresolved or [],
    }


LEGACY_FIT_RULE_DRAFTS = {
    "sba_7a": _imported(
        _any(
            _main_street_sba_fit(),
            _all(
                _rule("vertical", "eq", "dealer"),
                _rule("bank_statement_months", "gte", 6),
                _rule("tax_return_years", "gte", 2),
                _any(
                    _rule("evidence_available", "evidence_available", "current_p_and_l"),
                    _rule("evidence_available", "evidence_available", "profit_and_loss"),
                ),
                _rule("evidence_available", "evidence_available", "debt_schedule"),
                _rule(
                    "evidence_available",
                    "evidence_available",
                    "personal_financial_statement",
                ),
            ),
        ),
        source="dealer_ai_intake._compute_loan_program_fit + main_street_programs.compute_main_street_program_fit",
        unresolved=[
            "Define reviewed real-estate SBA 7(a) criteria.",
            "Confirm the dealer owner-identification requirement in the canonical evidence taxonomy.",
        ],
    ),
    "dealer_real_estate_capital": _imported(
        _all(
            _rule("vertical", "eq", "dealer"),
            _rule("declared_collateral", "eq", True),
        ),
        source="dealer_ai_intake._compute_loan_program_fit.real_estate_backed",
    ),
    "ez_term": _imported(
        _main_street_profile(
            _rule("business_age_years", "gte", 2),
            _rule("estimated_credit_score", "gte", 660),
            _rule("dscr", "gte", 1.0),
            _optional_threshold("requested_amount", "gte", 25_000),
            _optional_threshold("requested_amount", "lte", 500_000),
        ),
        source="main_street_programs.compute_main_street_program_fit.term_loan_3_5_year",
    ),
    "microcap": _imported(
        _main_street_profile(
            _rule("business_age_years", "gte", 2),
            _rule("estimated_credit_score", "gte", 660),
            _rule("dscr", "gte", 1.10),
            _optional_threshold("requested_amount", "gte", 15_000),
            _optional_threshold("requested_amount", "lte", 50_000),
            _any(
                {"not": _rule("nsf_or_overdraft_count", "present")},
                _rule("nsf_or_overdraft_count", "lte", 2),
            ),
            {
                "not": _rule(
                    "industry_key",
                    "in",
                    ["trucking_logistics", "restaurant_food_service"],
                )
            },
        ),
        source="main_street_programs.compute_main_street_program_fit.term_loan_10_year",
        unresolved=[
            "Represent and review the requested-amount-to-annual-revenue cap before publishing."
        ],
    ),
    "line_of_credit": _imported(
        _main_street_profile(
            _rule("annual_revenue", "gte", 100_000),
            _rule("bank_statement_months", "gte", 3),
            _any(
                {"not": _rule("nsf_or_overdraft_count", "present")},
                _rule("nsf_or_overdraft_count", "lte", 3),
            ),
        ),
        source="main_street_programs.compute_main_street_program_fit.line_of_credit",
    ),
    "equipment_financing": _imported(
        _main_street_profile(
            _rule("annual_revenue", "gte", 100_000),
            _rule("cash_flow", "gt", 0),
            _rule("equipment_financing_intent", "eq", True),
        ),
        source="main_street_programs.compute_main_street_program_fit.equipment_financing",
    ),
    "jumbo_term": _imported(
        _main_street_profile(
            _rule("annual_revenue", "gte", 5_000_000),
            _rule("dscr", "gte", 1.25),
            _rule("requested_amount", "gte", 1_000_000),
        ),
        source="main_street_programs.compute_main_street_program_fit.jumbo_term_loan",
    ),
    "hybrid_term_loc": _imported(
        _main_street_profile(
            _rule("annual_revenue", "gte", 200_000),
            _rule("dscr", "gte", 1.10),
        ),
        source="main_street_programs.compute_main_street_program_fit.term_loan_loc_hybrid",
    ),
    "transportation_finance": _imported(
        _main_street_profile(_rule("annual_revenue", "gte", 150_000)),
        source="main_street_programs.compute_main_street_program_fit.transportation_finance",
    ),
    "sba_grocery": _imported(
        _main_street_sba_fit(),
        source="main_street_programs.compute_main_street_program_fit.sba_grocery",
    ),
    "sba_made_in_america": _imported(
        _main_street_sba_fit(),
        source="main_street_programs.compute_main_street_program_fit.sba_made_in_america",
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    statement = sa.text(
        """
        INSERT INTO ai_playbook_templates (
            id, owner_type, owner_id, playbook_type, product_key,
            funding_program_id, name, description, rules, version,
            status, published_at, is_active, created_at, updated_at
        )
        SELECT gen_random_uuid(), 'platform', NULL, 'loan_product', catalog.program_key,
               catalog.id, catalog.name || ' legacy criteria import',
               'Imported from the legacy deterministic fit engine for super-admin review.',
               :rules, COALESCE((
                   SELECT MAX(existing.version) + 1
                   FROM ai_playbook_templates AS existing
                   WHERE existing.funding_program_id = catalog.id
               ), 1),
               'draft', NULL, true, now(), now()
        FROM funding_program_catalog AS catalog
        WHERE catalog.program_key = :program_key
          AND NOT EXISTS (
              SELECT 1
              FROM ai_playbook_templates AS existing
              WHERE existing.funding_program_id = catalog.id
                AND existing.status = 'draft'
                AND existing.rules #>> '{import,migration}' = :migration
          )
        """
    ).bindparams(sa.bindparam("rules", type_=postgresql.JSONB()))
    for program_key, rules in LEGACY_FIT_RULE_DRAFTS.items():
        bind.execute(
            statement,
            {
                "program_key": program_key,
                "rules": rules,
                "migration": revision,
            },
        )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM ai_playbook_templates
        WHERE status = 'draft'
          AND rules #>> '{import,migration}' = '0212_legacy_fit_rule_drafts'
        """
    )
