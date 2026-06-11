#!/usr/bin/env python3
"""BTS cancellation validation (US-only, phase 1.5).

Joins US DOT/BTS on-time performance data (scheduled departures,
cancellations with cause codes) against our hourly band classification:
were flights scheduled during sub-CAT-I hours cancelled for weather more
often than baseline? This turns the METAR climatology into demonstrated
operational cost.

Honesty notes: BTS 'B' cancellations are generic weather (a thunderstorm
cancel looks identical to fog); scheduled departure time is local, our
bands are computed per local hour, so the join is hour-resolution; only
US scheduled passenger service is covered.

Usage: python3 validate_bts.py   (expects pipeline/data/bts/*.zip)
"""

import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
RAW_SKY = HERE / "data" / "raw_sky"
BTS = HERE / "data" / "bts"
OUT = HERE / "out"

EFVS_FLOOR_SM = 0.19
CAT1_MIN_SM = 0.50
CAT1_DH_FT = 200


def main():
    zips = sorted(BTS.glob("*.zip"))
    if not zips:
        sys.exit("no BTS zips in pipeline/data/bts")

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")

    # US airports: faa id (iem_id) -> icao + tz
    con.execute("CREATE TABLE usmeta (icao TEXT, faa TEXT, tz TEXT)")
    with open(HERE / "airports_full.csv") as f:
        for a in csv.DictReader(f):
            if a["country"] == "US":
                con.execute("INSERT INTO usmeta VALUES (?,?,?)",
                            [a["icao"], a["iem_id"], a["tz"]])

    print("building US hourly band table (vis + ceiling)...", flush=True)
    con.execute(f"""
        CREATE TABLE us_hourly AS
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
        )
        SELECT m.faa,
               CAST(timezone(m.tz, h.ts_utc::TIMESTAMPTZ) AS DATE) AS d,
               hour(timezone(m.tz, h.ts_utc::TIMESTAMPTZ)) AS hr,
               CASE
                   WHEN h.vsby IS NULL THEN 'missing'
                   WHEN h.vsby < {EFVS_FLOOR_SM} THEN 'below'
                   WHEN h.vsby < {CAT1_MIN_SM} THEN 'efvs'
                   WHEN s.ceil_ft < {CAT1_DH_FT} THEN 'efvs'
                   ELSE 'normal'
               END AS band
        FROM hourly h
        JOIN usmeta m ON m.icao = h.icao
        LEFT JOIN sky_hourly s
          ON s.icao = h.icao AND date_trunc('hour', s.ts_utc) = date_trunc('hour', h.ts_utc)
        WHERE h.ts_utc >= '2023-01-01'
    """)
    print(con.execute("SELECT count(*) FROM us_hourly").fetchone()[0],
          "US station-hours", flush=True)

    con.execute("""CREATE TABLE flights (
        d DATE, origin TEXT, hr INTEGER, cancelled INTEGER, code TEXT)""")
    for z in zips:
        with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
            subprocess.run(["unzip", "-p", str(z), "*.csv"],
                           stdout=tmp, check=True)
            con.execute(f"""
                INSERT INTO flights
                SELECT FlightDate::DATE,
                       Origin,
                       TRY_CAST(CRSDepTime AS INTEGER) // 100,
                       Cancelled::DOUBLE::INTEGER,
                       COALESCE(CancellationCode, '')
                FROM read_csv_auto('{tmp.name}')
            """)
        print(f"  {z.name}", flush=True)
    n = con.execute("SELECT count(*) FROM flights").fetchone()[0]
    print(n, "flights loaded", flush=True)

    result = con.execute("""
        SELECT u.band,
               count(*) AS flights,
               sum(f.cancelled) AS cancelled,
               sum(CASE WHEN f.code = 'B' THEN 1 ELSE 0 END) AS wx_cancelled,
               round(100.0 * sum(CASE WHEN f.code='B' THEN 1 ELSE 0 END) / count(*), 3) AS wx_rate_pct
        FROM flights f
        JOIN us_hourly u ON u.faa = f.origin AND u.d = f.d AND u.hr = f.hr
        WHERE u.band != 'missing'
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    print("\nband        flights  cancelled  wx_cancelled  wx_rate%")
    rates = {}
    for band, fl, canc, wx, rate in result:
        rates[band] = rate
        print(f"{band:8} {fl:10} {canc:10} {wx:13} {rate:8}")
    if rates.get("normal"):
        for b in ("efvs", "below"):
            if rates.get(b):
                print(f"{b}: {rates[b]/rates['normal']:.1f}x baseline weather-cancel rate")

    per_airport = con.execute("""
        SELECT f.origin,
               count(*) FILTER (u.band IN ('efvs','below')) AS exposed_flights,
               round(100.0 * sum(CASE WHEN f.code='B' THEN 1 ELSE 0 END)
                     FILTER (u.band IN ('efvs','below'))
                   / nullif(count(*) FILTER (u.band IN ('efvs','below')), 0), 2) AS wx_rate_subcat1,
               round(100.0 * sum(CASE WHEN f.code='B' THEN 1 ELSE 0 END)
                     FILTER (u.band = 'normal')
                   / nullif(count(*) FILTER (u.band = 'normal'), 0), 2) AS wx_rate_normal
        FROM flights f
        JOIN us_hourly u ON u.faa = f.origin AND u.d = f.d AND u.hr = f.hr
        GROUP BY 1 HAVING exposed_flights >= 200
        ORDER BY exposed_flights DESC LIMIT 25
    """).fetchall()
    print("\ntop exposed airports (flights scheduled in sub-CAT-I hours, 2023-24):")
    print("origin  exposed  wx-cancel% (sub-CAT-I)  wx-cancel% (normal)")
    for r in per_airport:
        print(f"{r[0]:6} {r[1]:8} {str(r[2]):>12} {str(r[3]):>18}")

    OUT.mkdir(exist_ok=True)
    (OUT / "bts_validation.json").write_text(json.dumps({
        "window": "2023-2024",
        "bands": {b: {"flights": fl, "wxCancelPct": rate}
                  for b, fl, _, _, rate in result},
        "multiplier_efvs": round(rates.get("efvs", 0) / rates["normal"], 1) if rates.get("normal") else None,
        "multiplier_below": round(rates.get("below", 0) / rates["normal"], 1) if rates.get("normal") else None,
        "perAirport": [{"faa": r[0], "exposed": r[1], "wxSubCat1": r[2], "wxNormal": r[3]}
                       for r in per_airport],
    }, indent=1))
    print(f"\nwrote {OUT}/bts_validation.json")


if __name__ == "__main__":
    main()
