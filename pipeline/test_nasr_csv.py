#!/usr/bin/env python3
"""build_chase.py on the FAA's CSV tables: columns by name, or no build.

Run: python3 pipeline/test_nasr_csv.py   (also collected by pytest)

The FAA ends the fixed-width NASR .txt files on 2027-01-21. Reading them by
column offset is what put runway ends named "HIG" on the CHASE board for three
weeks in 2026-11, so the builder now reads the CSV tables that ship in the
same zip, by header name, and keeps the fixed-width reader only as a fallback.

Fixture (test_nasr_fixture.py): the real 2026-10-29 rows of KTMB, KPOB, KILN
and heliport 7NY9 in both formats.
"""
import contextlib
import csv
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_chase as bc  # noqa: E402
import test_nasr_fixture as fx  # noqa: E402

TMB, POB, ILN, HELI = "03325.1*A", "16723.03*A", "18717.*A", "16476.*H"


@contextlib.contextmanager
def fixture(**kw):
    with tempfile.TemporaryDirectory() as d:
        yield fx.write(Path(d), **kw)


def _quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


def _rewrite(path: Path, edit):
    """Rewrite one CSV table through edit(header, rows) -> (header, rows)."""
    with open(path, encoding="latin-1", newline="") as f:
        rows = list(csv.reader(f))
    head, body = edit(rows[0], rows[1:])
    with open(path, "w", encoding="latin-1", newline="") as f:
        csv.writer(f, lineterminator="\n").writerows([head] + body)


def test_csv_reader_reads_the_real_rows():
    with fixture(txt=False) as d:
        n = bc.read_nasr_csv(d / "CSV")
    assert (n["reader"], n["cycle"]) == ("csv", "2026-10-29")
    assert n["site_icao"] == {TMB: "KTMB", POB: "KPOB", ILN: "KILN"}, n["site_icao"]      # the heliport has no ICAO id
    # KTMB: three runways, plus helipad H1 (75 ft) which the 2,000 ft floor drops
    assert [(e["e"], e["len"], e["als"], e["rvr"]) for e in n["rwys"][TMB]] == [
        ("09L", 5003, None, None), ("27R", 5003, None, None), ("09R", 6000, "MALSR", None),
        ("27L", 6000, None, None), ("13", 4001, None, None), ("31", 4001, None, None)], n["rwys"][TMB]
    assert HELI not in n["rwys"] and n["ends"][HELI] == {"H1": None}
    assert n["ends"][TMB]["H1"] is None and "H1" not in [e["e"] for e in n["rwys"][TMB]]
    assert n["ends"][POB] == {"05": None, "23": "ALSF1", "051": None, "231": None}
    assert n["ils"] == {(TMB, "09R"): "I", (POB, "23"): "I", (ILN, "04L"): "I", (ILN, "22R"): "III"}, n["ils"]
    f = n["fac"][TMB]
    assert (f["lid"], f["icao"], f["name"], f["st"]) == ("TMB", "KTMB", "MIAMI EXEC", "FL")
    assert (round(f["lat"], 4), round(f["lon"], 4)) == (25.6476, -80.4332), (f["lat"], f["lon"])
    assert n["n_remarks"] == 4 and len(n["remarks"]) == 3, "three of the four remarks mention LED"


def test_csv_and_fixed_width_readers_agree():
    with fixture() as d:
        c = bc.read_nasr_csv(d / "CSV")
        t = _quiet(bc.read_nasr_txt, d)
    assert t["reader"] == "txt" and t["cycle"] == c["cycle"] == "2026-10-29"
    for key in ("site_icao", "rwys", "ils", "ends", "n_remarks"):
        assert c[key] == t[key], (key, c[key], t[key])
    assert sorted(c["remarks"]) == sorted(t["remarks"])
    for site in c["fac"]:
        a, b = c["fac"][site], t["fac"][site]
        assert {k: a[k] for k in ("lid", "icao", "name", "st")} == {k: b[k] for k in ("lid", "icao", "name", "st")}
        # 8-decimal degrees against arc seconds to 4 places: equal to a metre
        assert abs(a["lat"] - b["lat"]) < 1e-5 and abs(a["lon"] - b["lon"]) < 1e-5, (site, a, b)
    # and so does everything built on them
    fc, rc, sc = bc.led_census(c)
    ft, rt, st = bc.led_census(t)
    assert (fc, rc, sc) == (ft, rt, st)
    assert {s: f["ends"] for s, f in fc.items()} == {TMB: ["09R"], POB: ["23"]}, fc     # "RWY 9R" -> 09R; 05 dropped


