#!/usr/bin/env python3
"""Build chase.json — per-runway-end infrastructure for the CHASE board.

Sources (fetch_nasr.py), the NASR CSV tables read by column name:
  data/nasr/CSV/APT_BASE.csv     landing facilities: site -> ICAO, name, position
  data/nasr/CSV/APT_RWY.csv      runway length
  data/nasr/CSV/APT_RWY_END.csv  per runway end: approach light system + RVR sensors
  data/nasr/CSV/ILS_BASE.csv     per runway end: ILS system type + category
  data/nasr/CSV/APT_RMK.csv      airport remarks: the FAA's LED approach-light sentence
  data/nasr/FAACIFP18            CIFP approach records: LPV lines of minima per runway
  data/led_ir_curated.csv        LED approach lights the FAA lists WITH infrared
                                 emitters (curated by hand, source cited per row)
The fixed-width APT.txt / ILS.txt this build read until 2026-11 are only a
fallback now (see "legacy fixed-width reader" below).

Join is restricted to airports already in the atlas (out/app/airports.json,
country US). Canadian airports arrive later via the curated tier.

LED approach lights: an infrared EFVS may not see them, and the only FAA record
of where they are is one sentence in the airport remarks ("PILOTS CONDUCTING
EFVS OPS; BE AWARE LED ALS IN USE RWY nn."). It is parsed into "led": 1 on the
matching runway ends and into a top-level "led" block listing EVERY such
airport, on the board or not. FAA-owned systems only; charting lags the
installation by two months or more; a runway without the remark is "not
flagged by the FAA", not "known incandescent". Canada has no source at all:
curated Canadian ends carry no "led" key, which means unknown.

Minima-height tier per runway end (a labeled proxy, NOT chart DAs):
  ILS CAT I -> 200 ft   CAT II -> 100   CAT III -> 50
  LPV       -> 250      anything else -> 400 ("no low approach")
The CHASE filter "go-around height <= X ft AGL" cuts on this tier: an end
whose best approach only reaches a ~400 ft MDA never gets the sensor low
enough to acquire the lights in a real fog layer.

Output: out/app/chase.json  (+ mirrored to ../app/public/data/chase.json)
  { meta: {...},
    airports: { ICAO: [ {e,len,als,rvr,ils,lpv,tier[,led[,led_ir]][,cur]}, ... ] },
    led: { cycle, n_airports, n_ends, ..., airports: [ {id,name,st,lat,lon,atlas,
           board,ends:[{e,als,board[,led_ir,ir_asof]}]}, ... ], review: [...] } }
plus out/led_changes.txt: one line per LED change against the committed
chase.json, for the monthly commit message.

  python pipeline/build_chase.py
  python pipeline/build_chase.py --nasr DIR --out DIR   # another cycle, written
                                 only into DIR (regenerate-and-diff)
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
NASR = HERE / "data" / "nasr"
OUT = HERE / "out" / "app"
APP_DATA = HERE.parent / "app" / "public" / "data"
LED_IR_CSV = HERE / "data" / "led_ir_curated.csv"

MIN_RWY_LEN_FT = 2000  # drop helipads / ultralight strips outright


class SourceError(Exception):
    """The FAA files are not in the shape this build reads. main() turns it
    into a non-zero exit before anything is written."""


# ---- legacy fixed-width reader (fallback only) ------------------------------
#
# Everything from here to "NASR CSV tables" reads APT.txt / ILS.txt by column
# offset. main() uses it only when fetch_nasr.py found no CSV tables in the
# subscriber zip. DELETE this section (and its tests) once the FAA has
# stopped shipping the .txt files — its README says they end 2027-01-21 —
# and pipeline/refresh_reference.py no longer imports parse_apt from here.
#
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
    # what the LED parse reads: identity and position of the facility (APT
    # record) and the remark text (RMK record). Unchanged Jul-Nov 2026.
    "lid": (27, 31), "eff": (31, 41), "state": (48, 50), "name": (133, 183),
    "lat_s": (538, 550), "lon_s": (565, 577), "rmk_text": (31, 1531),
}
APT_OPTIONAL = (  # key, the FAA's field name in apt_rf.txt
    ("lid", "LOCATION IDENTIFIER"), ("eff", "INFORMATION EFFECTIVE DATE"),
    ("state", "ASSOCIATED STATE POST OFFICE CODE"), ("name", "OFFICIAL FACILITY NAME"),
    ("lat_s", "AIRPORT REFERENCE POINT LATITUDE (SECONDS)"),
    ("lon_s", "AIRPORT REFERENCE POINT LONGITUDE (SECONDS)"), ("rmk_text", "REMARK TEXT"),
)
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
    # The LED fields are looked up one at a time, each falling back to its
    # built-in slice: a layout file that lacks one of them must not throw away
    # the runway slices found above (the wholesale fallback is what put "HIG"
    # on the board in 2026-11).
    for key, name in APT_OPTIONAL:
        try:
            lay[key] = sl(after(0, name))
        except KeyError:
            lay[key] = APT_DEFAULT[key]
    if lay != APT_DEFAULT:
        moved = {k: (APT_DEFAULT[k], v) for k, v in lay.items() if v != APT_DEFAULT[k]}
        print(f"APT layout from {rf_path.name} differs from the built-in default: {moved}")
    return lay


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
    by_system: dict[tuple, str] = {}     # (site, end, system type) -> category
    gone = set()
    for line in open(path, encoding="latin-1"):
        # ILS.txt keeps the ILS1 record of a system that no longer exists; only
        # the ILS2 (localizer) record says so. ILS_BASE.csv drops those rows.
        # Until 2026-11 this reader did not look, and chase.json credited nine
        # runway ends with a decommissioned ILS (KILN 22L as CAT II, tier 100).
        # Keyed by the system as well as the end: a decommissioned LOCALIZER
        # record on an end must not take a live ILS on the same end with it
        # (no such pair exists in 2026-10-29's 1,627 systems; the key costs nothing).
        if line[0:4] == "ILS2" and line[28:50].strip() == "DECOMMISSIONED":
            gone.add((line[4:15].strip(), line[15:18].strip(), line[18:28].strip()))
        if line[0:4] != "ILS1":
            continue
        systype = line[18:28].strip()
        if not systype.startswith("ILS"):  # LOC/GS, LOCALIZER, LDA, SDF: see ILS_FULL_CODES
            continue
        cat = line[172:181].strip()
        if cat:
            by_system[(line[4:15].strip(), line[15:18].strip(), systype)] = cat
    cats: dict[tuple, str] = {}
    for (site, end, systype), cat in by_system.items():
        if (site, end, systype) not in gone:
            cats[(site, end)] = cat
    return cats


def _arp_seconds(s: str):
    """'122192.9870N' (arc seconds + hemisphere) -> 33.9425; '' -> None."""
    try:
        v = float(s[:-1]) / 3600.0
    except (ValueError, IndexError):
        return None
    return -v if s[-1:] in ("S", "W") else v


def parse_apt_led(path: Path, lay: dict | None = None):
    """What the LED parse needs from APT.txt: every facility's identity and
    position, every runway end's approach lights (runways of any length) and
    the remarks that mention LED. A second pass over the file, so that
    parse_apt() keeps the two-value return refresh_reference.py unpacks."""
    lay = lay or APT_DEFAULT
    cut = lambda line, k: line[lay[k][0]:lay[k][1]].strip()  # noqa: E731
    fac: dict[str, dict] = {}
    ends: dict[str, dict] = {}
    remarks: list[tuple] = []
    eff: set[str] = set()
    n_rmk = 0
    for line in open(path, encoding="latin-1"):
        rec = line[0:3]
        if rec == "APT":
            fac[cut(line, "site")] = {
                "lid": cut(line, "lid"), "icao": cut(line, "icao"), "name": cut(line, "name"),
                "st": cut(line, "state"),
                "lat": _arp_seconds(cut(line, "lat_s")), "lon": _arp_seconds(cut(line, "lon_s")),
            }
            eff.add(cut(line, "eff"))
        elif rec == "RWY":
            for k_end, k_als in (("base", "base_als"), ("recip", "recip_als")):
                eid = cut(line, k_end)
                if eid:
                    ends.setdefault(cut(line, "site"), {})[eid] = cut(line, k_als) or None
        elif rec == "RMK":
            n_rmk += 1
            text = cut(line, "rmk_text")
            if _LED_HINT.search(text):
                remarks.append((cut(line, "site"), text))
    return fac, ends, remarks, n_rmk, eff


def read_nasr_txt(nasr_dir: Path) -> dict:
    """The fixed-width files in the shape read_nasr_csv() returns."""
    for name in ("APT.txt", "ILS.txt"):
        if not (nasr_dir / name).exists():
            raise SourceError(f"{nasr_dir / name} is missing — run fetch_nasr.py")
    lay = apt_layout(nasr_dir / "Layout_Data" / "apt_rf.txt")
    site_icao, rwys = parse_apt(nasr_dir / "APT.txt", lay)
    fac, ends, remarks, n_rmk, eff = parse_apt_led(nasr_dir / "APT.txt", lay)
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", next(iter(eff))) if len(eff) == 1 else None
    return {
        "reader": "txt", "cycle": f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None,
        "site_icao": site_icao, "rwys": rwys, "ils": parse_ils(nasr_dir / "ILS.txt"),
        "fac": fac, "ends": ends, "remarks": remarks, "n_remarks": n_rmk,
    }


# ---- NASR CSV tables (the source) -------------------------------------------
#
# The FAA is ending the fixed-width .txt subscriber files (README in the
# 2026-11-26 zip: none from 2027-01-21, and since the 2026-09-03 cycle new
# kinds of data go only into the CSV tables). The CSV tables ship in the same
# zip and carry a header row, so every column is read by the name the FAA
# gives it. A column this build needs that is not in the header stops the
# build and is named: a renamed column must never read as "no approach lights
# anywhere", which is how fixed offsets read the 2026-10-29 APT.txt.
CSV_NEEDS = {
    "APT_BASE.csv": ("EFF_DATE", "SITE_NO", "SITE_TYPE_CODE", "STATE_CODE", "ARPT_ID", "ARPT_NAME",
                     "LAT_DECIMAL", "LONG_DECIMAL", "ICAO_ID"),
    "APT_RWY.csv": ("EFF_DATE", "SITE_NO", "SITE_TYPE_CODE", "RWY_ID", "RWY_LEN"),
    "APT_RWY_END.csv": ("EFF_DATE", "SITE_NO", "SITE_TYPE_CODE", "RWY_ID", "RWY_END_ID",
                        "RWY_VISUAL_RANGE_EQUIP_CODE", "APCH_LGT_SYSTEM_CODE"),
    "APT_RMK.csv": ("EFF_DATE", "SITE_NO", "SITE_TYPE_CODE", "REMARK"),
    "ILS_BASE.csv": ("EFF_DATE", "SITE_NO", "SITE_TYPE_CODE", "RWY_END_ID", "SYSTEM_TYPE_CODE", "CATEGORY"),
}
# ILS_BASE.csv SYSTEM_TYPE_CODE: LS = "ILS", LD = "ILS/DME" in ILS.txt (the two
# agree row for row on the 2026-10-29 cycle, 1,288 systems). Only these two are
# credited as an ILS on the board — the same cut parse_ils() makes with
# systype.startswith("ILS"), carried over from the first version of this file.
# LOC/DME (LE), LDA (LA, DD), LOCALIZER (LC) and SDF (SF) have no glide slope.
# LOC/GS (LG) is NOT like them, and this comment said it was until a review
# (2026-11-25) read the FAA's own files: ils_rf.txt defines LG as "LOCALIZER/
# GLIDE SLOPE", all 88 LG systems have an ILS_GS.csv row (87 category I, one
# II), CIFP publishes ILS approaches for 24 of them (KFLG I21, KGCN I03), and
# the FAA ILS Master lists CAT I minima for KFLG, KGCN and KFHU. Leaving LG out
# shows 69 board ends as "no ILS" (49 at Air Force fields) and keeps 13 airports
# off the default board (KBAB, KBKF, KCOF, KCVS, KEGI, KHRT, KLSV, KLUF, KMCF,
# KMGE, KNBG, KNFW, KSKA). Whether to credit them is the owner's decision;
# adding "LG" here and in parse_ils() changes exactly those 69 ends.
# test_nasr_csv.py pins today's cut so that it cannot change by accident.
ILS_FULL_CODES = ("LS", "LD")


def _faa_lists(csv_dir: Path, table: str, column: str):
    """Does the FAA's own column list for this cycle still name the column?
    (<APT|ILS>_CSV_DATA_STRUCTURE.csv: CSV File, Column Name, ...). None when
    that file is not there to ask."""
    doc = csv_dir / f"{table.split('_')[0]}_CSV_DATA_STRUCTURE.csv"
    try:
        with open(doc, encoding="latin-1", newline="") as f:
            return any(r.get("CSV File") == table and r.get("Column Name") == column for r in csv.DictReader(f))
    except OSError:
        return None


def csv_table(csv_dir: Path, name: str) -> list[dict]:
    """The columns this build needs from one NASR CSV table, by header name."""
    path = csv_dir / name
    need = CSV_NEEDS[name]
    if not path.exists():
        raise SourceError(f"{path} is missing — run fetch_nasr.py")
    with open(path, encoding="latin-1", newline="") as f:
        rd = csv.DictReader(f)
        have = rd.fieldnames or []
        missing = [c for c in need if c not in have]
        if missing:
            said = []
            for c in missing:
                listed = _faa_lists(csv_dir, name[:-4], c)
                said.append(c if listed is None else
                            f"{c} (the FAA's column list for this cycle "
                            f"{'still names it' if listed else 'no longer names it'})")
            raise SourceError(f"{name} has no column {', '.join(said)}. The FAA has renamed or dropped it; "
                              f"this build reads columns by name and will not guess. Header: {', '.join(have)}")
        rows = [{c: (r[c] or "").strip() for c in need} for r in rd]
    if not rows:
        raise SourceError(f"{name} has a header and no rows")
    return rows


def _decimal(s: str):
    try:
        return float(s)
    except ValueError:
        return None


def read_nasr_csv(csv_dir: Path) -> dict:
    """One cycle of NASR in the shape main() joins:
      site_icao {site: ICAO}             rwys {site: [{e,len,als,rvr}]}  (>= 2,000 ft)
      ils {(site, end): category}        fac  {site: {lid,icao,name,st,lat,lon}}
      ends {site: {end: ALS or None}}    (runways of any length)
      remarks [(site, text)]             (only those that mention LED)
    'site' is SITE_NO + '*' + SITE_TYPE_CODE ("02187.*A"), the key APT.txt uses."""
    site = lambda r: f"{r['SITE_NO']}*{r['SITE_TYPE_CODE']}"  # noqa: E731
    dates: dict[str, set] = {}

    def table(name):
        rows = csv_table(csv_dir, name)
        dates[name] = {r["EFF_DATE"] for r in rows}
        return rows

    fac: dict[str, dict] = {}
    site_icao: dict[str, str] = {}
    for r in table("APT_BASE.csv"):
        fac[site(r)] = {"lid": r["ARPT_ID"], "icao": r["ICAO_ID"], "name": r["ARPT_NAME"], "st": r["STATE_CODE"],
                        "lat": _decimal(r["LAT_DECIMAL"]), "lon": _decimal(r["LONG_DECIMAL"])}
        if r["ICAO_ID"]:
            site_icao[site(r)] = r["ICAO_ID"]

    length: dict[tuple, int] = {}
    for r in table("APT_RWY.csv"):
        try:
            length[(site(r), r["RWY_ID"])] = int(r["RWY_LEN"] or 0)
        except ValueError:
            length[(site(r), r["RWY_ID"])] = 0

    rwys: dict[str, list] = {}
    ends: dict[str, dict] = {}
    for r in table("APT_RWY_END.csv"):
        s, eid = site(r), r["RWY_END_ID"]
        if not eid:
            continue
        als = r["APCH_LGT_SYSTEM_CODE"] or None
        ends.setdefault(s, {})[eid] = als
        n = length.get((s, r["RWY_ID"]), 0)
        if n < MIN_RWY_LEN_FT:
            continue
        rvr = r["RWY_VISUAL_RANGE_EQUIP_CODE"]
        rwys.setdefault(s, []).append({"e": eid, "len": n, "als": als, "rvr": rvr if rvr and rvr != "N" else None})

    ils: dict[tuple, str] = {}
    for r in table("ILS_BASE.csv"):
        if r["SYSTEM_TYPE_CODE"] in ILS_FULL_CODES and r["CATEGORY"]:
            ils[(site(r), r["RWY_END_ID"])] = r["CATEGORY"]

    remarks = []
    n_rmk = 0
    for r in table("APT_RMK.csv"):
        n_rmk += 1
        if _LED_HINT.search(r["REMARK"]):
            remarks.append((site(r), r["REMARK"]))

    # fetch_nasr.py refreshes the tables one by one on file times, so two
    # cycles can sit side by side under the same fixed names. Joining runway
    # ends of one cycle to the ILS table of another would look fine and be wrong.
    seen = set().union(*dates.values())
    if len(seen) != 1 or not re.fullmatch(r"\d{4}/\d{2}/\d{2}", next(iter(seen))):
        raise SourceError("the CSV tables do not share one EFF_DATE: "
                          + "; ".join(f"{k} {sorted(v)[:3]}" for k, v in dates.items())
                          + f". Delete {csv_dir} and run fetch_nasr.py again")
    return {
        "reader": "csv", "cycle": next(iter(seen)).replace("/", "-"),
        "site_icao": site_icao, "rwys": rwys, "ils": ils,
        "fac": fac, "ends": ends, "remarks": remarks, "n_remarks": n_rmk,
    }


# ---- shared ------------------------------------------------------------------

RWY_ID_RE = re.compile(r"^(0?[1-9]|[12]\d|3[0-6])[LRCWSUGX]?$|^[NSEW]{1,3}$|^H\d*[A-Z]?$")


# Every approach-light code in the 2026-07 to 2026-11 runway-end tables (39,835
# ends in 2026-10-29) and the letters an RVR equipment code is made of
# (T touchdown, M midpoint, R rollout). A value outside these is a column that
# moved, not a new kind of light: the FAA's own list of ALS types is this one.
ALS_CODES = frozenset("ALSF1 ALSF2 MALS MALSF MALSR SSALR SSALF SSALS SALS SALSF ODALS RLLS NSTD ALSAF".split())
RVR_LETTERS = frozenset("TMR")


def sanity_problems(airports: dict) -> list[str]:
    """Reasons this build must not be written. Healthy (2026-08 cycle): 98% of
    ends carry a real runway identifier, 46% sit in the no-approach tier and
    29% have an ILS category. Mis-parsed (2026-10 cycle at the old offsets):
    4%, 97% and 1%.

    The approach-light and RVR columns have a volume check of their own
    (2026-11-25 review). Without it a blank APCH_LGT_SYSTEM_CODE column, or one
    extra cell ahead of it, was written with exit 0: no ALS anywhere, or "N"
    and "Y" as light systems and "P4L" as RVR (the value that was live from
    2026-11-03 to 11-24), and the default CHASE board held 0 US airports while
    every check here and in the watchdog stayed green. The FAA's README of the
    2026-11-26 cycle announces fields being nulled in place from 2026-12-24,
    so "present but empty" is something it does. Healthy: 32.8% of ends carry
    an approach-light code, all 14 codes known; 26.9% carry an RVR code."""
    ends = [e for v in airports.values() for e in v if not e.get("cur")]  # FAA-derived ends only
    if len(ends) < 2000:
        return [f"only {len(ends)} FAA runway ends (expected ~3,700)"]
    ident = sum(1 for e in ends if RWY_ID_RE.match(str(e["e"]))) / len(ends)
    tier400 = sum(1 for e in ends if e["tier"] == 400) / len(ends)
    ils = sum(1 for e in ends if e["ils"]) / len(ends)
    with_als = [e for e in ends if e.get("als")]
    odd_als = sorted({str(e["als"]) for e in with_als if e["als"] not in ALS_CODES})
    n_odd_als = sum(1 for e in with_als if e["als"] not in ALS_CODES)
    with_rvr = [e for e in ends if e.get("rvr")]
    odd_rvr = sorted({str(e["rvr"]) for e in with_rvr if not set(str(e["rvr"])) <= RVR_LETTERS})
    out = []
    if ident < 0.90:
        out.append(f"{ident:.0%} of runway ends have a plausible identifier (need >= 90%)")
    if tier400 > 0.70:
        out.append(f"{tier400:.0%} of runway ends are in the no-approach tier (healthy ~46%, limit 70%)")
    if ils < 0.15:
        out.append(f"{ils:.0%} of runway ends matched an ILS category (healthy ~29%, need >= 15%)")
    if len(with_als) / len(ends) < 0.20:
        out.append(f"{len(with_als) / len(ends):.0%} of runway ends carry an approach-light system (healthy ~33%, "
                   f"need >= 20%) — the approach-light column is empty or has moved")
    if with_als and n_odd_als / len(with_als) > 0.05:
        out.append(f"{n_odd_als} of {len(with_als)} approach-light codes are not approach-light systems "
                   f"({', '.join(odd_als[:6])}) — the approach-light column has moved")
    if odd_rvr:
        out.append(f"RVR equipment codes that are not made of T, M and R ({', '.join(odd_rvr[:6])}) — the RVR "
                   f"column has moved")
    if len(with_rvr) / len(ends) < 0.10:
        out.append(f"{len(with_rvr) / len(ends):.0%} of runway ends carry RVR equipment (healthy ~27%, need >= 10%) "
                   f"— the RVR column is empty or has moved")
    return out


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


# ---- LED approach lights ------------------------------------------------------
#
# NASR has no lamp-technology field. The FAA's record of LED approach lights is
# a sentence typed into the airport's general remarks:
#   PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY 09, RWY 15R & RWY 33R.
# (AIM 5-4-22; 47 airports / 54 runway ends in the 2026-10-29 cycle.) Free
# text, so the runway list is parsed, and because free text can be reworded at
# any cycle, led_problems() refuses a build whose LED list has collapsed.
# The cheap pre-filter over ~89,000 remarks. It lets through every remark that
# says LED in any spelling, and every remark addressed to EFVS pilots whether
# or not it says LED: until 2026-11-25 it looked for the letters LED only, so
# nine sentences reworded to "LIGHT EMITTING DIODE ALS IN USE" left the map
# and the board with nothing on the review list (0 EFVS remarks lack the word
# LED in the 2026-10-29 cycle, so today this adds none).
_LED_HINT = re.compile(r"L\.?E\.?D|LIGHT[- ]EMITTING|EFVS|ENHANCED\s+FLIGHT\s+VISION", re.I)
_LED_DOTS = re.compile(r"\bL\.E\.D\b\.?")    # "L.E.D" (KPHT's runway lights) is LED too
_LED_LONG = re.compile(r"\bLIGHT[- ]EMITTING\s+DIODES?\b")
LED_RE = re.compile(r"\bLEDS?\b")
ALS_KW = re.compile(r"\b(?:ALS|ALSF[- ]?[12]?|MALSR?|MALSF|SSAL[RSF]?|SALSF?|ODALS|RAILS?"
                    r"|APCH\s+(?:LGTS?|LTS?|LTG|LIGHTS?|LGTG|LIGHTING)|APPROACH\s+(?:LGTS?|LTS?|LTG|LIGHTS?|LIGHTING))\b")
STD_RE = re.compile(r"\bLED\s+ALS\s+IN\s+USE\b")
EFVS_RE = re.compile(r"\bEFVS\b|\bENHANCED\s+FLIGHT\s+VISION\b")
# runway or airfield lighting in general, with nothing that makes it some
# other kind of light: "RWY 15C/33C LED LGT IN USE" (KSPS, both ends ALSF1)
LGT_KW = re.compile(r"\b(?:LGTS?|LGTG|LTG|LIGHTS?|LIGHTING)\b")
RWY_KW = re.compile(r"\b(?:RWYS?|RUNWAYS?|AIRFIELD|AFLD|ARPT|AIRPORT)\b")
OTHER_LGT_KW = re.compile(r"\b(?:PAPIS?|VASIS?|REILS?|EDGE|[MHL]IRL|CNTRLN|TDZ|OBST(?:N|RUCTION)?S?|BCN|BEACON"
                          r"|WINDSOCK|WIND|HELIPADS?|HELIPORT|SIGNS?)\b")
# any "RWY nn" the runway pattern below cannot read as a runway end ("RWY 63")
RWY_NUM_RE = re.compile(r"\bRWYS?\s*(\d{1,3}[A-Z]?)(?![0-9A-Z])")
# One runway end. The two-digit alternatives come FIRST: with 0?[1-9] first,
# "RWY 36" reads as end "03" — the research census did exactly that for forty
# airports before it was caught. The lookahead rejects three-digit pseudo
# runways such as Pope's assault strip "051/231".
_RID = r"(?:[12]\d|3[0-6]|0?[1-9])[LCR]?(?![0-9A-Z])"
# "RWY 13 & RWY 31", "RWY 09, RWY 15R & RWY 33R", "RWY 09 & 33R" (no second
# RWY), "RWYS 13 AND 31", "RWY 05/23" — all seen in 2026 remarks or FAA print.
RWY_LIST_RE = re.compile(r"\bRWYS?\s*(" + _RID + r"(?:\s*(?:,\s*(?:AND\b|&)?|&|/|-|\bAND\b)\s*(?:RWYS?\s*)?"
                         + _RID + r")*)")
RID_RE = re.compile(r"(?<![0-9A-Z])" + _RID)
PAIR_RE = re.compile(_RID + r"\s*[/-]\s*" + _RID)

LED_MIN_REMARKS = 40_000   # airport remark rows; 88,993 in 2026-10-29. Fewer = a truncated table
LED_MIN_AIRPORTS = 30      # 34 in 2026-07, 47 in 2026-10, and LED lamps are not taken out again
LED_SHRINK = 0.80          # refuse a list more than a fifth smaller than the committed one
LED_TOKEN_TOLERANCE = 0.05  # share of named runways allowed to match no runway end (FAA typos)
LED_MAX_REMOVED = 2        # runway ends that may leave the list in one cycle without a person saying so
LED_REMARKS_KEEP = 0.95    # a remark table under this share of the committed one's rows is a cut download
LED_MAX_NO_ALS = 2         # ends named by the FAA sentence with no ALS on record (none in 2026-07 to 2026-11)


def _pad_end(tok: str) -> str:
    """'7R' -> '07R': the form APT_RWY_END.csv, APT.txt and chase.json all use."""
    num = tok.rstrip("LCR")
    return num.zfill(2) + tok[len(num):]


def led_ends_from_remark(text: str):
    """What one airport remark says about LED approach lights.

    None when it says nothing about them. Otherwise {"kind", "ends", "pair"}:
      kind "std"    the FAA sentence; ends = the runway ends it names, zero-padded
      kind "other"  different words with LED and approach lights in one
                    sentence (Pope AAF: "RWY 05/23 EDGE AND APCH LGT EQUIPPED
                    WITH NSTD LED FIXTURES")
      kind "watch"  LED lights with an EFVS warning, or approach lights named
                    in another sentence: not a claim that the approach lights
                    are LED, but a person should read it ("why" says which)
    ends can be empty (the 2025 wording was "LED ALS IN USE." with no runway).
    pair is True when the runways were written as a pair ("05/23").
    bad lists runway numbers that are not runway ends at all ("RWY 63"): the
    runway pattern cannot read them, so until 2026-11-25 a typo made the
    airport "runway not named" without counting as a runway that matched
    nothing, and the gate's tolerance for unmatched runways never saw it."""
    t = _LED_LONG.sub("LED", _LED_DOTS.sub("LED", text.upper()))
    if not LED_RE.search(t):
        # addressed to EFVS pilots and no LED in any spelling: the FAA sentence
        # reworded past this parser. A person reads it; it never just vanishes.
        if EFVS_RE.search(t):
            return {"kind": "watch", "ends": [], "pair": False, "bad": [],
                    "why": "a remark for EFVS pilots that does not say LED"}
        return None
    std = list(STD_RE.finditer(t))
    if std:
        kind = "std"
        # the rest of THAT sentence only: "... IN USE RWY 01C & RWY 19C. RWY 12
        # PAPI OTS." names two LED ends, not three
        scopes = [re.split(r"[.;]", t[m.end():], maxsplit=1)[0] for m in std]
    else:
        kind = "other"
        scopes = [s for s in re.split(r"[.;](?:\s+|$)", t) if LED_RE.search(s) and ALS_KW.search(s)]
        if not scopes:
            # LED PAPIs, edge, obstruction and heliport lights are not this
            # build's business (30 of the 78 LED remarks in 2026-10-29) —
            # unless the remark warns EFVS pilots (McChord's LED runway
            # lights) or names approach lights in another sentence
            why = ("LED lights with an EFVS warning, approach lights not named" if EFVS_RE.search(t) else
                   "LED lights and approach lights named in different sentences" if ALS_KW.search(t) else None)
            return {"kind": "watch", "ends": [], "pair": False, "bad": [], "why": why} if why else None
    ends: list[str] = []
    bad: list[str] = []
    pair = False
    for s in scopes:
        for m in RWY_LIST_RE.finditer(s):
            pair = pair or bool(PAIR_RE.search(m.group(1)))
            for tok in RID_RE.findall(m.group(1)):
                if _pad_end(tok) not in ends:
                    ends.append(_pad_end(tok))
        for tok in RWY_NUM_RE.findall(s):
            if not re.fullmatch(_RID, tok) and tok not in bad:
                bad.append(tok)
    return {"kind": kind, "ends": ends, "pair": pair, "bad": bad}


def led_runway_lights(text: str) -> bool:
    """Does a remark say the runway or airfield LIGHTING is LED, without
    naming approach lights or some other kind of light? ("RWY 15C/33C LED LGT
    IN USE" at KSPS, both ends ALSF1; "LED LGTG ON AIRFIELD RWYS AND TWYS" at
    KSLI.) Not a claim about approach lights, so never listed; led_census puts
    it on the review list where the airport has approach lights on record."""
    t = _LED_LONG.sub("LED", _LED_DOTS.sub("LED", text.upper()))
    return any(LED_RE.search(s) and LGT_KW.search(s) and RWY_KW.search(s) and not OTHER_LGT_KW.search(s)
               for s in re.split(r"[.;](?:\s+|$)", t))


def led_census(nasr: dict):
    """Every airport whose remarks say its approach lights are LED.

    Returns (found, review, stats):
      found  {site: {"ends": [...], "nonstd": bool, "text": remark}} — ends as
             the runway table spells them, in the order the remark names them
      review [{"id", "why", "text"}] — remarks a person should read; nothing
             the parser could not place is dropped silently
      stats  counts for led_problems()"""
    found: dict[str, dict] = {}
    review: list[dict] = []
    # no_als: runway ends the FAA sentence names that have no approach-light
    # system on record. One or two are FAA bookkeeping; all of them is an
    # approach-light column that came back empty (led_problems).
    stats = {"remarks": nasr["n_remarks"], "mention": 0, "std": 0, "tokens": 0, "unmatched": 0, "no_als": 0}

    def flag(site, why, text):
        fac = nasr["fac"].get(site) or {}
        item = {"id": fac.get("icao") or fac.get("lid") or site, "why": why, "text": text[:240]}
        if item not in review:
            review.append(item)

    for site, text in nasr["remarks"]:
        if not site.endswith("*A"):
            continue  # airports only: heliport remarks name LED perimeter and "APCH/DEP PATH" lights
        r = led_ends_from_remark(text)
        site_ends = nasr["ends"].get(site, {})
        if r is None:
            # LED runway lighting at a field that has approach lights: not a
            # claim about them, but nobody had read KSPS's or KSLI's
            if any(site_ends.values()) and led_runway_lights(text):
                flag(site, "LED runway or airfield lights at an airport with approach lights, approach lights not named", text)
            continue
        if r["kind"] == "watch":
            flag(site, r["why"], text)
            continue
        stats["mention"] += 1
        stats["std"] += r["kind"] == "std"
        by_key = {norm_end(e): e for e in site_ends}
        ends = []
        for tok in r["ends"]:
            stats["tokens"] += 1
            e = by_key.get(norm_end(tok))
            if e is None:
                stats["unmatched"] += 1
                flag(site, f"RWY {tok} is not a runway end at this airport", text)
            else:
                ends.append(e)
        for tok in r["bad"]:     # "RWY 63": a runway named, and it is no runway anywhere
            stats["tokens"] += 1
            stats["unmatched"] += 1
            flag(site, f"RWY {tok} is not a runway number", text)
        if r["kind"] == "other" and any(site_ends[e] for e in ends):
            # Wording that is not the FAA sentence can name runways for other
            # lights too. Pope AAF's "RWY 05/23 EDGE AND APCH LGT" covers edge
            # lights at both ends, and only 23 has approach lights (ALSF1).
            ends = [e for e in ends if site_ends[e]]
        for e in ends:
            if not site_ends[e]:
                stats["no_als"] += r["kind"] == "std"
                flag(site, f"RWY {e} has no approach light system on record", text)
        if not r["ends"]:
            flag(site, "LED approach lights with no runway named", text)
        f = found.setdefault(site, {"ends": [], "nonstd": True, "text": text, "rows": 0})
        f["rows"] += 1
        if r["kind"] == "std":
            f["nonstd"] = False
        # KLAS carries the same sentence twice (A110-46 and A110-49), both
        # "RWY 26L", and has approach lights on 01L and 26R as well: the second
        # row may be meant for another end. Merged as before, and now said.
        if f["rows"] > 1 and ends and all(e in f["ends"] for e in ends) \
                and any(v and e not in f["ends"] for e, v in site_ends.items()):
            flag(site, "the same LED remark is filed twice and another runway end here has approach lights", text)
        for e in ends:
            if e not in f["ends"]:
                f["ends"].append(e)
    return found, review, stats


def read_led_ir(path: Path) -> dict:
    """(ICAO, end) -> the curated row saying those LED lamps have infrared
    emitters. The FAA remark does not tell the two kinds apart, and to an
    infrared sensor they are opposites."""
    need = ("icao", "end", "asof", "source", "url")
    if not path.exists():
        # a committed input: its absence is a broken checkout, and building
        # without it would quietly turn "LED + IR" into plain "LED"
        raise SourceError(f"{path} is missing (curated list of LED approach lights with infrared emitters)")
    rows = {}
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        missing = [c for c in need if c not in (rd.fieldnames or [])]
        if missing:
            raise SourceError(f"{path.name} has no column {', '.join(missing)}")
        for r in rd:
            icao, end = (r["icao"] or "").strip().upper(), (r["end"] or "").strip().upper()
            if not icao:
                continue
            if not RWY_ID_RE.match(end) or not (r["source"] or "").strip() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (r["asof"] or "").strip()):
                raise SourceError(f"{path.name}: row {icao} {end!r} needs a runway end, a source and an asof date (YYYY-MM-DD)")
            rows[(icao, _pad_end(end))] = {"asof": r["asof"].strip(), "source": r["source"].strip(), "url": (r["url"] or "").strip()}
    return rows


