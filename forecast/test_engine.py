"""Tests for forecast/engine.py: the climatology prior, the logistic scorer,
the AWC truth parser, and one full offline issuance (network stubbed out)."""
import gzip
import json
import math

import pytest

import engine


def sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))


def logit(p):
    return math.log(p / (1 - p))


MEANS = {"v10": 0.04, "v05": 0.02, "v025": 0.01, "sub": 0.05}


# ------------------------------------------------------------ clim_logit

def test_clim_logit_shrinks_toward_global_mean():
    # (icao, mon, hr) -> (icao, mon, hr, n, r10, r05, r025, rsub)
    climo = {("KSFO", 7, 13): ("KSFO", 7, 13, 8, 0.25, 0.1, 0.0, 0.3)}
    # (rate*n + 2*mean) / (n + 2) with n=8: (0.25*8 + 2*0.04) / 10 = 0.208
    got = engine.clim_logit(climo, MEANS, "KSFO", 7, 13, "v10")
    assert got == pytest.approx(logit(0.208))


def test_clim_logit_uses_the_right_threshold_column():
    climo = {("KSFO", 7, 13): ("KSFO", 7, 13, 1000, 0.2, 0.1, 0.05, 0.3)}
    for thr, rate in [("v10", 0.2), ("v05", 0.1), ("v025", 0.05), ("sub", 0.3)]:
        want = (rate * 1000 + 2 * MEANS[thr]) / 1002
        assert engine.clim_logit(climo, MEANS, "KSFO", 7, 13, thr) == pytest.approx(logit(want))


def test_clim_logit_unknown_airport_falls_back_to_global_mean():
    assert engine.clim_logit({}, MEANS, "XXXX", 1, 0, "v05") == pytest.approx(logit(0.02))


def test_clim_logit_is_clamped_so_zero_and_one_rates_stay_finite():
    climo = {("A", 1, 0): ("A", 1, 0, 10**9, 0.0, 0.0, 0.0, 1.0)}
    lo = engine.clim_logit(climo, MEANS, "A", 1, 0, "v10")
    hi = engine.clim_logit(climo, MEANS, "A", 1, 0, "sub")
    assert lo == pytest.approx(logit(1e-4))
    assert hi == pytest.approx(logit(1 - 1e-4))


# ------------------------------------------------------------------ score

def test_score_is_the_logistic_of_the_linear_predictor():
    calib = {"intercept": -1.0, "coef": {"a": 2.0, "b": -0.5}}
    assert engine.score(calib, {"a": 1.5, "b": 4.0, "unused": 99}) == pytest.approx(sigmoid(-1 + 3 - 2))


def test_score_with_no_coefficients_is_the_intercept_alone():
    assert engine.score({"intercept": 0.0, "coef": {}}, {}) == 0.5


def test_score_raises_on_missing_feature():
    with pytest.raises(KeyError):
        engine.score({"intercept": 0.0, "coef": {"log_vis": 1.0}}, {})


# -------------------------------------------------------------- awc_truth

def awc_line(icao, obs, vis, layers=()):
    """A metars.cache.csv row: quoted raw METAR, then 0-based columns where
    0=station, 1=obs time, 9=visibility, and 21/23/25/27 are cover/base pairs."""
    cols = [""] * 30
    cols[0], cols[1], cols[9] = icao, obs, vis
    for i, (cover, base) in enumerate(layers):
        cols[21 + 2 * i], cols[22 + 2 * i] = cover, str(base)
    return f'"{icao} RAW METAR",' + ",".join(cols)


@pytest.fixture
def awc_feed(monkeypatch):
    def _install(lines):
        body = "\n".join(["No errors", "header,row", *lines]).encode("latin-1")
        monkeypatch.setattr(engine, "fetch", lambda url, timeout=180: gzip.compress(body))
    return _install


def test_awc_truth_flags_each_threshold(awc_feed):
    awc_feed([
        awc_line("KAAA", "2026-11-02T12:56:00Z", "0.75"),
        awc_line("KBBB", "2026-11-02T12:56:00Z", "0.25"),
        awc_line("KCCC", "2026-11-02T12:56:00Z", "0.125"),
        awc_line("KDDD", "2026-11-02T12:56:00Z", "6"),
    ])
    out = engine.awc_truth({"KAAA", "KBBB", "KCCC", "KDDD"})
    flags = {k: (v["v10"], v["v05"], v["v025"], v["sub"]) for k, v in out.items()}
    assert flags == {
        "KAAA": (1, 0, 0, 0),
        "KBBB": (1, 1, 0, 1),   # < 1/2 mile is sub-CAT-I on visibility alone
        "KCCC": (1, 1, 1, 1),
        "KDDD": (0, 0, 0, 0),
    }
    assert out["KAAA"]["obs"] == "2026-11-02T12:56:00Z"


