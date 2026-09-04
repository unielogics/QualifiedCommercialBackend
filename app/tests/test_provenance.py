"""Where a file came from, and where every document in it came from.

The trail exists because none of this was answerable before: an operator's
upload was attributed to the client because the browser pre-filled the client's
name, a field rep's upload arrived in the lead file as "Capital OS" with the rep
unrecoverable, and every intake upload wrote an audit row claiming the public
lead did it. These tests pin the rules that stop it happening again.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.bucket import BucketFile
from app.services import provenance


def _user(role="super_admin", name="Jonathan Franco"):
    return SimpleNamespace(id=uuid4(), name=name, email="jf@example.com", role=role)


# --- the vocabulary is closed ------------------------------------------------


def test_an_unknown_source_is_refused_rather_than_stored():
    with pytest.raises(ValueError):
        provenance.document_origin("smuggled")
    with pytest.raises(ValueError):
        provenance.file_origin("smuggled")


def test_the_two_vocabularies_do_not_quietly_overlap():
    # A file is "started"; a document "arrives". Sharing a token between them
    # would make describe() ambiguous about which level it is talking about.
    shared = set(provenance.FILE_SOURCES) & set(provenance.DOCUMENT_SOURCES)
    assert shared == {"field_rep", "dealer_partner"}, shared


def test_every_source_has_words_for_an_operator():
    for kind in {**provenance.FILE_SOURCES, **provenance.DOCUMENT_SOURCES}:
        label = provenance.Origin(kind=kind).label
        assert label and label != kind, kind


# --- the source follows the authenticated role, never the request body -------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("field_rep", "field_rep"),
        ("dealer_partner", "dealer_partner"),
        ("broker", "dealer_partner"),
        ("client", "client_room"),
        ("dealer", "client_room"),
        ("super_admin", "internal_upload"),
        ("loan_exec", "internal_upload"),
        ("", "internal_upload"),
    ],
)
def test_a_signed_in_uploader_is_classified_by_their_role(role, expected):
    assert provenance.document_source_for(_user(role=role)) == expected


def test_the_audit_role_follows_the_source_instead_of_always_saying_public_lead():
    # This is the bug the trail was built around: every intake upload logged
    # "public_lead", so a super admin's upload claimed to be the borrower's.
    assert provenance.actor_role_for("internal_upload") == "operator"
    assert provenance.actor_role_for("field_rep") == "field_rep"
    assert provenance.actor_role_for("dealer_partner") == "dealer_partner"
    # The client's own upload still is the public lead.
    assert provenance.actor_role_for("client_room") == "public_lead"


def test_a_source_with_no_human_is_not_mistaken_for_a_lost_identity():
    for kind in provenance.UNATTENDED_SOURCES:
        assert provenance.actor_role_for(kind) == "system"


# --- describing it -----------------------------------------------------------


def test_an_origin_reads_as_one_line_with_who_and_how():
    origin = provenance.document_origin("field_rep", actor=_user(role="field_rep", name="Dana Ruiz"), detail="Rep app")
    assert origin.describe() == "Uploaded by a field rep · Dana Ruiz · Rep app"
    assert origin.actor_user_id


def test_an_actor_falls_back_to_their_email_when_unnamed():
    actor = SimpleNamespace(id=uuid4(), name="", email="dana@example.com")
    assert provenance.document_origin("internal_upload", actor=actor).actor_name == "dana@example.com"


def test_detail_is_capped_so_a_long_path_cannot_overflow_the_column():
    origin = provenance.document_origin("drive", detail="x" * 500)
    assert len(origin.detail) == 200


def test_a_row_written_before_the_trail_says_so_instead_of_guessing():
    legacy = BucketFile(file_name="statement.pdf", uploaded_by_name="Loyd Bradley")
    assert provenance.describe_document(legacy) == "Uploaded by Loyd Bradley"

    blank = BucketFile(file_name="mystery.pdf")
    assert provenance.describe_document(blank) == "Source not recorded"


def test_a_stamped_row_reads_as_the_channel_it_came_through():
    row = BucketFile(file_name="bank.pdf", source_kind="client_room", uploaded_by_name="Loyd Bradley")
    assert provenance.describe_document(row) == "Client uploaded in their room · Loyd Bradley"


def test_the_file_row_exposes_the_same_line_to_the_api():
    row = BucketFile(file_name="x.pdf", source_kind="capital_os", source_detail="Capital OS")
    assert row.source_label == "Mirrored from Capital OS · Capital OS"


def test_a_file_origin_reads_as_how_the_file_was_started():
    row = SimpleNamespace(source_kind="public_form", source_actor_name="", source_detail="Dealer intake form")
    assert provenance.describe_file(row) == "Public form · Dealer intake form"

    internal = SimpleNamespace(source_kind="internal_user", source_actor_name="Jonathan Franco", source_detail="")
    assert provenance.describe_file(internal) == "Created internally · Jonathan Franco"

    unknown = SimpleNamespace(source_kind=None, source_actor_name="", source_detail="")
    assert provenance.describe_file(unknown) == "Source not recorded"


# --- the columns are actually on the rows ------------------------------------


def test_a_document_row_can_hold_the_person_not_just_their_name():
    # uploaded_by_name is a display string the client's own browser can supply;
    # the user id cannot be, which is the whole point.
    actor = _user()
    row = BucketFile(
        file_name="x.pdf",
        uploaded_by_name=actor.name,
        uploaded_by_user_id=actor.id,
        source_kind="internal_upload",
    )
    assert row.uploaded_by_user_id == actor.id
    assert row.source_kind == "internal_upload"


def test_every_model_that_holds_a_file_or_document_has_the_columns():
    from app.dealer_os.models import DealerBusiness, DealerDocument
    from app.models.public_underwriting_intake import PublicUnderwritingIntake

    for model, columns in (
        (BucketFile, ("source_kind", "source_detail", "uploaded_by_user_id")),
        (DealerDocument, ("source_kind", "source_detail", "uploaded_by_name", "uploaded_by_user_id")),
        (PublicUnderwritingIntake, ("source_kind", "source_detail", "source_actor_name", "source_user_id")),
        (DealerBusiness, ("source_kind", "source_detail", "source_actor_name", "source_user_id")),
    ):
        for column in columns:
            assert column in model.__table__.columns, f"{model.__name__}.{column}"
