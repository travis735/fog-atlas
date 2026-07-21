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
    DEST.mkdir(parents=True, exist_ok=True)
    dates = [f"{y}{m:02d}15" for y in (2023, 2024, 2025) for m in range(1, 13)]
    got = skip = miss = 0
    for d in dates:
        dest = DEST / f"nbs_{d}_12z.txt"
        if dest.exists() and dest.stat().st_size > 1_000_000:
            skip += 1
            continue
        url = f"{BASE}/blend.{d}/12/text/blend_nbstx.t12z"
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
