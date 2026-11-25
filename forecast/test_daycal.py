#!/usr/bin/env python3
"""Tests for the DEPLOY planner's day-level chance on days 1-2 (the "cal" tier).

Run: python3 forecast/test_daycal.py   (also collected by pytest)

Why it exists: until 2026-11 build_deploy.py published, as the chance of at
least one below-CAT-I hour tomorrow, 1 - prod(1 - p_hour) — consecutive fog
hours treated as independent coin flips. Scored on the 2023-25 hindcast that
number ran about 1.7x high (shown 0.5-0.6 -> 29% observed) and it alone set the
planner's GO word. It now goes through a map fitted on hindcasts
(forecast/out/daycal.json, written by forecast/fit_daycal.py). These tests pin
the map's shape, how it is applied, and that the build refuses to run without
it. test_bare_product_of_half_is_published_well_below_half is the one that
fails against the old behaviour.

Standard library only: build_deploy.py imports duckdb inside main() for this.
"""
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_deploy as bd  # noqa: E402

KEYS = ["d1"] + [bd.daymap_key(2, c) for c in bd.DAY2_COVER]


def _issuance(cycle: datetime, hourly: dict, icao: str = "KVIS") -> dict:
    """A current.json with one airport. hourly = {fhr: below-CAT-I percent}."""
    fhrs = sorted(hourly)
    return {"meta": {"cycle": cycle.strftime("%Y-%m-%dT%H:00:00Z")},
            "airports": {icao: {"fhrs": fhrs, "p": [[0, 0, 0, hourly[f]] for f in fhrs]}}}


def _full_cycle(cycle: datetime, by_valid: dict | None = None) -> dict:
    """Forecast hours as the engine merges them: NBH hourly for 1-25, NBS at
    the 3-hourly synoptic times beyond. by_valid = {(day_offset, utc_hour): pct}."""
    by_valid = by_valid or {}
    out = {}
    for f in range(1, 49):
        v = cycle + timedelta(hours=f)
        if f > 25 and v.hour % 3:
            continue
        out[f] = by_valid.get(((v.date() - cycle.date()).days, v.hour), 0)
    return out


def _grp(n=0, shown=None, obs=None, dates=0) -> dict:
    return {"n": n, "shown": shown, "obs": obs, "dates": dates}


def _receipt(sample: str, ok: bool = True, **over) -> dict:
    out = {"sample": sample, "n": 1000, "cycles": 30, "skill_pct": 5.0 if ok else -1.0,
           "ge50": _grp(120, 0.60, 0.55, 20), "b25_50": _grp(400, 0.34, 0.33, 28), "ge75": _grp(),
           "nov_feb": {"n": 500, "ge50": _grp(80, 0.60, 0.56, 12)},
           "rule_a_skill_over_0": ok, "rule_b_ge50_within_10": True, "pass": ok}
    out.update(over)
    return out


def _flat(v: float, key: str = "d1", ship: bool = True) -> dict:
    """A constant map in the committed file's full shape: published from every
    cycle hour that uses it, with the 12Z holdout as the check behind each."""
    hours = bd.map_cycle_hours(key) if ship else []
    return {"ship": ship, "x": [0.0, 1.0], "y": [v, v],
            "publish_hours": hours, "receipt_for_hour": {str(h): "12" for h in hours},
            "hold_months": [], "hours": {}, "areas": [],
            "holdout": _receipt("synthetic 12Z holdout", ok=ship, top=v, raw_050_maps_to=v, raw_for_50=None)}


def _daycal(**over) -> dict:
    """A synthetic file: every map a different constant, so the map that was used shows in p."""
    maps = {k: _flat(v, k) for k, v in (("d1", 0.01), ("d2h00", 0.20), ("d2h03", 0.23), ("d2h06", 0.26),
                                         ("d2h09", 0.29), ("d2h12", 0.32))}
    maps.update(over)
    return {"version": 2, "maps": maps}


def _published_day1(current: dict, today: date) -> float:
    """What the build publishes as tomorrow's chance, with the committed map."""
    days, _ = bd.cal_tier(current, today, bd.load_daycal())
    return days[("KVIS", 1)]["p"]


