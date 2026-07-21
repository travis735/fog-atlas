#!/usr/bin/env python3
"""The V3 forecast engine — one run = one shadow-mode forecast issuance.

1. Find the latest NBM cycle on the AWS mirror (NBH required, NBS if present)
2. Parse both, merge per airport: NBH hourly for lead 1-25, NBS 3-hourly beyond
3. Apply the fitted calibration (forecast/out/calibration.json) + climatology
   priors (forecast/out/climo.parquet) -> P(fog) at 4 thresholds per lead
4. Fetch current AWC obs once and record truth flags alongside
5. Write out/current.json (served to the site) and out/log/run_*.json
   (append-only shadow record: forecasts + obs, the scorecard's raw material)

Runs identically on a laptop and in the GitHub Action. No unbounded polling:
exactly one NBM probe walk, one AWC fetch per run.
"""
import gzip
import io
import json
import math
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from parse_nbm import parse_collective  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "out"
BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
THRESHOLDS = ["v10", "v05", "v025", "sub"]
RATE_COLS = {"v10": "r10", "v05": "r05", "v025": "r025", "sub": "rsub"}


def fetch(url: str, timeout: int = 180) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def latest_cycle() -> tuple[str, bytes, bytes | None]:
    now = datetime.now(timezone.utc)
    for back in range(1, 7):
        t = now - timedelta(hours=back)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        nbh = fetch(f"{BASE}/blend.{d}/{h}/text/blend_nbhtx.t{h}z")
        if nbh and len(nbh) > 1_000_000:
            nbs = fetch(f"{BASE}/blend.{d}/{h}/text/blend_nbstx.t{h}z")
            return f"{t.strftime('%Y-%m-%d')}T{h}:00:00Z", nbh, nbs
    raise RuntimeError("no NBM cycle found in the last 6 hours")


def load_climo():
    con = duckdb.connect()
    rows = con.execute(f"SELECT icao, mon, hr, n, r10, r05, r025, rsub FROM '{OUT / 'climo.parquet'}'").fetchall()
    means = con.execute(f"SELECT avg(r10), avg(r05), avg(r025), avg(rsub) FROM '{OUT / 'climo.parquet'}'").fetchone()
    climo = {(r[0], r[1], r[2]): r for r in rows}
    return climo, dict(zip(THRESHOLDS, means))


def clim_logit(climo, means, icao, mon, hr, thr):
    row = climo.get((icao, mon, hr))
    idx = {"v10": 4, "v05": 5, "v025": 6, "sub": 7}[thr]
    if row:
        n, rate = row[3], row[idx]
    else:
        n, rate = 0, means[thr]
    p = (rate * n + 2.0 * means[thr]) / (n + 2.0)
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def score(calib, feats: dict) -> float:
    z = calib["intercept"]
    for k, c in calib["coef"].items():
        z += c * feats[k]
    return 1.0 / (1.0 + math.exp(-z))


AWC = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"

def awc_truth(stations: set[str]) -> dict:
    raw = fetch(AWC, timeout=120)
    if not raw:
        return {}
    text = gzip.decompress(raw).decode("latin-1", "replace")
    out = {}
    for line in text.split("\n"):
        if not line.startswith('"'):
            continue
        close = line.find('",')
        if close < 0:
            continue
        cols = line[close + 2:].split(",")
        icao = cols[0]
        if icao not in stations or icao in out:
            continue
        try:
            vis = float(cols[9]) if cols[9] and "+" not in cols[9] else (10.0 if cols[9] else None)
        except ValueError:
            vis = None
        ceil = None
        for k in (21, 23, 25, 27):
            if k < len(cols) and cols[k] in ("BKN", "OVC", "OVX"):
                try:
                    b = float(cols[k + 1])
                    ceil = b if ceil is None else min(ceil, b)
                except ValueError:
                    pass
        if vis is None:
            continue
        out[icao] = {
            "obs": cols[1],
            "v10": int(vis < 1.0), "v05": int(vis < 0.5), "v025": int(vis < 0.25),
            "sub": int(vis < 0.5 or (ceil is not None and ceil < 200)),
        }
    return out