def led_block(nasr: dict, found: dict, review: list, airports: dict, atlas_icaos: set, ir_rows: dict) -> dict:
    """The top-level "led" object, and "led": 1 (plus "led_ir": 1) set on the
    matching runway ends of `airports` in place."""
    rows = []
    used_ir = set()
    for site, f in found.items():
        fac = nasr["fac"].get(site) or {}
        ident = fac.get("icao") or fac.get("lid") or site
        icao = nasr["site_icao"].get(site)
        board = {e["e"]: e for e in airports.get(icao, []) if not e.get("cur")} if icao else {}
        ends = []
        for e in f["ends"]:
            d = {"e": e, "als": nasr["ends"][site].get(e), "board": 1 if e in board else 0}
            ir = ir_rows.get((ident, e))
            if ir:
                d["led_ir"] = 1
                d["ir_asof"] = ir["asof"]
                used_ir.add((ident, e))
            if e in board:
                board[e]["led"] = 1
                if ir:
                    board[e]["led_ir"] = 1
            ends.append(d)
        row = {
            "id": ident, "name": fac.get("name") or None, "st": fac.get("st") or None,
            "lat": None if fac.get("lat") is None else round(fac["lat"], 4),
            "lon": None if fac.get("lon") is None else round(fac["lon"], 4),
            "atlas": 1 if ident in atlas_icaos else 0,
            "board": 1 if any(d["board"] for d in ends) else 0,
            "ends": ends,
        }
        if f["nonstd"]:   # not the FAA sentence: carry the words so the app can show them
            row["nonstd"] = 1
            row["rmk"] = f["text"][:240]
        if not ends:
            # The remark says the airport has LED approach lights and names no
            # runway this build could place (the 2025 wording named none; a
            # typo such as "RWY 63" does the same). Until 2026-11-25 such an
            # airport was ringed on the map while every row of it read "not
            # flagged" and "avoid LED ends" kept it. Every end there that has
            # approach lights now carries led_unk: LED, runway not known.
            row["rwy_unknown"] = 1
            row["rmk"] = f["text"][:240]
            for e in board.values():
                if e.get("als"):
                    e["led_unk"] = 1
            row["board"] = 1 if any(e.get("led_unk") for e in board.values()) else 0
        rows.append(row)
    rows.sort(key=lambda r: r["id"])
    all_ends = [d for r in rows for d in r["ends"]]
    # Curated rows the FAA remarks do not back this cycle (KMEM 18L: on the
    # FAA's list of 14 January 2025 as LED with infrared emitters, its remark
    # removed at the 2026-05-14 cycle). The lamps were not reported changed, so
    # the board end carries led_was: the app says so on the row and counts it
    # as LED under "avoid LED ends" — never under "only LED ends", and never on
    # the map, which shows what the FAA says now.
    by_icao = {(nasr["fac"].get(s) or {}).get("icao") or (nasr["fac"].get(s) or {}).get("lid"): s for s in nasr["ends"]}
    unused = []
    for (ident, e), v in sorted(ir_rows.items()):
        if (ident, e) in used_ir:
            continue
        hit = next((x for x in airports.get(ident, []) if x["e"] == e and not x.get("cur")), None)
        if hit is not None and not hit.get("led"):
            hit["led_was"] = 1
        unused.append({"id": ident, "e": e, "asof": v["asof"], "source": v["source"],
                       "als": (nasr["ends"].get(by_icao.get(ident)) or {}).get(e), "board": 1 if hit is not None else 0})
    return {
        "source": "FAA NASR airport remarks: PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY nn.",
        "cycle": nasr["cycle"],
        # how many airport remark rows this list was read out of: the next
        # build refuses a table much shorter than this one (led_problems)
        "n_remarks": nasr["n_remarks"],
        "n_airports": len(rows),
        "n_ends": len(all_ends),
        "n_airports_board": sum(r["board"] for r in rows),
        "n_ends_board": sum(d["board"] for d in all_ends),
        "n_airports_atlas": sum(r["atlas"] for r in rows),
        "n_led_ir": sum(1 for d in all_ends if d.get("led_ir")),
        "ir_sources": [{"source": s, "url": u, "asof": d} for s, u, d in
                       sorted({(v["source"], v["url"], v["asof"]) for k, v in ir_rows.items() if k in used_ir})],
        # curated infrared rows the FAA remarks do not back this cycle (KMEM
        # 18L: remark removed at the 2026-05-14 cycle). Not on the map; where
        # the end is on the board it carries led_was.
        "ir_unused": unused,
        "airports": rows,
        "review": sorted(review, key=lambda x: (x["id"], x["why"])),
    }


