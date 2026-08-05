"""Builds app/services/contract_templates.py mechanically from the
paragraph-per-line .txt dumps of the 5 source .docx contracts.

This script does NOT use an LLM and does NOT rewrite any legal wording. It:
  1. Splits each document's lines into: cover block, NOTICE paragraph,
     DRAFT-FOR-ATTORNEY-REVIEW paragraph, preamble paragraph, and body
     sections (RECITALS / ARTICLE N / SCHEDULE X / EXHIBIT N), dropping the
     TABLE OF CONTENTS (pure cross-reference, no contractual content) and
     the trailing signature block (ACKNOWLEDGMENT / IN WITNESS WHEREOF
     onward — the app renders its own typed-name/signature-pad UI in place
     of this, so the blank signature lines are not stored as body text).
  2. Detects fill-in blanks via regex (runs of underscores, optionally
     preceded by "$" or followed by "%", and bracketed placeholder tokens
     like "[CLIENT LEGAL NAME]") and replaces each with a named
     string.Template placeholder ($field_name), assigning names from
     nearby label context. Every substitution is 1:1 on the ORIGINAL
     characters — no surrounding prose is altered.
  3. Emits one Python dict literal per document (title, effective date
     label, preamble, notice, internal_notice, sections, field defaults)
     into a single generated file, which contract_templates.py then wraps
     with the rendering functions.

Usage: python scripts/build_contract_templates.py
Reads from C:/Users/franc/AppData/Local/Temp/qc_contracts/*_text.txt
Writes app/services/contract_templates_data.py
"""

from __future__ import annotations

import html
import re
from pathlib import Path

SRC_DIR = Path("C:/Users/franc/AppData/Local/Temp/qc_contracts")
OUT_FILE = Path(__file__).resolve().parent.parent / "app" / "services" / "contract_templates_data.py"

# Case-SENSITIVE: real section headings are always ALL CAPS ("ARTICLE 1
# Definitions", "SCHEDULE 1"). The Table of Contents lines that reference
# them are mixed-case ("Article 1. Definitions") and must NOT match here,
# or ToC lines get mistaken for real section boundaries.
HEADING_PATTERNS = [
    re.compile(r"^ARTICLE\s+\d+\b"),
    re.compile(r"^SCHEDULE\s+[A-Z0-9]+\b"),
    re.compile(r"^EXHIBIT\s+[A-Z0-9]+\b"),
]
PART_PATTERN = re.compile(r"^Part\s+\d+\s+[—-]\s+", re.IGNORECASE)
# Where the contractual body ends and the pure signature-block boilerplate
# begins (typed-name/signature blanks the app's own UI replaces).
SIGNATURE_BLOCK_START = re.compile(
    r"^(ACKNOWLEDGMENT|IN WITNESS WHEREOF|CERTIFIED BY CLIENT|AUTHORIZING PARTY|"
    r"ACKNOWLEDGED BY (CLIENT|REFERRAL PARTNER)|USER$|GUARANTOR$|REPRESENTATIVE$)"
)

# Blank-detection, in priority order (most specific first).
BLANK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\[CLIENT LEGAL NAME\]"), "client_legal_name"),
    (re.compile(r"\[REFERRAL PARTNER LEGAL NAME\]"), "referral_partner_legal_name"),
    (re.compile(r"\[INDIVIDUAL NAME\]"), "individual_name"),
    (re.compile(r"\[state\] \[entity type\]"), "client_entity_type_state"),
    (re.compile(r"______________, 20____"), "effective_date"),
    (re.compile(r"\$__________\."), "$__AMOUNT_DOLLAR_PERIOD__"),  # handled specially below
]

FIELD_NAME_CACHE: dict[str, int] = {}


def unescape(line: str) -> str:
    return html.unescape(line).strip()


