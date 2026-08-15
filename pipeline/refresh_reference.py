#!/usr/bin/env python3
"""Refresh AIRAC-cycle reference data (CI-runnable, no Mac dependency).

Rebuilds the four derived reference CSVs from current-cycle sources:

  data/us_ils_levels.csv   FAA ILS Master report: per-runway minima by ops
                           level (CAT I visibility, SA CAT I / CAT II /
                           SA CAT II / CAT III RVR), lowest per airport in
                           meters — link scraped from the aeronav
                           procedures/reports page (dated .xls)
  data/us_lpv.csv          CIFP SBAS path points: airport idents with an LPV
                           line of minima (FAACIFP18 via fetch_nasr.py)
  data/egnos_lpv.csv       ESSP EGNOS LPV procedures, operational airports
                           only: LPV200 vs plain LPV — current-cycle xlsx
                           scraped from the LPV procedures map page
  cat_curated.csv          faa-nasr rows only (US ILS categories from NASR
                           ILS.txt, atlas airports); faa-c060 / aip rows
                           are preserved untouched

Run fetch_nasr.py first (NASR APT/ILS + FAACIFP18 in data/nasr/). Each file
is rewritten only when its content changes, so a no-change cycle produces no
git diff. Exit code 0 with per-file status lines; a source that can't be
fetched leaves its CSV untouched and marks the run failed (exit 1) so CI
surfaces it without committing a partial refresh.

FAA + ESSP WAFs reject default urllib agents; send a browser UA.
"""
import csv
import io
import re
import sys
import urllib.request
from pathlib import Path

import openpyxl
import xlrd

from build_chase import parse_apt, parse_lpv

HERE = Path(__file__).parent
DATA = HERE / "data"
NASR = DATA / "nasr"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

ILS_MASTER_PAGE = "https://www.faa.gov/air_traffic/flight_info/aeronav/procedures/reports/"
ILS_MASTER_RE = re.compile(r"https://aeronav\.faa\.gov/[^\"']*ILS_Master_\d+\.xls")
EGNOS_PAGE = "https://egnos.gsc-europa.eu/lpv-procedures-map"
EGNOS_RE = re.compile(r"egnos_procedures-(\d+)\.xlsx")
EGNOS_FILE = "https://egnos.gsc-europa.eu/sites/default/files/lpv_procedures_map/egnos_procedures-{}.xlsx"

FT_M = 0.3048
MI_M = 1609.344


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def write_if_changed(path: Path, header: list, rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)  # \r\n line terminator, matching the committed files
    w.writerow(header)
    w.writerows(rows)
    new = buf.getvalue().encode()
    if path.exists() and path.read_bytes() == new:
        return f"  {path.name}: unchanged ({len(rows)} rows)"
    path.write_bytes(new)
    return f"  {path.name}: UPDATED ({len(rows)} rows)"


def full_list_us_icaos() -> set:
    # the full fetch list, NOT the atlas index — Alaska/Hawaii carry NASR
    # categories even though they never made it into the METAR archive
    with open(HERE / "airports_full.csv") as f:
        return {r["icao"] for r in csv.DictReader(f) if r["country"] == "US"}


# ---- FAA ILS Master: per-airport minima floors by ops level ----------------

def refresh_ils_master() -> str:
    page = get(ILS_MASTER_PAGE).decode(errors="replace")
    m = ILS_MASTER_RE.search(page)
    if not m:
        raise RuntimeError("ILS Master link not found on the aeronav reports page")
    url = m.group(0)
    dest = NASR / Path(url).name
    if not dest.exists():
        dest.write_bytes(get(url))
    print(f"  ILS Master: {dest.name}")

    sh = xlrd.open_workbook(dest).sheets()[0]

    def num(r, c):
        v = sh.cell_value(r, c)
        return float(v) if isinstance(v, (int, float)) and v else None

    floors: dict[str, dict] = {}
    for r in range(2, sh.nrows):
        icao = str(sh.cell_value(r, 3)).strip()
        if len(icao) != 4:
            continue
        if "COPTER" in str(sh.cell_value(r, 6)).upper():
            continue  # helicopter ILS minima (HAT ~100/RVR 1200) aren't fixed-wing floors
        f = floors.setdefault(icao, {})

        def take(key, meters):
            if meters is not None and (key not in f or meters < f[key]):
                f[key] = meters

        # CAT A..E straight-in visibility columns (value + unit); the lowest
        # across rows/runways is the airport's CAT I floor
        for vc, uc in ((26, 27), (29, 30), (32, 33), (35, 36), (38, 39)):
            v = num(r, vc)
            if v is None:
                continue
            uom = str(sh.cell_value(r, uc)).strip().upper()
            if uom == "FT":
                take("cat1_m", v * FT_M)
            elif uom == "MI":
                take("cat1_m", v * MI_M)
        sa1 = num(r, 40)
        if sa1:
            take("sacat1_m", sa1 * FT_M)
        # CAT II deck floor = whichever of CAT II / SA CAT II binds
        for c in (41, 42):
            v = num(r, c)
            if v:
                take("cat2_m", v * FT_M)
        c3 = num(r, 43)
        if c3:
            take("cat3_m", c3 * FT_M)

    rows = []
    for icao in sorted(floors):
        f = floors[icao]
        if "cat1_m" not in f:
            continue
        rows.append([icao] + ["" if f.get(k) is None else round(f[k])
                              for k in ("cat1_m", "sacat1_m", "cat2_m", "cat3_m")])
    return write_if_changed(DATA / "us_ils_levels.csv",
                            ["icao", "cat1_m", "sacat1_m", "cat2_m", "cat3_m"], rows)


