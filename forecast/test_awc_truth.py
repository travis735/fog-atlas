#!/usr/bin/env python3
"""Regression test for the truth-observation reader in forecast/engine.py.

Run: python3 forecast/test_awc_truth.py   (also collected by pytest)

Why it exists: awc_truth() read AWC's metars.cache.csv by fixed position
(visibility = 10th field, sky cover = 22nd/24th/26th/28th) although the file
has a header row. The observations it records are the truth the calibrated
forecast is scored against, so an upstream column change would not have broken
anything visibly: it would have filled the verification record with wrong
answers. The reader now finds columns by name and records nothing from a file
it cannot read with certainty.

HEADER and ROWS are real lines from the file as served 2026-11-24T22:24Z. The
functions are lifted out of engine.py by name (as test_sanitize.py does), so
this needs neither duckdb nor the network.
"""
import ast
import contextlib
import csv
import gzip
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
SRC = (HERE / "engine.py").read_text()
NS = {"csv": csv, "gzip": gzip, "fetch": lambda url, timeout=0: None}
WANT_FUNCS = ("_awc_refuse", "parse_awc_truth", "_csv_fields", "awc_truth")
WANT_NAMES = ("AWC", "AWC_TRUTH_COLS", "AWC_TRUTH_MIN_SHARE")
for node in ast.parse(SRC).body:
    is_const = isinstance(node, ast.Assign) and any(getattr(t, "id", "") in WANT_NAMES for t in node.targets)
    is_func = isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS
    if is_const or is_func:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "engine.py", "exec"), NS)
parse_awc_truth, awc_truth, AWC_TRUTH_COLS = NS["parse_awc_truth"], NS["awc_truth"], NS["AWC_TRUTH_COLS"]