def slugify_context(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or "field"


def next_unique(name: str) -> str:
    n = FIELD_NAME_CACHE.get(name, 0)
    FIELD_NAME_CACHE[name] = n + 1
    return name if n == 0 else f"{name}_{n + 1}"


def replace_blanks_in_paragraph(text: str, doc_key: str, field_schema: dict) -> str:
    """Replace every blank in `text` with a $placeholder, registering each
    new placeholder (keyed by content-derived name) into field_schema."""
    # Bracketed named placeholders first (exact literal replace, no naming needed).
    for pattern, name in BLANK_PATTERNS[:4]:
        m = pattern.search(text)
        if m:
            field_schema.setdefault(
                name, {"label": name.replace("_", " ").title(), "default": "", "raw_token": m.group(0)}
            )
            text = pattern.sub(f"${name}", text)
    m = BLANK_PATTERNS[4][0].search(text)
    if m:
        field_schema.setdefault(
            "effective_date", {"label": "Effective Date", "default": "", "raw_token": m.group(0)}
        )
        text = BLANK_PATTERNS[4][0].sub("$effective_date", text)

    # Generic underscore/blank runs: "______", "$________", "________%",
    # "____%", "___ County". Name each from the nearest preceding label
    # word(s) on the same line (heuristic — reviewed by hand after generation).
    def make_name(prefix_context: str, suffix: str) -> str:
        words = re.findall(r"[A-Za-z][A-Za-z']*", prefix_context)[-4:]
        base = slugify_context(" ".join(words)) if words else "blank"
        base = f"{doc_key}_{base}{suffix}"
        return next_unique(base)

    out = []
    pos = 0
    for m in re.finditer(r"\$?_{3,}%?|_{3,}", text):
        out.append(text[pos:m.start()])
        token = m.group(0)
        suffix = "_amount" if token.startswith("$") else ("_pct" if token.endswith("%") else "")
        prefix_context = "".join(out)[-60:]
        name = make_name(prefix_context, suffix)
        field_schema[name] = {
            "label": name.replace(f"{doc_key}_", "").replace("_", " ").title(),
            "default": "",
            "raw_token": token,
        }
        out.append(f"${name}")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def split_document(lines: list[str]) -> dict:
    lines = [unescape(l) for l in lines if unescape(l)]
    cover: list[str] = []
    i = 0
    while i < len(lines) and lines[i] != "RECITALS" and not any(p.match(lines[i]) for p in HEADING_PATTERNS):
        cover.append(lines[i])
        i += 1

    notice = None
    internal_notice = None
    preamble_paras: list[str] = []
    cover_clean: list[str] = []
    for line in cover:
        if line.startswith("NOTICE."):
            notice = line
        elif line.startswith("DRAFT FOR ATTORNEY REVIEW."):
            internal_notice = line
        elif line == "TABLE OF CONTENTS" or line.startswith(("Recitals", "Article ", "Schedule ", "Exhibit ")):
            continue  # ToC entries
        else:
            cover_clean.append(line)

    sections: list[dict] = []
    current_heading = None
    current_paras: list[str] = []
    skipping_toc = False
    hit_signature_block = False

    def flush():
        nonlocal current_heading, current_paras
        if current_heading is not None and not skipping_toc and current_paras:
            sections.append({"heading": current_heading, "paragraphs": current_paras})
        current_heading, current_paras = None, []

    while i < len(lines):
        line = lines[i]
        if SIGNATURE_BLOCK_START.match(line):
            hit_signature_block = True
        if line == "TABLE OF CONTENTS":
            flush()
            current_heading = line
            skipping_toc = True
            i += 1
            continue
        if line == "RECITALS" or any(p.match(line) for p in HEADING_PATTERNS):
            flush()
            skipping_toc = False
            hit_signature_block = False
            current_heading = line
            i += 1
            continue
        if PART_PATTERN.match(line) and current_heading is not None:
            flush()
            skipping_toc = False
            current_heading = f"{sections[-1]['heading'].split(' ', 2)[0] if sections else ''} {line}".strip()
            i += 1
            continue
        if current_heading is None:
            preamble_paras.append(line)
            i += 1
            continue
        if not hit_signature_block:
            current_paras.append(line)
        i += 1
    flush()

    return {
        "cover": cover_clean,
        "notice": notice,
        "internal_notice": internal_notice,
        "preamble": preamble_paras,
        "sections": sections,
    }


def process(doc_key: str, filename: str) -> dict:
    text = (SRC_DIR / filename).read_text(encoding="utf-8")
    lines = text.split("\n")
    parsed = split_document(lines)
    field_schema: dict = {}

    def rp(t: str) -> str:
        return replace_blanks_in_paragraph(t, doc_key, field_schema)

    parsed["notice"] = rp(parsed["notice"]) if parsed["notice"] else None
    parsed["internal_notice"] = parsed["internal_notice"] or None  # never rendered; keep verbatim
    # `cover` holds the party-identification recital (company names, entity
    # type/state, principal place of business) plus, for the client-facing
    # docs, the SCOPE and agreement-entry paragraphs -- all real contractual
    # text, not just title-page furniture. It was previously left out of
    # rendering entirely; now it's tokenized and folded into preamble so
    # nothing the signer needs to see (or fill in) is silently dropped.
    parsed["cover"] = [rp(p) for p in parsed["cover"]]
    parsed["preamble"] = parsed["cover"] + [rp(p) for p in parsed["preamble"]]
    for sec in parsed["sections"]:
        sec["paragraphs"] = [rp(p) for p in sec["paragraphs"]]
    parsed["field_schema"] = field_schema
    return parsed


DOCS = {
    "client_engagement": "eng_text.txt",
    "sba_engagement": "sba_text.txt",
    "referral_protection": "referral_text.txt",
    "platform_access": "platform_text.txt",
    "consulting_addendum": "fee_text.txt",
}


def py_repr(obj, indent=0) -> str:
    import json
    return json.dumps(obj, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    FIELD_NAME_CACHE.clear()
    results = {}
    for key, filename in DOCS.items():
        FIELD_NAME_CACHE.clear()
        results[key] = process(key, filename)
        n_fields = len(results[key]["field_schema"])
        n_secs = len(results[key]["sections"])
        print(f"{key}: {n_secs} sections, {n_fields} detected blanks")

    with OUT_FILE.open("w", encoding="utf-8") as f:
        f.write('"""Auto-generated by scripts/build_contract_templates.py from the\n')
        f.write("source .docx contract text. Do not hand-edit generated dict literals\n")
        f.write("directly here -- change contract_templates.py's post-processing instead,\n")
        f.write("or re-run the generator against a corrected source .txt.\n\"\"\"\n\n")
        f.write("from __future__ import annotations\n\n")
        f.write("CONTRACT_RAW_DATA: dict = ")
        f.write(py_repr(results))
        f.write("\n")
    print(f"Wrote {OUT_FILE}")
