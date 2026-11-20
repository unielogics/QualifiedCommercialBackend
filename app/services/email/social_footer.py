"""QC's public social profiles on outbound platform email."""

from __future__ import annotations

import re

INSTAGRAM_URL = "https://www.instagram.com/qualifiedcommercial/"
FACEBOOK_URL = "https://www.facebook.com/profile.php?id=61596339571230"

_LINKS = f"Instagram: {INSTAGRAM_URL}\nFacebook: {FACEBOOK_URL}"
_INVITATIONS = {
    "en": "Follow Qualified Commercial for financing guides and lending updates.",
    "es": "Siga a Qualified Commercial para recibir guías de financiamiento y novedades sobre préstamos.",
}
_HTML = (
    '<p data-qc-social-footer="1" style="margin:20px 0 0;font-family:Arial,sans-serif;'
    'font-size:13px;line-height:1.6">{invitation}<br>'
    f'<a href="{INSTAGRAM_URL}">Instagram</a> &middot; '
    f'<a href="{FACEBOOK_URL}">Facebook</a></p>'
)


def with_social_footer(text: str, html: str | None, locale: str = "en") -> tuple[str, str | None]:
    invitation = _INVITATIONS.get(locale, _INVITATIONS["en"])
    if _LINKS not in text:
        footer = f"{invitation}\n{_LINKS}"
        text = f"{text.rstrip()}\n\n{footer}" if text.strip() else footer
    if html and 'data-qc-social-footer="1"' not in html:
        end = re.search(r"</body\s*>|</html\s*>", html, flags=re.IGNORECASE)
        index = end.start() if end else len(html)
        html = html[:index] + _HTML.format(invitation=invitation) + html[index:]
    return text, html
