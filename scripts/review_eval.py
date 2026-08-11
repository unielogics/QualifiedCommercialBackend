"""Golden-fixture eval for the AI review synthesis pipeline.

Replays the EXACT synthesis stage of run_bucket_ai_review (same system
prompt, same content payload, same model tier, same deterministic
post-processing) against recorded per-file analyses, then asserts the
result against per-fixture expectations. Run it whenever review prompts or
post-processing change — it catches extraction regressions (empty
strengths/risks, null key metrics, readiness contradictions) before they
reach production.

Cost: one model_light call per fixture (~cents). Requires the prod/staging
env (Bedrock credentials); the DB is NOT touched.

Usage (inside the backend container or an env with app deps):
    python scripts/review_eval.py                 # run all fixtures
    python scripts/review_eval.py dealer-venture  # run one fixture by name

Fixtures live in scripts/review_eval_fixtures/*.json — snapshot new ones
from live leads with scripts/snapshot_review_fixture.py. Fixture shape:
    {
      "name": "dealer-venture",
      "review_type": "dealer_gatekeeper_v1",
      "bucket": {"name": ..., "client_name": ..., "purpose": ..., "bucket_type": ..., "description": ...},
      "ai_context": {...},
      "requested_documents": [{"id", "name", "category", "required", "status", "description"}],
      "uploaded_files": [{"id", "file_name", "content_type", "size_bytes"}],
      "per_file_analyses": [{"file_id", "file_name", "ai_classification", "key_facts", ...}],
      "expectations": {
        "key_metrics_ranges": {"ytd_annualized_revenue": [3300000, 3800000]},
        "key_metrics_not_null": ["estimated_ebitda_or_cash_flow"],
        "probability_status_in": ["Good probability - book call", ...],
        "require_strengths": true,
        "require_risks": true,
        "lending_ready": false
      }
    }
Numeric ranges are [min, max] inclusive. Every listed assertion must hold.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).resolve().parent / "review_eval_fixtures"

ALLOWED_STATUSES = {
    "Good probability - book call",
    "Promising but needs one clarification",
    "Not enough evidence yet",
    "Poor probability based on current file",
}


def _build_content(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Mirror of run_bucket_ai_review's synthesis payload — keep in sync."""
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "bucket": fixture.get("bucket") or {},
                    "ai_context": fixture.get("ai_context") or {},
                    "requested_documents": fixture.get("requested_documents") or [],
                    "uploaded_files": fixture.get("uploaded_files") or [],
                    "instruction": "Review the attached/readable files and the metadata. Identify what is available, missing, discrepant, unclear, or likely to be questioned by an underwriter.",
                },
                default=str,
            ),
        },
        {
            "type": "text",
            "text": "Per-file analyses (already extracted; synthesize from these — do not ask for the raw files):\n"
            + json.dumps(fixture.get("per_file_analyses") or [], default=str),
        },
    ]
    return content


