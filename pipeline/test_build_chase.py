#!/usr/bin/env python3
"""Regression test for pipeline/build_chase.py: FAA fixed-width layouts move.

Run: python3 pipeline/test_build_chase.py   (also collected by pytest)

The 2026-10-29 NASR cycle widened the pavement-classification field in
APT.txt from 11 to 16 characters, shifting every runway-end field 5 columns.
build_chase.py read the new file at the old columns, and the 2026-11-03 CI
refresh committed and deployed runway ends named "HIG" with ALS "TMRN" for
every US airport. Fixtures: the real layout lines and the real KSFO 01L/19R
runway record from the 2026-08-06 and 2026-10-29 cycles.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_chase as bc  # noqa: E402

RF_AUG = ['L AN 0011 00004  DLID    LANDING FACILITY SITE NUMBER', 'L AN 0007 01211  NONE    ICAO IDENTIFIER', 'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER', 'L AN 0011 00004  DLID    LANDING FACILITY SITE NUMBER', 'L AN 0007 00017  A30     RUNWAY IDENTIFICATION', 'R AN 0005 00024  A31     PHYSICAL RUNWAY LENGTH (NEAREST FOOT)', 'L AN 0011 00050  A39     PAVEMENT CLASSIFICATION NUMBER (PCN)', 'L AN 0003 00066  A30A    BASE END IDENTIFIER', 'L AN 0003 00234  A47     RUNWAY VISUAL RANGE EQUIPMENT (RVR)', 'L AN 0008 00238  A49     APPROACH LIGHT SYSTEM', 'L AN 0003 00288  A30A    RECIPROCAL END IDENTIFIER', 'L AN 0003 00456  A47     RUNWAY VISUAL RANGE EQUIPMENT (RVR)', 'L AN 0008 00460  A49     APPROACH LIGHT SYSTEM', 'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER', 'L AN 0007 00017  A30     RUNWAY IDENTIFICATION', 'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER']

RF_OCT = ['L AN 0011 00004  DLID    LANDING FACILITY SITE NUMBER', 'L AN 0007 01211  NONE    ICAO IDENTIFIER', 'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER', 'L AN 0011 00004  DLID    LANDING FACILITY SITE NUMBER', 'L AN 0007 00017  A30     RUNWAY IDENTIFICATION', 'R AN 0005 00024  A31     PHYSICAL RUNWAY LENGTH (NEAREST FOOT)', 'L AN 0016 00050  A39     PAVEMENT CLASSIFICATION', 'L AN 0003 00071  A30A    BASE END IDENTIFIER', 'L AN 0003 00239  A47     RUNWAY VISUAL RANGE EQUIPMENT (RVR)', 'L AN 0008 00243  A49     APPROACH LIGHT SYSTEM', 'L AN 0003 00293  A30A    RECIPROCAL END IDENTIFIER', 'L AN 0003 00461  A47     RUNWAY VISUAL RANGE EQUIPMENT (RVR)', 'L AN 0008 00465  A49     APPROACH LIGHT SYSTEM', 'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER', 'L AN 0007 00017  A30     RUNWAY IDENTIFICATION', 'L AN 0011 00004  N/A     LANDING FACILITY SITE NUMBER']

RWY_AUG = 'RWY02187.*A   CA01L/19R 7650 200ASPH-G      GRVD            HIGH 01L028          NNPI  G37-36-28.4323N 135388.4323N122-22-58.5426W440578.5426W   10.7       37-36-34.0304N 135394.0304N122-22-54.8330W440574.8330W   10.4 640   10.9     TMRN        YYN               B(V) 50                 19R208          NNPI  G37-37-35.3329N 135455.3329N122-22-14.1939W440534.1939W    9.2 583.15                                                                    11.2P4L  TMRN        NYN               C    50                 3RD PARTY SURVEY10/22/2014  75.0 200.0 400.0 877.0         3RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/2014 7650 7650 7650 7010                                                                                                                                             3RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/2014                                                    3RD PARTY SURVEY10/22/2014 7650 7650 7650 7650                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          '

RWY_OCT = 'RWY02187.*A   CA01L/19R 7650 200ASPH-G      GRVD PCR/956 /F/C/W/THIGH 01L028          NNPI  G37-36-28.4323N 135388.4323N122-22-58.5426W440578.5426W   10.7       37-36-34.0304N 135394.0304N122-22-54.8330W440574.8330W   10.4 640   10.9     TMRN        YYN               B(V) 50                 19R208          NNPI  G37-37-35.3329N 135455.3329N122-22-14.1939W440534.1939W    9.2 583.15                                                                    11.2P4L  TMRN        NYN               C    50                 3RD PARTY SURVEY10/22/2014  75.0 200.0 400.0 877.0         3RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/2014 7650 7650 7650 7010                                                                                                                                             3RD PARTY SURVEY10/22/20143RD PARTY SURVEY10/22/2014                                                    3RD PARTY SURVEY10/22/2014 7650 7650 7650 7650                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     '

APT_OCT = 'APT02187.*A                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               KSFO                                                                                                                                                                                                                                                                                                                             '


def _tmp(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="latin-1")
    f.write(text); f.close()
    return Path(f.name)


def _layout(lines):
    p = _tmp("\n".join(lines) + "\n")
    try:
        return bc.apt_layout(p)
    finally:
        p.unlink()


def _ends(rwy_line, lay, apt_line=APT_OCT):
    p = _tmp(apt_line + "\n" + rwy_line + "\n")
    try:
        site_icao, rwys = bc.parse_apt(p, lay)
    finally:
        p.unlink()
    assert site_icao == {"02187.*A": "KSFO"}, site_icao
    return rwys["02187.*A"]


def test_layout_is_read_from_the_faa_file():
    aug, octl = _layout(RF_AUG), _layout(RF_OCT)
    assert aug["base"] == (65, 68) and aug["recip"] == (287, 290) and aug["base_als"] == (237, 245)
    assert octl == bc.APT_DEFAULT, octl                    # the built-in fallback IS the 2026-10 layout
    assert all(octl[k][0] - aug[k][0] == 5 for k in ("base", "base_rvr", "base_als", "recip", "recip_rvr", "recip_als"))
    assert aug["icao"] == octl["icao"] == (1210, 1217) and aug["len"] == octl["len"] == (23, 28)


def test_missing_or_unreadable_layout_falls_back():
    assert bc.apt_layout(Path("/nonexistent/apt_rf.txt")) == bc.APT_DEFAULT
    assert _layout(["nothing useful here"]) == bc.APT_DEFAULT
    assert _layout([l for l in RF_OCT if "RECIPROCAL" not in l]) == bc.APT_DEFAULT


def test_each_cycle_parses_with_its_own_layout():
    for line, rf in ((RWY_AUG, RF_AUG), (RWY_OCT, RF_OCT)):
        ends = _ends(line, _layout(rf))
        assert [(e["e"], e["len"], e["als"], e["rvr"]) for e in ends] == [("01L", 7650, None, "TMR"), ("19R", 7650, None, "TMR")], ends


def test_old_columns_on_the_new_file_is_the_november_failure():
    ends = _ends(RWY_OCT, _layout(RF_AUG))
    assert ends[0]["e"] == "HIG" and ends[0]["als"] == "TMRN", ends   # what was live 2026-11-03..24


def _end(e, tier=400, ils=None):
    return {"e": e, "len": 7000, "als": None, "rvr": None, "ils": ils, "lpv": 0, "tier": tier}


def test_sanity_gate():
    good = {f"K{i:03d}": [_end("09", 200, "I"), _end("27", 250), _end("18"), _end("36L", 50, "IIIB")] for i in range(600)}
    assert bc.sanity_problems(good) == []
    bad = {f"K{i:03d}": [_end("HIG"), _end("0R"), _end("HIG"), _end("00R")] for i in range(600)}
    assert len(bc.sanity_problems(bad)) == 3, bc.sanity_problems(bad)
    assert bc.sanity_problems({"KAAA": [_end("09")]}), "a near-empty build must not pass"
    # curated Canadian ends are not FAA-derived and must not mask a bad FAA parse
    mixed = dict(bad, **{f"C{i:03d}": [dict(_end("09", 200, "I"), cur=1)] * 4 for i in range(2000)})
    assert bc.sanity_problems(mixed)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", name, "-", repr(e)[:300])
    print("FAILED" if fails else "passed")
    sys.exit(1 if fails else 0)
