"""Tests for forecast/parse_nbm.py against small hand-built NBM text blocks.

The blocks mimic the real NBM V5.0 layout: a row name, then 3-character
right-aligned cells that run together when a value fills all three
characters (e.g. ` CIG -88-88150`).
"""
import pytest

from parse_nbm import parse_collective


def row(key: str, vals) -> str:
    """One NBM text row: ' KEY ' then 3-wide right-aligned columns."""
    return f" {key:<4}" + "".join(f"{v:>3}" for v in vals)


def nbs_block(icao="KSFO", date="11/02/2026", cyc="1200"):
    return "\n".join([
        f" {icao}    NBM V4.2 NBS GUIDANCE    {date}  {cyc} UTC",
        row("DT", ["/NO", "V", "3"]),
        row("UTC", ["15", "18", "21"]),
        row("FHR", ["03", "06", "09"]),
        row("TMP", [55, 58, 61]),
        row("DPT", [54, 50, 48]),
        row("WSP", [3, 7, 12]),
        row("CIG", [2, -88, 15]),
        row("IFC", [60, 10, 0]),
        row("VIS", [30, 100, 70]),
        row("IFV", [45, 5, 0]),
    ])


def nbh_block(icao="KSFO", cyc="2200"):
    # NBH has no FHR row: leads come from the UTC row and roll past midnight
    return "\n".join([
        f" {icao}    NBM V4.2 NBH GUIDANCE    11/02/2026  {cyc} UTC",
        row("UTC", ["23", "00", "01"]),
        row("TMP", [50, 49, 48]),
        row("DPT", [49, 49, 48]),
        row("VIS", [2, 1, 4]),
        row("CIG", [1, 1, 3]),
        row("LIC", [70, 80, 75]),
        row("LIV", [55, 65, 60]),
    ])


def write(tmp_path, text, name="c.txt"):
    p = tmp_path / name
    p.write_text(text + "\n", encoding="latin-1")
    return p


def test_nbs_units_are_normalized(tmp_path):
    rows = parse_collective(write(tmp_path, nbs_block()))
    assert [r["fhr"] for r in rows] == [3, 6, 9]
    first, second, third = rows
    assert first["icao"] == "KSFO" and first["product"] == "NBS"
    assert first["cycle"] == "2026-11-02T12:00:00"
    # visibility arrives in tenths of a mile, 100 = 10 mi cap
    assert first["vis_sm"] == 3.0 and second["vis_sm"] == 10.0 and third["vis_sm"] == 7.0
    # ceiling arrives in hundreds of feet; -88 means unlimited
    assert first["ceil_ft"] == 200.0 and second["ceil_ft"] is None and third["ceil_ft"] == 1500.0
    assert (first["tmp"], first["dpt"], first["wsp"]) == (55.0, 54.0, 3.0)
    assert (first["ifc"], first["ifv"]) == (60.0, 45.0)
    # rows absent from the block come back as None, not a crash
    assert first["lic"] is None and first["liv"] is None


def test_nbh_leads_derived_from_utc_row_across_midnight(tmp_path):
    rows = parse_collective(write(tmp_path, nbh_block(cyc="2200")))
    assert [r["fhr"] for r in rows] == [1, 2, 3]
    assert all(r["product"] == "NBH" for r in rows)
    assert [r["liv"] for r in rows] == [55.0, 65.0, 60.0]
    assert [r["lic"] for r in rows] == [70.0, 80.0, 75.0]
    assert [r["vis_sm"] for r in rows] == [0.2, 0.1, 0.4]
    assert rows[2]["ceil_ft"] == 300.0


def test_station_filter_skips_other_blocks(tmp_path):
    text = nbs_block("KSFO") + "\n" + nbs_block("KOAK") + "\n" + nbs_block("KSJC")
    p = write(tmp_path, text)
    assert {r["icao"] for r in parse_collective(p)} == {"KSFO", "KOAK", "KSJC"}
    assert {r["icao"] for r in parse_collective(p, {"KOAK"})} == {"KOAK"}
    assert parse_collective(p, set()) == []


def test_block_without_lead_axis_is_dropped(tmp_path):
    text = " KSFO    NBM V4.2 NBS GUIDANCE    11/02/2026  1200 UTC\n" + row("TMP", [50, 51])
    assert parse_collective(write(tmp_path, text)) == []


def test_unlimited_ceiling_runs_together_but_still_reads_as_none(tmp_path):
    text = "\n".join([
        " KSFO    NBM V5.0 NBS GUIDANCE    11/02/2026  1200 UTC",
        row("FHR", ["06", "09", "12"]),
        row("CIG", [-88, -88, -88]),
        row("VIS", [100, 100, 100]),
    ])
    rows = parse_collective(write(tmp_path, text))
    assert [r["ceil_ft"] for r in rows] == [None, None, None]
    assert [r["vis_sm"] for r in rows] == [10.0, 10.0, 10.0]


# Known parser bug, found while writing these tests (seen in the live
# 2026-11-02 12z NBS/NBH collectives, ~2.5% of rows): each cell is read as
# body[start-2:end], one character wider than the 3-character cell, so a
# 3-digit value picks up the last character of its left neighbour. When the
# neighbour ends in 0 the extra digit is a harmless leading zero ("0150");
# otherwise a 15,000 ft ceiling after an unlimited one reads as 815,000 ft.
# Fixing it changes the engine's inputs, and the calibration was fit on the
# same parse, so the fix should ship together with a calibration refit.
@pytest.mark.xfail(strict=True, reason="parse_nbm reads one char past the cell's left edge")
@pytest.mark.parametrize("prev, value, expect", [
    (-88, 150, ("ceil_ft", 15000.0)),   # ' CIG -88150' -> 8150 -> 815,000 ft
    (42, 130, ("ceil_ft", 13000.0)),    # ' CIG  42130' -> 2130 -> 213,000 ft
])
def test_three_digit_cell_after_nonzero_neighbour(tmp_path, prev, value, expect):
    key, want = expect
    text = "\n".join([
        " KSFO    NBM V5.0 NBS GUIDANCE    11/02/2026  1200 UTC",
        row("FHR", ["06", "09"]),
        row("CIG", [prev, value]),
        row("VIS", [100, 100]),
    ])
    rows = parse_collective(write(tmp_path, text))
    assert rows[1][key] == want
