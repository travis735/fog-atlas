#!/usr/bin/env python3
"""Fetch routine hourly METAR history from the Iowa Environmental Mesonet.

One request per station for the full window (fewer, larger requests are
politer to IEM than per-year chunks). IEM rate-limits by IP; we space
requests and back off exponentially when told to slow down.

Usage: python3 fetch_iem.py [icao ...]   (default: all rows in airports_pilot.csv)
"""

import csv
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
START = dict(year1=2016, month1=1, day1=1)
END = dict(year2=2026, month2=1, day2=1)  # exclusive upper bound: full years 2016-2025
FIELDS = ["tmpf", "dwpf", "sknt", "vsby", "wxcodes"]
PAUSE_BETWEEN = 20  # seconds between stations
MAX_TRIES = 6


def fetch_station(iem_id: str, icao: str) -> bool:
    out = RAW / f"{icao}.csv"
    if out.exists() and out.stat().st_size > 10_000:
        print(f"{icao}: cached ({out.stat().st_size//1024} KB), skipping")
        return True
    params = [
        ("station", iem_id),
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
    delay = 60
    for attempt in range(1, MAX_TRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=600) as resp:
                body = resp.read()
        except Exception as e:
            print(f"{icao}: attempt {attempt} failed ({e}); retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
            continue
        text_head = body[:200].decode("utf-8", "replace")
        if "Too many requests" in text_head or "slow down" in text_head:
            print(f"{icao}: rate-limited; backing off {delay}s")
            time.sleep(delay)
            delay *= 2
            continue
        if not text_head.startswith("station"):
            print(f"{icao}: unexpected response: {text_head!r}; retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
            continue
        rows = body.count(b"\n") - 1
        if rows < 1000:
            print(f"{icao}: only {rows} rows — wrong station id for IEM? ({iem_id})")
            return False
        out.write_bytes(body)
        print(f"{icao}: {rows} rows, {len(body)//1024} KB")
        return True
    print(f"{icao}: giving up after {MAX_TRIES} attempts")
    return False


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    with open(HERE / "airports_pilot.csv") as f:
        airports = list(csv.DictReader(f))
    want = set(a.upper() for a in sys.argv[1:])
    if want:
        airports = [a for a in airports if a["icao"] in want]
    failures = []
    for i, a in enumerate(airports):
        if i:
            time.sleep(PAUSE_BETWEEN)
        if not fetch_station(a["iem_id"], a["icao"]):
            failures.append(a["icao"])
    if failures:
        print(f"\nFAILED: {' '.join(failures)}")
        sys.exit(1)
    print("\nAll stations fetched.")


if __name__ == "__main__":
    main()