def test_a_decommissioned_ils_is_no_ils_in_either_reader():
    # ILS.txt still lists KILN 04R and 22L (ILS/DME, CAT I and II); their ILS2
    # records say DECOMMISSIONED 03/18/2024 and ILS_BASE.csv has no row for them.
    # Until 2026-11 the fixed-width reader ignored ILS2 and chase.json showed
    # KILN 22L as a CAT II runway.
    with fixture() as d:
        ils_txt = bc.parse_ils(d / "ILS.txt")
        listed = {(l[4:15].strip(), l[15:18].strip()) for l in open(d / "ILS.txt") if l.startswith("ILS1")}
        ils_csv = bc.read_nasr_csv(d / "CSV")["ils"]
    assert {(ILN, "04R"), (ILN, "22L")} <= listed, "the fixture must carry the decommissioned records"
    assert (ILN, "04R") not in ils_txt and (ILN, "22L") not in ils_txt, ils_txt
    assert ils_txt == ils_csv == {(TMB, "09R"): "I", (POB, "23"): "I", (ILN, "04L"): "I", (ILN, "22R"): "III"}


def test_columns_are_read_by_name_not_position():
    with fixture(txt=False) as d:
        want = bc.read_nasr_csv(d / "CSV")
        for name in bc.CSV_NEEDS:      # reverse the column order and add one the FAA might add
            _rewrite(d / "CSV" / name, lambda h, rows: (["NEW_FAA_COLUMN"] + h[::-1], [["x"] + r[::-1] for r in rows]))
        assert bc.read_nasr_csv(d / "CSV") == want


def test_only_ils_and_ils_dme_are_credited_as_an_ils():
    """Pins today's cut (ILS_FULL_CODES), which a review (2026-11-25) found had
    no test: adding LOC/GS, or dropping the code filter, passed every test.
    A LOC/GS system (code LG) has a glide slope and a category in the FAA's
    files and is still NOT credited here: whether to is the owner's decision,
    and it must not change by accident. LOC/DME (LE) has no glide slope."""
    def add(code, end, cat):
        def edit(h, rows):
            row = list(rows[0])                                  # KTMB 09R, a real ILS row, as the template
            row[h.index("RWY_END_ID")], row[h.index("SYSTEM_TYPE_CODE")], row[h.index("CATEGORY")] = end, code, cat
            return h, rows + [row]
        return edit
    with fixture(txt=False) as d:
        base = bc.read_nasr_csv(d / "CSV")["ils"]
        _rewrite(d / "CSV" / "ILS_BASE.csv", add("LG", "27L", "I"))      # LOC/GS, category I, on KTMB 27L
        _rewrite(d / "CSV" / "ILS_BASE.csv", add("LE", "13", "I"))       # LOC/DME
        _rewrite(d / "CSV" / "ILS_BASE.csv", add("LD", "31", "II"))      # ILS/DME: this one counts
        got = bc.read_nasr_csv(d / "CSV")["ils"]
    assert got == {**base, (TMB, "31"): "II"}, got
    assert bc.ILS_FULL_CODES == ("LS", "LD")
    # the fixed-width reader makes the same cut on the system's name
    with fixture() as d:
        lines = (d / "ILS.txt").read_text(encoding="latin-1").splitlines()
        tmb1, tmb2 = lines[0], lines[1]
        assert tmb1.startswith("ILS1") and tmb1[18:28].strip() == "ILS" and tmb2.startswith("ILS2")

        def as_system(line, end, name):
            return line[:15] + end.ljust(3) + name.ljust(10) + line[28:]
        extra = [as_system(tmb1, "27L", "LOC/GS"), as_system(tmb2, "27L", "LOC/GS"),
                 as_system(tmb1, "31", "ILS/DME"), as_system(tmb2, "31", "ILS/DME")]
        (d / "ILS.txt").write_text("\n".join(lines + extra) + "\n", encoding="latin-1")
        txt = bc.parse_ils(d / "ILS.txt")
    assert (TMB, "27L") not in txt and txt[(TMB, "31")] == "I" and txt[(TMB, "09R")] == "I", txt


