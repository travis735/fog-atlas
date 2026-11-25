#!/usr/bin/env python3
"""pipeline/fetch_nasr.py: the CSV tables come out of a zip inside the zip.

Run: python3 pipeline/test_fetch_nasr.py   (also collected by pytest)

The files it extracts have fixed names (APT_BASE.csv, APT.txt) and no date in
them, so the ways to go wrong are all about a file of one cycle being read as
another: a second run that re-extracts nothing when the zip is new, a table
left over from last month, a zip that no longer has the .txt files (the FAA
ends them 2027-01-21) while last month's APT.txt is still on disk.

The zips here are built by the test with the real member names of
28DaySubscription_Effective_2026-10-29.zip; no network.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_nasr as fn  # noqa: E402


def _nasr_zip(path: Path, cycle="2026/10/29", csv=True, txt=True, drop=(), eff=None, mtime=None):
    """A subscriber zip the shape the FAA ships: CSV_Data/<DD_Mon_YYYY>_CSV.zip
    holding the tables, and the legacy .txt files beside it."""
    eff = eff or {}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("README.txt", "AIS subscriber files")
        if csv:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as zz:
                for name in fn.CSV_TABLES + fn.CSV_DOCS + ("NAV_BASE.csv",):
                    if name in drop:
                        continue
                    if name in fn.CSV_DOCS:
                        zz.writestr(name, "CSV File,Column Name,Max Length,Data Type,Nullable\n")
                    else:
                        zz.writestr(name, f"EFF_DATE,SITE_NO\n{eff.get(name, cycle)},{name} of {cycle}\n")
            z.writestr("CSV_Data/29_Oct_2026_CSV.zip", inner.getvalue())
        if txt:
            for name in fn.TXT_MEMBERS:
                z.writestr(name, f"{name} of {cycle}\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@contextlib.contextmanager
def nasr_dir():
    keep = fn.NASR_DIR
    with tempfile.TemporaryDirectory() as d:
        fn.NASR_DIR = Path(d) / "nasr"
        fn.NASR_DIR.mkdir()
        try:
            yield fn.NASR_DIR, Path(d)
        finally:
            fn.NASR_DIR = keep


def _run(f, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = f(*a)
    return out, buf.getvalue()


def _exit(f, *a):
    try:
        _run(f, *a)
    except SystemExit as e:
        return str(e.code)
    return None


def test_csv_tables_come_out_of_the_zip_in_the_zip():
    with nasr_dir() as (nasr, tmp):
        z = _nasr_zip(tmp / "sub.zip")
        cycle, said = _run(fn.extract_csv, z)
        assert cycle == "2026-10-29", cycle
        assert sorted(p.name for p in (nasr / "CSV").iterdir()) == sorted(fn.CSV_TABLES + fn.CSV_DOCS)
        assert (nasr / "CSV" / "APT_RMK.csv").read_text() == "EFF_DATE,SITE_NO\n2026/10/29,APT_RMK.csv of 2026/10/29\n"
        assert not (nasr / "CSV" / "NAV_BASE.csv").exists(), "only the tables the builder reads"
        assert said.count("extracted CSV/") == len(fn.CSV_TABLES + fn.CSV_DOCS)
        ok, _ = _run(fn.extract_legacy_txt, z)
        assert ok and (nasr / "APT.txt").exists() and (nasr / "Layout_Data" / "apt_rf.txt").exists()


def test_a_second_run_extracts_nothing():
    with nasr_dir() as (nasr, tmp):
        z = _nasr_zip(tmp / "sub.zip", mtime=1_700_000_000)
        _run(fn.extract_csv, z); _run(fn.extract_legacy_txt, z)
        before = {p: p.stat().st_mtime_ns for p in nasr.rglob("*") if p.is_file()}
        (cycle, said1), (_, said2) = _run(fn.extract_csv, z), _run(fn.extract_legacy_txt, z)
        assert cycle == "2026-10-29" and "extracted" not in said1 + said2, said1 + said2
        assert before == {p: p.stat().st_mtime_ns for p in nasr.rglob("*") if p.is_file()}


def test_a_newer_zip_replaces_every_fixed_name_member():
    with nasr_dir() as (nasr, tmp):
        old = _nasr_zip(tmp / "28DaySubscription_Effective_2026-10-29.zip", mtime=1_700_000_000)
        _run(fn.extract_csv, old); _run(fn.extract_legacy_txt, old)
        # next month: a new dated zip, downloaded now, so newer than every extract
        new = _nasr_zip(tmp / "28DaySubscription_Effective_2026-11-26.zip", cycle="2026/11/26",
                        mtime=max(p.stat().st_mtime for p in nasr.rglob("*") if p.is_file()) + 5)
        cycle, _ = _run(fn.extract_csv, new)
        _run(fn.extract_legacy_txt, new)
        assert cycle == "2026-11-26", cycle
        for name in fn.CSV_TABLES:
            assert "2026/11/26" in (nasr / "CSV" / name).read_text(), name
        assert (nasr / "APT.txt").read_text() == "APT.txt of 2026/11/26\n"


def test_tables_of_two_cycles_side_by_side_stop_the_fetch():
    with nasr_dir() as (nasr, tmp):
        z = _nasr_zip(tmp / "sub.zip", eff={"ILS_BASE.csv": "2026/11/26"})
        said = _exit(fn.extract_csv, z)
        assert said and "do not share one EFF_DATE" in said and "ILS_BASE.csv" in said, said


def test_a_table_missing_from_the_csv_zip_is_named():
    with nasr_dir() as (nasr, tmp):
        said = _exit(fn.extract_csv, _nasr_zip(tmp / "sub.zip", drop=("APT_RMK.csv",)))
        assert said and "has no APT_RMK.csv" in said, said
        assert not (nasr / "CSV").exists(), "nothing half-extracted"
    with nasr_dir() as (nasr, tmp):      # the column lists are documentation: their absence is not fatal
        cycle, _ = _run(fn.extract_csv, _nasr_zip(tmp / "sub.zip", drop=fn.CSV_DOCS))
        assert cycle == "2026-10-29" and not (nasr / "CSV" / "APT_CSV_DATA_STRUCTURE.csv").exists()


def test_a_zip_without_txt_files_removes_last_months_copies():
    # from 2027-01-21: APT.txt left on disk from the cycle before would be
    # read by refresh_reference.py as if it were current
    with nasr_dir() as (nasr, tmp):
        old = _nasr_zip(tmp / "old.zip", mtime=1_700_000_000)
        _run(fn.extract_csv, old); _run(fn.extract_legacy_txt, old)
        assert (nasr / "APT.txt").exists()
        new = _nasr_zip(tmp / "new.zip", cycle="2027/02/18", txt=False)
        cycle, _ = _run(fn.extract_csv, new)
        ok, said = _run(fn.extract_legacy_txt, new)
        assert cycle == "2027-02-18" and ok is False
        assert "the FAA has stopped the legacy .txt files" in said, said
        for name in fn.TXT_MEMBERS:
            assert not (nasr / name).exists(), name


def test_a_zip_without_csv_tables_removes_last_months_tables():
    # the builder falls back to APT.txt only when data/nasr/CSV is empty, so
    # tables of an older cycle must not be left there
    with nasr_dir() as (nasr, tmp):
        old = _nasr_zip(tmp / "old.zip", mtime=1_700_000_000)
        _run(fn.extract_csv, old)
        cycle, said = _run(fn.extract_csv, _nasr_zip(tmp / "new.zip", csv=False))
        assert cycle is None and "has no CSV_Data/*_CSV.zip" in said, said
        assert not any((nasr / "CSV" / name).exists() for name in fn.CSV_TABLES + fn.CSV_DOCS)


@contextlib.contextmanager
def _offline(tmp, edition="10/29/2026", **zip_kw):
    """Serve fetch_nasr.main() from memory: APRA's two answers and the two zips."""
    sub = _nasr_zip(tmp / "served.zip", **zip_kw).read_bytes()
    cifp = io.BytesIO()
    with zipfile.ZipFile(cifp, "w") as z:
        z.writestr("FAACIFP18", "HDR01FAACIFP18\n")
    urls = {
        fn.APRA_NASR: json.dumps({"edition": [{"editionDate": edition, "product": {
            "url": "https://nfdc.example/28DaySubscription_Effective_2026-10-29.zip"}}]}).encode(),
        "https://nfdc.example/28DaySubscription_Effective_2026-10-29.zip": sub,
        fn.APRA_CIFP: b'<productSet><edition><product productName="CIFP" url="https://aero.example/CIFP_261029.zip"/></edition></productSet>',
        "https://aero.example/CIFP_261029.zip": cifp.getvalue(),
    }
    asked = []
    keep = fn.get
    fn.get = lambda url, accept=None: (asked.append(url), urls[url])[1]
    try:
        yield asked
    finally:
        fn.get = keep


