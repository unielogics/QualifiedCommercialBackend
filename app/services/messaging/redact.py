"""Take the secrets out of the copy we keep.

Several of these messages carry a live credential: a secure-room PIN, a signing
token, a credit-consent link personal to one owner. Those paths deliberately
stored no body at all, which is why the log could never show what was sent.
Masking is what makes storing it safe — the preview shows the real message, and
a leaked read of the table opens nothing.

Two layers, in this order, because they fail differently:

1. **What the caller declares.** The code that just minted a token passes it in.
   Exact string replacement, so it cannot miss. This is an allowlist and it
   fails *safe*.
2. **A pattern backstop** for anything undeclared. This is a denylist, so it
   fails *open* on a shape nobody anticipated — which is exactly why layer 1
   exists and why every migrated call site should use it.

`public_underwriting_intake_email_sends` has done the same thing since it was
written: it stores `body_for_record`, with the one-time share passcode already
replaced. This generalises that.
"""

from __future__ import annotations

import re

MARKER = "[removed from the log]"

#: Query parameters whose value is a credential wherever it appears.
_SECRET_PARAMS = ("token", "t", "code", "pin", "passcode", "key", "secret", "access")

_PATTERNS: tuple[tuple[str, re.Pattern[str], object], ...] = (
    # The secure room: /buckets/request/<token>, and the fragment the handoff
    # puts the room PIN in. Keep the route, drop the secret — the preview still
    # says which link went.
    ("room_link", re.compile(r"(/buckets/request/)[A-Za-z0-9_\-]{6,}"), r"\1" + MARKER),
    ("room_pin_fragment", re.compile(r"#p=[A-Za-z0-9_\-]{4,}"), "#p=" + MARKER),
    # ?token=… / &t=… / ?code=… on any link we send.
    (
        "link_param",
        re.compile(r"([?&](?:" + "|".join(_SECRET_PARAMS) + r")=)[^\s&#\"'<>]{4,}", re.I),
        r"\1" + MARKER,
    ),
    # A bare code quoted next to the word for it: "your PIN is 104293".
    (
        "spelled_code",
        re.compile(r"\b(pin|code|passcode)\b([^\n\d]{0,24})\b\d{4,8}\b", re.I),
        lambda m: f"{m.group(1)}{m.group(2)}{MARKER}",
    ),
)


def mask_secrets(text: str | None, known: object = ()) -> tuple[str | None, list[str]]:
    """Return the text safe to store, and the names of what was removed.

    `known` is whatever the caller minted for this message. Those are replaced
    first and by exact match, so a secret that does not look like any pattern
    is still caught.
    """
    if not text:
        return text, []
    hits: list[str] = []
    out = text
    for secret in known or ():
        s = str(secret or "").strip()
        # Two characters is not a secret, it is a coincidence waiting to corrupt
        # the body it appears in.
        if len(s) < 4 or s not in out:
            continue
        out = out.replace(s, MARKER)
        hits.append("declared")
    for name, pattern, repl in _PATTERNS:
        out, n = pattern.subn(repl, out)
        if n:
            hits.append(name)
    return out, sorted(set(hits))


def mask_all(*texts: str | None, known: object = ()) -> tuple[list[str | None], list[str]]:
    """Mask several bodies (text and HTML) against one set of secrets, so both
    halves of a message agree about what was removed."""
    masked: list[str | None] = []
    hits: list[str] = []
    for text in texts:
        out, found = mask_secrets(text, known)
        masked.append(out)
        hits.extend(found)
    return masked, sorted(set(hits))
