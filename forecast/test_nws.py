#!/usr/bin/env python3
"""Tests for forecast/nws.py — plain asserts, no test framework needed.

    PYTHONDONTWRITEBYTECODE=1 python3 forecast/test_nws.py      # from the repo root
    pytest forecast/test_nws.py                                  # also works

Every function named test_* runs; the script exits non-zero on the first
failure of each test and prints a summary. No network: nws_batch is tested
against a fake urlopen.
"""
import io
import json
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import nws  # noqa: E402

PDX = ZoneInfo("America/Los_Angeles")
POINTS_KPDX = {"office": "PQR", "officeName": "Portland, OR", "gridX": 116, "gridY": 106,
               "forecast": "https://api.weather.gov/gridpoints/PQR/116,106/forecast", "tz": "America/Los_Angeles"}


def period(name, start, end, detailed, short="", day=True):
    return {"name": name, "startTime": start, "endTime": end, "isDaytime": day,
            "shortForecast": short, "detailedForecast": detailed}


def kpdx_rec():
    """The 2026-09-26 KPDX case: NWS Portland said patchy fog Saturday morning."""
    return {"fetched": "2026-09-26T12:05Z", "updated": "2026-09-26T03:41:12+00:00",
            "generated": "2026-09-26T12:05:00+00:00",
            "periods": [
                period("Tonight", "2026-09-25T18:00:00-07:00", "2026-09-26T06:00:00-07:00",
                       "Patchy fog after 2am. Mostly clear, with a low around 52.", "Patchy Fog", False),
                period("Saturday", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00",
                       "Patchy fog before 8am. Sunny, with a high near 78.", "Patchy Fog then Sunny"),
                period("Saturday Night", "2026-09-26T18:00:00-07:00", "2026-09-27T06:00:00-07:00",
                       "Mostly clear, with a low around 50.", "Mostly Clear", False),
                period("Sunday", "2026-09-27T06:00:00-07:00", "2026-09-27T18:00:00-07:00",
                       "Sunny, with a high near 80. Light wind.", "Sunny"),
                period("Sunday Night", "2026-09-27T18:00:00-07:00", "2026-09-28T06:00:00-07:00",
                       "Areas of dense fog after midnight. Otherwise, mostly cloudy.", "Areas Of Dense Fog", False),
                period("Monday", "2026-09-28T06:00:00-07:00", "2026-09-28T18:00:00-07:00",
                       "Widespread fog before 10am, then sunny.", "Widespread Fog then Sunny"),
            ]}


@contextmanager
def temp_constant(name, value):
    old = getattr(nws, name)
    setattr(nws, name, value)
    try:
        yield
    finally:
        setattr(nws, name, old)


# ---------------------------------------------------------------- FOG_RE

def test_fog_re_specificity():
    def kind(text):
        return nws._kind(text)
    assert kind("Areas of dense fog after midnight") == "dense fog"
    assert kind("Patchy dense fog before 9am") == "dense fog"
    assert kind("Widespread dense fog") == "dense fog"
    assert kind("Dense fog advisory in effect") == "dense fog"
    assert kind("Areas of fog before 9am") == "areas of fog"
    assert kind("Widespread fog before 10am") == "widespread fog"  # NWS coverage words stay distinct
    assert kind("Patchy fog before 8am") == "patchy fog"
    assert kind("Fog before 8am, then sunny") == "fog"
    assert kind("Patchy Fog then Sunny") == "patchy fog"          # Title Case shortForecast
    assert kind("Patchy fog, then areas of dense fog late") == "dense fog"  # most severe wins in-sentence
    assert kind("Foggy conditions expected") is None               # word boundary
    assert kind("Widespread haze and patchy smoke") is None
    assert kind("Smoke") is None and kind("Haze") is None
    assert kind("Areas of smoke before noon") is None
    assert kind("Areas of frost and patchy fog") == "patchy fog"
    assert kind("Fog likely") == "fog" and kind("Chance of fog") == "fog"
    # winter wording: freezing / ice fog keeps its coverage and its "dense"
    assert kind("Patchy freezing fog before 8am") == "patchy fog"
    assert kind("Areas of freezing fog") == "areas of fog"
    assert kind("Widespread dense freezing fog") == "dense fog"
    assert kind("Areas of dense freezing fog before 9am") == "dense fog"
    assert kind("Patchy ice fog") == "patchy fog"
    assert kind("Freezing fog") == "fog"
    assert nws.FOG_RE.search("Fog/Mist").group(0) == "Fog"         # punctuation is a boundary
    assert nws.FOG_RE.search("areas of dense fog").group(0) == "areas of dense fog"  # whole wording matched
    assert nws.SEVERITY["dense fog"] > nws.SEVERITY["widespread fog"] > nws.SEVERITY["areas of fog"] > \
        nws.SEVERITY["patchy fog"] > nws.SEVERITY["fog"] > nws.SEVERITY["no fog"]


