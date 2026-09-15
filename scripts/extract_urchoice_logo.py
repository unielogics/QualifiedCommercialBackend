"""Extract the approved UrChoice PNG from the legacy agreement source bundle.

The source artifact stores its image resources in a JSON manifest.  Keeping
this tiny provenance script beside the committed runtime asset makes the
origin repeatable without parsing that large artifact during a request.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "agreement_sources" / "artifact-633d324a-1788403760-21c1.html"
TARGET = ROOT / "app" / "assets" / "urchoice_logo.png"
RESOURCE_KEY = "299fbf5d-218f-4df5-8e08-5414a4e82ca8"


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    match = re.search(r'<script type="__bundler/manifest">\s*(\{.*?\})\s*</script>', source, re.S)
    if not match:
        raise RuntimeError("Agreement resource manifest was not found")
    manifest = json.loads(match.group(1))
    resource = manifest.get(RESOURCE_KEY)
    if not resource or resource.get("mime") != "image/png":
        raise RuntimeError("UrChoice PNG resource was not found")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(base64.b64decode(resource["data"]))
    print(f"wrote {TARGET} ({TARGET.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
