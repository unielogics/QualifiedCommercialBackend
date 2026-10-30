"""The AI intake's route table is exactly what it was before the handlers moved.

Splitting app/routers/dealer_ai_intake.py into per-router modules is a layout
change and nothing else. This snapshot was taken from the single-file module
on 2026-10-30; every (router, method, path, handler) must still be present,
and nothing new may appear by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.routers import dealer_ai_intake

ROUTERS = ["router", "funding_router", "mca_router", "client_router", "admin_router", "broker_router"]


def _route_table() -> list[list[str]]:
    rows: set[tuple[str, str, str, str]] = set()
    for name in ROUTERS:
        for route in getattr(dealer_ai_intake, name).routes:
            for method in sorted(getattr(route, "methods", []) or []):
                rows.add((name, method, route.path, getattr(route.endpoint, "__name__", "")))
    return [list(row) for row in sorted(rows)]


def test_the_route_table_is_unchanged_by_the_split() -> None:
    snapshot = json.loads((Path(__file__).parent / "_dealer_ai_intake_routes_snapshot.json").read_text())
    assert _route_table() == snapshot


def test_every_handler_is_still_reachable_on_the_old_module() -> None:
    # Tests and callers read handlers as attributes of the module they always lived in.
    for name in ROUTERS:
        for route in getattr(dealer_ai_intake, name).routes:
            handler = getattr(route, "endpoint", None)
            if handler is None:
                continue
            assert getattr(dealer_ai_intake, handler.__name__) is handler, handler.__name__


def test_helpers_the_suite_patches_still_live_on_the_old_module() -> None:
    # The reason the helpers did not move: patching these names must keep biting.
    for name in ("_log", "_read_bucket_object", "_put_bucket_object", "_bucket_storage_config", "_load_admin_dealer_lead", "_require_super_admin"):
        assert callable(getattr(dealer_ai_intake, name)), name


def test_every_name_the_handlers_reach_through_core_resolves() -> None:
    # A handler's `core.X` is looked up at call time, so a missing import in the
    # core module would surface as an AttributeError on a live request rather
    # than at import. Pin it statically.
    import re

    routes_dir = Path(dealer_ai_intake.__file__).parent / "dealer_ai_intake_routes"
    missing: set[str] = set()
    for module_file in sorted(routes_dir.glob("*.py")):
        for name in set(re.findall(r"\bcore\.([A-Za-z_][A-Za-z0-9_]*)", module_file.read_text())):
            if not hasattr(dealer_ai_intake, name):
                missing.add(name)
    assert not missing, sorted(missing)