def test_a_decommissioned_localizer_does_not_take_a_live_ils_on_the_same_end():
    """The fixed-width reader dropped every system on a runway end once ANY
    system there was decommissioned. None is paired that way in 2026-10-29's
    1,627 systems; one retired LOCALIZER beside a live ILS would have turned a
    CAT I end into "no ILS"."""
    with fixture() as d:
        lines = (d / "ILS.txt").read_text(encoding="latin-1").splitlines()
        live1, live2 = lines[0], lines[1]                               # KTMB 09R ILS, operational
        dead1 = live1[:18] + "LOCALIZER ".ljust(10) + live1[28:]
        dead2 = live2[:18] + "LOCALIZER ".ljust(10) + "DECOMMISSIONED".ljust(22) + live2[50:]
        (d / "ILS.txt").write_text("\n".join(lines + [dead1, dead2]) + "\n", encoding="latin-1")
        got = bc.parse_ils(d / "ILS.txt")
    assert got[(TMB, "09R")] == "I", got
    assert (ILN, "22L") not in got and (ILN, "04R") not in got, "a decommissioned ILS is still no ILS"


def test_airports_keep_apt_txt_order_and_the_file_names_its_own_cycle():
    """chase.json lists airports in APT.txt's record order (state, then site
    number), so a new cycle's diff is the cycle's changes and nothing else; and
    meta.nasr_file, whose date the app prints as the data's age, is the zip of
    the cycle that was read, not the first zip in the folder (a dev machine
    holds several). Neither had a test."""
    def to_alaska(h, rows):                                              # KILN, the highest site number, into state AK
        for r in rows:
            if r[h.index("ARPT_ID")] == "ILN":
                r[h.index("STATE_CODE")] = "AK"
        return h, rows
    with fixture(txt=False) as d, _small_build_allowed():
        _rewrite(d / "CSV" / "APT_BASE.csv", to_alaska)
        for name in ("28DaySubscription_Effective_2026-08-06.zip", "28DaySubscription_Effective_2026-10-29.zip",
                     "CIFP_261029.zip"):
            (d / name).write_bytes(b"")
        said, out = _refusal(d, _baseline(d, 2, 2))
        assert said is None, said
        j = json.loads((out / "chase.json").read_text())
    # AK, FL, NC: not site-number order (the curated Canadian fields follow the FAA's)
    assert list(j["airports"])[:3] == ["KILN", "KTMB", "KPOB"], list(j["airports"])[:5]
    assert j["meta"]["nasr_file"] == "28DaySubscription_Effective_2026-10-29.zip", j["meta"]
    assert j["led"]["n_remarks"] == 4


def test_an_led_airport_outside_the_us_board_is_still_in_the_atlas_list():
    """TISX (St Croix) has the FAA sentence, is in the fog atlas and is not a
    CHASE airport (country VI). Its ring says "in the atlas, not on the
    board"; computed from the US airports alone it would say "not in the atlas"."""
    def as_stx(h, rows):
        for r in rows:
            if r[h.index("ARPT_ID")] == "TMB":
                r[h.index("ICAO_ID")] = "TISX"
        return h, rows
    with fixture(txt=False) as d, _small_build_allowed():
        _rewrite(d / "CSV" / "APT_BASE.csv", as_stx)
        said, out = _refusal(d, _baseline(d, 2, 2))
        assert said is None, said
        j = json.loads((out / "chase.json").read_text())
    stx = {a["id"]: a for a in j["led"]["airports"]}["TISX"]
    assert "TISX" not in j["airports"] and (stx["atlas"], stx["board"]) == (1, 0), stx
    assert [e["board"] for e in stx["ends"]] == [0]


def test_the_change_lines_reach_the_job_summary_and_the_baseline_is_git_head():
    """The monthly commit body and the run's summary page are the only places a
    person sees an LED end come or go (chase.json is one minified line). The
    comparison is against the COMMITTED file, so a second local build still
    reports against what is live. Neither had a test."""
    import os
    import subprocess
    with fixture(txt=False) as d, _small_build_allowed():
        summary = d / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        try:
            said, out = _refusal(d, _baseline(d, 2, 2))
        finally:
            del os.environ["GITHUB_STEP_SUMMARY"]
        assert said is None, said
        text = summary.read_text()
    assert text.startswith("### LED approach lights\n") and "- LED ALS: +KPOB RWY 23\n" in text and "- LED ALS: -KMEM RWY 18L\n" in text
    # committed_chase: git HEAD wins over the file on disk; the file on disk only when git has nothing
    real = subprocess.run
    with tempfile.TemporaryDirectory() as t:
        on_disk = Path(t) / "chase.json"
        on_disk.write_text(json.dumps({"from": "disk"}))
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"from": "HEAD"}).encode())
        subprocess.run = fake
        try:
            got = bc.committed_chase(bc.APP_DATA / "chase.json")
        finally:
            subprocess.run = real
        assert got == ({"from": "HEAD"}, "git HEAD") and calls and calls[0][-1] == "HEAD:app/public/data/chase.json", (got, calls)

        def none(cmd, **kw):
            raise subprocess.CalledProcessError(128, cmd)
        subprocess.run = none
        try:
            assert bc.committed_chase(on_disk) == ({"from": "disk"}, "file on disk")
            assert bc.committed_chase(Path(t) / "absent.json") == (None, "none")
        finally:
            subprocess.run = real


