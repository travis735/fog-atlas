#!/usr/bin/env python3
"""Tests for the hourly DEPLOY planner build (forecast/build_deploy.py: assemble and what it calls).

Run: python3 forecast/test_planner.py   (also collected by pytest)

Why it exists: until 2026-11-25 the planner payload was built once a day, by
the first forecast tick at or after 10Z. The pilot read it the evening before,
14 to 20 hours old, above an hourly curve that was fresh, and the build had
three stop-gaps for its one chance a day: fail and retry ("wait"), write
nothing ("keep"), or write anyway. Now every hourly tick builds it, and these
tests pin the rule that replaced the stop-gaps:

  - every tick writes a payload from what its own cycle supports;
  - where a cycle hour has no checked day-level chance for a date, the last
    one published for the SAME DATE is carried from the previous payload,
    with the cycle it came from, for at most 24 h, and never changed;
  - a late cycle, a missing 26-48 h bulletin, or an advisory source that
    cannot be read is named in the payload and never stops the build;
  - a broken issuance still does.

test_a_carried_chance_is_matched_by_date_not_by_position is the core one: at
the 10:00Z flip yesterday's day 2 is today's day 1, and a carry by position
would put the wrong morning's chance on screen.

Standard library only (build_deploy imports duckdb inside main(); the test
that runs main() hands it a stand-in). The one test of gefs_live() needs
numpy and says so when it cannot run.
"""
import contextlib
import io
import json
import math
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_deploy as bd  # noqa: E402
import test_daycal as td  # noqa: E402  (its synthetic day map and issuance helpers)

UTC = timezone.utc
ATLAS = td.ATLAS
CHASE = ["KVIS", "KSFO", "KDEN", "CYVR", "KBOS"]
# the committed state in fog season, as a synthetic file: day 1 from every
# cycle hour; day 2 from none (the evening view and every part-day view failed)
NO_DAY_2 = [sk for sk in bd.SLOT_KEYS if sk.endswith("j2") or sk in ("h00/j1", "h03/j1", "h06/j1")]


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def _iso(cycle: datetime) -> str:
    return cycle.strftime("%Y-%m-%dT%H:00:00Z")


def _issue(cycle: datetime, by: dict | None = None, short: bool = False, drop=(), cut: dict | None = None) -> dict:
    """A current.json for CHASE. by = {icao: {(day_offset, utc_hour): percent}},
    day_offset from the cycle's date. short: the 26-48 h bulletin did not
    arrive. drop: airports with no forecast. cut = {icao: [leads to remove]}."""
    aps = {}
    for icao in CHASE:
        if icao in drop:
            continue
        hours = td._full_cycle(cycle, (by or {}).get(icao))
        hours = {f: v for f, v in hours.items() if (f <= 25 or not short) and f not in (cut or {}).get(icao, ())}
        fhrs = sorted(hours)
        aps[icao] = {"fhrs": fhrs, "p": [[0, 0, 0, hours[f]] for f in fhrs]}
    return {"meta": {"cycle": _iso(cycle)}, "airports": aps}


def _lower(tier: str = "climo"):
    """An advisory tier for the days the hourly forecast does not reach; eh says which date it was made for."""
    return lambda icao, dd, dte: (dte.day + 0.5, tier, 0.111)


