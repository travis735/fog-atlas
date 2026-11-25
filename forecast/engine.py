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
import csv
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
# The columns the truth record is built from, by header NAME (a subset of
# FEED_REQUIRED_COLUMNS in app/src/feed.ts; app/test/test_feed.mjs checks that).
# sky_cover / cloud_base_ft_agl repeat once per cloud layer.
AWC_TRUTH_COLS = ("station_id", "observation_time", "visibility_statute_mi", "sky_cover", "cloud_base_ft_agl")


def _awc_refuse(why: str) -> dict:
    """No observations from this file, said loudly. The run stays green on
    purpose (the forecast was already written and must still publish); the
    annotation is what makes the gap in the verification record visible."""
    print(f"::error title=AWC observations not recorded::{why} No truth observations were recorded from this file; "
          f"the forecast itself is unaffected. See parse_awc_truth in forecast/engine.py.")
    return {}


def parse_awc_truth(text: str, stations: set[str]) -> dict:
    """Truth flags per station from the text of AWC's metars.cache.csv.

    Columns are found by NAME in the header row. Until 2026-11-24 this read
    fixed positions (visibility = 10th field, sky cover = 22nd/24th/...), the
    same pattern as the FAA fixed-width and NBM header incidents: one column
    inserted upstream and every station would have been logged with visibility
    taken from the wrong field. Those rows are the TRUTH the calibrated forecast
    is scored against, so a wrong one is worse than a missing one. If a column
    this needs is missing or ambiguous, or the rows do not line up with the
    header, it records NOTHING from the file and says why.
    """
    lines = text.split("\n")
    # leading blank lines are skipped, as app/src/feed.ts and the watchdog do:
    # the three readers must agree on what a readable file is
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return _awc_refuse("metars.cache.csv came back empty.")
    header = [h.strip() for h in _csv_fields(lines[0].lstrip("\ufeff"))]
    where = {c: [i for i, h in enumerate(header) if h == c] for c in AWC_TRUTH_COLS}
    missing = [c for c in AWC_TRUTH_COLS if not where[c]]
    if missing:
        return _awc_refuse(f"metars.cache.csv has no column named {', '.join(missing)} "
                           f"(its first line starts {lines[0][:60]!r}).")
    twice = [c for c in AWC_TRUTH_COLS[:3] if len(where[c]) > 1]
    if twice:
        return _awc_refuse(f"metars.cache.csv has more than one column named {', '.join(twice)}.")
    covers, bases = where["sky_cover"], where["cloud_base_ft_agl"]
    if len(covers) != len(bases) or any(
            not (c < b and (k + 1 == len(covers) or b < covers[k + 1]))
            for k, (c, b) in enumerate(zip(covers, bases))):
        return _awc_refuse("the sky_cover / cloud_base_ft_agl columns of metars.cache.csv no longer pair up.")
    i_id, i_time, i_vis = (where[c][0] for c in AWC_TRUTH_COLS[:3])

    out, good, bad, bad_width = {}, 0, 0, 0
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = _csv_fields(line)
        # a row that does not line up with the header (a field inserted in the
        # data but not named) cannot be read by name
        if len(cols) != len(header):
            bad += 1
            bad_width = bad_width or len(cols)
            continue
        good += 1
        icao = cols[i_id]
        if icao not in stations or icao in out:
            continue
        v = cols[i_vis]
        try:
            vis = float(v) if v and "+" not in v else (10.0 if v else None)
        except ValueError:
            vis = None
        ceil = None
        for c, b in zip(covers, bases):
            if cols[c] in ("BKN", "OVC", "OVX"):
                try:
                    base = float(cols[b])
                    ceil = base if ceil is None else min(ceil, base)
                except ValueError:
                    pass
        if vis is None:
            continue
        out[icao] = {
            "obs": cols[i_time],
            "v10": int(vis < 1.0), "v05": int(vis < 0.5), "v025": int(vis < 0.25),
            "sub": int(vis < 0.5 or (ceil is not None and ceil < 200)),
        }
    if bad > good:
        return _awc_refuse(f"metars.cache.csv rows have {bad_width} fields but its header names {len(header)}.")
    if not good:
        # a header row and nothing under it: green and silent until 2026-11-25
        return _awc_refuse("metars.cache.csv has a header row and no reports under it.")
    return out


def _csv_fields(line: str) -> list[str]:
    """One CSV line into fields; raw_text is quoted and can contain commas."""
    try:
        return next(csv.reader([line.rstrip("\r")]), [])
    except csv.Error:
        return []


