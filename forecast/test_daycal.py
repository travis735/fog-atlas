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
fails against that old behaviour.

Since 2026-11-25 the planner is rebuilt every hour and its days are counted
from a service day that flips at 10:00Z, not from the UTC date: a build at
02Z on the 25th used to call the 26th "day 1", and the morning the pilot was
planning for was in neither day. What a cycle says about a date depends on
the cycle hour and on how many dates ahead it is, so maps and checks are
keyed by that (daycal.json version 3), with an area's own map where one
passed. test_the_evening_build_plans_for_the_coming_morning and
test_an_upcoming_fog_morning_stays_in_days_1_2_until_10z_of_its_date are the
ones that fail against counting from the UTC date.

Standard library only: build_deploy.py imports duckdb inside main() for this.
"""
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_deploy as bd  # noqa: E402

UTC = timezone.utc
POOLED = ("d1", "d1u", "d2f", "d2h09", "d2h12")
# a different constant per map, so the map that was used shows in p
VALUES = {"d1": 0.01, "d1u": 0.05, "d2f": 0.20, "d2h09": 0.29, "d2h12": 0.32}
AREAS = [{"name": "Central Valley", "icaos": ["KVIS"]}, {"name": "West Coast", "st": ["CA", "OR", "WA"]},
         {"name": "Canada", "country": "CA"}]
ATLAS = {"KVIS": {"st": "CA", "country": "US"}, "KSFO": {"st": "CA", "country": "US"},
         "KDEN": {"st": "CO", "country": "US"}, "CYVR": {"st": "BC", "country": "CA"},
         "KBOS": {"st": "MA", "country": "US"}}


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _build(cycle: datetime) -> datetime:
    """When the hourly job runs a cycle: the tick at h:25Z picks up cycle h-1."""
    return cycle + timedelta(hours=1, minutes=25)


def _issuance(cycle: datetime, hourly: dict, icao: str = "KVIS") -> dict:
    """A current.json with one airport. hourly = {fhr: below-CAT-I percent}."""
    fhrs = sorted(hourly)
    return {"meta": {"cycle": cycle.strftime("%Y-%m-%dT%H:00:00Z")},
            "airports": {icao: {"fhrs": fhrs, "p": [[0, 0, 0, hourly[f]] for f in fhrs]}}}


def _full_cycle(cycle: datetime, by_valid: dict | None = None) -> dict:
    """Forecast hours as the engine merges them: NBH hourly for 1-25, NBS at
    the 3-hourly synoptic times beyond. by_valid = {(day_offset, utc_hour): pct},
    day_offset counted from the cycle's own date."""
    by_valid = by_valid or {}
    out = {}
    for f in range(1, 49):
        v = cycle + timedelta(hours=f)
        if f > 25 and v.hour % 3:
            continue
        out[f] = by_valid.get(((v.date() - cycle.date()).days, v.hour), 0)
    return out


def _tier(cycle: datetime, by_valid: dict | None = None, dc: dict | None = None, now: datetime | None = None,
          atlas: dict | None = None, icao: str = "KVIS"):
    """cal_tier for one airport, built when the hourly job would build it."""
    sd = bd.service_day(now or _build(cycle))
    return bd.cal_tier(_issuance(cycle, _full_cycle(cycle, by_valid), icao), sd, dc if dc else _daycal(), atlas)


def _grp(n=0, shown=None, obs=None, dates=0) -> dict:
    return {"n": n, "shown": shown, "obs": obs, "dates": dates}


def _receipt(sample: str, key: str, v: float, ok: bool = True, **over) -> dict:
    out = {"sample": sample, "n": 1000, "cycles": 30, "skill_pct": 5.0 if ok else -1.0,
           "ge50": _grp(120, 0.60, 0.55, 20), "b25_50": _grp(400, 0.34, 0.33, 28), "ge75": _grp(),
           "nov_feb": {"n": 500, "ge50": _grp(80, 0.60, 0.56, 12)},
           "rule_a_skill_over_0": ok, "rule_b_ge50_within_10": True, "pass": ok,
           "map": key, "map_top": v, "map_050": v}
    out.update(over)
    return out


def _map(v: float, floor: bool = True, **extra) -> dict:
    return {"x": [0.0, 1.0], "y": [v, v], "floor": floor, "top": v, "raw_050_maps_to": v, "raw_for_50": None, **extra}


def _slot_map(sk: str) -> str:
    """The pooled map each (bucket, j) uses, written out from fit_daycal.py's
    pre-registration rather than read back from build_deploy."""
    rep, j = int(sk[1:3]), int(sk[-1])
    if j == 0:
        return "d1u"
    if j == 1:
        return "d1" if rep >= 9 else "d2f"
    return {9: "d2h09", 12: "d2h12"}.get(rep, "d2f")


def _daycal(held=(), hold: dict | None = None) -> dict:
    """A synthetic version-3 file: every slot published with its pooled map,
    except those named in `held` (their check failed). hold = {slot: months}.
    Every slot lists every area, on the pooled map, as the fitter writes them."""
    pooled = {a["name"]: {"use": "pooled", "map": None, "why": None, "hold_months": [], "check": None, "own": None}
              for a in AREAS}
    slots = {}
    for sk in bd.SLOT_KEYS:
        key = _slot_map(sk)
        ok = sk not in held
        c = _receipt(f"synthetic {sk[1:3]}Z sample", key, VALUES[key], ok=ok)
        if bd.slot_kind(int(sk[1:3]), int(sk[-1])) == "under_way":
            # the same chance against fog after the build is on screen (fit_daycal.after_build)
            c["after_build"] = {"n": 990, "ge50": _grp(120, 0.60, 0.52, 20), "b25_50": _grp(400, 0.34, 0.31, 28),
                                "ge75": _grp(), "ge50_within_10": True}
        slots[sk] = {"kind": bd.slot_kind(int(sk[1:3]), int(sk[-1])), "rep": int(sk[1:3]), "j": int(sk[-1]),
                     "map": key if ok else None, "check": c if ok else None, "tried": [c],
                     "why": None if ok else "failed", "hold_months": list((hold or {}).get(sk, [])),
                     "areas": json.loads(json.dumps(pooled))}
    return {"version": 3, "service_day_offset_h": 10, "areas": json.loads(json.dumps(AREAS)),
            "maps": {k: _map(v, home="h12/j1") for k, v in VALUES.items()}, "slots": slots, "regional": {}}


def _family(dc: dict, fam: str) -> dict:
    """A family judged by area, as the fitter writes it: a record for every area, each with a flag
    (here one with no judged sample, which says nothing on screen)."""
    return dc["regional"].setdefault(fam, {a["name"]: {"pass": False, "form": None, "years": "2023-25", "forms": {},
                                                       "flag": {"judged": [], "samples": {}}} for a in AREAS})


def _area_map(dc: dict, sk: str, area: str, v: float, floor: bool = False, hold=()) -> dict:
    """Give `area` its own map in slot sk, the way fit_daycal.py records one that passed."""
    fam = dc["slots"][sk]["map"]
    rk, form = f"{fam}@{area}", "floored" if floor else "unfloored"
    dc["maps"][rk] = _map(v, floor=floor, family=fam, area=area)
    dc["slots"][sk]["areas"][area] = {"use": "regional", "map": rk, "why": None, "hold_months": list(hold),
                                      "check": _receipt(f"synthetic {sk[1:3]}Z sample, {area}", rk, v), "own": None}
    oof = {"n": 2000, "skill_pct": 8.0, "ge50": _grp(), "b25_50": _grp(), "ge75": _grp()}
    _family(dc, fam)[area] = {
        "pass": True, "form": form, "years": "2023-25", "flag": {"judged": [], "samples": {}},
        "forms": {form: {"pass": True, "oof": oof, "hold_months": list(hold),
                         "rules": {"a_skill_over_0": True, "b_ge50_within_10": None, "c_beats_pooled": True,
                                   "d_b25_50_within_10": None, "e_ge50_empty_or_50": True,
                                   "f_unfloored_only_where_pooled_ran_high": None if floor else True}}},
        "by_peak_hour": {"b25_50": _grp(90, 0.35, 0.30, 20), "ge50": _grp(56, 0.63, 0.39, 15), "ge75": _grp()}}
    return dc


def _flag(dc: dict, fam: str, area: str, shown: float, obs: float, n: int = 296) -> dict:
    """An area that stays on the pooled map, with the record of how the pooled map did there."""
    g = _grp(n, shown, obs, 87)
    rec = _family(dc, fam)[area]
    rec["flag"] = {"name": area, "ok": abs(shown - obs) <= 0.10, "high": shown > obs, "show": "2023-25",
                   "judged": ["2023-25"],
                   "samples": {"2023-25": {"ge50": g, "b25_50": _grp(1000, 0.35, 0.34, 135),
                                           "ge75": _grp(20, 0.78, 0.7, 9)}}}
    return dc


def _published_day1(current: dict, sd: date) -> float:
    """What the build publishes as day 1's chance, with the committed map (no area: the pooled map)."""
    days, _ = bd.cal_tier(current, sd, bd.load_daycal())
    return days[("KVIS", 1)]["p"]


