#!/usr/bin/env python3
"""Fetch FAA NASR 28-day subscriber file + current CIFP via the APRA API.

Downloads into data/nasr/ the subscriber zip and the CIFP zip, and extracts
what the pipeline reads:

  CSV/APT_*.csv, CSV/ILS_*.csv   the NASR CSV tables, from the zip inside the
                                 zip (CSV_Data/<DD_Mon_YYYY>_CSV.zip) —
                                 build_chase.py's source
  APT.txt, ILS.txt, Layout_Data/ the legacy fixed-width files, for as long as
                                 the FAA ships them — refresh_reference.py
                                 still reads them, and build_chase.py falls
                                 back to them for a zip without CSV tables
  FAACIFP18                      CIFP approach records (LPV lines of minima)

Idempotent — skips downloads already present, and re-extracts a member only
when its zip is newer than the extract.

FAA WAF rejects curl/urllib default user agents; send a browser-ish UA.
"""
import csv
import io
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

# The FAA stops publishing the fixed-width .txt subscriber files: the README
# in the 2026-11-26 zip says "TXT NASR Subscriber files will no longer be
# available beginning January 21, 2027", and that since the 2026-09-03 cycle
# new kinds of data go only into the CSV tables. So the CSV tables are the
# source and the .txt members are extracted only while a zip still has them.
CSV_INNER_RE = re.compile(r"^CSV_Data/[^/]+_CSV\.zip$")
CSV_TABLES = ("APT_BASE.csv", "APT_RWY.csv", "APT_RWY_END.csv", "APT_RMK.csv",
              "ILS_BASE.csv", "ILS_GS.csv", "ILS_DME.csv", "ILS_MKR.csv", "ILS_RMK.csv")
# the FAA's own column lists for those tables: build_chase.py quotes them when
# a column it needs is missing. Documentation, so their absence is not fatal.
CSV_DOCS = ("APT_CSV_DATA_STRUCTURE.csv", "ILS_CSV_DATA_STRUCTURE.csv")
TXT_MEMBERS = ("APT.txt", "ILS.txt", "Layout_Data/apt_rf.txt", "Layout_Data/ils_rf.txt")


def get(url: str, accept: str | None = None) -> bytes:
    headers = {**UA, **({"Accept": accept} if accept else {})}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read()


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  {dest.name} already present, skipping")
        return
    print(f"  {url} -> {dest.name}")
    dest.write_bytes(get(url))


def _fresh(target: Path, zpath: Path) -> bool:
    # re-extract when the zip is newer than the extract — a new cycle's zip
    # (dated filename) must refresh the fixed-name members it contains
    return target.exists() and target.stat().st_mtime >= zpath.stat().st_mtime


def extract(zpath: Path, members: tuple) -> None:
    with zipfile.ZipFile(zpath) as z:
        for name in members:
            if _fresh(NASR_DIR / name, zpath):
                continue
            z.extract(name, NASR_DIR)
            print(f"  extracted {name}")


def _drop_stale(paths: list, why: str) -> None:
    # A fixed-name extract left over from an OLDER cycle would be read as the
    # current one: the file name carries no date. Remove it so the reader
    # fails on a missing file instead of quietly using last cycle's data.
    for p in paths:
        if p.exists():
            p.unlink()
            print(f"  removed {p.relative_to(NASR_DIR)} ({why})")


def extract_legacy_txt(zpath: Path) -> bool:
    """APT.txt / ILS.txt / layouts, when the zip still has them."""
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
    have = tuple(n for n in TXT_MEMBERS if n in names)
    gone = [n for n in TXT_MEMBERS if n not in names]
    if gone:
        print(f"  {zpath.name} has no {', '.join(gone)} — the FAA has stopped the legacy .txt files")
        _drop_stale([NASR_DIR / n for n in gone], "from an older cycle")
    extract(zpath, have)
    return "APT.txt" in have and "ILS.txt" in have


