import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.email import gmail_client, ses_client, social_footer
from app.services.messaging.outbox import Draft


@pytest.mark.parametrize("transport", ["gmail", "ses_simple", "ses_raw"])
@pytest.mark.parametrize("footer_in_draft", [False, True])
def test_delivered_email_has_social_links_once_in_each_format(monkeypatch, transport, footer_in_draft):
    text, html = "Your appointment is confirmed.", "<HTML><BODY>Confirmed.</BODY></HTML>"
    if footer_in_draft:
        text, html = social_footer.with_social_footer(text, html)
    if transport == "gmail":
        built = gmail_client.build_message(
            to="recipient@example.test", from_email="qc@example.test", subject="Confirmation",
            body=text, body_html=html,
        )
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(built.raw_base64))
    else:
        client = Mock()
        settings = SimpleNamespace(ses_region="us-east-1", ses_from_address="qc@example.test", ses_configuration_set="")
        monkeypatch.setattr(ses_client, "get_settings", lambda: settings)
        monkeypatch.setattr(ses_client, "_ses_client", lambda region: client)
        if transport == "ses_raw":
            result = ses_client.send_raw_email(to_emails=["recipient@example.test"], subject="Confirmation",
                                               body_text=text, body_html=html)
            message = BytesParser(policy=policy.default).parsebytes(
                client.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
            )
        else:
            result = ses_client.send_email(to_email="recipient@example.test", subject="Confirmation",
                                           body_text=text, body_html=html)
            rendered = client.send_email.call_args.kwargs["Message"]["Body"]
        assert result.ok
    if transport != "ses_simple":
        text = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
    else:
        text = rendered["Text"]["Data"]
        html = rendered["Html"]["Data"]
    assert text.startswith("Your appointment is confirmed.")
    assert html.index("data-qc-social-footer") < html.index("</BODY>")
    for url in (social_footer.INSTAGRAM_URL, social_footer.FACEBOOK_URL):
        assert text.count(url) == html.count(url) == 1


def test_plain_text_email_keeps_its_original_format():
    text, html = social_footer.with_social_footer("Your code is ready.", None)
    assert "Your code is ready." in text and social_footer.INSTAGRAM_URL in text
    assert html is None


def test_outbox_localizes_the_invitation_before_audit_and_provider_handoff():
    draft = Draft(to="recipient@example.test", subject="Guía", body_text="Su guía.",
                  body_html="<p>Su guía.</p>", artifact_locale="es")
    prepared = draft.with_social_footer()
    text, html = social_footer.with_social_footer(prepared.body_text, prepared.body_html)
    for representation in (text, html):
        assert "Siga a Qualified Commercial" in representation
        assert "Follow Qualified Commercial" not in representation
        assert representation.count(social_footer.INSTAGRAM_URL) == 1