async def _run_fixture(fixture: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    from app.services.ai.bedrock_client import get_client, model_light
    from app.services.bucket_ai import (
        _apply_lending_readiness,
        _compute_key_metrics_from_cache,
        _ensure_review_result_shape,
        _json_or_fallback,
        _merge_per_file_analyses,
        _reconcile_checklist_consistency,
        _text_from_response,
        build_review_system,
    )

    review_type = fixture.get("review_type")
    per_file = fixture.get("per_file_analyses") or []
    requested = fixture.get("requested_documents") or []

    client = get_client()
    resp = await client.messages.create(
        model=model_light(),
        max_tokens=6000,
        system=build_review_system(review_type),
        messages=[{"role": "user", "content": _build_content(fixture)}],
    )
    result = _ensure_review_result_shape(_json_or_fallback(_text_from_response(resp), "executive_summary"))
    _merge_per_file_analyses(result, per_file, requested_documents=requested)
    _compute_key_metrics_from_cache(result, per_file)
    _apply_lending_readiness(result, per_file)

    # Mirror of _repair_empty_strengths_risks (production runs it db-tracked;
    # the harness replays the same prompt untracked) — the synthesis model
    # frequently omits these sections, and production repairs them, so the
    # eval must validate the repaired result, not the raw omission.
    if not result.get("strengths") or not result.get("risks"):
        repair_prompt = {
            "executive_summary": str(result.get("executive_summary") or ""),
            "key_metrics": result.get("key_metrics") or {},
            "baseline_coverage": (result.get("document_evidence_map") or {}).get("baseline_coverage") or [],
            "instruction": (
                "From ONLY the data above, list the file's underwriting strengths and risks. "
                "3-6 of each when the data supports them, fewer if it does not. Never invent a fact "
                'not present above. Reply as JSON: {"strengths": ["..."], "risks": ["..."]}'
            ),
        }
        repair_resp = await client.messages.create(
            model=model_light(),
            max_tokens=1200,
            system="You are a commercial underwriting analyst. Ground every statement strictly in the provided data.",
            messages=[{"role": "user", "content": json.dumps(repair_prompt, default=str)}],
        )
        parsed = _json_or_fallback(_text_from_response(repair_resp), "strengths")
        if not result.get("strengths"):
            result["strengths"] = [str(item) for item in parsed.get("strengths") or [] if str(item).strip()][:6]
        if not result.get("risks"):
            result["risks"] = [str(item) for item in parsed.get("risks") or [] if str(item).strip()][:6]

    # Checklist consistency runs against a stub bucket built from the fixture.
    doc_rows = [
        SimpleNamespace(
            id=row.get("id") or f"doc-{index}",
            name=row.get("name") or "Document",
            description=row.get("description") or "",
            category=row.get("category"),
            required=bool(row.get("required", True)),
            status=row.get("status") or "requested",
        )
        for index, row in enumerate(requested)
    ]
    stub_bucket = SimpleNamespace(requested_documents=doc_rows, files=[])
    _reconcile_checklist_consistency(stub_bucket, result)

    failures: list[str] = []
    exp = fixture.get("expectations") or {}
    km = result.get("key_metrics") or {}

    for field, bounds in (exp.get("key_metrics_ranges") or {}).items():
        value = km.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            failures.append(f"key_metrics.{field}: expected a number in {bounds}, got {value!r}")
        elif not (bounds[0] <= value <= bounds[1]):
            failures.append(f"key_metrics.{field}: {value:,.0f} outside [{bounds[0]:,.0f}, {bounds[1]:,.0f}]")

    for field in exp.get("key_metrics_not_null") or []:
        value = km.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            failures.append(f"key_metrics.{field}: expected non-null")

    status = str(result.get("probability_status") or "")
    allowed = set(exp.get("probability_status_in") or ALLOWED_STATUSES)
    if status not in allowed:
        failures.append(f"probability_status {status!r} not in {sorted(allowed)}")

    if exp.get("require_strengths") and not result.get("strengths"):
        failures.append("strengths: expected non-empty")
    if exp.get("require_risks") and not result.get("risks"):
        failures.append("risks: expected non-empty")
    if "lending_ready" in exp and bool(result.get("lending_ready")) is not bool(exp["lending_ready"]):
        failures.append(f"lending_ready: expected {exp['lending_ready']}, got {result.get('lending_ready')}")

    # Invariants that hold for EVERY fixture, no expectation needed:
    if not str(result.get("executive_summary") or "").strip():
        failures.append("executive_summary: empty")
    outstanding = result.get("outstanding_checklist") or []
    if outstanding and result.get("lending_ready"):
        failures.append(f"consistency: lending_ready=True with outstanding checklist {outstanding}")
    one_next = str(result.get("one_next_step") or "").lower()
    if outstanding and ("advance to lender packaging" in one_next and "outstanding" not in one_next):
        failures.append("consistency: one_next_step claims completion despite outstanding checklist")

    return result, failures


async def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    fixtures = sorted(FIXTURES_DIR.glob("*.json"))
    if only:
        fixtures = [path for path in fixtures if path.stem == only]
    if not fixtures:
        print(f"No fixtures found in {FIXTURES_DIR}" + (f" matching {only!r}" if only else ""))
        return 2

    failed = 0
    for path in fixtures:
        fixture = json.loads(path.read_text())
        name = fixture.get("name") or path.stem
        try:
            result, failures = await _run_fixture(fixture)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {name}: harness error: {exc}")
            failed += 1
            continue
        if failures:
            failed += 1
            print(f"✗ {name}: {len(failures)} assertion(s) failed")
            for failure in failures:
                print(f"    - {failure}")
        else:
            km = result.get("key_metrics") or {}
            print(f"✓ {name}: status={result.get('probability_status')!r} metrics={sum(1 for v in km.values() if v is not None)}/{len(km)} populated")
    print(f"\n{len(fixtures) - failed}/{len(fixtures)} fixtures passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
