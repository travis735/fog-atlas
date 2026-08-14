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


# ---- V3.2 fitted day-scale tier (dayscale.json, ship-gated) ----
def load_dayscale():
    try:
        ds = json.load(open(OUT / "dayscale.json"))
        return ds if ds.get("ship") else None
    except FileNotFoundError:
        return None


def gefs_live(coords: dict) -> dict:
    """(icao, lead_day) -> {gefs_f16, gefs_f80, gefs_lv} from the latest cycle."""
    import numpy as np
    import eccodes as ec
    import concurrent.futures as cf
    LEADS = {"f072": 3, "f096": 4, "f120": 5, "f144": 6, "f168": 7, "f192": 8}
    MEMBERS = [f"gep{i:02d}" for i in range(1, 16)]
    now = datetime.now(timezone.utc)
    cycle = None
    for back in range(6, 30, 6):
        t = (now - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        t = t.replace(hour=(t.hour // 6) * 6)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        probe = fetch(f"https://noaa-gefs-pds.s3.amazonaws.com/gefs.{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f072.idx", 30)
        if probe:
            cycle = (d, h)
            break
    if not cycle:
        return {}
    d, h = cycle
    icaos = list(coords)
    lats = np.array([coords[i][1] for i in icaos])
    lons = np.array([coords[i][0] % 360 for i in icaos])

    def one(mem, lead):
        base = f"https://noaa-gefs-pds.s3.amazonaws.com/gefs.{d}/{h}/atmos/pgrb2bp5/{mem}.t{h}z.pgrb2b.0p50.{lead}"
        idx = fetch(base + ".idx", 60)
        if not idx:
            return None
        start = end = None
        lines = idx.decode().splitlines()
        for i, line in enumerate(lines):
            p = line.split(":")
            if len(p) > 4 and p[3] == "VIS" and p[4] == "surface":
                start = int(p[1])
                if i + 1 < len(lines):
                    end = int(lines[i + 1].split(":")[1]) - 1
                break
        if start is None:
            return None
        req = urllib.request.Request(base, headers={"Range": f"bytes={start}-{end if end else ''}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return (LEADS[lead], r.read())
        except Exception:
            return None

    grids: dict[int, list] = {}
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(lambda j: one(*j), [(m, l) for m in MEMBERS for l in LEADS]):
            if not res:
                continue
            lead, blob = res
            tmpf = HERE / "data" / "_gefs_live.grb2"
            tmpf.write_bytes(blob)
            try:
                with open(tmpf, "rb") as f:
                    gid = ec.codes_grib_new_from_file(f)
                lat1 = ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
                lon1 = ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
                dlat = ec.codes_get(gid, "jDirectionIncrementInDegrees")
                dlon = ec.codes_get(gid, "iDirectionIncrementInDegrees")
                ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
                vals = ec.codes_get_values(gid).reshape(nj, ni)
                rr = np.clip(np.round((lat1 - lats) / dlat).astype(int), 0, nj - 1)
                cc = np.clip(np.round((lons - lon1) / dlon).astype(int), 0, ni - 1)
                grids.setdefault(lead, []).append(vals[rr, cc])
                ec.codes_release(gid)
            except Exception:
                continue
    out = {}
    for lead, members in grids.items():
        m = np.stack(members)
        f16, f80 = (m < 1600).mean(axis=0), (m < 8000).mean(axis=0)
        lv = np.log1p(m / 1000.0).mean(axis=0)
        for j, icao in enumerate(icaos):
            out[(icao, lead)] = {"gefs_f16": float(f16[j]), "gefs_f80": float(f80[j]), "gefs_lv": float(lv[j])}
    print(f"GEFS live: cycle {d}/{h}z, {len(grids)} leads, {len(out)} airport-leads")
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

    # climatological sub-CAT-I hours/day by month, and the diurnal WINDOW
    # width (hours with rate >= half the peak hour) — how long fog typically
    # lasts once it's in, which bounds whether a distant base can reach it
    climo_day, climo_win = {}, {}
    con = duckdb.connect()
    for icao, mon, hrs in con.execute(
            f"SELECT icao, mon, sum(rsub) FROM '{OUT / 'climo.parquet'}' GROUP BY 1,2").fetchall():
        climo_day[(icao, mon)] = float(hrs)
    hourly = con.execute(
        f"SELECT icao, mon, hr, rsub FROM '{OUT / 'climo.parquet'}'").fetchall()
    by_im: dict[tuple, list] = {}
    for icao, mon, hr, r in hourly:
        by_im.setdefault((icao, mon), []).append(float(r or 0))
    for k, rates in by_im.items():
        peak = max(rates)
        climo_win[k] = sum(1 for r in rates if r >= peak * 0.5) if peak > 0.001 else 0

    # tier 1: calibrated 48 h from the engine's current.json (same CI run);
    # also derive P(any sub hour) per day from the hourly curve
    current = json.loads((OUT / "current.json").read_text())
    cyc = datetime.fromisoformat(current["meta"]["cycle"].replace("Z", "+00:00"))
    today = datetime.now(timezone.utc).date()
    cal_day, cal_noev, cal_win, cal_ws = {}, {}, {}, {}
    for icao, f in current["airports"].items():
        peak = max((r[3] for r in f["p"]), default=0)
        for fhr, p in zip(f["fhrs"], f["p"]):
            dt = cyc + timedelta(hours=fhr)
            dd = (dt.date() - today).days
            if 1 <= dd <= 2:
                step = 1 if fhr <= 25 else 3
                cal_day[(icao, dd)] = cal_day.get((icao, dd), 0.0) + step * p[3] / 100.0
                cal_noev[(icao, dd)] = cal_noev.get((icao, dd), 1.0) * (1.0 - p[3] / 100.0) ** step
                if p[3] >= max(15, peak * 0.4):
                    cal_win[(icao, dd)] = cal_win.get((icao, dd), 0) + step
                    cal_ws.setdefault((icao, dd), fhr)  # first in-window forecast hour

    # NBE per-day raw ingredients (fitted tier) + legacy factors (fallback)
    nbe_t, nbe_rows = latest_nbe(stations)
    nbe_f, nbe_raw = {}, {}
    for r in nbe_rows:
        dt = nbe_t + timedelta(hours=r["fhr"])
        dd = (dt.date() - today).days
        if not (3 <= dd <= 8):
            continue
        spread = (r["tmp"] - r["dpt"]) if r["tmp"] is not None and r["dpt"] is not None else None
        k = (r["icao"], dd)
        nbe_f.setdefault(k, []).append(spread_wind_factor(spread, r["wsp"]))
        if spread is not None:
            nbe_raw.setdefault(k, []).append((spread, r["wsp"] if r["wsp"] is not None else 5.0))
    print(f"NBE cycle {nbe_t.isoformat()} — day-factors for {len({k[0] for k in nbe_f})} airports")

    cpc = cpc_factors(coords)
    dayscale = load_dayscale()
    gefs = gefs_live({i: coords[i] for i in coords}) if dayscale else {}

    def sigmoid(z): return 1.0 / (1.0 + math.exp(-z))

    airports = {}
    for icao in coords:
        eh, tiers, pdays, wins, wss = [], [], [], [], []
        for dd in range(1, 15):
            dte = today + timedelta(days=dd)
            base = climo_day.get((icao, dte.month), 0.0)
            clm = dayscale["climo_day"].get(icao, {}).get(str(dte.month)) if dayscale else None
            p_clim = clm[0] if clm else None
            wins.append(cal_win.get((icao, dd), climo_win.get((icao, dte.month), 0)) if (icao, dd) in cal_day
                        else climo_win.get((icao, dte.month), 0))
            wss.append(cal_ws.get((icao, dd)))
            if (icao, dd) in cal_day:
                eh.append(round(cal_day[(icao, dd)], 2)); tiers.append("cal")
                pdays.append(round(1.0 - cal_noev[(icao, dd)], 3))
                continue
            # fitted day-scale tier: model P(any sub day) from live GEFS + NBE
            raw = nbe_raw.get((icao, dd))
            g = gefs.get((icao, dd))
            if dayscale and 3 <= dd <= 8 and raw and g and p_clim is not None:
                pc = min(max(p_clim, 1e-4), 1 - 1e-4)
                feats = {"clim_logit": math.log(pc / (1 - pc)),
                         "gefs_f16": g["gefs_f16"], "gefs_f80": g["gefs_f80"], "gefs_lv": g["gefs_lv"],
                         "nbe_spread": min(max(min(s for s, _ in raw), -5), 40),
                         "nbe_wsp": min(max(sum(w for _, w in raw) / len(raw), 0), 50),
                         "lead": dd}
                z = dayscale["intercept"] + sum(dayscale["coef"][k] * feats[k] for k in dayscale["features"])
                p_any = sigmoid(z)
                hrs_day = clm[1]
                ratio = min(max(p_any / pc, 0.3), 3.0)
                eh.append(round(hrs_day * ratio, 2)); tiers.append("fit"); pdays.append(round(p_any, 3))
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
            f = min(max(f, 0.3), 2.2)
            eh.append(round(base * f, 2)); tiers.append(tier)
            pdays.append(round(min(p_clim * f, 0.95), 3) if p_clim is not None else None)
        airports[icao] = {"eh": eh, "tiers": tiers, "p": pdays, "win": wins, "ws": wss}

    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cycle": current["meta"]["cycle"],  # anchor for per-day ws forecast-hours
        "day1": (today + timedelta(days=1)).isoformat(),
        "nbe_cycle": nbe_t.strftime("%Y-%m-%dT%H:%MZ"),
        "tiers": {"cal": "calibrated model (verified tier)",
                  "fit": "fitted day-scale model: GEFS ensemble vis + NBM-extended ingredients (holdout-verified)",
                  "nbe": "climatology x NBM-extended fog ingredients (advisory, unfitted)",
                  "cpc": "climatology x CPC moisture outlook (advisory)",
                  "climo": "climatology"},
        "dayscale_skill": (load_dayscale() or {}).get("test", {}).get("skill_pct"),
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
