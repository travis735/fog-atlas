#!/usr/bin/env python3
"""Tests for the two output-safety helpers in forecast/build_pages.py.

Run: python3 forecast/test_sanitize.py   (also collected by pytest)

clean_upstream() strips markup from third-party text (TAF/METAR, NWS point
forecasts) where it enters the bake; ld_json() keeps a string from closing
the <script> element its JSON-LD sits in. The helpers are lifted out of
build_pages.py by name, so this needs none of the bake's dependencies.
"""
import ast
import json
import re
import sys
from pathlib import Path

SRC = (Path(__file__).parent / "build_pages.py").read_text()
NS = {"re": re, "json": json}
_tree = ast.parse(SRC)
for node in _tree.body:
    is_markup = isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_MARKUP" for t in node.targets)
    is_helper = isinstance(node, ast.FunctionDef) and node.name in ("clean_upstream", "ld_json")
    if is_markup or is_helper:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "build_pages.py", "exec"), NS)
clean_upstream, ld_json = NS["clean_upstream"], NS["ld_json"]


def test_markup_is_removed_from_strings_at_any_depth():
    rec = {"rawTAF": "TAF KPDX 270301Z <script>alert(1)</script> P6SM", "visib": "6+", "n": 3, "none": None,
           "fcsts": [{"wxString": "BR <img src=x onerror=alert(1)>"}],
           "periods": [{"detailedForecast": "Patchy fog before 8am. </script><script>x()</script>"}]}
    out = clean_upstream(rec)
    flat = json.dumps(out)
    assert "<" not in flat and ">" not in flat, flat
    assert out["visib"] == "6+" and out["n"] == 3 and out["none"] is None
    assert out["periods"][0]["detailedForecast"].startswith("Patchy fog before 8am. ")
    assert rec["rawTAF"].count("<") == 2, "the input must not be mutated"


def test_ordinary_text_passes_through_unchanged():
    for s in ("Patchy fog before 8am", "TAF KSFO 241120Z 2412/2518 VRB03KT 1/2SM FG VV002",
              "Chance of precipitation is 30%.", "Kos International Airport \"Ippokratis\"", "A & B", "Zürich — Flughafen", ""):
        assert clean_upstream(s) is s or clean_upstream(s) == s
    assert clean_upstream("tab\tand\nnewline stay") == "tab\tand\nnewline stay"
    assert clean_upstream("nul\x00 and bell\x07 go") == "nul  and bell  go"


def test_ld_json_cannot_close_its_script_element():
    obj = {"@type": "FAQPage", "text": "before </script><script>alert(1)</script> after", "q": "a \"quoted\" word"}
    s = ld_json(obj)
    assert "</" not in s and "<\\/script>" in s
    assert json.loads(s) == obj, "the escape must be invisible to a JSON parser"


def test_every_json_ld_block_uses_ld_json():
    blocks = re.findall(r'application/ld\+json">\{(.*?)\}</script>', SRC)
    assert blocks, "no JSON-LD sinks found — did the template change?"
    assert all(b.startswith(("ld_json(", "jsonld(")) for b in blocks), blocks
    body = SRC[SRC.index("def jsonld("):]
    assert "return ld_json(" in body[:body.index("\ndef ", 10)], "jsonld() must return through ld_json()"


def test_the_bake_cleans_all_three_feeds():
    assert "tafs, obs, nws_raw = clean_upstream(tafs), clean_upstream(obs), clean_upstream(nws_raw)" in SRC


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
