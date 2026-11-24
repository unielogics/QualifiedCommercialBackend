"""Render the same bounded email template for an operator preview and actual delivery."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import tinycss2

from app.services.email.social_footer import with_social_footer

MERGE_FIELDS = ("first_name", "last_name", "full_name", "business_name", "email", "program")
_TOKEN = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_TAGS = set("p br hr div span center header footer main section article figure figcaption address font h1 h2 h3 h4 h5 h6 strong b em i u s small sub sup a img ul ol li blockquote table thead tbody tfoot tr td th caption".split())
_BLOCKS = set("p div center h1 h2 h3 h4 h5 h6 blockquote ul ol table caption".split())
_VOID = {"br", "hr", "img"}
_SHELL = {"html", "head", "title", "meta"}
_CSS_PROPERTIES = set("""
    color background background-color font font-family font-size font-weight font-style line-height
    text-align text-decoration text-transform letter-spacing vertical-align white-space word-break
    overflow-wrap width height min-width max-width max-height border border-width border-color
    border-style border-collapse border-spacing border-radius margin padding table-layout list-style-type
    display background-image background-size background-position background-repeat box-sizing
    object-fit text-size-adjust -webkit-text-size-adjust -ms-text-size-adjust
""".split())
for side in ("left", "right", "top", "bottom"):
    _CSS_PROPERTIES.update([f"padding-{side}", f"margin-{side}", f"border-{side}"])
    _CSS_PROPERTIES.update(f"border-{side}-{kind}" for kind in ("width", "style", "color"))


def _css_value(tokens, depth=0) -> str | None:
    if depth > 8:
        return None
    parts = []
    for token in tokens:
        kind = token.type
        if kind == "comment":
            continue
        if kind == "url":
            parts.append(f'url("{_web_url(token.value)}")')
        elif kind == "function" and token.lower_name == "url":
            args = [v for v in token.arguments if v.type not in {"comment", "whitespace"}]
            if len(args) != 1 or args[0].type != "string":
                raise ValueError("Use a complete HTTPS URL in CSS background images.")
            parts.append(f'url("{_web_url(args[0].value)}")')
        elif kind == "function":
            if token.lower_name == "expression":
                raise ValueError("Active CSS content is not supported.")
            if token.lower_name not in {"rgb", "rgba", "hsl", "hsla", "calc", "min", "max", "clamp", "linear-gradient"}:
                return None
            arg = _css_value(token.arguments, depth + 1)
            if arg is None:
                return None
            parts.append(f"{token.lower_name}({arg})")
        elif kind in {"number", "percentage", "dimension", "hash", "ident", "whitespace", "string", "literal"}:
            value = tinycss2.serialize([token])
            if not re.fullmatch(r"[\w\s.,+\-#%/*'\"]+", value):
                return None
            parts.append(value)
        else:
            return None
    return "".join(parts).strip()


def _styles(style) -> str:
    result = []
    for item in tinycss2.parse_declaration_list(style, skip_comments=True, skip_whitespace=True):
        if item.type != "declaration":
            continue
        if item.lower_name in {"behavior", "-moz-binding"}:
            raise ValueError("Active CSS content is not supported.")
        if item.lower_name in _CSS_PROPERTIES:
            value = _css_value(item.value)
            if value:
                result.append(f"{item.lower_name}:{value}" + (" !important" if item.important else ""))
    return ";".join(result)


_MEDIA = re.compile(
    r"(?:(?:only\s+)?(?:screen|all)\s+and\s+)?\(\s*(?:min|max)-width\s*:\s*\d+(?:\.\d+)?(?:px|em|rem)\s*\)",
    re.I,
)


def _stylesheet(rules, *, allow_media=True) -> str:
    result = []
    for rule in rules:
        if rule.type == "at-rule":
            if rule.lower_at_keyword == "import":
                raise ValueError("Use CSS inside the template instead of importing an external stylesheet.")
            condition = tinycss2.serialize(rule.prelude).strip()
            if allow_media and rule.lower_at_keyword == "media" and rule.content and _MEDIA.fullmatch(condition):
                nested = tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True)
                styles = _stylesheet(nested, allow_media=False)
                if styles:
                    result.append(f"@media {condition}{{{styles}}}")
        elif rule.type == "qualified-rule":
            selectors = []
            for selector in tinycss2.serialize(rule.prelude).strip().split(","):
                selector = selector.strip()
                if selector == ":root":
                    selector = "body"
                if not re.fullmatch(r"[a-zA-Z.#*_-][\w. #*>-]{0,179}", selector, re.ASCII):
                    continue
                selector = re.sub(r"(?<![\w.#-])(?:html|body)(?![\w-])", ".qc-mail-body", selector, flags=re.I)
                selectors.append(f".qc-mail-template {selector}")
            style = _styles(rule.content)
            if selectors and style:
                result.append(",".join(selectors) + "{" + style + "}")
    return "".join(result)


def _web_url(value: str) -> str:
    if re.search(r"[\x00-\x20\x7f\\<>\"'\x60]", value) or "{{" in value or "}}" in value:
        raise ValueError("Use a complete, literal HTTPS URL for links and images.")
    try:
        parts = urlsplit(value)
        valid = parts.scheme == "https" and parts.hostname and parts.username is None and parts.password is None
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use a complete HTTPS URL without credentials for links and images.")
    return value


def _values(values: Mapping[str, str] | None, locale: str) -> dict[str, str]:
    clean = {key: " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(val)).split())[:320] for key, val in (values or {}).items()
             if key in MERGE_FIELDS and val}
    full = clean.get("full_name", "")
    words = full.split(maxsplit=1)
    first = clean.get("first_name") or (words[0] if words else "")
    last = clean.get("last_name") or (words[1] if len(words) > 1 else "")
    es = locale == "es"
    return {
        "first_name": first or ("cliente" if es else "there"),
        "last_name": last or ("cliente" if es else "customer"),
        "full_name": full or " ".join(filter(None, (first, last))) or ("cliente" if es else "customer"),
        "business_name": clean.get("business_name") or ("su empresa" if es else "your business"),
        "email": clean.get("email") or ("su correo electrónico" if es else "your email address"),
        "program": clean.get("program") or ("financiamiento comercial" if es else "business financing"),
    }


def _merge(text: str, context: dict[str, str] | None) -> str:
    if context is None:
        return text
    parts = []
    pos = 0
    for match in _TOKEN.finditer(text):
        gap = text[pos:match.start()]
        if "{{" in gap or "}}" in gap or match[1] not in context:
            raise ValueError("Use a supported merge field such as {{first_name}} or {{program}}.")
        parts.extend((gap, context[match[1]]))
        pos = match.end()
    tail = text[pos:]
    if "{{" in tail or "}}" in tail:
        raise ValueError("Use a supported merge field inside matching double braces.")
    return "".join((*parts, tail))


class _Fragment(HTMLParser):
    """Rebuild the safe fragment from parsed tokens and derive its readable text alternative."""

    def __init__(self, context: dict[str, str] | None):
        super().__init__(convert_charrefs=True)
        self.context = context
        self.markup: list[str] = []
        self.plain: list[str] = []
        self.stack: list[tuple[str, str]] = []
        self.in_head = False
        self.in_style = False
        self.style_media = ""
        self.stylesheets: list[str] = []
        self.has_body = False

    def handle_starttag(self, tag, attrs):
        if any(key.startswith("on") for key, _ in attrs):
            raise ValueError("Remove event handlers from the email template.")
        if tag == "style":
            self.in_style = True
            self.style_media = (dict(attrs).get("media") or "").strip()
            return
        if tag in _SHELL or tag == "link":
            if tag == "head":
                self.in_head = True
            elif tag == "meta" and (dict(attrs).get("http-equiv") or "").lower() == "refresh":
                raise ValueError("Automatic redirects are not supported in email templates.")
            return
        if (tag != "body" and tag not in _TAGS) or self.in_head:
            raise ValueError(f"Unsupported HTML element <{tag}>. Use inline formatting, links, lists or tables.")
        safe = {}
        for key, raw in attrs:
            val = (raw or "").strip()
            if key == "style":
                safe[key] = _styles(val)
            elif (key == "href" and tag == "a") or (key == "src" and tag == "img") or (
                key == "background" and tag in {"body", "table", "td", "th"}
            ):
                safe[key] = _web_url(val)
            elif key in {"alt", "title"}:
                safe[key] = _merge(val, self.context)
            elif key in {"width", "height", "cellpadding", "cellspacing", "border", "colspan", "rowspan"} and re.fullmatch(r"\d{1,4}%?", val):
                safe[key] = val
            elif key in {"align", "valign", "bgcolor", "dir", "lang", "role"} and re.fullmatch(r"[\w#-]{1,40}", val):
                safe[key] = val
            elif key == "class":
                safe[key] = " ".join(v for v in val.split() if re.fullmatch(r"[\w-]{1,100}", v, re.ASCII))
            elif key == "id" and re.fullmatch(r"[\w-]{1,100}", val, re.ASCII):
                safe[key] = val
            elif tag == "font" and key in {"face", "size", "color"} and re.fullmatch(r"[\w ,#+-]{1,100}", val):
                safe[key] = val
            # Benign editor metadata and vendor-specific attributes do not travel.
        if tag == "body":
            safe["class"] = "qc-mail-body " + safe.get("class", "")
            self.has_body = True
        if "href" in safe:
            safe["rel"] = "noopener noreferrer"
        attrs_html = "".join(f' {k}="{html.escape(v, quote=True)}"' for k, v in safe.items())
        self.markup.append(f"<{'div' if tag == 'body' else tag}{attrs_html}>")
        if tag not in _VOID:
            self.stack.append((tag, safe.get("href", "")))
        if tag in _BLOCKS or tag == "hr":
            self.plain.append("\n\n")
        elif tag == "li":
            self.plain.append("\n• ")
        elif tag in {"br", "tr"}:
            self.plain.append("\n")
        elif tag in {"td", "th"}:
            self.plain.append("\t")
        elif tag == "img" and safe.get("alt"):
            self.plain.append(safe["alt"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
            return
        if tag in _VOID or tag in _SHELL or tag == "link":
            if tag == "head":
                self.in_head = False
            return
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("Check that the HTML tags are closed in the order they were opened.")
        _, href = self.stack.pop()
        self.markup.append(f"</{'div' if tag == 'body' else tag}>")
        if href:
            self.plain.append(f" ({href})")
        if tag in _BLOCKS:
            self.plain.append("\n\n")

    def handle_data(self, data):
        if self.in_style:
            rules = tinycss2.parse_stylesheet(data, skip_comments=True, skip_whitespace=True)
            css = _stylesheet(rules)
            if not self.style_media or self.style_media in {"screen", "all"}:
                self.stylesheets.append(css)
            elif _MEDIA.fullmatch(self.style_media):
                self.stylesheets.append(f"@media {self.style_media}{{{css}}}")
        elif not self.in_head:
            value = _merge(data, self.context)
            self.markup.append(html.escape(value))
            self.plain.append(re.sub(r"\s+", " ", value))

    def render(self, source: str) -> tuple[str, str]:
        try:
            self.feed(source)
            self.close()
        except AssertionError as exc:
            # HTMLParser reports malformed marked declarations as AssertionError.
            raise ValueError("Remove the invalid HTML declaration from the template.") from exc
        if self.stack or self.in_head or self.in_style:
            raise ValueError("Close the HTML elements before saving the template.")
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(self.plain))
        markup = "".join(self.markup)
        if not self.has_body:
            markup = f'<div class="qc-mail-body">{markup}</div>'
        return markup, re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    body_text: str
    body_html: str


def _content(subject, body, body_html, context):
    if not subject.strip() or re.search(r"[\x00-\x1f\x7f]", subject):
        raise ValueError("Enter a single-line email subject.")
    subject = _merge(subject, context)
    if not subject.strip() or re.search(r"[\x00-\x1f\x7f]", subject):
        raise ValueError("Personalization must produce a nonempty, single-line subject.")
    text = _merge(body or "", context).strip()
    styles = ""
    if body_html and body_html.strip():
        fragment = _Fragment(context)
        markup, derived_text = fragment.render(body_html)
        styles = "".join(fragment.stylesheets)
        if not derived_text:
            raise ValueError("The HTML needs readable content. Include text or descriptive image alt text.")
        text = text or derived_text
    else:
        markup = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in text.split("\n\n"))
    if not text:
        raise ValueError("Enter an email body in plain text or HTML.")
    return subject, text, markup, styles


def validate_template(subject: str, body: str, body_html: str | None = None) -> None:
    _content(subject, body, body_html, dict.fromkeys(MERGE_FIELDS, "Example"))


def render_message(*, subject: str, body: str, body_html: str | None = None,
                   values: Mapping[str, str] | None = None, locale: str = "en",
                   mailing_address: str, unsubscribe_url: str, personalize: bool = True) -> RenderedMessage:
    context = _values(values, locale) if personalize else None
    subject, text, markup, styles = _content(subject, body, body_html, context)
    unsub = "Cancelar suscripción" if locale == "es" else "Unsubscribe"
    text += f"\n\nQualified Commercial · {mailing_address}\n{unsub}: {unsubscribe_url}"
    wrapper = ('class="qc-mail-template"' if body_html and body_html.strip()
               else 'style="font-family:Arial,sans-serif;max-width:640px;margin:auto"')
    markup = (f'<div {wrapper}>{markup}</div><hr><p>Qualified Commercial · {html.escape(mailing_address)}<br>'
              f'<a href="{html.escape(unsubscribe_url, quote=True)}">{unsub}</a></p>')
    if body_html and body_html.strip():
        sheet = f"<style>{styles}</style>" if styles else ""
        markup = (f'<!doctype html><html lang="{("es" if locale == "es" else "en")}"><head>'
                  f'<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                  f'{sheet}</head><body>{markup}</body></html>')
    text, markup = with_social_footer(text, markup, locale)
    return RenderedMessage(subject, text, markup)
