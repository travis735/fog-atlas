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
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
NASR = HERE / "data" / "nasr"
OUT = HERE / "out" / "app"
APP_DATA = HERE.parent / "app" / "public" / "data"

MIN_RWY_LEN_FT = 2000  # drop helipads / ultralight strips outright

# NASR APT.txt is fixed-width and the FAA moves the columns: the 2026-10-29
# cycle widened the pavement-classification field from 11 to 16 characters
# (PCN -> PCR), shifting every runway-end field 5 columns right. Hard-coded
# offsets then read "HIG" (from "HIGH" edge lights) as the runway end and
# "P4L" (a PAPI code) as the RVR; the 2026-11-03 CI refresh committed and
# deployed that, and the CHASE board ran on garbage for three weeks. So the
# offsets come from Layout_Data/apt_rf.txt, which ships in the same zip and
# describes the file it sits next to; APT_DEFAULT (the 2026-10-29 layout) is
# only a fallback, and sanity_problems() below refuses to write nonsense.
APT_DEFAULT = {  # 0-based slices
    "icao": (1210, 1217), "site": (3, 14), "len": (23, 28),
    "base": (70, 73), "base_rvr": (238, 241), "base_als": (242, 250),
    "recip": (292, 295), "recip_rvr": (460, 463), "recip_als": (464, 472),
}
_RF_LINE = re.compile(r"^\s*[LR]\s+[A-Z]{1,2}\s+(\d+)\s+(\d+)\s+\S+\s+(.+?)\s*$")


def apt_layout(rf_path: Path) -> dict:
    """Field slices for APT.txt from the FAA's own record-layout file."""
    if not rf_path.exists():
        print(f"WARNING: {rf_path} missing — using the built-in 2026-10-29 APT layout")
        return dict(APT_DEFAULT)
    fields = []  # (start_1based, length, NAME) in file order
    for line in open(rf_path, encoding="latin-1"):
        m = _RF_LINE.match(line)
        if m:
            fields.append((int(m.group(2)), int(m.group(1)), m.group(3).upper()))

    def after(i0, prefix):
        for i in range(i0, len(fields)):
            if fields[i][2].startswith(prefix):
                return i
        raise KeyError(prefix)

    try:
        lay = {}
        sl = lambda i: (fields[i][0] - 1, fields[i][0] - 1 + fields[i][1])  # noqa: E731
        lay["icao"] = sl(after(0, "ICAO IDENTIFIER"))
        lay["site"] = sl(after(0, "LANDING FACILITY SITE NUMBER"))
        i_len = after(0, "PHYSICAL RUNWAY LENGTH")
        lay["len"] = sl(i_len)
        i_base = after(i_len, "BASE END IDENTIFIER")
        i_recip = after(i_base, "RECIPROCAL END IDENTIFIER")
        lay["base"] = sl(i_base)
        lay["base_rvr"] = sl(after(i_base, "RUNWAY VISUAL RANGE EQUIPMENT"))
        lay["base_als"] = sl(after(i_base, "APPROACH LIGHT SYSTEM"))
        lay["recip"] = sl(i_recip)
        lay["recip_rvr"] = sl(after(i_recip, "RUNWAY VISUAL RANGE EQUIPMENT"))
        lay["recip_als"] = sl(after(i_recip, "APPROACH LIGHT SYSTEM"))
        # a base-end field found beyond the reciprocal end means the layout
        # text changed shape, not just columns — do not trust it
        if not (lay["base"][0] < lay["base_rvr"][0] < lay["base_als"][0] < lay["recip"][0]
                < lay["recip_rvr"][0] < lay["recip_als"][0]):
            raise KeyError("field order")
    except KeyError as e:
        print(f"WARNING: could not read {e} from {rf_path.name} — using the built-in 2026-10-29 APT layout")
        return dict(APT_DEFAULT)
    if lay != APT_DEFAULT:
        moved = {k: (APT_DEFAULT[k], v) for k, v in lay.items() if v != APT_DEFAULT[k]}
        print(f"APT layout from {rf_path.name} differs from the built-in default: {moved}")
    return lay


RWY_ID_RE = re.compile(r"^(0?[1-9]|[12]\d|3[0-6])[LRCWSUGX]?$|^[NSEW]{1,3}$|^H\d*[A-Z]?$")


def sanity_problems(airports: dict) -> list[str]:
    """Reasons this build must not be written. Healthy (2026-08 cycle): 98% of
    ends carry a real runway identifier, 46% sit in the no-approach tier and
    29% have an ILS category. Mis-parsed (2026-10 cycle at the old offsets):
    4%, 97% and 1%."""
    ends = [e for v in airports.values() for e in v if not e.get("cur")]  # FAA-derived ends only
    if len(ends) < 2000:
        return [f"only {len(ends)} FAA runway ends (expected ~3,700)"]
    ident = sum(1 for e in ends if RWY_ID_RE.match(str(e["e"]))) / len(ends)
    tier400 = sum(1 for e in ends if e["tier"] == 400) / len(ends)
    ils = sum(1 for e in ends if e["ils"]) / len(ends)
    out = []
    if ident < 0.90:
        out.append(f"{ident:.0%} of runway ends have a plausible identifier (need >= 90%)")
    if tier400 > 0.70:
        out.append(f"{tier400:.0%} of runway ends are in the no-approach tier (healthy ~46%, limit 70%)")
    if ils < 0.15:
        out.append(f"{ils:.0%} of runway ends matched an ILS category (healthy ~29%, need >= 15%)")
    return out


def parse_apt(path: Path, lay: dict | None = None):
    lay = lay or APT_DEFAULT
    cut = lambda line, k: line[lay[k][0]:lay[k][1]]  # noqa: E731
    site_icao: dict[str, str] = {}
    rwys: dict[str, list] = {}
    for line in open(path, encoding="latin-1"):
        rec = line[0:3]
        if rec == "APT":
            icao = cut(line, "icao").strip()
            if icao:
                site_icao[cut(line, "site").strip()] = icao
        elif rec == "RWY":
            site = cut(line, "site").strip()
            try:
                length = int(cut(line, "len").strip() or 0)
            except ValueError:
                length = 0
            if length < MIN_RWY_LEN_FT:
                continue
            for eid, als, rvr in (
                (cut(line, "base"), cut(line, "base_als"), cut(line, "base_rvr")),
                (cut(line, "recip"), cut(line, "recip_als"), cut(line, "recip_rvr")),
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

    site_icao, rwys = parse_apt(NASR / "APT.txt", apt_layout(NASR / "Layout_Data" / "apt_rf.txt"))
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

    # refuse to write (and so to commit and deploy) an implausible build —
    # exiting non-zero fails the CI job before its commit step
    problems = sanity_problems(airports)
    if problems:
        for probe in ("KSFO", "KSTS", "KOSH"):
            print(probe, json.dumps(airports.get(probe, "MISSING"))[:400])
        sys.exit("build_chase: output failed its sanity checks, chase.json NOT written — "
                 + "; ".join(problems) + f". Source: {meta['nasr_file']}. The FAA has probably "
                 "changed a fixed-width layout again: compare Layout_Data/apt_rf.txt and ils_rf.txt "
                 "with the offsets in this file.")

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