def _led_end_set(block: dict | None) -> set:
    return {(a["id"], d["e"]) for a in (block or {}).get("airports", []) for d in a.get("ends", [])}


def led_problems(led: dict, stats: dict, prev: dict | None, allow_removed: int = LED_MAX_REMOVED) -> list[str]:
    """Reasons the LED list must not be written. The source is free text the
    FAA can reword at any cycle; without this a reworded sentence would empty
    the list and the map would say "no LED approach lights anywhere".

    A review (2026-11-25) doctored the real 2026-10-29 tables and four edits
    were written with exit 0: nine sentences reworded without the letters LED
    (47 airports became 38, nothing on the review list), nine airports'
    remarks deleted, the remark table cut to 80,000 of 88,993 rows, and an
    approach-light column that came back blank (every listed end "has no
    approach lights"). A dropped end reads "not flagged" on the board and
    "avoid LED ends" shows it again, so each now stops the build:
      - more than allow_removed runway ends gone against the committed list
        (one left in 2026: KMEM 18L). `--allow-led-removals N` lets a person
        who has read the remarks through;
      - a remark table under 95% of the committed one's rows;
      - more than two ends named by the FAA sentence with no approach-light
        system on record."""
    out = []
    was_rows = (prev or {}).get("n_remarks")
    if stats["remarks"] < LED_MIN_REMARKS:
        out.append(f"only {stats['remarks']:,} airport remarks read (about 89,000 expected)")
    elif isinstance(was_rows, int) and stats["remarks"] < LED_REMARKS_KEEP * was_rows:
        out.append(f"only {stats['remarks']:,} airport remarks read; the committed chase.json was built from "
                   f"{was_rows:,} (a table under {LED_REMARKS_KEEP:.0%} of the last one is a truncated download)")
    if stats.get("no_als", 0) > LED_MAX_NO_ALS:
        out.append(f"{stats['no_als']} runway ends the FAA says have LED approach lights have no approach-light "
                   f"system on record (up to {LED_MAX_NO_ALS} tolerated) — the approach-light column is empty or has moved")
    gone = sorted(_led_end_set(prev) - _led_end_set(led))
    if len(gone) > allow_removed:
        out.append(f"{len(gone)} runway ends left the LED list against the committed chase.json "
                   f"({', '.join(f'{a} {e}' for a, e in gone[:12])}{' ...' if len(gone) > 12 else ''}); up to "
                   f"{allow_removed} is normal. Read their remarks in APT_RMK.csv: if the FAA really withdrew them, "
                   f"build again with --allow-led-removals {len(gone)}")
    if stats["mention"] and not stats["std"]:
        out.append(f"{stats['mention']} remarks mention LED approach lights but none uses the FAA sentence "
                   "\"LED ALS IN USE\" — the FAA has reworded it and the runway parser needs a look")
    allowed = max(1, int(LED_TOKEN_TOLERANCE * stats["tokens"]))
    if stats["unmatched"] > allowed:
        out.append(f"{stats['unmatched']} of the {stats['tokens']} runways named in LED remarks are not runway ends "
                   f"at their airport (up to {allowed} tolerated) — the runway wording has changed")
    if led["n_airports"] < LED_MIN_AIRPORTS:
        out.append(f"only {led['n_airports']} airports with LED approach lights (at least {LED_MIN_AIRPORTS} expected: "
                   "47 in 2026-10, and LED lamps are not taken out again)")
    # the older, coarser form of the same check, kept for a committed file whose
    # list cannot be compared end by end; a person who has named how many ends
    # may go (--allow-led-removals) has answered it
    if prev and not (allow_removed > LED_MAX_REMOVED and len(gone) <= allow_removed):
        for key, what in (("n_airports", "airports"), ("n_ends", "runway ends")):
            was = prev.get(key) or 0
            if led[key] < LED_SHRINK * was:
                out.append(f"LED approach lights fell from {was} to {led[key]} {what} against the committed chase.json "
                           f"(cycle {prev.get('cycle')}); one removal is normal, a fifth of the list is not")
    return out