# ---- the service day -----------------------------------------------------------
def test_service_day_flips_at_10z():
    """Days are counted from the UTC date of (build time - 10 h)."""
    sd = bd.service_day
    assert bd.SERVICE_DAY_OFFSET_H == 10
    assert sd(_utc(2026, 12, 8, 0, 0)) == date(2026, 12, 7)
    assert sd(_utc(2026, 12, 8, 9, 59)) == date(2026, 12, 7)
    assert sd(_utc(2026, 12, 8, 10, 0)) == date(2026, 12, 8)
    assert sd(_utc(2026, 12, 8, 23, 59)) == date(2026, 12, 8)
    # a month end, a year end, a leap day
    assert sd(_utc(2026, 12, 1, 9, 59)) == date(2026, 11, 30) and sd(_utc(2026, 12, 1, 10, 0)) == date(2026, 12, 1)
    assert sd(_utc(2027, 1, 1, 0, 0)) == date(2026, 12, 31) and sd(_utc(2027, 1, 1, 9, 59)) == date(2026, 12, 31)
    assert sd(_utc(2027, 1, 1, 10, 0)) == date(2027, 1, 1)
    assert sd(_utc(2028, 3, 1, 3, 0)) == date(2028, 2, 29)
    # the clock's zone does not move it: 03:00 Mountain time is 10:00Z, and a naive time is taken as UTC
    mst = timezone(timedelta(hours=-7))
    assert sd(datetime(2026, 12, 8, 2, 59, tzinfo=mst)) == date(2026, 12, 7)
    assert sd(datetime(2026, 12, 8, 3, 0, tzinfo=mst)) == date(2026, 12, 8)
    assert sd(datetime(2026, 12, 8, 9, 59)) == date(2026, 12, 7) and sd(datetime(2026, 12, 8, 10, 0)) == date(2026, 12, 8)


def test_an_upcoming_fog_morning_stays_in_days_1_2_until_10z_of_its_date():
    """Every minute of the two days before 10Z on the morning's own date: the
    morning is day 2, then day 1, and never neither. Counted from the UTC date
    (the old way) the morning dropped out at 00Z, ten hours early, which is
    every build of a US evening."""
    for morning in (date(2026, 12, 9), date(2026, 12, 1), date(2027, 1, 1), date(2028, 3, 1)):
        leaves = datetime(morning.year, morning.month, morning.day, 10, tzinfo=UTC)
        t = leaves - timedelta(days=2)
        while t < leaves:
            sd = bd.service_day(t)
            k = (morning - sd).days
            assert k in (1, 2), (morning, t, k)
            assert k == (1 if t >= leaves - timedelta(days=1) else 2), (morning, t, k)
            t += timedelta(minutes=1)
        assert (morning - bd.service_day(leaves)).days == 0                              # 10:00Z: it is the service day
        assert (morning - bd.service_day(leaves - timedelta(days=2, minutes=1))).days == 3  # not yet day 2


def test_the_evening_build_plans_for_the_coming_morning():
    """The regression for the service day. 02:25Z on 8 December, 01Z cycle, fog
    forecast for 12Z-15Z that same UTC date: that morning is day 1, judged on
    the hours still ahead. The old build counted from the UTC date, called
    the 9th day 1, and showed the pilot nothing about the morning he was
    deciding on."""
    cyc = _utc(2026, 12, 8, 1)
    fog = {(0, h): 40 for h in (12, 13, 14, 15)}
    days, info = _tier(cyc, fog)
    d1, d2 = info["days"]["1"], info["days"]["2"]
    assert info["service_day"] == "2026-12-07" and d1["date"] == "2026-12-08" and d2["date"] == "2026-12-09"
    assert d1["kind"] == "under_way" and d1["under_way"] is True and d1["cover"] == {"from": 2, "through": 23}
    a = days[("KVIS", 1)]
    assert abs(a["eh"] - 1.6) < 1e-9 and a["pk"] == 40 and a["cov"] == 23
    assert a["map"] == "d1u" and a["p"] == 0.4                    # the under-way map's 0.05, floored at the 40% hour
    assert days[("KVIS", 2)]["eh"] == 0.0 and days[("KVIS", 2)]["map"] == "d2f" and d2["kind"] == "evening"
    # counted from the UTC date, as before: day 1 is the 9th and the fog morning is nowhere
    old_days, old_info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, fog)), _build(cyc).date(), _daycal())
    assert old_info["days"]["1"]["date"] == "2026-12-09" and old_days[("KVIS", 1)]["eh"] == 0.0


# ---- what a cycle covers ---------------------------------------------------------
def test_covered_hours_of_a_day_under_way_never_include_a_past_hour():
    for h in range(24):
        cyc = _utc(2026, 12, 8, h)
        hours = bd.covered_hours(cyc, cyc.date())
        assert hours == list(range(h + 1, 24)), (h, hours)      # leads 1-23 are all hourly
        assert bd.expected_used(cyc, cyc.date()) == 23 - h
        # the day before is never forecast at all
        assert bd.covered_hours(cyc, cyc.date() - timedelta(days=1)) == []
    # and the statistic itself: an analysis-hour row (lead 0) or a past one never enters it
    cyc = _utc(2026, 12, 8, 6)
    hourly = _full_cycle(cyc, {(0, 12): 30})
    hourly.update({0: 99, -3: 99})
    fh = sorted(hourly)
    s = bd.day_stats(fh, [hourly[f] for f in fh], cyc, [cyc.date()])[cyc.date()]
    assert s["pk"] == 30 and s["used"] == 17 and abs((1 - s["noev"]) - 0.30) < 1e-9 and s["last"] == 23
    assert s["ws"] == 6                                         # the window starts at lead 6 (12Z), not at the bogus rows


def test_what_each_cycle_hour_covers():
    """NBH hourly through lead 25, NBS at the synoptic hours beyond, 48 h in all."""
    day = lambda h, j: (_utc(2026, 12, 8, h), date(2026, 12, 8) + timedelta(days=j))
    three = lambda last: list(range(0, last + 1, 3))
    want = {(9, 1): list(range(0, 11)) + [12, 15, 18, 21],      # the old daily bake's tomorrow
            (12, 1): list(range(0, 14)) + [15, 18, 21],
            (21, 1): list(range(0, 23)), (22, 1): list(range(0, 24)), (23, 1): list(range(0, 24)),
            (0, 1): [0, 1] + three(21)[1:], (8, 1): list(range(0, 10)) + [12, 15, 18, 21],
            (9, 2): three(9), (11, 2): three(9), (12, 2): three(12), (14, 2): three(12), (15, 2): three(15),
            (18, 2): three(18), (20, 2): three(18), (21, 2): three(21), (22, 2): three(21), (23, 2): three(21),
            (0, 2): [0], (8, 2): [0, 3, 6], (3, 0): list(range(4, 24)), (8, 0): list(range(9, 24)),
            (9, 3): [], (0, 3): []}
    for (h, j), hours in want.items():
        assert bd.covered_hours(*day(h, j)) == hours, (h, j, bd.covered_hours(*day(h, j)))
    used = {(9, 1): 23, (12, 1): 23, (21, 1): 23, (22, 1): 24, (23, 1): 24, (0, 1): 23, (8, 1): 22,
            (9, 2): 12, (12, 2): 15, (15, 2): 18, (18, 2): 21, (21, 2): 24, (23, 2): 22, (3, 0): 20, (8, 0): 15}
    for (h, j), n in used.items():
        assert bd.expected_used(*day(h, j)) == n, (h, j, bd.expected_used(*day(h, j)))
    # the three views of one thing agree for every cycle hour and reach: what
    # the build expects, what a whole issuance holds, what the statistic used
    for h in range(24):
        cyc = _utc(2026, 12, 8, h)
        hourly = _full_cycle(cyc)
        fh = sorted(hourly)
        targets = [cyc.date() + timedelta(days=j) for j in (0, 1, 2)]
        st = bd.day_stats(fh, [0] * len(fh), cyc, targets)
        for d in targets:
            hours = bd.covered_hours(cyc, d)
            assert (d in st) == bool(hours), (h, d)
            if hours:
                assert st[d]["used"] == bd.expected_used(cyc, d) and st[d]["last"] == hours[-1], (h, d, st[d])


def test_bucket_and_reach_for_every_cycle_hour():
    assert bd.BUCKETS == ["h00", "h03", "h06", "h09", "h12", "h15", "h18", "h21"]
    for h in range(24):
        assert bd.bucket_of(h) == f"h{h - h % 3:02d}"
        assert bd.slot_kind(h, 0) == ("under_way" if h <= 8 else None), h
        assert bd.slot_kind(h, 1) == ("evening" if h <= 8 else "ahead"), h
        assert bd.slot_kind(h, 2) == (None if h <= 8 else "partial" if h <= 14 else "after_next"), h
        assert bd.slot_kind(h, 3) is None and bd.slot_kind(h, -1) is None
        # the tick one hour after a cycle asks it for exactly two checked reaches
        cyc = _utc(2026, 12, 8, h)
        sd = bd.service_day(_build(cyc))
        js = [(sd + timedelta(days=k) - cyc.date()).days for k in (1, 2)]
        assert js == ([0, 1] if h <= 8 else [1, 2]), (h, js)
    assert bd.SLOT_KEYS == ["h00/j0", "h00/j1", "h03/j0", "h03/j1", "h06/j0", "h06/j1", "h09/j1", "h09/j2",
                            "h12/j1", "h12/j2", "h15/j1", "h15/j2", "h18/j1", "h18/j2", "h21/j1", "h21/j2"]


# ---- the committed file -----------------------------------------------------------
def test_committed_maps_rise_and_stay_inside_0_1():
    dc = bd.load_daycal()
    assert set(POOLED) <= set(dc["maps"])
    for key, m in dc["maps"].items():
        x, y = m["x"], m["y"]
        assert len(x) == len(y) >= 2, key
        assert all(a < b for a, b in zip(x, x[1:])), f"{key}: x must rise strictly"
        assert all(a <= b for a, b in zip(y, y[1:])), f"{key}: y must never fall"
        assert all(0.0 <= v <= 1.0 for v in x + y), key
        # and so does everything read off it, on a fine grid and past both ends
        out = [bd.apply_daymap(m, i / 1000.0) for i in range(-50, 1051)]
        assert all(a <= b + 1e-12 for a, b in zip(out, out[1:])), f"{key}: mapped values fall somewhere"
        assert all(0.0 <= v <= 1.0 for v in out), key
        # a thin area cannot print 100%: every block behind a breakpoint held 200 station-days
        assert m["min_block_n"] >= 200 and y[-1] < 0.95, (key, m["min_block_n"], y[-1])