# ---- CIFP: airports with an LPV line of minima -----------------------------

def refresh_us_lpv() -> str:
    lpv = parse_lpv(NASR / "FAACIFP18")
    return write_if_changed(DATA / "us_lpv.csv", ["icao"],
                            [[icao] for icao in sorted(lpv)])


# ---- ESSP EGNOS: operational LPV procedures at airports --------------------

def refresh_egnos() -> str:
    page = get(EGNOS_PAGE).decode(errors="replace")
    m = EGNOS_RE.search(page)
    if not m:
        raise RuntimeError("EGNOS cycle not found on the LPV procedures map page")
    cycle = m.group(1)
    dest = NASR / f"egnos_procedures-{cycle}.xlsx"
    if not dest.exists():
        dest.write_bytes(get(EGNOS_FILE.format(cycle)))
    print(f"  EGNOS: cycle {cycle}")

    sh = openpyxl.load_workbook(dest, read_only=True)["Sheet1"]
    cls: dict[str, str] = {}
    for row in list(sh.iter_rows(values_only=True))[1:]:
        icao, status, typ, proc = (str(row[0]).strip(), str(row[2]).strip().lower(),
                                   str(row[3]).strip().lower(), str(row[5]).strip())
        if status != "operational" or typ != "airport" or not proc.startswith("LPV"):
            continue
        c = "LPV200" if proc.startswith("LPV200") else "LPV"
        if cls.get(icao) != "LPV200":  # LPV200 wins for the airport
            cls[icao] = c
    return write_if_changed(DATA / "egnos_lpv.csv", ["icao", "lpv_class"],
                            [[i, cls[i]] for i in sorted(cls)])


# ---- NASR ILS categories -> cat_curated.csv faa-nasr rows ------------------

def refresh_cat_nasr() -> str:
    site_icao, _ = parse_apt(NASR / "APT.txt")
    us = full_list_us_icaos()

    # unlike build_chase's tier parse (glideslope ILS only), the category
    # badge counts ANY precision-family record with a published category —
    # NASR marks LOC/GS, LOC/DME, LDA rows CAT I too
    rank = {"CATI": 1, "CATII": 2, "CATIII": 3}
    best: dict[str, str] = {}
    for line in open(NASR / "ILS.txt", encoding="latin-1"):
        if line[0:4] != "ILS1":
            continue
        cat = line[172:181].strip()
        icao = site_icao.get(line[4:15].strip())
        if not cat or not icao or icao not in us:
            continue
        c = "CATIII" if cat.startswith("III") else "CATII" if cat.startswith("II") else "CATI"
        if rank[c] > rank.get(best.get(icao, ""), 0):
            best[icao] = c

    path = HERE / "cat_curated.csv"
    kept = [r for r in csv.reader(open(path)) if r and r[2] != "faa-nasr"]
    header, kept = kept[0], kept[1:]
    covered = {r[0] for r in kept}
    nasr = [[i, best[i], "faa-nasr"] for i in sorted(best) if i not in covered]
    return write_if_changed(path, header, kept + nasr)


def main() -> None:
    NASR.mkdir(parents=True, exist_ok=True)
    failed = False
    for name, fn in (("ILS Master", refresh_ils_master), ("CIFP LPV", refresh_us_lpv),
                     ("EGNOS", refresh_egnos), ("NASR categories", refresh_cat_nasr)):
        try:
            print(fn())
        except Exception as e:  # keep the committed CSV; surface the failure
            print(f"  {name}: FAILED — {e}")
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