def led_change_lines(led: dict, prev: dict | None) -> list[str]:
    """One line per LED change against the committed chase.json — the file is
    one minified line, so its git diff says nothing a person can read. These
    go to the log, the job summary and the monthly commit message."""
    def ends(b):
        return {(a["id"], d["e"]) for a in b.get("airports", []) for d in a["ends"]}

    def unknown(b):
        return {a["id"] for a in b.get("airports", []) if not a["ends"]}

    def ir(b):
        return {(a["id"], d["e"]) for a in b.get("airports", []) for d in a["ends"] if d.get("led_ir")}

    def rev(b):
        return {(x["id"], x["why"]) for x in b.get("review", [])}

    now = f"{led['n_airports']} airports, {led['n_ends']} runway ends (FAA NASR {led['cycle']})"
    if not prev:
        lines = [f"LED ALS: {now}; the committed chase.json has no LED list, so every one is new"]
        prev = {}
    else:
        lines = [f"LED ALS: {now}; was {prev.get('n_airports')} airports, {prev.get('n_ends')} runway ends "
                 f"(FAA NASR {prev.get('cycle')})"]
    n0 = len(lines)
    lines += [f"LED ALS: +{a} RWY {e}" for a, e in sorted(ends(led) - ends(prev))]
    lines += [f"LED ALS: -{a} RWY {e}" for a, e in sorted(ends(prev) - ends(led))]
    lines += [f"LED ALS: +{a} (runway not named)" for a in sorted(unknown(led) - unknown(prev))]
    lines += [f"LED ALS: -{a} (runway not named)" for a in sorted(unknown(prev) - unknown(led))]
    lines += [f"LED ALS: {a} RWY {e} now marked LED + IR (curated)" for a, e in sorted(ir(led) - ir(prev))]
    lines += [f"LED ALS: {a} RWY {e} no longer marked LED + IR" for a, e in sorted((ir(prev) - ir(led)) & ends(led))]
    lines += [f"LED ALS review: +{a} — {why}" for a, why in sorted(rev(led) - rev(prev))]
    lines += [f"LED ALS review: -{a} — {why}" for a, why in sorted(rev(prev) - rev(led))]
    if len(lines) == n0:
        lines.append("LED ALS: no change")
    return lines