def test_short_forecast_fallback():
    rec = {"fetched": "2026-09-26T12:05Z", "updated": "2026-09-26T03:41:12+00:00", "periods": [
        period("Saturday", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00", "", "Patchy Fog then Sunny")]}
    s = nws.nws_summary(rec, PDX, datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc))
    assert s["verdict"] == "patchy fog"
    assert s["mentions"][0]["phrase"] == "Patchy Fog then Sunny"
    assert s["mentions"][0]["period"] == "Saturday"


# ------------------------------------------------------- sentence extraction

def test_sentence_extraction():
    p = period("Saturday", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00", "Patchy fog before 8am.")
    ms = nws._mentions_in(p["detailedForecast"], p)
    assert [m["phrase"] for m in ms] == ["Patchy fog before 8am"]
    assert ms[0]["start"] == "2026-09-26T06:00:00-07:00" and ms[0]["end"] == "2026-09-26T18:00:00-07:00"
    p = period("Saturday Night", "2026-09-26T18:00:00-07:00", "2026-09-27T06:00:00-07:00",
               "Mostly clear, with a low around 50. Patchy fog after 4am. Light and variable wind.")
    ms = nws._mentions_in(p["detailedForecast"], p)
    assert [m["phrase"] for m in ms] == ["Patchy fog after 4am"]
    assert ms[0]["kind"] == "patchy fog"
    # two fog sentences in one period -> two mentions, verbatim, no trailing period
    p = period("Tonight", "2026-09-25T18:00:00-07:00", "2026-09-26T06:00:00-07:00",
               "Patchy fog after 2am.  Areas of dense fog after 4am. Low around 45.")
    ms = nws._mentions_in(p["detailedForecast"], p)
    assert [m["phrase"] for m in ms] == ["Patchy fog after 2am", "Areas of dense fog after 4am"]
    assert [m["kind"] for m in ms] == ["patchy fog", "dense fog"]
    # a percent-terminated sentence does not swallow the next one
    p = period("Monday", "2026-09-28T06:00:00-07:00", "2026-09-28T18:00:00-07:00",
               "Chance of precipitation is 30%. Patchy fog before 9am.")
    assert [m["phrase"] for m in nws._mentions_in(p["detailedForecast"], p)] == ["Patchy fog before 9am"]


# ------------------------------------------------------------- horizon

def test_horizon_filtering():
    now = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
    rec = {"fetched": "2026-09-26T00:00Z", "updated": "2026-09-25T20:00:00+00:00", "periods": [
        period("Past", "2026-09-25T00:00:00+00:00", "2026-09-25T12:00:00+00:00", "Dense fog before 9am."),
        period("Now", "2026-09-25T12:00:00+00:00", "2026-09-26T12:00:00+00:00", "Sunny, with a high near 70."),
        period("Hour47", "2026-09-27T23:00:00+00:00", "2026-09-28T11:00:00+00:00", "Patchy fog before 8am."),
        period("Hour60", "2026-09-28T12:00:00+00:00", "2026-09-29T00:00:00+00:00", "Areas of fog before 9am."),
    ]}
    s = nws.nws_summary(rec, "UTC", now)
    names = [m["period"] for m in s["mentions"]]
    assert "Past" not in names, "a period entirely in the past must be ignored"
    assert names == ["Hour47"], names
    assert s["verdict"] == "patchy fog"
    assert s["horizonEnd"] == "2026-09-28T11:00Z"           # end of the last considered period
    assert s["horizonEndLocal"] == "Monday 11 am"
    # a period ending exactly at now is in the past; one straddling now counts
    rec2 = {"fetched": "2026-09-26T00:00Z", "periods": [
        period("EndsNow", "2026-09-25T12:00:00+00:00", "2026-09-26T00:00:00+00:00", "Dense fog."),
        period("Straddle", "2026-09-25T18:00:00-06:00", "2026-09-26T06:00:00-06:00", "Patchy fog after 2am.")]}
    s2 = nws.nws_summary(rec2, "UTC", now)
    assert [m["period"] for m in s2["mentions"]] == ["Straddle"]
    # nothing overlapping the horizon -> None (not a fake "no fog through None")
    rec3 = {"fetched": "2026-09-20T00:00Z", "periods": [
        period("Old", "2026-09-20T00:00:00+00:00", "2026-09-20T12:00:00+00:00", "Dense fog.")]}
    assert nws.nws_summary(rec3, "UTC", now) is None
    assert nws.nws_summary(None, "UTC", now) is None
    assert nws.nws_summary({}, "UTC", now) is None
    assert nws.nws_summary({"periods": []}, "UTC", now) is None
    # custom horizon: 24 h excludes the hour-47 period
    assert nws.nws_summary(rec, "UTC", now, horizon_h=24)["verdict"] == "no fog"


# ------------------------------------------------------------- verdict

def test_verdict_precedence():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

    def verdict(*texts):
        ps = [period(f"P{i}", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00", t)
              for i, t in enumerate(texts)]
        return nws.nws_summary({"fetched": "2026-09-26T12:00Z", "periods": ps}, PDX, now)["verdict"]
    assert verdict("Sunny.", "Clear.") == "no fog"
    assert verdict("Fog before 8am.", "Sunny.") == "fog"
    assert verdict("Fog before 8am.", "Patchy fog after 2am.") == "patchy fog"
    assert verdict("Patchy fog after 2am.", "Areas of fog before 9am.") == "areas of fog"
    assert verdict("Widespread fog.", "Patchy fog.") == "widespread fog"
    assert verdict("Widespread fog.", "Areas of fog.") == "widespread fog"
    assert verdict("Patchy fog.", "Areas of fog.", "Dense fog after midnight.") == "dense fog"
    assert verdict("Areas of dense freezing fog before 9am.", "Patchy fog after midnight.") == "dense fog"
    assert verdict("Patchy dense fog.", "Areas of fog.") == "dense fog"
    assert verdict("Foggy.", "Smoke.") == "no fog"


# ------------------------------------------------------- summary fields

def test_summary_fields_kpdx():
    now = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)  # Sat 7 am PDT, fog on the field
    s = nws.nws_summary(kpdx_rec(), PDX, now, points_entry=POINTS_KPDX)
    assert s["office"] == "PQR" and s["officeName"] == "Portland, OR"
    assert s["source"] == POINTS_KPDX["forecast"]
    assert s["issued"] == "2026-09-26T03:41Z"
    assert s["issuedLocal"] == "8:41 pm Friday"
    assert s["fetched"] == "2026-09-26T12:05Z"
    # Tonight ended 13:00Z (< now) -> ignored; Monday starts 13:00Z Mon = hour 47 -> in
    assert [m["period"] for m in s["mentions"]] == ["Saturday", "Sunday Night", "Monday"]
    assert s["mentions"][0] == {"period": "Saturday", "start": "2026-09-26T06:00:00-07:00",
                                "end": "2026-09-26T18:00:00-07:00", "kind": "patchy fog",
                                "phrase": "Patchy fog before 8am", "periodLabel": "Saturday"}
    assert [m["periodLabel"] for m in s["mentions"]] == ["Saturday", "Sunday night", "Monday"]
    assert s["mentions"][1]["kind"] == "dense fog" and s["mentions"][2]["kind"] == "widespread fog"
    assert s["verdict"] == "dense fog"
    assert s["issuedShort"] == "Fri 8:41 pm"
    assert s["horizonEnd"] == "2026-09-29T01:00Z" and s["horizonEndLocal"] == "Monday 6 pm"
    # no points entry -> attribution fields None, everything else intact
    s0 = nws.nws_summary(kpdx_rec(), "America/Los_Angeles", now)
    assert s0["office"] is None and s0["officeName"] is None and s0["source"] is None
    assert s0["mentions"] == s["mentions"] and s0["issuedLocal"] == "8:41 pm Friday"
    # tz as a string works the same as a ZoneInfo
    assert nws.nws_summary(kpdx_rec(), "America/Los_Angeles", now)["horizonEndLocal"] == "Monday 6 pm"
    # 12 h later: Saturday is over, only the night/Monday mentions remain
    later = datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)
    s2 = nws.nws_summary(kpdx_rec(), PDX, later, points_entry=POINTS_KPDX)
    assert [m["period"] for m in s2["mentions"]] == ["Sunday Night", "Monday"]
    # issued falls back generated -> fetched when updateTime is missing
    r = kpdx_rec(); r["updated"] = None
    assert nws.nws_summary(r, PDX, now)["issued"] == "2026-09-26T12:05Z"
    r["generated"] = None
    assert nws.nws_summary(r, PDX, now)["issued"] == "2026-09-26T12:05Z"
    r["fetched"] = None
    s3 = nws.nws_summary(r, PDX, now)
    assert s3["issued"] is None and s3["issuedLocal"] is None and s3["issuedShort"] is None
    assert json.dumps(s)  # JSON-serialisable for data.json
    # at the real bake instant (10:25Z = 03:25 PDT) "Tonight" is still live and
    # is the FIRST mention: its label must name the night the NWS meant
    bake = datetime(2026, 9, 26, 10, 25, tzinfo=timezone.utc)
    sb = nws.nws_summary(kpdx_rec(), PDX, bake, points_entry=POINTS_KPDX)
    assert [(m["period"], m["periodLabel"]) for m in sb["mentions"]][:2] == \
        [("Tonight", "Friday night"), ("Saturday", "Saturday")]


# ------------------------------------------------------- period_label

def test_period_label():
    f = nws.period_label
    # relative names resolve against the period's own start/end in the airport tz
    assert f("Tonight", "2026-09-25T18:00:00-07:00", "2026-09-26T06:00:00-07:00", PDX) == "Friday night"
    assert f("Tonight", "2026-09-25T22:00:00-07:00", "2026-09-26T06:00:00-07:00", PDX) == "Friday night"  # issued mid-evening
    assert f("Overnight", "2026-09-26T00:00:00-07:00", "2026-09-26T06:00:00-07:00", PDX) == "early Saturday"
    assert f("Overnight", "2026-09-26T01:00:00-04:00", "2026-09-26T06:00:00-04:00", "America/New_York") == "early Saturday"
    assert f("Today", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00", PDX) == "Saturday"
    assert f("This Afternoon", "2026-09-26T12:00:00-07:00", "2026-09-26T18:00:00-07:00", PDX) == "Saturday afternoon"
    assert f("Rest of Today", "2026-09-26T15:00:00-07:00", "2026-09-26T18:00:00-07:00", PDX) == "Saturday"
    # times given in UTC resolve in the airport's zone: 01:00Z Saturday is 18:00 PDT Friday
    assert f("Tonight", "2026-09-26T01:00:00+00:00", "2026-09-26T13:00:00+00:00", PDX) == "Friday night"
    assert f("Today", "2026-09-27T01:00:00+00:00", "2026-09-27T01:00:00+00:00", PDX) == "Saturday"   # 18:00 PDT Sat
    assert f("Today", "2026-09-27T01:00:00+00:00", "2026-09-27T01:00:00+00:00", "UTC") == "Sunday"   # the tz matters
    # Eastern bakes (06:25 EDT) see "Today"/"This Afternoon" as the first periods
    assert f("This Afternoon", "2026-09-26T12:00:00-04:00", "2026-09-26T18:00:00-04:00", "America/New_York") == "Saturday afternoon"
    # proper names pass through; "X Night" reads as prose
    assert f("Saturday", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00", PDX) == "Saturday"
    assert f("Sunday Night", "2026-09-27T18:00:00-07:00", "2026-09-28T06:00:00-07:00", PDX) == "Sunday night"
    assert f("Columbus Day", "2026-10-12T06:00:00-07:00", "2026-10-12T18:00:00-07:00", PDX) == "Columbus Day"
    assert f("Martin Luther King Jr. Day", "s", "e", PDX) == "Martin Luther King Jr. Day"
    # unparseable times: the raw name, never a crash
    assert f("Tonight", None, None, PDX) == "Tonight"
    assert f("", "s", "e", PDX) == "" and f(None, "s", "e", PDX) == ""


# ------------------------------------------------------- phrase_for_prose

def test_phrase_for_prose():
    f = nws.phrase_for_prose
    assert f({"phrase": "Patchy fog before 8am", "period": "Saturday"}) == "patchy fog before 8am Saturday"
    assert f({"phrase": "Patchy fog before 8am.", "period": "Saturday"}) == "patchy fog before 8am Saturday"
    assert f({"phrase": "Areas of dense fog after midnight", "period": "Sunday Night"}) == \
        "areas of dense fog after midnight Sunday Night"
    # phrase already ends with / names the period -> not repeated
    assert f({"phrase": "Patchy fog Saturday", "period": "Saturday"}) == "patchy fog Saturday"
    assert f({"phrase": "Patchy fog before 8am Saturday morning", "period": "Saturday"}) == \
        "patchy fog before 8am Saturday morning"
    assert f({"phrase": "Patchy fog Saturday night", "period": "Saturday Night"}) == "patchy fog Saturday night"
    # relative period names print as the resolved label (the page is served
    # ~24 h, so "tonight" would be read as the reader's night, not the NWS's)
    assert f({"phrase": "Patchy fog after 2am", "period": "Tonight", "periodLabel": "Friday night"}) == \
        "patchy fog after 2am Friday night"
    assert f({"phrase": "Patchy fog before 9am", "period": "This Afternoon", "periodLabel": "Saturday afternoon"}) == \
        "patchy fog before 9am Saturday afternoon"
    assert f({"phrase": "Patchy fog after 4am", "period": "Overnight", "periodLabel": "early Sunday"}) == \
        "patchy fog after 4am early Sunday"
    assert f({"phrase": "Areas of dense fog after midnight", "period": "Sunday Night", "periodLabel": "Sunday night"}) == \
        "areas of dense fog after midnight Sunday night"
    assert f({"phrase": "Patchy fog", "period": "Columbus Day", "periodLabel": "Columbus Day"}) == "patchy fog Columbus Day"
    # a mention without a label (not from nws_summary) falls back to the raw name
    assert f({"phrase": "Patchy fog after 2am", "period": "Tonight"}) == "patchy fog after 2am tonight"
    # the phrase already naming the raw period or the label's weekday is not repeated
    assert f({"phrase": "Patchy fog late tonight", "period": "Tonight", "periodLabel": "Friday night"}) == "patchy fog late tonight"
    assert f({"phrase": "Patchy fog Friday night", "period": "Tonight", "periodLabel": "Friday night"}) == "patchy fog Friday night"
    # only the first letter changes ("8am", "AM" wording untouched)
    assert f({"phrase": "Dense fog until 10 AM PDT", "period": "Today", "periodLabel": "Saturday"}) == \
        "dense fog until 10 AM PDT Saturday"
    assert f({"phrase": "Patchy fog", "period": ""}) == "patchy fog"
    assert f({"phrase": "", "period": "Saturday"}) == "Saturday"
    # end-to-end with the KPDX summary, at 7 am and at the 10:25Z bake instant
    s = nws.nws_summary(kpdx_rec(), PDX, datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc))
    assert f(s["mentions"][0]) == "patchy fog before 8am Saturday"
    sb = nws.nws_summary(kpdx_rec(), PDX, datetime(2026, 9, 26, 10, 25, tzinfo=timezone.utc))
    assert f(sb["mentions"][0]) == "patchy fog after 2am Friday night"
    assert f(sb["mentions"][2]) == "areas of dense fog after midnight Sunday night"


# -------------------------------------------------------- last-good cache

def test_with_last_good_merge():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d) / "out" / "nws_cache.json"   # parent must be created by the function
        with temp_constant("NWS_CACHE", cache):
            # no cache yet: fresh passes through, file written
            fresh = {"KPDX": {"fetched": "2026-09-26T12:00Z", "periods": [1]}}
            assert nws.nws_with_last_good(dict(fresh), now) == fresh
            saved = json.loads(cache.read_text())
            assert saved["saved"] == "2026-09-26T12:00Z" and saved["nws"] == fresh
            # seed a cache: one young entry, one at 29 h, one at 31 h, one missing/garbage stamp
            cache.write_text(json.dumps({"saved": "2026-09-25T12:00Z", "nws": {
                "KPDX": {"fetched": "2026-09-25T12:00Z", "periods": ["cached-pdx"]},
                "KSEA": {"fetched": "2026-09-25T12:00Z", "periods": ["cached-sea"]},   # 24 h -> reused
                "KSFO": {"fetched": "2026-09-25T07:00Z", "periods": ["cached-sfo"]},   # 29 h -> reused
                "KLAX": {"fetched": "2026-09-25T05:00Z", "periods": ["cached-lax"]},   # 31 h -> dropped
                "KJFK": {"fetched": "garbage", "periods": ["cached-jfk"]},
                "KBOS": {"periods": ["cached-bos"]},
                "KXXX": "not a dict"}}))
            fresh = {"KPDX": {"fetched": "2026-09-26T12:00Z", "periods": ["fresh-pdx"]}}
            merged = nws.nws_with_last_good(fresh, now)
            assert merged["KPDX"]["periods"] == ["fresh-pdx"], "fresh beats cached"
            assert merged["KSEA"]["periods"] == ["cached-sea"]
            assert merged["KSFO"]["periods"] == ["cached-sfo"]
            assert "KLAX" not in merged and "KJFK" not in merged and "KBOS" not in merged and "KXXX" not in merged
            assert set(merged) == {"KPDX", "KSEA", "KSFO"}
            # the merge is what got saved (so a second outage still has the young entries)
            saved = json.loads(cache.read_text())
            assert set(saved["nws"]) == {"KPDX", "KSEA", "KSFO"} and saved["saved"] == "2026-09-26T12:00Z"
            # exact boundary: fetched exactly max_age_h ago is kept (>= cutoff), one minute older is not
            cache.write_text(json.dumps({"saved": "x", "nws": {
                "KA": {"fetched": "2026-09-25T06:00Z"}, "KB": {"fetched": "2026-09-25T05:59Z"}}}))
            merged = nws.nws_with_last_good({}, now)
            assert set(merged) == {"KA"}
            # custom max_age
            cache.write_text(json.dumps({"saved": "x", "nws": {"KA": {"fetched": "2026-09-25T06:00Z"}}}))
            assert nws.nws_with_last_good({}, now, max_age_h=6) == {}
            # unreadable cache -> fresh only, still saved, never raises
            cache.write_text("{not json")
            assert nws.nws_with_last_good({"KPDX": {"fetched": "2026-09-26T12:00Z"}}, now) == \
                {"KPDX": {"fetched": "2026-09-26T12:00Z"}}
            assert json.loads(cache.read_text())["nws"] == {"KPDX": {"fetched": "2026-09-26T12:00Z"}}


def test_load_points_missing():
    with tempfile.TemporaryDirectory() as d:
        with temp_constant("NWS_POINTS", Path(d) / "nope.json"):
            assert nws.load_points() == {}
        p = Path(d) / "bad.json"; p.write_text("{oops")
        with temp_constant("NWS_POINTS", p):
            assert nws.load_points() == {}
        p.write_text(json.dumps({"KPDX": POINTS_KPDX}))
        with temp_constant("NWS_POINTS", p):
            assert nws.load_points() == {"KPDX": POINTS_KPDX}


# -------------------------------------------------------------- nws_batch

class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def nws_doc(n_periods=14, update="2026-09-26T03:41:12+00:00"):
    return {"properties": {"updated": "old-field", "updateTime": update, "generatedAt": "2026-09-26T12:05:00+00:00",
                           "periods": [{"number": i + 1, "name": f"P{i}", "startTime": "s", "endTime": "e",
                                        "isDaytime": bool(i % 2), "temperature": 60, "windSpeed": "5 mph",
                                        "shortForecast": "Sunny", "detailedForecast": "Sunny."}
                                       for i in range(n_periods)]}}


def test_nws_batch_fake_network():
    calls = {}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        assert req.get_header("User-agent") == nws.UA, "api.weather.gov requires our User-Agent"
        calls[url] = calls.get(url, 0) + 1
        if "/flaky/" in url and calls[url] == 1:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        if "/timeout/" in url and calls[url] == 1:
            raise TimeoutError("timed out")
        if "/gone/" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if "/dead/" in url:
            raise urllib.error.HTTPError(url, 500, "Unexpected Problem", {}, None)
        return FakeResp(json.dumps(nws_doc()).encode())

    points = {"KAAA": {"forecast": "https://x/ok/AAA"},
              "KBBB": {"forecast": "https://x/flaky/BBB"},
              "KCCC": {"forecast": "https://x/timeout/CCC"},
              "KDDD": {"forecast": "https://x/gone/DDD"},
              "KEEE": {"forecast": "https://x/dead/EEE"},
              "KFFF": {"office": "X"},  # no forecast url -> not attempted
              "KGGG": None}
    with temp_constant("RETRY_SLEEP_S", 0):
        old = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            out = nws.nws_batch(points, budget_s=30, workers=4, timeout_s=5)
        finally:
            urllib.request.urlopen = old
    assert set(out) == {"KAAA", "KBBB", "KCCC"}, out.keys()
    assert calls["https://x/flaky/BBB"] == 2 and calls["https://x/timeout/CCC"] == 2   # retried once
    assert calls["https://x/gone/DDD"] == 1, "4xx is not retried"
    assert calls["https://x/dead/EEE"] == 2, "5xx retried once then given up"
    rec = out["KAAA"]
    assert len(rec["periods"]) == 6, "trimmed to the first 6 of 14"
    assert set(rec["periods"][0]) == set(nws.PERIOD_KEYS), "period trimmed to the page's keys"
    assert rec["periods"][0]["name"] == "P0" and rec["periods"][5]["name"] == "P5"
    assert rec["updated"] == "2026-09-26T03:41:12+00:00", "updateTime preferred over updated"
    assert rec["generated"] == "2026-09-26T12:05:00+00:00"
    datetime.strptime(rec["fetched"], "%Y-%m-%dT%H:%MZ")
    assert json.dumps(out)
    # budget already spent -> nothing new starts, still returns (never raises)
    with temp_constant("RETRY_SLEEP_S", 0):
        urllib.request.urlopen = fake_urlopen
        try:
            assert nws.nws_batch({"KAAA": {"forecast": "https://x/ok/AAA2"}}, budget_s=-1) == {}
        finally:
            urllib.request.urlopen = old
    assert "https://x/ok/AAA2" not in calls
    assert nws.nws_batch({}) == {}
    assert nws.nws_batch(None) == {}


def test_batch_empty_periods_is_a_failure():
    """A 200 whose body has no periods (degraded / reshaped response) must not
    count as fetched: it would replace the < 30 h last-good entry with nothing
    and then be written back to the cache, so the next bake could not recover
    either — the exact outage the cache exists for."""
    import contextlib
    calls = {}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls[url] = calls.get(url, 0) + 1
        if "/empty/" in url:
            return FakeResp(json.dumps({"properties": {"updateTime": "2026-09-26T03:41:12+00:00", "periods": []}}).encode())
        if "/noprops/" in url:
            return FakeResp(json.dumps({"title": "Unexpected Problem", "status": 500}).encode())
        return FakeResp(json.dumps(nws_doc(n_periods=3)).encode())
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d, temp_constant("NWS_CACHE", Path(d) / "nws_cache.json"), \
            temp_constant("RETRY_SLEEP_S", 0):
        good = {"fetched": "2026-09-26T06:00Z", "updated": "2026-09-26T03:41:12+00:00",
                "periods": [period("Saturday", "2026-09-26T06:00:00-07:00", "2026-09-26T18:00:00-07:00",
                                   "Patchy fog before 9am.")]}
        nws.NWS_CACHE.write_text(json.dumps({"saved": "2026-09-26T06:00Z", "nws": {"KEMP": good, "KNOP": good}}))
        old = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                out = nws.nws_batch({"KEMP": {"forecast": "https://x/empty/E"}, "KNOP": {"forecast": "https://x/noprops/N"},
                                     "KOK": {"forecast": "https://x/ok/O"}}, budget_s=30, workers=2, timeout_s=5)
        finally:
            urllib.request.urlopen = old
        assert set(out) == {"KOK"}, out.keys()
        assert calls["https://x/empty/E"] == 2, "treated like a 5xx: one retry, then the station fails"
        assert "fetched 1 of 3" in buf.getvalue() and "(2 failed)" in buf.getvalue()
        assert "::warning::NWS point forecasts: NWS: fetched 1 of 3" in buf.getvalue(), \
            "fewer than half the stations fetched -> a run annotation, not a plain green line"
        merged = nws.nws_with_last_good(out, now)
        assert merged["KEMP"] == good and merged["KNOP"] == good, "the last-good entry survives the bad 200"
        assert nws.nws_summary(merged["KEMP"], PDX, now)["verdict"] == "patchy fog"
        assert json.loads(nws.NWS_CACHE.read_text())["nws"]["KEMP"] == good, "and is not overwritten on disk"
    # a healthy majority does not warn
    buf = io.StringIO()
    urllib.request.urlopen = fake_urlopen
    try:
        with contextlib.redirect_stdout(buf):
            nws.nws_batch({"KA": {"forecast": "https://x/ok/A"}, "KB": {"forecast": "https://x/ok/B"},
                           "KC": {"forecast": "https://x/noprops/C"}}, budget_s=30, workers=2, timeout_s=5)
    finally:
        urllib.request.urlopen = old
    assert "::warning::" not in buf.getvalue()


def test_batch_updated_fallback():
    def fake_urlopen(req, timeout=None):
        doc = nws_doc(n_periods=3, update=None)
        del doc["properties"]["updateTime"]
        return FakeResp(json.dumps(doc).encode())
    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        out = nws.nws_batch({"KAAA": {"forecast": "https://x/ok"}})
    finally:
        urllib.request.urlopen = old
    assert out["KAAA"]["updated"] == "old-field" and len(out["KAAA"]["periods"]) == 3


def test_batch_gzip_body():
    """api.weather.gov only reliably answers urllib when we accept gzip, so the
    request must carry Accept-Encoding: gzip and the body must be inflated
    (by header, or by magic bytes when a proxy strips the header)."""
    import gzip

    class GzResp(FakeResp):
        headers = {"Content-Encoding": "gzip"}

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["accept_encoding"] = req.get_header("Accept-encoding")
        raw = json.dumps(nws_doc(n_periods=2)).encode()
        return GzResp(gzip.compress(raw)) if "/hdr" in req.full_url else FakeResp(gzip.compress(raw))
    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        out = nws.nws_batch({"KAAA": {"forecast": "https://x/hdr"}, "KBBB": {"forecast": "https://x/nohdr"}})
    finally:
        urllib.request.urlopen = old
    assert seen["accept_encoding"] == "gzip", "Accept-Encoding: gzip is load-bearing for api.weather.gov"
    assert set(out) == {"KAAA", "KBBB"}, out.keys()
    assert all(len(out[i]["periods"]) == 2 for i in out)


# ------------------------------------------- build_pages consumers (import-guarded)

def test_build_pages_snippet_and_mentions():
    """The page-side consumers: snippet() must not split the LSX office name
    'St. Louis, MO' (or 'Jr. Day') as a sentence end, and the quoted mention
    is the earliest one, with a more severe later one appended, not instead."""
    try:
        import build_pages as bp
    except Exception as e:  # build_pages needs the repo layout; nws.py tests still count
        print(f"     (build_pages not importable here: {e}; skipped)")
        return
    tail = "31 fog hours/yr, season peaks December and January."
    lead = ("The NWS St. Louis, MO forecast (issued 10:41 pm Friday) calls for patchy fog before 9am, then a chance of "
            "showers and thunderstorms between 1pm and 4pm Saturday. NWS model guidance (National Blend of Models) shows "
            "no dense-fog signal, visibility under a mile, in the next 48 hours.")
    d = bp.snippet(lead, tail)
    assert d.startswith("The NWS St. Louis, MO forecast (issued 10:41 pm Friday) calls for patchy fog"), d
    assert "The NWS St. 31" not in d
    pub = ("Yes, fog is likely: 62% chance of fog (visibility under 1 mile) around 3 am Saturday, with the fog window "
           "roughly 1 am to 6 am local time. The NWS St. Louis, MO forecast (issued 3:52 am Saturday) also calls for "
           "patchy fog before 9am Saturday.")
    assert bp.snippet(pub, tail) == "Yes, fog is likely: 62% chance of fog (visibility under 1 mile) around 3 am Saturday, with the fog window roughly 1 am to 6 am local time."
    assert bp.snippet("Patchy fog Martin Luther King Jr. Day. Sunny after.", "") == "Patchy fog Martin Luther King Jr. Day. Sunny after."
    # tail candidates: the first that fits wins, none forced past the cap
    first = "No, fog is unlikely through Sunday 1 am — the calibrated forecast peaks at just 5%."
    assert bp.snippet(first, ["x" * 100, "NWS Twin Cities, MN: areas of fog (early Sunday).", tail]) == \
        first + " NWS Twin Cities, MN: areas of fog (early Sunday)."
    assert bp.snippet(first, ["x" * 100, "y" * 100]) == first
    # the mention a page leads with is the earliest; the verdict's is appended
    s = nws.nws_summary(kpdx_rec(), PDX, datetime(2026, 9, 26, 10, 25, tzinfo=timezone.utc), points_entry=POINTS_KPDX)
    assert bp.nws_mention(s)["period"] == "Tonight"
    assert bp.nws_mention_top(s)["kind"] == "dense fog" and bp.nws_mention_top(s)["period"] == "Sunday Night"
    assert bp.nws_upgrade(s) is bp.nws_mention_top(s)
    one = nws.nws_summary({"fetched": "2026-09-26T12:05Z", "periods": kpdx_rec()["periods"][:2]}, PDX,
                          datetime(2026, 9, 26, 10, 25, tzinfo=timezone.utc))
    assert bp.nws_mention(one)["period"] == "Tonight" and bp.nws_upgrade(one) is None, "same kind later: no upgrade"
    assert bp.nws_mention({"verdict": "no fog", "mentions": []}) is None and bp.nws_upgrade(None) is None
    assert bp.nws_label(bp.nws_mention(s)) == "Friday night" and bp.nws_label({"period": "Tonight"}) == "Tonight"
    assert set(bp.NWS_RANK) == {k for k in nws.SEVERITY if k != "no fog"}, "hub ranking covers every kind"


# ------------------------------------------------------------------ runner

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