HEADER = 'raw_text,station_id,observation_time,latitude,longitude,temp_c,dewpoint_c,wind_dir_degrees,wind_speed_kt,wind_gust_kt,visibility_statute_mi,altim_in_hg,sea_level_pressure_mb,corrected,auto,auto_station,maintenance_indicator_on,no_signal,lightning_sensor_off,freezing_rain_sensor_off,present_weather_sensor_off,wx_string,sky_cover,cloud_base_ft_agl,sky_cover,cloud_base_ft_agl,sky_cover,cloud_base_ft_agl,sky_cover,cloud_base_ft_agl,flight_category,three_hr_pressure_tendency_mb,maxT_c,minT_c,maxT24hr_c,minT24hr_c,precip_in,pcp3hr_in,pcp6hr_in,pcp24hr_in,snow_in,vert_vis_ft,metar_type,elevation_m'
ROWS = [
    '"METAR KSFO 242156Z 07004KT 10SM SCT033 BKN200 14/10 A3009 RMK AO2 SLP188 T01440100",KSFO,2026-11-24T21:56:00.000Z,37.6196,-122.3656,14.4,10,70,4,,10+,30.09,1018.8,,,TRUE,,,,,,,SCT,3300,BKN,20000,,,,,VFR,,,,,,,,,,,,METAR,2',
    '"SPECI KSMP 242217Z AUTO 1/4SM FZFG VV001 M01/M01 A2995 RMK AO2 T10111011 ",KSMP,2026-11-24T22:17:00.000Z,47.2764,-121.3383,-1.1,-1.1,,,,0.25,29.95,,,TRUE,TRUE,,,,,,FZFG,OVX,100,,,,,,,LIFR,,,,,,,,,,,1,SPECI,1206',
    '"METAR MMIO 242140Z 36006KT 1/2SM -DZ FG VV001 08/08 A3025 RMK 8//// VSBY LWR S",MMIO,2026-11-24T21:40:00.000Z,25.5440,-100.9280,8,8,360,6,,0.5,30.25,,,,,,,,,,-DZ FG,OVX,100,,,,,,,LIFR,,,,,,,,,,,1,METAR,1430',
    '"METAR ETHC 242120Z 06003KT 0100 FZFG VV001 M04/M04 Q1025 RED",ETHC,2026-11-24T21:20:00.000Z,52.5910,10.0220,-4,-4,60,3,,0.06,30.27,,,,,,,,,,FZFG,OVX,100,,,,,,,LIFR,,,,,,,,,,,1,METAR,35',
    '"METAR RPVK 242200Z VRB01KT 9999 FEW013TCU SCT016 BKN070 25/24 Q1011 RMK A2985 TCU S,SW,W,NW",RPVK,2026-11-24T22:00:00.000Z,11.6830,122.3830,25,24,0,1,,6+,29.85,,,,,,,,,,,FEW,1300,SCT,1600,BKN,7000,,,VFR,,,,,,,,,,,,METAR,6',
    '"METAR PAKU 242155Z 06022KT 1SM R06/P6000FT BLSN BR OVC008 M07/M08 A2978",PAKU,2026-11-24T21:55:00.000Z,70.3310,-149.6130,-7,-8,60,22,,1,29.78,,,,,,,,,,BLSN BR,OVC,800,,,,,,,IFR,,,,,,,,,,,,METAR,23',
    '"METAR KSMF 242153Z 00000KT 2SM HZ FEW012 OVC250 11/08 A3014 RMK AO2 SFC VIS 3 SLP205 T01110078",KSMF,2026-11-24T21:53:00.000Z,38.7007,-121.5948,11.1,7.8,0,0,,2,30.14,1020.5,,,TRUE,,,,,,HZ,FEW,1200,OVC,25000,,,,,IFR,,,,,,,,,,,,METAR,7',
    '"METAR K40B 242220Z AUTO VRB03G10KT M04/M12 RMK AO1 SLPNO T10391123",K40B,2026-11-24T22:20:00.000Z,46.6160,-69.5330,-3.9,-12.3,0,3,10,,,,,TRUE,TRUE,,,,,,,,,,,,,,,null,,,,,,,,,,,,METAR,344',
]
STATIONS = {"KSFO", "KSMP", "MMIO", "ETHC", "RPVK", "PAKU", "K40B"}   # KSMF is in the file but not a forecast station
TEXT = "\n".join([HEADER] + ROWS) + "\n"
VIS_AT = HEADER.split(",").index("visibility_statute_mi")

EXPECT = {
    "KSFO": {"obs": "2026-11-24T21:56:00.000Z", "v10": 0, "v05": 0, "v025": 0, "sub": 0},   # 10SM
    "KSMP": {"obs": "2026-11-24T22:17:00.000Z", "v10": 1, "v05": 1, "v025": 0, "sub": 1},   # 1/4SM VV001
    "MMIO": {"obs": "2026-11-24T21:40:00.000Z", "v10": 1, "v05": 0, "v025": 0, "sub": 1},   # 1/2SM VV001: sub by ceiling alone
    "ETHC": {"obs": "2026-11-24T21:20:00.000Z", "v10": 1, "v05": 1, "v025": 1, "sub": 1},   # 0100 m
    "RPVK": {"obs": "2026-11-24T22:00:00.000Z", "v10": 0, "v05": 0, "v025": 0, "sub": 0},   # 9999, commas in the raw text
    "PAKU": {"obs": "2026-11-24T21:55:00.000Z", "v10": 0, "v05": 0, "v025": 0, "sub": 0},   # exactly 1SM is not < 1
}                                                                                             # K40B reports no visibility: not recorded


