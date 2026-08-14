#!/usr/bin/env python3
"""Fetch archived NBM NBS text collectives from the AWS Open Data mirror.

Pilot sample: 12z on the 15th of each month, 2023-01 .. 2025-12 (36 cycles,
~1 GB). Idempotent; files land in forecast/data/hindcast/ (gitignored).
A denser sample (weekly) should be pulled before the go-live bar check —
this pilot proves the pipeline and gets calibration in the ballpark.
"""
import sys
import time
import urllib.request
from pathlib import Path

DEST = Path(__file__).parent / "data" / "hindcast"
BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"


def main() -> None:
    import argparse
    from datetime import date, timedelta
    ap = argparse.ArgumentParser()
    ap.add_argument("--start"); ap.add_argument("--end"); ap.add_argument("--step-days", type=int, default=7)
    ap.add_argument("--product", choices=["nbs", "nbh"], default="nbs")
    args = ap.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)
    if args.start and args.end:
        d0, d1 = date.fromisoformat(args.start), date.fromisoformat(args.end)
        dates = []
        while d0 <= d1:
            dates.append(d0.strftime("%Y%m%d"))
            d0 += timedelta(days=args.step_days)
    else:
        dates = [f"{y}{m:02d}15" for y in (2023, 2024, 2025) for m in range(1, 13)]
    got = skip = miss = 0
    for d in dates:
        dest = DEST / f"{args.product}_{d}_12z.txt"
        if dest.exists() and dest.stat().st_size > 1_000_000:
            skip += 1
            continue
        url = f"{BASE}/blend.{d}/12/text/blend_{args.product}tx.t12z"
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                dest.write_bytes(r.read())
            got += 1
        except Exception as e:
            print(f"  miss {d}: {e}")
            miss += 1
        time.sleep(0.4)
    print(f"fetched {got}, skipped {skip}, missing {miss} of {len(dates)}")


if __name__ == "__main__":
    sys.exit(main())