def test_every_published_slot_has_a_passing_check_behind_it():
    """The committed file, read the way the build reads it: no map is used from
    a bucket of cycle hours unless the check on real 2025 cycles at that
    bucket's own hour passed the ship rule, and no area map unless it passed
    there on the area's own rows."""
    dc = bd.load_daycal()
    for sk, s in dc["slots"].items():
        if s["map"] is None:
            assert not any(c["pass"] for c in s["tried"]), sk
            continue
        c = s["check"]
        assert c["map"] == s["map"] and f"{sk[1:3]}Z" in c["sample"] and "2025" in c["sample"], (sk, c["sample"])
        assert c["skill_pct"] > 0, sk
        g = c["ge50"]
        assert g["n"] < 50 or abs(g["shown"] - g["obs"]) <= 0.10, (sk, g)
        for name, a in s["areas"].items():
            if a["use"] == "regional":
                ac = a["check"]
                assert ac["skill_pct"] > 0 and ac["map"] == f"{s['map']}@{name}", (sk, name)
                for band in ("ge50", "b25_50"):
                    assert ac[band]["n"] < 50 or abs(ac[band]["shown"] - ac[band]["obs"]) <= 0.10, (sk, name, band)
                assert dc["regional"][s["map"]][name]["pass"] is True


def test_what_the_committed_file_publishes():
    """The fit's outcome, pinned so a refit that changes it is seen (update
    this test with the new receipts, on purpose, when it does).
    Day 1 has a day-level chance from every cycle hour. Day 2 has one from no
    cycle hour in November-February: every map for it either failed its check
    (an evening build's day 2 ran 11-16 points high at 50% or more) or missed
    in the fog season."""
    dc = bd.load_daycal()
    assert bd.day1_cycle_hours(dc) == list(range(24))
    assert dc["slots"]["h00/j0"]["map"] == "d1"                  # the one named fallback: d1u failed at 00Z
    assert [c["map"] for c in dc["slots"]["h00/j0"]["tried"]] == ["d1u", "d1"]
    assert dc["slots"]["h03/j0"]["map"] == "d1u" and dc["slots"]["h06/j0"]["map"] == "d1u"
    for h in range(24):
        cyc = _utc(2026, 12, 8, h)
        sd = bd.service_day(_build(cyc))
        assert bd.pick_map(dc, cyc, sd + timedelta(days=1))["why"] is None, h
        assert bd.pick_map(dc, cyc, sd + timedelta(days=2))["why"] in ("hour", "season"), h
    for sk in ("h00/j1", "h03/j1", "h06/j1"):                    # what a US evening build sees as day 2
        s = dc["slots"][sk]
        assert s["map"] is None and s["tried"][0]["map"] == "d2f" and s["tried"][0]["rule_b_ge50_within_10"] is False
    # the Central Valley has its own map in every day-1 slot; it is unfloored and never reaches 25%
    for sk in bd.SLOT_KEYS:
        if dc["slots"][sk]["kind"] in ("under_way", "ahead"):
            a = dc["slots"][sk]["areas"]["Central Valley"]
            m = dc["maps"][a["map"]]
            assert a["use"] == "regional" and m["floor"] is False and m["y"][-1] < 0.25, (sk, a["map"])
            # West Coast fields outside it keep the pooled map, and its flag
            assert dc["slots"][sk]["areas"]["West Coast"]["use"] == "pooled", sk
    assert dc["regional"]["d1"]["West Coast"]["flag"]["ok"] is False
    # Mountain West keeps the pooled map in every slot (regional rule f, added
    # after the result): its unfloored map topped out at 39% and had replaced a
    # pooled map whose 50%-and-over values there verified (62% shown, 61% observed)
    assert not [k for k in dc["maps"] if k.endswith("@Mountain West")]
    assert all(s["areas"]["Mountain West"]["use"] == "pooled" for s in dc["slots"].values())
    mw = dc["regional"]["d1"]["Mountain West"]
    assert mw["pass"] is False and mw["forms"]["unfloored"]["rules"]["f_unfloored_only_where_pooled_ran_high"] is False
    assert all(v is not False for k, v in mw["forms"]["unfloored"]["rules"].items() if not k.startswith("f_")), \
        "rule (f) is the only thing that withholds it"
    # the areas that do have a map: the Central Valley's pooled record ran high (rule f holds), Canada's is floored
    assert dc["regional"]["d1"]["Central Valley"]["forms"]["unfloored"]["rules"]["f_unfloored_only_where_pooled_ran_high"] is True
    assert sorted(k for k in dc["maps"] if "@" in k) == ["d1@Canada", "d1@Central Valley", "d1u@Central Valley"]
    assert dc["maps"]["d1@Canada"]["floor"] is True


def test_bare_product_of_half_is_published_well_below_half():
    """The regression. Six consecutive hours at 11% multiply out to 0.503: the
    old build published that, and the app said GO. The hindcast says such a
    day fogs roughly one time in four."""
    cyc = _utc(2026, 12, 8, 9)
    hours = _full_cycle(cyc, {(1, h): 11 for h in range(4, 10)})      # 04Z-09Z tomorrow
    current = _issuance(cyc, hours)
    tomorrow = date(2026, 12, 9)
    st = bd.day_stats(sorted(hours), [hours[f] for f in sorted(hours)], cyc, [tomorrow])
    assert abs((1.0 - st[tomorrow]["noev"]) - 0.503) < 0.001, "fixture: the bare product should be 0.503"
    p = _published_day1(current, bd.service_day(_build(cyc)))
    assert p is not None and 0.11 <= p <= 0.40, f"a bare product of 0.50 was published as {p}"
    for key in ("d1", "d1u"):                                    # both day-1 maps, daytime and evening builds
        assert bd.apply_daymap(bd.load_daycal()["maps"][key], 0.50) <= 0.40, key


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
    cyc = _utc(2026, 12, 8, 9)
    days, _ = _tier(cyc, {(1, 6): 60})                         # day-1 map says 0.01 for everything
    d = days[("KVIS", 1)]
    assert d["pk"] == 60 and d["p"] == 0.6 and abs(d["praw"] - 0.6) < 1e-9 and d["ppool"] == 0.6
    # the same on a day under way (an evening build) and on day 2
    days, _ = _tier(_utc(2026, 12, 8, 3), {(0, 13): 70, (1, 12): 45})
    assert days[("KVIS", 1)]["p"] == 0.7 and days[("KVIS", 2)]["p"] == 0.45


def test_an_unfloored_area_map_can_sit_below_the_peak_hour_and_the_payload_says_so():
    """The Central Valley's own map passed its check only without the floor:
    days whose peak hour showed 50% or more fogged about 4 times in 10 there.
    p is then the area map's value alone, the pooled value rides in the log,
    and the receipt marks the area floor=false so no screen prints p and pk
    as if they agreed."""
    cyc = _utc(2026, 12, 8, 9)
    dc = _area_map(_daycal(), "h09/j1", "Central Valley", 0.11, floor=False)
    days, info = _tier(cyc, {(1, 6): 60}, dc, atlas=ATLAS)
    d = days[("KVIS", 1)]
    assert d["area"] == "Central Valley" and d["map"] == "d1@Central Valley"
    assert d["p"] == 0.11 and d["pk"] == 60 and d["ppool"] == 0.6
    r = bd.day_receipt(dc, info["days"]["1"], 9, ATLAS, list(ATLAS))
    cv = {a["name"]: a for a in r["areas"]}["Central Valley"]
    assert cv["regional"] is True and cv["floor"] is False and cv["top"] == 0.11 and cv["icaos"] == ["KVIS"]
    assert cv["by_peak_hour"]["ge50"]["obs"] == 0.39            # what a 50%+ peak hour has meant there
    # a floored area map keeps the floor
    dc = _area_map(_daycal(), "h09/j1", "Central Valley", 0.11, floor=True)
    assert _tier(cyc, {(1, 6): 60}, dc, atlas=ATLAS)[0][("KVIS", 1)]["p"] == 0.6


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
    targets = {today + timedelta(days=dd): dd for dd in (1, 2)}
    new = {targets[d]: s for d, s in bd.day_stats(f["fhrs"], [r[3] for r in f["p"]], cyc, targets).items()}
    assert set(new) == {1, 2}
    for dd in (1, 2):
        assert new[dd]["eh"] == cal_day[dd] and new[dd]["noev"] == cal_noev[dd], dd
        assert new[dd]["win"] == cal_win[dd] and new[dd]["ws"] == cal_ws[dd], dd
    # a 17Z cycle: tomorrow is 19 hourly leads and the 21Z point, the day after is six points through 15Z
    assert new[1]["used"] == 22 and new[1]["last"] == 21 and new[2]["used"] == 18 and new[2]["last"] == 15
    assert new[2]["pk"] == 48
    # `through` is the fitter's: a 12Z cycle standing in for one that reaches
    # only 09Z. It cuts the statistic and leaves eh, win and ws alone.
    d2 = today + timedelta(days=2)
    cut = bd.day_stats(f["fhrs"], [r[3] for r in f["p"]], cyc, targets, through={d2: 9})
    assert cut[today + timedelta(days=1)] == new[1]
    assert {k: cut[d2][k] for k in ("eh", "win", "ws")} == {k: new[2][k] for k in ("eh", "win", "ws")}
    assert cut[d2]["used"] == 12 and cut[d2]["last"] == 9 and cut[d2]["pk"] == 48
    assert cut[d2]["noev"] > new[2]["noev"]                    # the 12Z and 15Z points are not in it


