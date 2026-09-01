from app.dealer_os.services import qc_master_application, routing_resolution


def test_current_routing_result_selects_potential_program() -> None:
    routing = {
        "rules_version": "current",
        "programs": [
            {
                "program_key": "term_loan_3_5_year",
                "name": "EZ Term",
                "status": "potential",
            },
            {
                "program_key": "term_loan_10_year",
                "name": "MicroCap",
                "status": "blocked",
            },
        ],
    }

    key, label = qc_master_application._route_from(routing, None)

    assert key == "term_loan_3_5_year"
    assert label == "EZ Term"


def test_routing_blockers_include_guided_correction_targets() -> None:
    routing = {
        "programs": [
            {
                "program_key": "term_loan_3_5_year",
                "name": "EZ Term",
                "matched_rules": [
                    {
                        "rule_id": "term_loan_3_5_year.amount",
                        "explanation": "The requested amount is outside the supported range.",
                        "matched_value": "750000",
                    }
                ],
                "unresolved": [
                    "Six current verified bank months are still required through Plaid Assets."
                ],
            }
        ]
    }

    blockers = routing_resolution.blockers(routing)

    assert blockers[0]["correction_step"] == 1
    assert blockers[0]["correction_anchor"] == "funding-request"
    assert blockers[1]["correction_step"] == 2
    assert blockers[1]["correction_anchor"] == "bank-evidence"


def test_financial_condition_opens_step_three_confirmation() -> None:
    target = routing_resolution.correction_target(
        "term_loan_10_year.dscr",
        "Calculate business DSCR from cash flow and monthly debt payments.",
    )

    assert target == {
        "correction_step": 3,
        "correction_anchor": "financial-confirmation",
        "correction_label": "Review financial profile",
    }