def test_awc_truth_low_ceiling_alone_is_sub(awc_feed):
    awc_feed([
        awc_line("KLOW", "t", "3", [("FEW", 100), ("OVC", 150)]),
        awc_line("KSCT", "t", "3", [("SCT", 100), ("BKN", 900)]),  # SCT is not a ceiling
        awc_line("KMIN", "t", "3", [("BKN", 900), ("OVX", 0)]),    # lowest ceiling layer wins
    ])
    out = engine.awc_truth({"KLOW", "KSCT", "KMIN"})
    assert out["KLOW"]["sub"] == 1 and out["KLOW"]["v10"] == 0
    assert out["KSCT"]["sub"] == 0
    assert out["KMIN"]["sub"] == 1


def test_awc_truth_handles_10_plus_and_missing_visibility(awc_feed):
    awc_feed([
        awc_line("KPLS", "t", "10+"),
        awc_line("KNOV", "t", ""),
        awc_line("KBAD", "t", "M1/4"),
    ])
    out = engine.awc_truth({"KPLS", "KNOV", "KBAD"})
    assert out["KPLS"]["v10"] == 0
    assert "KNOV" not in out and "KBAD" not in out


def test_awc_truth_keeps_first_ob_and_only_known_stations(awc_feed):
    awc_feed([
        awc_line("KSFO", "2026-11-02T12:56:00Z", "0.25"),
        awc_line("KSFO", "2026-11-02T11:56:00Z", "10"),
        awc_line("KOTH", "2026-11-02T12:56:00Z", "0.25"),
    ])
    out = engine.awc_truth({"KSFO"})
    assert set(out) == {"KSFO"} and out["KSFO"]["v05"] == 1


def test_awc_truth_feed_down_returns_empty(monkeypatch):
    monkeypatch.setattr(engine, "fetch", lambda url, timeout=180: None)
    assert engine.awc_truth({"KSFO"}) == {}


# ------------------------------------------------------------ main (offline)

def nbm_row(icao, product, fhr, vis=10.0, **kw):
    r = {"icao": icao, "product": product, "cycle": "2026-11-02T12:00:00", "fhr": fhr,
         "tmp": 60.0, "dpt": 50.0, "wsp": 5.0, "vis_sm": vis, "ceil_ft": None,
         "ifc": 0.0, "ifv": 0.0, "lic": None, "liv": None}
    r.update(kw)
    return r


def flat_model(intercept=0.0, coef=None):
    return {"intercept": intercept, "coef": coef or {}}