def awc_truth(stations: set[str]) -> dict:
    raw = fetch(AWC, timeout=120)
    if not raw:
        # fetch() swallows every error and returns None. This used to return
        # {} without a word: an AWC block of the runner would have left the
        # verification record empty hour after hour on green runs, the only
        # trace being "obs recorded: 0" in the step log.
        return _awc_refuse("metars.cache.csv.gz could not be downloaded (no response, or an empty one).")
    try:
        text = gzip.decompress(raw).decode("latin-1", "replace")
    except Exception as e:  # noqa: BLE001 (BadGzipFile, EOFError, zlib.error)
        # an error page served in place of the .gz used to raise here, AFTER
        # current.json was written and BEFORE the next step uploaded it: one
        # bad AWC response would have cost that hour's forecast
        return _awc_refuse(f"metars.cache.csv.gz did not decompress ({e!r}).")
    return parse_awc_truth(text, stations)


def main() -> None:
    stations = set(json.load(open(HERE / "stations.json")))
    raw_calib = json.load(open(OUT / "calibration.json"))
    if raw_calib.get("version") == 2:
        products = raw_calib["products"]
        offsets = raw_calib.get("offsets", {})
    else:  # v1 fallback: one model for every product, no offsets
        products = {"NBS": {"features": list(next(iter(raw_calib["thresholds"].values()))["coef"]),
                            "thresholds": raw_calib["thresholds"]}}
        offsets = {}
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
                "liv": r.get("liv") if r.get("liv") is not None else 0.0,
                "lic": r.get("lic") if r.get("lic") is not None else 0.0,
            }
            prod = r["product"] if r["product"] in products else "NBS"
            pcal = products[prod]["thresholds"]
            poff = offsets.get(prod, {})
            row = []
            for thr in THRESHOLDS:
                feats = dict(feats_base,
                             clim_logit=clim_logit(climo, means, icao, valid.month, valid.hour, thr))
                z_extra = poff.get(thr, {}).get(icao, 0.0)
                p = score(pcal[thr], feats) if thr in pcal else 0.0
                if z_extra and 0.0 < p < 1.0:
                    z = math.log(p / (1 - p)) + z_extra
                    p = 1.0 / (1.0 + math.exp(-z))
                row.append(round(100 * p))
            probs.append(row)
        airports[icao] = {"fhrs": fhrs, "p": probs,
                          "liv": [byf[f]["liv"] for f in fhrs],
                          "vis": [byf[f]["vis_sm"] for f in fhrs]}

    # A full-size bulletin that parses to (almost) nothing is an upstream
    # FORMAT change, and it must fail the run, not publish. On 2026-11-03 NOAA
    # dropped the version token from the NBM station headers, the parser
    # matched 0 stations, and this script wrote {"airports": {}} to KV on every
    # run for 21 days — all green. Raising here stops the job BEFORE
    # current.json is written or uploaded: KV keeps the last good issuance, the
    # run goes red, and the watchdog's age check turns the gap into an issue.
    floor = len(stations) // 2
    if len(airports) < floor:
        raise RuntimeError(
            f"parsed forecasts for {len(airports)} of {len(stations)} stations (floor {floor}) from a "
            f"{len(nbh) // 1024} KB NBH bulletin — the NBM text format has probably changed "
            f"(see HEADER_RE and ROWS in forecast/parse_nbm.py). Nothing was published; "
            f"KV still holds the last good issuance.")

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # per-airport public flips: the committed record of deliberate bar-check
    # decisions (forecast/bar_status.json). Absent file = full shadow.
    try:
        bar = json.load(open(HERE / "bar_status.json"))
        public_airports = sorted(bar.get("public", []))
    except FileNotFoundError:
        public_airports = []
    current = {
        "meta": {"cycle": cycle_iso, "generated": generated,
                 "public": bool(public_airports),  # legacy master flag
                 "publicAirports": public_airports,
                 "calib_version": raw_calib.get("version", 1),
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
        json.dump({"cycle": cycle_iso, "generated": generated,
                   "calib": raw_calib.get("version", 1), "obs": obs,
                   "forecasts": {k: {"fhrs": v["fhrs"], "p": v["p"]} for k, v in airports.items()}},
                  f, separators=(",", ":"))
    size = (OUT / "current.json").stat().st_size
    print(f"current.json: {len(airports)} airports, {size // 1024}KB; obs recorded: {len(obs)}")
    ks = airports.get("KSFO", {})
    if ks:
        print("KSFO first 6 leads p(v10):", [row[0] for row in ks["p"][:6]], "fhrs", ks["fhrs"][:6])


if __name__ == "__main__":
    main()
