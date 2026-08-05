"""Paragraph-content-only fidelity check (headings/ToC intentionally excluded
-- those are dropped by design, verified separately by inspection)."""
from __future__ import annotations

import html
import importlib.util
import re
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

HEADING_RE = re.compile(r"^ARTICLE \d|^SCHEDULE [A-Z0-9]|^EXHIBIT \d")
SIGNATURE_BLOCK_START = re.compile(
    r"^(ACKNOWLEDGMENT|IN WITNESS WHEREOF|CERTIFIED BY CLIENT|AUTHORIZING PARTY|"
    r"ACKNOWLEDGED BY (CLIENT|REFERRAL PARTNER)|USER$|GUARANTOR$|REPRESENTATIVE$)"
)


def normalize(s: str) -> str:
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def sub_placeholders(text: str, field_schema: dict) -> str:
    def repl(m: "re.Match") -> str:
        name = m.group(1)
        info = field_schema.get(name)
        raw = info.get("raw_token") if info else None
        return raw if raw else m.group(0)
    return re.sub(r"\$([A-Za-z0-9_]+)", repl, text)


for key, fn in DOCS.items():
    doc = DATA[key]
    src_lines = [normalize(l) for l in (SRC_DIR / fn).read_text(encoding="utf-8").split("\n") if normalize(l)]

    paras: list[str] = list(doc["cover"])
    if doc["notice"]:
        paras.append(doc["notice"])
    if doc["internal_notice"]:
        paras.append(doc["internal_notice"])
    paras.extend(doc["preamble"])
    for sec in doc["sections"]:
        paras.extend(sec["paragraphs"])

    recon_set = {normalize(sub_placeholders(p, doc["field_schema"])) for p in paras}

    src_content = [
        l for l in src_lines
        if l != "TABLE OF CONTENTS"
        and not l.startswith(("Recitals", "Article ", "Schedule ", "Exhibit "))
        and not HEADING_RE.match(l)
    ]
    missing = [l for l in src_content if l not in recon_set]
    print(f"{key}: content lines={len(src_content)} missing={len(missing)}")
    for l in missing[:25]:
        print("   -", l[:160])
