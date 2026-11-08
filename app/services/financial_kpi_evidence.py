"""Read-only calculation receipts for the financial cockpit's seven KPI cards."""

from app.services.financial_metric_review import number, stamp


def attach(cockpit):
    review = cockpit["metric_review"]
    operands = {row["key"]: row for row in review["inputs"]}
    bank = cockpit["bank"]
    months = bank["months"]
    scope = {"kind": "counted_accounts", "accounts": [row["key"] for row in bank["accounts"] if row.get("counted")],
             "months": [row["month"] for row in months], "per_account_editing": False,
             "note": "Saved aggregate reviews apply to these counted accounts and months. Individual-account views remain source observations."}

    def calculation(key, label, formula, input_keys, value, *, unit="currency", factor=1, notes=None, details=None, selected=False):
        inputs = [operands[name] for name in input_keys if name in operands]
        refs = {stamp(ref): ref for row in inputs for ref in row["source_refs"]}
        staff_corrected = any(item.get("correction") for item in inputs)
        reviewed_aggregate = staff_corrected and key.startswith("bank.")
        source_value = inputs[0]["original_value"] if key.startswith("bank.") and inputs else None
        base_formula = formula
        notes = list(notes or [])
        if reviewed_aggregate:
            formula = f"staff-reviewed aggregate input; historical source basis: {base_formula}"
            notes.append("This reviewed aggregate is shown separately from its historical source mean or subtotal. Source statement months and other independently reviewed aggregates are not reconstructed by this correction.")
        row = {"key": key, "label": label, "formula": formula, "value": value, "unit": unit,
               "input_keys": input_keys, "source_refs": list(refs.values()),
               "period": inputs[0]["period"] if inputs else None, "annualization_factor": factor,
               "missing_inputs": [item["key"] for item in inputs if item["missing"]],
               "notes": notes, "details": {**(details or {}), "reviewed_aggregate": reviewed_aggregate}, "selected": False, "kpi_selected": selected,
               "account_scope": scope if key.startswith("bank.") or key == "net_profit.bank_cash_flow" else {"kind": "business"},
               "staff_corrected": staff_corrected}
        if key.startswith("bank."):
            row.update(source_value=source_value, base_formula=base_formula)
        row["source_stamp"] = stamp({"key": key, "scope": row["account_scope"], "inputs": [{"key": item["key"], "source_stamp": item["source_stamp"], "value": item["value"]} for item in inputs]})
        review["calculations"].append(row)
        return row

    valid_net = [row for row in months if row.get("net") is not None]
    calculation("bank.deposits", "Average monthly deposits", "sum of known monthly deposits / months with known deposits",
                ["bank.avg_deposits"], bank["summary"].get("avg_deposits"),
                details={"months": months, "included_months": sum(row.get("deposits") is not None for row in months)})
    calculation("bank.net_cash_flow", "Net cash flow per month", "sum of monthly (deposits − withdrawals) / months with both amounts known",
                ["bank.avg_net"], bank["summary"].get("avg_net"),
                notes=["Loan principal, transfers and financing proceeds are not operating profit. These are historical statement cash movements."],
                details={"months": months, "included_months": len(valid_net)})
    calculation("bank.average_balance", "Average balance", "mean of known monthly printed average balances; use mean ending balances only when no monthly average is reported",
                ["bank.average_balance"], bank["summary"].get("average_balance"),
                notes=["This is the existing monthly-mean balance measure, not a reconstructed daily cash balance."],
                details={"months": months, "basis": bank["summary"].get("average_balance_basis")})
    def valid_count(value):
        value = number(value)
        return value is not None and value >= 0 and value == int(value)
    complete_months = sum(valid_count(row.get("nsf")) and all(valid_count(source.get("nsf")) for source in row.get("risk_sources", [])) for row in months)
    calculation("bank.nsf_overdrafts", "NSF and overdraft items", "sum of known monthly NSF/overdraft item counts; observed fee lines are evidence, not additional incidents",
                ["bank.nsf_total"], operands.get("bank.nsf_total", {}).get("value"), unit="number",
                notes=["A partial reported subtotal is a lower bound. Missing monthly counts are not zero. Returned items and negative-balance account-days are separate measures."],
                details={"months": months, "complete": bool(months) and complete_months == len(months),
                         "source_complete": bool(months) and complete_months == len(months),
                         "reported_months": complete_months, "total_months": len(months),
                         "events": (bank.get("overdrafts") or {}).get("items", [])})
    net_profit = cockpit["net_profit"]
    for candidate in net_profit.get("candidates", []):
        if candidate["key"] == "tax_return":
            key = next((row["key"] for row in reversed(review["inputs"]) if row["key"].startswith("tax_return:")
                        and row["key"].endswith(f":{candidate.get('period')}.income")), None)
            keys, formula, factor = [key] if key else [], "selected business tax-return income line", 1
        elif candidate["key"] == "profit_and_loss":
            keys = ["profit_and_loss.net_income", "profit_and_loss.months_covered"]
            covered = (cockpit.get("profit_and_loss") or {}).get("months_covered")
            factor = 12 / covered if covered and 0 < covered < 12 else 1
            formula = "P&L net income × existing reporting-period annualization factor"
        else:
            keys, formula, factor = ["bank.avg_net"], "reviewed average monthly cash retained × 12 (cash proxy, not accounting profit)", 12
        receipt = calculation(f"net_profit.{candidate['key']}", candidate["label"] + " net-profit basis", formula, keys, candidate.get("value"),
                              factor=factor, notes=[net_profit["note"]] if net_profit.get("note") else [], selected=candidate is net_profit.get("conclusion"))
        candidate["input_keys"] = keys
        candidate["source_refs"] = receipt["source_refs"]
    review["account_scope"] = scope
    for item in review["inputs"]:
        item["used_by"] = [row["key"] for row in review["calculations"] if item["key"] in row.get("input_keys", [])]
