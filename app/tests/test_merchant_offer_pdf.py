from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.merchant_processing_offer import MerchantProcessingOffer
from app.services import merchant_processing as mp
from app.services.merchant_offer_pdf import filename_for, render_merchant_offer_html


def _offer(**overrides) -> MerchantProcessingOffer:
    values = {
        "id": uuid.uuid4(),
        "profile_id": uuid.uuid4(),
        "status": mp.STATUS_EXTRACTED,
        "terms_version": 4,
        "terms": {
            "provider_name": "Acme Processing",
            "prepared_for": "Mora's Market",
            "prepared_on": "September 15, 2026",
            "current_processor": "Legacy Pay",
            "current_monthly_volume": 207667,
            "current_monthly_fees": 4702,
            "current_effective_rate_pct": 2.26,
            "proposed_monthly_fees": 778,
            "proposed_effective_rate_pct": 0.37,
            "proposed_pricing_model": "surcharge",
            "contract_term_months": 24,
            "early_termination_fee": 150,
            "equipment_notes": "Two countertop terminals included",
            # The extraction prompt historically calls this a desk note. Even
            # though client_view still carries it for compatibility, this
            # renderer deliberately does not print it.
            "notes": "CLIENT-PDF-MUST-NOT-PRINT-THIS-NOTE",
            "options": [
                {
                    "label": "ConsumerChoice",
                    "effective_rate_pct": 0.37,
                    "monthly_fees": 778,
                    "monthly_savings": 3924,
                }
            ],
        },
        "desk_terms": {
            "agent_residual_pct": 47.25,
            "agent_residual_monthly": 987654.32,
            "signing_bonus": 876543.21,
            "partner_notes": "INTERNAL-ONLY-PARTNER-MEMO",
            "savings_warning": "INTERNAL-ONLY-SAVINGS-WARNING",
        },
        "estimated_monthly_savings": Decimal("3924.00"),
        "estimated_annual_savings": Decimal("47088.00"),
        "savings_basis": "fees_diff",
    }
    values.update(overrides)
    return MerchantProcessingOffer(**values)


def test_client_offer_html_is_branded_and_uses_only_sanitized_values() -> None:
    offer = _offer()

    html = render_merchant_offer_html(
        offer,
        business_name="Mora's Market",
        partner_name="Acme Processing",
    )

    assert "Qualified Commercial" in html
    assert "Presented with" in html
    assert "UrChoice" in html
    assert "data:image/png;base64," in html
    assert "#0b1d3a" in html
    assert "#45d7cb" in html
    assert "Acme Processing" in html
    assert "$47,088.00" in html
    assert "ConsumerChoice" in html
    assert "Two countertop terminals included" in html
    assert "CLIENT-PDF-MUST-NOT-PRINT-THIS-NOTE" not in html
    assert "INTERNAL-ONLY-PARTNER-MEMO" not in html
    assert "INTERNAL-ONLY-SAVINGS-WARNING" not in html
    assert "987,654" not in html
    assert "876,543" not in html
    for key in mp.DESK_ONLY_KEYS:
        assert key not in html


def test_client_offer_filename_is_stable_and_safe() -> None:
    assert (
        filename_for(_offer(), "Mora's Market / Downtown")
        == "Mora-s-Market-Downtown-Merchant-Processing-Offer-v4.pdf"
    )
