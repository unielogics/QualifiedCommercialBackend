from app.services.email.parser import extract_deal_id, inject_deal_id, parse_raw_email


def test_extract_deal_id_from_subject():
    assert extract_deal_id("[QC-15465354] Re: 418 Sycamore — UW conditions") == "15465354"


def test_extract_returns_none_when_missing():
    assert extract_deal_id("Re: just a generic email") is None


def test_inject_adds_id_when_missing():
    assert inject_deal_id("Re: 418 Sycamore", "L-2598") == "[QC-L-2598] Re: 418 Sycamore"


def test_inject_idempotent_when_already_present():
    s = "[QC-L-2598] Re: 418 Sycamore"
    assert inject_deal_id(s, "L-2598") == s


def test_parse_raw_email_extracts_deal_id():
    parsed = parse_raw_email(
        subject="[QC-L-2598] Insurance binder needed",
        body="Please send the binder by Friday.",
        sender="lender@example.com",
    )
    assert parsed.is_qc_deal is True
    assert parsed.deal_id == "L-2598"
