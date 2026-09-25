from __future__ import annotations

import inspect

from app.routers import buckets


def test_bucket_file_delete_retains_storage_for_recovery() -> None:
    source = inspect.getsource(buckets.delete_admin_file)
    assert 'file.delete_storage_status = "retained_soft_delete"' in source
    assert "_delete_s3_object" not in source
    assert "selectinload(BucketFile.public_shares)" in source
    assert "file.public_shares.clear()" in source


def test_deleted_file_list_and_restore_routes_are_registered() -> None:
    contract = {
        (route.path, method)
        for route in buckets.router.routes
        for method in getattr(route, "methods", set())
    }
    assert any(path.endswith("/admin/{bucket_id}/deleted-files") and method == "GET" for path, method in contract)
    assert any(path.endswith("/admin/{bucket_id}/files/{file_id}/restore") and method == "POST" for path, method in contract)


def test_restore_rejects_rows_whose_storage_was_already_removed() -> None:
    source = inspect.getsource(buckets.restore_admin_file)
    assert "retained_soft_delete" in source
    assert '"delete_failed"' not in source
    assert "predates recovery retention" in source
