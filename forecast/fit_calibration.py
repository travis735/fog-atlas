#!/usr/bin/env python3
"""Fit the per-threshold calibration: NBM guidance -> P(fog event) at our thresholds.

Join: archived NBS forecasts (forecast/data/hindcast/) x hourly truth
(forecast/out/truth.parquet), obs rounded to nearest hour. Features per row:
  log_vis      log1p(NBM deterministic vis, SM)
  cig_k        NBM ceiling / 1000 ft (unlimited -> 25)
  ifv, ifc     NBM IFR probability rows (%)
  spread       tmp - dpt (F)
  wsp          wind speed (kt)
  clim_logit   per icao x month x hour base rate for the SAME threshold (2016-2025)
  lead         forecast hour
Model: sklearn LogisticRegression per threshold, trained on 2023-2024,
tested on 2025. Report Brier vs climatology-only baseline. Output:
forecast/out/calibration.json (committed — the engine applies it verbatim).
"""
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

HERE = Path(__file__).parent
OUT = HERE / "out"
THRESHOLDS = ["v10", "v05", "v025", "sub"]
FEATURES = ["log_vis", "cig_k", "ifv", "ifc", "spread", "wsp", "clim_logit", "lead"]


def load_forecasts() -> pd.DataFrame:
    from parse_nbm import parse_collective
    stations = set(json.load(open(HERE / "stations.json")))
    rows = []
    files = sorted((HERE / "data" / "hindcast").glob("nbs_*.txt"))
    for f in files:
        rows.extend(parse_collective(f, stations))
    print(f"parsed {len(rows):,} forecast rows from {len(files)} cycles")
    df = pd.DataFrame(rows)
    df = df[df.fhr <= 48].copy()
    df["valid"] = pd.to_datetime(df.cycle) + pd.to_timedelta(df.fhr, unit="h")
    return df


def main() -> None:
    fc = load_forecasts()
    con = duckdb.connect()
    con.register("fc", fc)
    joined = con.execute(f"""
        WITH t AS (
          SELECT icao, date_trunc('hour', ts + INTERVAL 30 MINUTE) AS hr_ts,
                 max(v10) AS v10, max(v05) AS v05, max(v025) AS v025, max(sub) AS sub
          FROM '{OUT / "truth.parquet"}' GROUP BY 1, 2
        ), c AS (
          SELECT * FROM '{OUT / "climo.parquet"}'
        )
        SELECT f.*, t.v10, t.v05, t.v025, t.sub,
               c.r10, c.r05, c.r025, c.rsub, c.n AS clim_n
        FROM fc f
        JOIN t ON t.icao = f.icao AND t.hr_ts = f.valid
        JOIN c ON c.icao = f.icao AND c.mon = month(f.valid) AND c.hr = hour(f.valid)
    """).fetchdf()
    print(f"joined rows: {len(joined):,} ({joined.icao.nunique()} airports)")

    df = joined.dropna(subset=["vis_sm", "ifv", "ifc"]).copy()
    df["log_vis"] = np.log1p(df.vis_sm)
    df["cig_k"] = df.ceil_ft.fillna(25000) / 1000.0
    df["spread"] = (df.tmp - df.dpt).clip(-5, 40).fillna(10)
    df["wsp"] = df.wsp.clip(0, 50).fillna(5)
    df["lead"] = df.fhr
    df["year"] = df.valid.dt.year

    calib = {"features": FEATURES, "thresholds": {}, "meta": {
        "train": "2023-2024 pilot (monthly 12z cycles)", "test": "2025",
        "rows": int(len(df))}}
    print(f"\n{'thr':5s} {'n_test':>8s} {'base%':>6s}  {'Brier_clim':>10s} {'Brier_model':>11s} {'skill%':>7s}")
    for thr in THRESHOLDS:
        rate_col = {"v10": "r10", "v05": "r05", "v025": "r025", "sub": "rsub"}[thr]
        d = df.copy()
        # smoothed climatology prior for this threshold
        alpha = 2.0
        p_clim = ((d[rate_col] * d.clim_n + alpha * d[rate_col].mean()) / (d.clim_n + alpha)).clip(1e-4, 1 - 1e-4)
        d["clim_logit"] = np.log(p_clim / (1 - p_clim))
        train, test = d[d.year <= 2024], d[d.year == 2025]
        if len(test) < 500 or train[thr].sum() < 50:
            print(f"{thr:5s} insufficient data"); continue
        m = LogisticRegression(max_iter=2000, C=1.0)
        m.fit(train[FEATURES], train[thr])
        p = m.predict_proba(test[FEATURES])[:, 1]
        b_model = brier_score_loss(test[thr], p)
        b_clim = brier_score_loss(test[thr], p_clim.loc[test.index])
        skill = 100 * (1 - b_model / b_clim)
        print(f"{thr:5s} {len(test):8,d} {100*test[thr].mean():6.2f}  {b_clim:10.5f} {b_model:11.5f} {skill:+6.1f}%")
        calib["thresholds"][thr] = {
            "intercept": float(m.intercept_[0]),
            "coef": dict(zip(FEATURES, map(float, m.coef_[0]))),
            "test_brier_model": float(b_model), "test_brier_clim": float(b_clim),
            "test_skill_pct": float(skill), "test_n": int(len(test)),
        }
    json.dump(calib, open(OUT / "calibration.json", "w"), indent=1)
    print(f"\nwrote {OUT / 'calibration.json'}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    main()
