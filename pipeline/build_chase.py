#!/usr/bin/env python3
"""Build chase.json — per-runway-end infrastructure for the CHASE board.

Sources (fetch_nasr.py):
  data/nasr/APT.txt    NASR landing facilities: site->ICAO, runway records
                       with per-end approach light system + RVR sensors
  data/nasr/ILS.txt    ILS1 records: per runway end, system type + category
  data/nasr/FAACIFP18  CIFP approach records: LPV lines of minima per runway

Join is restricted to airports already in the atlas (out/app/airports.json,
country US). Canadian airports arrive later via the curated tier.

Minima-height tier per runway end (a labeled proxy, NOT chart DAs):
  ILS CAT I -> 200 ft   CAT II -> 100   CAT III -> 50
  LPV       -> 250      anything else -> 400 ("no low approach")
The CHASE filter "go-around height <= X ft AGL" cuts on this tier: an end
whose best approach only reaches a ~400 ft MDA never gets the sensor low
enough to acquire the lights in a real fog layer.

Output: out/app/chase.json  (+ mirrored to ../app/public/data/chase.json)
  { meta: {...}, airports: { ICAO: [ {e,len,als,rvr,ils,lpv,tier}, ... ] } }
"""
import json
import re
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
NASR = HERE / "data" / "nasr"
OUT = HERE / "out" / "app"
APP_DATA = HERE.parent / "app" / "public" / "data"

MIN_RWY_LEN_FT = 2000  # drop helipads / ultralight strips outright

# NASR APT.txt fixed-width offsets (1-based starts from Layout_Data/apt_rf.txt)
# APT record: ICAO 1211 len 7; site 4 len 11
# RWY record: id 17 len 7, length 24 len 5,
#   base end: id 66 len 3, RVR 234 len 3, ALS 238 len 8
#   recip end: id 288 len 3, RVR 456 len 3, ALS 460 len 8


def parse_apt(path: Path):
    site_icao: dict[str, str] = {}
    rwys: dict[str, list] = {}
    for line in open(path, encoding="latin-1"):
        rec = line[0:3]
        if rec == "APT":
            icao = line[1210:1217].strip()
            if icao:
                site_icao[line[3:14].strip()] = icao
        elif rec == "RWY":
            site = line[3:14].strip()
            try:
                length = int(line[23:28].strip() or 0)
            except ValueError:
                length = 0
            if length < MIN_RWY_LEN_FT:
                continue
            for eid, als, rvr in (
                (line[65:68], line[237:245], line[233:236]),
                (line[287:290], line[459:467], line[455:458]),
            ):
                eid = eid.strip()
                if not eid:
                    continue
                rvr = rvr.strip()
                rwys.setdefault(site, []).append({
                    "e": eid,
                    "len": length,
                    "als": als.strip() or None,
                    "rvr": rvr if rvr and rvr != "N" else None,
                })
    return site_icao, rwys


def parse_ils(path: Path):
    """(site, end) -> category string, precision systems only (glideslope)."""
    cats: dict[tuple, str] = {}
    for line in open(path, encoding="latin-1"):
        if line[0:4] != "ILS1":
            continue
        systype = line[18:28].strip()
        if not systype.startswith("ILS"):  # LOCALIZER/LDA/SDF: no GS, high minima
            continue
        cat = line[172:181].strip()
        if cat:
            cats[(line[4:15].strip(), line[15:18].strip())] = cat
    return cats


def parse_lpv(path: Path):
    """ICAO -> set of runway ends with an LPV line of minima (CIFP)."""
    lpv: dict[str, set] = {}
    rwy_re = re.compile(r"^R(\d{2}[LRC]?)")
    for line in open(path, encoding="latin-1"):
        if not line.startswith("SUSAP ") or "LPV" not in line:
            continue
        icao = line[6:10].strip()
        m = rwy_re.match(line[13:19].strip())
        if m:
            lpv.setdefault(icao, set()).add(m.group(1).lstrip("0"))
    return lpv


def tier_for(ils_cat, has_lpv: bool) -> int:
    if ils_cat:
        if ils_cat.startswith("III"):
            return 50
        if ils_cat.startswith("II"):
            return 100
        return 200
    if has_lpv:
        return 250
    return 400


def norm_end(e: str) -> str:
    return e.lstrip("0")


def main() -> None:
    src = OUT / "airports.json"
    if not src.exists():  # CI runners have no pipeline/out — use the committed copy
        src = APP_DATA / "airports.json"
    atlas = json.load(open(src))["airports"]
    us_icaos = {a["icao"] for a in atlas if a["country"] == "US"}

    site_icao, rwys = parse_apt(NASR / "APT.txt")
    ils = parse_ils(NASR / "ILS.txt")
    lpv = parse_lpv(NASR / "FAACIFP18")

    airports: dict[str, list] = {}
    for site, ends in rwys.items():
        icao = site_icao.get(site)
        if not icao or icao not in us_icaos:
            continue
        out_ends = []
        for e in ends:
            key = norm_end(e["e"])
            cat = ils.get((site, e["e"])) or ils.get((site, key))
            has_lpv = key in lpv.get(icao, ())
            out_ends.append({
                **e,
                "ils": cat,
                "lpv": 1 if has_lpv else 0,
                "tier": tier_for(cat, has_lpv),
            })
        if out_ends:
            airports[icao] = out_ends

    # Canadian curated tier (agent-researched AIP Canada/CFS; owner-audited).
    # Only high/med confidence rows apply; low-confidence ends are omitted —
    # better absent than confidently wrong on a chase board.
    ca_csv = HERE / "data" / "ca_chase_curated.csv"
    n_ca = 0
    if ca_csv.exists():
        import csv as _csv
        ca_airports: dict[str, list] = {}
        for r in _csv.DictReader(open(ca_csv)):
            if r["conf"] not in ("high", "med"):
                continue
            cat = r["ils"] or None
            has_lpv = r["lpv"] == "1"
            ca_airports.setdefault(r["icao"], []).append({
                "e": r["end"],
                "len": int(r["len"]),
                "als": r["als"] or None,
                "rvr": "Y" if r["rvr"] == "1" else None,
                "ils": cat,
                "lpv": 1 if has_lpv else 0,
                "tier": tier_for(cat, has_lpv),
                "cur": 1,
            })
        airports.update(ca_airports)
        n_ca = len(ca_airports)

    meta = {
        "source": "FAA NASR + CIFP via APRA; Canada curated from AIP Canada/CFS",
        "nasr_file": next((p.name for p in sorted(NASR.glob("*.zip")) if p.name != "cifp.zip"), "unknown"),
        "airports": len(airports),
        "us": len(airports) - n_ca,
        "ca": n_ca,
    }
    out = {"meta": meta, "airports": airports}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "chase.json").write_text(json.dumps(out, separators=(",", ":")))
    APP_DATA.mkdir(parents=True, exist_ok=True)
    (APP_DATA / "chase.json").write_text(json.dumps(out, separators=(",", ":")))

    # ---- validation summary ----
    n_ends = sum(len(v) for v in airports.values())
    als_dist = Counter(e["als"] for v in airports.values() for e in v if e["als"])
    tier_dist = Counter(e["tier"] for v in airports.values() for e in v)
    print(f"airports: {len(airports)} / {len(us_icaos)} US atlas airports; ends: {n_ends}")
    print("ALS distribution:", dict(als_dist.most_common(10)))
    print("tier distribution:", dict(sorted(tier_dist.items())))
    for probe in ("KSFO", "KSTS", "KOSH"):
        print(probe, json.dumps(airports.get(probe, "MISSING")))


if __name__ == "__main__":
    main()
