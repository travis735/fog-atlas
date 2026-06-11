#!/usr/bin/env python3
"""Build the full fetch list from OurAirports data.

Selection: large + medium airports with a 4-letter ICAO identifier — the
population that flies instrument approaches. IEM station ids: US/territory
stations use the FAA local code (SFO, not KSFO); everywhere else the ICAO.
Timezones come from timezonefinder (lat/lon -> IANA). CAT II/III flags are
merged from the curated pilot list; everything else is assumed CAT I until
curated (catIls=CATI, confidence=assumed).

Output: airports_full.csv sorted large-first (fetch priority), same schema
as airports_pilot.csv plus a `size` column.
"""

import csv
import re
from pathlib import Path

from timezonefinder import TimezoneFinder

HERE = Path(__file__).parent
ICAO_RE = re.compile(r"^[A-Z]{4}$")

US_COUNTRIES = {"US", "PR", "VI", "GU", "AS", "MP"}


def main():
    tf = TimezoneFinder()
    curated = {}
    with open(HERE / "airports_pilot.csv") as f:
        for row in csv.DictReader(f):
            curated[row["icao"]] = (row["cat_ils"], row["cat_ils_confidence"])
    # authoritative FAA sources override the hand-curated pilot rows:
    # cat_curated.csv = foreign OpSpec C060 list + US NASR ILS categories
    if (HERE / "cat_curated.csv").exists():
        with open(HERE / "cat_curated.csv") as f:
            for row in csv.DictReader(f):
                curated[row["icao"]] = (row["cat_ils"], row["cat_ils_confidence"])

    rows = []
    with open(HERE / "data" / "ourairports.csv") as f:
        for a in csv.DictReader(f):
            if a["type"] not in ("large_airport", "medium_airport"):
                continue
            icao = a["icao_code"] or a["gps_code"] or a["ident"]
            if not ICAO_RE.match(icao):
                continue
            lat, lon = float(a["latitude_deg"]), float(a["longitude_deg"])
            tz = tf.timezone_at(lat=lat, lng=lon)
            if not tz:
                continue
            if a["iso_country"] in US_COUNTRIES and a["local_code"]:
                iem_id = a["local_code"]
            else:
                iem_id = icao
            cat, conf = curated.get(icao, ("CATI", "assumed"))
            rows.append({
                "icao": icao,
                "iem_id": iem_id,
                "name": a["name"].replace(",", ";"),
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "tz": tz,
                "country": a["iso_country"],
                "cat_ils": cat,
                "cat_ils_confidence": conf,
                "size": a["type"].replace("_airport", ""),
            })

    # dedupe on icao (OurAirports has a few duplicate idents), large first
    seen = set()
    rows = [r for r in rows if not (r["icao"] in seen or seen.add(r["icao"]))]
    rows.sort(key=lambda r: (r["size"] != "large", r["country"], r["icao"]))

    out = HERE / "airports_full.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    large = sum(1 for r in rows if r["size"] == "large")
    print(f"{len(rows)} airports ({large} large, {len(rows)-large} medium) -> {out}")


if __name__ == "__main__":
    main()
