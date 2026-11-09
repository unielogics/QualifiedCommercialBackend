"""Keep the underwriting synthesis focused on summaries, without dropping stored evidence."""

from typing import Any

_TRANSACTION_ARRAYS = frozenset({"transactions", "transaction_lines", "transfer_lines"})


def summary_analyses(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove bank transaction arrays only from the model's summary payload.

    Individual source lines remain in the immutable analysis for the account
    movement read model and evidence inspector. Sending every export row into
    the synthesis can swamp the monthly figures and exhaust its context.
    """
    result = []
    for item in analyses:
        from app.services.document_associations import source_excluded

        if source_excluded(item.get("identity_association")):
            result.append({key: item.get(key) for key in ("file_id", "file_name", "identity_association")})
            continue
        classes = {item.get("ai_classification"), item.get("classification"),
                   *(item.get("classifications") or [])}
        source_facts = item.get("key_facts")
        if not isinstance(source_facts, dict) or (
            "bank_statement" not in classes and source_facts.get("source") != "transactions_export"
        ):
            result.append(item)
            continue
        removed = 0

        def compact(value: Any) -> Any:
            nonlocal removed
            if isinstance(value, dict):
                out = {}
                for key, child in value.items():
                    if key in _TRANSACTION_ARRAYS and isinstance(child, list):
                        removed += len(child)
                    else:
                        out[key] = compact(child)
                return out
            if isinstance(value, list):
                return [compact(child) for child in value]
            return value

        facts = compact(item["key_facts"])
        if removed:
            facts["transaction_detail_summary"] = {
                "stored_line_count": removed,
                "note": "Individual source lines are retained for evidence review. This count does not establish transfers or change monthly figures.",
            }
        result.append({**item, "key_facts": facts})
    return result