def extract_csv(zpath: Path) -> str | None:
    """The CSV tables out of the zip inside the zip, into data/nasr/CSV/.
    Returns the cycle date they carry (YYYY-MM-DD), or None when this zip has
    no CSV tables at all."""
    dest = NASR_DIR / "CSV"
    with zipfile.ZipFile(zpath) as z:
        inner = [n for n in z.namelist() if CSV_INNER_RE.match(n)]
        if not inner:
            print(f"  {zpath.name} has no CSV_Data/*_CSV.zip")
            _drop_stale([dest / n for n in CSV_TABLES + CSV_DOCS], "from another cycle")
            return None
        if len(inner) > 1:
            sys.exit(f"fetch_nasr: {zpath.name} holds {len(inner)} CSV zips ({', '.join(inner)}) — "
                     "which one is the cycle is not ours to guess")
        if not all(_fresh(dest / n, zpath) for n in CSV_TABLES):
            with zipfile.ZipFile(io.BytesIO(z.read(inner[0]))) as zz:
                names = set(zz.namelist())
                missing = [n for n in CSV_TABLES if n not in names]
                if missing:
                    sys.exit(f"fetch_nasr: {inner[0]} in {zpath.name} has no {', '.join(missing)} — "
                             "the FAA renamed or dropped a CSV table build_chase.py reads")
                dest.mkdir(parents=True, exist_ok=True)
                for name in CSV_TABLES + tuple(n for n in CSV_DOCS if n in names):
                    if _fresh(dest / name, zpath):
                        continue
                    zz.extract(name, dest)
                    print(f"  extracted CSV/{name} (from {inner[0]})")
    # Every table must carry ONE effective date: members are refreshed one by
    # one on file times, so a half-finished earlier run could leave two cycles
    # side by side under the same fixed names.
    dates = {}
    for name in CSV_TABLES:
        with open(dest / name, encoding="latin-1", newline="") as f:
            row = next(csv.DictReader(f), None)
        dates[name] = (row or {}).get("EFF_DATE", "")
    if len(set(dates.values())) != 1 or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", next(iter(dates.values()))):
        sys.exit(f"fetch_nasr: the CSV tables do not share one EFF_DATE: {dates} — "
                 f"delete {dest} and run again")
    return next(iter(dates.values())).replace("/", "-")


def main() -> None:
    NASR_DIR.mkdir(parents=True, exist_ok=True)

    # APRA serves XML unless JSON is requested explicitly (default changed 2026-08)
    nasr_meta = json.loads(get(APRA_NASR, accept="application/json"))
    nasr_url = nasr_meta["edition"][0]["product"]["url"]
    edition = nasr_meta["edition"][0]["editionDate"]
    print(f"NASR edition {edition}")
    nasr_zip = NASR_DIR / Path(nasr_url).name
    download(nasr_url, nasr_zip)
    cycle = extract_csv(nasr_zip)
    has_txt = extract_legacy_txt(nasr_zip)
    if cycle is None and not has_txt:
        sys.exit(f"fetch_nasr: {nasr_zip.name} has neither the CSV tables nor APT.txt/ILS.txt — "
                 "nothing for build_chase.py to read")
    if cycle is not None:
        # APRA says MM/DD/YYYY; the tables say YYYY/MM/DD. A zip whose tables
        # are for another cycle than the one APRA calls current is the wrong
        # file, however fresh it looks.
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", edition or "")
        want = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None
        if want and want != cycle:
            sys.exit(f"fetch_nasr: APRA calls {want} the current cycle but the CSV tables in "
                     f"{nasr_zip.name} are for {cycle}")
        print(f"  CSV tables: cycle {cycle}")

    cifp_xml = get(APRA_CIFP).decode()
    m = re.search(r'url="([^"]+)"', cifp_xml)
    if not m:
        sys.exit("CIFP url not found in APRA response")
    cifp_zip = NASR_DIR / Path(m.group(1)).name  # dated name — new cycle, new file
    download(m.group(1), cifp_zip)
    extract(cifp_zip, ("FAACIFP18",))

    print("done")


if __name__ == "__main__":
    main()