# ---- which map, for every cycle hour, day and region ------------------------------
def _expected(h: int, k: int) -> tuple[str, str, int]:
    """(pooled map, kind, UTC hour the forecast of that planner day runs through)
    for the tick one hour after an h-Z cycle — the pre-registration's table."""
    if h <= 8:
        return ("d1u", "under_way", 23) if k == 1 else ("d2f", "evening", 21)
    if k == 1:
        return "d1", "ahead", {21: 22, 22: 23, 23: 23}.get(h, 21)
    if h <= 11:
        return "d2h09", "partial", 9
    if h <= 14:
        return "d2h12", "partial", 12
    return "d2f", "after_next", min(h - h % 3, 21)


def test_map_selection_for_every_cycle_hour_day_and_region():
    """Synthetic file, every slot published, the Central Valley with its own
    map in the daytime day-1 slots and one evening slot, Canada in one slot."""
    dc = _daycal()
    cv = {"h09/j1", "h12/j1", "h15/j1", "h18/j1", "h21/j1", "h03/j0"}
    for sk in cv:
        _area_map(dc, sk, "Central Valley", 0.11)
    _area_map(dc, "h18/j2", "Canada", 0.44, floor=True)
    fields = {"KVIS": "Central Valley", "KSFO": "West Coast", "CYVR": "Canada", "KDEN": None}
    for h in range(24):
        cyc = _utc(2026, 6, 8, h)
        sd = bd.service_day(_build(cyc))
        cur = {"meta": {"cycle": cyc.strftime("%Y-%m-%dT%H:00:00Z")},
               "airports": {i: _issuance(cyc, _full_cycle(cyc))["airports"]["KVIS"] for i in fields}}
        days, info = bd.cal_tier(cur, sd, dc, ATLAS)
        assert info["bucket"] == bd.bucket_of(h) and info["cycle_hour"] == h
        for k in (1, 2):
            key, kind, through = _expected(h, k)
            target = sd + timedelta(days=k)
            sk = f"{bd.bucket_of(h)}/j{(target - cyc.date()).days}"
            d = info["days"][str(k)]
            assert (d["slot"], d["kind"], d["map"], d["why"]) == (sk, kind, key, None), (h, k, d)
            assert d["cover"]["through"] == through and d["date"] == target.isoformat(), (h, k, d)
            assert d["cover"]["from"] == (h + 1 if kind == "under_way" else 0), (h, k, d)
            for icao, area in fields.items():
                sel = bd.pick_map(dc, cyc, target, area)
                want = key
                if area == "Central Valley" and sk in cv:
                    want = f"{key}@Central Valley"
                if area == "Canada" and sk == "h18/j2":
                    want = "d2f@Canada"
                assert sel["key"] == want and sel["why"] is None and sel["slot"] == sk, (h, k, area, sel["key"])
                assert sel["regional"] is ("@" in want) and sel["floor"] is (want != f"{key}@Central Valley")
                got = days[(icao, k)]
                assert got["area"] == area and got["map"] == want and got["cov"] == through, (h, k, icao, got)
                assert got["p"] == {"d1@Central Valley": 0.11, "d1u@Central Valley": 0.11,
                                    "d2f@Canada": 0.44}.get(want, VALUES[key]), (h, k, icao, got)
                assert got["ppool"] == VALUES[key] and got["cal"] is True
    # without the airport list every field gets the pooled map
    cyc = _utc(2026, 6, 8, 9)
    days, _ = bd.cal_tier(_issuance(cyc, _full_cycle(cyc)), bd.service_day(_build(cyc)), dc)
    assert days[("KVIS", 1)]["map"] == "d1" and days[("KVIS", 1)]["area"] is None


def test_map_selection_in_the_committed_file_for_every_cycle_hour_day_and_region():
    """The same walk over the real file: for each cycle hour, planner day and
    area the build uses the slot's pooled map, or the area's own where the
    file says it passed there, or nothing — and says why."""
    dc = bd.load_daycal()
    names = [a["name"] for a in dc["areas"]]
    assert names == ["Central Valley", "West Coast", "Mountain West", "Plains", "Great Lakes", "Southeast",
                     "Northeast", "Alaska", "Hawaii", "Canada"]
    seen = set()
    for month in (12, 6):
        for h in range(24):
            cyc = _utc(2026, month, 8, h)
            sd = bd.service_day(_build(cyc))
            for k in (1, 2):
                key, kind, _ = _expected(h, k)
                target = sd + timedelta(days=k)
                slot = dc["slots"][f"{bd.bucket_of(h)}/j{(target - cyc.date()).days}"]
                assert slot["kind"] == kind
                for area in [None] + names:
                    sel = bd.pick_map(dc, cyc, target, area)
                    if slot["map"] is None:
                        assert sel["why"] == "hour" and sel["map"] is None, (h, k, area)
                    elif month in slot["hold_months"]:
                        assert sel["why"] == "season" and sel["map"] is None, (h, k, area)
                    else:
                        a = slot["areas"].get(area) if area else None
                        own = bool(a) and a["use"] == "regional" and month not in a["hold_months"]
                        want = f"{slot['map']}@{area}" if own else slot["map"]
                        assert sel["why"] is None and sel["key"] == want, (h, k, area, sel["key"])
                        assert sel["map"] is dc["maps"][want] and sel["floor"] is dc["maps"][want]["floor"]
                        assert slot["map"] in ((key, "d1") if kind == "under_way" else (key,)), (h, k, slot["map"])
                        seen.add(want)
    assert {k for k in dc["maps"] if "@" in k} <= seen, "an area map nothing uses is in the file"


def test_a_region_with_no_passing_map_keeps_the_pooled_number_and_its_flag():
    """West Coast fields outside the Central Valley: their own map did not pass,
    so they keep the pooled number, and the record of how the pooled map did
    there (ran 11 points high at 50% or more) goes out with it."""
    dc = _area_map(_daycal(), "h09/j1", "Central Valley", 0.11)
    dc["slots"]["h09/j1"]["areas"]["West Coast"] = {"use": "pooled", "map": None, "hold_months": [], "check": None,
                                                    "why": "leave-one-year-out failed", "own": None}
    _flag(dc, "d1", "West Coast", 0.651, 0.541)
    bd_dc = json.loads(json.dumps(dc))                          # as it comes off disk
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "daycal.json"
        p.write_text(json.dumps(dc))
        assert bd.load_daycal(p) == bd_dc, "the fixture itself must load"
    cyc = _utc(2026, 12, 8, 9)
    cur = {"meta": {"cycle": cyc.strftime("%Y-%m-%dT%H:00:00Z")},
           "airports": {i: _issuance(cyc, _full_cycle(cyc, {(1, 6): 30}))["airports"]["KVIS"] for i in ("KVIS", "KSFO")}}
    days, info = bd.cal_tier(cur, bd.service_day(_build(cyc)), dc, ATLAS)
    sfo, vis = days[("KSFO", 1)], days[("KVIS", 1)]
    assert sfo["area"] == "West Coast" and sfo["map"] == "d1" and sfo["p"] == 0.3 == sfo["ppool"]
    assert vis["map"] == "d1@Central Valley" and vis["p"] == 0.11 and vis["ppool"] == 0.3
    r = bd.day_receipt(dc, info["days"]["1"], 9, ATLAS, list(ATLAS))
    by = {a["name"]: a for a in r["areas"]}
    wc = by["West Coast"]
    assert wc["regional"] is False and wc["ok"] is False and wc["high"] is True and wc["years"] == "2023-25"
    assert wc["ge50"]["n"] == 296 and wc["ge50"]["ok"] is False and wc["icaos"] == ["KSFO"]
    assert "floor" not in wc and "top" not in wc
    assert by["Central Valley"]["regional"] is True and by["Central Valley"]["ok"] is True
    assert "Canada" not in by                                   # nothing of its own to say: the pooled receipt stands
    # an area map held for the season falls back the same way, in those months only
    dc = _area_map(_daycal(), "h09/j1", "Central Valley", 0.11, hold=[11, 12, 1, 2])
    _flag(dc, "d1", "Central Valley", 0.649, 0.349, n=63)
    for month, want in ((12, "d1"), (6, "d1@Central Valley")):
        c = _utc(2026, month, 8, 9)
        assert bd.pick_map(dc, c, c.date() + timedelta(days=1), "Central Valley")["key"] == want, month
        _, info = _tier(c, dc=dc, atlas=ATLAS)
        cv = {a["name"]: a for a in bd.day_receipt(dc, info["days"]["1"], 9, ATLAS, list(ATLAS))["areas"]}["Central Valley"]
        # in the held months the receipt is the pooled map's record there, not the area map's
        assert cv["regional"] is (month == 6) and cv["ok"] is (month == 6), (month, cv)


# ---- when there is no number ---------------------------------------------------------
def test_a_slot_whose_check_failed_publishes_no_number_and_keeps_the_forecast():
    """Day 2 seen from a 09Z cycle reaches 00Z-09Z only, and the map for that
    reach failed on real 09Z cycles (shown 59%, observed 46%). Until
    2026-11-25 the day then fell to climatology whole. Now it stays a "cal"
    day: expected hours, peak hour and window are the forecast's, with the
    hour it runs through, and p is null with the reason."""
    cyc = _utc(2026, 12, 8, 9)
    dc = _daycal(held=("h09/j2",))
    days, info = _tier(cyc, {(2, 6): 40, (2, 9): 20, (1, 12): 30}, dc)
    d2 = days[("KVIS", 2)]
    assert d2["cal"] is True and d2["p"] is None and d2["why"] == "hour" and d2["map"] is None
    assert d2["pk"] == 40 and abs(d2["eh"] - 1.8) < 1e-9 and d2["cov"] == 9 and d2["ws"] == 45
    assert d2["praw"] is not None                              # still logged, to re-score the map later
    assert info["applied"] == {"1": True, "2": False} and info["why"] == {"1": None, "2": "hour"}
    assert info["days"]["2"]["slot"] == "h09/j2" and info["days"]["2"]["map"] is None
    # the same for day 1 from a bucket whose check failed
    days, info = _tier(_utc(2026, 12, 8, 4), {(0, 12): 30}, _daycal(held=("h03/j0",)))
    assert days[("KVIS", 1)]["cal"] is True and days[("KVIS", 1)]["p"] is None and days[("KVIS", 1)]["pk"] == 30
    assert info["applied"]["1"] is False and info["why"]["1"] == "hour"