def main() -> None:
    stations = set(json.load(open(HERE / "stations.json")))
    calib = json.load(open(OUT / "calibration.json"))["thresholds"]
    climo, means = load_climo()

    cycle_iso, nbh, nbs = latest_cycle()
    cycle = datetime.fromisoformat(cycle_iso.replace("Z", "+00:00"))
    print(f"cycle {cycle_iso}  nbh {len(nbh) // 1024}KB  nbs {'yes' if nbs else 'no'}")

    tmp = HERE / "data"
    tmp.mkdir(exist_ok=True)
    (tmp / "_nbh_run.txt").write_bytes(nbh)
    rows = parse_collective(tmp / "_nbh_run.txt", stations)
    if nbs:
        (tmp / "_nbs_run.txt").write_bytes(nbs)
        rows += parse_collective(tmp / "_nbs_run.txt", stations)

    # merge: NBH wins lead 1-25, NBS fills 26-48
    per: dict[str, dict[int, dict]] = {}
    for r in rows:
        if r["fhr"] < 1 or r["fhr"] > 48 or r["vis_sm"] is None:
            continue
        keep = per.setdefault(r["icao"], {})
        cur = keep.get(r["fhr"])
        if cur is None or (r["product"] == "NBH") > (cur["product"] == "NBH"):
            keep[r["fhr"]] = r

    airports = {}
    for icao, byf in per.items():
        fhrs = sorted(byf)
        probs = []
        for f in fhrs:
            r = byf[f]
            valid = cycle + timedelta(hours=f)
            feats_base = {
                "log_vis": math.log1p(min(max(r["vis_sm"], 0), 10)),
                "cig_k": (r["ceil_ft"] if r["ceil_ft"] is not None else 25000) / 1000.0,
                "ifv": r["ifv"] if r["ifv"] is not None else 0.0,
                "ifc": r["ifc"] if r["ifc"] is not None else 0.0,
                "spread": min(max((r["tmp"] - r["dpt"]) if r["tmp"] is not None and r["dpt"] is not None else 10, -5), 40),
                "wsp": min(max(r["wsp"] if r["wsp"] is not None else 5, 0), 50),
                "lead": f,
            }
            row = []
            for thr in THRESHOLDS:
                feats = dict(feats_base,
                             clim_logit=clim_logit(climo, means, icao, valid.month, valid.hour, thr))
                row.append(round(100 * score(calib[thr], feats)))
            probs.append(row)
        airports[icao] = {"fhrs": fhrs, "p": probs,
                          "liv": [byf[f]["liv"] for f in fhrs],
                          "vis": [byf[f]["vis_sm"] for f in fhrs]}

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current = {
        "meta": {"cycle": cycle_iso, "generated": generated,
                 "public": False,  # shadow mode: pages must not show percentages
                 "thresholds": THRESHOLDS, "n_airports": len(airports)},
        "airports": airports,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "current.json").write_text(json.dumps(current, separators=(",", ":")))

    obs = awc_truth(stations)
    logdir = OUT / "log"
    logdir.mkdir(exist_ok=True)
    run_id = generated.replace(":", "").replace("-", "")[:13]
    # gzipped: hourly logs must fit KV's free tier until the R2 migration
    with gzip.open(logdir / f"run_{run_id}.json.gz", "wt") as f:
        json.dump({"cycle": cycle_iso, "generated": generated, "obs": obs,
                   "forecasts": {k: {"fhrs": v["fhrs"], "p": v["p"]} for k, v in airports.items()}},
                  f, separators=(",", ":"))
    size = (OUT / "current.json").stat().st_size
    print(f"current.json: {len(airports)} airports, {size // 1024}KB; obs recorded: {len(obs)}")
    ks = airports.get("KSFO", {})
    if ks:
        print("KSFO first 6 leads p(v10):", [row[0] for row in ks["p"][:6]], "fhrs", ks["fhrs"][:6])


if __name__ == "__main__":
    main()