# ---- the committed map --------------------------------------------------------
def test_committed_maps_rise_and_stay_inside_0_1():
    dc = bd.load_daycal()
    for key in KEYS:
        m = dc["maps"][key]
        x, y = m["x"], m["y"]
        assert len(x) == len(y) >= 2, key
        assert all(a < b for a, b in zip(x, x[1:])), f"{key}: x must rise strictly"
        assert all(a <= b for a, b in zip(y, y[1:])), f"{key}: y must never fall"
        assert all(0.0 <= v <= 1.0 for v in x + y), key
        # and so does everything read off it, on a fine grid and past both ends
        out = [bd.apply_daymap(m, i / 1000.0) for i in range(-50, 1051)]
        assert all(a <= b + 1e-12 for a, b in zip(out, out[1:])), f"{key}: mapped values fall somewhere"
        assert all(0.0 <= v <= 1.0 for v in out), key


def test_day1_map_shipped_with_its_receipts():
    m = bd.load_daycal()["maps"]["d1"]
    h = m["holdout"]
    assert m["ship"] is True
    assert h["skill_pct"] > 0, "the shipped day-1 map must beat climatology on the holdout"
    assert h["ge50"]["n"] < 50 or abs(h["ge50"]["shown"] - h["ge50"]["obs"]) <= 0.10


def test_every_published_cycle_hour_has_a_passing_check_behind_it():
    """The committed file, read the way the build reads it: no map is used from
    a cycle hour unless the check named for that hour passed the ship rule."""
    dc = bd.load_daycal()
    for key in KEYS:
        m = dc["maps"][key]
        for h in m["publish_hours"]:
            src = m["receipt_for_hour"][str(h)]
            s = m["holdout"] if src == "12" else m["hours"][src]
            assert s["skill_pct"] > 0, (key, h, src)
            g = s["ge50"]
            assert g["n"] < 50 or abs(g["shown"] - g["obs"]) <= 0.10, (key, h, src, g)
    d1 = dc["maps"]["d1"]
    assert 9 in d1["publish_hours"], "the daily bake runs the 09Z cycle: day 1 must be published from it"
    # the 00Z sample is on file and failed (15 points high at 50% or more):
    # that failure, not a constant, is what keeps the evening hours out
    assert d1["hours"]["00"]["pass"] is False and 0 not in d1["publish_hours"]


def test_the_daily_bake_publishes_day_1_and_no_evening_redeploy_does():
    """With the committed map. 09Z is the bake. A redeploy on a US evening runs
    a 00Z-08Z cycle: tomorrow (UTC) is then leads 24-47 h, nearly all
    three-hourly, and scored on real 00Z cycles the map ran 15 points high."""
    dc = bd.load_daycal()
    for hour, want in ((9, True), (12, True), (21, True), (0, False), (2, False), (22, False)):
        cyc = datetime(2026, 12, 8, hour, tzinfo=timezone.utc)
        current = _issuance(cyc, _full_cycle(cyc, {(1, h): 11 for h in range(4, 10)}))
        days, info = bd.cal_tier(current, cyc.date(), dc)
        d = days[("KVIS", 1)]
        assert d["cal"] is True, hour                           # the tier is unchanged either way
        assert (d["p"] is not None) is want, (hour, d)
        assert info["applied"]["1"] is want and info["why"]["1"] == (None if want else "hour"), (hour, info)
        assert d["praw"] is not None                            # still logged for re-scoring
    # a cycle from yesterday's UTC date (a 23Z cycle picked up after midnight) is not a checked reach either
    cyc = datetime(2026, 12, 8, 21, tzinfo=timezone.utc)
    days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc)), date(2026, 12, 9), dc)
    assert days[("KVIS", 1)]["p"] is None and info["why"]["1"] == "hour"


def test_day_2_is_not_calibrated_on_the_daily_bake():
    """Real 09Z cycles of 2025: day 2 shown at 58% verified at 46%. The 09Z
    bake therefore publishes day 2 on the climatology tiers."""
    dc = bd.load_daycal()
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, {(2, 6): 40})), cyc.date(), dc)
    assert days[("KVIS", 2)]["cal"] is False and days[("KVIS", 2)]["p"] is None
    assert info["applied"] == {"1": True, "2": False} and info["why"]["2"] == "hour"


def test_bare_product_of_half_is_published_well_below_half():
    """The regression. Six consecutive hours at 11% multiply out to 0.503: the
    old build published that, and the app said GO. The hindcast says such a
    day fogs roughly one time in four."""
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    hours = _full_cycle(cyc, {(1, h): 11 for h in range(4, 10)})      # 04Z-09Z tomorrow
    current = _issuance(cyc, hours)
    st = bd.cal_day_stats(sorted(hours), [hours[f] for f in sorted(hours)], cyc, cyc.date(), 9)
    assert abs((1.0 - st[1]["noev"]) - 0.503) < 0.001, "fixture: the bare product should be 0.503"
    p = _published_day1(current, cyc.date())
    assert p is not None and 0.11 <= p <= 0.40, f"a bare product of 0.50 was published as {p}"
    assert bd.apply_daymap(bd.load_daycal()["maps"]["d1"], 0.50) <= 0.40


