#!/usr/bin/env python3
"""Regression test for forecast/parse_nbm.py against real NBM text blocks.

Run: python3 forecast/test_parse_nbm.py   (also collected by pytest)

Why it exists: on 2026-11-03 NOAA dropped the version token from the station
header (" KSFO    NBM V5.0 NBH GUIDANCE" -> " KSFO   NBM  NBH GUIDANCE"). The
parser's header regex required the token, matched 0 stations, and the engine
published an empty forecast on every run for 21 days. The blocks below are
KSFO from the 2026-11-24 15Z NBH and 13Z NBS bulletins, trimmed to the rows
the parser reads; the "old" variants re-insert the version token.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from parse_nbm import parse_collective  # noqa: E402

NBH_NEW = [' KSFO   NBM  NBH GUIDANCE    11/24/2026  1500 UTC', ' UTC  16 17 18 19 20 21 22 23 00 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 ', ' TMP  50 53 56 58 60 61 62 62 61 61 60 59 59 58 57 57 57 56 56 56 55 55 54 54 55', ' DPT  47 49 50 51 50 51 51 51 50 51 52 52 53 54 54 54 54 54 53 53 53 53 53 53 54', ' WSP  10 10 11 14 16 17 18 17 17 17 18 19 20 20 20 18 15 12 10  8  7  7  6  6  6', ' CIG 130130130130130130130180160130 60 60 45 45 35 35 35 35 35 35 35 35 35 35 35', ' IFC   0  0  0  0  0  0  0  0  0  0  0  0  0  0  1  0  0  0  0  0  2  5  7 10 11', ' LIC   0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  1  3  3', ' VIS 100100100100100100100100100100100100100100100100100100100100100100100100100', ' IFV   0  0  0  0  0  0  0  0  0  0  1  2  1  4  2  0  2  3  2  2  1  3  4  5  5', ' LIV   0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  1  2  3']

NBS_NEW = [' KSFO    NBM  NBS GUIDANCE    11/24/2026  1300 UTC', ' DT /NOV  24/NOV  25                /NOV  26                /NOV  27      ', ' UTC  18 21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21 00 03 06 09 12 ', ' FHR  05 08 11 14 17 20 23 26 29 32 35 38 41 44 47 50 53 56 59 62 65 68 71 ', ' TMP  56 61 61 59 57 56 55 54 59 63 63 58 55 52 52 51 55 60 62 58 55 53 52', ' DPT  50 51 50 52 54 54 53 53 56 56 55 54 53 52 51 51 54 54 53 53 52 51 50', ' WSP  11 17 17 19 20 12  7  6  6  3  4  0  2  4  3  3  4  4  5  2  0  3  4', ' CIG 130130170 45 35 35 35 27 27 45-88-88-88-88-88-88 19-88-88-88-88-88-88', ' IFC   0  0  0  0  1  0  3 12 10  2  1  0  2  9 15 19 18  2  0  0  0  5  9', ' VIS 100100100100100100100100100100100100100100100100100100100100100100 60', ' IFV   0  0  0  0  1  0  1  5  4  0  0  1  1  4  9 11  5  1  1  4  6  8 14']


def _parse(lines, stations=None):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="latin-1") as f:
        f.write("\n".join(lines) + "\n")
        p = Path(f.name)
    try:
        return parse_collective(p, stations)
    finally:
        p.unlink()


def _old(lines):
    """The pre-2026-11-03 header: version token present."""
    return [lines[0].replace(" NBM  ", " NBM V5.0 ", 1)] + lines[1:]


def test_new_header_nbh():
    rows = _parse(NBH_NEW, {"KSFO"})
    assert len(rows) == 25, len(rows)
    assert {r["icao"] for r in rows} == {"KSFO"} and {r["product"] for r in rows} == {"NBH"}
    assert rows[0]["cycle"] == "2026-11-24T15:00:00"
    assert [r["fhr"] for r in rows] == list(range(1, 26))      # NBH leads derive from the UTC row
    assert rows[0]["vis_sm"] == 10.0 and rows[0]["ceil_ft"] == 13000.0
    assert rows[0]["tmp"] == 50.0 and rows[0]["dpt"] == 47.0 and rows[0]["wsp"] == 10.0
    assert rows[-1]["liv"] == 3.0 and rows[-1]["ifv"] == 5.0


def test_new_header_nbs():
    rows = _parse(NBS_NEW, {"KSFO"})
    assert len(rows) == 23, len(rows)
    assert {r["product"] for r in rows} == {"NBS"}
    assert rows[0]["fhr"] == 5 and rows[-1]["fhr"] == 71
    assert rows[-1]["vis_sm"] == 6.0                             # "60" tenths of a mile
    assert rows[10]["ceil_ft"] is None                           # -88 = unlimited
    assert rows[0]["ceil_ft"] == 13000.0


def test_old_header_still_parses_identically():
    for new in (NBH_NEW, NBS_NEW):
        assert new[0] != _old(new)[0], "fixture header did not change"
        assert _parse(_old(new), {"KSFO"}) == _parse(new, {"KSFO"})


def test_station_filter_and_unknown_text():
    assert _parse(NBH_NEW, {"KOAK"}) == []                       # filtered out, not mis-assigned
    assert _parse(NBH_NEW, None)[0]["icao"] == "KSFO"            # no filter = every station
    junk = [" KSFO   SOMETHING ELSE GUIDANCE    11/24/2026  1500 UTC"] + NBH_NEW[1:]
    assert _parse(junk, {"KSFO"}) == []                          # an unrecognised header yields nothing


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
