from types import SimpleNamespace

from app.dealer_os.router import _can_delete_documents, _document_month_keys
from app.dealer_os.services.buckets_link import audit_bucket_name
from app.enums import Role


def test_application_bucket_name_matches_application_name() -> None:
    dealer = SimpleNamespace(
        name="Qualified Commercial",
        legal_name="Qualified Commercial LLC",
        city="Garfield",
        state="NJ",
    )

    assert audit_bucket_name(dealer) == "Qualified Commercial"


def test_document_delete_policy_locks_non_admin_after_submission() -> None:
    assert _can_delete_documents(
        Role.FIELD_REP,
        application_submitted=False,
        package_evidence_exists=False,
    )
    assert not _can_delete_documents(
        Role.FIELD_REP,
        application_submitted=True,
        package_evidence_exists=False,
    )
    assert not _can_delete_documents(
        Role.LOAN_EXEC,
        application_submitted=False,
        package_evidence_exists=True,
    )
    assert _can_delete_documents(
        Role.SUPER_ADMIN,
        application_submitted=True,
        package_evidence_exists=True,
    )


def test_document_month_keys_ignore_malformed_values() -> None:
    document = SimpleNamespace(
        extracted={
            "months": [
                {"month": "2026-06"},
                {"month": "2026-13"},
                {"month": "June 2026"},
                None,
            ]
        },
        doc_meta={"pl_months": [{"month": "2026-07"}]},
    )

    assert {value.isoformat() for value in _document_month_keys(document)} == {"2026-06-01"}
    assert {
        value.isoformat() for value in _document_month_keys(document, "pl_months")
    } == {"2026-07-01"}