# ---- applying a map -----------------------------------------------------------
def test_interpolation_at_between_and_beyond_breakpoints():
    m = {"x": [0.0, 0.2, 0.6, 0.9], "y": [0.01, 0.05, 0.25, 0.70]}
    for x, y in zip(m["x"], m["y"]):
        assert abs(bd.apply_daymap(m, x) - y) < 1e-12, (x, y)
    assert abs(bd.apply_daymap(m, 0.1) - 0.03) < 1e-12        # halfway 0.01 -> 0.05
    assert abs(bd.apply_daymap(m, 0.4) - 0.15) < 1e-12        # halfway 0.05 -> 0.25
    assert abs(bd.apply_daymap(m, 0.3) - 0.10) < 1e-12        # a quarter of the way
    assert abs(bd.apply_daymap(m, 0.8) - 0.55) < 1e-12        # two thirds 0.25 -> 0.70
    assert bd.apply_daymap(m, -0.2) == 0.01                    # held flat below the first point
    assert bd.apply_daymap(m, 0.95) == 0.70 and bd.apply_daymap(m, 1.0) == 0.70   # and above the last


def test_day_chance_is_never_below_the_peak_hour():
    """p and pk are printed side by side; a 60% hour inside a '33% day' would argue."""
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    current = _issuance(cyc, _full_cycle(cyc, {(1, 6): 60}))
    days, _ = bd.cal_tier(current, cyc.date(), _daycal())      # day-1 map says 0.01 for everything
    d = days[("KVIS", 1)]
    assert d["pk"] == 60 and d["p"] == 0.6 and abs(d["praw"] - 0.6) < 1e-9


def test_eh_win_ws_are_built_exactly_as_before():
    """The map changes p only. This is the pre-daycal loop of build_deploy.main(), verbatim."""
    cyc = datetime(2026, 12, 8, 17, tzinfo=timezone.utc)
    today = cyc.date()
    hours = _full_cycle(cyc, {(1, 3): 8, (1, 4): 22, (1, 5): 41, (1, 6): 55, (1, 7): 30, (1, 21): 16,
                              (2, 0): 5, (2, 3): 12, (2, 6): 35, (2, 9): 48, (2, 12): 20, (2, 15): 44})
    f = {"fhrs": sorted(hours), "p": [[0, 0, 0, hours[k]] for k in sorted(hours)]}
    cal_day, cal_noev, cal_win, cal_ws = {}, {}, {}, {}
    peak = max((r[3] for r in f["p"]), default=0)
    for fhr, p in zip(f["fhrs"], f["p"]):
        dt = cyc + timedelta(hours=fhr)
        dd = (dt.date() - today).days
        if 1 <= dd <= 2:
            step = 1 if fhr <= 25 else 3
            cal_day[dd] = cal_day.get(dd, 0.0) + step * p[3] / 100.0
            cal_noev[dd] = cal_noev.get(dd, 1.0) * (1.0 - p[3] / 100.0) ** step
            if p[3] >= max(15, peak * 0.4):
                cal_win[dd] = cal_win.get(dd, 0) + step
                cal_ws.setdefault(dd, fhr)
    new = bd.cal_day_stats(f["fhrs"], [r[3] for r in f["p"]], cyc, today, 23)   # 23 = every day-2 hour
    assert set(new) == {1, 2}
    for dd in (1, 2):
        assert new[dd]["eh"] == cal_day[dd] and new[dd]["noev"] == cal_noev[dd], dd
        assert new[dd]["win"] == cal_win[dd] and new[dd]["ws"] == cal_ws[dd], dd
    # the production cover (12Z for a 17Z cycle) changes the day-2 statistic and nothing else
    cut = bd.cal_day_stats(f["fhrs"], [r[3] for r in f["p"]], cyc, today, bd.day2_cover_hour(cyc.hour))
    assert cut[1] == new[1]
    assert {k: cut[2][k] for k in ("eh", "win", "ws")} == {k: new[2][k] for k in ("eh", "win", "ws")}
    assert cut[2]["pk"] == 48 and new[2]["pk"] == 48 and cut[2]["used"] == 15 and new[2]["used"] == 18
    assert cut[2]["noev"] > new[2]["noev"]                     # the 15Z hour is not in the statistic


