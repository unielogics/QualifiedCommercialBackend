"""Adversarial verification of build_contract_templates.py's output.

For each generated document, re-flattens sections+placeholders back into
plain text (substituting each $placeholder with its raw original token so
the reconstruction is byte-for-byte comparable to source) and reports any
paragraph present in source but missing from the reconstruction (or vice
versa), plus a full field_schema dump for manual review of placeholder
naming.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

MOD_PATH = Path(__file__).resolve().parent.parent / "app" / "services" / "contract_templates_data.py"
spec = importlib.util.spec_from_file_location("contract_templates_data", MOD_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore
DATA = mod.CONTRACT_RAW_DATA

SRC_DIR = Path("C:/Users/franc/AppData/Local/Temp/qc_contracts")
DOCS = {
    "client_engagement": "eng_text.txt",
    "sba_engagement": "sba_text.txt",
    "referral_protection": "referral_text.txt",
    "platform_access": "platform_text.txt",
    "consulting_addendum": "fee_text.txt",
}


def reconstruct(doc: dict, include_headings: bool = True) -> str:
    parts = list(doc["cover"])
    if doc["notice"]:
        parts.append(doc["notice"])
    if doc["internal_notice"]:
        parts.append(doc["internal_notice"])
    parts.extend(doc["preamble"])
    for sec in doc["sections"]:
        if include_headings:
            parts.append(sec["heading"])
        parts.extend(sec["paragraphs"])
    text = "\n".join(parts)
    # Name-boundary-safe: match the longest possible $identifier token (word
    # chars only) so "$foo" never matches inside "$foo_2". A single regex
    # pass avoids the prefix-corruption bug of sequential str.replace calls.
    def sub(m: "re.Match") -> str:
        name = m.group(1)
        info = doc["field_schema"].get(name)
        raw = info.get("raw_token") if info else None
        return raw if raw else m.group(0)

    text = re.sub(r"\$([A-Za-z0-9_]+)", sub, text)
    return text


def normalize(s: str) -> str:
    import html
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


for key, filename in DOCS.items():
    doc = DATA[key]
    recon = reconstruct(doc)
    src_lines = [normalize(l) for l in (SRC_DIR / filename).read_text(encoding="utf-8").split("\n") if normalize(l)]
    recon_lines = [normalize(l) for l in recon.split("\n") if normalize(l)]
    src_set = set(src_lines)
    recon_set = set(recon_lines)
    missing_from_recon = [l for l in src_lines if l not in recon_set]
    extra_in_recon = [l for l in recon_lines if l not in src_set]
    print(f"\n=== {key} ===")
    print(f"  source lines: {len(src_lines)}, reconstructed: {len(recon_lines)}")
    print(f"  fields detected: {len(doc['field_schema'])}")
    if missing_from_recon:
        print(f"  MISSING FROM RECONSTRUCTION ({len(missing_from_recon)}):")
        for l in missing_from_recon[:15]:
            print(f"    - {l[:120]}")
    if extra_in_recon:
        print(f"  EXTRA IN RECONSTRUCTION ({len(extra_in_recon)}):")
        for l in extra_in_recon[:15]:
            print(f"    + {l[:120]}")
    if not missing_from_recon and not extra_in_recon:
        print("  OK: line sets match exactly")