def _refusal(d, argv=()):
    """Run the whole build on the fixture; return (exit message, output dir)."""
    out = d / "out"
    out.mkdir(exist_ok=True)
    (out / "chase.json").write_text("LAST GOOD")
    (out / "led_changes.txt").write_text("LED ALS: lines of an earlier run\n")
    (d / "FAACIFP18").write_text("")          # no LPV lines: not what is under test
    try:
        _quiet(bc.main, ["--nasr", str(d), "--out", str(out), *argv])
    except SystemExit as e:
        return str(e.code), out
    return None, out


def test_a_missing_column_stops_the_build_and_is_named():
    with fixture(txt=False) as d:
        def drop(h, rows):
            i = h.index("APCH_LGT_SYSTEM_CODE")
            return h[:i] + ["APCH_LIGHT_SYSTEM"] + h[i + 1:], rows       # the FAA renames a column
        _rewrite(d / "CSV" / "APT_RWY_END.csv", drop)
        try:
            bc.read_nasr_csv(d / "CSV")
            raise AssertionError("a renamed column was read as if nothing had happened")
        except bc.SourceError as e:
            msg = str(e)
        assert "APT_RWY_END.csv has no column APCH_LGT_SYSTEM_CODE" in msg, msg
        assert "APCH_LIGHT_SYSTEM" in msg, "the header it did find is quoted"
        # the FAA's own column list in the fixture still names the old column
        assert "the FAA's column list for this cycle still names it" in msg, msg
        said, out = _refusal(d)
        assert said and "APT_RWY_END.csv has no column APCH_LGT_SYSTEM_CODE" in said and "NOT written" in said, said
        assert (out / "chase.json").read_text() == "LAST GOOD"
        # the commit step reads led_changes.txt: an earlier run's lines must not survive a refused build
        assert not (out / "led_changes.txt").exists()


def test_two_missing_columns_are_both_named():
    with fixture(txt=False) as d:
        _rewrite(d / "CSV" / "ILS_BASE.csv", lambda h, rows: ([c for c in h if c not in ("CATEGORY", "SYSTEM_TYPE_CODE")],
                                                              [r[:5] for r in rows]))
        try:
            bc.read_nasr_csv(d / "CSV")
            raise AssertionError("no error")
        except bc.SourceError as e:
            assert "ILS_BASE.csv has no column SYSTEM_TYPE_CODE, CATEGORY" in str(e), str(e)


def test_tables_from_two_cycles_are_refused():
    with fixture(txt=False) as d:
        _rewrite(d / "CSV" / "ILS_BASE.csv", lambda h, rows: (h, [["2026/11/26"] + r[1:] for r in rows]))
        try:
            bc.read_nasr_csv(d / "CSV")
            raise AssertionError("two cycles were joined")
        except bc.SourceError as e:
            assert "do not share one EFF_DATE" in str(e) and "ILS_BASE.csv ['2026/11/26']" in str(e), str(e)


def test_an_empty_or_absent_table_is_refused():
    with fixture(txt=False) as d:
        _rewrite(d / "CSV" / "APT_RMK.csv", lambda h, rows: (h, []))
        try:
            bc.read_nasr_csv(d / "CSV")
            raise AssertionError("an empty remark table read as 'no remarks'")
        except bc.SourceError as e:
            assert "APT_RMK.csv has a header and no rows" in str(e), str(e)
    with fixture() as d:        # one table gone, APT.txt present: NOT a reason to fall back quietly
        (d / "CSV" / "ILS_BASE.csv").unlink()
        said, out = _refusal(d)
        assert said and "holds only" in said and "ILS_BASE.csv" in said, said
        assert (out / "chase.json").read_text() == "LAST GOOD"


@contextlib.contextmanager
def _small_build_allowed():
    """The fixture is four facilities; the volume gates exist to refuse
    exactly that. Lift them to test what a passing build writes."""
    keep = (bc.sanity_problems, bc.LED_MIN_REMARKS, bc.LED_MIN_AIRPORTS)
    bc.sanity_problems, bc.LED_MIN_REMARKS, bc.LED_MIN_AIRPORTS = (lambda airports: []), 1, 1
    try:
        yield
    finally:
        bc.sanity_problems, bc.LED_MIN_REMARKS, bc.LED_MIN_AIRPORTS = keep


