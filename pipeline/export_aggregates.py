#!/usr/bin/env python3
"""Export app-ready JSON aggregates from the classified observations.

This file defines the pipeline -> app contract:

  out/app/airports.json        index for the map: one record per airport with
                               annual band stats, cause mix, CAT flag, and a
                               compact 12x24 month x local-hour sub-CAT-I grid
                               (percent, 1 decimal) driving the time scrubber.
  out/app/detail/{icao}.json   deep-dive: separate efvs/below 12x24 grids,
                               monthly band split, cause mix, coverage.

Grids are [month][hour] = percent of hours, local time, months 1-12 -> idx 0-11.
"""

import csv
import json
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
OUT = HERE / "out"
APP = OUT / "app"


def grid_from_rows(rows, value_idx):
    g = [[0.0] * 24 for _ in range(12)]
    for r in rows:
        g[int(r[0]) - 1][int(r[1])] = round(float(r[value_idx]), 1)
    return g


def main():
    (APP / "detail").mkdir(parents=True, exist_ok=True)
    with open(HERE / "airports_pilot.csv") as f:
        meta = {a["icao"]: a for a in csv.DictReader(f)}

    con = duckdb.connect()
    con.execute(f"CREATE VIEW c AS SELECT * FROM '{OUT}/classified.parquet'")

    index = []
    for icao, m in sorted(meta.items()):
        stats = con.execute("""
            SELECT count(*) FILTER (band != 'missing') AS valid_hours,
                   count(*) AS total_hours,
                   round(8766.0 * (count(*) FILTER (band='efvs')) / nullif(count(*) FILTER (band!='missing'),0), 1),
                   round(8766.0 * (count(*) FILTER (band='below')) / nullif(count(*) FILTER (band!='missing'),0), 1)
            FROM c WHERE icao = ?
        """, [icao]).fetchone()
        if not stats or not stats[0]:
            continue
        valid_hours, total_hours, efvs_hpy, below_hpy = stats

        causes = dict(con.execute("""
            SELECT cause, round(100.0 * count(*) / sum(count(*)) OVER (), 1)
            FROM c WHERE icao = ? AND band IN ('efvs','below') GROUP BY cause
        """, [icao]).fetchall())

        grids = {}
        for bands, key in [(("efvs", "below"), "subcat1"), (("efvs",), "efvs"), (("below",), "below")]:
            rows = con.execute(f"""
                SELECT mon, hr,
                       100.0 * count(*) FILTER (band IN {repr(tuple(bands)) if len(bands)>1 else f"('{bands[0]}')"})
                             / nullif(count(*) FILTER (band != 'missing'), 0) AS pct
                FROM c WHERE icao = ? GROUP BY mon, hr
            """, [icao]).fetchall()
            grids[key] = grid_from_rows(rows, 2)

        monthly = con.execute("""
            SELECT mon,
                   round(100.0 * count(*) FILTER (band='efvs') / nullif(count(*) FILTER (band!='missing'),0), 2),
                   round(100.0 * count(*) FILTER (band='below') / nullif(count(*) FILTER (band!='missing'),0), 2)
            FROM c WHERE icao = ? GROUP BY mon ORDER BY mon
        """, [icao]).fetchall()

        index.append({
            "icao": icao,
            "name": m["name"],
            "lat": float(m["lat"]),
            "lon": float(m["lon"]),
            "country": m["country"],
            "tz": m["tz"],
            "catIls": m["cat_ils"],
            "catConfidence": m["cat_ils_confidence"],
            "efvsHoursPerYear": efvs_hpy,
            "belowHoursPerYear": below_hpy,
            "causes": causes,
            "grid": grids["subcat1"],
        })

        detail = {
            "icao": icao,
            "name": m["name"],
            "validHours": valid_hours,
            "coveragePct": round(100.0 * valid_hours / 87672, 1),
            "efvsGrid": grids["efvs"],
            "belowGrid": grids["below"],
            "monthly": [{"mon": r[0], "efvsPct": r[1], "belowPct": r[2]} for r in monthly],
            "causes": causes,
        }
        (APP / "detail" / f"{icao}.json").write_text(json.dumps(detail))

    (APP / "airports.json").write_text(json.dumps({
        "generated": "2016-2025 snapshot",
        "bands": {"efvs": "300-800m prevailing visibility", "below": "<300m"},
        "airports": index,
    }))
    sizes = sum(p.stat().st_size for p in APP.rglob("*.json"))
    print(f"exported {len(index)} airports, {sizes//1024} KB total -> {APP}")


if __name__ == "__main__":
    main()
