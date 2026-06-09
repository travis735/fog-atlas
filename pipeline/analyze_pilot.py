#!/usr/bin/env python3
"""Pilot analysis: classify METARs into EFVS visibility bands and print
validation climatology for whatever stations exist in data/raw/.

Bands (prevailing visibility as RVR proxy — documented approximation):
  normal   >= 0.50 SM (~800 m)   conventional CAT I workable
  efvs     0.19-0.50 SM (300-800 m)  below CAT I minima, EFVS-usable
  below    < 0.19 SM (~300 m)    too low for EFVS

Hourly basis: last routine ob in each UTC hour (US stations report ~:53-:56,
many intl stations report :00/:30 — taking the last keeps one ob per hour).
"""

import csv
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
OUT = HERE / "out"

EFVS_FLOOR_SM = 0.19   # ~300 m
CAT1_MIN_SM = 0.50     # ~800 m


def main():
    OUT.mkdir(exist_ok=True)
    files = sorted(RAW.glob("*.csv"))
    if not files:
        sys.exit("no raw data in pipeline/data/raw — run fetch_iem.py first")
    list_name = sys.argv[sys.argv.index("--list") + 1] if "--list" in sys.argv else "airports_pilot.csv"
    with open(HERE / list_name) as f:
        airports = {a["icao"]: a for a in csv.DictReader(f)}

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("""
        CREATE TABLE tzmap (icao TEXT, tz TEXT, cat_ils TEXT);
    """)
    for icao, a in airports.items():
        con.execute("INSERT INTO tzmap VALUES (?, ?, ?)", [icao, a["tz"], a["cat_ils"]])

    # single streaming statement: only the compact classified table is
    # materialized (raw obs at full scale are hundreds of millions of rows)
    con.execute(f"""
        CREATE TABLE classified AS
        WITH obs AS (
            SELECT
                regexp_extract(filename, '([A-Z0-9]+)\\.csv$', 1) AS icao,
                strptime(valid, '%Y-%m-%d %H:%M') AS ts_utc,
                TRY_CAST(vsby AS DOUBLE) AS vsby,
                COALESCE(wxcodes, '') AS wx
            FROM read_csv_auto('{RAW}/*.csv', filename=true, all_varchar=true,
                               union_by_name=true)
        ),
        -- one ob per UTC hour: the last routine report in the hour
        hourly AS (
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY icao, date_trunc('hour', ts_utc)
                    ORDER BY ts_utc DESC) AS rn
                FROM obs
            ) WHERE rn = 1
        )
        SELECT
            h.icao,
            month(timezone(t.tz, h.ts_utc::TIMESTAMPTZ)) AS mon,
            hour(timezone(t.tz, h.ts_utc::TIMESTAMPTZ)) AS hr,
            CASE
                WHEN h.vsby IS NULL THEN 'missing'
                WHEN h.vsby < {EFVS_FLOOR_SM} THEN 'below'
                WHEN h.vsby < {CAT1_MIN_SM} THEN 'efvs'
                ELSE 'normal'
            END AS band,
            CASE
                WHEN h.wx LIKE '%FG%' THEN 'FG'
                WHEN h.wx LIKE '%SN%' THEN 'SN'
                WHEN h.wx LIKE '%FU%' OR h.wx LIKE '%HZ%' THEN 'HZ/FU'
                WHEN h.wx LIKE '%BR%' THEN 'BR'
                WHEN h.wx = '' THEN 'none'
                ELSE 'other'
            END AS cause,
            t.cat_ils
        FROM hourly h JOIN tzmap t USING (icao)
    """)

    print("=== Coverage & band split (per airport, all hours 2016-2025) ===")
    print("max possible hours ~87,672; coverage below that means archive gaps\n")
    summary = con.execute("""
        SELECT icao,
               count(*) AS hours,
               round(100.0 * count(*) FILTER (band='missing') / count(*), 2) AS missing_pct,
               round(100.0 * count(*) FILTER (band='efvs') / count(*) FILTER (band!='missing'), 3) AS efvs_pct,
               round(100.0 * count(*) FILTER (band='below') / count(*) FILTER (band!='missing'), 3) AS below_pct,
               round(8766 * (count(*) FILTER (band='efvs')) / (count(*) FILTER (band!='missing')), 1) AS efvs_hrs_per_yr,
               round(8766 * (count(*) FILTER (band='below')) / (count(*) FILTER (band!='missing')), 1) AS below_hrs_per_yr,
               any_value(cat_ils) AS cat_ils
        FROM classified GROUP BY icao ORDER BY efvs_hrs_per_yr DESC
    """).df()
    print(summary.to_string(index=False))

    print("\n=== Cause mix within sub-CAT-I hours (row %) ===")
    causes = con.execute("""
        SELECT icao,
               round(100.0 * count(*) FILTER (cause='FG') / count(*), 1) AS fg,
               round(100.0 * count(*) FILTER (cause='BR') / count(*), 1) AS br,
               round(100.0 * count(*) FILTER (cause='HZ/FU') / count(*), 1) AS hz_fu,
               round(100.0 * count(*) FILTER (cause='SN') / count(*), 1) AS sn,
               round(100.0 * count(*) FILTER (cause IN ('none','other')) / count(*), 1) AS other,
               count(*) AS subcat1_hours
        FROM classified WHERE band IN ('efvs','below')
        GROUP BY icao ORDER BY subcat1_hours DESC
    """).df()
    print(causes.to_string(index=False))

    if len(summary) <= 25:
        print("\n=== Monthly sub-CAT-I frequency (% of hours, by airport) ===")
        monthly = con.execute("""
            PIVOT (
                SELECT icao, mon,
                       round(100.0 * count(*) FILTER (band IN ('efvs','below'))
                             / count(*) FILTER (band != 'missing'), 2) AS pct
                FROM classified GROUP BY icao, mon
            ) ON mon USING first(pct) GROUP BY icao ORDER BY icao
        """).df()
        print(monthly.to_string(index=False))

    for icao in (summary.icao if len(summary) <= 25 else summary.icao[:5]):
        print(f"\n=== {icao}: month x local-hour sub-CAT-I % (peak structure) ===")
        grid = con.execute(f"""
            PIVOT (
                SELECT mon, hr,
                       round(100.0 * count(*) FILTER (band IN ('efvs','below'))
                             / count(*) FILTER (band != 'missing'), 1) AS pct
                FROM classified WHERE icao = '{icao}' GROUP BY mon, hr
            ) ON hr USING first(pct) GROUP BY mon ORDER BY mon
        """).df()
        print(grid.to_string(index=False))

    con.execute(f"COPY classified TO '{OUT}/classified.parquet' (FORMAT PARQUET)")
    print(f"\nwrote {OUT}/classified.parquet")


if __name__ == "__main__":
    main()
