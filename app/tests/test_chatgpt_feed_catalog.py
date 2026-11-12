from __future__ import annotations

import csv
import gzip
import io
from unittest.mock import AsyncMock, patch

import pytest

from app.services.chatgpt_feed_catalog import (
    build_preview,
    catalog_preview,
    retirement_snapshot_preview,
    snapshot_bytes,
)


def program(key="merchant_cash_advance", **values):
    return {"program_key": key, "public_slug": key.replace("_", "-"),
            "name": key, "short_description": "Approved business financing.",
            "commercial_terms": {"version": 1, "status": "published", "minimum_amount": 25000,
                                 "maximum_amount": 350000, "pricing_basis": "factor_rate",
                                 "minimum_pricing": 1.22, "maximum_pricing": 1.38}, **values}


def approved_config(programs):
    preview = catalog_preview(programs, {})
    return {"acceptance_confirmed": True, "acceptance_reference": "Confirmed by OpenAI account team",
            "catalog_overrides": {
                item["item_id"]: {"included": item["item_id"] != "capital_os", "actual_price": "149.99",
                                  "content_approved": True, "pricing_confirmed": True,
                                  "approved_source_revision": item["source_revision"]}
                for item in preview["items"]}}


def test_default_is_paused_with_no_fabricated_price_or_claim():
    preview = catalog_preview([program()], {})
    assert not preview["ready_for_upload"]
    assert preview["eligible_count"] == 0
    mca = preview["items"][0]
    assert mca["item_id"] == "merchant_cash_advance"
    assert mca["price"] is None
    assert "factor rate is not an interest rate or APR" in mca["description"]
    assert "1.22" not in mca["description"]
    assert all(item["price"] is None for item in preview["items"])


def test_stable_snapshot_round_trips_plain_text_quotes_and_unicode():
    programs = [program()]
    config = approved_config(programs)
    config["catalog_overrides"]["merchant_cash_advance"]["description"] = 'Review, plan and "prepare" — together.'
    preview = catalog_preview(programs, config)
    assert preview["ready_for_upload"]
    blob = snapshot_bytes(preview)
    assert blob == snapshot_bytes(preview)
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(blob).decode("utf-8"))))
    assert len(rows) == 1
    assert rows[0]["price"] == "149.99 USD"
    assert rows[0]["description"] == 'Review, plan and "prepare" — together.'
    assert rows[0]["is_eligible_checkout"] == "false"
    assert rows[0]["is_eligible_search"] == "true"


def test_latest_source_requires_renewed_approval():
    programs = [program()]
    config = approved_config(programs)
    programs[0]["commercial_terms"]["version"] = 2
    preview = catalog_preview(programs, config)
    assert not preview["ready_for_upload"]
    assert any("source has changed" in issue for issue in preview["items"][0]["issues"])


def test_unpublished_terms_and_private_products_never_become_rows():
    rows = [program(), program("arf_private", name="Private lender identity"),
            program("retired", status="retired")]
    config = approved_config(rows)
    rows[0]["commercial_terms"]["status"] = "draft"
    preview = catalog_preview(rows, config)
    assert {item["item_id"] for item in preview["items"]} == {"merchant_cash_advance", "capital_os"}
    assert not preview["ready_for_upload"]
    assert any("No published" in issue for issue in preview["items"][0]["issues"])


@pytest.mark.parametrize("price", [None, "", 0, -1, "1.999", "NaN", "Infinity", True, "149.99 USD", "1,000"])
def test_invalid_purchase_prices_cannot_be_exported(price):
    rows = [program()]
    config = approved_config(rows)
    config["catalog_overrides"]["merchant_cash_advance"]["actual_price"] = price
    preview = catalog_preview(rows, config)
    assert not preview["ready_for_upload"]
    with pytest.raises(ValueError):
        snapshot_bytes(preview)


@pytest.mark.parametrize("url", ["http://example.com/a", "https://localhost/a", "https://127.0.0.1/a",
                                "https://169.254.169.254/a", "https://user:secret@example.com/a",
                                "https://example.com/a?token=secret", "https://example.com/a#secret"])
def test_private_and_credential_bearing_urls_block_export(url):
    rows = [program()]
    config = approved_config(rows)
    config["catalog_overrides"]["merchant_cash_advance"]["canonical_url"] = url
    assert not catalog_preview(rows, config)["ready_for_upload"]


def test_partial_validity_requires_explicit_exclusion_instead_of_silent_drop():
    rows = [program(), program("new_unknown_program")]
    config = approved_config(rows)
    preview = catalog_preview(rows, config)
    assert preview["eligible_count"] == 1
    assert not preview["ready_for_upload"]
    config["catalog_overrides"]["new_unknown_program"]["included"] = False
    assert catalog_preview(rows, config)["ready_for_upload"]


