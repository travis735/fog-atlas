#!/usr/bin/env python3
"""Fit the v2 calibration: per-product models + shrunken per-airport offsets.

v2 over v1:
  - NBH gets its own model with the LIV/LIC rows (NBM's direct LIFR
    probabilities, hourly product only) as features; NBS keeps the v1 set.
  - Per-airport intercept recalibration: for each (product, threshold,
    airport), a logit offset solving mean(predicted) = mean(observed) on the
    train years (bisection — monotone), shrunk by n/(n+3000), |delta| <= 1.5.
    This targets systematic per-airport bias (the bar-check blowups) without
    letting thin samples swing anything.

Train 2023-2024, test 2025. Output: forecast/out/calibration.json (v2 schema:
{version:2, products:{NBS:{...},NBH:{...}}, offsets:{product:{thr:{icao:d}}}}).
The engine applies the product-matched model + offset per row.
"""
import json
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

HERE = Path(__file__).parent
OUT = HERE / "out"
THRESHOLDS = ["v10", "v05", "v025", "sub"]
RATE_COL = {"v10": "r10", "v05": "r05", "v025": "r025", "sub": "rsub"}
BASE_FEATURES = ["log_vis", "cig_k", "ifv", "ifc", "spread", "wsp", "clim_logit", "lead"]
PRODUCT_FEATURES = {"NBS": BASE_FEATURES, "NBH": BASE_FEATURES + ["liv", "lic"]}


def load_forecasts() -> pd.DataFrame:
    from parse_nbm import parse_collective
    stations = set(json.load(open(HERE / "stations.json")))
    rows = []
    files = sorted((HERE / "data" / "hindcast").glob("nb[sh]_*.txt"))
    for f in files:
        rows.extend(parse_collective(f, stations))
    print(f"parsed {len(rows):,} forecast rows from {len(files)} collectives")
    df = pd.DataFrame(rows)
    df = df[(df.fhr >= 1) & (df.fhr <= 48)].copy()
    df["valid"] = pd.to_datetime(df.cycle) + pd.to_timedelta(df.fhr, unit="h")
    return df


