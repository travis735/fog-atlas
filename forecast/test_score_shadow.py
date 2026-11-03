"""Tests for forecast/score_shadow.py: the shadow-mode scorecard.

Each test writes a handful of issuance logs plus a climatology parquet into
a temp dir, runs score_shadow.main(), and checks shadow_report.json against
Brier scores worked out by hand."""
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

import score_shadow as ss

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ob(t, fog):
    return {"obs": iso(t), "v10": fog, "v05": fog, "v025": 0, "sub": fog}


def full_climo(icaos, rate):
    return [{"icao": i, "mon": m, "hr": h, "n": 100, "r10": rate, "r05": rate, "r025": rate, "rsub": rate}
            for i in icaos for m in range(1, 13) for h in range(24)]


@pytest.fixture
def scorecard(tmp_path, monkeypatch, write_climo):
    logs = tmp_path / "data" / "shadowlogs"
    logs.mkdir(parents=True)
    monkeypatch.setattr(ss, "HERE", tmp_path)
    monkeypatch.setattr(ss, "LOGS", logs)

    class Box:
        n = 0

        def climo(self, rows):
            write_climo(tmp_path / "out" / "climo.parquet", rows)

        def log(self, cycle, forecasts=None, obs=None, gz=False):
            Box.n += 1
            body = json.dumps({"cycle": iso(cycle), "generated": iso(cycle),
                               "forecasts": forecasts or {}, "obs": obs or {}}).encode()
            name = f"run_{Box.n:05d}.json" + (".gz" if gz else "")
            (logs / name).write_bytes(gzip.compress(body) if gz else body)

        def run(self):
            ss.main()
            return json.loads((tmp_path / "out" / "shadow_report.json").read_text())
    return Box()


def test_hour_key_rounds_to_nearest_hour():
    assert ss.hour_key("2025-01-01T01:29:59Z") == T0 + timedelta(hours=1)
    assert ss.hour_key("2025-01-01T01:30:00Z") == T0 + timedelta(hours=2)
    assert ss.hour_key("2025-01-01T23:51:00Z") == T0 + timedelta(hours=24)


def test_load_reads_plain_and_gzipped_logs(tmp_path):
    (tmp_path / "a.json").write_text('{"x": 1}')
    (tmp_path / "b.json.gz").write_bytes(gzip.compress(b'{"x": 2}'))
    assert ss.load(tmp_path / "a.json") == {"x": 1}
    assert ss.load(tmp_path / "b.json.gz") == {"x": 2}


def test_no_logs_is_a_clean_no_op(scorecard, tmp_path):
    ss.main()
    assert not (tmp_path / "out" / "shadow_report.json").exists()


def test_brier_and_skill_against_climatology(scorecard):
    scorecard.climo(full_climo(["KSFO"], 0.5))
    # one issuance: 80% at lead 1, 20% at lead 2, 90% at lead 3 (never verified)
    scorecard.log(T0, forecasts={"KSFO": {"fhrs": [1, 2, 3],
                                          "p": [[80, 80, 80, 80], [20, 20, 20, 20], [90, 90, 90, 90]]}})
    # truth arrives in later logs; obs times round to the nearest hour
    scorecard.log(T0 + timedelta(hours=1), obs={"KSFO": ob(T0 + timedelta(minutes=56), 1)}, gz=True)
    scorecard.log(T0 + timedelta(hours=2), obs={"KSFO": ob(T0 + timedelta(hours=2, minutes=10), 0)})
    # the same issuance replayed must not be double counted
    scorecard.log(T0, forecasts={"KSFO": {"fhrs": [1, 2], "p": [[80, 80, 80, 80], [20, 20, 20, 20]]}})

    rep = scorecard.run()
    v10 = rep["thresholds"]["v10"]
    # model: ((0.8-1)^2 + (0.2-0)^2) / 2 = 0.04 ; climatology 0.5: 0.25
    assert v10["n"] == 2
    assert v10["brier_model"] == pytest.approx(0.04)
    assert v10["brier_clim"] == pytest.approx(0.25)
    assert v10["skill_pct"] == pytest.approx(84.0)
    assert v10["by_lead"] == {"01-06": {"n": 2, "skill_pct": pytest.approx(84.0)}}
    # v025 never happened: model ((0.8)^2 + (0.2)^2)/2 = 0.34 vs 0.25 -> negative skill
    assert rep["thresholds"]["v025"]["skill_pct"] == pytest.approx(100 * (1 - 0.34 / 0.25))
    assert rep["runs"] == 4


