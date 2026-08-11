"""Snapshot a live lead's review inputs into a review_eval fixture.

Pulls the bucket's per-file analyses, requested documents, file metadata, and
AI context from the database and writes scripts/review_eval_fixtures/<name>.json
with an expectations stub derived from the CURRENT latest review (±15% bands on
its numeric key metrics). Review the generated expectations by hand — they
encode what "correct" means for this file from now on.

Usage (inside the backend container):
    python scripts/snapshot_review_fixture.py <bucket_id> <fixture-name>
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).resolve().parent / "review_eval_fixtures"


async def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    bucket_id, name = sys.argv[1], sys.argv[2]

    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db import SessionLocal
    from app.models.bucket import Bucket, BucketFileAnalysis

    async with SessionLocal() as db:
        bucket = (
            await db.execute(
                select(Bucket)
                .where(Bucket.id == UUID(bucket_id))
                .options(
                    selectinload(Bucket.requested_documents),
                    selectinload(Bucket.files),
                    selectinload(Bucket.ai_reviews),
                )
            )
        ).scalar_one()
        files = [f for f in bucket.files if f.status == "uploaded" and f.deleted_at is None]
        analyses = (
            await db.execute(
                select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id.in_([f.id for f in files]))
            )
        ).scalars().all()
        analysis_by_file = {a.bucket_file_id: a for a in analyses}

        per_file = []
        for file in files:
            analysis = analysis_by_file.get(file.id)
            if analysis is None:
                continue
            data = analysis.analysis or {}
            per_file.append(
                {
                    "file_id": str(file.id),
                    "file_name": file.file_name,
                    "ai_classification": analysis.classification,
                    "confidence": analysis.confidence,
                    "summary": analysis.summary,
                    "supports": data.get("supports") or [],
                    "baseline_categories_supported": data.get("baseline_categories_supported") or [],
                    "red_flags": data.get("red_flags") or [],
                    "limitations": data.get("limitations") or [],
                    "key_facts": data.get("key_facts") or {},
                }
            )

        latest = max((r for r in bucket.ai_reviews if r.status == "completed"), key=lambda r: r.created_at, default=None)
        current_km = (latest.result or {}).get("key_metrics") if latest and isinstance(latest.result, dict) else {}
        ranges = {
            key: [round(value * 0.85), round(value * 1.15)]
            for key, value in (current_km or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        }

        fixture = {
            "name": name,
            "review_type": (bucket.ai_context or {}).get("review_type"),
            "bucket": {
                "name": bucket.name,
                "client_name": bucket.client_name,
                "purpose": bucket.purpose,
                "bucket_type": bucket.bucket_type,
                "description": bucket.description,
            },
            "ai_context": bucket.ai_context or {},
            "requested_documents": [
                {
                    "id": str(doc.id),
                    "name": doc.name,
                    "category": doc.category,
                    "required": doc.required,
                    "status": doc.status,
                    "description": doc.description or "",
                }
                for doc in bucket.requested_documents
            ],
            "uploaded_files": [
                {
                    "id": str(file.id),
                    "file_name": file.file_name,
                    "content_type": file.content_type,
                    "size_bytes": file.size_bytes,
                }
                for file in files
            ],
            "per_file_analyses": per_file,
            "expectations": {
                "key_metrics_ranges": ranges,
                "require_strengths": True,
                "require_risks": True,
            },
        }

    FIXTURES_DIR.mkdir(exist_ok=True)
    out = FIXTURES_DIR / f"{name}.json"
    out.write_text(json.dumps(fixture, indent=2, default=str))
    print(f"wrote {out} ({len(per_file)} per-file analyses, {len(ranges)} metric ranges)")
    print("Review the expectations block by hand before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
