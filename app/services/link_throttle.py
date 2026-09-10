"""How often one address may miss on a no-login token, in one place.

Every public link in this system is a bearer credential: the URL is the whole
answer, so a token that can be probed without cost can be found. The counter
that stops that was written once for the forwarded production package
(`production_packages._note_miss` / `_locked`) and would otherwise be written a
second time for the worksheet and a third for the next one. It is the same
counter, so it lives here.

**In process, and correct only under `--workers 1`.** The dictionary below is
this container's memory, not the database's: two workers would each keep their
own tally and a prober would get double the attempts. The API runs one worker
deliberately — the Dockerfile says why, APScheduler fires in process — so this
is accurate today, and it joins the list of things to move (to the row, or to
Redis) on the day that changes. It is a *throttle*, not the security boundary:
the PIN lockout that actually protects a link is persisted on the link row,
where a restart cannot forget it. Losing this tally on deploy costs a prober
nothing they could not get by waiting fifteen minutes anyway.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status

#: Ten misses from one address against one token in a quarter of an hour is
#: nobody's typo. Matches production_packages exactly, on purpose: two public
#: links that behave differently under probing teach a prober which is which.
MISS_LIMIT = 10
MISS_WINDOW = timedelta(minutes=15)

#: A shared link is a person typing, not a script. Twenty a second is a fast
#: human on a numeric keypad; two thousand an hour is a long working session
#: with room to spare. Past that the link is either compromised or looping, and
#: either way every write is also a broadcast to everyone else on the sheet.
WRITE_BURST_LIMIT = 20
WRITE_BURST_WINDOW = timedelta(seconds=1)
WRITE_HOURLY_LIMIT = 2000
WRITE_HOURLY_WINDOW = timedelta(hours=1)

_HITS: dict[str, list[datetime]] = defaultdict(list)


def _now() -> datetime:
    return datetime.now(UTC)


def note(key: str, *, window: timedelta) -> int:
    """Record one hit against `key` and return how many are live in `window`.

    Pruning on write keeps the dictionary from growing without bound: a key
    nobody has touched for a window is emptied the next time it is touched, and
    an untouched key holds at most a window's worth of timestamps.
    """
    now = _now()
    hits = [t for t in _HITS[key] if now - t < window]
    hits.append(now)
    _HITS[key] = hits
    return len(hits)


def count(key: str, *, window: timedelta) -> int:
    """How many hits are live in `window`, without recording one."""
    now = _now()
    hits = [t for t in _HITS[key] if now - t < window]
    _HITS[key] = hits
    return len(hits)


def miss_key(token: str, client_ip: str | None, *, prefix: str = "link") -> str:
    """The throttle key for one (token, address) pair.

    The token is hashed before it is used as a key: this dictionary outlives
    the request, and a raw bearer token sitting in process memory under a name
    a traceback would print is a token in a crash dump. The address is the one
    Caddy appended, never the one the client wrote — see request_context.
    """
    digest = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:16]}:{client_ip or '-'}"


def note_miss(key: str) -> None:
    note(key, window=MISS_WINDOW)


def locked(key: str) -> bool:
    return count(key, window=MISS_WINDOW) >= MISS_LIMIT


def too_many() -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes."
    )


def note_write(key: str) -> None:
    note(key, window=WRITE_BURST_WINDOW)
    note(f"{key}:hour", window=WRITE_HOURLY_WINDOW)


def write_capped(key: str) -> bool:
    """Whether this writer has run past either cap. Checked before the write is
    counted, so the limit is the number allowed rather than one less."""
    if count(key, window=WRITE_BURST_WINDOW) >= WRITE_BURST_LIMIT:
        return True
    return count(f"{key}:hour", window=WRITE_HOURLY_WINDOW) >= WRITE_HOURLY_LIMIT


def clear() -> None:
    """Forget every tally. For tests, and for nothing else — a route that
    called this would hand a prober an unlimited budget."""
    _HITS.clear()
