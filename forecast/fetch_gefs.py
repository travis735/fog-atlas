#!/usr/bin/env python3
"""Fetch GEFS ensemble surface-visibility slices via .idx byte ranges.

Each member grib is ~500 MB, but the .idx sidecar gives field offsets, so we
range-GET only the VIS:surface record (~0.2 MB). Sample: weekly 06z cycles,
members gep01-gep15, leads f072..f192 step 24 (days 3-8) — the day-scale
fitted tier's ensemble feature (member fraction below each threshold).

Output: forecast/data/gefs/{date}_{member}_{lead}.grb2 (gitignored).
"""
import argparse
import concurrent.futures as cf
import urllib.request
from datetime import date, timedelta
from pathlib import Path

DEST = Path(__file__).parent / "data" / "gefs"
BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
LEADS = ["f072", "f096", "f120", "f144", "f168", "f192"]
MEMBERS = [f"gep{i:02d}" for i in range(1, 16)]


def fetch_one(d: str, mem: str, lead: str) -> str:
    out = DEST / f"{d}_{mem}_{lead}.grb2"
    if out.exists() and out.stat().st_size > 10_000:
        return "skip"
    base = f"{BASE}/gefs.{d}/06/atmos/pgrb2bp5/{mem}.t06z.pgrb2b.0p50.{lead}"
    try:
        with urllib.request.urlopen(base + ".idx", timeout=60) as r:
            lines = r.read().decode().splitlines()
        start = end = None
        for i, line in enumerate(lines):
            parts = line.split(":")
            if len(parts) > 4 and parts[3] == "VIS" and parts[4] == "surface":
                start = int(parts[1])
                if i + 1 < len(lines):
                    end = int(lines[i + 1].split(":")[1]) - 1
                break
        if start is None:
            return "noVIS"
        req = urllib.request.Request(base, headers={"Range": f"bytes={start}-{end if end else ''}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out.write_bytes(r.read())
        return "ok"
    except Exception as e:
        return f"err:{type(e).__name__}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-05")
    ap.add_argument("--end", default="2025-12-28")
    ap.add_argument("--step-days", type=int, default=7)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)
    d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
    dates = []
    while d0 <= d1:
        dates.append(d0.strftime("%Y%m%d"))
        d0 += timedelta(days=args.step_days)
    jobs = [(d, m, l) for d in dates for m in MEMBERS for l in LEADS]
    print(f"{len(jobs)} slices ({len(dates)} dates x {len(MEMBERS)} members x {len(LEADS)} leads)")
    tally: dict[str, int] = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(lambda j: fetch_one(*j), jobs):
            tally[res.split(":")[0]] = tally.get(res.split(":")[0], 0) + 1
    print("tally:", tally)


if __name__ == "__main__":
    main()