def _as_previous(payload, dc: dict):
    """Through a file and read_previous, the way forecast.yml hands the live payload to the next build."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "deploy_prev.json"
        p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return bd.read_previous(p, dc)


def _build(cycle: datetime, dc: dict, now: datetime | None = None, prev=None, by: dict | None = None,
           short: bool = False, drop=(), cut: dict | None = None, tier: str = "climo", **kw):
    """One hourly build: (payload as a reader of KV gets it, deploylog, warning lines)."""
    prev_payload, note = _as_previous(prev, dc) if prev is not None else (None, None)
    payload, log, warnings = bd.assemble(_issue(cycle, by, short, drop, cut), now or td._build(cycle), dc, ATLAS,
                                         CHASE, {}, _lower(tier), prev=prev_payload, prev_note=note, **kw)
    return json.loads(json.dumps(payload)), log, warnings


# ---- the carry ------------------------------------------------------------------------
def test_a_carried_chance_is_matched_by_date_not_by_position():
    """The core. Before the flip day 2 is Dec 9; after it day 1 is Dec 9. The
    09Z cycle has no checked chance for it here, so it carries Dec 9's own
    number (30%), never the one that sat in position 0 an hour ago (Dec 8's 60%)."""
    dc = td._daycal(held=("h09/j1",))
    before, _, _ = _build(_utc(2026, 12, 8, 8), dc, by={"KVIS": {(0, 15): 60, (1, 12): 30}})
    assert before["meta"]["day1"] == "2026-12-08" and before["meta"]["days"]["2"]["date"] == "2026-12-09"
    assert before["airports"]["KVIS"]["p"][:2] == [0.6, 0.3] and before["airports"]["KVIS"]["pfrom"] == [None, None]

    after, log, warns = _build(_utc(2026, 12, 8, 9), dc, prev=before, by={"KVIS": {(1, 12): 20}})
    a, day1 = after["airports"]["KVIS"], after["meta"]["days"]["1"]
    assert after["meta"]["day1"] == "2026-12-09" == day1["date"]
    assert a["p"][0] == 0.3, a["p"][:2]
    assert a["pfrom"] == ["2026-12-08T08:00:00Z", None]
    # everything else about the day is this cycle's own
    assert a["pk"][0] == 20 and a["eh"][0] == 0.6 and a["cov"][0] == 21 and a["tiers"][0] == "cal"
    assert day1["cycle"] == after["meta"]["cycle"] == "2026-12-08T09:00:00Z"
    c = day1["chance"]
    assert c["status"] == "carried" and c["cycle"] == "2026-12-08T08:00:00Z" and c["why"] == "hour"
    assert c["n"] == {"cal": 5, "fresh": 0, "carried": 5, "none": 0} and c["too_old"] == 0
    # the check that went out with the number comes with it: the evening view from the 06Z-08Z cycles
    r = c["carried"][0]
    assert r["cycle"] == "2026-12-08T08:00:00Z" and r["n"] == 5 and r["below_peak"] == []
    assert r["receipt"]["date"] == "2026-12-09" and r["receipt"]["slot"] == "h06/j1" and r["receipt"]["map"] == "d2f"
    # this build's own receipt for the day says it published nothing
    assert after["meta"]["daycal"]["applied"]["1"] is False and after["meta"]["daycal"]["why"]["1"] == "hour"
    assert any("day 1" in w and "carry" in w for w in warns), warns
    # the deploylog keeps this cycle's bare product beside the carried number, so the failed hour can be re-scored
    assert log["airports"]["KVIS"]["praw"][0] is not None and log["airports"]["KVIS"]["map"][0] is None
    assert log["airports"]["KVIS"]["pfrom"][0] == "2026-12-08T08:00:00Z"
    # day 2 (Dec 10) is new to the planner: nothing to carry, and it has its own fresh number here
    assert after["meta"]["days"]["2"]["chance"]["status"] == "fresh" and a["pfrom"][1] is None


def test_a_chance_is_not_carried_once_its_cycle_is_24_h_old():
    dc = td._daycal(held=("h09/j1",))
    src, _, _ = _build(_utc(2026, 12, 8, 12), dc)                # day 2 = Dec 10, through 12Z: 0.32
    assert src["airports"]["KVIS"]["p"][1] == 0.32 and src["meta"]["days"]["2"]["date"] == "2026-12-10"
    # 23 h 59 min after the 12Z cycle: carried, onto day 1 (the flip was at 10Z)
    young, _, _ = _build(_utc(2026, 12, 9, 10), dc, now=_utc(2026, 12, 9, 11, 59), prev=src)
    assert young["meta"]["days"]["1"]["date"] == "2026-12-10"
    assert young["airports"]["KVIS"]["p"][0] == 0.32 and young["airports"]["KVIS"]["pfrom"][0] == "2026-12-08T12:00:00Z"
    # 24 h after it: dropped, and the payload says there was one
    old, _, warns = _build(_utc(2026, 12, 9, 11), dc, now=_utc(2026, 12, 9, 12, 0), prev=src)
    a, c = old["airports"]["KVIS"], old["meta"]["days"]["1"]["chance"]
    assert a["p"][0] is None and a["pfrom"][0] is None and a["tiers"][0] == "cal"
    assert c["status"] == "none" and c["why"] == "hour" and c["too_old"] == 5 and c["cycle"] is None
    assert c["n"] == {"cal": 5, "fresh": 0, "carried": 0, "none": 5} and c["carried"] == []
    assert any("NO VERDICT" in w for w in warns), warns
    assert bd.CARRY_MAX_AGE_H == 24 and old["meta"]["carry"]["max_age_h"] == 24


def test_a_chain_of_carries_keeps_the_first_cycle_and_its_receipt():
    """Carried at 16:25Z from the 14Z cycle and again at 17:25Z from that
    payload: it still says 14Z, still shows the 12Z-14Z check, and still
    expires 24 h after 14Z, not 24 h after the last build that passed it on."""
    dc = td._daycal(held=("h15/j2", "h18/j2", "h21/j2", "h00/j1", "h03/j1", "h06/j1", "h09/j1", "h12/j1"))
    p = None
    for h in (14, 15, 16, 23):
        p, _, _ = _build(_utc(2026, 6, 8, h), dc, prev=p)
        a, c = p["airports"]["KVIS"], p["meta"]["days"]["2"]["chance"]
        assert p["meta"]["days"]["2"]["date"] == "2026-06-10" and a["p"][1] == 0.32, h
        if h == 14:
            assert c["status"] == "fresh" and a["pfrom"][1] is None
            continue
        assert c["status"] == "carried" and a["pfrom"][1] == "2026-06-08T14:00:00Z" == c["cycle"], h
        assert [e["cycle"] for e in c["carried"]] == ["2026-06-08T14:00:00Z"]
        assert c["carried"][0]["receipt"]["slot"] == "h12/j2" and c["carried"][0]["receipt"]["cover_end_utc"] == 12
    # across the flip (now day 1) at 13:25Z the next day it is 23 h 25 min old: still carried
    p, _, _ = _build(_utc(2026, 6, 9, 12), dc, prev=p)
    assert p["meta"]["days"]["1"]["date"] == "2026-06-10" and p["airports"]["KVIS"]["pfrom"][0] == "2026-06-08T14:00:00Z"
    assert p["meta"]["days"]["1"]["chance"]["carried"][0]["receipt"]["slot"] == "h12/j2"
    # one more tick is 24 h 25 min after the 14Z cycle, though only an hour after the last build
    p, _, _ = _build(_utc(2026, 6, 9, 13), dc, prev=p)
    assert p["airports"]["KVIS"]["p"][0] is None and p["meta"]["days"]["1"]["chance"]["too_old"] == 5


def test_a_carried_chance_below_the_new_peak_hour_is_flagged_and_left_alone():
    """p and pk must not argue. A carried number is what an older cycle said;
    when the hourly forecast has risen above it the build neither raises it
    (no check covers a day-level number at this cycle hour) nor drops it. It
    lists the field, and the screen says the forecast has risen since."""
    dc = td._area_map(td._daycal(held=("h15/j2",)), "h12/j2", "Central Valley", 0.07, floor=False)
    src, _, _ = _build(_utc(2026, 6, 8, 14), dc, by={"KSFO": {(2, 6): 10}, "KVIS": {(2, 6): 10}})
    assert src["airports"]["KSFO"]["p"][1] == 0.32 and src["airports"]["KVIS"]["p"][1] == 0.07
    new, _, _ = _build(_utc(2026, 6, 8, 15), dc, prev=src,
                       by={"KSFO": {(2, 6): 45}, "KVIS": {(2, 6): 45}, "KDEN": {(2, 6): 32}})
    c = new["meta"]["days"]["2"]["chance"]["carried"][0]
    for icao, p in (("KSFO", 0.32), ("KVIS", 0.07), ("KDEN", 0.32)):
        a = new["airports"][icao]
        assert a["p"][1] == p and a["pk"][1] in (45, 32) and a["pfrom"][1] == "2026-06-08T14:00:00Z", icao
    # KSFO: floored map, 32% carried, peak hour now 45%. KDEN: 32% against a 32% peak is not below it.
    # KVIS: its area map is not floored at the peak hour, so p under pk is that map's normal state.
    assert c["below_peak"] == ["KSFO"], c["below_peak"]


def test_nothing_is_carried_from_a_payload_that_is_absent_malformed_or_older_than_the_hourly_build():
    dc = td._daycal(held=("h09/j1",))
    good, _, _ = _build(_utc(2026, 12, 8, 8), dc, by={"KVIS": {(1, 12): 30}})
    text = json.dumps(good)

    def carried(prev):
        p, _, _ = _build(_utc(2026, 12, 8, 9), dc, prev=prev)
        return p["airports"]["KVIS"]["p"][0], p["meta"]["carry"]["previous"]["status"], p

    assert carried(good)[:2] == (0.3, "ok")
    # what `wrangler kv key get` has printed before the value on this project
    assert carried(" ⛅️ wrangler 4.167.0\n-------------------\n" + text)[:2] == (0.3, "ok")
    old_format = json.loads(text)
    del old_format["meta"]["format"]                           # every payload before 2026-11-25 (bare-product p)
    other_map = json.loads(text)
    other_map["meta"]["daycal"]["map_file"] = "2026-11-24T02:00Z"
    v2 = json.loads(text)
    v2["meta"]["daycal"]["version"] = 2
    no_dates = json.loads(text)
    no_dates["meta"]["day1"] = "tomorrow"
    for prev, want in (("", "unreadable"), ("Value not found", "unreadable"), (text[:len(text) // 2], "unreadable"),
                       ("[1, 2]", "unreadable"), ('{"error": "no deploy data yet"}', "malformed"),
                       ({"meta": {}, "airports": []}, "malformed"), (old_format, "old_format"),
                       (other_map, "other_daymap"), (v2, "other_daymap"), (no_dates, "malformed")):
        p0, status, p = carried(prev)
        assert (p0, status) == (None, want), (want, p0, status)
        assert p["airports"]["KVIS"]["pfrom"] == [None, None] and p["meta"]["days"]["1"]["chance"]["status"] == "none"
    # no file at all: the first build, or the KV read failed
    for path in (None, "/nonexistent/deploy_prev.json"):
        prev, note = bd.read_previous(path, dc)
        assert prev is None and note["status"] == "absent" and note["generated"] is None, path
    p, _, _ = bd.assemble(_issue(_utc(2026, 12, 8, 9)), _utc(2026, 12, 8, 10, 25), dc, ATLAS, CHASE, {}, _lower())
    assert p["airports"]["KVIS"]["p"][0] is None and p["meta"]["carry"]["previous"]["status"] == "absent"

    # a payload that reads fine with one bad airport entry: that airport is not carried, the rest are
    def one_bad(edit) -> dict:
        prev = json.loads(text)
        edit(prev["airports"], prev["meta"])
        return _build(_utc(2026, 12, 8, 9), dc, prev=prev)[0]

    for name, edit in {
        "a short p list": lambda a, m: a["KVIS"].update(p=[0.6]),
        "a chance that is text": lambda a, m: a["KVIS"]["p"].__setitem__(1, "0.3"),
        "a chance above 1": lambda a, m: a["KVIS"]["p"].__setitem__(1, 1.7),
        "a chance that is true": lambda a, m: a["KVIS"]["p"].__setitem__(1, True),
        "not a number": lambda a, m: a["KVIS"]["p"].__setitem__(1, float("nan")),
        "no tiers": lambda a, m: a["KVIS"].pop("tiers"),
        "a day that was not on the forecast tier": lambda a, m: a["KVIS"]["tiers"].__setitem__(1, "climo"),
        "an entry that is a list": lambda a, m: a.update(KVIS=[1, 2, 3]),
        "a source cycle that is not a time": lambda a, m: a["KVIS"].update(pfrom=[None, "yesterday"]),
        "a source cycle in the future": lambda a, m: a["KVIS"].update(pfrom=[None, "2026-12-09T08:00:00Z"]),
    }.items():
        p = one_bad(edit)
        assert p["airports"]["KVIS"]["p"][0] is None and p["airports"]["KSFO"]["p"][0] == 0.2, name
        assert p["meta"]["days"]["1"]["chance"]["n"] == {"cal": 5, "fresh": 0, "carried": 4, "none": 1}, name
    # a number whose receipt is not in the payload is not carried at all
    p = one_bad(lambda a, m: m["daycal"]["days"].pop("2"))
    assert p["meta"]["days"]["1"]["chance"]["n"]["carried"] == 0 and p["airports"]["KSFO"]["p"][0] is None
    # an advisory day (3-14) of the previous payload is never carried as a day-level chance: Dec 9 was its day 4
    far, _, _ = _build(_utc(2026, 12, 5, 9), dc, now=_utc(2026, 12, 5, 10, 25))
    assert far["meta"]["day1"] == "2026-12-06" and far["airports"]["KVIS"]["p"][3] == 0.111
    p, _, _ = _build(_utc(2026, 12, 8, 9), dc, prev=far)
    assert p["airports"]["KVIS"]["p"][0] is None
    # a "previous" payload from a cycle after this build's own clock (a wrong clock, a rehearsal left
    # in KV): its Dec 9 chance is real and has its receipt, and it is still not carried
    held = td._daycal(held=("h06/j1",))
    p, _, _ = _build(_utc(2026, 12, 8, 6), held, now=_utc(2026, 12, 8, 7, 25), prev=good)
    assert p["meta"]["days"]["2"]["date"] == "2026-12-09" and p["meta"]["carry"]["previous"]["status"] == "ok"
    assert p["airports"]["KVIS"]["p"][1] is None and p["meta"]["days"]["2"]["chance"]["n"]["carried"] == 0


def test_no_chance_and_nothing_to_carry_is_a_null_with_its_reason():
    dc = td._daycal(held=NO_DAY_2)
    before, _, _ = _build(_utc(2026, 12, 8, 8), dc)
    after, _, warns = _build(_utc(2026, 12, 8, 9), dc, prev=before, by={"KVIS": {(2, 6): 40}})
    a, d2 = after["airports"]["KVIS"], after["meta"]["days"]["2"]
    # day 2 (Dec 10) from the 09Z cycle: its hours through 09Z, its peak hour, and no day-level chance
    assert a["tiers"][1] == "cal" and a["p"][1] is None and a["pfrom"][1] is None
    assert a["pk"][1] == 40 and a["eh"][1] == 1.2 and a["cov"][1] == 9
    assert d2["date"] == "2026-12-10" and d2["cover"] == {"from": 0, "through": 9}
    assert d2["chance"] == {"status": "none", "cycle": None, "why": "hour", "too_old": 0, "carried": [],
                            "n": {"cal": 5, "fresh": 0, "carried": 0, "none": 5}}
    # day 1 is this cycle's own, so nothing about it is a warning
    assert after["meta"]["days"]["1"]["chance"]["status"] == "fresh" and warns == []
    assert after["meta"]["days"]["1"]["chance"]["cycle"] == "2026-12-08T09:00:00Z"


def test_an_airport_that_lost_its_hours_keeps_its_own_last_chance():
    """One field short of hours in this cycle: it carries, the others are fresh, and the day still reads fresh."""
    dc = td._daycal()
    before, _, _ = _build(_utc(2026, 12, 8, 11), dc, by={"KDEN": {(1, 12): 30}})
    assert before["airports"]["KDEN"]["p"][0] == 0.3
    after, _, warns = _build(_utc(2026, 12, 8, 12), dc, prev=before, cut={"KDEN": [14, 15, 16, 17]})
    c = after["meta"]["days"]["1"]["chance"]
    assert after["airports"]["KDEN"]["p"][0] == 0.3 and after["airports"]["KDEN"]["pfrom"][0] == "2026-12-08T11:00:00Z"
    assert after["airports"]["KSFO"]["pfrom"][0] is None
    assert c["status"] == "fresh" and c["cycle"] == "2026-12-08T12:00:00Z" and c["why"] is None
    assert c["n"] == {"cal": 5, "fresh": 4, "carried": 1, "none": 0} and warns == []
    assert [(e["cycle"], e["n"]) for e in c["carried"]] == [("2026-12-08T11:00:00Z", 1)]


# ---- the service day in the payload --------------------------------------------------------
def test_the_payload_just_before_and_just_after_the_10z_flip():
    dc = td._daycal(held=NO_DAY_2)
    before, _, _ = _build(_utc(2026, 12, 8, 8), dc)                                   # the 09:25Z tick
    after, _, _ = _build(_utc(2026, 12, 8, 9), dc, prev=before)                       # the 10:25Z tick
    m = before["meta"]
    assert (m["service_day"], m["day1"], m["next_flip"]) == ("2026-12-07", "2026-12-08", "2026-12-08T10:00:00Z")
    assert m["generated"] == "2026-12-08T09:25:00Z" and m["cycle"] == "2026-12-08T08:00:00Z" and m["format"] == 2
    d1, d2 = m["days"]["1"], m["days"]["2"]
    assert (d1["date"], d1["kind"], d1["under_way"], d1["cover"]) == ("2026-12-08", "under_way", True, {"from": 9, "through": 23})
    assert (d2["date"], d2["kind"], d2["under_way"], d2["cover"]) == ("2026-12-09", "evening", False, {"from": 0, "through": 21})
    assert d1["chance"]["status"] == "fresh" and d2["chance"]["status"] == "none" and d2["chance"]["why"] == "hour"
    m = after["meta"]
    assert (m["service_day"], m["day1"], m["next_flip"]) == ("2026-12-08", "2026-12-09", "2026-12-09T10:00:00Z")
    d1, d2 = m["days"]["1"], m["days"]["2"]
    assert (d1["date"], d1["kind"], d1["under_way"], d1["cover"]) == ("2026-12-09", "ahead", False, {"from": 0, "through": 21})
    assert (d2["date"], d2["kind"], d2["cover"]) == ("2026-12-10", "partial", {"from": 0, "through": 9})
    assert d1["chance"]["status"] == "fresh" and d1["chance"]["cycle"] == "2026-12-08T09:00:00Z"
    assert m["daycal"]["service_day"] == m["service_day"] and m["daycal"]["days"]["1"]["date"] == m["day1"]
    # the same 08Z cycle one second either side of 10:00:00Z: only the clock moves the days
    for now, sd, day1 in ((_utc(2026, 12, 8, 9, 59, 59), "2026-12-07", "2026-12-08"),
                          (_utc(2026, 12, 8, 10, 0, 0), "2026-12-08", "2026-12-09")):
        p, _, _ = _build(_utc(2026, 12, 8, 8), dc, now=now)
        assert (p["meta"]["service_day"], p["meta"]["day1"], p["meta"]["days"]["1"]["date"]) == (sd, day1, day1), now
        assert len(p["airports"]["KVIS"]["eh"]) == 14


def test_every_hourly_tick_of_a_day_writes_a_payload_with_the_committed_day_map():
    """The committed daycal.json through 24 ticks, each carrying from the one
    before. No tick stops or keeps the old payload (the daily build's "wait"
    and "keep"), day 1 is this cycle's own at every hour, and in December
    day 2 has no day-level chance from any hour and nothing to carry."""
    dc = bd.load_daycal()
    prev = None
    for h in range(24):
        cyc = _utc(2026, 12, 8, h)
        p, _, warns = _build(cyc, dc, prev=prev, by={"KBOS": {(0, 20): 30, (1, 11): 30, (2, 6): 30}})
        sd = bd.service_day(td._build(cyc))
        assert p["meta"]["service_day"] == sd.isoformat() and p["meta"]["cycle"] == _iso(cyc), h
        d1, d2 = p["meta"]["days"]["1"], p["meta"]["days"]["2"]
        assert d1["date"] == (sd + timedelta(days=1)).isoformat() and d2["date"] == (sd + timedelta(days=2)).isoformat()
        assert d1["chance"]["status"] == "fresh" and d1["chance"]["n"]["fresh"] == 5, (h, d1["chance"])
        # "season": the 12Z-14Z and 18Z-20Z views of day 2 pass overall and are held for November-February
        assert d2["chance"]["status"] == "none", (h, d2["chance"])
        assert d2["chance"]["why"] == ("season" if h in (12, 13, 14, 18, 19, 20) else "hour"), (h, d2["chance"])
        assert p["airports"]["KBOS"]["p"][0] is not None and p["airports"]["KBOS"]["p"][1] is None, h
        assert p["airports"]["KBOS"]["tiers"][:3] == ["cal", "cal", "climo"], h
        assert p["meta"]["carry"]["previous"]["status"] == ("ok" if prev else "absent") and warns == [], (h, warns)
        prev = p
    assert not hasattr(bd, "bake_decision")


# ---- gaps: named, never a reason to stop -----------------------------------------------------
def test_a_build_without_the_26_48_h_bulletin_still_publishes_and_names_the_gap():
    """The engine fetches the 26-48 h bulletin once and goes on without it.
    The daily build failed here ("wait") so the next tick retried; an hourly
    build that failed would leave the planner stale for as long as NOAA is slow."""
    dc = td._daycal()
    before, _, _ = _build(_utc(2026, 12, 8, 11), dc, by={"KVIS": {(1, 12): 30}})
    assert before["airports"]["KVIS"]["p"][0] == 0.3 and before["meta"]["gaps"] == []
    after, _, warns = _build(_utc(2026, 12, 8, 12), dc, prev=before, short=True, by={"KVIS": {(1, 5): 40}})
    a, d1 = after["airports"]["KVIS"], after["meta"]["days"]["1"]
    # what the cycle has is fresh: tomorrow through 13Z
    assert a["tiers"][0] == "cal" and a["eh"][0] == 0.4 and a["pk"][0] == 40 and a["cov"][0] == 13
    assert d1["cover"] == {"from": 0, "through": 21} and d1["short"] == 5
    # the chance is the 11Z cycle's, and it is below the new peak hour, so it is listed as such
    assert a["p"][0] == 0.3 and a["pfrom"][0] == "2026-12-08T11:00:00Z"
    c = d1["chance"]
    assert c["status"] == "carried" and c["why"] == "hours" and c["carried"][0]["below_peak"] == ["KVIS"]
    assert [g["code"] for g in after["meta"]["gaps"]] == ["short_issuance"]
    g = after["meta"]["gaps"][0]
    # the panel prints this sentence as it is: its own words and a weekday date, no "cycle", "bulletin" or ISO date
    assert g["hours"] == 25 and g["text"] == ("The 12Z forecast arrived without its second day: it ends "
                                              "Wed 9 Dec at 13Z.")
    assert g["text"] in warns and any("day 1" in w for w in warns)
    # the day after has no forecast hour at all in this issuance: it is not a forecast day, and nothing is carried onto it
    assert a["tiers"][1] == "climo" and a["cov"][1] is None and a["pk"][1] is None and a["pfrom"][1] is None
    # with nothing to carry from it is a null and a reason, still not a failure
    alone, _, warns = _build(_utc(2026, 12, 8, 12), dc, short=True)
    c = alone["meta"]["days"]["1"]["chance"]
    assert alone["airports"]["KVIS"]["p"][0] is None and c["status"] == "none" and c["why"] == "hours"
    assert any("NO VERDICT" in w for w in warns)


def test_a_late_cycle_is_named_and_still_built():
    """10:25Z and only the 08Z cycle is on the mirror. The daily build wrote
    nothing ("keep"); this one builds the new day 1 from what 08Z says of it."""
    dc = td._daycal(held=("h06/j1",))
    before, _, _ = _build(_utc(2026, 12, 8, 8), dc)                                   # 09:25Z, on time
    late, _, warns = _build(_utc(2026, 12, 8, 8), dc, now=_utc(2026, 12, 8, 10, 25), prev=before,
                            by={"KVIS": {(1, 12): 30}})
    m = late["meta"]
    assert m["day1"] == "2026-12-09" and m["days"]["1"]["kind"] == "evening"
    assert [g["code"] for g in m["gaps"]] == ["late_cycle"] and m["gaps"][0]["hours"] == 1
    assert m["gaps"][0]["text"] == "Built at 10:25Z from the 08Z forecast: the 09Z forecast had not arrived."
    assert m["gaps"][0]["text"] in warns
    a = late["airports"]["KVIS"]
    assert a["tiers"][0] == "cal" and a["pk"][0] == 30 and a["p"][0] is None
    assert m["days"]["1"]["chance"]["status"] == "none" and m["days"]["1"]["chance"]["why"] == "hour"
    # an on-time tick has no gap
    assert before["meta"]["gaps"] == []


def test_a_broken_issuance_still_stops_the_build():
    dc = td._daycal()
    now = _utc(2026, 12, 8, 10, 25)

    def refused(current, when=now) -> str:
        try:
            bd.assemble(current, when, dc, ATLAS, CHASE, {}, _lower())
        except RuntimeError as e:
            assert "deploy.json was not written" in str(e), e
            return str(e)
        raise AssertionError("built a payload from a broken issuance")

    good = _issue(_utc(2026, 12, 8, 9))
    bd.assemble(good, now, dc, ATLAS, CHASE, {}, _lower())
    # the 2026-11 outage: a full-size bulletin parsed to nothing, for 21 days
    assert "0 of 5" in refused({"meta": good["meta"], "airports": {}})
    assert "1 of 5" in refused(_issue(_utc(2026, 12, 8, 9), drop=("KSFO", "KDEN", "CYVR", "KBOS")))
    empty = _issue(_utc(2026, 12, 8, 9))
    for i in ("KSFO", "KDEN", "CYVR", "KBOS"):
        empty["airports"][i] = {"fhrs": [], "p": []}
    assert "1 of 5" in refused(empty)
    bd.assemble(_issue(_utc(2026, 12, 8, 9), drop=("KSFO", "KDEN", "CYVR")), now, dc, ATLAS, CHASE, {}, _lower())
    # a leftover current.json: the engine looks back 6 h, so 8 h is not this run's
    assert "not" in refused(_issue(_utc(2026, 12, 8, 2))) and "2026-12-08T02:00:00Z" in refused(_issue(_utc(2026, 12, 8, 2)))
    bd.assemble(_issue(_utc(2026, 12, 8, 3)), now, dc, ATLAS, CHASE, {}, _lower())
    refused(_issue(_utc(2026, 12, 6, 9)))
    refused(_issue(_utc(2026, 12, 8, 11)))                      # a cycle after the build: the clock is wrong
    refused({"airports": good["airports"]})
    refused({"meta": {"cycle": "soon"}, "airports": good["airports"]})
    refused([])
    assert bd.MAX_CYCLE_AGE_H == 8
    # main() runs the same check on its own before any download, so a broken issuance is red in seconds
    assert bd.check_issuance(good, CHASE, now) == _utc(2026, 12, 8, 9)
    for bad in ({"meta": good["meta"], "airports": {}}, empty, _issue(_utc(2026, 12, 8, 2)), []):
        try:
            bd.check_issuance(bad, CHASE, now)
        except RuntimeError as e:
            assert "deploy.json was not written" in str(e)
        else:
            raise AssertionError("check_issuance passed a broken issuance")


# ---- days 3-14 ----------------------------------------------------------------------------------
def test_days_3_14_keep_the_last_builds_values_when_a_source_cannot_be_read():
    dc = td._daycal(held=NO_DAY_2)
    ok = {"nbe": {"ok": True, "cycle": "2026-12-08T07:00Z", "airports": 5}, "gefs": None,
          "cpc": {"ok": True, "products": ["610prcp", "814prcp"]}}
    down = dict(ok, nbe={"ok": False, "cycle": None, "airports": 0})
    first, _, _ = _build(_utc(2026, 12, 8, 8), dc, tier="fit", inputs=ok)             # 09:25Z, day 3 = Dec 10
    assert first["meta"]["advisory"]["status"] == "fresh" and first["meta"]["advisory"]["built"] == "2026-12-08T09:25:00Z"
    assert first["airports"]["KVIS"]["tiers"][2:] == ["fit"] * 12 and first["airports"]["KVIS"]["eh"][2] == 10.5
    assert first["meta"]["nbe_cycle"] == "2026-12-08T07:00Z"

    # 10:25Z, after the flip (day 3 = Dec 11), NBE unreachable: this build could only make climatology
    second, _, warns = _build(_utc(2026, 12, 8, 9), dc, prev=first, tier="climo", inputs=down)
    a, adv = second["airports"]["KVIS"], second["meta"]["advisory"]
    # Dec 11..21 keep the fitted values made for THOSE dates; Dec 22 is new to the planner and is climatology
    assert a["tiers"][2:] == ["fit"] * 11 + ["climo"] and a["eh"][2:] == [d + 0.5 for d in range(11, 23)], a["eh"]
    assert a["tiers"][:2] == ["cal", "cal"] and a["eh"][:2] == [0.0, 0.0]            # never a forecast day
    assert adv["status"] == "reused" and adv["built"] == "2026-12-08T09:25:00Z" and adv["missing"] == ["nbe"]
    assert adv["reused"] == {"values": 55, "airports": 5, "from_generated": "2026-12-08T09:25:00Z"}
    g = [g for g in second["meta"]["gaps"] if g["code"] == "advisory"]
    assert len(g) == 1 and g[0]["missing"] == ["nbe"] and "NBM extended" in g[0]["text"] and g[0]["text"] in warns
    assert second["meta"]["nbe_cycle"] is None
    # an hour later still down: the values are still the 09:25Z build's and say so
    third, _, _ = _build(_utc(2026, 12, 8, 10), dc, prev=second, tier="climo", inputs=down)
    assert third["meta"]["advisory"]["built"] == "2026-12-08T09:25:00Z" and third["airports"]["KVIS"]["tiers"][2] == "fit"
    assert third["meta"]["advisory"]["reused"]["values"] == 55
    # 24 h after the build that computed them they are dropped: lower tier, and the payload says degraded
    stale, _, warns = _build(_utc(2026, 12, 9, 8), dc, now=_utc(2026, 12, 9, 9, 25), prev=third, tier="climo", inputs=down)
    assert stale["meta"]["advisory"]["status"] == "degraded" and stale["meta"]["advisory"]["reused"] is None
    assert stale["airports"]["KVIS"]["tiers"][2:] == ["climo"] * 12 and any("lower tier" in w for w in warns)
    # every source read: this build's own values, whatever the previous payload had
    fresh, _, _ = _build(_utc(2026, 12, 8, 9), dc, prev=first, tier="nbe", inputs=ok)
    assert fresh["airports"]["KVIS"]["tiers"][2:] == ["nbe"] * 12 and fresh["meta"]["advisory"]["status"] == "fresh"
    assert fresh["meta"]["gaps"] == []
    # a previous value on a lower or equal tier is never taken over a better one from this build
    worse, _, _ = _build(_utc(2026, 12, 8, 8), dc, tier="climo", inputs=ok)
    kept, _, _ = _build(_utc(2026, 12, 8, 9), dc, prev=worse, tier="cpc", inputs=down)
    assert kept["airports"]["KVIS"]["tiers"][2:] == ["cpc"] * 12 and kept["meta"]["advisory"]["status"] == "degraded"
    # nothing to reuse from (the first build ever, or an old-format payload): degraded, never a failure
    alone, _, _ = _build(_utc(2026, 12, 8, 9), dc, tier="climo", inputs=down)
    assert alone["meta"]["advisory"]["status"] == "degraded" and alone["airports"]["KVIS"]["tiers"][2] == "climo"


# ---- the verification status -----------------------------------------------------------------------
def test_every_airport_says_how_the_hourly_forecast_has_verified_there():
    report = {"generated": "2026-11-01T17:57:06.463469+00:00", "runs": 928,
              "bar": {"rule": "n>=3000 pairs AND >=10 v10 event-hours; skill>0 on v10 AND sub",
                      "pass": [{"icao": "KSFO", "n": 12826}, {"icao": "KZZZ", "n": 9000}],
                      "fail": [{"icao": "KDEN", "n": 12694}], "insufficient_n": 3}}
    dc = td._daycal()
    p, log, _ = _build(_utc(2026, 12, 8, 9), dc, drop=("KBOS",), scorecard=report)
    assert {i: a["bar"] for i, a in p["airports"].items()} == {
        "KVIS": "few", "KSFO": "pass", "KDEN": "fail", "CYVR": "few", "KBOS": "few"}
    b = p["meta"]["bar"]
    assert b["available"] is True and b["scored"] == "2026-11-01" and b["runs"] == 928
    assert b["rule"] == report["bar"]["rule"] and set(b["codes"]) == {"pass", "fail", "few"} and b["rule_plain"]
    assert b["counts"] == {"pass": 1, "fail": 1, "few": 3}      # every airport in the payload
    assert b["counts_cal"] == {"pass": 1, "fail": 1, "few": 2}  # KBOS has no hourly forecast in this issuance
    assert log["airports"]["KSFO"]["bar"] == "pass"
    # without a scorecard the status is absent and says so; the planner is still built
    p, _, _ = _build(_utc(2026, 12, 8, 9), dc, scorecard=None, scorecard_why="shadow_report.json not found")
    assert all(a["bar"] is None for a in p["airports"].values())
    assert p["meta"]["bar"] == {"available": False, "why": "shadow_report.json not found"}
    # the committed scorecard loads, and no airport is on both lists
    r, why = bd.load_scorecard()
    assert r is not None and why == "", why
    ok, bad = {x["icao"] for x in r["bar"]["pass"]}, {x["icao"] for x in r["bar"]["fail"]}
    assert ok and bad and not ok & bad and isinstance(r["bar"]["rule"], str)
    # a scorecard with nobody scored would label every field "few" and look fine: it is refused instead
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "shadow_report.json"
        for text in (json.dumps(dict(report, bar=dict(report["bar"], **{"pass": [], "fail": []}))), "{}", "not json",
                     json.dumps(dict(report, bar=dict(report["bar"], rule=None))), json.dumps({"bar": {"pass": [1]}})):
            f.write_text(text)
            r, why = bd.load_scorecard(f)
            assert r is None and str(f) in why, text[:40]
        assert bd.load_scorecard(Path(d) / "absent.json")[0] is None


# ---- the assembly --------------------------------------------------------------------------------------
def test_the_assembly_on_a_small_fixture():
    dc = td._daycal(held=NO_DAY_2)
    cur = _issue(_utc(2026, 12, 8, 9), {"KVIS": {**{(1, h): 11 for h in range(4, 10)}, (2, 6): 40}}, drop=("KBOS",))
    now = _utc(2026, 12, 8, 10, 25, 7)
    payload, log, warnings = bd.assemble(cur, now, dc, ATLAS, CHASE, {("KVIS", 12): 5}, _lower("nbe"))
    json.dumps(payload), json.dumps(log)                        # both must serialise as they are
    assert set(payload) == {"meta", "airports"} and list(payload["airports"]) == CHASE and warnings == []
    m = payload["meta"]
    assert set(m) == {"format", "generated", "cycle", "service_day", "day1", "next_flip", "days", "carry", "gaps",
                      "advisory", "bar", "nbe_cycle", "daycal", "tiers", "dayscale_skill"}
    assert m["generated"] == "2026-12-08T10:25:07Z" and m["format"] == bd.PAYLOAD_FORMAT == 2
    assert set(m["days"]) == {"1", "2"} and set(m["days"]["1"]) == {"date", "kind", "under_way", "cycle", "cover",
                                                                    "short", "chance"}
    assert set(m["days"]["1"]["chance"]) == {"status", "cycle", "why", "n", "too_old", "carried"}
    assert set(m["carry"]) == {"max_age_h", "rule", "previous"} and set(m["advisory"]) >= {"status", "built", "reused",
                                                                                       "missing", "inputs", "rule"}
    assert m["daycal"]["version"] == 3 and "map_file" in m["daycal"] and set(m["daycal"]["days"]) == {"1", "2"}
    a = payload["airports"]["KVIS"]
    assert set(a) == {"eh", "tiers", "p", "win", "ws", "pk", "cov", "ehc", "pfrom", "bar"}
    assert all(len(a[k]) == 14 for k in ("eh", "tiers", "p", "win", "ws", "pk"))
    assert len(a["cov"]) == len(a["pfrom"]) == len(a["ehc"]) == 2
    assert a["tiers"] == ["cal", "cal"] + ["nbe"] * 12 and a["p"][0] == 0.11 and a["pk"][:2] == [11, 40]
    assert a["eh"][:3] == [0.66, 1.2, 11.5] and a["cov"] == [21, 9]
    # the window is cut at 40% of the issuance's peak (16% here): day 1's 11% hours are outside it, so it
    # shows the climatological window; day 2's one 40% point is three hours, starting at lead 45
    assert a["ws"][:2] == [None, 45] and a["win"][:3] == [5, 3, 5]
    assert a["p"][1] is None and a["p"][2:] == [0.111] * 12
    # an airport the issuance does not cover: the lower tier on every day, no peak hour, no coverage
    b = payload["airports"]["KBOS"]
    assert b["tiers"] == ["nbe"] * 14 and b["cov"] == [None, None] and b["pk"] == [None] * 14 and b["pfrom"] == [None, None]
    assert m["days"]["1"]["chance"]["n"]["cal"] == 4
    # the bare product, the pooled value and the map name ride in the log only
    assert set(log["airports"]["KVIS"]) - set(a) == {"praw", "ppool", "map"} and log["meta"] is m
    assert abs(log["airports"]["KVIS"]["praw"][0] - 0.503) < 0.001 and log["airports"]["KVIS"]["map"] == ["d1", None]
    assert log["airports"]["KVIS"]["praw"][1] is not None       # the slot that published nothing can be re-scored


def test_a_day_2_seen_in_part_carries_the_climatology_of_the_hours_it_does_not_reach():
    """2026-11-25 review: day 2 went onto the "cal" tier with eh summed over
    the hours the cycle covers, so the 10:25Z build (09Z cycle, day 2 through
    09Z) counted 31% of the West Coast's day-2 hours and 66% of the
    Southeast's in every 7- and 14-day total, and KVIS day 2 read 0.75 h at
    10:25Z and 2.70 h six hours later for the same date. eh stays the
    forecast's own sum; ehc carries the climatological hours of the UTC hours
    past the forecast, for a total to add."""
    dc = td._daycal(held=NO_DAY_2)
    asked = []

    def fill(icao, dte, hours):
        asked.append((icao, dte.isoformat(), list(hours)))
        return 0.1 * len(hours)                                 # 0.1 expected hour per UTC hour, flat

    def build(h, **kw):
        cyc = _utc(2026, 12, 8, h)
        return bd.assemble(_issue(cyc, {"KVIS": {(2, 6): 40}}, **kw), td._build(cyc), dc, ATLAS, CHASE, {}, _lower(),
                           fill=fill)[0]["airports"]["KVIS"]
    # 09Z cycle: day 2 (Dec 10) is 00Z, 03Z, 06Z, 09Z, twelve hour-equivalents, so 12Z-23Z is not reached
    a = build(9)
    assert a["cov"] == [21, 9] and a["eh"][1] == 1.2 and a["ehc"] == [None, 1.2]
    assert ("KVIS", "2026-12-10", list(range(12, 24))) in asked
    # 12Z: through 12Z, nine hours left; 15Z: through 15Z, six; 18Z: three; 21Z: the whole date
    assert [build(h)["ehc"][1] for h in (12, 15, 18, 21)] == [0.9, 0.6, 0.3, None]
    # an evening build's day 2 is the whole next date: nothing to add
    assert build(3)["ehc"] == [None, None] and build(7)["ehc"] == [None, None]
    # a day UNDER WAY is never filled: the hours it is missing are behind it (03Z cycle, day 1 = its own date)
    assert not [q for q in asked if q[1] == "2026-12-08"]
    # no forecast hour of day 2 at all (the 26-48 h bulletin did not arrive): the whole day is the lower tier's, no ehc
    b = build(12, short=True)
    assert b["tiers"][1] == "climo" and b["ehc"] == [None, None]
    # without a climatology table nothing is added and nothing breaks
    cyc = _utc(2026, 12, 8, 9)
    assert bd.assemble(_issue(cyc), td._build(cyc), dc, ATLAS, CHASE, {}, _lower())[0]["airports"]["KVIS"]["ehc"] == [None, None]
    # the hours listed stand for what the cycle really leaves out, at every daytime cycle hour
    for h in range(9, 24):
        cyc = _utc(2026, 12, 8, h)
        days, info = bd.cal_tier(_issue(cyc), bd.service_day(td._build(cyc)), dc, ATLAS)
        unc, through = days[("KVIS", 2)]["unc"], info["days"]["2"]["cover"]["through"]
        # the three-hourly point at T is summed as three hours (T, T+1, T+2): the first hour not reached is T+3
        assert unc == list(range(through + 3, 24)) and (not unc) == (through == 21), (h, unc, through)
        if h <= 22:     # all of day 2 is three-hourly points here: what is summed and what is added make 24 hours
            assert bd.expected_used(cyc, _utc(2026, 12, 10).date()) + len(unc) == 24, h


def test_the_same_gefs_lead_feeds_the_same_date_on_both_sides_of_the_flip():
    """2026-11-25 review: GEFS leads were filed by position (f072 = day 3)
    while days were counted from the service day. A build at 08:13Z on the
    25th (service day the 24th, newest GEFS cycle the 25th 00Z) fed day 3,
    the 27th, the ensemble valid on the 28th 00Z. The 10:25Z build of the same
    cycle fed that lead to the 28th. Of 4,180 airport-days it moved the fitted
    chance by 10 points or more at 16."""
    cyc = _utc(2026, 11, 25, 0)
    before, after = bd.service_day(_utc(2026, 11, 25, 7, 25)), bd.service_day(_utc(2026, 11, 25, 10, 25))
    assert (before.isoformat(), after.isoformat()) == ("2026-11-24", "2026-11-25")
    by_date = lambda sd: {(sd + timedelta(days=dd)).isoformat(): fh for dd, fh in bd.gefs_leads(cyc, sd).items()}
    b, a = by_date(before), by_date(after)
    assert all(b[d] == a[d] for d in set(a) & set(b)) and len(set(a) & set(b)) == 5
    assert a == {"2026-11-28": 72, "2026-11-29": 96, "2026-11-30": 120, "2026-12-01": 144, "2026-12-02": 168,
                 "2026-12-03": 192}
    # before the flip day 3 is the 27th, 48 h from the 00Z cycle: it has a lead of its own, not the 28th's
    assert b["2026-11-27"] == 48 and b["2026-11-28"] == 72 and sorted(bd.gefs_leads(cyc, before)) == [3, 4, 5, 6, 7, 8]
    # every lead is valid on the date it feeds, whatever the cycle hour and whichever side of the flip
    for h in (0, 6, 12, 18):
        c = _utc(2026, 11, 25, h)
        for now in (c + timedelta(hours=6, minutes=25), c + timedelta(hours=11, minutes=25)):
            sd = bd.service_day(now)
            leads = bd.gefs_leads(c, sd)
            assert sorted(leads) == [3, 4, 5, 6, 7, 8], (h, now, leads)
            for dd, fh in leads.items():
                assert (c + timedelta(hours=fh)).date() == sd + timedelta(days=dd), (h, now, dd, fh)
    # the daily 10:25Z build's alignment is unchanged: f072 to day 3 ... f192 to day 8 (06Z, 12Z, 18Z cycles too)
    for h in (0, 6, 12, 18):
        c = _utc(2026, 11, 25, h)
        assert bd.gefs_leads(c, c.date()) == {3: 72, 4: 96, 5: 120, 6: 144, 7: 168, 8: 192}
    # a cycle two days old reaches day 8 with nothing: that day is left out, not fed a lead for another date
    assert 8 not in bd.gefs_leads(_utc(2026, 11, 23, 0), _utc(2026, 11, 25).date())
    # 12 h is the limit. From a (made-up) 19Z cycle the only lead near the 27th is f048, valid 13 h after
    # its 06Z: the 27th gets none. The 18Z cycle above, 12 h off, feeds every day.
    assert bd.GEFS_MAX_OFF_H == 12 and sorted(bd.gefs_leads(_utc(2026, 11, 25, 19), before)) == [4, 5, 6, 7, 8]


# ---- main() and gefs_live(): what no test ran ---------------------------------------------------
# 2026-11-25 gate review: every test above goes through assemble() or a pure
# helper. Three one-line changes in the code around it passed all of them and
# changed a published number: main()'s fill summing all 24 hours of
# climatology (ehc would count the forecast's hours twice), main() counting
# NBE days from the UTC date again (days 3-8 a day off from 00Z to 10Z), and
# gefs_live() filing leads by position again (f072 = day 3).
class _Clock(datetime):
    """build_deploy's clock, stopped at `at`: main() and gefs_live() ask datetime.now() themselves."""
    at = None

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at.replace(tzinfo=None)


@contextlib.contextmanager
def _swapped(obj, **attrs):
    """Attributes of a module set for one test and put back after it."""
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def _module(name: str, stand_in):
    """`import name` gives stand_in for one test (build_deploy imports duckdb and eccodes inside its functions)."""
    old = sys.modules.get(name)
    sys.modules[name] = stand_in
    try:
        yield
    finally:
        if old is None:
            del sys.modules[name]
        else:
            sys.modules[name] = old


# climatological chance of an hour below CAT I by UTC hour, the same at every field: 0.01 at 00Z to 0.24
# at 23Z, so a sum over any set of hours says which hours were summed
RATE = {h: (h + 1) / 100 for h in range(24)}


class _Climo:
    """Stands in for duckdb over climo.parquet: the two queries main() makes, answered from RATE.
    December only (the month of every planner day in the test), so another month's rows would show."""

    def connect(self):
        return self

    def execute(self, sql: str):
        if "sum(rsub)" in sql:      # expected hours a day, by field and month
            self.rows = [(i, 12, sum(RATE.values())) for i in CHASE]
        else:                       # the chance of each UTC hour
            self.rows = [(i, 12, h, r) for i in CHASE for h, r in RATE.items()]
        return self

    def fetchall(self):
        return self.rows


def _main(now: datetime, cycle: datetime, nbe: tuple) -> dict:
    """build_deploy.main() at `now`: the payload it writes for CHASE from a synthetic issuance of `cycle`.

    Stand-ins: the clock, the directories (a temporary copy of the layout
    main() reads), duckdb, and the two fetchers main() calls for days 3-14
    (nbe is what latest_nbe returns; the CPC outlooks say nothing). No
    dayscale.json, so the fitted tier and GEFS are off. Everything between is
    the code that runs in CI, with the committed daycal.json."""
    _Clock.at = now
    with tempfile.TemporaryDirectory() as d:
        here, data = Path(d) / "forecast", Path(d) / "app" / "public" / "data"
        (here / "out").mkdir(parents=True)
        data.mkdir(parents=True)
        (data / "airports.json").write_text(json.dumps(
            {"airports": [dict(a, icao=i, lon=-120.0, lat=37.0) for i, a in ATLAS.items()]}))
        (data / "chase.json").write_text(json.dumps({"airports": {i: [] for i in CHASE}}))
        (here / "out" / "current.json").write_text(json.dumps(_issue(cycle)))
        with _swapped(bd, datetime=_Clock, HERE=here, OUT=here / "out", latest_nbe=lambda stations: nbe,
                      cpc_factors=lambda airports: ({i: [] for i in airports}, list(bd.CPC_PRODUCTS))), \
                _swapped(sys, argv=["build_deploy.py"]), _module("duckdb", _Climo()), \
                contextlib.redirect_stdout(io.StringIO()):
            bd.main()
        return json.loads((here / "out" / "deploy.json").read_text())


def test_main_counts_days_3_14_from_the_service_day_and_fills_only_the_hours_not_reached():
    # one NBE row per field, from the 01Z cycle of Dec 8, valid on Dec 12 at 21Z (116 h after the cycle;
    # 116 h after either build is Dec 13)
    nbe = (_utc(2026, 12, 8, 1), [{"icao": i, "fhr": 116, "tmp": 40.0, "dpt": 38.0, "wsp": 4.0} for i in CHASE])
    before = _main(_utc(2026, 12, 8, 7, 25), _utc(2026, 12, 8, 6), nbe)       # service day Dec 7: Dec 12 is day 5
    after = _main(_utc(2026, 12, 8, 10, 25), _utc(2026, 12, 8, 9), nbe)       # service day Dec 8: Dec 12 is day 4
    assert (before["meta"]["day1"], after["meta"]["day1"]) == ("2026-12-08", "2026-12-09")
    for p in (before, after):
        day1 = datetime.strptime(p["meta"]["day1"], "%Y-%m-%d")
        assert p["meta"]["advisory"]["inputs"]["nbe"] == {"ok": True, "airports": 5, "cycle": "2026-12-08T01:00Z"}
        for i in CHASE:
            a = p["airports"][i]
            # the NBE row is on the planner day whose DATE is Dec 12, on both sides of the 10Z flip
            on = [(day1 + timedelta(days=k)).strftime("%Y-%m-%d") for k, t in enumerate(a["tiers"]) if t == "nbe"]
            assert on == ["2026-12-12"] and a["tiers"][:2] == ["cal", "cal"], (p["meta"]["generated"], i, a["tiers"])
    # 10:25Z, 09Z cycle: day 2 (Dec 10) is forecast through 09Z, a point that stands for 09Z-11Z. ehc is the
    # climatology of 12Z-23Z and of nothing else (all 24 hours would be 3.0, and count the forecast's twice).
    unreached = round(sum(RATE[h] for h in range(12, 24)), 2)
    assert unreached == 2.22 and round(sum(RATE.values()), 2) == 3.0
    for i in CHASE:
        assert after["airports"][i]["cov"] == [21, 9] and after["airports"][i]["ehc"] == [None, unreached], (i, after["airports"][i])
        # 07:25Z, 06Z cycle: day 2 is the whole of Dec 9, nothing to add
        assert before["airports"][i]["ehc"] == [None, None], (i, before["airports"][i]["ehc"])


def test_gefs_live_reads_each_planner_day_from_the_lead_valid_on_its_date():
    """The reader itself, on a stand-in mirror that holds the 00Z cycle of the
    25th: before the flip (07:25Z, service day the 24th) it must ask for
    f048-f168 and file f048 under day 3, the 27th; after it (10:25Z) f072-f192,
    with f072 under day 3, the 28th. Each member file's "grid" is its own
    forecast hour, so a lead filed under another day shows in the value."""
    try:
        import numpy as np
    except ImportError as e:
        print(f"     (numpy is not importable here: {e}; gefs_live() was not run)")
        return
    asked = []

    def fetch(url, timeout=180):                                # the index files: only that one cycle is there
        if "gefs.20261125/00/" not in url:
            raise OSError("HTTP 404")
        return b"1:0:d=2026112500:VIS:surface:anl:\n2:99:d=2026112500:GUST:surface:anl:\n"

    def urlopen(req, timeout=None):                             # the VIS bytes of one member file: its lead
        asked.append(req.full_url)
        return io.BytesIO(str(int(req.full_url[-3:])).encode())

    grid = {"latitudeOfFirstGridPointInDegrees": 90.0, "longitudeOfFirstGridPointInDegrees": 0.0,
            "jDirectionIncrementInDegrees": 0.5, "iDirectionIncrementInDegrees": 0.5, "Ni": 720, "Nj": 361}
    eccodes = types.SimpleNamespace(
        codes_grib_new_from_file=lambda f: int(f.read()), codes_get=lambda gid, key: grid[key],
        codes_get_values=lambda gid: np.full(361 * 720, gid * 100.0),      # visibility: f048 is 4,800 m everywhere
        codes_release=lambda gid: None)
    cyc, coords, by_date = _utc(2026, 11, 25, 0), {"KVIS": (-119.39, 36.32), "KBOS": (-71.01, 42.36)}, {}
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "data").mkdir()
        with _swapped(bd, datetime=_Clock, HERE=Path(d), fetch=fetch), _swapped(bd.urllib.request, urlopen=urlopen), \
                _module("eccodes", eccodes), contextlib.redirect_stdout(io.StringIO()):
            for now, first in ((_utc(2026, 11, 25, 7, 25), 48), (_utc(2026, 11, 25, 10, 25), 72)):
                _Clock.at = now
                del asked[:]
                sd = bd.service_day(now)
                out, info = bd.gefs_live(coords, sd)
                want = {dd: first + 24 * (dd - 3) for dd in range(3, 9)}
                assert want == bd.gefs_leads(cyc, sd)
                assert info == {"cycle": "2026-11-25T00Z", "leads": [3, 4, 5, 6, 7, 8], "members": {str(dd): 15 for dd in want},
                                "hours": {str(dd): fh for dd, fh in want.items()}}, (now, info)
                # the member files asked for are those leads, 15 members each, and no other
                assert sorted(int(u[-3:]) for u in asked) == sorted(list(want.values()) * 15), (now, sorted(set(asked))[:3])
                for dd, fh in want.items():
                    for icao in coords:
                        g = out[(icao, dd)]
                        assert g["fh"] == fh and abs(g["gefs_lv"] - math.log1p(fh / 10)) < 1e-9, (now, icao, dd, fh, g)
                        assert by_date.setdefault(((sd + timedelta(days=dd)).isoformat(), icao), g) == g
    assert len(by_date) == 7 * len(coords)                      # the 27th to Dec 3: five dates read on both sides


def test_one_deploylog_per_build():
    """It was deploy_<date>.json.gz: 24 builds a day would have overwritten it 23 times."""
    names = {bd.deploylog_name(_iso(_utc(2026, 12, 8, h)), _utc(2026, 12, 8, h) + timedelta(hours=1, minutes=25))
             for h in range(24)}
    assert len(names) == 24 and "deploy_20261208T09Z_20261208T102500Z.json.gz" in names
    # the same cycle built twice (a late cycle, or a person's dispatch beside the tick) is two logs
    a = bd.deploylog_name("2026-12-08T08:00:00Z", _utc(2026, 12, 8, 9, 25))
    b = bd.deploylog_name("2026-12-08T08:00:00Z", _utc(2026, 12, 8, 10, 25))
    assert a != b and sorted([b, a]) == [a, b] and all(n.startswith("deploy_") and n.endswith(".json.gz") for n in (a, b))


def test_the_kv_read_back_compares_what_was_built_with_what_is_live():
    with tempfile.TemporaryDirectory() as d:
        mine, back = Path(d) / "deploy.json", Path(d) / "back.json"
        mine.write_text(json.dumps({"meta": {"generated": "2026-12-08T10:25:07Z"}, "airports": {}}))
        back.write_text(mine.read_text())
        assert bd.same_build(mine, back)[0] is True
        back.write_text(" ⛅️ wrangler 4.167.0\n" + mine.read_text())
        assert bd.same_build(mine, back)[0] is True
        # the put did not land: KV still holds the build of an hour ago
        back.write_text(json.dumps({"meta": {"generated": "2026-12-08T09:25:11Z"}, "airports": {}}))
        ok, said = bd.same_build(mine, back)
        assert ok is False and "2026-12-08T09:25:11Z" in said and "2026-12-08T10:25:07Z" in said
        for text in ("", "Value not found", '{"error": "no deploy data yet"}', "{"):
            back.write_text(text)
            assert bd.same_build(mine, back)[0] is False, text
        assert bd.same_build(mine, Path(d) / "absent.json")[0] is False
        assert bd.same_build(Path(d) / "absent.json", back)[0] is False


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
