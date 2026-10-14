"""Stable public aliases for internal partner-specific identifiers."""

import hashlib


def public_requirement_key(key: str | None) -> str | None:
    """Keep borrower correlations without exposing the source program key."""
    if key and key.startswith("arf_"):
        return f"requirement_{hashlib.sha256(key.encode()).hexdigest()[:24]}"
    return key