def test_a_map_is_only_used_from_the_buckets_it_passed_in():
    """Synthetic file: the evening day-1 map failed at 06Z, day 2's at 00Z and 15Z."""
    dc = _daycal(held=("h06/j0", "h00/j1", "h15/j2"))
    for h in range(24):
        days, info = _tier(_utc(2026, 6, 8, h), dc=dc)
        want1, want2 = not 6 <= h <= 8, not (h <= 2 or 15 <= h <= 17)
        assert (days[("KVIS", 1)]["p"] is not None) is want1, h
        assert (days[("KVIS", 2)]["p"] is not None) is want2, h
        assert days[("KVIS", 1)]["cal"] is True and days[("KVIS", 2)]["cal"] is True
        assert info["why"] == {"1": None if want1 else "hour", "2": None if want2 else "hour"}, h
    assert bd.day1_cycle_hours(dc) == [h for h in range(24) if not 6 <= h <= 8]


def test_a_cycle_no_check_reaches_gets_no_number():
    """The engine fell back to a cycle from an earlier date than the build
    expects. A 12Z cycle read at 10:25Z the next morning: day 1 is two dates
    after the cycle, covered through 12Z only, and day 2 is beyond its 48 h."""
    cyc = _utc(2026, 12, 7, 12)
    days, info = _tier(cyc, {(2, 6): 30}, now=_utc(2026, 12, 8, 10, 25))
    assert info["days"]["1"]["date"] == "2026-12-09" and info["days"]["1"]["kind"] == "partial"
    assert days[("KVIS", 1)]["p"] == 0.32                       # it is the checked "through 12Z" reach, whatever day it is called
    assert ("KVIS", 2) not in days and info["why"]["2"] == "reach" and info["days"]["2"]["cover"] is None
    # an evening cycle read after the 10Z flip: its own date is now the service day, not a planner day
    cyc = _utc(2026, 12, 8, 8)
    days, info = _tier(cyc, {(1, 12): 30, (2, 6): 50}, now=_utc(2026, 12, 8, 10, 25))
    assert info["days"]["1"] == dict(info["days"]["1"], date="2026-12-09", kind="evening", map="d2f", why=None)
    assert days[("KVIS", 1)]["p"] == 0.3
    d2 = days[("KVIS", 2)]                                      # three NBS points of the 10th: forecast hours, no chance
    assert d2["cal"] is True and d2["p"] is None and d2["why"] == "reach" and d2["cov"] == 6 and d2["pk"] == 50
    # and pick_map alone, for a date behind the cycle or far ahead
    for j in (-1, 3):
        assert bd.pick_map(_daycal(), cyc, cyc.date() + timedelta(days=j))["why"] == "reach"


def test_a_map_is_not_used_in_the_months_it_missed():
    """Day 2 through 12Z at 50% or more verified at 46% in Nov-Feb 2025: the
    committed slot carries those months as held, and a 12Z build in December
    publishes no day-level chance for day 2."""
    dc = _daycal(hold={"h12/j2": [11, 12, 1, 2]})
    for when, want in ((_utc(2026, 12, 8, 12), False), (_utc(2026, 6, 8, 12), True),
                       (_utc(2026, 10, 30, 12), False)):       # day 2 = 1 November
        days, info = _tier(when, dc=dc)
        assert (days[("KVIS", 2)]["p"] is not None) is want, when
        assert days[("KVIS", 2)]["cal"] is True
        assert info["why"]["2"] == (None if want else "season")
        assert days[("KVIS", 1)]["p"] == 0.01                   # day 1 has its own months
    committed = bd.load_daycal()["slots"]["h12/j2"]
    assert committed["hold_months"] == [11, 12, 1, 2] or committed["map"] is None


def test_too_few_hours_means_no_number_for_that_airport():
    """At most three hour-equivalents of what the cycle should cover may be
    missing (the old "20 of 23" for tomorrow)."""
    cyc = _utc(2026, 12, 8, 9)
    for drop, want in (((27,), True), ((15, 16, 17), True), ((15, 16, 17, 18), False), ((27, 15), False),
                       ((39, 42, 45, 48), True)):               # the last four are day 2's: day 1 does not care
        hours = _full_cycle(cyc, {(1, 5): 30})
        for f in drop:
            del hours[f]                                        # lead 27 is the 12Z NBS point: three hour-equivalents
        days, info = bd.cal_tier(_issuance(cyc, hours), date(2026, 12, 8), _daycal())
        d = days[("KVIS", 1)]
        assert (d["p"] is not None) is want and d["why"] == (None if want else "hours"), (drop, d)
        assert d["cal"] is True and d["pk"] == 30
    # day 2 through 09Z is four NBS points: one may be missing, two may not
    for drop, want in (((45,), True), ((42, 45), False)):
        hours = _full_cycle(cyc)
        for f in drop:
            del hours[f]
        days, _ = bd.cal_tier(_issuance(cyc, hours), date(2026, 12, 8), _daycal())
        assert (days[("KVIS", 2)]["p"] is not None) is want, drop
    # a day under way: 17 hours left after a 06Z cycle, 14 are enough, 13 are not
    cyc = _utc(2026, 12, 8, 6)
    for n_drop, want in ((3, True), (4, False)):
        hours = _full_cycle(cyc)
        for f in range(10, 10 + n_drop):
            del hours[f]
        days, _ = bd.cal_tier(_issuance(cyc, hours), date(2026, 12, 7), _daycal())
        assert (days[("KVIS", 1)]["p"] is not None) is want, n_drop
    assert bd.MAX_MISSING_HOURS == 3


def test_half_a_tomorrow_gets_no_day_level_number():
    """No 26-48 h bulletin: eh is summed from what is there, the tier stays
    "cal" (the watchdog counts it), but no chance is claimed for the whole day."""
    cyc = _utc(2026, 12, 8, 9)
    hours = {f: v for f, v in _full_cycle(cyc, {(1, 5): 30}).items() if f <= 25}
    days, info = bd.cal_tier(_issuance(cyc, hours), date(2026, 12, 8), _daycal())
    d = days[("KVIS", 1)]
    assert d["cal"] is True and d["p"] is None and d["pk"] == 30 and abs(d["eh"] - 0.3) < 1e-9 and d["cov"] == 10
    # the map was ready and the bulletin was not. Until 2026-11-25 the build
    # stopped here ("wait") so the next tick retried; the hourly build writes
    # what it has and carries the last published chance for that date
    # (forecast/test_planner.py: test_a_build_without_the_26_48_h_bulletin_...)
    assert info["applied"]["1"] is False and info["why"]["1"] == "hours"
    assert not hasattr(bd, "bake_decision"), "write / wait / keep went with the daily build"


def test_which_cycle_hours_publish_a_day_1_chance_of_their_own():
    """day1_cycle_hours: the hours whose own tick has a checked day-1 chance.
    What a build from any other hour does (it carries, it never keeps the old
    payload or stops) is pinned in forecast/test_planner.py."""
    assert bd.day1_cycle_hours(_daycal(held=("h06/j0",))) == [h for h in range(24) if not 6 <= h <= 8]
    none = _daycal(held=[sk for sk in bd.SLOT_KEYS if sk.endswith(("j0",)) or sk in
                         ("h09/j1", "h12/j1", "h15/j1", "h18/j1", "h21/j1")])
    assert bd.day1_cycle_hours(none) == []
    assert bd.day1_cycle_hours(bd.load_daycal()) == list(range(24))   # the committed file: every hour


# ---- the payload ------------------------------------------------------------------
def _lower(icao, dd, dte):
    return 1.23, "climo", 0.111


def test_the_payload_carries_the_mapped_chance_never_the_bare_product():
    """The assembly itself. A review put the bare product back with one line in
    main() (pdays.append(c["praw"])) and every test passed, because none ran
    this step; the same for pk on every tier and for a day 2 kept "cal"
    without its map."""
    cyc = _utc(2026, 12, 8, 9)
    sd = bd.service_day(_build(cyc))
    hours = _full_cycle(cyc, {**{(1, h): 11 for h in range(4, 10)}, (2, 6): 40})
    current = _issuance(cyc, hours)
    cal, info = bd.cal_tier(current, sd, bd.load_daycal())
    a = bd.airport_entry("KVIS", sd, cal, {("KVIS", 12): 5}, _lower)
    assert set(a) == {"eh", "tiers", "p", "win", "ws", "pk", "cov", "ehc"}, "praw and ppool must not ride in the payload"
    assert all(len(a[k]) == 14 for k in a if k not in ("cov", "ehc")) and len(a["cov"]) == len(a["ehc"]) == 2
    assert a["ehc"] == [None, None]                             # no climatology table was handed in: nothing is added
    c1 = cal[("KVIS", 1)]
    assert abs(c1["praw"] - 0.503) < 0.001                      # the bare product is still computed, for the log
    assert a["tiers"][0] == "cal" and a["p"][0] == c1["p"] and a["p"][0] <= 0.40, a["p"][0]
    assert a["pk"][0] == 11 and a["eh"][0] == round(c1["eh"], 2) and a["cov"][0] == 21
    # day 2 on the 09Z bake: the forecast's hours, peak hour and window through
    # 09Z, on the "cal" tier, and no day-level chance (its map failed its check)
    assert info["applied"]["2"] is False and info["why"]["2"] == "hour"
    assert a["tiers"][1] == "cal" and a["p"][1] is None and a["eh"][1] == 1.2 and a["pk"][1] == 40
    assert a["cov"][1] == 9 and a["ws"][1] == 45 and a["win"][1] == 3
    # every later day is the lower tier's, whole; pk is only ever beside a "cal" day
    assert a["tiers"][2:] == ["climo"] * 12 and a["pk"][2:] == [None] * 12
    assert a["p"][2:] == [0.111] * 12 and a["eh"][2:] == [1.23] * 12 and a["ws"][2:] == [None] * 12
    # with a map that passes for day 2, day 2 carries its own mapped chance
    cal2, info2 = bd.cal_tier(current, sd, _daycal())
    b = bd.airport_entry("KVIS", sd, cal2, {}, _lower)
    assert info2["applied"]["2"] is True and b["tiers"][:3] == ["cal", "cal", "climo"]
    assert b["p"][1] == max(0.29, 0.40) and b["pk"][1] == 40    # the map's 0.29, floored at the 40% peak hour
    assert b["p"][1] != cal2[("KVIS", 2)]["praw"]
    # an airport the hourly forecast does not cover gets the lower tier on every day, and no coverage hour
    c = bd.airport_entry("KZZZ", sd, cal2, {}, _lower)
    assert c["tiers"] == ["climo"] * 14 and c["cov"] == [None, None] and c["pk"] == [None] * 14
    # an area map's value goes out as p; the pooled value it replaced does not
    dc = _area_map(_daycal(), "h09/j1", "Central Valley", 0.07)
    cal3, _ = bd.cal_tier(current, sd, dc, ATLAS)
    assert cal3[("KVIS", 1)]["p"] == 0.07 and cal3[("KVIS", 1)]["ppool"] == 0.11   # the pooled map, floored at 11%
    e = bd.airport_entry("KVIS", sd, cal3, {}, _lower)
    assert e["p"][0] == 0.07 and "ppool" not in e and "praw" not in e and "map" not in e