def solve_offset(z: np.ndarray, y: np.ndarray) -> float:
    """delta with mean(sigmoid(z+delta)) == mean(y); monotone -> bisection."""
    target = y.mean()
    if target <= 0 or target >= 1:
        return 0.0
    lo, hi = -3.0, 3.0
    for _ in range(45):
        mid = (lo + hi) / 2
        if (1 / (1 + np.exp(-(z + mid)))).mean() < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main() -> None:
    fc = load_forecasts()
    con = duckdb.connect()
    con.register("fc", fc)
    df = con.execute(f"""
        WITH t AS (
          SELECT icao, date_trunc('hour', ts + INTERVAL 30 MINUTE) AS hr_ts,
                 max(v10) AS v10, max(v05) AS v05, max(v025) AS v025, max(sub) AS sub
          FROM '{OUT / "truth.parquet"}' GROUP BY 1, 2
        )
        SELECT f.*, t.v10, t.v05, t.v025, t.sub,
               c.r10, c.r05, c.r025, c.rsub, c.n AS clim_n
        FROM fc f
        JOIN t ON t.icao = f.icao AND t.hr_ts = f.valid
        JOIN '{OUT / "climo.parquet"}' c
          ON c.icao = f.icao AND c.mon = month(f.valid) AND c.hr = hour(f.valid)
    """).fetchdf()
    print(f"joined rows: {len(df):,} ({df.icao.nunique()} airports)")

    df = df.dropna(subset=["vis_sm", "ifv", "ifc"]).copy()
    df["log_vis"] = np.log1p(df.vis_sm)
    df["cig_k"] = df.ceil_ft.fillna(25000) / 1000.0
    df["spread"] = (df.tmp - df.dpt).clip(-5, 40).fillna(10)
    df["wsp"] = df.wsp.clip(0, 50).fillna(5)
    df["lead"] = df.fhr
    df["liv"] = df.liv.fillna(0.0)
    df["lic"] = df.lic.fillna(0.0)
    df["year"] = df.valid.dt.year

    calib = {"version": 2, "products": {}, "offsets": {},
             "meta": {"train": "2023-2024 weekly 12z hindcast", "test": "2025",
                      "offset_rule": "mean-match bisection, shrink n/(n+3000), |d|<=1.5"}}
    print(f"\n{'prod':4s} {'thr':5s} {'n_test':>8s} {'skill_pool%':>11s} {'skill+off%':>10s} {'v1_skill%':>9s}")
    try:
        v1 = json.load(open(OUT / "calibration.json"))
        v1_skill = {t: v1["thresholds"][t]["test_skill_pct"] for t in THRESHOLDS} if "thresholds" in v1 else {}
    except Exception:
        v1_skill = {}

    for product, feats in PRODUCT_FEATURES.items():
        sub = df[df["product"] == product]
        if product == "NBH":
            sub = sub[(sub.liv.notna())]
        if len(sub) < 50_000:
            print(f"{product}: insufficient rows ({len(sub)})")
            continue
        calib["products"][product] = {"features": feats, "thresholds": {}}
        calib["offsets"][product] = {}
        for thr in THRESHOLDS:
            rate = RATE_COL[thr]
            d = sub.copy()
            alpha = 2.0
            p_clim = ((d[rate] * d.clim_n + alpha * d[rate].mean()) / (d.clim_n + alpha)).clip(1e-4, 1 - 1e-4)
            d["clim_logit"] = np.log(p_clim / (1 - p_clim))
            train, test = d[d.year <= 2024], d[d.year == 2025]
            if len(test) < 500 or train[thr].sum() < 50:
                continue
            m = LogisticRegression(max_iter=2000, C=1.0)
            m.fit(train[feats], train[thr])
            z_tr = m.decision_function(train[feats])
            z_te = m.decision_function(test[feats])

            # per-airport shrunken offsets from TRAIN residual bias
            offs = {}
            tr_icao = train.icao.values
            y_tr = train[thr].values
            for icao in np.unique(tr_icao):
                mask = tr_icao == icao
                n = int(mask.sum())
                if n < 200:
                    continue
                delta = solve_offset(z_tr[mask], y_tr[mask])
                delta *= n / (n + 3000.0)
                delta = float(np.clip(delta, -1.5, 1.5))
                if abs(delta) >= 0.05:
                    offs[str(icao)] = round(delta, 3)
            calib["offsets"][product][thr] = offs

            off_te = np.array([offs.get(i, 0.0) for i in test.icao.values])
            p_pool = 1 / (1 + np.exp(-z_te))
            p_off = 1 / (1 + np.exp(-(z_te + off_te)))
            y_te = test[thr].values
            b_clim = brier_score_loss(y_te, p_clim.loc[test.index])
            s_pool = 100 * (1 - brier_score_loss(y_te, p_pool) / b_clim)
            s_off = 100 * (1 - brier_score_loss(y_te, p_off) / b_clim)
            print(f"{product:4s} {thr:5s} {len(test):8,d} {s_pool:11.1f} {s_off:10.1f} {v1_skill.get(thr, float('nan')):9.1f}")
            calib["products"][product]["thresholds"][thr] = {
                "intercept": float(m.intercept_[0]),
                "coef": dict(zip(feats, map(float, m.coef_[0]))),
                "test_skill_pooled_pct": float(s_pool),
                "test_skill_offsets_pct": float(s_off),
                "test_n": int(len(test)), "n_offsets": len(offs),
            }

    json.dump(calib, open(OUT / "calibration.json", "w"), indent=1)
    n_off = sum(len(v) for p in calib["offsets"].values() for v in p.values())
    print(f"\nwrote v2 calibration ({n_off} nonzero offsets)")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    main()