def test_mca_and_refinance_remain_separate_with_expected_priority():
    rows = [program("mca_refinance"), program("sba_7a"), program("quidity_10_year_term"),
            program("ez_term"), program()]
    preview = catalog_preview(rows, {})
    assert [item["item_id"] for item in preview["items"]] == [
        "merchant_cash_advance", "ez_term", "quidity_10_year_term", "capital_os", "sba_7a", "mca_refinance"]
    assert preview["items"][0]["url"] != preview["items"][-1]["url"]


def test_empty_catalog_duplicate_ids_and_empty_snapshots_fail_closed():
    assert not catalog_preview([], {})["ready_for_upload"]
    with pytest.raises(ValueError, match="duplicate"):
        catalog_preview([program(), program()], {})
    with pytest.raises(ValueError, match="empty"):
        snapshot_bytes({"ready_for_upload": True, "items": []})


def test_retired_rows_keep_explicit_search_disable():
    rows = [program()]
    preview = catalog_preview(rows, approved_config(rows))
    preview["items"][0]["is_eligible_search"] = False
    parsed = list(csv.DictReader(io.StringIO(gzip.decompress(snapshot_bytes(preview)).decode())))
    assert parsed[0]["is_eligible_search"] == "false"


async def test_builder_uses_only_approved_public_catalog_and_propagates_failure():
    with patch("app.services.chatgpt_feed_catalog.funding_programs.public_catalog", new_callable=AsyncMock) as public:
        public.return_value = [program()]
        await build_preview(object(), {})
        public.assert_awaited_once()
        public.side_effect = RuntimeError("Source unavailable")
        with pytest.raises(RuntimeError, match="Source unavailable"):
            await build_preview(object(), {})


def test_last_exported_offer_can_be_retired_with_a_nonempty_disable_snapshot():
    programs = [program()]
    config = approved_config(programs)
    previous = [item for item in catalog_preview(programs, config)["items"] if item["eligible"]]
    current = catalog_preview([], config)
    retirement = retirement_snapshot_preview(current, previous, config, {"merchant_cash_advance"})
    assert retirement["ready_for_upload"]
    assert retirement["eligible_count"] == 0
    assert retirement["export_count"] == 1
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(snapshot_bytes(retirement)).decode())))
    assert len(rows) == 1
    assert rows[0]["price"] == previous[0]["price"]
    assert rows[0]["is_eligible_search"] == "false"


def test_absence_or_invalid_replacement_is_not_a_retirement():
    programs = [program()]
    config = approved_config(programs)
    previous = [item for item in catalog_preview(programs, config)["items"] if item["eligible"]]
    assert not retirement_snapshot_preview(catalog_preview([], config), previous, config, set())["ready_for_upload"]
    config["catalog_overrides"]["merchant_cash_advance"]["actual_price"] = None
    invalid = retirement_snapshot_preview(catalog_preview(programs, config), previous, config, set())
    assert not invalid["ready_for_upload"]
    assert invalid["disabled_count"] == 0
    config["catalog_overrides"]["merchant_cash_advance"]["included"] = False
    excluded = retirement_snapshot_preview(catalog_preview(programs, config), previous, config, set())
    assert excluded["ready_for_upload"]
    assert excluded["disabled_count"] == 1
    assert len({item["item_id"] for item in excluded["items"]}) == len(excluded["items"])


def test_disable_records_survive_later_snapshots_until_reactivated():
    original = [program()]
    config = approved_config(original)
    previous = [item for item in catalog_preview(original, config)["items"] if item["eligible"]]
    removed = retirement_snapshot_preview(catalog_preview([], config), previous, config, {"merchant_cash_advance"})
    persisted = [item for item in removed["items"] if item["eligible"]]
    replacement = [program("ez_term")]
    next_config = approved_config(replacement)
    next_snapshot = retirement_snapshot_preview(catalog_preview(replacement, next_config), persisted, next_config, set())
    assert next_snapshot["ready_for_upload"]
    assert next_snapshot["disabled_count"] == 1
    assert {item["item_id"] for item in next_snapshot["items"] if item["eligible"]} == {"merchant_cash_advance", "ez_term"}
    restored = retirement_snapshot_preview(catalog_preview(original, config), persisted, config, set())
    assert restored["ready_for_upload"]
    assert restored["disabled_count"] == 0
    assert restored["items"][0]["is_eligible_search"] is True
