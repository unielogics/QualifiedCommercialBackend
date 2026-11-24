import pytest

from app.services.email.social_footer import FACEBOOK_URL, INSTAGRAM_URL
from app.services.marketing_content import render_message, validate_template

ENVELOPE = {"mailing_address": "123 Example Ave", "unsubscribe_url": "https://example.test/u/demo"}


def test_html_only_generates_matching_accessible_text_and_localized_shared_footer():
    source = ('<html><head><title>Mail</title></head><body><table role="presentation" cellspacing="0"><tr>'
              '<td style="background-color:#fff;padding:12px;font-family:Arial,sans-serif">'
              '<h2>Hola {{first_name}}</h2><p>{{business_name}}</p><ul><li>Primero</li></ul>'
              '<a href="https://example.test/guia?a=1&amp;b=2">Leer la guía</a></td></tr></table></body></html>')
    validate_template("{{full_name}}", "", source)
    result = render_message(subject="{{full_name}}", body="", body_html=source,
                            values={"full_name": "Imani Example", "business_name": "<b>Empresa</b>"},
                            locale="es", **ENVELOPE)
    assert result.subject == "Imani Example"
    assert "Hola Imani" in result.body_text and "Leer la guía (https://example.test/guia?a=1&b=2)" in result.body_text
    assert "• Primero" in result.body_text and "Mail" not in result.body_text
    assert "&lt;b&gt;Empresa&lt;/b&gt;" in result.body_html and "<b>Empresa</b>" not in result.body_html
    for fmt in (result.body_text, result.body_html):
        assert "Cancelar suscripción" in fmt and ENVELOPE["unsubscribe_url"] in fmt
        assert fmt.count(INSTAGRAM_URL) == fmt.count(FACEBOOK_URL) == 1
        assert "Siga a Qualified Commercial" in fmt


@pytest.mark.parametrize("source", [
    '<script>1</script>', '<iframe src="https://example.test"></iframe>',
    '<a href="javascript:alert(1)">read</a>', '<a href="data:text/html,123">read</a>',
    '<a href="//example.test/">read</a>', '<a href="https://user@example.test">read</a>',
    '<a href="{{program}}">read</a>', '<span onclick="doSomething()">read</span>',
    '<div style="background:url(data:image/svg+xml,test)">x</div>',
    '<div style="color:expression(1)">x</div>', '<svg><image></image></svg>',
    '<img src="data:text/html,123">', '<p><i>bad nesting</p></i>', '<p> </p>', '<![BOGUS]>hi',
])
def test_unsafe_invalid_or_empty_html_is_rejected(source):
    with pytest.raises(ValueError):
        validate_template("A subject", "", source)


def test_plain_fallback_preserves_newlines_and_html_uses_plain_body_when_html_is_omitted():
    result = render_message(subject="{{business_name}}", body="Hola {{first_name}}\n{{program}}\n\nGracias.",
                            locale="es", **ENVELOPE)
    assert result.subject == "su empresa"
    assert "Hola cliente<br>financiamiento comercial</p><p>Gracias." in result.body_html
    assert "Hola cliente\nfinanciamiento comercial\n\nGracias." in result.body_text


def test_explicit_plain_alternative_and_safe_values_substitute_once():
    result = render_message(subject="Hello {{first_name}}", body="Read by email: {{email}}",
                            body_html="<p>News for {{last_name}}</p>",
                            values={"full_name": "Jessie Doe", "email": "jessie@example.test",
                                    "first_name": "{{program}}\nLine"}, **ENVELOPE)
    assert result.subject == "Hello {{program}} Line"
    assert "jessie@example.test" in result.body_text and "News for Doe" in result.body_html


def test_unknown_or_unbalanced_merge_tags_are_rejected_and_legacy_plain_is_literal():
    for source in ("{{ssn}}", "{{program}", "{{ business.personal_name }}"):
        with pytest.raises(ValueError, match="merge field"):
            validate_template("Subject", source)
    result = render_message(subject="{{old}}", body="{{arbitrary}}", personalize=False, **ENVELOPE)
    assert result.subject == "{{old}}" and result.body_text.startswith("{{arbitrary}}")


def test_exported_responsive_newsletter_preserves_its_own_layout():
    source = """<!DOCTYPE html><html xmlns:v="urn:schemas-microsoft-com:vml">
    <head><meta name="viewport" content="width=device-width,initial-scale=1">
    <link rel="preconnect" href="https://example.test">
    <style type="text/css">
      body { margin: 0; -webkit-text-size-adjust:100%; }
      .hero, .brand > td {background-color:#163957; color:#fff; mso-line-height-rule:exactly;}
      @media only screen and (max-width: 600px) {
        .container { width:100% !important; }
      }
    </style></head><body class="newsletter" style="background-color:#eef;">
    <table role="presentation" width="760" cellpadding="0" cellspacing="0" class="container" data-export="custom">
    <tr class="brand"><td class="hero" style="mso-table-lspace:0pt;padding:24px">
    <img src="https://example.test/logo.png" alt="QC logo" width="90">
    <p>Hello {{first_name}}</p><a href="https://example.test/programs">Explore programs</a>
    </td></tr></table></body></html>"""
    result = render_message(subject="Campaign", body="", body_html=source, **ENVELOPE)
    markup = result.body_html
    assert '<head>' in markup and '.qc-mail-template .hero' in markup
    assert '@media only screen and (max-width: 600px)' in markup and 'width:100% !important' in markup
    assert 'mso-' not in markup and 'data-export' not in markup and 'max-width:640px' not in markup
    assert 'class="container"' in markup and 'class="hero"' in markup and 'width="760"' in markup
    assert 'padding:24px' in markup and 'https://example.test/logo.png' in markup
    assert "QC logo" in result.body_text and 'Explore programs (https://example.test/programs)' in result.body_text
    assert result.body_text.count("mso") == 0 and 'Follow Qualified Commercial' in markup


def test_newsletter_conditional_comments_do_not_trigger_declaration_validation():
    template = ('<!DOCTYPE html><!--[if mso]><table><tr><td><![endif]-->'
                '<p>Visible newsletter</p><!-- sample: <![BOGUS]> -->')
    result = render_message(subject="News", body="", body_html=template, **ENVELOPE)
    assert "Visible newsletter" in result.body_html and "BOGUS" not in result.body_html


@pytest.mark.parametrize("style", [
    "@import url(https://example.test/foreign.css);",
    ".a{background-image:url(javascript:bad)}",
    ".a{color:expression(1)}",
    ".a{behavior:url(https://example.test/legacy.htc)}",
    ".a{background-image:url('https://example.test/\\3c bad')}",
])
def test_stylesheet_rejects_active_content_and_unsafe_resources(style):
    with pytest.raises(ValueError):
        validate_template("Newsletter", "", f"<head><style>{style}</style></head><p>hello</p>")


def test_scoped_https_background_and_missing_merge_fields():
    from app.services.marketing_content import MERGE_FIELDS

    result = render_message(subject="News", body="", body_html=(
        '<style>.card{background-image:url("https://example.test/cover.png")}</style>'
        '<p class="card">Cover</p>'
    ), **ENVELOPE)
    assert '.qc-mail-template .card{background-image:url("https://example.test/cover.png")}' in result.body_html
    for field in MERGE_FIELDS:
        tag = "{{" + field + "}}"
        for locale in ("en", "es"):
            result = render_message(subject=tag, body="", body_html=f"<p>{tag}</p>", locale=locale,
                                    values={"first_name": "\x00"}, **ENVELOPE)
            assert result.subject.strip() and "\x00" not in result.subject
