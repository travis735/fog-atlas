#!/usr/bin/env python3
"""Build hourly fog truth + climatological base rates for the forecast tier.

Inputs:  pipeline/data/raw/{icao}.csv (hourly vis) + raw_sky/{icao}.csv (layers)
         forecast/data/nbs_sample.txt (any NBM collective — defines coverage)
Outputs: forecast/out/truth.parquet          hourly flags per covered airport
         forecast/out/climo.parquet         per icao x month x hour base rates
         forecast/stations.json             covered station list (committed)

Thresholds (statute miles, matching atlas band rules):
  v10  vis < 1.0        public headline "fog"
  v05  vis < 0.5        dense-ish / sub-CAT-I visibility term
  v025 vis < 0.25       dense fog (NWS advisory threshold)
  sub  vis < 0.5 OR ceiling < 200 ft   EFVS/pro event (atlas definition)
"""
import json
import re
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
PIPE = HERE.parent / "pipeline"
OUT = HERE / "out"


def covered_stations(collective: Path) -> list[str]:
    ids = set()
    for line in open(collective, encoding="latin-1"):
        m = re.match(r"^ ([A-Z0-9]{3,5})\s+NBM V", line)
        if m:
            ids.add(m.group(1))
    atlas = json.load(open(PIPE / "out" / "app" / "airports.json"))["airports"]
    return sorted(a["icao"] for a in atlas if a["icao"] in ids)


def main() -> None:
    sample = HERE / "data" / "nbs_sample.txt"
    stations = covered_stations(sample)
    OUT.mkdir(exist_ok=True)
    json.dump(stations, open(HERE / "stations.json", "w"))
    print(f"{len(stations)} NBM-covered atlas airports")

    con = duckdb.connect()
    con.execute("SET threads TO 8")
    vis_files = [str(PIPE / "data" / "raw" / f"{s}.csv") for s in stations
                 if (PIPE / "data" / "raw" / f"{s}.csv").exists()]
    sky_files = [str(PIPE / "data" / "raw_sky" / f"{s}.csv") for s in stations
                 if (PIPE / "data" / "raw_sky" / f"{s}.csv").exists()]
    print(f"vis files: {len(vis_files)}, sky files: {len(sky_files)}")

    # IEM's station column is the 3-letter FAA id for US fields but ICAO for
    # Canada — the FILENAME is the reliable ICAO key
    con.execute(f"""
        CREATE VIEW vis AS
        SELECT regexp_extract(filename, '([A-Z0-9]+)\\.csv$', 1) AS icao,
               valid::TIMESTAMP AS ts, TRY_CAST(vsby AS DOUBLE) AS vsby
        FROM read_csv_auto({vis_files!r}, union_by_name=true, filename=true)
    """)
    con.execute(f"""
        CREATE VIEW sky AS
        SELECT regexp_extract(filename, '([A-Z0-9]+)\\.csv$', 1) AS icao,
               valid::TIMESTAMP AS ts,
          LEAST(
            CASE WHEN skyc1 IN ('BKN','OVC','VV') THEN COALESCE(TRY_CAST(skyl1 AS DOUBLE), 99999) ELSE 99999 END,
            CASE WHEN skyc2 IN ('BKN','OVC','VV') THEN COALESCE(TRY_CAST(skyl2 AS DOUBLE), 99999) ELSE 99999 END,
            CASE WHEN skyc3 IN ('BKN','OVC','VV') THEN COALESCE(TRY_CAST(skyl3 AS DOUBLE), 99999) ELSE 99999 END
          ) AS ceil
        FROM read_csv_auto({sky_files!r}, union_by_name=true, filename=true)
    """)
    con.execute("""
        CREATE TABLE truth AS
        SELECT v.icao, v.ts,
               v.vsby,
               NULLIF(s.ceil, 99999) AS ceil,
               (v.vsby < 1.0)::TINYINT  AS v10,
               (v.vsby < 0.5)::TINYINT  AS v05,
               (v.vsby < 0.25)::TINYINT AS v025,
               (v.vsby < 0.5 OR COALESCE(s.ceil, 99999) < 200)::TINYINT AS sub
        FROM vis v LEFT JOIN sky s ON v.icao = s.icao AND v.ts = s.ts
        WHERE v.vsby IS NOT NULL
    """)
    con.execute(f"COPY truth TO '{OUT / 'truth.parquet'}' (FORMAT PARQUET)")

    con.execute("""
        CREATE TABLE climo AS
        SELECT icao, month(ts) AS mon, hour(ts) AS hr,
               count(*) AS n,
               avg(v10) AS r10, avg(v05) AS r05, avg(v025) AS r025, avg(sub) AS rsub
        FROM truth GROUP BY 1, 2, 3
    """)
    con.execute(f"COPY climo TO '{OUT / 'climo.parquet'}' (FORMAT PARQUET)")

    n, span = con.execute("SELECT count(*), max(ts) - min(ts) FROM truth").fetchone()
    print(f"truth rows: {n:,}  span: {span}")
    for icao in ("KSFO", "CYQI", "KVLD"):
        r = con.execute(
            "SELECT round(avg(v10)*100,2), round(avg(sub)*100,2) FROM truth WHERE icao=?",
            [icao]).fetchone()
        print(f"  {icao}: vis<1mi {r[0]}% of hours, sub-CAT-I {r[1]}%")


if __name__ == "__main__":
    sys.exit(main())