def test_no_day_level_chance_means_a_null_not_a_zero():
    cyc = _utc(2026, 12, 8, 4)                                  # an evening build whose bucket failed its check
    sd = bd.service_day(_build(cyc))
    cal, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc, {(0, 12): 30})), sd, _daycal(held=("h03/j0",)))
    a = bd.airport_entry("KVIS", sd, cal, {}, _lower)
    assert a["tiers"][0] == "cal" and a["p"][0] is None and a["pk"][0] == 30   # watchdog check 7 still counts the tier
    assert a["cov"][0] == 23


# ---- the receipts on screen ---------------------------------------------------------
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


def _committed_receipt(cyc: datetime, k: int, applied=None) -> dict:
    """The receipt the committed file gives planner day k of the build that runs this cycle."""
    dc = bd.load_daycal()
    _, info = bd.cal_tier(_issuance(cyc, _full_cycle(cyc)), bd.service_day(_build(cyc)), dc, ATLAS)
    return bd.day_receipt(dc, info["days"][str(k)], cyc.hour, ATLAS, list(ATLAS),
                          info["applied"][str(k)] if applied is None else applied)


def test_the_receipt_is_the_check_on_the_builds_own_bucket():
    """A 09Z build used to print the 12Z holdout's numbers whatever cycle built it."""
    dc = bd.load_daycal()
    june = lambda h: _utc(2026, 6, 8, h)
    r9, r10, r15, r2, r4 = (_committed_receipt(june(h), 1) for h in (9, 10, 15, 2, 4))
    for r, sk, hh in ((r9, "h09/j1", "09Z"), (r10, "h09/j1", "09Z"), (r15, "h15/j1", "15Z"), (r2, "h00/j0", "00Z"),
                      (r4, "h03/j0", "03Z")):
        c = dc["slots"][sk]["check"]
        assert r["slot"] == sk and r["checked_from"] == int(hh[:2]) and hh in r["sample"], (sk, r["sample"])
        assert r["ge50"] == c["ge50"] and r["skill_pct"] == c["skill_pct"] and r["map"] == c["map"]
        assert r["ge50"]["dates"] < r["ge50"]["n"], "the number of cycle dates rides beside the airport-days"
        assert r["ge75"]["n"] >= 50, "the top band is always backed by a group the rule could judge"
        assert r["top"] == dc["maps"][r["map"]]["y"][-1]
        assert "reliability" not in r and "x" not in r and "tried" not in r
    assert r9["cycle_hour"] == 9 and r10["cycle_hour"] == 10
    assert r2["under_way"] is True and r2["cover"] == {"from": 3, "through": 23} and r2["date"] == "2026-06-08"
    assert r9["under_way"] is False and r9["cover"] == {"from": 0, "through": 21} and r9["date"] == "2026-06-09"
    # the 09Z sample's top band is thin (46 airport-days): the 12Z holdout's stands in, and says so
    assert dc["slots"]["h09/j1"]["check"]["ge75"]["n"] < 50 and "12Z" in r9["ge75"]["sample"]
    # day 2 of an evening build: its map failed, so no receipt goes out with
    # it — only what the failed check found, so the screen can say why
    for r in (_committed_receipt(_utc(2026, 12, 8, 3), 2), _committed_receipt(june(9), 1, applied=False)):
        assert r["checked_from"] is None and "ge50" not in r and "areas" not in r and "skill_pct" not in r, r
    r = _committed_receipt(_utc(2026, 12, 8, 3), 2)
    assert r["map"] is None and r["shipped"] is False and r["kind"] == "evening" and r["cover_end_utc"] is None
    assert [c["map"] for c in r["failed"]] == ["d2f"] and r["failed"][0]["ge50"]["shown"] - r["failed"][0]["ge50"]["obs"] > 0.10
    # day 2 seen in part says how far the forecast reaches
    r = _committed_receipt(_utc(2026, 12, 8, 9), 2)
    assert r["kind"] == "partial" and r["cover_end_utc"] == 9 and r["cover"] == {"from": 0, "through": 9}
    assert _committed_receipt(_utc(2026, 12, 8, 16), 2)["cover_end_utc"] == 15


def test_each_area_gets_its_own_record_not_the_pooled_one():
    """The screen printed "58% of the time" beside KVIS. Central Valley fields
    shown at 50% or more by the pooled map fogged about a third of the time.
    They now get the area's own map and its leave-one-year-out record; fields
    that stay on the pooled map get the record of how it did in their area."""
    for cyc in (_utc(2026, 12, 8, 9), _utc(2026, 12, 8, 4)):    # a daytime build and an evening one
        r = _committed_receipt(cyc, 1)
        by = {a["name"]: a for a in r["areas"]}
        cv, wc = by["Central Valley"], by["West Coast"]
        assert "KVIS" in cv["icaos"] and "KSFO" not in cv["icaos"] and "KSFO" in wc["icaos"]
        assert cv["regional"] is True and cv["ok"] is True and cv["floor"] is False and cv["years"] == "2023-25"
        assert cv["top"] < 0.25 and cv["skill_pct"] > 0 and cv["ge50"]["n"] == 0, cv
        assert cv["by_peak_hour"]["ge50"]["n"] > 0 and cv["hourly"]["eh"]["ratio"] > 0
        assert wc["regional"] is False and wc["ok"] is False and wc["high"] is True
        assert wc["ge50"]["n"] >= 50 and wc["ge50"]["shown"] - wc["ge50"]["obs"] > 0.10 and wc["ge50"]["ok"] is False
        assert wc["ge50"] != r["ge50"]
        for a in r["areas"]:
            if a["regional"]:
                assert a["ok"] is True and isinstance(a["floor"], bool) and 0 < a["top"] < 1, a["name"]
            elif "ok" in a:
                # a pooled-map area that is listed with a verdict was judged on a group the rule could judge
                assert a["ge50"]["n"] >= 50 and a["years"] and isinstance(a["ok"], bool), a["name"]
                assert a["ok"] == (abs(a["ge50"]["shown"] - a["ge50"]["obs"]) <= 0.10), a["name"]
            else:
                assert a["hourly"]["runs"] in ("high", "low"), a["name"]   # listed only for its hourly record
        listed = [i for a in r["areas"] for i in a["icaos"]]
        assert len(listed) == len(set(listed)), "a field belongs to one area"
    # where the hourly forecast's expected hours ran well off in an area, the receipt says which way
    by = {a["name"]: a for a in _committed_receipt(_utc(2026, 12, 8, 4), 1)["areas"]}
    assert by["Central Valley"]["hourly"]["runs"] == "high" and by["Central Valley"]["hourly"]["eh"]["ratio"] > 1.25