@pytest.fixture
def sandbox(tmp_path, monkeypatch, write_climo):
    """engine.main() pointed at a temp dir with every network call stubbed."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(engine, "HERE", tmp_path)
    monkeypatch.setattr(engine, "OUT", out)
    (tmp_path / "stations.json").write_text(json.dumps(["KSFO", "KOAK"]))
    write_climo(out / "climo.parquet", [
        {"icao": "KSFO", "mon": 11, "hr": 13, "n": 100, "r10": 0.1, "r05": 0.05, "r025": 0.02, "rsub": 0.12},
    ])
    monkeypatch.setattr(engine, "latest_cycle", lambda: ("2026-11-02T12:00:00Z", b"nbh", b"nbs"))
    monkeypatch.setattr(engine, "awc_truth", lambda st: {"KSFO": {"obs": "x", "v10": 1, "v05": 0, "v025": 0, "sub": 0}})

    class Box:
        dir = tmp_path
        rows = {"NBH": [], "NBS": []}

        def calibration(self, calib):
            (out / "calibration.json").write_text(json.dumps(calib))

        def run(self):
            def fake_parse(path, stations):
                prod = "NBH" if "nbh" in path.name else "NBS"
                return [r for r in self.rows[prod] if r["icao"] in stations]
            monkeypatch.setattr(engine, "parse_collective", fake_parse)
            engine.main()
            current = json.loads((out / "current.json").read_text())
            logs = list((out / "log").glob("run_*.json.gz"))
            assert len(logs) == 1
            with gzip.open(logs[0], "rt") as f:
                log = json.load(f)
            return current, log
    return Box()


def v2(products, offsets=None):
    return {"version": 2, "products": products, "offsets": offsets or {}}


def test_main_merges_products_nbh_wins_and_leads_are_clipped(sandbox):
    sandbox.calibration(v2({
        "NBH": {"thresholds": {t: flat_model(logit(0.30)) for t in engine.THRESHOLDS}},
        "NBS": {"thresholds": {t: flat_model(logit(0.70)) for t in engine.THRESHOLDS}},
    }))
    sandbox.rows["NBH"] = [nbm_row("KSFO", "NBH", f) for f in (0, 1, 2)]
    sandbox.rows["NBS"] = [nbm_row("KSFO", "NBS", f) for f in (2, 30, 49)] + [
        nbm_row("KSFO", "NBS", 33, vis=None),     # no visibility -> skipped
        nbm_row("KZZZ", "NBS", 3),                # not in stations.json
    ]
    current, log = sandbox.run()
    ksfo = current["airports"]["KSFO"]
    assert ksfo["fhrs"] == [1, 2, 30]           # 0 and 49 are outside 1..48
    assert ksfo["p"] == [[30] * 4, [30] * 4, [70] * 4]  # NBH model wins lead 2
    assert set(current["airports"]) == {"KSFO"}
    assert current["meta"]["n_airports"] == 1 and current["meta"]["calib_version"] == 2
    assert current["meta"]["cycle"] == "2026-11-02T12:00:00Z"
    # the shadow log carries the same forecasts plus the recorded truth
    assert log["forecasts"]["KSFO"] == {"fhrs": ksfo["fhrs"], "p": ksfo["p"]}
    assert log["obs"]["KSFO"]["v10"] == 1


def test_main_applies_per_airport_logit_offsets(sandbox):
    sandbox.calibration(v2(
        {"NBS": {"thresholds": {t: flat_model(0.0) for t in engine.THRESHOLDS}}},
        {"NBS": {"v10": {"KSFO": math.log(3)}, "sub": {"KOAK": -math.log(3)}}},
    ))
    sandbox.rows["NBS"] = [nbm_row("KSFO", "NBS", 6), nbm_row("KOAK", "NBS", 6)]
    current, _ = sandbox.run()
    # sigmoid(0 + ln 3) = 0.75; sigmoid(0 - ln 3) = 0.25; everything else stays 0.5
    assert current["airports"]["KSFO"]["p"] == [[75, 50, 50, 50]]
    assert current["airports"]["KOAK"]["p"] == [[50, 50, 50, 25]]


def test_main_feeds_climatology_prior_and_capped_features(sandbox):
    # p = sigmoid(clim_logit + log_vis): checks the valid-time climo lookup
    # (12z cycle + 1h -> Nov, 13z) and the 10-mile visibility cap
    model = flat_model(0.0, {"clim_logit": 1.0, "log_vis": 1.0})
    sandbox.calibration(v2({"NBS": {"thresholds": {"v10": model}}}))
    sandbox.rows["NBS"] = [nbm_row("KSFO", "NBS", 1, vis=40.0)]
    current, _ = sandbox.run()
    means = {"v10": 0.1}
    prior = (0.1 * 100 + 2 * means["v10"]) / 102
    want = sigmoid(logit(prior) + math.log1p(10))
    p = current["airports"]["KSFO"]["p"][0]
    assert p[0] == round(100 * want)
    assert p[1:] == [0, 0, 0]                   # thresholds missing from the model -> 0


def test_main_unknown_product_falls_back_to_nbs_model(sandbox):
    sandbox.calibration(v2({"NBS": {"thresholds": {t: flat_model(logit(0.4)) for t in engine.THRESHOLDS}}}))
    sandbox.rows["NBH"] = [nbm_row("KSFO", "NBH", 1)]   # no NBH model in this calibration
    current, _ = sandbox.run()
    assert current["airports"]["KSFO"]["p"] == [[40] * 4]


def test_main_reads_v1_calibration(sandbox):
    sandbox.calibration({"thresholds": {t: flat_model(logit(0.2), {"lead": 0.0}) for t in engine.THRESHOLDS}})
    sandbox.rows["NBS"] = [nbm_row("KSFO", "NBS", 3)]
    current, log = sandbox.run()
    assert current["airports"]["KSFO"]["p"] == [[20] * 4]
    assert current["meta"]["calib_version"] == 1 and log["calib"] == 1


def test_main_public_airports_come_from_bar_status(sandbox):
    sandbox.calibration(v2({"NBS": {"thresholds": {}}}))
    sandbox.rows["NBS"] = [nbm_row("KSFO", "NBS", 3)]
    current, _ = sandbox.run()
    assert current["meta"]["public"] is False and current["meta"]["publicAirports"] == []

    (sandbox.dir / "bar_status.json").write_text(json.dumps({"public": ["KSFO", "KAST"]}))
    for f in (sandbox.dir / "out" / "log").glob("*"):
        f.unlink()
    current, _ = sandbox.run()
    assert current["meta"]["public"] is True and current["meta"]["publicAirports"] == ["KAST", "KSFO"]