def committed_chase(path: Path):
    """(chase.json to compare against, where it came from). The committed copy
    (git HEAD) when the file is tracked, so that a second local build still
    reports against what is live; else the file on disk; else None."""
    try:
        rel = path.resolve().relative_to(HERE.parent.resolve()).as_posix()
        raw = subprocess.run(["git", "-C", str(HERE.parent), "show", f"HEAD:{rel}"],
                             capture_output=True, check=True, timeout=60).stdout
        return json.loads(raw), "git HEAD"
    except (ValueError, OSError, subprocess.SubprocessError):
        pass  # not in the repo, no git, or not committed yet
    try:
        return json.loads(path.read_text()), "file on disk"
    except (OSError, ValueError):
        return None, "none"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Build chase.json from the FAA NASR tables.")
    ap.add_argument("--nasr", type=Path, default=NASR,
                    help="directory holding CSV/, FAACIFP18 (and APT.txt/ILS.txt); default pipeline/data/nasr")
    ap.add_argument("--out", type=Path, default=None,
                    help="write chase.json and led_changes.txt only into this directory (app/public/data untouched)")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="chase.json to compare the LED list against (default: the committed app/public/data/chase.json)")
    ap.add_argument("--reader", choices=("csv", "txt"), default=None,
                    help="force a reader (default: csv when the CSV tables are there)")
    ap.add_argument("--allow-led-removals", type=int, default=LED_MAX_REMOVED, metavar="N",
                    help=f"let up to N runway ends leave the LED list against the committed chase.json (default "
                         f"{LED_MAX_REMOVED}); for a person who has read the remarks and found the FAA withdrew them")
    args = ap.parse_args(argv)
    try:
        build(args)
    except SourceError as e:
        _refuse(f"build_chase: {e}. chase.json NOT written.")