def test_an_area_record_is_the_buckets_own_where_that_missed_by_more():
    """2026-11-25 review: every area record on screen came from the map's FIT
    hour. Builds from 00Z-02Z cycles use map d1 (the named fallback), so a West
    Coast GO at 79% said "about 11 points high" (12Z cycles, the next date)
    while that bucket's own West Coast rows ran 24 points high at 50% and over
    and 30 at 75% and over. The receipt now carries the judged sample with the
    larger miss, and says which cycles it is."""
    # the rule, on a synthetic file
    fit_hour = {"judged": ["2023-25"], "samples": {"2023-25": {"ge50": _grp(296, 0.651, 0.541, 87),
                                                               "b25_50": _grp(1000, 0.35, 0.34, 135),
                                                               "ge75": _grp(87, 0.78, 0.70, 47)}}}
    own = {"sample": "30 real 00Z cycles of 2025, never fitted on", "n": 2000, "ge50": _grp(128, 0.683, 0.438, 21),
           "b25_50": _grp(300, 0.36, 0.30, 25), "ge75": _grp(57, 0.787, 0.491, 16)}
    r = bd.area_record(fit_hour, own)
    assert r["years"] == "2025" and "00Z" in r["sample"] and r["ok"] is False and r["high"] is True
    assert r["ge50"]["n"] == 128 and r["ge75"]["n"] == 57 and r["ge75"]["ok"] is False
    # the fit hour's record stands where it is the larger miss, or the bucket's own is too thin to judge
    mild = dict(own, ge50=_grp(128, 0.66, 0.60, 21))
    assert bd.area_record(fit_hour, mild)["years"] == "2023-25" and bd.area_record(fit_hour, mild)["ok"] is False
    thin = dict(own, ge50=_grp(49, 0.683, 0.438, 12))
    assert bd.area_record(fit_hour, thin)["years"] == "2023-25" and "sample" not in bd.area_record(fit_hour, thin)
    # an area the fit hour could not judge is still judged where the bucket can
    assert bd.area_record({"judged": [], "samples": {}}, own)["ok"] is False
    assert bd.area_record(None, thin) is None and bd.area_record(None, None) is None
    # ok is false when ANY judged sample misses, whichever is shown
    fine = {"judged": ["2025"], "samples": {"2025": {"ge50": _grp(100, 0.60, 0.57, 30), "b25_50": _grp(), "ge75": _grp()}}}
    assert bd.area_record(fine, own)["ok"] is False and bd.area_record(fine, mild)["ok"] is True

    # the committed file, a build from the 02Z cycle (bucket h00, map d1 by the fallback)
    dc = bd.load_daycal()
    assert dc["slots"]["h00/j0"]["map"] == "d1"
    r = _committed_receipt(_utc(2026, 12, 8, 2), 1)
    wc = {a["name"]: a for a in r["areas"]}["West Coast"]
    slot_own = dc["slots"]["h00/j0"]["areas"]["West Coast"]["own"]
    assert wc["years"] == "2025" and "00Z" in wc["sample"], wc
    assert {k: wc["ge50"][k] for k in ("n", "shown", "obs")} == {k: slot_own["ge50"][k] for k in ("n", "shown", "obs")}
    assert wc["ge50"]["shown"] - wc["ge50"]["obs"] > 0.20 and wc["ok"] is False and wc["high"] is True
    assert wc["ge75"]["n"] >= 50 and wc["ge75"]["shown"] - wc["ge75"]["obs"] > 0.25
    # ... and a daytime build still prints a judged record there that misses (11 points over three years)
    wc9 = {a["name"]: a for a in _committed_receipt(_utc(2026, 12, 8, 9), 1)["areas"]}["West Coast"]
    assert wc9["ok"] is False and wc9["ge50"]["n"] >= 50
    # every bucket that publishes a day-level chance has every area's own record
    for sk, s in dc["slots"].items():
        for name, a in s["areas"].items():
            assert ("own" in a) == (s["map"] is not None), (sk, name)


def test_expected_hours_and_peak_hour_records_are_the_buckets_own_reach():
    """Same review: from 00Z cycles Central Valley expected hours ran 1.87x
    the observed, and the screen printed the 12Z record (x1.06, no warning)
    beside them; the peak-hour line quoted the 12Z group of 11 airport-days
    where evening cycles had 56."""
    dc = bd.load_daycal()
    cv = {a["name"]: a for a in _committed_receipt(_utc(2026, 12, 8, 2), 1)["areas"]}["Central Valley"]
    own = dc["slots"]["h00/j0"]["areas"]["Central Valley"]["own"]
    assert own["eh"]["judged"] is True and own["eh"]["ratio"] > 1.5
    assert cv["hourly"]["runs"] == "high" and cv["hourly"]["eh"]["ratio"] == own["eh"]["ratio"]
    assert "00Z" in cv["hourly"]["sample"]
    # the peak-hour record: the larger of the bucket's own cycles and the three
    # years of the family that sees a day under way (d1u at 03Z), never d1's 12Z rows
    g = cv["by_peak_hour"]["ge50"]
    kin = dc["regional"]["d1u"]["Central Valley"]["by_peak_hour"]["ge50"]
    far = dc["regional"]["d1"]["Central Valley"]["by_peak_hour"]["ge50"]
    assert g["n"] == max(own["by_peak_hour"]["ge50"]["n"], kin["n"]) and g["n"] > far["n"], (g, kin, far)
    assert g["sample"] in ("2023-25", own["sample"])
    # the rule itself
    a = {"sample": "own cycles", "by_peak_hour": {"ge50": _grp(60, 0.6, 0.3, 9), "b25_50": _grp(10, 0.3, 0.2, 4), "ge75": _grp()}}
    b = {"years": "2023-25", "by_peak_hour": {"ge50": _grp(56, 0.63, 0.39, 15), "b25_50": _grp(90, 0.35, 0.3, 20), "ge75": _grp()}}
    rec = bd.peak_hour_record(a, b)
    assert (rec["ge50"]["n"], rec["ge50"]["sample"]) == (60, "own cycles")
    assert (rec["b25_50"]["n"], rec["b25_50"]["sample"]) == (90, "2023-25") and rec["ge75"] is None
    assert bd.peak_hour_record(None, None) is None and bd.peak_hour_record(None, b)["ge50"]["sample"] == "2023-25"


def test_a_day_under_way_carries_its_record_from_the_build_on():
    """The under-way check counts fog at any hour after the cycle hour. The
    build of cycle h is on screen at h+1:25, when the hour filed as h+1 is
    over. Counted from the build on, the 03Z and 06Z samples ran 10 and 11
    points high at 50% and over (the check said 9 and 7). The verdicts are not
    changed; the record rides in the receipt so the screen can print it."""
    dc = bd.load_daycal()
    for h, sk in ((2, "h00/j0"), (4, "h03/j0"), (7, "h06/j0")):
        r = _committed_receipt(_utc(2026, 12, 8, h), 1)
        ab, c = r["after_build"], dc["slots"][sk]["check"]
        assert r["under_way"] is True and ab == c["after_build"]
        assert ab["ge50"]["n"] >= 50 and abs(ab["ge50"]["shown"] - c["ge50"]["shown"]) < 0.01
        # fog after the build is a subset of fog after the cycle: never observed more often
        assert ab["ge50"]["obs"] <= c["ge50"]["obs"] + 0.005 and ab["ge75"]["obs"] <= c["ge75"]["obs"] + 0.005, sk
    late = dc["slots"]["h06/j0"]["check"]["after_build"]["ge50"]
    assert late["shown"] - late["obs"] > 0.10, "the 06Z sample misses the 10-point rule once the first hour is left out"
    # a day ahead has no such record: the build is hours before its first counted hour
    assert "after_build" not in _committed_receipt(_utc(2026, 12, 8, 9), 1)


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


def _refused(good: dict, cases: dict) -> None:
    """Every edit of `good` must be refused by name; `good` itself must load."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "daycal.json"
        p.write_text(json.dumps(good))
        assert bd.load_daycal(p)["version"] == 3, "the unedited file must load"
        for name, edit in cases.items():
            if isinstance(edit, str):
                p.write_text(edit)
            else:
                dc = json.loads(json.dumps(good))
                edit(dc)
                p.write_text(json.dumps(dc))
            try:
                _raises_naming(p)
            except AssertionError as e:
                raise AssertionError(f"{name}: {e}") from None


def test_hand_edits_of_the_committed_file_are_refused():
    """A review (2026-11-24) hand-edited copies of this file and three edits
    loaded cleanly: one map carrying another's breakpoints, a map inflated
    1.4x, and a held map flipped to ship beside a receipt that said it failed.
    The same edits in the version-3 shape, and the ones it makes possible."""
    real = json.loads(bd.DAYCAL.read_text())
    cv = "d1@Central Valley"
    assert real["slots"]["h03/j1"]["map"] is None and cv in real["maps"], "fixture: the committed outcome changed"

    def publish_failed_slot(dc):
        s = dc["slots"]["h03/j1"]
        s.update(map="d2f", check=s["tried"][0], why=None)

    def pass_a_failed_receipt(dc):
        s = dc["slots"]["h03/j1"]
        s["tried"][0]["pass"] = True
        s.update(map="d2f", check=s["tried"][0], why=None)

    def lost_from_check(sk: str, key: str):
        """`key` gone from a slot's check AND from its copy among those tried, so the two still match."""
        def edit(dc):
            s = dc["slots"][sk]
            for c in [s["check"]] + [t for t in s["tried"] if t == s["check"]]:
                c.pop(key)
        return edit

    _refused(real, {
        "d1u carrying d1's breakpoints": lambda dc: dc["maps"]["d1u"].update(x=dc["maps"]["d1"]["x"], y=dc["maps"]["d1"]["y"]),
        "the day-1 map inflated 1.4x": lambda dc: dc["maps"]["d1"].update(
            y=[round(min(v * 1.4, 1.0), 4) for v in dc["maps"]["d1"]["y"]]),
        "an evening day-2 slot that failed, published": publish_failed_slot,
        "... with its receipt's verdict flipped": pass_a_failed_receipt,
        "the evening day-1 slot given the day-2 map": lambda dc: (
            dc["slots"]["h03/j0"]["tried"][0].update(map="d2f", map_top=dc["maps"]["d2f"]["y"][-1],
                                                     map_050=dc["maps"]["d2f"]["raw_050_maps_to"]),
            dc["slots"]["h03/j0"].update(map="d2f", check=dc["slots"]["h03/j0"]["tried"][0]),
            [a.update(use="pooled", map=None) for a in dc["slots"]["h03/j0"]["areas"].values()]),
        "a published slot withheld by hand": lambda dc: dc["slots"]["h09/j1"].update(map=None),
        "the season hold lifted from day 2 through 12Z": lambda dc: dc["slots"]["h12/j2"].update(hold_months=[]),
        "the Central Valley map floored again": lambda dc: dc["maps"][cv].update(floor=True),
        "the Central Valley map raised": lambda dc: dc["maps"][cv].update(y=[min(v * 2, 1.0) for v in dc["maps"][cv]["y"]]),
        "West Coast given the Central Valley's map": lambda dc: dc["slots"]["h09/j1"]["areas"]["West Coast"].update(
            use="regional", map=cv, check=dc["slots"]["h09/j1"]["areas"]["Central Valley"]["check"]),
        "West Coast given a map of its own that never passed": lambda dc: (
            dc["maps"].update({"d1@West Coast": dict(dc["maps"][cv], area="West Coast")}),
            dc["slots"]["h09/j1"]["areas"]["West Coast"].update(
                use="regional", map="d1@West Coast",
                check=dict(dc["slots"]["h09/j1"]["areas"]["Central Valley"]["check"], map="d1@West Coast"))),
        "an area map used where its bucket check failed": lambda dc: dc["slots"]["h09/j1"]["areas"]["Central Valley"][
            "check"].update(**{"pass": False}),
        "the service day moved to 00Z": lambda dc: dc.update(service_day_offset_h=0),
        "a slot dropped": lambda dc: dc["slots"].pop("h21/j2"),
        # 2026-11-25: the records the screen prints the larger miss from, and rule (f)
        "a day under way without its record from the build on": lambda dc: dc["slots"]["h03/j0"]["check"].pop("after_build"),
        "an area without its record on the bucket's own sample": lambda dc: dc["slots"]["h00/j0"]["areas"][
            "West Coast"].pop("own"),
        "an own record with a band missing": lambda dc: dc["slots"]["h00/j0"]["areas"]["West Coast"]["own"].pop("ge75"),
        "the Central Valley map kept though the pooled map did not run high there": lambda dc: dc["regional"]["d1"][
            "Central Valley"]["forms"]["unfloored"]["rules"].update(f_unfloored_only_where_pooled_ran_high=False),
        "an unfloored area map from before rule (f)": lambda dc: dc["regional"]["d1"]["Central Valley"]["forms"][
            "unfloored"]["rules"].pop("f_unfloored_only_where_pooled_ran_high"),
        # 2026-11-25 gate review: a file that had LOST one of these loaded cleanly. Without its Central
        # Valley entry a 02Z cycle published KVIS at 79% (its own map: 23%) with the pooled receipt.
        "a slot that lost its Central Valley entry": lambda dc: dc["slots"]["h00/j0"]["areas"].pop("Central Valley"),
        "a slot that lost every area": lambda dc: dc["slots"]["h00/j0"].update(areas={}),
        "a slot that lost the entry of an area on the pooled map": lambda dc: dc["slots"]["h00/j0"]["areas"].pop(
            "West Coast"),
        "an area on the pooled map without its flag": lambda dc: dc["regional"]["d1"]["West Coast"].pop("flag"),
        "an area on the pooled map without its record": lambda dc: dc["regional"]["d1"].pop("West Coast"),
        "a published check without its 50%-and-over group": lost_from_check("h09/j1", "ge50"),
        "a published check without its 75%-and-over group": lost_from_check("h09/j1", "ge75"),
        # the case above pops after_build from the check only, and is refused for not matching its copy
        "a day under way without its record from the build on, in the check and its copy": lost_from_check(
            "h03/j0", "after_build"),
        "last week's file": json.dumps(dict(real, version=2)),
    })


