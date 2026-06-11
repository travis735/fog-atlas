#!/usr/bin/env python3
"""Phase 3: train fog nowcast models and benchmark against climatology.

For each horizon k in {1,2,3,6} hours, predict P(sub-CAT-I at t+k) and
score on the UNSAMPLED 2024-2025 test set against three baselines:

  clim         the airport's month x hour climatological rate (train years)
  persistence  predict current state continues (p = 1 if sub now else clim)
  blend        persistence-style hand blend: sub now -> persistence curve
               flavor; else clim  (what the app effectively communicates)

Models: logistic regression (browser-shippable) and HistGradientBoosting
(complexity ceiling check). Sampled-out quiet rows are weight-corrected in
training; evaluation needs no correction (test is unsampled).

Scores: Brier (lower better, the honest probability score) + AUC.
"""

import json
from pathlib import Path

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

OUT = Path(__file__).parent / "out" / "model"
NEG_SAMPLE_RATE = 0.02  # must match build_training_set.py

FEATURES = ["vsby_c", "log_vis", "ceil_c", "low_ceil", "spread", "tmpf_c", "sknt_c",
            "vis_trend_c", "sub_now", "sub_prev_f", "mon_sin", "mon_cos",
            "hr_sin", "hr_cos", "clim_logit"]


def featurize(df):
    out = {}
    vs = df["vsby"].astype(float).clip(0, 10)
    out["vsby_c"] = vs
    out["log_vis"] = np.log1p(vs)
    ceil = df["ceil_ft"].astype(float)
    out["ceil_c"] = np.where(np.isnan(ceil), 25000, ceil).clip(0, 25000) / 1000.0
    out["low_ceil"] = (np.where(np.isnan(ceil), 25000, ceil) < 1000).astype(float)
    spread = (df["tmpf"] - df["dwpf"]).astype(float)
    out["spread"] = np.where(np.isnan(spread), 10.0, spread).clip(-5, 40)
    out["tmpf_c"] = np.nan_to_num(df["tmpf"].astype(float), nan=59.0).clip(-40, 120)
    out["sknt_c"] = np.nan_to_num(df["sknt"].astype(float), nan=5.0).clip(0, 50)
    out["vis_trend_c"] = np.nan_to_num(df["vis_trend"].astype(float), nan=0.0).clip(-10, 10)
    out["sub_now"] = df["sub"].fillna(False).astype(float).to_numpy()
    sp = df["sub_prev"]
    out["sub_prev_f"] = np.where(sp.isna(), 0.0, sp.fillna(False).astype(float))
    out["mon_sin"] = np.sin(2 * np.pi * df["mon"] / 12)
    out["mon_cos"] = np.cos(2 * np.pi * df["mon"] / 12)
    out["hr_sin"] = np.sin(2 * np.pi * df["hr"] / 24)
    out["hr_cos"] = np.cos(2 * np.pi * df["hr"] / 24)
    clim = np.nan_to_num(df["clim_p"].astype(float), nan=0.005).clip(1e-4, 1 - 1e-4)
    out["clim_logit"] = np.log(clim / (1 - clim))
    X = np.column_stack([out[f] for f in FEATURES])
    return X, clim


def main():
    con = duckdb.connect()
    train = con.execute(f"SELECT * FROM '{OUT}/train.parquet'").df()
    test = con.execute(f"SELECT * FROM '{OUT}/test.parquet'").df()
    print(f"train {len(train)} rows, test {len(test)} rows", flush=True)

    Xtr, _ = featurize(train)
    Xte, clim_te = featurize(test)
    # weight-correct the sampled-out quiet rows
    interesting = (train["sub"].fillna(False).astype(bool)
                   | train[["y1", "y2", "y3", "y6"]].fillna(False).astype(bool).any(axis=1))
    wtr = np.where(interesting, 1.0, 1.0 / NEG_SAMPLE_RATE)

    results = {}
    for k in ("y1", "y2", "y3", "y6"):
        ytr = train[k].astype("Float64")
        ok_tr = ~ytr.isna()
        yte = test[k].astype("Float64")
        ok_te = ~yte.isna()
        ytrv = ytr[ok_tr].astype(int).to_numpy()
        ytev = yte[ok_te].astype(int).to_numpy()
        Xtrv, Xtev = Xtr[ok_tr.values], Xte[ok_te.values]
        climv = clim_te[ok_te.values]
        subnow = test["sub"].fillna(False).astype(float)[ok_te].to_numpy()

        base_clim = np.clip(climv, 1e-4, 1 - 1e-4)
        base_pers = np.where(subnow > 0.5, 0.7, base_clim)  # crude persistence

        lr = LogisticRegression(max_iter=2000, C=1.0)
        lr.fit(Xtrv, ytrv, sample_weight=wtr[ok_tr.values])
        p_lr = lr.predict_proba(Xtev)[:, 1]

        gbm = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.08, max_depth=None,
            max_leaf_nodes=63, early_stopping=True, validation_fraction=0.05,
            random_state=7)
        gbm.fit(Xtrv, ytrv, sample_weight=wtr[ok_tr.values])
        p_gbm = gbm.predict_proba(Xtev)[:, 1]

        row = {}
        for name, p in (("clim", base_clim), ("persist", base_pers),
                        ("logreg", p_lr), ("gbm", p_gbm)):
            row[name] = {
                "brier": round(float(brier_score_loss(ytev, p)), 5),
                "auc": round(float(roc_auc_score(ytev, p)), 4),
            }
        results[k] = row
        print(f"\n=== horizon {k} (test n={len(ytev)}, base rate {ytev.mean():.4f}) ===")
        for name, s in row.items():
            print(f"  {name:8} brier={s['brier']:.5f}  auc={s['auc']:.4f}")

        if k == "y2":
            # keep the y2 logistic model for potential app export
            coef = {f: round(float(c), 5) for f, c in zip(FEATURES, lr.coef_[0])}
            (OUT / "logreg_y2.json").write_text(json.dumps(
                {"intercept": round(float(lr.intercept_[0]), 5), "coef": coef,
                 "features": FEATURES}, indent=1))
            imp = sorted(zip(FEATURES, np.abs(lr.coef_[0])), key=lambda t: -t[1])
            print("  top |coef|:", [(f, round(float(v), 2)) for f, v in imp[:6]])

    (OUT / "benchmark.json").write_text(json.dumps(results, indent=1))
    print(f"\nwrote {OUT}/benchmark.json")


if __name__ == "__main__":
    main()