def test_lead_buckets_and_missing_climo(scorecard):
    scorecard.climo([r for r in full_climo(["KSFO"], 0.5)])   # no climo for KNOC
    fhrs = [6, 7, 12, 13, 24, 25, 48]
    fc = {"fhrs": fhrs, "p": [[50] * 4] * len(fhrs)}
    scorecard.log(T0, forecasts={"KSFO": fc, "KNOC": fc})
    for h in fhrs:
        t = T0 + timedelta(hours=h)
        scorecard.log(t, obs={"KSFO": ob(t, 0), "KNOC": ob(t, 0)})
    rep = scorecard.run()
    by = rep["thresholds"]["v10"]["by_lead"]
    assert {k: v["n"] for k, v in by.items()} == {"01-06": 1, "07-12": 2, "13-24": 2, "25-48": 2}
    assert rep["thresholds"]["v10"]["n"] == 7     # KNOC has no climatology -> not scored


def test_future_valid_times_are_not_scored(scorecard):
    future = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2)
    scorecard.climo(full_climo(["KSFO"], 0.5))
    scorecard.log(future, forecasts={"KSFO": {"fhrs": [1], "p": [[50] * 4]}})
    scorecard.log(future, obs={"KSFO": ob(future + timedelta(hours=1), 1)})
    rep = scorecard.run()
    assert rep["thresholds"] == {}


def test_bar_check_pass_fail_and_insufficient(scorecard):
    """Pre-registered rule: >=3000 pairs and >=10 v10 event-hours, then skill > 0 on v10 AND sub."""
    scorecard.climo(full_climo(["KGOOD", "KBAD", "KTHIN"], 0.1))
    hours = 150
    fog = {h for h in range(hours) if h % 10 == 0}      # 15 fog hours
    for c in range(hours):
        t = T0 + timedelta(hours=c)
        fhrs = list(range(1, 25))
        good = [[95 if (c + f) in fog else 2] * 4 for f in fhrs]   # sharp and right
        bad = [[2 if (c + f) in fog else 95] * 4 for f in fhrs]    # confidently wrong
        forecasts = {"KGOOD": {"fhrs": fhrs, "p": good}, "KBAD": {"fhrs": fhrs, "p": bad}}
        if c < 5:
            forecasts["KTHIN"] = {"fhrs": fhrs, "p": good}
        scorecard.log(t, forecasts=forecasts,
                      obs={k: ob(t, int(c in fog)) for k in ("KGOOD", "KBAD", "KTHIN")})
    rep = scorecard.run()
    bar = rep["bar"]
    assert [r["icao"] for r in bar["pass"]] == ["KGOOD"]
    assert [r["icao"] for r in bar["fail"]] == ["KBAD"]
    assert bar["insufficient_n"] == 1
    good = bar["pass"][0]
    assert good["n"] >= 3000 and good["events_v10"] >= 10
    assert good["skill_v10"] > 0 and good["skill_sub"] > 0
    assert bar["fail"][0]["skill_v10"] < 0


# Known discrepancy, found while writing these tests: the published bar rule
# (score_shadow.py's comment and the methodology note on every airport page)
# asks for ">= 10 observed event-hours", but per_apt[...]["events"] adds one
# for every verified *pair* whose outcome was fog. One foggy hour is verified
# by up to 48 leads, so 3 foggy hours count as ~48 "events" and clear the
# floor. Changing the count changes which airports pass a pre-registered
# rule, so it is left for the owner to decide.
@pytest.mark.xfail(strict=True, reason="bar floor counts fog pairs, not distinct fog hours")
def test_bar_check_needs_ten_distinct_fog_hours(scorecard):
    scorecard.climo(full_climo(["KDRY"], 0.1))
    for c in range(150):
        t = T0 + timedelta(hours=c)
        fhrs = list(range(1, 25))
        scorecard.log(t, forecasts={"KDRY": {"fhrs": fhrs, "p": [[1] * 4] * 24}},
                      obs={"KDRY": ob(t, int(c in (0, 50, 100)))})
    rep = scorecard.run()
    assert rep["bar"]["pass"] == [] and rep["bar"]["insufficient_n"] == 1
