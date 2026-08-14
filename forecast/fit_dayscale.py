#!/usr/bin/env python3
"""Fit the V3.2 day-scale tier: P(chaseable fog day) at leads 3-8.

Target: for each airport x cycle-date x lead-day D (3..8), did the LOCAL
calendar day D contain >=1 sub-CAT-I hour (approx local = UTC + round(lon/15)).
Features:
  clim_logit  per-airport month P(any-sub day) from the 10-yr truth
  gefs_f16    GEFS member fraction with vis < 1600 m at the airport gridpoint
  gefs_f80    fraction < 8000 m
  gefs_lv     mean log1p(vis km) across members
  nbe_spread  min dew-point spread (F) across the day's NBE steps
  nbe_wsp     mean wind (kt) across the day's steps
  lead        D
Train <=2024, test 2025. SHIP RULE (pre-registered): pooled 2025 Brier skill
vs the day-climatology baseline > 0 AND >=4 of 6 lead-days individually > 0.

Outputs: forecast/out/dayscale.json (model + per-airport month day-climo) and
forecast/out/gefs_cache.parquet (extracted member values, reusable).
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

HERE = Path(__file__).parent
OUT = HERE / "out"
LEAD_OF = {"f072": 3, "f096": 4, "f120": 5, "f144": 6, "f168": 7, "f192": 8}


def airport_coords() -> dict:
    atlas = json.load(open(HERE.parent / "app" / "public" / "data" / "airports.json"))["airports"]
    st = set(json.load(open(HERE / "stations.json")))
    return {a["icao"]: (a["lat"], a["lon"] % 360, a["lon"]) for a in atlas if a["icao"] in st}


def build_gefs_cache(coords: dict) -> pd.DataFrame:
    cache = OUT / "gefs_cache.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        print(f"gefs cache: {len(df):,} rows (cached)")
        return df
    import eccodes as ec
    icaos = list(coords)
    lats = np.array([coords[i][0] for i in icaos])
    lons = np.array([coords[i][1] for i in icaos])
    acc: dict[tuple, list] = defaultdict(list)
    files = sorted((HERE / "data" / "gefs").glob("*.grb2"))
    print(f"decoding {len(files):,} GEFS slices…")
    for k, p in enumerate(files):
        date, mem, lead = p.stem.split("_")
        try:
            with open(p, "rb") as f:
                gid = ec.codes_grib_new_from_file(f)
            lat1 = ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
            lon1 = ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
            dlat = ec.codes_get(gid, "jDirectionIncrementInDegrees")
            dlon = ec.codes_get(gid, "iDirectionIncrementInDegrees")
            ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
            vals = ec.codes_get_values(gid).reshape(nj, ni)
            rr = np.clip(np.round((lat1 - lats) / dlat).astype(int), 0, nj - 1)
            cc = np.clip(np.round((lons - lon1) / dlon).astype(int), 0, ni - 1)
            v = vals[rr, cc]
            acc[(date, LEAD_OF[lead])].append(v)
            ec.codes_release(gid)
        except Exception:
            continue
        if k % 2000 == 0:
            print(f"  {k:,}/{len(files):,}")
    rows = []
    for (date, lead), members in acc.items():
        m = np.stack(members)  # members x airports
        f16 = (m < 1600).mean(axis=0)
        f80 = (m < 8000).mean(axis=0)
        lv = np.log1p(m / 1000.0).mean(axis=0)
        for j, icao in enumerate(icaos):
            rows.append((icao, date, lead, float(f16[j]), float(f80[j]), float(lv[j]), m.shape[0]))
    df = pd.DataFrame(rows, columns=["icao", "date", "lead", "gefs_f16", "gefs_f80", "gefs_lv", "n_members"])
    df.to_parquet(cache)
    print(f"gefs cache built: {len(df):,} rows")
    return df


def build_nbe_features() -> pd.DataFrame:
    import sys
    sys.path.insert(0, str(HERE))
    from parse_nbm import parse_collective
    stations = set(json.load(open(HERE / "stations.json")))
    rows = []
    files = sorted((HERE / "data" / "hindcast").glob("nbe_*.txt"))
    print(f"parsing {len(files)} NBE collectives…")
    for p in files:
        date = p.stem.split("_")[1]
        for r in parse_collective(p, stations):
            D = (r["fhr"] + 11) // 24  # 13z cycle: fhr 24D-11..24D+12 ~ lead-day D
            if 3 <= D <= 8 and r["tmp"] is not None and r["dpt"] is not None:
                rows.append((r["icao"], date, D, r["tmp"] - r["dpt"], r["wsp"] if r["wsp"] is not None else 5.0))
    df = pd.DataFrame(rows, columns=["icao", "date", "lead", "spread", "wsp"])
    agg = df.groupby(["icao", "date", "lead"]).agg(
        nbe_spread=("spread", "min"), nbe_wsp=("wsp", "mean")).reset_index()
    print(f"NBE day-features: {len(agg):,}")
    return agg


def build_targets_and_climo(coords: dict):
    con = duckdb.connect()
    con.execute(f"""
        CREATE VIEW t AS SELECT icao, ts, sub FROM '{OUT / "truth.parquet"}'
    """)
    tz = pd.DataFrame([(i, round(coords[i][2] / 15)) for i in coords], columns=["icao", "tzoff"])
    con.register("tz", tz)
    con.execute("""
        CREATE TABLE days AS
        SELECT t.icao, (t.ts + INTERVAL 1 HOUR * tz.tzoff)::DATE AS ld,
               max(sub) AS any_sub, sum(sub) AS hrs
        FROM t JOIN tz ON tz.icao = t.icao
        GROUP BY 1, 2
    """)
    targets = con.execute("SELECT icao, ld, any_sub, hrs FROM days").fetchdf()
    climo = con.execute("""
        SELECT icao, month(ld) AS mon, avg(any_sub) AS p_day, avg(hrs) AS hrs_day
        FROM days GROUP BY 1, 2
    """).fetchdf()
    print(f"targets: {len(targets):,} airport-days; climo rows: {len(climo):,}")
    return targets, climo


def main() -> None:
    coords = airport_coords()
    gefs = build_gefs_cache(coords)
    nbe = build_nbe_features()
    targets, climo = build_targets_and_climo(coords)

    df = gefs.merge(nbe, on=["icao", "date", "lead"], how="inner")
    df["cycle_date"] = pd.to_datetime(df.date, format="%Y%m%d")
    df["valid_ld"] = (df.cycle_date + pd.to_timedelta(df.lead, unit="D")).dt.date
    targets["ld"] = pd.to_datetime(targets.ld).dt.date
    df = df.merge(targets.rename(columns={"ld": "valid_ld"}), on=["icao", "valid_ld"], how="inner")
    df["mon"] = pd.to_datetime(df.valid_ld.astype(str)).dt.month
    df = df.merge(climo, on=["icao", "mon"], how="inner")
    p_clim = df.p_day.clip(1e-4, 1 - 1e-4)
    df["clim_logit"] = np.log(p_clim / (1 - p_clim))
    df["nbe_spread"] = df.nbe_spread.clip(-5, 40)
    df["nbe_wsp"] = df.nbe_wsp.clip(0, 50)
    df["year"] = pd.to_datetime(df.valid_ld.astype(str)).dt.year
    print(f"assembled: {len(df):,} rows, {df.icao.nunique()} airports")

    FEATS = ["clim_logit", "gefs_f16", "gefs_f80", "gefs_lv", "nbe_spread", "nbe_wsp", "lead"]
    train, test = df[df.year <= 2024], df[df.year == 2025]
    m = LogisticRegression(max_iter=2000)
    m.fit(train[FEATS], train.any_sub)
    p = m.predict_proba(test[FEATS])[:, 1]
    b_model = brier_score_loss(test.any_sub, p)
    b_clim = brier_score_loss(test.any_sub, test.p_day.clip(1e-4, 1 - 1e-4))
    skill = 100 * (1 - b_model / b_clim)
    print(f"\nPOOLED 2025: n={len(test):,} base={test.any_sub.mean():.3f} "
          f"Brier {b_model:.5f} vs climo {b_clim:.5f} -> skill {skill:+.1f}%")
    per_lead = {}
    pos = 0
    for L in range(3, 9):
        te = test[test.lead == L]
        if not len(te):
            continue
        pl = m.predict_proba(te[FEATS])[:, 1]
        s = 100 * (1 - brier_score_loss(te.any_sub, pl) / brier_score_loss(te.any_sub, te.p_day.clip(1e-4, 1 - 1e-4)))
        per_lead[L] = round(s, 1)
        pos += s > 0
        print(f"  lead d{L}: skill {s:+.1f}% (n={len(te):,})")
    ship = skill > 0 and pos >= 4
    print(f"\nSHIP RULE: pooled>0 AND >=4/6 leads>0 -> {'SHIP' if ship else 'HOLD'}")

    cl = defaultdict(dict)
    for r in climo.itertuples():
        cl[r.icao][str(int(r.mon))] = [round(float(r.p_day), 4), round(float(r.hrs_day), 3)]
    out = {
        "version": 1, "ship": bool(ship), "features": FEATS,
        "intercept": float(m.intercept_[0]),
        "coef": dict(zip(FEATS, map(float, m.coef_[0]))),
        "test": {"n": int(len(test)), "skill_pct": float(skill), "per_lead": per_lead},
        "rule": "pooled 2025 skill>0 AND >=4/6 lead-days>0 vs day-climatology",
        "climo_day": cl,
    }
    json.dump(out, open(OUT / "dayscale.json", "w"))
    print(f"wrote dayscale.json ({'SHIP' if ship else 'HOLD'})")


if __name__ == "__main__":
    main()