def _baseline(d, n_airports, n_ends):
    p = d / "baseline.json"
    p.write_text(json.dumps({"meta": {}, "airports": {}, "led": {
        "cycle": "2026-10-01", "n_airports": n_airports, "n_ends": n_ends, "review": [],
        "airports": [{"id": "KTMB", "ends": [{"e": "09R", "als": "MALSR", "board": 1}]},
                     {"id": "KMEM", "ends": [{"e": "18L", "als": "MALSR", "board": 1}]}]}}))
    return ["--baseline", str(p)]


def test_whole_build_from_csv_and_from_the_fallback_write_the_same_led_list():
    built = {}
    for reader, kw in (("csv", {}), ("txt", {"csv_tables": False})):
        with fixture(**kw) as d, _small_build_allowed():
            said, out = _refusal(d, _baseline(d, 2, 2))
            assert said is None, said
            built[reader] = json.loads((out / "chase.json").read_text())
            changes = (out / "led_changes.txt").read_text().splitlines()
        j = built[reader]
        assert j["meta"]["nasr_reader"] == reader and j["meta"]["nasr_cycle"] == "2026-10-29", j["meta"]
        assert "2026-10-29" in j["meta"]["nasr_file"], "the app prints the date in nasr_file"
        led = j["led"]
        assert [(a["id"], [e["e"] for e in a["ends"]]) for a in led["airports"]] == [("KPOB", ["23"]), ("KTMB", ["09R"])]
        assert [e for e in j["airports"]["KTMB"] if e.get("led")] == \
            [{"e": "09R", "len": 6000, "als": "MALSR", "rvr": None, "ils": "I", "lpv": 0, "tier": 200, "led": 1}]
        # KILN 22L: ALSF2, 9,000 ft, and no ILS any more
        assert [e for e in j["airports"]["KILN"] if e["e"] == "22L"] == \
            [{"e": "22L", "len": 9000, "als": "ALSF2", "rvr": "TMR", "ils": None, "lpv": 0, "tier": 400}]
        assert changes == [
            "LED ALS: 2 airports, 2 runway ends (FAA NASR 2026-10-29); was 2 airports, 2 runway ends (FAA NASR 2026-10-01)",
            "LED ALS: +KPOB RWY 23",
            "LED ALS: -KMEM RWY 18L"], changes
    assert built["csv"]["airports"] == built["txt"]["airports"]
    assert built["csv"]["led"] == built["txt"]["led"]


def test_three_ends_leaving_the_list_stop_the_whole_build_until_a_person_allows_it():
    def baseline(d):
        p = d / "baseline.json"
        p.write_text(json.dumps({"meta": {}, "airports": {}, "led": {
            "cycle": "2026-10-01", "n_airports": 2, "n_ends": 5, "n_remarks": 4, "review": [],
            "airports": [{"id": "KTMB", "ends": [{"e": "09R"}, {"e": "27L"}, {"e": "13"}]},
                         {"id": "KPOB", "ends": [{"e": "23"}, {"e": "05"}]}]}}))
        return ["--baseline", str(p)]
    with fixture(txt=False) as d, _small_build_allowed():
        said, out = _refusal(d, baseline(d))
        assert said and "3 runway ends left the LED list" in said and "KPOB 05, KTMB 13, KTMB 27L" in said, said
        assert "NOT written" in said and (out / "chase.json").read_text() == "LAST GOOD"
        said, out = _refusal(d, baseline(d) + ["--allow-led-removals", "3"])
        assert said is None, said
        lines = (out / "led_changes.txt").read_text().splitlines()
        assert [x for x in lines if x.startswith("LED ALS: -")] == ["LED ALS: -KPOB RWY 05", "LED ALS: -KTMB RWY 13",
                                                                   "LED ALS: -KTMB RWY 27L"], lines


def test_a_collapsed_led_list_is_not_written_over_the_last_good_file():
    with fixture(txt=False) as d, _small_build_allowed():
        said, out = _refusal(d, _baseline(d, 47, 54))       # the committed file lists 47 airports; this cycle finds 2
        assert said and "fell from 47 to 2 airports" in said and "NOT written" in said, said
        assert (out / "chase.json").read_text() == "LAST GOOD" and not (out / "led_changes.txt").exists()


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