def test_main_on_a_cold_directory_then_again():
    with nasr_dir() as (nasr, tmp), _offline(tmp) as asked:
        _, said = _run(fn.main)
        assert "CSV tables: cycle 2026-10-29" in said and said.rstrip().endswith("done"), said
        for rel in ("28DaySubscription_Effective_2026-10-29.zip", "CIFP_261029.zip", "FAACIFP18", "APT.txt", "ILS.txt",
                    "Layout_Data/apt_rf.txt", "Layout_Data/ils_rf.txt", *(f"CSV/{n}" for n in fn.CSV_TABLES)):
            assert (nasr / rel).exists(), rel
        assert len(asked) == 4
        del asked[:]
        _, again = _run(fn.main)
        assert "extracted" not in again and again.count("already present, skipping") == 2, again
        assert asked == [fn.APRA_NASR, fn.APRA_CIFP], "the zips are not downloaded twice"


def test_main_refuses_a_zip_whose_tables_are_another_cycle():
    # APRA says 11/26 is current and serves a zip whose tables say 10/29
    with nasr_dir() as (nasr, tmp), _offline(tmp, edition="11/26/2026"):
        said = _exit(fn.main)
        assert said and "APRA calls 2026-11-26 the current cycle" in said and "are for 2026-10-29" in said, said


def test_main_refuses_a_zip_with_nothing_to_read():
    with nasr_dir() as (nasr, tmp), _offline(tmp, csv=False, txt=False):
        said = _exit(fn.main)
        assert said and "neither the CSV tables nor APT.txt/ILS.txt" in said, said


def test_main_carries_on_when_only_the_txt_files_are_gone():
    with nasr_dir() as (nasr, tmp), _offline(tmp, txt=False):
        _, said = _run(fn.main)
        assert "CSV tables: cycle 2026-10-29" in said and not (nasr / "APT.txt").exists(), said


if __name__ == "__main__":
    fails = 0
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            try:
                test(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", name, "-", repr(e)[:300])
    print("FAILED" if fails else "passed")
    sys.exit(1 if fails else 0)