def _refuse(message: str) -> None:
    """Exit non-zero: the monthly job stops before its commit step, and the
    reason shows on the run's summary page, not only deep in a step log."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::error::{message}")
    sys.exit(message)


def build(args) -> None:
    nasr_dir = args.nasr
    # the monthly job puts these lines in its commit message (reference.yml);
    # a refused build must not leave an earlier run's lines there
    changes_file = (args.out or OUT.parent) / "led_changes.txt"
    changes_file.unlink(missing_ok=True)

    src = OUT / "airports.json"
    if not src.exists():  # CI runners have no pipeline/out — use the committed copy
        src = APP_DATA / "airports.json"
    atlas = json.load(open(src))["airports"]
    us_icaos = {a["icao"] for a in atlas if a["country"] == "US"}

    present = [name for name in CSV_NEEDS if (nasr_dir / "CSV" / name).exists()]
    if args.reader == "txt" or (args.reader is None and not present):
        print(f"WARNING: no NASR CSV tables in {nasr_dir / 'CSV'} — reading the legacy fixed-width APT.txt / ILS.txt")
        nasr = read_nasr_txt(nasr_dir)
    elif len(present) < len(CSV_NEEDS):
        # some tables and not others is a half-finished fetch, not a zip
        # without CSV tables: falling back here would hide it
        raise SourceError(f"{nasr_dir / 'CSV'} holds only {', '.join(present)} of {', '.join(CSV_NEEDS)} — "
                          "run fetch_nasr.py again")
    else:
        nasr = read_nasr_csv(nasr_dir / "CSV")
    print(f"NASR cycle {nasr['cycle']} ({nasr['reader']} reader)")
    site_icao, rwys, ils = nasr["site_icao"], nasr["rwys"], nasr["ils"]
    lpv = parse_lpv(nasr_dir / "FAACIFP18")

    # chase.json has always listed airports in APT.txt's record order, and the
    # CSV tables come in another (site number). APT.txt's order is: state code,
    # then site number, facilities with no state last — that rule reproduces
    # its exact record order in the 2026-07 to 2026-11 cycles. Keeping it
    # means a new cycle's diff is the cycle's changes and nothing else, and
    # nothing downstream that walks the object sees a reshuffle.
    def apt_txt_order(site: str):
        state = (nasr["fac"].get(site) or {}).get("st") or ""
        try:
            number = float(site.split("*")[0])
        except ValueError:
            number = float("inf")
        return (state == "", state, number, site)

    airports: dict[str, list] = {}
    for site in sorted(rwys, key=apt_txt_order):
        ends = rwys[site]
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
        ca_airports: dict[str, list] = {}
        for r in csv.DictReader(open(ca_csv)):
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

    # LED approach lights: flags the US runway ends above in place. Curated
    # Canadian ends are never flagged — there is no Canadian source, so for
    # them a missing "led" key means unknown, not "no LED".
    found, review, led_stats = led_census(nasr)
    ir_rows = read_led_ir(LED_IR_CSV)
    led = led_block(nasr, found, review, airports, {a["icao"] for a in atlas}, ir_rows)

    # The app prints the date in nasr_file as the data's age ("FAA NASR
    # 2026-10-29"), so it must be the zip of the cycle that was read. A dev
    # machine holds several cycles' zips and the first one by name is the
    # oldest; a runner holds exactly one.
    zips = [p.name for p in sorted(nasr_dir.glob("*.zip")) if not p.name.lower().startswith("cifp")]
    mine = [z for z in zips if nasr["cycle"] and nasr["cycle"] in z]
    meta = {
        "source": "FAA NASR + CIFP via APRA; Canada curated from AIP Canada/CFS",
        "nasr_file": mine[0] if mine else f"cycle {nasr['cycle']} (zip not kept)" if nasr["cycle"]
        else zips[0] if zips else "unknown",
        "nasr_cycle": nasr["cycle"],
        "nasr_reader": nasr["reader"],
        "airports": len(airports),
        "us": len(airports) - n_ca,
        "ca": n_ca,
    }
    out = {"meta": meta, "airports": airports, "led": led}

    if args.baseline:
        prev, prev_from = json.loads(args.baseline.read_text()), str(args.baseline)
    else:
        prev, prev_from = committed_chase(APP_DATA / "chase.json")
    prev_led = (prev or {}).get("led")
    changes = led_change_lines(led, prev_led)

    # refuse to write (and so to commit and deploy) an implausible build —
    # exiting non-zero fails the CI job before its commit step
    problems = sanity_problems(airports) + led_problems(led, led_stats, prev_led,
                                                        getattr(args, "allow_led_removals", LED_MAX_REMOVED))
    if problems:
        for probe in ("KSFO", "KSTS", "KOSH"):
            print(probe, json.dumps(airports.get(probe, "MISSING"))[:400])
        print("LED remarks:", led_stats, "| compared with", prev_from)
        for line in changes[:60]:
            print(line)
        _refuse("build_chase: output failed its sanity checks, chase.json NOT written — "
                + "; ".join(problems) + f". Source: {meta['nasr_file']}, {nasr['reader']} reader. "
                + ("The FAA has probably changed a fixed-width layout again: compare Layout_Data/apt_rf.txt "
                   "and ils_rf.txt with the offsets in this file." if nasr["reader"] == "txt" else
                   "Look at the NASR CSV tables in data/nasr/CSV and at the LED remarks in APT_RMK.csv."))

    text = json.dumps(out, separators=(",", ":"))
    targets = [args.out] if args.out else [OUT, APP_DATA]
    for d in targets:
        d.mkdir(parents=True, exist_ok=True)
        (d / "chase.json").write_text(text)
    changes_file.write_text("\n".join(changes) + "\n")

    # ---- LED approach lights: what changed against the committed file ----
    print(f"LED approach lights (compared with chase.json from {prev_from}):")
    for line in changes:
        print(" ", line)
    for x in led["ir_unused"]:
        print(f"  curated infrared row {x['id']} RWY {x['e']} not applied: no LED remark for that end in this cycle")
    for x in led["review"]:
        print(f"  review {x['id']}: {x['why']} — {x['text'][:140]}")
    # A runway end that leaves the list reads "not flagged" on the board from
    # then on, and an LED airport whose runway the build could not place is
    # ringed on the map with no runway end tagged: both get an annotation on
    # the run page, not only a line in a commit body nobody opens.
    warn = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "WARNING: "
    unplaced = {r["id"] for r in led["airports"] if r.get("rwy_unknown")}
    for a, e in sorted(_led_end_set(prev_led) - _led_end_set(led)):
        if a in unplaced:
            continue    # said below: the airport still has its LED remark, only the runway is lost
        print(f"{warn}LED approach lights: {a} RWY {e} is no longer in the FAA remarks. The board now shows that "
              f"end as not flagged. Read the airport's remarks before trusting that.")
    for r in led["airports"]:
        if r.get("rwy_unknown"):
            print(f"{warn}LED approach lights: {r['id']} has an LED remark and no runway this build could place. "
                  f"The app treats every approach-light end there as LED.")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### LED approach lights\n" + "".join(f"- {line}\n" for line in changes))

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