def test_malformed_daycal_raises_and_names_the_file():
    good = _area_map(_daycal(held=("h00/j1",)), "h09/j1", "Central Valley", 0.11)
    assert bd.load_daycal.__defaults__[0].name == "daycal.json"
    d1 = lambda dc: dc["maps"]["d1"]
    slot = lambda dc, sk="h09/j1": dc["slots"][sk]
    cv = lambda dc: dc["slots"]["h09/j1"]["areas"]["Central Valley"]
    _refused(good, {
        "not json": "{\"version\": 3, \"maps\": ",
        "empty": "",
        "a list": "[]",
        "wrong version": lambda dc: dc.update(version=2),      # last week's shape: keyed by planner day, not by reach
        "no maps": lambda dc: dc.pop("maps"),
        "no slots": lambda dc: dc.pop("slots"),
        "no areas": lambda dc: dc.pop("areas"),
        "no regional": lambda dc: dc.pop("regional"),
        "areas not named": lambda dc: dc.update(areas=[{"st": ["CA"]}]),
        "no service day offset": lambda dc: dc.pop("service_day_offset_h"),
        "another service day offset": lambda dc: dc.update(service_day_offset_h=12),
        "a pooled map missing": lambda dc: dc["maps"].pop("d2h09"),
        "y falls": lambda dc: d1(dc).update(x=[0.0, 0.5, 1.0], y=[0.1, 0.3, 0.2]),
        "x does not rise": lambda dc: d1(dc).update(x=[0.0, 0.5, 0.5], y=[0.1, 0.2, 0.3]),
        "y above 1": lambda dc: d1(dc).update(x=[0.0, 1.0], y=[0.1, 1.2]),
        "lengths differ": lambda dc: d1(dc).update(x=[0.0, 0.5, 1.0], y=[0.1, 0.2]),
        "one point": lambda dc: d1(dc).update(x=[0.5], y=[0.2]),
        "NaN": lambda dc: d1(dc).update(x=[0.0, 1.0], y=[0.1, float("nan")]),
        "strings": lambda dc: d1(dc).update(x=["0", "1"], y=["0.1", "0.2"]),
        "no floor flag": lambda dc: d1(dc).pop("floor"),
        "a pooled map unfloored": lambda dc: d1(dc).update(floor=False),
        "a map that is nobody's": lambda dc: dc["maps"].update(extra=_map(0.5)),
        "an area map for an unlisted area": lambda dc: dc["maps"].update(
            {"d1@Atlantis": _map(0.5, family="d1", area="Atlantis")}),
        # the three hand edits the 2026-11-24 review made that the first loader accepted
        "one map carrying another's breakpoints": lambda dc: dc["maps"]["d2h09"].update(
            x=dc["maps"]["d2h12"]["x"], y=dc["maps"]["d2h12"]["y"]),
        "a map inflated 1.4x": lambda dc: d1(dc).update(y=[0.014, 0.014]),
        "a held slot flipped to publish": lambda dc: slot(dc, "h00/j1").update(
            map="d2f", check=slot(dc, "h00/j1")["tried"][0]),
        # the slots
        "a slot missing": lambda dc: dc["slots"].pop("h06/j0"),
        "a slot too many": lambda dc: dc["slots"].update({"h00/j2": slot(dc)}),
        "a slot of the wrong kind": lambda dc: slot(dc).update(kind="evening"),
        "no hold_months": lambda dc: slot(dc).pop("hold_months"),
        "a month 13": lambda dc: slot(dc).update(hold_months=[13]),
        "no tried": lambda dc: slot(dc).pop("tried"),
        "no areas in a slot": lambda dc: slot(dc).pop("areas"),
        # a map for another reach, with a receipt that matches its breakpoints in every way
        "a map for another reach": lambda dc: (
            slot(dc)["tried"][0].update(map="d2f", map_top=0.20, map_050=0.20),
            slot(dc).update(map="d2f", check=slot(dc)["tried"][0])),
        "the wrong partial map": lambda dc: (
            slot(dc, "h09/j2")["tried"][0].update(map="d2h12", map_top=0.32, map_050=0.32),
            slot(dc, "h09/j2").update(map="d2h12", check=slot(dc, "h09/j2")["tried"][0])),
        "a published slot with no check": lambda dc: slot(dc).update(check=None),
        "a check that is not one of those tried": lambda dc: slot(dc).update(check=dict(slot(dc)["check"], n=1)),
        "a check that failed": lambda dc: (slot(dc)["tried"][0].update(**{"pass": False}),
                                           slot(dc).update(check=slot(dc)["tried"][0])),
        "a verdict its own rule results contradict": lambda dc: (
            slot(dc)["tried"][0].update(rule_b_ge50_within_10=False), slot(dc).update(check=slot(dc)["tried"][0])),
        "a receipt for other breakpoints": lambda dc: (slot(dc)["tried"][0].update(map_top=0.5),
                                                       slot(dc).update(check=slot(dc)["tried"][0])),
        "a slot withheld beside a check that passed": lambda dc: slot(dc, "h12/j1").update(map=None, check=None),
        "a season miss without the hold": lambda dc: (
            slot(dc)["tried"][0]["nov_feb"].update(ge50=_grp(80, 0.61, 0.46, 12)),
            slot(dc).update(check=slot(dc)["tried"][0])),
        # the area maps
        "an unlisted area in a slot": lambda dc: slot(dc)["areas"].update(Atlantis=cv(dc)),
        "a use that is neither": lambda dc: cv(dc).update(use="maybe"),
        "an area map in a slot that publishes nothing": lambda dc: slot(dc, "h00/j1")["areas"].update(
            {"Central Valley": cv(dc)}),
        "another family's area map": lambda dc: slot(dc, "h03/j0")["areas"].update({"Central Valley": cv(dc)}),
        # Canada has a map of its own that passed, with the same values, and is pointed at the Central Valley's
        "an area pointed at another area's map": lambda dc: (
            _area_map(dc, "h09/j1", "Canada", 0.11),
            slot(dc)["areas"]["Canada"].update(map="d1@Central Valley")),
        "an area map that is not in the file": lambda dc: dc["maps"].pop("d1@Central Valley"),
        "an area map with no leave-one-year-out record": lambda dc: dc["regional"]["d1"].pop("Central Valley"),
        "an area map whose record says it failed": lambda dc: dc["regional"]["d1"]["Central Valley"].update(
            **{"pass": False}),
        "an area map in a form that did not pass": lambda dc: dc["maps"]["d1@Central Valley"].update(floor=True),
        "an area map with a failed rule": lambda dc: dc["regional"]["d1"]["Central Valley"]["forms"]["unfloored"][
            "rules"].update(c_beats_pooled=False),
        "an area map with no check at this bucket": lambda dc: cv(dc).update(check=None),
        "an area map whose check here failed": lambda dc: cv(dc)["check"].update(**{"pass": False}),
        "an area map with other breakpoints than its check": lambda dc: dc["maps"]["d1@Central Valley"].update(
            y=[0.3, 0.3]),
    })


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
