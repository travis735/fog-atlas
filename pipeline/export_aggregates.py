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
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
OUT = HERE / "out"
APP = OUT / "app"


def grid_from_rows(rows, value_idx):
    g = [[0.0] * 24 for _ in range(12)]
    for r in rows:
        if r[value_idx] is not None:  # cell can be all-missing (archive gap)
            g[int(r[0]) - 1][int(r[1])] = round(float(r[value_idx]), 1)
    return g


def main():
    (APP / "detail").mkdir(parents=True, exist_ok=True)
    list_name = sys.argv[sys.argv.index("--list") + 1] if "--list" in sys.argv else "airports_pilot.csv"
    with open(HERE / list_name) as f:
        meta = {a["icao"]: a for a in csv.DictReader(f)}

    # minima-aware floors, segmented by OPERATOR EQUIPAGE: the achievable
    # floor is whichever binds — flight deck or ground infrastructure.
    # US: per-ops-level published minima from the FAA ILS Master; LPV from
    # CIFP path points; international approximated from capability class.
    us_levels = {}
    fp = HERE / "data" / "us_ils_levels.csv"
    if fp.exists():
        with open(fp) as f:
            for r in csv.DictReader(f):
                us_levels[r["icao"]] = {k: float(r[k]) for k in
                                        ("cat1_m", "sacat1_m", "cat2_m", "cat3_m") if r[k]}
    lpv_set = set()
    fp = HERE / "data" / "us_lpv.csv"
    if fp.exists():
        with open(fp) as f:
            lpv_set = {r["icao"] for r in csv.DictReader(f)}

    # EGNOS LPV procedures (ESSP, operational only): LPV200 = DH 200ft
    # (CAT I-class minima), plain LPV = DH >= 250ft
    egnos = {}
    fp = HERE / "data" / "egnos_lpv.csv"
    if fp.exists():
        with open(fp) as f:
            egnos = {r["icao"]: r["lpv_class"] for r in csv.DictReader(f)}

    # international per-runway CAT I floors + LPV, eAIP-researched (agents,
    # audited); overrides the class approximation where present
    intl_floors = {}
    fp = HERE / "data" / "intl_floors.csv"
    if fp.exists():
        with open(fp) as f:
            for r in csv.DictReader(f):
                intl_floors[r["icao"]] = {
                    "cat1_m": float(r["cat1_m"]) if r["cat1_m"] else None,
                    "lpv": r["lpv"],
                }

    AUTHORITATIVE = ("faa-nasr", "faa-c060", "curated", "verify", "aip")

    def floors_for(m):
        """Achievable visibility floor (m) by equipage:
        cat1 deck / HUD (SA CAT I, EASA LTS CAT I) / cat2 / cat3."""
        icao = m["icao"]
        if icao in us_levels:
            lv = us_levels[icao]
            f1 = lv.get("cat1_m", 800.0)
            # SA CAT I: HUD to 150ft DH on a standard CAT I ILS — FAA
            # publishes which runways; RVR 1400 (~427m) where approved
            fh = min(f1, lv.get("sacat1_m", f1))
            f2 = min(fh, lv.get("cat2_m", fh))
            f3 = min(f2, lv.get("cat3_m", f2))
            return {"cat1": f1, "hud": fh, "cat2": f2, "cat3": f3}
        if m["country"] == "US":
            f = 800.0 if icao in lpv_set else 1600.0
            return {"cat1": f, "hud": f, "cat2": f, "cat3": f}
        cls = m["cat_ils"]
        ils_confirmed = (m["cat_ils_confidence"] in AUTHORITATIVE and cls != "NONE")
        f1 = 1600.0 if cls == "NONE" else 800.0
        if cls == "NONE" and icao in egnos:
            # EGNOS LPV substitutes for the missing ILS
            f1 = 800.0 if egnos[icao] == "LPV200" else 1100.0
        ifl = intl_floors.get(icao)
        if ifl and ifl["cat1_m"]:
            f1 = ifl["cat1_m"]
        elif ifl and ifl.get("lpv") == "yes" and cls == "NONE":
            f1 = min(f1, 800.0)
        # HUD tier abroad: EASA LTS CAT I (RVR 400m) analog — only where an
        # ILS is confirmed AND the CAT I floor is equipment-driven (<=800m).
        # Terrain-limited minima (e.g. Pasto RVR 2400) bind every deck:
        # a HUD lowers decision height, not mountains.
        fh = min(f1, 427.0) if (ils_confirmed and f1 <= 800.0) else f1
        if cls == "CATIII": return {"cat1": f1, "hud": fh, "cat2": min(fh, 350.0), "cat3": min(fh, 175.0)}
        if cls == "CATII":  return {"cat1": f1, "hud": fh, "cat2": min(fh, 350.0), "cat3": min(fh, 350.0)}
        return {"cat1": f1, "hud": fh, "cat2": fh, "cat3": fh}

    # vbin upper edges in meters (must match analyze_pilot.py's bins)
    VBIN_UPPER = [306, 354, 450, 805, 1609]

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

        # reliability: low coverage, or the MYAF signature — sub-CAT-I obs
        # dominated by literal-zero visibility with NO diurnal structure
        # (real fog is morning-skewed; encoding artifacts are flat)
        cov = 100.0 * valid_hours / 87672
        zr = con.execute("""
            SELECT count(*) FILTER (band IN ('efvs','below')) AS n_sub,
                   count(*) FILTER (band IN ('efvs','below') AND vzero) AS n_zero,
                   100.0 * count(*) FILTER (band IN ('efvs','below') AND hr BETWEEN 3 AND 9)
                       / nullif(count(*) FILTER (band != 'missing' AND hr BETWEEN 3 AND 9), 0) AS morn,
                   100.0 * count(*) FILTER (band IN ('efvs','below') AND hr BETWEEN 12 AND 18)
                       / nullif(count(*) FILTER (band != 'missing' AND hr BETWEEN 12 AND 18), 0) AS aft
            FROM c WHERE icao = ?
        """, [icao]).fetchone()
        n_sub, n_zero, morn, aft = zr
        if cov < 40:
            reliability = "low-coverage"
        elif (n_sub >= 50 and n_zero / n_sub > 0.5
              and (morn or 0) / max(aft or 0, 0.01) < 1.5):
            reliability = "suspect-reporting"
        else:
            reliability = "ok"

        causes = dict(con.execute("""
            SELECT cause, round(100.0 * count(*) / sum(count(*)) OVER (), 1)
            FROM c WHERE icao = ? AND band IN ('efvs','below') GROUP BY cause
        """, [icao]).fetchall())

        # minima-aware EFVS opportunity per equipage level: hours/yr below
        # the ACHIEVABLE floor (deck x ground) yet within EFVS range
        # (>=300m). Bins count only when fully below the floor
        # (conservative); ceiling-only hours count where a ~200ft DH is the
        # binding constraint (floor>=450m).
        floors = floors_for(m)
        vrow = con.execute("""
            SELECT count(*) FILTER (vbin = 1), count(*) FILTER (vbin = 2),
                   count(*) FILTER (vbin = 3), count(*) FILTER (vbin = 4),
                   count(*) FILTER (cause = 'CEIL' AND band = 'efvs')
            FROM c WHERE icao = ?
        """, [icao]).fetchone()

        def opp(floor_m):
            h = sum(vrow[k - 1] for k in range(1, 5) if VBIN_UPPER[k] <= floor_m)
            if floor_m >= 450:
                h += vrow[4]
            return round(8766.0 * h / valid_hours, 1) if valid_hours else 0.0

        opp_by_equip = {k: opp(v) for k, v in floors.items()}

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
            "size": m.get("size", "large"),
            "coveragePct": round(cov, 1),
            "reliability": reliability,
            # ILS presence: authoritative for the US (NASR), the C060 list,
            # and AIP-curated internationals; cat NONE = verified no ILS;
            # everything else is unknown, NOT no
            "ils": ("no" if m["cat_ils"] == "NONE"
                    else "yes" if m["cat_ils_confidence"] in ("faa-nasr", "faa-c060", "curated", "verify", "aip")
                    else "no" if m["country"] == "US" else "unknown"),
            "lpv": ("yes" if icao in lpv_set else "no" if m["country"] == "US"
                    else "yes" if icao in egnos
                    else intl_floors.get(icao, {}).get("lpv", "unknown") or "unknown"),
            "floors": {k: round(v) for k, v in floors.items()},
            "efvsOppByEquip": opp_by_equip,
            # back-compat / default audience: the CAT I-equipped operator
            "floorM": round(floors["cat1"]),
            "efvsOppHoursPerYear": opp_by_equip["cat1"],
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
