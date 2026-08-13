#!/usr/bin/env python3
"""Build deploy.json — per-airport expected chaseable hours for the next 14 days.

The DEPLOY planner's data. Three honesty tiers, labeled and separately logged:
  days 1-2   calibrated: sum of hourly P(sub-CAT-I) from the live 48 h forecast
  days 3-8   advisory: climatological hours/day x an NBE-ingredient factor
             (dew-point spread + wind from the extended blend; heuristic,
             UNFITTED — logged daily so it accrues its own verification)
  days 9-14  advisory: climatological hours/day x a CPC precip-outlook tilt
             (Above-normal moisture leans fog up, Below leans down; mild)
CPC is US-only; Canadian airports keep factor 1.0 there (climatology).

Inputs: forecast/out/current.json (engine, same CI run), NBE from the AWS
mirror, CPC shapefiles, climo.parquet, chase.json + airports.json.
Output: forecast/out/deploy.json (KV `deploy` + R2 deploylogs/ via CI).
"""
import gzip
import io
import json
import math
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from parse_nbm import parse_collective  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "out"
BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
CPC = "https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst"


def fetch(url, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def latest_nbe(stations):
    now = datetime.now(timezone.utc)
    for back in range(1, 26):
        t = now - timedelta(hours=back)
        if t.hour not in (1, 7, 13, 19):  # NBE runs 4x daily
            continue
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        try:
            raw = fetch(f"{BASE}/blend.{d}/{h}/text/blend_nbetx.t{h}z")
        except Exception:
            continue
        if len(raw) > 5_000_000:
            p = HERE / "data" / "_nbe_run.txt"
            p.write_bytes(raw)
            return t.replace(minute=0, second=0, microsecond=0), parse_collective(p, stations)
    raise RuntimeError("no NBE cycle found")


def point_in_shape(lon, lat, shape) -> bool:
    pts = shape.points
    parts = list(shape.parts) + [len(pts)]
    inside = False
    for i in range(len(parts) - 1):
        ring = pts[parts[i]:parts[i + 1]]
        for j in range(len(ring)):
            x1, y1 = ring[j - 1]
            x2, y2 = ring[j]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def cpc_factors(airports):
    """(icao) -> {(start,end): factor} from 6-10 and 8-14 day precip outlooks."""
    import shapefile
    out = {a: [] for a in airports}
    for prod in ("610prcp", "814prcp"):
        try:
            z = zipfile.ZipFile(io.BytesIO(fetch(f"{CPC}/{prod}_latest.zip", 60)))
            tmp = HERE / "data" / f"_cpc_{prod}"
            tmp.mkdir(parents=True, exist_ok=True)
            z.extractall(tmp)
            shp = next(tmp.glob("*.shp"))
            r = shapefile.Reader(str(shp))
            fields = [f[0] for f in r.fields[1:]]
            for sh, rec in zip(r.shapes(), r.records()):
                d = dict(zip(fields, rec))
                cat, prob = d.get("Cat"), float(d.get("Prob") or 33)
                if cat not in ("Above", "Below"):
                    continue
                lean = (prob - 33.0) / 67.0
                f = 1.0 + 0.40 * lean if cat == "Above" else 1.0 - 0.30 * lean
                start = d["Start_Date"] if isinstance(d["Start_Date"], date) else date.fromisoformat(str(d["Start_Date"]))
                end = d["End_Date"] if isinstance(d["End_Date"], date) else date.fromisoformat(str(d["End_Date"]))
                for icao, (lon, lat) in airports.items():
                    if point_in_shape(lon, lat, sh):
                        out[icao].append((start, end, f))
        except Exception as e:
            print(f"  CPC {prod} unavailable: {e}")
    return out


def spread_wind_factor(spread_f, wsp_kt):
    if spread_f is None:
        f = 1.0
    elif spread_f <= 3: f = 1.6
    elif spread_f <= 6: f = 1.25
    elif spread_f <= 10: f = 1.0
    elif spread_f <= 15: f = 0.75
    else: f = 0.5
    if wsp_kt is not None:
        if wsp_kt <= 5: f *= 1.2
        elif wsp_kt <= 10: f *= 1.0
        elif wsp_kt <= 15: f *= 0.8
        else: f *= 0.6
    return min(max(f, 0.35), 2.0)


def main() -> None:
    # committed copy (CI runners have no pipeline/out); identical content
    atlas = {a["icao"]: a for a in json.load(open(HERE.parent / "app" / "public" / "data" / "airports.json"))["airports"]}
    chase = json.load(open(HERE.parent / "app" / "public" / "data" / "chase.json"))["airports"]
    coords = {i: (atlas[i]["lon"], atlas[i]["lat"]) for i in chase if i in atlas}
    stations = set(coords)
    print(f"{len(coords)} chase airports")

    # climatological sub-CAT-I hours/day by month
    climo_day = {}
    for icao, mon, hrs in duckdb.connect().execute(
            f"SELECT icao, mon, sum(rsub) FROM '{OUT / 'climo.parquet'}' GROUP BY 1,2").fetchall():
        climo_day[(icao, mon)] = float(hrs)  # sum over 24 hourly rates = hours/day

    # tier 1: calibrated 48 h from the engine's current.json (same CI run)
    current = json.loads((OUT / "current.json").read_text())
    cyc = datetime.fromisoformat(current["meta"]["cycle"].replace("Z", "+00:00"))
    today = datetime.now(timezone.utc).date()
    cal_day = {}
    for icao, f in current["airports"].items():
        for fhr, p in zip(f["fhrs"], f["p"]):
            dt = cyc + timedelta(hours=fhr)
            dd = (dt.date() - today).days
            if 1 <= dd <= 2:
                step = 1 if fhr <= 25 else 3
                cal_day[(icao, dd)] = cal_day.get((icao, dd), 0.0) + step * p[3] / 100.0

    # tier 2: NBE spread/wind factors per day
    nbe_t, nbe_rows = latest_nbe(stations)
    nbe_f = {}
    for r in nbe_rows:
        dt = nbe_t + timedelta(hours=r["fhr"])
        dd = (dt.date() - today).days
        if not (3 <= dd <= 8):
            continue
        spread = (r["tmp"] - r["dpt"]) if r["tmp"] is not None and r["dpt"] is not None else None
        f = spread_wind_factor(spread, r["wsp"])
        k = (r["icao"], dd)
        nbe_f.setdefault(k, []).append(f)
    print(f"NBE cycle {nbe_t.isoformat()} — day-factors for {len({k[0] for k in nbe_f})} airports")

    cpc = cpc_factors(coords)

    airports = {}
    for icao in coords:
        eh, tiers = [], []
        for dd in range(1, 15):
            dte = today + timedelta(days=dd)
            base = climo_day.get((icao, dte.month), 0.0)
            if (icao, dd) in cal_day:
                eh.append(round(cal_day[(icao, dd)], 2)); tiers.append("cal")
                continue
            f = 1.0
            tier = "climo"
            fs = nbe_f.get((icao, dd))
            if fs:
                f *= sum(fs) / len(fs); tier = "nbe"
            for (s, e, cf) in cpc.get(icao, []):
                if s <= dte <= e:
                    f *= math.sqrt(cf) if tier == "nbe" else cf
                    tier = tier if tier == "nbe" else "cpc"
            eh.append(round(base * min(max(f, 0.3), 2.2), 2)); tiers.append(tier)
        airports[icao] = {"eh": eh, "tiers": tiers}

    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "day1": (today + timedelta(days=1)).isoformat(),
        "nbe_cycle": nbe_t.strftime("%Y-%m-%dT%H:%MZ"),
        "tiers": {"cal": "calibrated model (verified tier)",
                  "nbe": "climatology x NBM-extended fog ingredients (advisory, unfitted)",
                  "cpc": "climatology x CPC moisture outlook (advisory)",
                  "climo": "climatology"},
    }
    out = {"meta": meta, "airports": airports}
    (OUT / "deploy.json").write_text(json.dumps(out, separators=(",", ":")))
    logdir = OUT / "deploylog"
    logdir.mkdir(exist_ok=True)
    with gzip.open(logdir / f"deploy_{today.isoformat()}.json.gz", "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    size = (OUT / "deploy.json").stat().st_size // 1024
    print(f"deploy.json: {len(airports)} airports, {size}KB")
    for probe in ("KVLD", "KMCN", "KSFO", "CYQI"):
        a = airports.get(probe)
        if a:
            print(f"  {probe}: 14d EH total {round(sum(a['eh']),1)}h  days: {[round(x,1) for x in a['eh']]}")


if __name__ == "__main__":
    main()