# ---- day 2: which map, and when there is none ----------------------------------
def test_day2_cover_hour_for_each_cycle_hour():
    want = {0: 0, 9: 9, 12: 12, 17: 12, 23: 12,               # the five that matter: midnight, the bake, the fit, a manual redeploy
            1: 0, 2: 0, 3: 3, 5: 3, 6: 6, 8: 6, 10: 9, 11: 9, 13: 12}
    for h, c in want.items():
        assert bd.day2_cover_hour(h) == c, (h, bd.day2_cover_hour(h), c)
        assert bd.daymap_key(2, bd.day2_cover_hour(h)) == f"d2h{c:02d}"
    assert bd.daymap_key(1, 9) == "d1"


def test_day2_uses_the_map_for_the_hours_the_cycle_covers():
    dc = _daycal()
    for h, want_p, used in ((0, 0.20, 3), (9, 0.29, 12), (12, 0.32, 15), (17, 0.32, 15)):
        cyc = datetime(2026, 12, 8, h, tzinfo=timezone.utc)
        days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc)), cyc.date(), dc)
        assert info["cover2"] == bd.day2_cover_hour(h) and info["applied"] == {"1": True, "2": True}, h
        assert days[("KVIS", 2)]["cal"] and days[("KVIS", 2)]["p"] == want_p, (h, days[("KVIS", 2)])
        assert days[("KVIS", 1)]["p"] == 0.01
    # a 23Z cycle is picked up after midnight: `today` has moved on, the 48 h
    # no longer reach day 2 at all, and tomorrow is leads 25-46 h — a reach no
    # check covers, so day 1 keeps its tier but gets no day-level number
    cyc = datetime(2026, 12, 8, 23, tzinfo=timezone.utc)
    days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc)), date(2026, 12, 9), dc)
    assert ("KVIS", 2) not in days and info["applied"] == {"1": False, "2": False}
    assert days[("KVIS", 1)]["cal"] and days[("KVIS", 1)]["p"] is None and info["why"]["1"] == "hour"


def test_hours_after_the_cover_stay_out_of_the_day2_chance():
    """A 17Z cycle reaches 15Z of day 2, but no hindcast map has seen past 12Z."""
    cyc = datetime(2026, 12, 8, 17, tzinfo=timezone.utc)
    days, _ = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, {(2, 15): 90, (2, 6): 10})), cyc.date(), _daycal())
    d = days[("KVIS", 2)]
    assert d["pk"] == 10 and d["p"] == 0.32                    # not floored to 0.90
    assert abs(d["eh"] - (0.3 + 2.7)) < 1e-9                   # eh still counts every forecast hour


def test_day2_is_not_cal_when_its_map_did_not_ship():
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    dc = _daycal(d2h09=_flat(0.29, "d2h09", ship=False))
    days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, {(2, 6): 40})), cyc.date(), dc)
    d2 = days[("KVIS", 2)]
    assert d2["cal"] is False and d2["p"] is None              # the caller drops it to the lower tiers
    assert d2["praw"] is not None                              # still logged, to re-score the map later
    assert info["applied"] == {"1": True, "2": False} and info["key2"] == "d2h09"
    assert info["why"] == {"1": None, "2": "held"}
    assert days[("KVIS", 1)]["cal"] is True


def test_a_map_is_only_used_from_the_cycle_hours_it_was_checked_on():
    """Synthetic file: day 1 checked for 09Z-21Z, day 2 (to 12Z) for 12Z only."""
    d1 = dict(_flat(0.01), publish_hours=list(range(9, 22)))
    d2 = dict(_flat(0.32, "d2h12"), publish_hours=[12])
    dc = _daycal(d1=d1, d2h12=d2)
    for hour, want1, want2 in ((8, False, True), (9, True, True), (12, True, True), (13, True, False),
                               (21, True, False), (22, False, False)):
        cyc = datetime(2026, 6, 8, hour, tzinfo=timezone.utc)
        days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc)), cyc.date(), dc)
        assert (days[("KVIS", 1)]["p"] is not None) is want1, hour
        assert days[("KVIS", 1)]["cal"] is True
        assert days[("KVIS", 2)]["cal"] is want2, hour          # 8Z and 9Z use d2h06 / d2h09, published in this file
        assert info["why"]["1"] == (None if want1 else "hour") and info["why"]["2"] == (None if want2 else "hour")


