#!/usr/bin/env python3
"""Fetch FAA NASR 28-day subscriber file + current CIFP via the APRA API.

Downloads into data/nasr/: the subscriber zip (APT.txt, ILS.txt, layouts)
and the CIFP zip (FAACIFP18). Idempotent — skips files already present.
Feeds build_chase.py (per-runway infrastructure for the CHASE board).

FAA WAF rejects curl/urllib default user agents; send a browser-ish UA.
"""
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
NASR_DIR = HERE / "data" / "nasr"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

APRA_NASR = "https://external-api.faa.gov/apra/nfdc/nasr/chart?edition=current"
APRA_CIFP = "https://external-api.faa.gov/apra/cifp/chart?edition=current"


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read()


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  {dest.name} already present, skipping")
        return
    print(f"  {url} -> {dest.name}")
    dest.write_bytes(get(url))


def main() -> None:
    NASR_DIR.mkdir(parents=True, exist_ok=True)

    nasr_meta = json.loads(get(APRA_NASR))
    nasr_url = nasr_meta["edition"][0]["product"]["url"]
    edition = nasr_meta["edition"][0]["editionDate"]
    print(f"NASR edition {edition}")
    nasr_zip = NASR_DIR / Path(nasr_url).name
    download(nasr_url, nasr_zip)
    with zipfile.ZipFile(nasr_zip) as z:
        for name in ("APT.txt", "ILS.txt", "Layout_Data/apt_rf.txt", "Layout_Data/ils_rf.txt"):
            if not (NASR_DIR / name).exists():
                z.extract(name, NASR_DIR)
                print(f"  extracted {name}")

    cifp_xml = get(APRA_CIFP).decode()
    m = re.search(r'url="([^"]+)"', cifp_xml)
    if not m:
        sys.exit("CIFP url not found in APRA response")
    cifp_zip = NASR_DIR / "cifp.zip"
    download(m.group(1), cifp_zip)
    with zipfile.ZipFile(cifp_zip) as z:
        if not (NASR_DIR / "FAACIFP18").exists():
            z.extract("FAACIFP18", NASR_DIR)
            print("  extracted FAACIFP18")

    print("done")


if __name__ == "__main__":
    main()
