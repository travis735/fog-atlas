#!/usr/bin/env python3
"""Tests for the terminal-forecast (TAF) layer of forecast/build_pages.py —
plain asserts, no test framework needed.

    PYTHONDONTWRITEBYTECODE=1 python3 forecast/test_build_pages.py      # from the repo root

Every function named test_* runs; the script prints ok / FAIL per test and
exits non-zero if any failed.

What is guarded: a page must not print a terminal forecast as current when
it had ended before the page was built (VABP's page said "dense fog
Thursday" on 2026-11-25 from a TAF issued on the 18th), nor a fog group
inside a still-running forecast that was already over at the build.

The records are real ones from aviationweather.gov in the shape the bake's
awc_batch returns (seven of 2026-11-15, one of 2026-11-28), cut down to the
fields the code reads, plus two built by hand where a round local time makes
the sentence easy to check.
Airports come from the committed atlas (app/public/data/airports.json), the
same file main() falls back to. No network; the one test that writes a file
writes it under a temp directory, never under forecast/out.
"""
import inspect
import io
import json
import re
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import build_pages as bp  # noqa: E402

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")  # GMT in November: local time == UTC


def Z(day, hour, minute=0, month=11):
    """A UTC time in 2026 (November unless said)."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# ------------------------------------------------- real records (2026-11-15)

# TAF VABP 151700Z 1518/1603 … TEMPO 1523/1603 1200 BR FU HZ
#   base 15 18:00Z-21:00Z 3000 m · BECMG 21:00Z (complete 23:00Z) 2500 m · TEMPO 15 23:00Z-16 03:00Z 1200 m (0.75 mi)
VABP = json.loads("""{"icaoId": "VABP", "issueTime": "2026-11-15T17:00:00.000Z", "validTimeFrom": 1794765600, "validTimeTo": 1794798000, "rawTAF": "TAF VABP 151700Z 1518/1603 VRB05KT 3000 BR FU HZ NSC BECMG 1521/1523 2500 BR FU HZ TEMPO 1523/1603 1200 BR FU HZ", "mostRecent": 1, "fcsts": [{"timeFrom": 1794765600, "timeTo": 1794776400, "timeBec": null, "fcstChange": null, "probability": null, "visib": 1.86, "wxString": "BR FU HZ"}, {"timeFrom": 1794776400, "timeTo": 1794798000, "timeBec": 1794783600, "fcstChange": "BECMG", "probability": null, "visib": 1.55, "wxString": "BR FU HZ"}, {"timeFrom": 1794783600, "timeTo": 1794798000, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 0.75, "wxString": "BR FU HZ"}]}""")

# TAF CYXJ 151840Z 1519/1607 … 1/4SM FZFG … FM152100 3/4SM BR … FM160000 P6SM … PROB30 1604/1607 1/2SM FZFG
#   base 15 19:00Z-21:00Z 1/4 mi FZFG · FM 21:00Z-16 00:00Z 3/4 mi BR · clear from 16 00:00Z · PROB30 16 04:00Z-07:00Z 1/2 mi FZFG
CYXJ = json.loads("""{"icaoId": "CYXJ", "issueTime": "2026-11-15T18:40:00.000Z", "validTimeFrom": 1794769200, "validTimeTo": 1794812400, "rawTAF": "TAF CYXJ 151840Z 1519/1607 11005KT 1/4SM FZFG VV002 TEMPO 1519/1521 1SM BR SCT002 FM152100 15005KT 3/4SM BR FEW002 TEMPO 1521/1524 6SM BR FM160000 16005KT P6SM SKC FM160400 18010KT P6SM SCT050 PROB30 1604/1607 1/2SM FZFG RMK NXT FCST BY 160100Z", "mostRecent": 1, "fcsts": [{"timeFrom": 1794769200, "timeTo": 1794776400, "timeBec": null, "fcstChange": null, "probability": null, "visib": 0.25, "wxString": "FZFG"}, {"timeFrom": 1794769200, "timeTo": 1794776400, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 1, "wxString": "BR"}, {"timeFrom": 1794776400, "timeTo": 1794787200, "timeBec": null, "fcstChange": "FM", "probability": null, "visib": 0.75, "wxString": "BR"}, {"timeFrom": 1794776400, "timeTo": 1794787200, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 6, "wxString": "BR"}, {"timeFrom": 1794787200, "timeTo": 1794801600, "timeBec": null, "fcstChange": "FM", "probability": null, "visib": "6+", "wxString": null}, {"timeFrom": 1794801600, "timeTo": 1794812400, "timeBec": null, "fcstChange": "FM", "probability": null, "visib": "6+", "wxString": null}, {"timeFrom": 1794801600, "timeTo": 1794812400, "timeBec": null, "fcstChange": "PROB", "probability": 30, "visib": 0.5, "wxString": "FZFG"}]}""")

# TAF VTCN 151700Z 1518/1618 … BECMG 1521/1523 0800 FG BECMG 1601/1603 9999 NSW …
#   fog group 15 21:00Z -> 16 01:00Z as AWC times it; the clearing BECMG starts 16 01:00Z and is complete 16 03:00Z
VTCN = json.loads("""{"icaoId": "VTCN", "issueTime": "2026-11-15T17:00:00.000Z", "validTimeFrom": 1794765600, "validTimeTo": 1794852000, "rawTAF": "TAF VTCN 151700Z 1518/1618 03005KT 9999 SCT030 BECMG 1521/1523 00000KT 0800 FG BECMG 1601/1603 01005KT 9999 NSW BECMG 1606/1608 16005KT", "mostRecent": 1, "fcsts": [{"timeFrom": 1794765600, "timeTo": 1794776400, "timeBec": null, "fcstChange": null, "probability": null, "visib": "6+", "wxString": null}, {"timeFrom": 1794776400, "timeTo": 1794790800, "timeBec": 1794783600, "fcstChange": "BECMG", "probability": null, "visib": 0.5, "wxString": "FG"}, {"timeFrom": 1794790800, "timeTo": 1794808800, "timeBec": 1794798000, "fcstChange": "BECMG", "probability": null, "visib": "6+", "wxString": "NSW"}, {"timeFrom": 1794808800, "timeTo": 1794852000, "timeBec": 1794816000, "fcstChange": "BECMG", "probability": null, "visib": "6+", "wxString": "NSW"}]}""")

# TAF AMD CYCG 151453Z 1515/1524 … PROB30 1515/1518 1/2SM FG … FM151800 P6SM … TEMPO 1518/1524 3SM -SHRA BR
#   the only fog group (PROB30, 1/2 mi FG) ran 15 15:00Z-18:00Z; the TAF itself runs to 16 00:00Z
CYCG = json.loads("""{"icaoId": "CYCG", "issueTime": "2026-11-15T14:53:00.000Z", "validTimeFrom": 1794754800, "validTimeTo": 1794787200, "rawTAF": "TAF AMD CYCG 151453Z 1515/1524 VRB03KT 6SM BR OVC040 TEMPO 1515/1518 2SM -RA BR SCT002 OVC030 PROB30 1515/1518 1/2SM FG VV002 FM151800 18006KT P6SM BKN040 TEMPO 1518/1524 3SM -SHRA BR BKN020 RMK NXT FCST BY 152000Z", "mostRecent": 1, "fcsts": [{"timeFrom": 1794754800, "timeTo": 1794765600, "timeBec": null, "fcstChange": null, "probability": null, "visib": 6, "wxString": "BR"}, {"timeFrom": 1794754800, "timeTo": 1794765600, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 2, "wxString": "-RA BR"}, {"timeFrom": 1794754800, "timeTo": 1794765600, "timeBec": null, "fcstChange": "PROB", "probability": 30, "visib": 0.5, "wxString": "FG"}, {"timeFrom": 1794765600, "timeTo": 1794787200, "timeBec": null, "fcstChange": "FM", "probability": null, "visib": "6+", "wxString": null}, {"timeFrom": 1794765600, "timeTo": 1794787200, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 3, "wxString": "-SHRA BR"}]}""")

# TAF VERK 150200Z 1503/1512 … 1000 MIFG … — valid to 15 12:00Z, AWC still served it at 19:14Z
VERK = json.loads("""{"icaoId": "VERK", "issueTime": "2026-11-15T02:00:00.000Z", "validTimeFrom": 1794711600, "validTimeTo": 1794744000, "rawTAF": "TAF VERK 150200Z 1503/1512 00000KT 1000 MIFG NSC BECMG 1505/1507 34003KT 2500 HZ FEW020", "mostRecent": 1, "fcsts": [{"timeFrom": 1794711600, "timeTo": 1794718800, "timeBec": null, "fcstChange": null, "probability": null, "visib": 0.62, "wxString": "MIFG"}, {"timeFrom": 1794718800, "timeTo": 1794744000, "timeBec": 1794726000, "fcstChange": "BECMG", "probability": null, "visib": 1.55, "wxString": "HZ"}]}""")

# TAF FZIC 270500Z 2706/2812 … FM280000 0800 FG … — issued 27 OCTOBER, valid to 28 Oct 12:00Z;
# AWC was still serving it as the station's latest TAF on 15 November (and on 28 November)
FZIC = json.loads("""{"icaoId": "FZIC", "issueTime": "2026-10-27T05:00:00.000Z", "validTimeFrom": 1793080800, "validTimeTo": 1793188800, "rawTAF": "TAF FZIC 270500Z 2706/2812 27010KT 7000 SCT010 FEW016CB TEMPO 2707/2710 3000 TSRA BECMG 2710/2712 6000 NSW BKN016 BECMG 2715/2717 NSC FM280000 00000KT 0800 FG BKN002 FM280700 27008KT 7000 NSW SCT014", "mostRecent": 1, "fcsts": [{"timeFrom": 1793080800, "timeTo": 1793095200, "timeBec": null, "fcstChange": null, "probability": null, "visib": 4.35, "wxString": null}, {"timeFrom": 1793084400, "timeTo": 1793095200, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 1.86, "wxString": "TSRA"}, {"timeFrom": 1793095200, "timeTo": 1793113200, "timeBec": 1793102400, "fcstChange": "BECMG", "probability": null, "visib": 3.73, "wxString": "NSW"}, {"timeFrom": 1793113200, "timeTo": 1793145600, "timeBec": 1793120400, "fcstChange": "BECMG", "probability": null, "visib": 3.73, "wxString": "NSW"}, {"timeFrom": 1793145600, "timeTo": 1793170800, "timeBec": null, "fcstChange": "FM", "probability": null, "visib": 0.5, "wxString": "FG"}, {"timeFrom": 1793170800, "timeTo": 1793188800, "timeBec": null, "fcstChange": "FM", "probability": null, "visib": 4.35, "wxString": "NSW"}]}""")

# TAF EIKN 272300Z 2800/2824 … 1500 BR … BECMG 2800/2802 9999 NSW … PROB30 TEMPO 2809/2811 0800 RADZ FG …
#   fetched 2026-11-28 02:26Z. The opening line (0.93 mi) is replaced by a BECMG that starts with the validity
#   and is complete at 28 02:00Z — yet AWC gives that opening line timeTo = the end of the TAF
EIKN = json.loads("""{"icaoId": "EIKN", "issueTime": "2026-11-27T23:00:00.000Z", "validTimeFrom": 1795824000, "validTimeTo": 1795910400, "rawTAF": "TAF EIKN 272300Z 2800/2824 14012KT 1500 BR BKN002 BECMG 2800/2802 9999 NSW SCT006 TEMPO 2802/2810 15015G27KT TEMPO 2807/2811 4000 -RA BKN006 PROB40 TEMPO 2808/2811 2000 RA BKN002 BECMG 2809/2811 23008KT PROB30 TEMPO 2809/2811 0800 RADZ FG BKN001 BECMG 2816/2818 19010KT", "mostRecent": 1, "fcsts": [{"timeFrom": 1795824000, "timeTo": 1795910400, "timeBec": null, "fcstChange": null, "probability": null, "visib": 0.93, "wxString": "BR"}, {"timeFrom": 1795824000, "timeTo": 1795856400, "timeBec": 1795831200, "fcstChange": "BECMG", "probability": null, "visib": "6+", "wxString": "NSW"}, {"timeFrom": 1795831200, "timeTo": 1795860000, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": "", "wxString": null}, {"timeFrom": 1795849200, "timeTo": 1795863600, "timeBec": null, "fcstChange": "TEMPO", "probability": null, "visib": 2.49, "wxString": "-RA"}, {"timeFrom": 1795852800, "timeTo": 1795863600, "timeBec": null, "fcstChange": "TEMPO", "probability": 40, "visib": 1.24, "wxString": "RA"}, {"timeFrom": 1795856400, "timeTo": 1795881600, "timeBec": 1795863600, "fcstChange": "BECMG", "probability": null, "visib": "6+", "wxString": "NSW"}, {"timeFrom": 1795856400, "timeTo": 1795863600, "timeBec": null, "fcstChange": "TEMPO", "probability": 30, "visib": 0.5, "wxString": "RA DZ FG"}, {"timeFrom": 1795881600, "timeTo": 1795910400, "timeBec": 1795888800, "fcstChange": "BECMG", "probability": null, "visib": "6+", "wxString": "NSW"}]}""")
DUBLIN = ZoneInfo("Europe/Dublin")  # GMT in November

# TAF MDBH 151600Z COR 1518/1618 … 9999 … PROB40 1520/1602 7000 TSRA … BECMG 1600/1602 00000KT
#   AWC decoded two of the three groups: the final BECMG (a wind change) is missing, and the opening line is given
#   timeTo 16 00:00Z — where that lost BECMG starts, 18 h before the TAF ends (16 18:00Z)
MDBH = json.loads("""{"icaoId": "MDBH", "issueTime": "2026-11-15T16:00:00.000Z", "validTimeFrom": 1794765600, "validTimeTo": 1794852000, "rawTAF": "TAF MDBH 151600Z COR 1518/1618 13012KT 9999 FEW020CB BKN022 SCT100 PROB40 1520/1602 7000 TSRA FEW014CB BKN015 BKN070 BECMG 1600/1602 00000KT", "mostRecent": 1, "fcsts": [{"timeFrom": 1794765600, "timeTo": 1794787200, "timeBec": null, "fcstChange": null, "probability": null, "visib": "6+", "wxString": null}, {"timeFrom": 1794772800, "timeTo": 1794794400, "timeBec": null, "fcstChange": "PROB", "probability": 40, "visib": 4.35, "wxString": "TSRA"}]}""")
SANTO_DOMINGO = ZoneInfo("America/Santo_Domingo")  # UTC-4 all year

# what the code returned for these records before it knew the build time
# (commit d38c58aa) — the wording a still-ahead forecast must keep, apart
# from the date now carried by the issue stamp
BEFORE = {
    "VABP": {"issued": "2026-11-15T17:00Z", "validTo": "2026-11-16T03:00Z", "horizonEnd": "2026-11-16T03:00Z",
             "verdict": "possible dense fog", "minVisMi": 0.75,
             "sentence": "The airport's own terminal forecast (TAF issued 17:00Z) carries a temporary/probability group "
                         "with dense fog around Monday 4 am through Monday 8 am.",
             "raw": VABP["rawTAF"]},
    "VTCN": {"issued": "2026-11-15T17:00Z", "validTo": "2026-11-16T18:00Z", "horizonEnd": "2026-11-16T18:00Z",
             "verdict": "dense fog", "minVisMi": 0.5,
             "sentence": "The airport's own terminal forecast (TAF issued 17:00Z) calls for dense fog (visibility under "
                         "1 mile) from about Monday 4 am through Tuesday 1 am.",
             "raw": VTCN["rawTAF"]},
}
TZ = {"VABP": ZoneInfo("Asia/Kolkata"), "VTCN": ZoneInfo("Asia/Bangkok"), "CYXJ": ZoneInfo("America/Dawson_Creek"),
      "CYCG": ZoneInfo("America/Vancouver"), "VERK": ZoneInfo("Asia/Kolkata"), "FZIC": ZoneInfo("Africa/Lubumbashi")}
SAVED = Z(15, 19, 14)  # when the bake that saved these records ran


# ------------------------------------------------------ records built by hand

def group(t0, t1, visib="6+", wx=None, change=None, prob=None, bec=None):
    return {"timeFrom": int(t0.timestamp()), "timeTo": int(t1.timestamp()), "timeBec": int(bec.timestamp()) if bec else None,
            "fcstChange": change, "probability": prob, "visib": visib, "wxString": wx}


def taf(issued, valid_from, valid_to, groups, icao="EGNH"):
    return {"icaoId": icao, "issueTime": issued.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "validTimeFrom": int(valid_from.timestamp()), "validTimeTo": int(valid_to.timestamp()),
            "rawTAF": f"TAF {icao} {issued:%d%H%M}Z {valid_from:%d%H}/{valid_to:%d%H} HANDBUILT", "mostRecent": 1, "fcsts": groups}


def morning_fog():
    """Issued Thursday 19 Nov 04:40Z, valid Thursday 05:00Z to Friday 05:00Z.
    The only dense-fog group runs Thursday 05:00-09:00; clear from 09:00."""
    return taf(Z(19, 4, 40), Z(19, 5), Z(20, 5),
               [group(Z(19, 5), Z(19, 9), 0.25, "FG"),
                group(Z(19, 9), Z(20, 5), "6+", None, "FM")])


def passing_fog():
    """Same validity, clear throughout, with a TEMPO (Thursday 06:00-10:00,
    0.19 mi FG) and a PROB30 (Friday 01:00-04:00, 0.5 mi FG)."""
    return taf(Z(19, 4, 40), Z(19, 5), Z(20, 5),
               [group(Z(19, 5), Z(20, 5), "6+"),
                group(Z(19, 6), Z(19, 10), 0.19, "FG", "TEMPO"),
                group(Z(20, 1), Z(20, 4), 0.5, "FG", "PROB", 30)])


def mdbh_with(visib, wx):
    """The real MDBH record exactly as AWC decoded it — opening line cut off
    18 h before the TAF ends — with other weather on that opening line."""
    rec = json.loads(json.dumps(MDBH))
    rec["fcsts"][0].update(visib=visib, wxString=wx)
    return rec


# ------------------------------------------------------------------ helpers

_ATLAS = {}


def airport(icao, **over):
    """A real atlas airport, read the way main() reads the atlas. Should a
    station leave the atlas one day, any other airport's record stands in
    under its code: these tests are about the TAF line, not the climatology."""
    if not _ATLAS:
        doc = bp.read_json(bp.PIPE_OUT / "app" / "airports.json", bp.APP_DATA / "airports.json")
        _ATLAS["window"] = doc.get("window", {"start": "2016-01-01", "through": "2025-12-31"})
        _ATLAS["by"] = {a["icao"]: a for a in doc["airports"]}
    base = _ATLAS["by"].get(icao) or next(iter(_ATLAS["by"].values()))
    return {**base, "icao": icao, "_covered": False, **over}


def bake(icao, rec, now, **over):
    """The airport page and its data.json as main() bakes them for a station
    with no Fog Atlas forecast: page(…, covered=False, fc=None, taf=rec)."""
    a = airport(icao, **over)
    return bp.page(a, None, False, {}, None, _ATLAS["window"], {}, now, taf=rec)


def bake_city(icao, rec, now, **over):
    a = airport(icao, **over)
    city = (a.get("muni") or "Testville", f"{a['country']}-XX", a["country"], "testville")
    return bp.city_page(city, [a], a, None, _ATLAS["window"], {}, {}, now, taf=rec)


def undated(sentence):
    """The sentence with the date taken off its issue stamp."""
    return re.sub(r"(TAF issued \d\d:\d\dZ) \d{1,2} [A-Z][a-z]{2}\)", r"\1)", sentence)


@contextmanager
def temp_constant(name, value):
    old = getattr(bp, name)
    setattr(bp, name, value)
    try:
        yield
    finally:
        setattr(bp, name, old)


TAF_PHRASE = "terminal forecast (TAF"  # opens the TAF sentence wherever it is printed


# --------------------------------------------- A1: an ended TAF says nothing

def test_ended_taf_gets_no_summary():
    # FZIC: issued 27 October, the "latest TAF" AWC returned on 15 November
    assert bp.taf_summary(FZIC, TZ["FZIC"], SAVED) is None
    # VERK: ended seven hours before the bake that fetched it
    assert bp.taf_summary(VERK, TZ["VERK"], SAVED) is None
    # VABP ten days on — the case found on the live site
    assert bp.taf_summary(VABP, TZ["VABP"], SAVED + timedelta(days=10)) is None


def test_live_taf_is_kept():
    s = bp.taf_summary(VABP, TZ["VABP"], SAVED)
    assert s and s["verdict"] == "possible dense fog" and s["minVisMi"] == 0.75
    assert s["issued"] == "2026-11-15T17:00Z" and s["validTo"] == "2026-11-16T03:00Z" == s["horizonEnd"]
    assert s["raw"] == VABP["rawTAF"]


def test_build_time_is_required():
    """No caller can ask for a summary without saying when the page is built."""
    p = inspect.signature(bp.taf_summary).parameters
    assert list(p)[:3] == ["rec", "tz", "now_utc"] and p["now_utc"].default is inspect.Parameter.empty


def test_boundary_validity_ends_exactly_at_the_build():
    end = datetime.fromtimestamp(VABP["validTimeTo"], tz=UTC)  # 16 Nov 03:00Z
    assert bp.taf_summary(VABP, TZ["VABP"], end) is None, "validTimeTo == build time is ended"
    assert bp.taf_summary(VABP, TZ["VABP"], end + timedelta(seconds=1)) is None
    assert bp.taf_summary(VABP, TZ["VABP"], end - timedelta(seconds=1))["verdict"] == "possible dense fog"


def test_no_usable_valid_time_to_is_judged_by_issue_plus_30h():
    issued = Z(15, 17)
    bad_values = [None, 0, -5, "1794798000", "2026-11-16T03:00:00Z", True, [], {}, float("nan"), float("inf"), 1e300]
    recs = [{k: v for k, v in VABP.items() if k != "validTimeTo"}] + [{**VABP, "validTimeTo": v} for v in bad_values]
    for rec in recs:
        shown = repr(rec.get("validTimeTo", "<missing>"))
        s = bp.taf_summary(rec, TZ["VABP"], issued + timedelta(hours=29, minutes=59))
        assert s is not None, f"validTimeTo {shown}: still inside issue + 30 h"
        assert s["validTo"] == s["horizonEnd"] == "2026-11-16T23:00Z", shown
        assert bp.taf_summary(rec, TZ["VABP"], issued + timedelta(hours=30)) is None, f"validTimeTo {shown}: issue + 30 h reached"
        assert bp.taf_summary(rec, TZ["VABP"], issued + timedelta(days=10)) is None, shown


def test_a_far_future_valid_time_to_cannot_keep_a_taf_alive():
    """A validity end a year out (a typo upstream) is capped by issue + 30 h."""
    rec = {**VABP, "validTimeTo": VABP["validTimeTo"] + 365 * 86400}
    assert bp.taf_summary(rec, TZ["VABP"], Z(15, 17) + timedelta(hours=29)) is not None
    assert bp.taf_summary(rec, TZ["VABP"], Z(15, 17) + timedelta(hours=30)) is None
    assert bp.taf_summary(rec, TZ["VABP"], Z(25, 10)) is None


def test_window_closed_before_validity_end_says_nothing():
    """FZIC is valid 31 h after issue, and the page speaks for 30 h ("through
    …" names that hour). In the last hour the sentence would end in the past."""
    assert bp.taf_summary(FZIC, TZ["FZIC"], Z(28, 10, 59, month=10)) is not None
    assert bp.taf_summary(FZIC, TZ["FZIC"], Z(28, 11, 0, month=10)) is None   # issue + 30 h; validity runs to 12:00Z
    assert bp.taf_summary(FZIC, TZ["FZIC"], Z(28, 11, 30, month=10)) is None


def test_unreadable_record_never_reaches_a_page_and_never_raises():
    now = SAVED
    for rec in (None, {}, {**VABP, "fcsts": []}, {**VABP, "issueTime": None}, {**VABP, "issueTime": ""},
                {**VABP, "issueTime": "not a time"}, {**VABP, "issueTime": 1794762000},
                {k: v for k, v in VABP.items() if k != "issueTime"}):
        assert bp.taf_summary(rec, UTC, now) is None
    # an issue time with no zone is UTC (AWC's times are); with no validity end it must still compare
    naive = {**VABP, "issueTime": "2026-11-15T17:00:00", "validTimeTo": None}
    assert bp.taf_summary(naive, TZ["VABP"], now)["issued"] == "2026-11-15T17:00Z"
    assert bp.taf_summary(naive, TZ["VABP"], now + timedelta(days=3)) is None


def test_taf_expired_is_the_same_rule_the_summary_applies():
    """main() drops with taf_expired(); taf_summary() refuses on its own.
    For every record that can be read the two must agree at every moment."""
    recs = {"VABP": VABP, "CYXJ": CYXJ, "VTCN": VTCN, "CYCG": CYCG, "VERK": VERK, "FZIC": FZIC}
    recs["no validTimeTo"] = {**VABP, "validTimeTo": None}
    recs["validTimeTo a year out"] = {**VABP, "validTimeTo": VABP["validTimeTo"] + 365 * 86400}
    n = 0
    for name, rec in recs.items():
        issued, valid_to, end = bp.taf_window(rec)
        assert end == min(valid_to, issued + timedelta(hours=30)), name
        for t in (issued, end - timedelta(seconds=1), end, end + timedelta(seconds=1), valid_to, SAVED,
                  SAVED + timedelta(hours=6), SAVED + timedelta(days=10)):
            gone = bp.taf_summary(rec, TZ.get(name, UTC), t) is None
            assert bp.taf_expired(rec, t) == gone == (end <= t), (name, t)
            n += 1
    assert n == 64
    # a record that cannot be read is not "ended"; it has no summary either way
    for rec in (None, {}, {**VABP, "fcsts": []}, {**VABP, "issueTime": "not a time"}):
        assert bp.taf_window(rec) is None and bp.taf_expired(rec, SAVED) is False
        assert bp.taf_summary(rec, UTC, SAVED) is None


def test_drop_expired_tafs_keeps_only_live_ones_and_logs_the_count():
    tafs = {"FZIC": FZIC, "VERK": VERK, "VABP": VABP, "CYXJ": CYXJ}
    before = json.dumps(tafs, sort_keys=True)
    out = io.StringIO()
    with redirect_stdout(out):
        live = bp.drop_expired_tafs(tafs, SAVED)
    assert sorted(live) == ["CYXJ", "VABP"]
    assert live["VABP"] is VABP, "live records pass through untouched"
    assert json.dumps(tafs, sort_keys=True) == before and len(tafs) == 4, "the caller's dict is not edited"
    assert "2 of 4" in out.getvalue(), out.getvalue()
    quiet = io.StringIO()
    with redirect_stdout(quiet):
        assert bp.drop_expired_tafs({"VABP": VABP}, SAVED) == {"VABP": VABP}
        assert bp.drop_expired_tafs({}, SAVED) == {}
    assert quiet.getvalue() == "", "nothing dropped, nothing logged"


def test_main_drops_ended_tafs_after_the_cache_is_saved():
    """main() cannot be run here (network, 7,000 pages); its wiring is read."""
    src = inspect.getsource(bp.main)
    merge = src.index("tafs, obs = awc_with_last_good(tafs, obs, now_utc)")
    drop = src.index("tafs = drop_expired_tafs(tafs, now_utc)")
    first_use = min(src.index("clean_upstream(tafs)"), src.index("tafs.get("))
    assert merge < drop < first_use, "ended TAFs go after the cache is saved and before anything reads tafs"
    assert src.count("awc_with_last_good(") == 1 and "taf=tafs.get(icao)" in src and "taf=tafs.get(prim)" in src


def test_cache_is_still_written_and_may_hold_an_ended_taf():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "out" / "awc_cache.json"
        with temp_constant("AWC_CACHE", cache), redirect_stdout(io.StringIO()):
            # first bake: the fresh pass carries one ended TAF and one live one
            tafs, obs = bp.awc_with_last_good({"FZIC": FZIC, "VABP": VABP}, {}, SAVED)
            assert cache.exists(), "the cache is written, as before"
            saved = json.loads(cache.read_text())
            assert saved["saved"] == "2026-11-15T19:14Z" and sorted(saved["taf"]) == ["FZIC", "VABP"], \
                "the ended record stays in the cache"
            assert sorted(bp.drop_expired_tafs(tafs, SAVED)) == ["VABP"], "…and does not go on to a page"
            assert sorted(json.loads(cache.read_text())["taf"]) == ["FZIC", "VABP"], "dropping does not rewrite the cache"
            # next bake, AWC down: VABP is young enough for the cache rule
            # (issued 11 h ago) but its validity ended an hour ago
            later = Z(16, 4)
            tafs2, _ = bp.awc_with_last_good({}, {}, later)
            assert sorted(tafs2) == ["VABP"], "reused from the cache by issue age (FZIC is too old even for that)"
            assert bp.drop_expired_tafs(tafs2, later) == {}, "a reused record past its validity is not quoted"
            assert json.loads(cache.read_text())["saved"] == "2026-11-16T04:00Z"


# ------------------------------------- A1 on the outputs: page, city, hub

def test_page_with_an_ended_taf_prints_none_of_it():
    """VERK at the bake that fetched it: valid to 12:00Z, built 19:14Z. The
    old page led with "Dense fog in the airport's own forecast"."""
    html, data = bake("VERK", VERK, SAVED)
    blob = json.dumps(data)
    assert TAF_PHRASE not in html and TAF_PHRASE not in blob, "no TAF sentence in the page, its meta, JSON-LD or data.json"
    assert VERK["rawTAF"] not in html and VERK["rawTAF"] not in blob
    assert "taf" not in data["forecast"]
    assert "Dense fog in the airport's own forecast" not in html and "<b>Climatology answer</b>" in html
    assert "calls for dense fog" not in html and "calls for dense fog" not in blob
    # main()'s sitemap freshness and its "with TAF" count both read this
    assert not bool(data["forecast"].get("taf"))
    # FZIC, 19 days on
    html, data = bake("FZIC", FZIC, SAVED)
    assert TAF_PHRASE not in html and "taf" not in data["forecast"] and FZIC["rawTAF"] not in json.dumps(data)


def test_page_with_a_live_taf_still_prints_it():
    html, data = bake("VABP", VABP, SAVED)
    assert TAF_PHRASE in html and data["forecast"]["taf"]["verdict"] == "possible dense fog"
    assert data["forecast"]["taf"]["raw"] == VABP["rawTAF"]
    assert "carries a temporary/probability group with dense fog around Monday 4 am through Monday 8 am." in data["forecast"]["wording"]
    assert "sentence" not in data["forecast"]["taf"]


def test_city_page_with_an_ended_taf_prints_none_of_it():
    html, data = bake_city("VERK", VERK, SAVED)
    blob = json.dumps(data)
    assert TAF_PHRASE not in html and TAF_PHRASE not in blob and VERK["rawTAF"] not in blob
    assert "taf" not in data["forecast"] and "calls for dense fog" not in html
    html, data = bake_city("VABP", VABP, SAVED)
    assert TAF_PHRASE in html and data["forecast"]["taf"]["verdict"] == "possible dense fog"


def test_hub_lists_only_tafs_that_still_speak():
    """Five stations as the bake of 2026-11-15 19:14Z had them. FZIC and VERK
    had ended; CYCG's only fog group was over; CYXJ and VABP still call fog."""
    entries = []
    for icao, rec in (("FZIC", FZIC), ("VERK", VERK), ("CYCG", CYCG), ("CYXJ", CYXJ), ("VABP", VABP)):
        a = airport(icao)
        _, data = bake(icao, rec, SAVED)
        entries.append({"icao": icao, "name": a["name"], "country": a["country"], "place": a.get("muni"),
                        "fc": data["forecast"], "obs": data.get("currentObservation")})
    html, data = bp.forecast_hub(entries, SAVED)
    assert [(r["icao"], r["verdict"]) for r in data["tafDenseFog"]] == [("CYXJ", "dense fog"), ("VABP", "possible dense fog")]
    assert all(datetime.fromisoformat(r["tafIssued"].replace("Z", "+00:00")) > SAVED - timedelta(hours=30)
               for r in data["tafDenseFog"])
    assert "1 more airports' own terminal forecasts call for dense fog" in data["summary"]
    for gone in ("fzic", "verk", "cycg"):
        assert f'href="/fog/{gone}/"' not in html, f"{gone} is not a hub row"
    assert 'href="/fog/cyxj/"' in html and 'href="/fog/vabp/"' in html
    assert html.count("TAF issued") == 2


# ------------------- A2: inside a live TAF, a group that is over is not news

def test_page_built_in_the_evening_does_not_announce_the_morning_fog():
    """Built Thursday 20:00 local; the only dense-fog group ran 05:00-09:00."""
    html, data = bake("EGNH", morning_fog(), Z(19, 20), tz="Europe/London")
    assert "from about Thursday 5 am" not in html
    assert "calls for dense fog" not in html and "Dense fog in the airport's own forecast" not in html
    t = data["forecast"]["taf"]
    assert t["verdict"] == "no fog" and t["minVisMi"] is None
    assert "does not forecast dense fog through Friday 5 am." in data["forecast"]["wording"]
    # the hub would not list it either
    assert t["verdict"] not in ("dense fog", "possible dense fog")


def test_group_still_ahead_keeps_the_old_wording():
    s = bp.taf_summary(morning_fog(), LONDON, Z(19, 4, 50))
    assert s["verdict"] == "dense fog" and s["minVisMi"] == 0.25
    assert undated(s["sentence"]) == ("The airport's own terminal forecast (TAF issued 04:40Z) calls for dense fog "
                                      "(visibility under 1 mile) from about Thursday 5 am through Friday 5 am.")


def test_real_records_all_ahead_match_the_code_as_it_was():
    """Built after issue and before the first group starts, nothing in the
    record is past: every field is what the old code returned, and the
    sentence differs only by the date on the stamp."""
    for icao in ("VABP", "VTCN"):
        rec = {"VABP": VABP, "VTCN": VTCN}[icao]
        s = bp.taf_summary(rec, TZ[icao], Z(15, 17, 30))
        assert {**s, "sentence": undated(s["sentence"])} == BEFORE[icao], icao


def test_group_under_way_is_said_as_under_way():
    s = bp.taf_summary(morning_fog(), LONDON, Z(19, 7))
    assert s["verdict"] == "dense fog" and s["minVisMi"] == 0.25
    assert s["sentence"] == ("The airport's own terminal forecast (TAF issued 04:40Z 19 Nov) calls for dense fog "
                             "(visibility under 1 mile) in a period already under way at page build (Thursday 7 am), "
                             "through Friday 5 am.")
    assert "from about" not in s["sentence"]
    # a group that starts exactly at the build is under way, not "from about" now
    assert "already under way at page build (Thursday 5 am)," in bp.taf_summary(morning_fog(), LONDON, Z(19, 5))["sentence"]
    # real record: CYXJ's 1/4 mi FZFG began 14 minutes before the bake (12:14 pm local)
    s = bp.taf_summary(CYXJ, TZ["CYXJ"], SAVED)
    assert s["verdict"] == "dense fog" and "from about Sunday 12 pm" not in s["sentence"]
    assert "in a period already under way at page build (Sunday 12 pm), through Monday 12 am." in s["sentence"]


def test_group_ends_exactly_at_the_build():
    rec = morning_fog()
    assert bp.taf_summary(rec, LONDON, Z(19, 8, 59))["verdict"] == "dense fog"
    s = bp.taf_summary(rec, LONDON, Z(19, 9))   # timeTo == build time: over
    assert s["verdict"] == "no fog" and s["minVisMi"] is None
    assert undated(s["sentence"]) == ("The airport's own terminal forecast (TAF issued 04:40Z) does not forecast dense "
                                      "fog through Friday 5 am.")


def test_tempo_and_prob_groups_follow_the_clock_too():
    rec = passing_fog()
    def at(*t):
        return bp.taf_summary(rec, LONDON, Z(*t))
    s = at(19, 5, 10)   # both ahead: the old wording
    assert s["verdict"] == "possible dense fog" and s["minVisMi"] == 0.19
    assert undated(s["sentence"]) == ("The airport's own terminal forecast (TAF issued 04:40Z) carries a temporary/probability "
                                      "group with dense fog around Thursday 6 am through Friday 5 am.")
    s = at(19, 8)       # TEMPO running
    assert s["verdict"] == "possible dense fog" and "around Thursday 6 am" not in s["sentence"]
    assert s["sentence"].endswith("carries a temporary/probability group with dense fog already under way at page build "
                                  "(Thursday 8 am), through Friday 5 am.")
    s = at(19, 10)      # TEMPO over (ends exactly now); the PROB30 is what is left
    assert s["verdict"] == "possible dense fog" and s["minVisMi"] == 0.5
    assert s["sentence"].endswith("with dense fog around Friday 1 am through Friday 5 am.")
    s = at(20, 2)       # PROB30 running
    assert "already under way at page build (Friday 2 am)," in s["sentence"]
    s = at(20, 4)       # both over, an hour of validity left
    assert s["verdict"] == "no fog" and s["minVisMi"] is None
    # a PROB40 TEMPO as AWC decodes it (fcstChange TEMPO + probability) is the same kind of group
    rec2 = taf(Z(19, 4, 40), Z(19, 5), Z(20, 5), [group(Z(19, 5), Z(20, 5), "6+"),
                                                group(Z(19, 6), Z(19, 10), 0.3, "FG", "TEMPO", 40)])
    assert bp.taf_summary(rec2, LONDON, Z(19, 5, 30))["verdict"] == "possible dense fog"
    assert bp.taf_summary(rec2, LONDON, Z(19, 10))["verdict"] == "no fog"


def test_real_record_whose_only_fog_group_was_over():
    """CYCG, built 19:14Z: the PROB30 1/2SM FG ran 15:00-18:00Z. The old page
    still said "…dense fog around Sunday 8 am", over an hour after it ended."""
    s = bp.taf_summary(CYCG, TZ["CYCG"], SAVED)
    assert s["verdict"] == "mist or haze" and s["minVisMi"] == 3.0   # what is left: TEMPO 3SM -SHRA BR
    assert "dense fog around" not in s["sentence"] and "Sunday 8 am" not in s["sentence"]
    assert "visibility down to about 4,800 m" in s["sentence"]
    html, data = bake("CYCG", CYCG, SAVED)
    assert "dense fog around" not in html and data["forecast"]["taf"]["verdict"] == "mist or haze"


def test_later_fog_group_gives_the_start_time_once_the_first_is_over():
    """CYXJ: 1/4 mi fog to 21:00Z, 3/4 mi to 00:00Z, clear, then PROB30 fog at 04:00Z."""
    s = bp.taf_summary(CYXJ, TZ["CYXJ"], Z(15, 21, 30))   # first group over, the 3/4 mi group running
    assert s["verdict"] == "dense fog" and s["minVisMi"] == 0.5 and "already under way at page build (Sunday 2 pm)," in s["sentence"]
    s = bp.taf_summary(CYXJ, TZ["CYXJ"], Z(16, 0, 14))    # both over: only the PROB30 is ahead
    assert s["verdict"] == "possible dense fog" and s["minVisMi"] == 0.5
    assert s["sentence"].endswith("with dense fog around Sunday 9 pm through Monday 12 am.")
    assert "Sunday 12 pm" not in s["sentence"]


def test_fog_being_replaced_by_becmg_counts_until_the_change_is_complete():
    """VTCN: AWC ends the fog group at 16 01:00Z, where "BECMG 1601/1603 9999
    NSW" starts — but until 03:00Z the TAF still allows the fog."""
    def at(*t):
        return bp.taf_summary(VTCN, TZ["VTCN"], Z(*t))
    assert "from about Monday 4 am" in at(15, 19, 14)["sentence"]          # ahead
    assert "already under way at page build (Monday 7 am)," in at(16, 0, 30)["sentence"]
    s = at(16, 1, 30)   # inside the clearing transition
    assert s["verdict"] == "dense fog" and s["minVisMi"] == 0.5 and "already under way at page build (Monday 8 am)," in s["sentence"]
    assert at(16, 2, 59)["verdict"] == "dense fog"
    s = at(16, 3, 0)    # transition complete
    assert s["verdict"] == "no fog" and s["minVisMi"] is None and "dense fog (visibility" not in s["sentence"]


def test_tempo_fog_is_not_stretched_by_a_becmg_that_starts_when_it_ends():
    rec = taf(Z(19, 4, 40), Z(19, 5), Z(20, 5),
              [group(Z(19, 5), Z(19, 9), "6+"),
               group(Z(19, 6), Z(19, 9), 0.25, "FG", "TEMPO"),
               group(Z(19, 9), Z(20, 5), "6+", "NSW", "BECMG", bec=Z(19, 11))])
    assert bp.taf_summary(rec, LONDON, Z(19, 8, 59))["verdict"] == "possible dense fog"
    assert bp.taf_summary(rec, LONDON, Z(19, 9))["verdict"] == "no fog"


def test_lowest_visibility_and_mist_come_from_what_is_left():
    rec = taf(Z(19, 4, 40), Z(19, 5), Z(20, 5),
              [group(Z(19, 5), Z(19, 9), 1.5, "BR"),            # mist, over at 09:00
               group(Z(19, 9), Z(19, 15), 4.0, "HZ", "FM"),      # haze
               group(Z(19, 15), Z(20, 5), "6+", None, "FM")])
    s = bp.taf_summary(rec, LONDON, Z(19, 4, 50))
    assert s["verdict"] == "mist or haze" and s["minVisMi"] == 1.5 and "about 2,400 m" in s["sentence"]
    s = bp.taf_summary(rec, LONDON, Z(19, 10))
    assert s["verdict"] == "mist or haze" and s["minVisMi"] == 4.0 and "about 6,400 m" in s["sentence"]
    s = bp.taf_summary(rec, LONDON, Z(19, 16))
    assert s["verdict"] == "no fog" and s["minVisMi"] is None


def test_opening_line_replaced_at_once_is_over_when_the_change_is_complete():
    """EIKN, 2026-11-28: "1500 BR … BECMG 2800/2802 9999 NSW". AWC leaves the
    opening line (0.93 mi) running to the end of the TAF; the TAF itself has
    it gone by 02:00Z. What is left after that is the PROB30 fog at 09:00Z."""
    def at(*t):
        return bp.taf_summary(EIKN, DUBLIN, Z(*t))
    assert EIKN["fcsts"][0]["timeTo"] == EIKN["validTimeTo"], "the record as AWC sent it: opening line 'runs' all day"
    s = at(27, 23, 30)   # before validity: the old wording
    assert s["verdict"] == "dense fog" and undated(s["sentence"]).endswith(
        "calls for dense fog (visibility under 1 mile) from about Saturday 12 am through Sunday 12 am.")
    assert "in a period already under way at page build (Saturday 1 am)," in at(28, 1, 59)["sentence"]
    s = at(28, 2)        # BECMG complete: the opening line is over
    assert s["verdict"] == "possible dense fog" and s["minVisMi"] == 0.5
    assert s["sentence"].endswith("carries a temporary/probability group with dense fog around Saturday 9 am through Sunday 12 am.")
    s = at(28, 12)       # the PROB30 is over too
    assert s["verdict"] == "no fog" and s["minVisMi"] is None and "dense fog (visibility" not in s["sentence"]


def test_last_prevailing_group_holds_to_the_end_of_the_taf():
    """MDBH, 2026-11-15. AWC lost the TAF's final group ("BECMG 1600/1602
    00000KT", a wind change) and ends the opening line where that group
    began: 16 00:00Z, 18 h before the TAF does. The opening line's visibility
    is still the forecast for those 18 h; counting it as over made the page
    say "does not forecast dense fog" from a verdict built on no group."""
    assert len(MDBH["fcsts"]) == 2 and MDBH["fcsts"][0]["timeTo"] == MDBH["validTimeTo"] - 18 * 3600, \
        "the record as AWC sent it: the last prevailing group 'ends' 18 h early"
    # the same decoded shape with fog on the opening line (0300 FG)
    fog = mdbh_with(0.19, "FG")
    assert "from about Sunday 2 pm through Monday 2 pm." in bp.taf_summary(fog, SANTO_DOMINGO, Z(15, 17))["sentence"]  # ahead
    for t in (Z(15, 23, 59), Z(16, 0), Z(16, 2), Z(16, 7, 14), Z(16, 17, 59)):   # before AWC's cut, at it, after it
        s = bp.taf_summary(fog, SANTO_DOMINGO, t)
        assert s["verdict"] == "dense fog" and s["minVisMi"] == 0.19, t
        assert "does not forecast dense fog" not in s["sentence"] and "already under way at page build (" in s["sentence"], t
    assert bp.taf_summary(fog, SANTO_DOMINGO, Z(16, 7, 14))["sentence"] == (
        "The airport's own terminal forecast (TAF issued 16:00Z 15 Nov) calls for dense fog (visibility under 1 mile) "
        "in a period already under way at page build (Monday 3 am), through Monday 2 pm.")
    assert bp.taf_summary(fog, SANTO_DOMINGO, Z(16, 18)) is None   # the TAF itself has ended
    # no validTimeTo in the record: the end the page uses in its place (issue + 30 h) is what the line holds to
    for bad in (None, 0, "1794852000"):
        s = bp.taf_summary({**fog, "validTimeTo": bad}, SANTO_DOMINGO, Z(16, 7, 14))
        assert s["verdict"] == "dense fog" and s["validTo"] == s["horizonEnd"] == "2026-11-16T22:00Z", repr(bad)
        assert bp.taf_summary({**fog, "validTimeTo": bad}, SANTO_DOMINGO, Z(16, 21, 59))["verdict"] == "dense fog", repr(bad)
        assert bp.taf_summary({**fog, "validTimeTo": bad}, SANTO_DOMINGO, Z(16, 22)) is None, repr(bad)
    # mist on the opening line (3000 BR) holds the same way, and stays the lowest visibility
    s = bp.taf_summary(mdbh_with(1.86, "BR"), SANTO_DOMINGO, Z(16, 7, 14))
    assert s["verdict"] == "mist or haze" and s["minVisMi"] == 1.86 and "about 3,000 m" in s["sentence"]
    # the record as issued (9999): no fog, before AWC's cut and after it
    for t, low in ((SAVED, 4.35), (Z(16, 7, 14), None)):   # the PROB40's 7000 m counts until it is over (16 02:00Z)
        s = bp.taf_summary(MDBH, SANTO_DOMINGO, t)
        assert s["verdict"] == "no fog" and s["minVisMi"] == low, t
        assert undated(s["sentence"]) == ("The airport's own terminal forecast (TAF issued 16:00Z) does not forecast "
                                          "dense fog through Monday 2 pm.")


def test_page_and_hub_keep_the_fog_of_a_last_group_awc_cut_short():
    """Built 16 Nov 07:14Z, seven hours after AWC's end for the opening line."""
    now, fog = Z(16, 7, 14), mdbh_with(0.19, "FG")
    html, data = bake("MDBH", fog, now, tz="America/Santo_Domingo")
    t = data["forecast"]["taf"]
    assert t["verdict"] == "dense fog" and t["minVisMi"] == 0.19 and t["validTo"] == "2026-11-16T18:00Z"
    assert "Dense fog in the airport's own forecast" in html and "does not forecast dense fog" not in html
    assert "in a period already under way at page build (Monday 3 am), through Monday 2 pm." in data["forecast"]["wording"]
    chtml, cdata = bake_city("MDBH", fog, now, tz="America/Santo_Domingo")
    assert cdata["forecast"]["taf"]["verdict"] == "dense fog" and "does not forecast dense fog" not in chtml
    a = airport("MDBH")
    hub_html, hub = bp.forecast_hub([{"icao": "MDBH", "name": a["name"], "country": a["country"], "place": a.get("muni"),
                                      "fc": data["forecast"], "obs": data.get("currentObservation")}], now)
    assert [(r["icao"], r["verdict"]) for r in hub["tafDenseFog"]] == [("MDBH", "dense fog")]
    assert 'href="/fog/mdbh/"' in hub_html


def test_a_garbled_line_after_the_last_change_does_not_take_its_place():
    """VILK, 2026-11-28: a mistyped final group ("BECMG 2904/906 …") comes back
    from AWC as a line with no change word spanning the whole TAF, listed
    after the real last change. That change is still the forecast's last
    word: cut short, it holds to the end of the TAF all the same."""
    rec = taf(Z(19, 4, 40), Z(19, 5), Z(20, 5),
              [group(Z(19, 5), Z(19, 9), "6+"),
               group(Z(19, 9), Z(19, 12), 0.25, "FG", "FM"),   # the last change, stopped at 12:00
               group(Z(19, 5), Z(20, 5), "", None)])           # the garbled line
    s = bp.taf_summary(rec, LONDON, Z(19, 20))
    assert s["verdict"] == "dense fog" and "already under way at page build (Thursday 8 pm)," in s["sentence"]
    assert bp.taf_summary(rec, LONDON, Z(20, 4, 59))["verdict"] == "dense fog"


def test_when_each_group_stops_applying():
    def ends(fcsts, valid_to=None):
        return [e and e.strftime("%d %H:%M") for e in bp.taf_group_ends(fcsts, valid_to)]

    def valid_to(rec):
        return datetime.fromtimestamp(rec["validTimeTo"], tz=UTC)
    # --- with no end of the TAF given, the groups' own times decide
    # VTCN: base until its BECMG is complete (23:00, AWC says 21:00); fog until the clearing one is (03:00, AWC 01:00)
    assert ends(VTCN["fcsts"]) == ["15 23:00", "16 03:00", "16 08:00", "16 18:00"]
    # EIKN: opening line 02:00 (AWC: end of TAF); TEMPO/PROB groups keep their own end
    assert ends(EIKN["fcsts"]) == ["28 02:00", "28 11:00", "28 10:00", "28 11:00", "28 11:00", "28 18:00", "28 11:00", "29 00:00"]
    # CYXJ: FM changes take over at once; a TEMPO is not stretched by the FM after it
    assert ends(CYXJ["fcsts"]) == ["15 21:00", "15 21:00", "16 00:00", "16 00:00", "16 04:00", "16 07:00", "16 07:00"]
    # a change group dated BEFORE the group it follows in the list (a typo upstream, seen in DNMM
    # 2026-11-15) cannot end that group: AWC's own end stands
    odd = [group(Z(19, 5), Z(19, 12), 0.25, "FG"),
           group(Z(18, 20), Z(19, 5), "6+", "NSW", "BECMG", bec=Z(18, 22))]
    assert ends(odd) == ["19 12:00", "19 05:00"]
    # a second line with no change word (AWC's decoding of a garbled TEMPO, seen in DNGO) ends nothing
    garbled = [group(Z(19, 5), Z(20, 5), 0.25, "FG"), group(Z(19, 5), Z(20, 5), "", None)]
    assert ends(garbled) == ["20 05:00", "20 05:00"]
    # a BECMG whose "complete" time is before its start (DNJO, "BECMG 0719/0712"): its start decides
    back = [group(Z(19, 5), Z(19, 9), 0.25, "FG"), group(Z(19, 9), Z(20, 5), "6+", "NSW", "BECMG", bec=Z(19, 2))]
    assert ends(back) == ["19 09:00", "20 05:00"]
    assert bp.taf_group_ends([], None) == [] == bp.taf_group_ends([], Z(20, 5))
    # --- with the TAF's own end: the last prevailing group holds to it
    # MDBH: AWC stops the opening line at 16 00:00Z, where a BECMG it then lost begins; the TAF runs to 16 18:00Z
    assert ends(MDBH["fcsts"]) == ["16 00:00", "16 02:00"]                     # AWC's own times
    assert ends(MDBH["fcsts"], valid_to(MDBH)) == ["16 18:00", "16 02:00"]     # the PROB40 keeps its own end
    # a record AWC decoded in full: the TAF's end moves nothing
    for rec in (VABP, CYXJ, VTCN, CYCG, VERK, FZIC, EIKN):
        assert ends(rec["fcsts"], valid_to(rec)) == ends(rec["fcsts"]), rec["icaoId"]
    # only the LAST prevailing group is held: one with an FM after it ends at the FM, a TEMPO/PROB at its own end
    assert ends(morning_fog()["fcsts"], Z(21, 5)) == ["19 09:00", "21 05:00"]
    assert ends(passing_fog()["fcsts"], Z(21, 5)) == ["21 05:00", "19 10:00", "20 04:00"]
    # a last group with no usable end of its own gets the TAF's; one AWC ends after the TAF is left alone
    assert ends([{**group(Z(19, 5), Z(19, 9), 0.25, "FG"), "timeTo": None}], Z(20, 5)) == ["20 05:00"]
    assert ends(morning_fog()["fcsts"], Z(19, 12)) == ["19 09:00", "20 05:00"]
    # every line with no FM/BECMG after it is held: a garbled line listed last does not shield the real last change
    cut = [group(Z(19, 5), Z(19, 9), "6+"), group(Z(19, 9), Z(19, 12), 0.25, "FG", "FM"), group(Z(19, 5), Z(20, 5), "", None)]
    assert ends(cut, Z(20, 5)) == ["19 09:00", "20 05:00", "20 05:00"]


def test_group_with_no_usable_end_time():
    """Not seen from AWC (0 of 8,550 groups on 2026-11-15). Nothing raises;
    a group that cannot be shown to be over still counts, as it did before."""
    for bad in (None, "0900", [], 0):
        # a TEMPO with no end: kept
        rec = passing_fog()
        rec["fcsts"][1]["timeTo"] = bad
        s = bp.taf_summary(rec, LONDON, Z(19, 20))
        assert s["verdict"] == "possible dense fog" and "already under way at page build (Thursday 8 pm)," in s["sentence"], repr(bad)
        # the last prevailing line with no end: kept
        rec = taf(Z(19, 4, 40), Z(19, 5), Z(20, 5), [group(Z(19, 5), Z(20, 5), 0.25, "FG")])
        rec["fcsts"][0]["timeTo"] = bad
        assert bp.taf_summary(rec, LONDON, Z(19, 20))["verdict"] == "dense fog", repr(bad)
        del rec["fcsts"][0]["timeTo"]
        assert bp.taf_summary(rec, LONDON, Z(19, 20))["verdict"] == "dense fog"
        # a prevailing line with no end but an FM after it: the FM ends it
        rec = morning_fog()
        rec["fcsts"][0]["timeTo"] = bad
        assert bp.taf_summary(rec, LONDON, Z(19, 8))["verdict"] == "dense fog", repr(bad)
        assert bp.taf_summary(rec, LONDON, Z(19, 20))["verdict"] == "no fog", repr(bad)


def test_group_that_has_not_begun_is_never_over():
    """A nonsense end time on a group still ahead (AWC decoded HKJK's "TEMPO
    0813/0817" a week early, 2026-11-15) must not remove the group."""
    rec = passing_fog()
    rec["fcsts"][2]["timeTo"] = int(Z(12, 4).timestamp())   # PROB30 Friday 01:00, "ending" a week before
    s = bp.taf_summary(rec, LONDON, Z(19, 12))
    assert s["verdict"] == "possible dense fog" and s["sentence"].endswith("around Friday 1 am through Friday 5 am.")


# ------------------------------------------------------- A3: the issue stamp

def test_issue_stamp_carries_its_date():
    """An airport page shows no build time, and the page is read for a day
    after the bake: "issued 17:00Z" alone would read as today's."""
    s = bp.taf_summary(VABP, TZ["VABP"], SAVED)
    assert s["sentence"].startswith("The airport's own terminal forecast (TAF issued 17:00Z 15 Nov) ")
    assert undated(s["sentence"]) == BEFORE["VABP"]["sentence"], "nothing else in the sentence moved"
    html, data = bake("VABP", VABP, SAVED)
    assert "(TAF issued 17:00Z 15 Nov)" in html and "(TAF issued 17:00Z 15 Nov)" in data["forecast"]["wording"]
    assert "(TAF issued 17:00Z)" not in html
    assert bp.taf_summary(morning_fog(), LONDON, Z(19, 4, 50))["sentence"].startswith(
        "The airport's own terminal forecast (TAF issued 04:40Z 19 Nov) ")


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
