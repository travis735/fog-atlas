#!/usr/bin/env python3
"""Fetch routine hourly METAR history from the Iowa Environmental Mesonet.

Two modes per station, decided automatically:
  full         no usable local file — fetch 2016-01-01 -> today
  incremental  local file present — fetch from its last observation's DAY
               forward, drop overlap, append (quarterly-refresh mode)

Batches several stations per request (IEM serves multi-station CSVs much
faster than per-station round-trips); stations are grouped by identical
fetch-start so one request window fits all members. Stations whose FULL
fetch returns too few rows (no METAR feed, wrong id) are recorded in
data/raw/_missing.txt so reruns don't refetch them forever — incremental
fetches never blacklist (a quiet quarter is not a dead feed).
IEM rate-limits by IP: we pause between requests and back off when told.

Usage:
  python3 fetch_iem.py                            # airports_pilot.csv
  python3 fetch_iem.py --list airports_full.csv   # full list (resumable)
  python3 fetch_iem.py KSFO LFPG                  # specific airports
  python3 fetch_iem.py --sky --list ...           # ceiling pass (raw_sky/)
  python3 fetch_iem.py --batch 10 --pause 10 ...  # crawl tuning
"""

import csv
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
HERE = Path(__file__).parent

args = sys.argv[1:]
SKY = "--sky" in args
if SKY:
    args.remove("--sky")


def _flag(name, default, cast):
    if name in args:
        i = args.index(name)
        v = cast(args[i + 1])
        del args[i : i + 2]
        return v
    return default


BATCH = _flag("--batch", 5, int)
PAUSE_BETWEEN = _flag("--pause", 20, int)   # seconds between batch requests
RAW = HERE / "data" / ("raw_sky" if SKY else "raw")
MISSING = RAW / "_missing.txt"
START_DAY = date(2016, 1, 1)
END_DAY = date.today() + timedelta(days=1)   # exclusive
FIELDS = (["skyc1", "skyc2", "skyc3", "skyl1", "skyl2", "skyl3"] if SKY
          else ["tmpf", "dwpf", "sknt", "vsby", "wxcodes"])
MIN_ROWS = 1000        # fewer rows than this over the full window = no feed
MIN_LAG_DAYS = 2       # local file this fresh -> skip the station entirely
MAX_TRIES = 6


def log(msg):
    print(msg, flush=True)


def last_valid_ts(path: Path) -> str | None:
    """Timestamp string ('YYYY-MM-DD HH:MM') of the file's last data row."""
    try:
        with open(path, "rb") as f:
            f.seek(max(f.seek(0, 2) - 4096, 0))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        if len(lines) < 1 or lines[-1].startswith("station"):
            return None
        return lines[-1].split(",")[1]
    except Exception:
        return None