def test_a_map_is_not_used_in_the_months_it_missed():
    """Day 2 at 50% or more verified at 46% in Nov-Feb 2025: the committed
    day-2 map carries those months as held, and a 12Z build in December
    therefore leaves day 2 to the climatology tiers."""
    dc = _daycal(d2h12=dict(_flat(0.32, "d2h12"), hold_months=[11, 12, 1, 2]))
    for when, want in ((datetime(2026, 12, 8, 12, tzinfo=timezone.utc), False),
                       (datetime(2026, 6, 8, 12, tzinfo=timezone.utc), True),
                       (datetime(2026, 10, 30, 12, tzinfo=timezone.utc), False)):   # day 2 = 1 November
        days, info = bd.cal_tier(_issuance(when, _full_cycle(when)), when.date(), dc)
        assert days[("KVIS", 2)]["cal"] is want, when
        assert info["why"]["2"] == (None if want else "season")
        assert days[("KVIS", 1)]["p"] == 0.01                   # day 1 has its own months
    committed = bd.load_daycal()["maps"]["d2h12"]
    assert committed["hold_months"] == [11, 12, 1, 2] or not committed["publish_hours"]


def test_day2_is_not_cal_when_an_expected_hour_is_missing():
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    hours = _full_cycle(cyc)
    del hours[45]                                              # the 06Z point of day 2
    days, info = bd.cal_tier(_issuance(cyc, hours), cyc.date(), _daycal())
    assert days[("KVIS", 2)]["cal"] is False and info["applied"]["2"] is False


def test_half_a_tomorrow_gets_no_day_level_number():
    """No 26-48 h bulletin: eh is summed from what is there, the tier stays
    "cal" (the watchdog counts it), but no chance is claimed for the whole day."""
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    hours = {f: v for f, v in _full_cycle(cyc, {(1, 5): 30}).items() if f <= 25}
    days, info = bd.cal_tier(_issuance(cyc, hours), cyc.date(), _daycal())
    d = days[("KVIS", 1)]
    assert d["cal"] is True and d["p"] is None and d["pk"] == 30 and abs(d["eh"] - 0.3) < 1e-9
    # the map was ready and the bulletin was not: the build stops, so the
    # next hourly run tries the bake again
    assert info["applied"]["1"] is False and info["why"]["1"] == "hours"
    pub = list(range(9, 22))
    at = lambda h, day=8: datetime(2026, 12, day, h, 25, tzinfo=timezone.utc)
    what, said = bd.bake_decision(info, "2026-12-08T09:00:00Z", at(10), pub)
    assert what == "wait" and "2026-12-08T09:00:00Z" in said and "deploy.json was not written" in said


def test_what_the_build_does_at_each_hour():
    """write / wait / keep (bake_decision). A build that cannot carry a
    day-level chance never replaces a live payload that does."""
    pub = list(range(9, 22))
    dc = _daycal(d1=dict(_flat(0.01), publish_hours=pub))
    at = lambda h, day=8: datetime(2026, 12, day, h, 25, tzinfo=timezone.utc)

    def decide(cycle: datetime, now: datetime, daycal=dc):
        _, info = bd.cal_tier(_issuance(cycle, _full_cycle(cycle, {(1, 12): 30})), now.date(), daycal)
        return bd.bake_decision(info, cycle.strftime("%Y-%m-%dT%H:00:00Z"), now, daycal["maps"]["d1"]["publish_hours"])[0]

    cyc = lambda h, day=8: datetime(2026, 12, day, h, tzinfo=timezone.utc)
    assert decide(cyc(9), at(10)) == "write"                 # the daily bake
    assert decide(cyc(12), at(13)) == "write" and decide(cyc(21), at(22)) == "write"
    # a redeploy on a US evening: nothing a later run today would fix before
    # the bake is owed, so the morning payload is left alone
    assert decide(cyc(1), at(2)) == "keep" and decide(cyc(8), at(9)) == "keep"
    assert decide(cyc(22), at(23)) == "keep"                 # past the last checked hour: no later run today helps
    assert decide(cyc(23, day=7), at(0)) == "keep"           # yesterday's last cycle, picked up after midnight
    # the scheduled bake at 10:25Z finds the 09Z bulletin late and the engine
    # falls back to 08Z: fail, and let the next hourly run bake from 10Z
    assert decide(cyc(8), at(10)) == "wait" and decide(cyc(7), at(11)) == "wait"
    # a day-1 map that is held outright is a committed state: written, with no day-level chance
    held = _daycal(d1=_flat(0.01, "d1", ship=False))
    assert decide(cyc(9), at(10), held) == "write"
    assert bd.BAKE_OWED_FROM_UTC == 10


def test_day1_map_that_did_not_ship_publishes_no_number():
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    dc = _daycal(d1=_flat(0.01, "d1", ship=False))
    days, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, {(1, 5): 30})), cyc.date(), dc)
    assert days[("KVIS", 1)]["cal"] is True and days[("KVIS", 1)]["p"] is None
    assert info["applied"]["1"] is False and info["why"]["1"] == "held"