def _old_positional(text, stations):
    """awc_truth's parser as it stood at fb899ce5, verbatim: fixed positions."""
    out = {}
    for line in text.split("\n"):
        if not line.startswith('"'):
            continue
        close = line.find('",')
        if close < 0:
            continue
        cols = line[close + 2:].split(",")
        icao = cols[0]
        if icao not in stations or icao in out:
            continue
        try:
            vis = float(cols[9]) if cols[9] and "+" not in cols[9] else (10.0 if cols[9] else None)
        except ValueError:
            vis = None
        ceil = None
        for k in (21, 23, 25, 27):
            if k < len(cols) and cols[k] in ("BKN", "OVC", "OVX"):
                try:
                    b = float(cols[k + 1])
                    ceil = b if ceil is None else min(ceil, b)
                except ValueError:
                    pass
        if vis is None:
            continue
        out[icao] = {"obs": cols[1],
                     "v10": int(vis < 1.0), "v05": int(vis < 0.5), "v025": int(vis < 0.25),
                     "sub": int(vis < 0.5 or (ceil is not None and ceil < 200))}
    return out


def _insert(line, value):
    """One more field just ahead of visibility, raw text left quoted."""
    f = next(csv.reader([line]))
    f = f[:VIS_AT] + [value] + f[VIS_AT:]
    return ",".join(([f'"{f[0]}"'] if line.startswith('"') else [f[0]]) + f[1:])


def _run(text, stations=STATIONS):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = parse_awc_truth(text, stations)
    return out, buf.getvalue()


def test_real_rows_read_by_name():
    out, said = _run(TEXT)
    assert out == EXPECT, out
    assert said == "", said


def test_same_answers_as_the_positional_reader_on_todays_layout():
    assert _run(TEXT)[0] == _old_positional(TEXT, STATIONS)
    # a byte-order mark or CRLF line ends change nothing
    assert _run(chr(0xFEFF) + TEXT)[0] == EXPECT
    assert _run(TEXT.replace("\n", "\r\n"))[0] == EXPECT


def test_field_inserted_in_the_rows_records_nothing_instead_of_wrong_truth():
    shifted = "\n".join([HEADER] + [_insert(r, "7") for r in ROWS]) + "\n"
    out, said = _run(shifted)
    assert out == {}, out
    assert said.startswith("::error ") and "rows have 45 fields but its header names 44" in said, said
    # what the positional reader made of the same file: every fog report logged as clear
    wrong = _old_positional(shifted, STATIONS)
    assert wrong["KSMP"]["sub"] == 0 and wrong["ETHC"]["v10"] == 0 and len(wrong) == 7, wrong


def test_named_column_inserted_in_header_and_rows_reads_the_same_truth():
    both = "\n".join([_insert(HEADER, "wind_chill_c")] + [_insert(r, "7") for r in ROWS]) + "\n"
    out, said = _run(both)
    assert out == EXPECT, out
    assert said == "", said
    assert _old_positional(both, STATIONS)["KSMP"]["sub"] == 0      # positional: wrong again


def test_missing_or_renamed_column_records_nothing_and_names_it():
    for col in AWC_TRUTH_COLS:
        renamed = TEXT.replace(HEADER, ",".join(h + "_x" if h == col else h for h in HEADER.split(",")))
        out, said = _run(renamed)
        assert out == {}, (col, out)
        assert said.startswith("::error ") and f"no column named {col}" in said, said
        assert said.count("\n") == 1, "one annotation, one line"
    twice, said = _run(TEXT.replace(HEADER, HEADER.replace("altim_in_hg", "visibility_statute_mi")))
    assert twice == {} and "more than one column named visibility_statute_mi" in said, said
    unpaired, said = _run(TEXT.replace(HEADER, HEADER.replace("sky_cover,cloud_base_ft_agl,flight_category", "sky_cover,cloud_top_ft,flight_category")))
    assert unpaired == {} and "no longer pair up" in said, said


def test_not_a_table_records_nothing():
    for body in ("", "upstream unavailable", "<!DOCTYPE html>\n<html><body>503</body></html>\n", "\n".join(ROWS)):
        out, said = _run(body)
        assert out == {}, (body[:20], out)
        assert said.startswith("::error "), (body[:20], said)


