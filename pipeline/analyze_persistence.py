#!/usr/bin/env python3
"""Phase 2: persistence statistics — once an airport drops below CAT I,
how long does it stay there?

Builds the full hourly band time series (vis + ceiling, same rules as
analyze_pilot.py), finds maximal runs of consecutive sub-CAT-I hours
(a missing hour breaks the run), and emits per-airport:

  n        number of events (runs) in 2016-2025
  medianH / p25H / p75H   event duration quartiles
  curve    P(event still ongoing after k more hours), k = 1..8
  buckets  the same curve conditioned on season x time-of-day of the
           event START (DJF/MAM/JJA/SON x night/morning/afternoon/evening),
           only where >= 20 events support it

Output: out/persistence.json keyed by ICAO. Airports with < 25 events
total get no stats (the app says "insufficient events").

Usage: python3 analyze_persistence.py --list airports_full.csv
"""

import csv
import json
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
RAW_SKY = HERE / "data" / "raw_sky"
OUT = HERE / "out"

EFVS_FLOOR_SM = 0.19
CAT1_MIN_SM = 0.50
CAT1_DH_FT = 200
MIN_EVENTS = 25
MIN_BUCKET_EVENTS = 20
SEASONS = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}
TODS = ["night", "morning", "afternoon", "evening"]  # 0-5, 6-11, 12-17, 18-23


def main():
    OUT.mkdir(exist_ok=True)
    list_name = sys.argv[sys.argv.index("--list") + 1] if "--list" in sys.argv else "airports_pilot.csv"
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE TABLE tzmap (icao TEXT, tz TEXT)")
    with open(HERE / list_name) as f:
        for a in csv.DictReader(f):
            con.execute("INSERT INTO tzmap VALUES (?,?)", [a["icao"], a["tz"]])

    print("building event table (runs of consecutive sub-CAT-I hours)...", flush=True)
    con.execute(f"""
        CREATE TABLE events AS
        WITH obs AS (
            SELECT regexp_extract(filename, '([A-Z0-9]+)\\.csv$', 1) AS icao,
                   strptime(valid, '%Y-%m-%d %H:%M') AS ts_utc,
                   TRY_CAST(vsby AS DOUBLE) AS vsby
            FROM read_csv_auto('{RAW}/*.csv', filename=true, all_varchar=true,
                               union_by_name=true)
        ),
        hourly AS (
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY icao, date_trunc('hour', ts_utc)
                    ORDER BY ts_utc DESC) AS rn
                FROM obs) WHERE rn = 1
        ),
        sky AS (
            SELECT regexp_extract(filename, '([A-Z0-9]+)\\.csv$', 1) AS icao,
                   strptime(valid, '%Y-%m-%d %H:%M') AS ts_utc,
                   least(
                     CASE WHEN skyc1 IN ('BKN','OVC','VV') THEN TRY_CAST(skyl1 AS DOUBLE) END,
                     CASE WHEN skyc2 IN ('BKN','OVC','VV') THEN TRY_CAST(skyl2 AS DOUBLE) END,
                     CASE WHEN skyc3 IN ('BKN','OVC','VV') THEN TRY_CAST(skyl3 AS DOUBLE) END
                   ) AS ceil_ft
            FROM read_csv_auto('{RAW_SKY}/*.csv', filename=true, all_varchar=true,
                               union_by_name=true)
        ),
        sky_hourly AS (
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY icao, date_trunc('hour', ts_utc)
                    ORDER BY ts_utc DESC) AS rn
                FROM sky) WHERE rn = 1
        ),
        banded AS (
            SELECT h.icao,
                   date_trunc('hour', h.ts_utc) AS ts,
                   (h.vsby IS NOT NULL AND
                    (h.vsby < {CAT1_MIN_SM} OR s.ceil_ft < {CAT1_DH_FT})) AS sub
            FROM hourly h
            LEFT JOIN sky_hourly s
              ON s.icao = h.icao
             AND date_trunc('hour', s.ts_utc) = date_trunc('hour', h.ts_utc)
            WHERE h.vsby IS NOT NULL
        ),
        runs AS (
            SELECT icao, ts, sub,
                   -- new run when state changes OR the series has a gap
                   sum(CASE WHEN prev_sub IS NULL OR prev_sub != sub
                              OR ts - prev_ts > INTERVAL 1 HOUR
                            THEN 1 ELSE 0 END)
                       OVER (PARTITION BY icao ORDER BY ts) AS run_id
            FROM (
                SELECT icao, ts, sub,
                       lag(sub) OVER (PARTITION BY icao ORDER BY ts) AS prev_sub,
                       lag(ts) OVER (PARTITION BY icao ORDER BY ts) AS prev_ts
                FROM banded
            )
        )
        SELECT r.icao,
               min(r.ts) AS start_utc,
               count(*) AS dur_h,
               month(timezone(t.tz, min(r.ts)::TIMESTAMPTZ)) AS start_mon,
               hour(timezone(t.tz, min(r.ts)::TIMESTAMPTZ)) AS start_hr
        FROM runs r JOIN tzmap t USING (icao)
        WHERE r.sub
        GROUP BY r.icao, r.run_id, t.tz
    """)
    n_events = con.execute("SELECT count(*) FROM events").fetchone()[0]
    print(f"{n_events} sub-CAT-I events across all airports", flush=True)

    rows = con.execute("""
        SELECT icao, dur_h, start_mon, start_hr FROM events ORDER BY icao
    """).fetchall()

    def curve(durs):
        n = len(durs)
        return [round(sum(1 for d in durs if d > k) / n, 3) for k in range(1, 9)]

    def quart(durs, q):
        s = sorted(durs)
        return s[min(int(q * len(s)), len(s) - 1)]

    by_icao = {}
    for icao, dur, mon, hr in rows:
        by_icao.setdefault(icao, []).append((dur, mon, hr))

    out = {}
    for icao, evs in by_icao.items():
        durs = [d for d, _, _ in evs]
        if len(durs) < MIN_EVENTS:
            continue
        buckets = {}
        for season in ("DJF", "MAM", "JJA", "SON"):
            for ti, tod in enumerate(TODS):
                sel = [d for d, m, h in evs
                       if SEASONS[m] == season and h // 6 == ti]
                if len(sel) >= MIN_BUCKET_EVENTS:
                    buckets[f"{season}-{tod}"] = {"n": len(sel), "curve": curve(sel)}
        out[icao] = {
            "n": len(durs),
            "medianH": quart(durs, 0.5),
            "p25H": quart(durs, 0.25),
            "p75H": quart(durs, 0.75),
            "curve": curve(durs),
            "buckets": buckets,
        }

    (OUT / "persistence.json").write_text(json.dumps(out))
    print(f"{len(out)} airports with >= {MIN_EVENTS} events -> {OUT}/persistence.json")


if __name__ == "__main__":
    main()