# ---- the payload ------------------------------------------------------------------
def _lower(icao, dd, dte):
    return 1.23, "climo", 0.111


def test_the_payload_carries_the_mapped_chance_never_the_bare_product():
    """The assembly itself. A review put the bare product back with one line in
    main() (pdays.append(c["praw"])) and every test passed, because none ran
    this step; the same for pk on every tier and for a day 2 kept "cal"
    without its map."""
    cyc = datetime(2026, 12, 8, 9, tzinfo=timezone.utc)
    hours = _full_cycle(cyc, {**{(1, h): 11 for h in range(4, 10)}, (2, 6): 40})
    current = _issuance(cyc, hours)
    cal, info = bd.cal_tier(current, cyc.date(), bd.load_daycal())
    a = bd.airport_entry("KVIS", cyc.date(), cal, {("KVIS", 12): 5}, _lower)
    assert set(a) == {"eh", "tiers", "p", "win", "ws", "pk"}, "praw (or anything else) must not ride in the payload"
    assert all(len(a[k]) == 14 for k in a)
    c1 = cal[("KVIS", 1)]
    assert abs(c1["praw"] - 0.503) < 0.001                      # the bare product is still computed, for the log
    assert a["tiers"][0] == "cal" and a["p"][0] == c1["p"] and a["p"][0] <= 0.40, a["p"][0]
    assert a["pk"][0] == 11 and a["eh"][0] == round(c1["eh"], 2)
    # day 2 is not "cal" on the 09Z bake: it takes the lower tier's numbers whole
    assert info["applied"]["2"] is False
    assert a["tiers"][1] == "climo" and a["p"][1] == 0.111 and a["eh"][1] == 1.23
    assert a["pk"][1] is None and a["ws"][1] is None and a["win"][1] == 5
    # and so does every later day; pk is only ever beside a "cal" day
    assert a["tiers"][2:] == ["climo"] * 12 and a["pk"][1:] == [None] * 13
    # with a map that covers day 2, day 2 is "cal" and carries its own mapped chance
    cal2, info2 = bd.cal_tier(current, cyc.date(), _daycal())
    b = bd.airport_entry("KVIS", cyc.date(), cal2, {}, _lower)
    assert info2["applied"]["2"] is True and b["tiers"][:3] == ["cal", "cal", "climo"]
    assert b["p"][1] == max(0.29, 0.40) and b["pk"][1] == 40    # the map's 0.29, floored at the 40% peak hour
    assert b["p"][1] != cal2[("KVIS", 2)]["praw"]


def test_no_day_level_chance_means_a_null_not_a_zero():
    cyc = datetime(2026, 12, 8, 2, tzinfo=timezone.utc)         # an evening redeploy: unchecked cycle hour
    cal, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, {(1, 12): 30})), cyc.date(), bd.load_daycal())
    a = bd.airport_entry("KVIS", cyc.date(), cal, {}, _lower)
    assert a["tiers"][0] == "cal" and a["p"][0] is None and a["pk"][0] == 30   # watchdog check 7 still counts the tier


# ---- the receipts on screen ---------------------------------------------------------
ATLAS = {"KVIS": {"st": "CA", "country": "US"}, "KSFO": {"st": "CA", "country": "US"},
         "KDEN": {"st": "CO", "country": "US"}, "CYVR": {"st": "BC", "country": "CA"},
         "KBOS": {"st": "MA", "country": "US"}}


def test_a_field_is_filed_under_the_first_area_that_lists_it():
    areas = [("Central Valley", {"icaos": ["KVIS"]}), ("West Coast", {"st": ["CA", "OR", "WA"]}),
             ("Canada", {"country": "CA"})]
    assert bd.area_of("KVIS", ATLAS["KVIS"], areas) == "Central Valley"
    assert bd.area_of("KSFO", ATLAS["KSFO"], areas) == "West Coast"
    assert bd.area_of("CYVR", ATLAS["CYVR"], areas) == "Canada"   # country "CA" is not the state "CA"
    assert bd.area_of("KDEN", ATLAS["KDEN"], areas) is None and bd.area_of("KZZZ", None, areas) is None


def test_a_group_is_only_judged_when_it_has_50_airport_days():
    """The ship rule's own floor and tolerance, used for every area group that goes on screen."""
    assert bd.band_ok({"n": 49, "shown": 0.65, "obs": 0.20}) is None      # too thin to say, however far off
    assert bd.band_ok({"n": 0, "shown": None, "obs": None}) is None and bd.band_ok(None) is None
    assert bd.band_ok({"n": 50, "shown": 0.65, "obs": 0.56}) is True      # 9 points
    assert bd.band_ok({"n": 50, "shown": 0.65, "obs": 0.54}) is False     # 11 points high
    assert bd.band_ok({"n": 300, "shown": 0.62, "obs": 0.78}) is False    # 16 points low is a miss too
    assert (bd.BAND_MIN_N, bd.BAND_MAX_GAP) == (50, 0.10)