def test_a_bad_download_does_not_raise():
    # gzip.decompress used to raise here, after current.json was written and
    # before the workflow's upload step: the forecast for that hour was lost
    for raw in (b"<html>503 Service Unavailable</html>", gzip.compress(TEXT.encode())[:40]):
        NS["fetch"] = lambda url, timeout=0, raw=raw: raw
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert awc_truth(STATIONS) == {}
        assert buf.getvalue().startswith("::error "), buf.getvalue()
    NS["fetch"] = lambda url, timeout=0: gzip.compress(TEXT.encode("latin-1"))
    assert awc_truth(STATIONS) == EXPECT


def test_no_download_an_empty_one_and_a_header_with_no_rows_all_say_so():
    """Three ways to record nothing that used to be silent on a green run
    (review, 2026-11-25): fetch() returning None, zero bytes, and a file that
    is only its header row. Each now leaves an ::error annotation."""
    for label, raw in (("unreachable", None), ("zero bytes", b""),
                       ("header only", gzip.compress((HEADER + "\n").encode("latin-1"))),
                       ("blank lines only", gzip.compress(b"\n\n  \n"))):
        NS["fetch"] = lambda url, timeout=0, raw=raw: raw
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert awc_truth(STATIONS) == {}, label
        assert buf.getvalue().startswith("::error title=AWC observations not recorded::"), (label, buf.getvalue())
        assert buf.getvalue().count("\n") == 1, (label, "one annotation, not one per line")


def test_a_file_that_gives_about_nothing_is_refused_however_well_formed():
    """The freshness and the header were checked; nothing checked how much came
    out. Six doctored copies of the live file recorded 0 to 326 of the usual
    1,112 observations on a green run with no annotation (review, 2026-11-25).
    Under half the forecast stations is now a refusal, with the count."""
    def edit(fn):
        return "\n".join([HEADER] + [fn(r) for r in ROWS]) + "\n"

    def with_vis(value):
        def one(row):
            f = next(csv.reader([row]))
            f[VIS_AT] = value(f[VIS_AT])
            return ",".join([f'"{f[0]}"'] + f[1:])
        return one
    cases = {
        "every visibility empty": edit(with_vis(lambda v: "")),
        "visibility written as text": edit(with_vis(lambda v: "M1/4" if v else v)),
        "station ids in lower case": edit(lambda r: r.replace(",KSFO,", ",ksfo,").replace(",KSMP,", ",ksmp,").replace(",MMIO,", ",mmio,")
                                          .replace(",ETHC,", ",ethc,").replace(",RPVK,", ",rpvk,").replace(",PAKU,", ",paku,")),
        "cut after two rows": "\n".join([HEADER] + ROWS[:2]) + "\n",
    }
    for label, text in cases.items():
        out, said = _run(text)
        assert out == {}, (label, out)
        assert said.startswith("::error title=AWC observations not recorded::metars.cache.csv gave an observation for only "), (label, said)
        assert "of 7 forecast stations" in said and said.count("\n") == 1, (label, said)
    # half is the floor, not most: 4 of 7 stations is recorded as it always was
    out, said = _run("\n".join([HEADER] + ROWS[:4]) + "\n")
    assert sorted(out) == ["ETHC", "KSFO", "KSMP", "MMIO"] and said == "", (out, said)
    assert NS["AWC_TRUTH_MIN_SHARE"] == 0.5


def test_leading_blank_lines_do_not_cost_the_file():
    """feed.ts and the watchdog skip blank lines ahead of the header; the engine refused the whole file."""
    out, said = _run("\n  \n" + TEXT)
    assert out == EXPECT and said == "", (out, said)


def test_whole_real_file_matches_the_positional_reader():
    """The full 2026-11-24T22:24Z file (the app test's fixture) against the real station list."""
    gz = HERE.parent / "app" / "test" / "fixtures" / "metars.cache-2026-11-24T2224Z.csv.gz"
    if not gz.exists():
        return
    text = gzip.decompress(gz.read_bytes()).decode("latin-1", "replace")
    stations = set(json.load(open(HERE / "stations.json")))
    new, said = _run(text, stations)
    assert said == "", said
    assert new == _old_positional(text, stations)
    assert len(new) > 900, len(new)


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
