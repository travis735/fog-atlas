#!/usr/bin/env python3
"""Phase 3: build the model training/test datasets.

One row per (airport, hour) with features at time t and labels = sub-CAT-I
at t+1/2/3/6h. Sub-CAT-I = vis < 0.5 SM OR ceiling < 200 ft (same rules as
the app). The airport's climatological sub-CAT-I rate for that month x hour
— computed on TRAIN YEARS ONLY — is included as a feature, so the trained
model directly competes with its own climatology input.

Sampling: TRAIN (2016-2023) keeps every row that is sub-CAT-I now or at any
label horizon, plus 2% of quiet rows (weight-corrected at train time).
TEST (2024-2025) is written UNSAMPLED for calibrated evaluation, restricted
to airports with >= 25 sub-CAT-I events (where prediction matters).

Output: out/model/train.parquet, out/model/test.parquet, out/model/clim.parquet
"""

import csv
import json
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
RAW_SKY = HERE / "data" / "raw_sky"
OUT = HERE / "out" / "model"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    list_name = sys.argv[sys.argv.index("--list") + 1] if "--list" in sys.argv else "airports_full.csv"
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("CREATE TABLE tzmap (icao TEXT, tz TEXT)")
    with open(HERE / list_name) as f:
        for a in csv.DictReader(f):
            con.execute("INSERT INTO tzmap VALUES (?,?)", [a["icao"], a["tz"]])

    # airports worth modeling: enough events for persistence stats
    persist = json.loads((HERE / "out" / "persistence.json").read_text())
    con.execute("CREATE TABLE keep (icao TEXT)")
    for icao in persist:
        con.execute("INSERT INTO keep VALUES (?)", [icao])
    print(f"{len(persist)} airports with >=25 events kept", flush=True)

    print("building hourly feature table...", flush=True)
    con.execute(f"""
        CREATE TABLE hourly AS
        WITH obs AS (
            SELECT regexp_extract(filename, '([A-Z0-9]+)\\.csv$', 1) AS icao,
                   strptime(valid, '%Y-%m-%d %H:%M') AS ts_utc,
                   TRY_CAST(vsby AS DOUBLE) AS vsby,
                   TRY_CAST(tmpf AS DOUBLE) AS tmpf,
                   TRY_CAST(dwpf AS DOUBLE) AS dwpf,
                   TRY_CAST(sknt AS DOUBLE) AS sknt
            FROM read_csv_auto('{RAW}/*.csv', filename=true, all_varchar=true,
                               union_by_name=true)
        ),
        hr AS (
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
        skyh AS (
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY icao, date_trunc('hour', ts_utc)
                    ORDER BY ts_utc DESC) AS rn
                FROM sky) WHERE rn = 1
        )
        SELECT h.icao,
               date_trunc('hour', h.ts_utc) AS ts,
               h.vsby, h.tmpf, h.dwpf, h.sknt,
               s.ceil_ft,
               (h.vsby IS NOT NULL AND
                (h.vsby < 0.5 OR s.ceil_ft < 200)) AS sub,
               month(timezone(t.tz, h.ts_utc::TIMESTAMPTZ)) AS mon,
               hour(timezone(t.tz, h.ts_utc::TIMESTAMPTZ)) AS hr
        FROM hr h
        JOIN keep k ON k.icao = h.icao
        JOIN tzmap t ON t.icao = h.icao
        LEFT JOIN skyh s ON s.icao = h.icao
            AND date_trunc('hour', s.ts_utc) = date_trunc('hour', h.ts_utc)
        WHERE h.vsby IS NOT NULL
    """)
    n = con.execute("SELECT count(*) FROM hourly").fetchone()[0]
    print(f"{n} hourly rows", flush=True)

    # climatology prior per (icao, mon, hr) from TRAIN years only
    con.execute("""
        CREATE TABLE clim AS
        SELECT icao, mon, hr,
               avg(CASE WHEN sub THEN 1.0 ELSE 0.0 END) AS clim_p,
               count(*) AS n
        FROM hourly WHERE year(ts) <= 2023
        GROUP BY 1, 2, 3
    """)
    con.execute(f"COPY clim TO '{OUT}/clim.parquet' (FORMAT PARQUET)")

    print("building labeled rows...", flush=True)
    con.execute("""
        CREATE TABLE labeled AS
        SELECT b.icao, b.ts, b.mon, b.hr,
               b.vsby, b.ceil_ft, b.tmpf, b.dwpf, b.sknt, b.sub,
               b.vsby - lag(b.vsby) OVER w AS vis_trend,
               CASE WHEN lag(b.ts) OVER w = b.ts - INTERVAL 1 HOUR
                    THEN lag(b.sub) OVER w END AS sub_prev,
               CASE WHEN lead(b.ts, 1) OVER w = b.ts + INTERVAL 1 HOUR
                    THEN lead(b.sub, 1) OVER w END AS y1,
               CASE WHEN lead(b.ts, 2) OVER w = b.ts + INTERVAL 2 HOUR
                    THEN lead(b.sub, 2) OVER w END AS y2,
               CASE WHEN lead(b.ts, 3) OVER w = b.ts + INTERVAL 3 HOUR
                    THEN lead(b.sub, 3) OVER w END AS y3,
               CASE WHEN lead(b.ts, 6) OVER w = b.ts + INTERVAL 6 HOUR
                    THEN lead(b.sub, 6) OVER w END AS y6,
               c.clim_p
        FROM hourly b
        LEFT JOIN clim c ON c.icao = b.icao AND c.mon = b.mon AND c.hr = b.hr
        WINDOW w AS (PARTITION BY b.icao ORDER BY b.ts)
    """)

    con.execute(f"""
        COPY (
            SELECT * FROM labeled
            WHERE year(ts) <= 2023 AND y2 IS NOT NULL
              AND (sub OR COALESCE(y1, false) OR COALESCE(y2, false)
                   OR COALESCE(y3, false) OR COALESCE(y6, false)
                   OR random() < 0.02)
        ) TO '{OUT}/train.parquet' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (
            SELECT * FROM labeled
            WHERE year(ts) >= 2024 AND y2 IS NOT NULL
        ) TO '{OUT}/test.parquet' (FORMAT PARQUET)
    """)
    for split in ("train", "test"):
        n, pos = con.execute(f"""
            SELECT count(*), sum(CASE WHEN y2 THEN 1 ELSE 0 END)
            FROM '{OUT}/{split}.parquet'""").fetchone()
        print(f"{split}: {n} rows, {pos} positive (y2)", flush=True)


if __name__ == "__main__":
    main()