def test_the_receipt_is_the_check_nearest_the_builds_own_cycle_hour():
    """A 09Z build used to print the 12Z holdout's numbers whatever cycle built it."""
    dc = bd.load_daycal()
    d1 = dc["maps"]["d1"]
    r9 = bd.day_receipt(dc, "d1", 9, ATLAS, list(ATLAS))
    r15 = bd.day_receipt(dc, "d1", 15, ATLAS, list(ATLAS))
    r2 = bd.day_receipt(dc, "d1", 2, ATLAS, list(ATLAS))
    assert r9["checked_from"] == 9 and r9["ge50"] == d1["hours"]["09"]["ge50"] and "09Z" in r9["sample"]
    assert r15["checked_from"] == 12 and r15["ge50"] == d1["holdout"]["ge50"] and "12Z" in r15["sample"]
    # not published from 02Z, and a day the build did not apply the map to:
    # no receipt at all goes out, so none can be printed beside the wrong number
    for r in (r2, bd.day_receipt(dc, "d1", 9, ATLAS, list(ATLAS), applied=False)):
        assert r["checked_from"] is None and "ge50" not in r and "areas" not in r and "skill_pct" not in r, r
    assert r9["ge50"]["dates"] < r9["ge50"]["n"], "the number of cycle dates rides beside the airport-days"
    for r in (r9, r15):
        assert r["ge75"]["n"] >= 50, "the top band is always backed by a group the rule could judge"
        assert "reliability" not in r and "x" not in r


def test_central_valley_fields_get_their_own_record_not_the_pooled_one():
    """The screen printed "58% of the time" beside KVIS. Central Valley fields
    shown at 50% or more fogged about a third of the time over three years."""
    dc = bd.load_daycal()
    r = bd.day_receipt(dc, "d1", 9, ATLAS, list(ATLAS))
    by = {a["name"]: a for a in r["areas"]}
    cv = by["Central Valley"]
    assert "KVIS" in cv["icaos"] and "KSFO" not in cv["icaos"]
    assert cv["ok"] is False and cv["high"] is True
    assert cv["ge50"]["n"] >= 50 and cv["ge50"]["obs"] < 0.45 < cv["ge50"]["shown"]
    assert cv["ge50"] != r["ge50"] and cv["ge50"]["ok"] is False
    assert "KSFO" in by["West Coast"]["icaos"] and "CYVR" in by["Canada"]["icaos"]
    # every listed area was judged on a group the rule could judge, and says which years
    for a in r["areas"]:
        assert a["ge50"]["n"] >= 50 and a["years"] and isinstance(a["ok"], bool), a["name"]
        assert a["ok"] == (abs(a["ge50"]["shown"] - a["ge50"]["obs"]) <= 0.10), a["name"]
    listed = [i for a in r["areas"] for i in a["icaos"]]
    assert len(listed) == len(set(listed)), "a field belongs to one area"
    assert "KDEN" not in listed                                  # Mountain West: too few fog days to judge


# ---- the file is required -------------------------------------------------------
def _raises_naming(path: Path) -> str:
    try:
        bd.load_daycal(path)
    except RuntimeError as e:
        assert path.name in str(e), f"the error must name the file: {e}"
        return str(e)
    raise AssertionError(f"load_daycal accepted {path}")


def test_missing_daycal_raises_and_names_the_file():
    with tempfile.TemporaryDirectory() as d:
        msg = _raises_naming(Path(d) / "daycal.json")
        assert "not found" in msg


def test_hand_edits_of_the_committed_file_are_refused():
    """The review's three edits, made to a copy of the real file."""
    real = json.loads(bd.DAYCAL.read_text())

    def edited(edit):
        dc = json.loads(json.dumps(real))
        edit(dc["maps"])
        return json.dumps(dc)

    cases = {
        "d2h09 carrying d2h12's breakpoints": edited(lambda m: m["d2h09"].update(x=m["d2h12"]["x"], y=m["d2h12"]["y"])),
        "the day-1 map inflated 1.4x": edited(lambda m: m["d1"].update(y=[round(min(v * 1.4, 1.0), 4) for v in m["d1"]["y"]])),
        "the held d2h00 map flipped to ship": edited(lambda m: m["d2h00"].update(ship=True)),
        "day 1 published from every cycle hour again": edited(lambda m: m["d1"].update(
            publish_hours=list(range(24)), receipt_for_hour={str(h): "00" for h in range(24)})),
    }
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "daycal.json"
        p.write_text(json.dumps(real))
        assert bd.load_daycal(p)["version"] == 2, "an unedited copy must load"
        for name, text in cases.items():
            p.write_text(text)
            try:
                _raises_naming(p)
            except AssertionError as e:
                raise AssertionError(f"{name}: {e}") from None


