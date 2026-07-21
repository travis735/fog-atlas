#!/usr/bin/env python3
"""Score the shadow record: every issued forecast vs what actually happened.

Reads issuance logs (forecast/data/shadowlogs/, synced from R2 via
`wrangler r2 object get`), joins each forecast (cycle + lead -> valid hour)
against observations recorded in LATER logs, and reports Brier score vs the
per-airport climatology baseline — pooled and by lead bucket.

This is the chunk-5 bar-check tool. Run it any time; per-airport public
flips only happen at the pre-registered 3-4 week mark via a deliberate
decision, never automatically.

Logs may be raw JSON or gzip (R2 serves content-decoded; local runs are gz).
"""
import gzip
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
LOGS = HERE / "data" / "shadowlogs"
THRESHOLDS = ["v10", "v05", "v025", "sub"]
RATE = {"v10": "r10", "v05": "r05", "v025": "r025", "sub": "rsub"}


def load(p: Path) -> dict:
    raw = p.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def hour_key(iso: str) -> datetime:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)


def main() -> None:
    runs = sorted(LOGS.glob("run_*.json*"))
    if not runs:
        print("no logs in", LOGS, "- sync first: wrangler r2 object get fogatlas-forecast/logs/... ")
        return
    logs = [load(p) for p in runs]
    print(f"{len(logs)} issuance logs, {logs[0]['generated']} .. {logs[-1]['generated']}")

    # observed truth: (icao, valid_hour) -> flags (latest ob wins per hour)
    truth: dict[tuple, dict] = {}
    for lg in logs:
        for icao, ob in lg.get("obs", {}).items():
            try:
                truth[(icao, hour_key(ob["obs"]))] = ob
            except (KeyError, ValueError):
                continue
    print(f"truth points: {len(truth):,}")

    climo = {}
    for icao, m, h, r10, r05, r025, rsub in duckdb.connect().execute(
            f"SELECT icao, mon, hr, r10, r05, r025, rsub FROM '{HERE / 'out' / 'climo.parquet'}'").fetchall():
        climo[(icao, m, h)] = {"v10": r10, "v05": r05, "v025": r025, "sub": rsub}

    # join issued forecasts to later truth
    scored = defaultdict(lambda: {"n": 0, "bs_m": 0.0, "bs_c": 0.0})
    per_lead = defaultdict(lambda: defaultdict(lambda: {"n": 0, "bs_m": 0.0, "bs_c": 0.0}))
    seen = set()
    for lg in logs:
        cycle = datetime.fromisoformat(lg["cycle"].replace("Z", "+00:00"))
        for icao, f in lg.get("forecasts", {}).items():
            for fhr, p in zip(f["fhrs"], f["p"]):
                valid = cycle + timedelta(hours=fhr)
                ob = truth.get((icao, valid))
                if ob is None or valid > datetime.now(timezone.utc):
                    continue
                key = (lg["cycle"], icao, fhr)
                if key in seen:
                    continue
                seen.add(key)
                cl = climo.get((icao, valid.month, valid.hour))
                if cl is None:
                    continue
                for i, thr in enumerate(THRESHOLDS):
                    y = ob[thr]
                    pm = p[i] / 100.0
                    pc = min(max(cl[thr], 1e-4), 1 - 1e-4)
                    s = scored[thr]
                    s["n"] += 1; s["bs_m"] += (pm - y) ** 2; s["bs_c"] += (pc - y) ** 2
                    lb = "01-06" if fhr <= 6 else "07-12" if fhr <= 12 else "13-24" if fhr <= 24 else "25-48"
                    pl = per_lead[thr][lb]
                    pl["n"] += 1; pl["bs_m"] += (pm - y) ** 2; pl["bs_c"] += (pc - y) ** 2

    print(f"\n{'thr':5s} {'n':>9s} {'Brier_model':>11s} {'Brier_clim':>10s} {'skill%':>7s}")
    report = {"generated": datetime.now(timezone.utc).isoformat(), "runs": len(logs), "thresholds": {}}
    for thr in THRESHOLDS:
        s = scored[thr]
        if s["n"] == 0:
            print(f"{thr:5s} {'0':>9s}  (no verifiable pairs yet)")
            continue
        bm, bc = s["bs_m"] / s["n"], s["bs_c"] / s["n"]
        skill = 100 * (1 - bm / bc) if bc > 0 else 0.0
        print(f"{thr:5s} {s['n']:9,d} {bm:11.5f} {bc:10.5f} {skill:+6.1f}%")
        report["thresholds"][thr] = {
            "n": s["n"], "brier_model": bm, "brier_clim": bc, "skill_pct": skill,
            "by_lead": {lb: {"n": v["n"],
                             "skill_pct": 100 * (1 - (v["bs_m"] / v["n"]) / (v["bs_c"] / v["n"])) if v["n"] and v["bs_c"] > 0 else None}
                        for lb, v in sorted(per_lead[thr].items())},
        }
    out = HERE / "out" / "shadow_report.json"
    out.write_text(json.dumps(report, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