def fetch_batch(batch, start_day: date):
    """batch: list of (iem_id, icao, since_ts|None). One shared window.
    since_ts None = full fetch (blacklist eligible). Returns False on hard failure."""
    params = [
        *[("station", iem_id) for iem_id, _, _ in batch],
        *[("data", f) for f in FIELDS],
        ("year1", str(start_day.year)), ("month1", str(start_day.month)), ("day1", str(start_day.day)),
        ("year2", str(END_DAY.year)), ("month2", str(END_DAY.month)), ("day2", str(END_DAY.day)),
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("missing", "empty"),
        ("trace", "empty"),
        ("report_type", "3"),  # routine hourly METARs only
    ]
    url = BASE + "?" + urllib.parse.urlencode(params)
    ids = " ".join(icao for _, icao, _ in batch)
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

        for iem_id, icao, since_ts in batch:
            rows = by_station.get(iem_id, [])
            out = RAW / f"{icao}.csv"
            if since_ts is None:
                if len(rows) < MIN_ROWS:
                    log(f"  {icao}: only {len(rows)} rows — no feed, marked missing")
                    with open(MISSING, "a") as f:
                        f.write(f"{icao}\n")
                    continue
                out.write_text(header + "\n" + "\n".join(rows) + "\n")
                log(f"  {icao}: {len(rows)} rows (full)")
            else:
                # append only what's newer than the file's last observation;
                # ISO 'YYYY-MM-DD HH:MM' strings compare correctly as text
                fresh = [r for r in rows if r.split(",")[1] > since_ts]
                if not fresh:
                    log(f"  {icao}: up to date (0 new)")
                    continue
                existing_header = out.open().readline().rstrip("\n")
                if existing_header != header:
                    log(f"  {icao}: header changed — refetch this station with a full pass")
                    continue
                with open(out, "a") as f:
                    f.write("\n".join(fresh) + "\n")
                log(f"  {icao}: +{len(fresh)} rows (since {since_ts})")
        return True
    log(f"[{ids}] giving up after {MAX_TRIES} attempts")
    return False


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    list_path = HERE / "airports_pilot.csv"
    rest = list(args)
    if rest and rest[0] == "--list":
        list_path = HERE / rest[1]
        rest = rest[2:]
    with open(list_path) as f:
        airports = list(csv.DictReader(f))
    want = set(a.upper() for a in rest)
    if want:
        airports = [a for a in airports if a["icao"] in want]

    missing = set(MISSING.read_text().split()) if MISSING.exists() else set()
    todo = []   # (iem_id, icao, since_ts|None, start_day)
    fresh = 0
    for a in airports:
        icao = a["icao"]
        if icao in missing:
            continue
        path = RAW / f"{icao}.csv"
        if path.exists() and path.stat().st_size > 100_000:
            ts = last_valid_ts(path)
            try:
                last_day = date.fromisoformat(ts[:10]) if ts else None
            except ValueError:
                last_day = None
            if last_day is None:  # unreadable tail — refetch from scratch
                todo.append((a["iem_id"], icao, None, START_DAY))
                continue
            if (date.today() - last_day).days < MIN_LAG_DAYS:
                fresh += 1
                continue
            todo.append((a["iem_id"], icao, ts, last_day))
        else:
            todo.append((a["iem_id"], icao, None, START_DAY))

    n_full = sum(1 for t in todo if t[2] is None)
    log(f"{len(airports)} listed, {len(todo)} to fetch "
        f"({n_full} full, {len(todo) - n_full} incremental; "
        f"{fresh} already fresh, {sum(1 for a in airports if a['icao'] in missing)} known-missing) "
        f"window through {END_DAY - timedelta(days=1)}")

    # group by fetch-start so one request window covers every batch member —
    # the window is the batch's EARLIEST start and each member drops its own
    # overlap by since_ts, so starts within ~a month can share a request.
    # Full fetches first (they unlock new stations), then oldest gaps.
    todo.sort(key=lambda t: (t[2] is not None, t[3]))
    failures = []
    t0 = time.time()
    done = 0
    i = 0
    while i < len(todo):
        batch = [todo[i]]
        while (len(batch) < BATCH and i + len(batch) < len(todo)
               and (todo[i + len(batch)][2] is None) == (batch[0][2] is None)
               and (todo[i + len(batch)][3] - batch[0][3]).days <= 31):
            batch.append(todo[i + len(batch)])
        i += len(batch)
        if done:
            time.sleep(PAUSE_BETWEEN)
        if not fetch_batch([(t[0], t[1], t[2]) for t in batch], batch[0][3]):
            failures.extend(t[1] for t in batch)
        done += len(batch)
        rate = done / max(time.time() - t0, 1) * 3600
        log(f"-- {done}/{len(todo)} ({rate:.0f} stations/hr)")

    if failures:
        log(f"\nFAILED (will retry on rerun): {' '.join(failures)}")
        sys.exit(1)
    log("\nAll stations fetched.")


if __name__ == "__main__":
    main()