def test_malformed_daycal_raises_and_names_the_file():
    good = _daycal(d2h00=_flat(0.20, "d2h00", ship=False))
    assert bd.load_daycal.__defaults__[0].name == "daycal.json"

    def broken(edit):
        dc = json.loads(json.dumps(good))
        edit(dc)
        return json.dumps(dc)

    cases = {
        "not json": "{\"version\": 1, \"maps\": ",
        "empty": "",
        "a list": "[]",
        "wrong version": broken(lambda dc: dc.update(version=1)),   # the first run's shape: no cycle-hour gate in it
        "no maps": broken(lambda dc: dc.pop("maps")),
        "a day-2 map missing": broken(lambda dc: dc["maps"].pop("d2h09")),
        "ship is not true/false": broken(lambda dc: dc["maps"]["d1"].update(ship="yes")),
        "y falls": broken(lambda dc: dc["maps"]["d1"].update(x=[0.0, 0.5, 1.0], y=[0.1, 0.3, 0.2])),
        "x does not rise": broken(lambda dc: dc["maps"]["d1"].update(x=[0.0, 0.5, 0.5], y=[0.1, 0.2, 0.3])),
        "y above 1": broken(lambda dc: dc["maps"]["d1"].update(x=[0.0, 1.0], y=[0.1, 1.2])),
        "lengths differ": broken(lambda dc: dc["maps"]["d1"].update(x=[0.0, 0.5, 1.0], y=[0.1, 0.2])),
        "one point": broken(lambda dc: dc["maps"]["d1"].update(x=[0.5], y=[0.2])),
        "NaN": broken(lambda dc: dc["maps"]["d1"].update(x=[0.0, 1.0], y=[0.1, float("nan")])),
        "strings": broken(lambda dc: dc["maps"]["d1"].update(x=["0", "1"], y=["0.1", "0.2"])),
        # the three hand edits the 2026-11-24 review made that the first loader accepted
        "one map carrying another's breakpoints": broken(
            lambda dc: dc["maps"]["d2h09"].update(x=dc["maps"]["d2h12"]["x"], y=dc["maps"]["d2h12"]["y"])),
        "a map inflated 1.4x": broken(lambda dc: dc["maps"]["d1"].update(y=[0.014, 0.014])),
        "a held map flipped to ship": broken(lambda dc: dc["maps"]["d2h00"].update(ship=True)),
        # and the gate this file now carries
        "no holdout receipt": broken(lambda dc: dc["maps"]["d1"].pop("holdout")),
        "no publish_hours": broken(lambda dc: dc["maps"]["d1"].pop("publish_hours")),
        "no hold_months": broken(lambda dc: dc["maps"]["d1"].pop("hold_months")),
        "no areas": broken(lambda dc: dc["maps"]["d1"].pop("areas")),
        "a held map with hours to publish from": broken(lambda dc: dc["maps"]["d2h00"].update(publish_hours=[0])),
        "an hour with no check named": broken(lambda dc: dc["maps"]["d1"].update(receipt_for_hour={})),
        "an hour whose check is not on file": broken(
            lambda dc: dc["maps"]["d1"].update(receipt_for_hour={str(h): "09" for h in range(24)})),
        "an hour whose check failed": broken(lambda dc: dc["maps"]["d1"].update(
            hours={"00": _receipt("synthetic 00Z", ok=False)}, receipt_for_hour={str(h): "00" for h in range(24)})),
        "an hour another map uses": broken(lambda dc: dc["maps"]["d2h09"].update(
            publish_hours=[9, 14], receipt_for_hour={"9": "12", "14": "12"})),
    }
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "daycal.json"
        p.write_text(json.dumps(good))
        assert bd.load_daycal(p)["version"] == 2, "the fixture itself must load"
        for name, text in cases.items():
            p.write_text(text)
            try:
                _raises_naming(p)
            except AssertionError as e:
                raise AssertionError(f"{name}: {e}") from None


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", name, "-", repr(e))
    print(f"{'FAILED' if fails else 'passed'}")
    sys.exit(1 if fails else 0)
