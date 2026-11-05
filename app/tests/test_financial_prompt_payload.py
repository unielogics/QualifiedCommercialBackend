from copy import deepcopy

from app.services.financial_prompt_payload import summary_analyses


def test_bank_source_rows_stay_stored_while_synthesis_keeps_the_financial_summaries():
    inputs = [{"file_id": "bank", "ai_classification": "bank_statement", "key_facts": {
        "transactions": [{"description": "source row"}] * 2000,
        "total_deposits_and_credits": 3500,
        "months": [{"statement_period": "2026-05", "transfer_lines": [{"amount": 100}],
                    "recurring_credits": [{"payee": "Customer", "total_amount": 3500}],
                    "overdraft_items": [{"date": "2026-05-12", "amount": 34}]}],
    }}, {"classification": "debt_schedule", "key_facts": {"debts": [{"lender": "Bank"}]}}]
    before = deepcopy(inputs)
    output = summary_analyses(inputs)
    assert inputs == before
    facts = output[0]["key_facts"]
    assert "transactions" not in facts
    assert "transfer_lines" not in facts["months"][0]
    assert facts["transaction_detail_summary"]["stored_line_count"] == 2001
    assert facts["total_deposits_and_credits"] == 3500
    assert facts["months"][0]["recurring_credits"] == before[0]["key_facts"]["months"][0]["recurring_credits"]
    assert facts["months"][0]["overdraft_items"] == before[0]["key_facts"]["months"][0]["overdraft_items"]
    assert output[1] == inputs[1]


def test_other_documents_and_missing_bank_facts_are_not_reinterpreted():
    inputs = [{"classification": "other", "key_facts": {"transactions": [{"value": 1}]}},
              {"classification": "bank_statement", "key_facts": None}]
    assert summary_analyses(inputs) == inputs


def test_deterministic_export_is_compacted_even_when_its_document_classification_is_preserved():
    inputs = [{"ai_classification": "debt_schedule", "key_facts": {
        "source": "transactions_export", "transactions": [{"amount": 100}] * 2000,
        "debts": [{"lender": "Bank"}], "total_deposits_and_credits": 100,
    }}]
    output = summary_analyses(inputs)
    assert "transactions" not in output[0]["key_facts"]
    assert output[0]["ai_classification"] == "debt_schedule"
    assert output[0]["key_facts"]["debts"] == [{"lender": "Bank"}]
    assert output[0]["key_facts"]["total_deposits_and_credits"] == 100
    assert len(inputs[0]["key_facts"]["transactions"]) == 2000
