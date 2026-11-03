"""Tests for the calibration fit (fit_calibration.solve_offset) and a
contract check on the committed calibration.json / climo.parquet: the files
the hourly engine loads must have the shape engine.main() expects."""
import json
import math

import duckdb
import numpy as np
import pytest

import engine
import fit_calibration as fc

OUT = fc.OUT


def sigmoid(z):
    return 1 / (1 + np.exp(-z))


# ------------------------------------------------------------ solve_offset

@pytest.mark.parametrize("target", [0.05, 0.2, 0.5, 0.8])
def test_solve_offset_matches_the_observed_rate(target):
    rng = np.random.default_rng(0)
    z = rng.normal(-1.0, 0.5, 5000)
    y = np.zeros(5000)
    y[: int(target * 5000)] = 1
    d = fc.solve_offset(z, y)
    assert sigmoid(z + d).mean() == pytest.approx(target, abs=1e-6)


def test_solve_offset_sign_follows_the_bias():
    z = np.full(100, math.log(0.1 / 0.9))     # model says 10%
    under = np.r_[np.ones(30), np.zeros(70)]  # observed 30%: model too low
    over = np.r_[np.ones(5), np.zeros(95)]    # observed 5%: model too high
    assert fc.solve_offset(z, under) > 0
    assert fc.solve_offset(z, over) < 0
    # constant z: the answer is exactly logit(target) - z
    assert fc.solve_offset(z, under) == pytest.approx(math.log(0.3 / 0.7) - z[0], abs=1e-9)


def test_solve_offset_is_zero_when_rate_is_degenerate():
    z = np.zeros(10)
    assert fc.solve_offset(z, np.zeros(10)) == 0.0
    assert fc.solve_offset(z, np.ones(10)) == 0.0


def test_solve_offset_is_bounded_to_plus_minus_3():
    z = np.full(1000, -10.0)
    y = np.r_[np.ones(500), np.zeros(500)]
    assert fc.solve_offset(z, y) == pytest.approx(3.0, abs=1e-6)


# ---------------------------------------------- committed artifact contract

ENGINE_FEATURES = {"log_vis", "cig_k", "ifv", "ifc", "spread", "wsp", "lead", "liv", "lic", "clim_logit"}


@pytest.fixture(scope="module")
def calibration():
    return json.loads((OUT / "calibration.json").read_text())


def test_committed_calibration_covers_every_product_and_threshold(calibration):
    assert calibration["version"] == 2
    assert set(calibration["products"]) == {"NBS", "NBH"}
    for prod, spec in calibration["products"].items():
        assert set(spec["thresholds"]) == set(engine.THRESHOLDS), prod
        for thr, m in spec["thresholds"].items():
            assert set(m["coef"]) <= ENGINE_FEATURES, (prod, thr)
            assert set(m["coef"]) == set(spec["features"])
            assert all(math.isfinite(v) for v in [m["intercept"], *m["coef"].values()])


def test_committed_offsets_respect_the_shrinkage_cap(calibration):
    for prod, by_thr in calibration["offsets"].items():
        assert prod in calibration["products"]
        for thr, offs in by_thr.items():
            assert thr in engine.THRESHOLDS
            assert all(0.05 <= abs(d) <= 1.5 for d in offs.values()), (prod, thr)


def test_committed_climatology_is_well_formed():
    con = duckdb.connect()
    path = OUT / "climo.parquet"
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()]
    assert cols == ["icao", "mon", "hr", "n", "r10", "r05", "r025", "rsub"]
    bad = con.execute(f"""
        SELECT count(*) FROM '{path}'
        WHERE mon NOT BETWEEN 1 AND 12 OR hr NOT BETWEEN 0 AND 23 OR n < 1
           OR r10 NOT BETWEEN 0 AND 1 OR rsub NOT BETWEEN 0 AND 1
           OR r05 > r10 OR r025 > r05      -- nested thresholds: rarer events, lower rates
    """).fetchone()[0]
    assert bad == 0
    dupes = con.execute(f"SELECT count(*) - count(DISTINCT (icao, mon, hr)) FROM '{path}'").fetchone()[0]
    assert dupes == 0


def test_engine_loads_committed_artifacts_and_scores_a_row(calibration):
    climo, means = engine.load_climo()
    icao = next(iter(climo))[0]
    feats = {"log_vis": math.log1p(0.25), "cig_k": 0.2, "ifv": 60.0, "ifc": 70.0, "spread": 0.5,
             "wsp": 3.0, "lead": 6, "liv": 50.0, "lic": 60.0}
    for prod, spec in calibration["products"].items():
        for thr in engine.THRESHOLDS:
            f = dict(feats, clim_logit=engine.clim_logit(climo, means, icao, 1, 12, thr))
            p = engine.score(spec["thresholds"][thr], f)
            assert 0.0 < p < 1.0
