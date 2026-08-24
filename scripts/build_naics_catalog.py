"""Build the checked-in NAICS/PBA catalogue from official source files.

Run only when refreshing the versioned catalogue. Runtime and migrations read
the generated CSV and never scrape external sites.
"""

from __future__ import annotations

import csv
import html
import json
import re
import tempfile
import urllib.request
from pathlib import Path

import openpyxl

CENSUS_URL = "https://www.census.gov/naics/2022NAICS/2022_NAICS_Structure.xlsx"
IRS_URL = "https://www.irs.gov/instructions/i1120"
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "data" / "naics_2022.csv"


def clean_label(value: object) -> str:
    label = html.unescape(str(value or "")).strip()
    return re.sub(r"T\s*$", "", label).strip()


def sector_code(code: str) -> str:
    prefix = code[:2]
    if prefix in {"31", "32", "33"}:
        return "31-33"
    if prefix in {"44", "45"}:
        return "44-45"
    if prefix in {"48", "49"}:
        return "48-49"
    return prefix


def download(url: str, suffix: str) -> Path:
    target = Path(tempfile.gettempdir()) / f"qc_catalog_source{suffix}"
    request = urllib.request.Request(url, headers={"User-Agent": "QualifiedCommercial taxonomy importer/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        target.write_bytes(response.read())
    return target


def main() -> None:
    workbook = openpyxl.load_workbook(download(CENSUS_URL, ".xlsx"), read_only=True, data_only=True)
    sheet = workbook.active
    rows: dict[tuple[int, str], dict[str, object]] = {}
    for source_row in sheet.iter_rows(min_row=4, values_only=True):
        raw_code, raw_label = source_row[1], source_row[2]
        if raw_code is None or raw_label is None:
            continue
        code = str(raw_code).strip()
        if code.endswith(".0"):
            code = code[:-2]
        if len(code) not in {2, 3, 6} and "-" not in code:
            continue
        level = 2 if len(code) == 2 or "-" in code else len(code)
        if level not in {2, 3, 6}:
            continue
        parent = "" if level == 2 else sector_code(code) if level == 3 else code[:3]
        rows[(level, code)] = {
            "level": level,
            "code": code,
            "label": clean_label(raw_label),
            "parent_code": parent,
            "source": "census_2022",
            "aliases": [],
        }

    irs_html = download(IRS_URL, ".html").read_text(encoding="utf-8", errors="ignore")
    for code, label_html in re.findall(
        r'<span class="code">\s*(\d{6})\s*-\s*</span>\s*<span class="activity">(.*?)</span>',
        irs_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        label = clean_label(re.sub(r"<[^>]+>", "", label_html))
        key = (6, code)
        if key in rows:
            row = rows[key]
            if label.casefold() != str(row["label"]).casefold():
                row["aliases"].append(label)
            row["source"] = "census_2022,irs_pba_2025"
        else:
            rows[key] = {
                "level": 6,
                "code": code,
                "label": label,
                "parent_code": code[:3],
                "source": "irs_pba_2025",
                "aliases": [],
            }

    # The IRS catalogue includes 999000 (unable to classify), which has no
    # Census hierarchy row. Keep the exact IRS activity and create only the
    # missing parents required to navigate to it.
    for (level, _code), row in list(rows.items()):
        parent_code = str(row["parent_code"])
        if level != 6 or (3, parent_code) in rows:
            continue
        sector = sector_code(parent_code)
        rows.setdefault(
            (2, sector),
            {
                "level": 2,
                "code": sector,
                "label": "Unclassified business activity",
                "parent_code": "",
                "source": "irs_pba_2025",
                "aliases": [],
            },
        )
        rows[(3, parent_code)] = {
            "level": 3,
            "code": parent_code,
            "label": "Unclassified establishments",
            "parent_code": sector,
            "source": "irs_pba_2025",
            "aliases": [],
        }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["level", "code", "label", "parent_code", "source", "aliases"])
        writer.writeheader()
        for row in sorted(rows.values(), key=lambda item: (int(item["level"]), str(item["code"]))):
            writer.writerow({**row, "aliases": json.dumps(row["aliases"], ensure_ascii=True)})
    print(f"wrote {len(rows)} taxonomy rows to {OUTPUT}")


if __name__ == "__main__":
    main()
