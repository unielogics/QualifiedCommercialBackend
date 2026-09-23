"""Low-cardinality structured operational metrics for booking workflows.

The production log stream is already shipped to CloudWatch.  Emitting one
stable JSON envelope lets dashboards and alarms use metric filters without
making booking correctness depend on a separate telemetry provider.  This
module must therefore stay best-effort: observability can never fail a booking.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import request_context

log = logging.getLogger("qc.booking.metrics")

_UUID_PATH = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


def route_name(path: str) -> str | None:
    """Return a bounded booking route label, or ``None`` outside this domain."""

    normalized = _UUID_PATH.sub("/:id", path or "")
    # `/public/booking` is the canonical API route. Keep the former
    # `/public/book` spelling bounded too while old links/hosts drain, so a
    # legacy request cannot introduce one metric dimension per customer slug.
    for public_prefix in ("/public/booking/", "/public/book/"):
        if public_prefix not in normalized:
            continue
        prefix, _, suffix = normalized.partition(public_prefix)
        tail = suffix.split("/", 1)
        normalized = f"{prefix}{public_prefix}:slug"
        if len(tail) > 1:
            normalized += f"/{tail[1]}"
        break
    if not any(
        marker in normalized
        for marker in (
            "/booking/",
            "/appointments",
            "/prospects/:id/appointments",
            "/public/booking/",
            "/public/book/",
        )
    ):
        return None
    return normalized[:180]


def emit(metric: str, value: float | int = 1, *, unit: str = "count", **dimensions: Any) -> None:
    """Write one structured metric event without ever raising to the caller."""

    try:
        context = request_context.current()
        payload = {
            "event": "booking_metric",
            "metric": metric[:96],
            "value": value,
            "unit": unit[:24],
            "request_id": context.request_id or None,
            "actor_user_id": str(context.actor_user_id) if context.actor_user_id else None,
            "actor_label": context.actor_label or None,
            "job": context.job or None,
            **{
                str(key)[:64]: value
                for key, value in dimensions.items()
                if value is not None
            },
        }
        log.info("%s", json.dumps(payload, default=str, separators=(",", ":"), sort_keys=True))
    except Exception:  # pragma: no cover - observability is intentionally best effort
        log.debug("booking metric emission failed", exc_info=True)
