#!/usr/bin/env python3
"""Fetch routine hourly METAR history from the Iowa Environmental Mesonet.

Batches several stations per request (IEM serves multi-station CSVs much
faster than per-station round-trips) and splits the response into per-ICAO
files. Stations that return too few rows (no METAR feed, wrong id) are
recorded in data/raw/_missing.txt so reruns don't refetch them forever.
IEM rate-limits by IP: we pause between requests and back off when told.

Usage:
  python3 fetch_iem.py                          # airports_pilot.csv
  python3 fetch_iem.py --list airports_full.csv # full list (resumable)
  python3 fetch_iem.py KSFO LFPG                # specific airports
"""

import csv
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
HERE = Path(__file__).parent
SKY = "--sky" in sys.argv  # second pass: sky condition (ceiling) fields
if SKY:
    sys.argv.remove("--sky")
RAW = HERE / "data" / ("raw_sky" if SKY else "raw")
MISSING = RAW / "_missing.txt"
START = dict(year1=2016, month1=1, day1=1)
END = dict(year2=2026, month2=1, day2=1)  # exclusive: full years 2016-2025
FIELDS = (["skyc1", "skyc2", "skyc3", "skyl1", "skyl2", "skyl3"] if SKY
          else ["tmpf", "dwpf", "sknt", "vsby", "wxcodes"])
BATCH = 5
PAUSE_BETWEEN = 20   # seconds between batch requests
MIN_ROWS = 1000      # fewer rows than this over 10 years = no usable feed
MAX_TRIES = 6


def log(msg):
    print(msg, flush=True)


def fetch_batch(batch):
    """batch: list of (iem_id, icao). Returns False on hard failure."""
    params = [
        *[("station", iem_id) for iem_id, _ in batch],
        *[("data", f) for f in FIELDS],
        *[(k, str(v)) for k, v in {**START, **END}.items()],
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("missing", "empty"),
        ("trace", "empty"),
        ("report_type", "3"),  # routine hourly METARs only
    ]
    url = BASE + "?" + urllib.parse.urlencode(params)
    ids = " ".join(icao for _, icao in batch)
    delay = 60
    for attempt in range(1, MAX_TRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=900) as resp:
                body = resp.read().decode("utf-8", "replace")
        except Exception as e:
            log(f"[{ids}] attempt {attempt} failed ({e}); retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
            continue
        head = body[:200]
        if "Too many requests" in head or "slow down" in head:
            log(f"[{ids}] rate-limited; backing off {delay}s")
            time.sleep(delay)
            delay *= 2
            continue
        if not head.startswith("station"):
            log(f"[{ids}] unexpected response: {head!r}; retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
            continue

        lines = body.splitlines()
        header = lines[0]
        by_station = {}
        for line in lines[1:]:
            sid = line[: line.index(",")] if "," in line else ""
            by_station.setdefault(sid, []).append(line)

        icao_by_iem = {iem_id: icao for iem_id, icao in batch}
        for iem_id, icao in batch:
            rows = by_station.get(iem_id, [])
            if len(rows) < MIN_ROWS:
                log(f"  {icao}: only {len(rows)} rows — no feed, marked missing")
                with open(MISSING, "a") as f:
                    f.write(f"{icao}\n")
                continue
            out = RAW / f"{icao}.csv"
            out.write_text(header + "\n" + "\n".join(rows) + "\n")
            log(f"  {icao}: {len(rows)} rows")
        # anything in the response we didn't ask for is impossible; anything
        # asked for and absent was handled above
        return True
    log(f"[{ids}] giving up after {MAX_TRIES} attempts")
    return False


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    list_path = HERE / "airports_pilot.csv"
    args = sys.argv[1:]
    if args and args[0] == "--list":
        list_path = HERE / args[1]
        args = args[2:]
    with open(list_path) as f:
        airports = list(csv.DictReader(f))
    want = set(a.upper() for a in args)
    if want:
        airports = [a for a in airports if a["icao"] in want]

    missing = set(MISSING.read_text().split()) if MISSING.exists() else set()
    todo = [
        (a["iem_id"], a["icao"]) for a in airports
        if a["icao"] not in missing
        and not ((RAW / (a["icao"] + ".csv")).exists()
                 and (RAW / (a["icao"] + ".csv")).stat().st_size > 100_000)
    ]
    log(f"{len(airports)} listed, {len(todo)} to fetch "
        f"({len(airports) - len(todo)} cached or known-missing)")

    failures = []
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        if i:
            time.sleep(PAUSE_BETWEEN)
        batch = todo[i : i + BATCH]
        if not fetch_batch(batch):
            failures.extend(icao for _, icao in batch)
        done = min(i + BATCH, len(todo))
        rate = done / max(time.time() - t0, 1) * 3600
        log(f"-- {done}/{len(todo)} ({rate:.0f} stations/hr)")

    if failures:
        log(f"\nFAILED (will retry on rerun): {' '.join(failures)}")
        sys.exit(1)
    log("\nAll stations fetched.")


if __name__ == "__main__":
    main()
