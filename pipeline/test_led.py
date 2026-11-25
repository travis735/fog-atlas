#!/usr/bin/env python3
"""LED approach lights in pipeline/build_chase.py: one FAA sentence, parsed.

Run: python3 pipeline/test_led.py   (also collected by pytest)

An infrared EFVS may not see LED approach lights, and the only FAA record of
where they are is free text in the NASR airport remarks. Free text is where a
parser goes wrong quietly: the research census read "RWY 36" as runway "03"
for forty airports before anyone looked, and a reworded sentence would leave
the map saying there are no LED approach lights anywhere.

Every remark below is a real one, copied from APT_RMK.csv of the 2026-07-09 to
2026-11-26 cycles, unless its test says it is made up.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_chase as bc  # noqa: E402

STD = "PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE "
POPE = ("CAUTION: ALL TWY EDGE LGT AND RWY 05/23 EDGE AND APCH LGT EQUIPPED WITH NSTD LED FIXTURES. "
        "LED FIXTURES MAY NOT BE COMPATIBLE WITH ALL NVD OR ENHANCED FLIGHT VISION SYSTEMS.")
MCCHORD = ("ALL TWY EDGE LGT AND RWY 16-34 EDGE, CNTRLN, TDZ LGT EQUIPPED WITH NTSD LED FIXTURES MAY NOT BE "
           "COMPATIBLE WITH ALL NVDS OR ENHANCED FLIGHT VISION SYSTEMS.")


def ends(text):
    r = bc.led_ends_from_remark(text)
    return None if r is None else (r["kind"], r["ends"])


def test_the_faa_sentence_as_the_faa_types_it():
    real = {
        STD + "RWY 36.": ["36"],                           # KVLD and 5 more: must not read as 03
        STD + "RWY 7R.": ["07R"],                          # KLAX: no leading zero; the runway table says 07R
        STD + "RWY 01C.": ["01C"],                         # KIAD: leading zero kept
        STD + "RWY 03.": ["03"],                           # KMHK
        STD + "RWY 9R": ["09R"],                           # KTMB: no leading zero AND no full stop
        STD + "RWY 18R. ": ["18R"],                        # KMCO: blank after the full stop
        STD + "RWY 10L.": ["10L"],                         # KBIL
        STD + "RWY 26L.": ["26L"],                         # KLAS
        STD + "RWY 13 & RWY 31.": ["13", "31"],            # KFOE
        STD + "RWY 17L & RWY 35L.": ["17L", "35L"],        # KOKC
        STD + "RWY 31L & RWY 31R.": ["31L", "31R"],        # KDAL
        STD + "RWY 09 & 33R.": ["09", "33R"],              # KIAH, July and August: no second "RWY"
        STD + "RWY 09, RWY 15R & RWY 33R.": ["09", "15R", "33R"],   # KIAH since October
    }
    for text, want in real.items():
        assert ends(text) == ("std", want), (text, ends(text))


def test_every_runway_number_survives_and_36_is_not_03():
    # the two-digit alternatives must be tried before the one-digit one
    for n in range(1, 37):
        for suffix in ("", "L", "C", "R"):
            want = [f"{n:02d}{suffix}"]
            for token in {f"{n}{suffix}", f"{n:02d}{suffix}"}:
                assert ends(f"{STD}RWY {token}.") == ("std", want), (token, ends(f"{STD}RWY {token}."))
    assert ends(STD + "RWY 37.") == ("std", []), "37 is not a runway"
    assert ends(STD + "RWY 051/231.") == ("std", []), "Pope's assault strip is not runways 05 and 23"


def test_wordings_the_faa_has_printed_or_may_type_next():
    # made up, after the forms in the printed Chart Supplement ("Rwy 13 and
    # Rwy 31") and the 2025 remark that named no runway at all
    made_up = {
        STD + "RWYS 13 AND 31.": ["13", "31"],
        STD + "RWY 13/31.": ["13", "31"],
        STD + "RWY 4L, 4R, AND 22L": ["04L", "04R", "22L"],
        STD + "RWY36.": ["36"],
        STD + "RWY 08L-26R.": ["08L", "26R"],
        STD.rstrip() + ".": [],                                           # real, Dec 2024 - Apr 2025
        STD + "RWY 01C & RWY 19C. RWY 12 PAPI OTS.": ["01C", "19C"],      # the next sentence is not LED
        STD + "RWY 14; RWY 32 PAPI OTS.": ["14"],
    }
    for text, want in made_up.items():
        assert ends(text) == ("std", want), (text, ends(text))
    # the sentence twice in one remark
    assert ends(STD + "RWY 16. " + STD + "RWY 34.") == ("std", ["16", "34"])
    # lower case, as the Chart Supplement prints it
    assert ends("Pilots conducting EFVS ops; be aware LED ALS in use Rwy 17L & Rwy 35L.") == ("std", ["17L", "35L"])


def test_pope_aaf_is_read_although_it_is_not_the_faa_sentence():
    r = bc.led_ends_from_remark(POPE)
    assert (r["kind"], r["ends"], r["pair"]) == ("other", ["05", "23"], True), r
    # made up: other words that still say the approach lights are LED
    assert ends("RWY 14 MALSR CONVERTED TO LED.") == ("other", ["14"])


def test_led_lights_that_are_not_approach_lights_are_left_alone():
    real = [
        "RWY 17/35 LED LGT CAN APPR BRGTR THAN RWY 16/34; ENSURE COR RWY ALNMT.",   # KFTW: APPR = appear
        "RWY 03R - 03L PAPI LED. RWY 21R - 21L PAPI INCANDESCENT.",                # KLSV
        "CAUTION: LED OBST LGT INSTL WHICH MAY NOT BE VIS TO SOME NVD OR NGT VISION GOGGLES.",   # KCKA
        "SERVICE-LGT: RWY 15C/33C LED LGT IN USE.",                                 # KSPS: runway lights
        "LED LGTG ON AIRFIELD RWYS AND TWYS.  LEDS MAY NOT BE COMPATIBLE WITH CERTAIN NGT VISION DEVICES.",   # KSLI
        "FAA APV LED RWY AND TWY EDGE MED INTENSITY LGT IN USE.",                   # KHBE
        "TWO APCH/DEP PATHS; ONE NORTH & ONE WEST, EACH DIRECTION LIGHTED BT FIVE (5) AMBER LED LIGHTS.",   # 7NY9 heliport
        "APCH DEP PATHS:  290/110 DEGS AND 105/285 DEGS (STD) WITH GREEN LED LEAD-IN LGTS.",   # NR80 heliport
        "RWY LGTS 200 FT SPACING USING LEDS.  RWY 18/36 MKD WITH PAVERS & LGTS.",   # 82J
        "LED",                                                                      # 14OK heliport, the whole remark
        "ARPT CLSD TO NON-ENG ACFT.",                                               # KTMB: no LED at all
    ]
    for text in real:
        assert bc.led_ends_from_remark(text) is None, (text, bc.led_ends_from_remark(text))
    # made up: LED hides inside CALLED and CLOSED
    assert bc.led_ends_from_remark("MALSR RWY 14 CLOSED; TWR MUST BE CALLED.") is None


def test_an_efvs_warning_without_approach_lights_goes_to_a_person():
    r = bc.led_ends_from_remark(MCCHORD)           # KTCM: LED edge, centerline and TDZ lights
    assert r["kind"] == "watch" and r["ends"] == [] and "EFVS" in r["why"], r
    # made up: approach lights and LED in the same remark but different sentences
    r = bc.led_ends_from_remark("MALSR RWY 14 OTS. RWY 32 PAPI LED.")
    assert r["kind"] == "watch" and "different sentences" in r["why"], r


# ---- the census: remarks joined to the airport's runway ends -----------------

def _nasr(remarks, ends_by_site, n_remarks=89_000):
    fac = {s: {"lid": s.split(".")[0], "icao": "K" + s.split(".")[0], "name": s + " FLD", "st": "ZZ",
               "lat": 40.123456, "lon": -100.654321} for s in ends_by_site}
    return {"reader": "csv", "cycle": "2026-10-29", "fac": fac, "ends": ends_by_site,
            "site_icao": {s: f["icao"] for s, f in fac.items()}, "remarks": remarks, "n_remarks": n_remarks}


def test_census_joins_remarks_to_runway_ends():
    n = _nasr(
        [("LAX.*A", STD + "RWY 7R."),
         ("LAS.*A", STD + "RWY 26L."), ("LAS.*A", STD + "RWY 26L."),      # KLAS: elements A110-46 and A110-49
         ("POB.*A", POPE),
         ("TCM.*A", MCCHORD),
         ("7NY9.*H", STD + "RWY 36."),                                   # made up: a heliport is never an ALS
         ("FTW.*A", "RWY 17/35 LED LGT CAN APPR BRGTR THAN RWY 16/34; ENSURE COR RWY ALNMT.")],
        {"LAX.*A": {"07L": "MALSR", "25R": None, "07R": "MALSR", "25L": "ALSF2"},
         "LAS.*A": {"08L": None, "26R": "MALS", "08R": None, "26L": "MALSF"},
         "POB.*A": {"05": None, "23": "ALSF1", "051": None, "231": None},
         "TCM.*A": {"16": "ALSF1", "34": "ALSF1"}, "7NY9.*H": {"36": "MALSR"}, "FTW.*A": {"17": None}})
    found, review, stats = bc.led_census(n)
    assert {s: f["ends"] for s, f in found.items()} == {"LAX.*A": ["07R"], "LAS.*A": ["26L"], "POB.*A": ["23"]}, found
    assert found["POB.*A"]["nonstd"] and not found["LAX.*A"]["nonstd"]
    assert stats == {"remarks": 89_000, "mention": 4, "std": 3, "tokens": 5, "unmatched": 0, "no_als": 0}, stats
    assert [(x["id"], x["why"]) for x in review] == [
        # the second KLAS row may be meant for 26R (MALS): merged, and said
        ("KLAS", "the same LED remark is filed twice and another runway end here has approach lights"),
        ("KTCM", "LED lights with an EFVS warning, approach lights not named")], review


def test_census_never_drops_what_it_cannot_place():
    n = _nasr(
        [("AAA.*A", STD + "RWY 19."),            # made up: the airport has 18/36, the FAA typed 19
         ("BBB.*A", STD + "RWY 09."),            # made up: 09 exists and has no approach lights on record
         ("CCC.*A", STD.rstrip() + ".")],        # the 2025 wording: no runway
        {"AAA.*A": {"18": "MALSR", "36": None}, "BBB.*A": {"09": None, "27": "MALSR"}, "CCC.*A": {"14": "MALSR"}})
    found, review, stats = bc.led_census(n)
    assert {s: f["ends"] for s, f in found.items()} == {"AAA.*A": [], "BBB.*A": ["09"], "CCC.*A": []}, found
    assert stats["tokens"] == 2 and stats["unmatched"] == 1 and stats["no_als"] == 1, stats
    assert sorted((x["id"], x["why"]) for x in review) == [
        ("KAAA", "RWY 19 is not a runway end at this airport"),
        ("KBBB", "RWY 09 has no approach light system on record"),
        ("KCCC", "LED approach lights with no runway named")], review


def _built(n, airports, atlas, ir=None):
    found, review, stats = bc.led_census(n)
    return bc.led_block(n, found, review, airports, atlas, ir or {}), stats


def _end(e, als=None, **kw):
    return {"e": e, "len": 8000, "als": als, "rvr": None, "ils": None, "lpv": 0, "tier": 400, **kw}


def test_block_lists_every_led_airport_and_flags_the_board_ends():
    n = _nasr([("SAV.*A", STD + "RWY 10."), ("SGT.*A", STD + "RWY 36."), ("STX.*A", STD + "RWY 10."), ("POB.*A", POPE),
               ("YXX.*A", STD + "RWY 07.")],        # made up: NASR lists 82 Canadian fields, none with this remark
              {"SAV.*A": {"10": "MALSR", "28": None}, "SGT.*A": {"18": None, "36": "MALSF"},
               "STX.*A": {"10": "MALSR", "28": None}, "POB.*A": {"05": None, "23": "ALSF1"},
               "YXX.*A": {"07": "MALSR", "25": None}})
    n["fac"]["YXX.*A"]["icao"] = n["site_icao"]["YXX.*A"] = "CYXX"
    airports = {"KSAV": [_end("10", "MALSR"), _end("28")], "KPOB": [_end("05"), _end("23", "ALSF1")],
                "CYXX": [_end("07", "SSALR", cur=1)]}     # on the board from the curated Canadian tier
    ir = {("KSAV", "10"): {"asof": "2025-01-14", "source": "FAA list", "url": "https://example.invalid/list.pdf"},
          ("KMEM", "18L"): {"asof": "2025-01-14", "source": "FAA list", "url": ""}}
    led, _ = _built(n, airports, {"KSAV", "KPOB", "KSTX", "CYXX"}, ir)
    # Canada has no source: a curated end is never flagged from an FAA remark,
    # so on a curated end no "led" key means unknown, not "no LED"
    assert "led" not in airports["CYXX"][0], airports["CYXX"]
    assert [a["id"] for a in led["airports"]] == ["CYXX", "KPOB", "KSAV", "KSGT", "KSTX"]   # sorted, every one
    by = {a["id"]: a for a in led["airports"]}
    assert (by["CYXX"]["board"], by["CYXX"]["ends"]) == (0, [{"e": "07", "als": "MALSR", "board": 0}]), by["CYXX"]
    assert by["KSAV"]["ends"] == [{"e": "10", "als": "MALSR", "board": 1, "led_ir": 1, "ir_asof": "2025-01-14"}]
    assert (by["KSGT"]["atlas"], by["KSGT"]["board"], by["KSGT"]["ends"]) == (0, 0, [{"e": "36", "als": "MALSF", "board": 0}])
    assert (by["KSTX"]["atlas"], by["KSTX"]["board"]) == (1, 0)                            # in the atlas, not on the board
    assert by["KPOB"]["nonstd"] == 1 and by["KPOB"]["rmk"].startswith("CAUTION: ALL TWY EDGE LGT")
    assert (by["KSAV"]["lat"], by["KSAV"]["lon"], by["KSAV"]["name"]) == (40.1235, -100.6543, "SAV.*A FLD")
    counts = {k: v for k, v in led.items() if k.startswith("n_")}
    assert counts == {"n_remarks": 89_000, "n_airports": 5, "n_ends": 5, "n_airports_board": 2, "n_ends_board": 2,
                      "n_airports_atlas": 4, "n_led_ir": 1}, counts
    assert led["cycle"] == "2026-10-29"
    # no remark, so not on the map; KMEM is not among this fixture's airports, so nothing is flagged either
    assert led["ir_unused"] == [{"id": "KMEM", "e": "18L", "asof": "2025-01-14", "source": "FAA list", "als": None,
                                 "board": 0}]
    # the board ends themselves
    assert airports["KSAV"][0]["led"] == 1 and airports["KSAV"][0]["led_ir"] == 1
    assert airports["KPOB"][1]["led"] == 1 and "led_ir" not in airports["KPOB"][1]
    for e in (airports["KSAV"][1], airports["KPOB"][0]):
        assert "led" not in e and "led_ir" not in e, e


def test_an_led_airport_with_no_runway_placed_flags_every_approach_light_end():
    """2026-11-25 review: two real sentences changed to "RWY 63". The build
    wrote; KVLD was ringed on the map while its row read "not flagged" and
    "avoid LED ends" kept it. The remark says the airport has LED approach
    lights, so every end there that has approach lights carries led_unk."""
    n = _nasr([("VLD.*A", STD + "RWY 63."), ("CCC.*A", STD.rstrip() + "."), ("SAV.*A", STD + "RWY 10.")],
              {"VLD.*A": {"17": None, "35": "MALSR", "04": None, "22": "MALS"}, "CCC.*A": {"14": "MALSR", "32": None},
               "SAV.*A": {"10": "MALSR", "28": None}})
    airports = {"KVLD": [_end("17"), _end("35", "MALSR"), _end("04"), _end("22", "MALS")],
                "KCCC": [_end("14", "MALSR"), _end("32")], "KSAV": [_end("10", "MALSR"), _end("28")]}
    led, stats = _built(n, airports, {"KVLD", "KCCC", "KSAV"})
    by = {a["id"]: a for a in led["airports"]}
    for icao, want in (("KVLD", ["35", "22"]), ("KCCC", ["14"])):
        assert by[icao]["rwy_unknown"] == 1 and by[icao]["ends"] == [] and by[icao]["board"] == 1, by[icao]
        assert by[icao]["rmk"].startswith("PILOTS CONDUCTING EFVS OPS"), "the app shows the remark's own words"
        assert [e["e"] for e in airports[icao] if e.get("led_unk")] == want, airports[icao]
        assert not [e for e in airports[icao] if e.get("led")], "led is only ever a runway end the FAA named"
    assert "led_unk" not in airports["KSAV"][0] and airports["KSAV"][0]["led"] == 1
    # a number that is no runway anywhere counts as a runway that matched nothing, and is named
    assert stats["tokens"] == 2 and stats["unmatched"] == 1, stats
    assert ("KVLD", "RWY 63 is not a runway number") in [(x["id"], x["why"]) for x in led["review"]], led["review"]
    r = bc.led_ends_from_remark(STD + "RWY 63 & RWY 17.")
    assert r["ends"] == ["17"] and r["bad"] == ["63"], r
    assert bc.led_ends_from_remark(STD + "RWY 36, RWY 7R & RWY 051.")["bad"] == ["051"]
    # two such typos in one cycle's remarks are over the gate's tolerance of a runway that matches nothing
    assert bc.led_problems(_led(47, 54), dict(HEALTHY, tokens=58, unmatched=3), _led(47, 54)), "3 of 58 must stop the build"


def test_a_reworded_efvs_remark_never_just_leaves_the_list():
    """Same review: nine sentences changed to "LIGHT EMITTING DIODE ALS IN USE"
    or to "LED APCH LTG IN USE" left the map and the board, the first with
    nothing on the review list (the pre-filter looked for the letters LED)."""
    assert bc.led_ends_from_remark("PILOTS CONDUCTING EFVS OPS; BE AWARE LIGHT EMITTING DIODE ALS IN USE RWY 14.")["ends"] == ["14"]
    assert bc.led_ends_from_remark("PILOTS CONDUCTING EFVS OPS; BE AWARE LIGHT EMITTING DIODE ALS IN USE RWY 14.")["kind"] == "std"
    r = bc.led_ends_from_remark("PILOTS CONDUCTING EFVS OPS; BE AWARE LED APCH LTG IN USE RWY 14.")
    assert (r["kind"], r["ends"]) == ("other", ["14"]), r
    # addressed to EFVS pilots and no LED in any spelling: a person reads it
    r = bc.led_ends_from_remark("PILOTS CONDUCTING EFVS OPS; BE AWARE SOLID STATE ALS IN USE RWY 14.")
    assert r["kind"] == "watch" and r["why"] == "a remark for EFVS pilots that does not say LED", r
    assert bc.led_ends_from_remark("RWY 14 PAPI OTS.") is None
    # the pre-filter the CSV and fixed-width readers both use lets all three through
    for text in ("BE AWARE LIGHT EMITTING DIODE ALS IN USE RWY 14", "PILOTS CONDUCTING EFVS OPS; SOLID STATE ALS",
                 "ENHANCED FLIGHT VISION SYSTEM USERS: NEW ALS RWY 14", "L.E.D", "LED ALS IN USE"):
        assert bc._LED_HINT.search(text), text
    assert not bc._LED_HINT.search("RWY 14 PAPI OTS.")
    n = _nasr([("AAA.*A", "PILOTS CONDUCTING EFVS OPS; BE AWARE SOLID STATE ALS IN USE RWY 36.")],
              {"AAA.*A": {"18": None, "36": "MALSR"}})
    found, review, _ = bc.led_census(n)
    assert found == {} and [(x["id"], x["why"]) for x in review] == [("KAAA", "a remark for EFVS pilots that does not say LED")]


def test_led_runway_lighting_at_a_field_with_approach_lights_goes_to_a_person():
    """KSPS ("RWY 15C/33C LED LGT IN USE", both ends ALSF1) and KSLI ("LED LGTG
    ON AIRFIELD RWYS AND TWYS") were dropped without a word: not a claim about
    approach lights, so not listed, but nobody had been asked to read them."""
    sps, sli = "SERVICE-LGT: RWY 15C/33C LED LGT IN USE.", "LED LGTG ON AIRFIELD RWYS AND TWYS.  LEDS MAY NOT BE COMPATIBLE WITH CERTAIN NGT VISION DEVICES."
    assert bc.led_runway_lights(sps) and bc.led_runway_lights(sli)
    for other in ("RWY 03R - 03L PAPI LED. RWY 21R - 21L PAPI INCANDESCENT.", "CAUTION: DRONE RWY; LED OBSTN LGT IN USE ON FMQ-22 WX EQUIP.",
                  "FAA APV LED RWY AND TWY EDGE MED INTENSITY LGT IN USE.", "DUAL LIGHTING SYSTEM OPERATIONS IN EFFECT DUE TO TESTING OF LED MIRL & TWY LGTS.",
                  "CTN: LED OBST LGTS MAY NOT BE VIS NVD OR NGT VISION GOGGLES.", "ARPT CLSD TO NON-ENG ACFT."):
        assert not bc.led_runway_lights(other), other
    n = _nasr([("SPS.*A", sps), ("SLI.*A", sli), ("3MT4.*A", "RWY 04/22 LED LGTS AND REFLECTORS.")],
              {"SPS.*A": {"15C": "ALSF1", "33C": "ALSF1"}, "SLI.*A": {"22R": "SALSF", "04L": None}, "3MT4.*A": {"04": None, "22": None}})
    found, review, stats = bc.led_census(n)
    assert found == {} and stats["mention"] == 0, "never listed: it is not a claim about approach lights"
    assert sorted(x["id"] for x in review) == ["KSLI", "KSPS"], review       # the strip with no approach lights is left alone
    assert all("approach lights not named" in x["why"] for x in review)


def test_the_same_remark_filed_twice_is_said_when_another_end_has_approach_lights():
    """KLAS carries the FAA sentence twice (A110-46, A110-49), both "RWY 26L",
    and has approach lights on 01L and 26R too: the second may be meant for
    another end. It is merged as before, and now on the review list."""
    n = _nasr([("LAS.*A", STD + "RWY 26L."), ("LAS.*A", STD + "RWY 26L."), ("TMB.*A", STD + "RWY 9R."), ("TMB.*A", STD + "RWY 9R.")],
              {"LAS.*A": {"01L": "MALSF", "26R": "MALS", "26L": "MALSF"}, "TMB.*A": {"09R": "MALSR", "27L": None}})
    found, review, _ = bc.led_census(n)
    assert found["LAS.*A"]["ends"] == ["26L"] and found["TMB.*A"]["ends"] == ["09R"]
    assert [(x["id"], x["why"]) for x in review] == [
        ("KLAS", "the same LED remark is filed twice and another runway end here has approach lights")], review


def test_a_listed_end_with_no_remark_is_marked_on_the_board():
    """KMEM 18L is on the FAA's list of 14 January 2025 as LED with infrared
    emitters; its remark left NASR at the 2026-05-14 cycle. The board showed it
    with no tag and "avoid LED ends" kept it. It now carries led_was (never
    led: the map and "only LED ends" show what the FAA says today)."""
    n = _nasr([("SAV.*A", STD + "RWY 10.")], {"SAV.*A": {"10": "MALSR", "28": None}, "MEM.*A": {"18L": "MALSR", "36R": "ALSF2"}})
    airports = {"KSAV": [_end("10", "MALSR"), _end("28")], "KMEM": [_end("18L", "MALSR"), _end("36R", "ALSF2")]}
    ir = {("KSAV", "10"): {"asof": "2025-01-14", "source": "FAA list", "url": "https://example.invalid/l.pdf"},
          ("KMEM", "18L"): {"asof": "2025-01-14", "source": "FAA list", "url": "https://example.invalid/l.pdf"}}
    led, _ = _built(n, airports, {"KSAV", "KMEM"}, ir)
    assert led["ir_unused"] == [{"id": "KMEM", "e": "18L", "asof": "2025-01-14", "source": "FAA list", "als": "MALSR", "board": 1}]
    assert airports["KMEM"][0] == dict(_end("18L", "MALSR"), led_was=1) and "led_was" not in airports["KMEM"][1]
    assert "led" not in airports["KMEM"][0] and [a["id"] for a in led["airports"]] == ["KSAV"]
    assert "led_was" not in airports["KSAV"][0] and airports["KSAV"][0]["led_ir"] == 1


# ---- the gate ---------------------------------------------------------------

def _led(n_airports, n_ends, cycle="2026-11-26"):
    return {"cycle": cycle, "n_airports": n_airports, "n_ends": n_ends, "airports": [], "review": []}


HEALTHY = {"remarks": 89_004, "mention": 48, "std": 47, "tokens": 56, "unmatched": 0, "no_als": 0}


def test_gate_passes_a_healthy_cycle_and_a_single_removal():
    prev = _led(47, 54, "2026-10-29")
    assert bc.led_problems(_led(47, 54), HEALTHY, prev) == []
    assert bc.led_problems(_led(47, 54), HEALTHY, None) == [], "a first build has nothing to compare with"
    # KMEM 18L left the list at the 2026-05-14 cycle: one removal is normal
    assert bc.led_problems(_led(46, 53), HEALTHY, prev) == []
    assert bc.led_problems(_led(60, 70), HEALTHY, prev) == [], "growth is the programme working"
    assert bc.led_problems(_led(47, 54), dict(HEALTHY, unmatched=2), prev) == [], "two FAA typos in 56 runways pass"


def test_gate_refuses_a_reworded_sentence():
    p = bc.led_problems(_led(47, 54), dict(HEALTHY, std=0), _led(47, 54))
    assert len(p) == 1 and "reworded" in p[0] and "48 remarks" in p[0], p


def test_gate_refuses_runways_that_are_not_at_the_airport():
    p = bc.led_problems(_led(47, 54), dict(HEALTHY, unmatched=3), _led(47, 54))
    assert len(p) == 1 and "3 of the 56 runways" in p[0], p


def test_gate_refuses_a_list_a_fifth_smaller_than_the_committed_one():
    prev = _led(47, 54, "2026-10-29")
    p = bc.led_problems(_led(37, 43), HEALTHY, prev)
    assert len(p) == 2 and "fell from 47 to 37 airports" in p[0] and "fell from 54 to 43 runway ends" in p[1], p
    assert bc.led_problems(_led(38, 44), HEALTHY, prev) == [], "38 of 47 is inside a fifth"
    p = bc.led_problems(_led(47, 40), HEALTHY, prev)     # same airports, a quarter of the runway ends gone
    assert len(p) == 1 and "runway ends" in p[0], p


def test_gate_refuses_an_empty_or_truncated_remark_table():
    p = bc.led_problems(_led(0, 0), {"remarks": 89_004, "mention": 0, "std": 0, "tokens": 0, "unmatched": 0}, None)
    assert len(p) == 1 and "only 0 airports" in p[0], p              # no previous file to lean on
    p = bc.led_problems(_led(47, 54), dict(HEALTHY, remarks=1_200), _led(47, 54))
    assert len(p) == 1 and "only 1,200 airport remarks" in p[0], p


def test_gate_refuses_more_than_two_runway_ends_leaving_the_list():
    """2026-11-25 review: nine airports' remarks deleted (47 to 38) was written
    with exit 0; only ten were refused. A dropped end reads "not flagged" on
    the board from then on. One end left in all of 2026 (KMEM 18L)."""
    full = _listed([(f"K{i:03d}", "09") for i in range(47)], cycle="2026-10-29")
    less = lambda k: _listed([(f"K{i:03d}", "09") for i in range(k, 47)])
    assert bc.led_problems(less(1), HEALTHY, full) == [] and bc.led_problems(less(2), HEALTHY, full) == []
    p = bc.led_problems(less(3), HEALTHY, full)
    assert len(p) == 1 and "3 runway ends left the LED list" in p[0] and "K000 09, K001 09, K002 09" in p[0], p
    assert "--allow-led-removals 3" in p[0], "the message says how a person who has read the remarks lets it through"
    assert bc.led_problems(less(3), HEALTHY, full, allow_removed=3) == []
    assert len(bc.led_problems(less(9), HEALTHY, full)) == 1, "the nine the review got through"
    # an end swapped for another at the same airport is one gone and one new
    swapped = _listed([("K000", "27"), ("K001", "27"), ("K002", "27")] + [(f"K{i:03d}", "09") for i in range(3, 47)])
    assert any("3 runway ends left" in x for x in bc.led_problems(swapped, HEALTHY, full))
    assert bc.led_problems(full, HEALTHY, None) == [], "a first build has nothing to compare with"


def test_gate_refuses_a_remark_table_shorter_than_the_last_one():
    """Same review: APT_RMK.csv cut to 80,000 of 88,993 rows was written (the
    floor was a fixed 40,000) and two LED airports were simply gone."""
    prev = dict(_led(47, 54, "2026-10-29"), n_remarks=88_993)
    assert bc.led_problems(_led(47, 54), dict(HEALTHY, remarks=88_100), prev) == []          # a normal month
    p = bc.led_problems(_led(47, 54), dict(HEALTHY, remarks=80_000), prev)
    assert len(p) == 1 and "only 80,000 airport remarks read" in p[0] and "88,993" in p[0], p
    assert bc.led_problems(_led(47, 54), dict(HEALTHY, remarks=80_000), _led(47, 54)) == [], \
        "a committed file from before the count was stored falls back to the fixed floor"


def test_gate_refuses_led_ends_that_have_no_approach_lights_on_record():
    """Same review: with the approach-light column blank, every end the FAA
    sentence names "has no approach light system on record" and the build only
    added 56 review lines."""
    assert bc.led_problems(_led(47, 54), dict(HEALTHY, no_als=2), _led(47, 54)) == []
    p = bc.led_problems(_led(47, 54), dict(HEALTHY, no_als=54), _led(47, 54))
    assert len(p) == 1 and "54 runway ends the FAA says have LED approach lights have no approach-light" in p[0], p
    blank = {f"A{i:02d}.*A": {"18": None, "36": None} for i in range(40)}       # the 40 airports of _real_like, lights gone
    led, stats = _built(_nasr([(s, "PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY 36.") for s in blank], blank), {}, set())
    assert stats["no_als"] == 40 and any("no approach-light" in x for x in bc.led_problems(led, stats, None))


def _real_like(sentence="LED ALS IN USE", shift=False):
    """Forty airports in the FAA's own words; `sentence` and `shift` break them
    the two ways a new cycle could."""
    sites = {f"A{i:02d}.*A": {"18": None, "36": "MALSR"} for i in range(40)}
    text = f"PILOTS CONDUCTING EFVS OPS; BE AWARE {sentence} RWY {'35' if shift else '36'}."
    return _nasr([(s, text) for s in sites], sites)


def test_gate_end_to_end_on_remarks():
    prev = _led(40, 40, "2026-10-29")
    led, stats = _built(_real_like(), {}, set())
    assert (led["n_airports"], led["n_ends"]) == (40, 40) and bc.led_problems(led, stats, prev) == []
    # the FAA rewords the sentence: the other-wording path still finds the
    # runways, and the build is refused all the same so that a person looks
    led, stats = _built(_real_like(sentence="LED APCH LGTS NOW IN USE"), {}, set())
    assert led["n_ends"] == 40 and stats["std"] == 0
    assert any("reworded" in p for p in bc.led_problems(led, stats, prev)), bc.led_problems(led, stats, prev)
    # a wording with no approach-light word at all empties the list
    led, stats = _built(_real_like(sentence="LED LAMPS FITTED"), {}, set())
    assert led["n_airports"] == 0 and len(bc.led_problems(led, stats, prev)) == 3, bc.led_problems(led, stats, prev)
    # the runway named is not at the airport
    led, stats = _built(_real_like(shift=True), {}, set())
    p = bc.led_problems(led, stats, prev)
    assert any("40 of the 40 runways" in x for x in p) and any("runway ends" in x for x in p), p


# ---- the change lines -------------------------------------------------------

def _listed(pairs, ir=(), review=(), cycle="2026-11-26"):
    by = {}
    for a, e in pairs:
        by.setdefault(a, [])
        if e:
            by[a].append({"e": e, "als": "MALSR", "board": 1, **({"led_ir": 1} if (a, e) in ir else {})})
    return {"cycle": cycle, "n_airports": len(by), "n_ends": sum(len(v) for v in by.values()),
            "airports": [{"id": a, "ends": v} for a, v in sorted(by.items())],
            "review": [{"id": a, "why": w, "text": ""} for a, w in review]}


def test_change_lines_name_each_runway_end():
    prev = _listed([("KMEM", "18L"), ("KSAV", "10"), ("KIAH", "09"), ("KIAH", "33R")], cycle="2026-10-29")
    new = _listed([("KSAV", "10"), ("KIAH", "09"), ("KIAH", "15R"), ("KIAH", "33R"), ("KSNS", "31")])
    assert bc.led_change_lines(new, prev) == [
        "LED ALS: 3 airports, 5 runway ends (FAA NASR 2026-11-26); was 3 airports, 4 runway ends (FAA NASR 2026-10-29)",
        "LED ALS: +KIAH RWY 15R",
        "LED ALS: +KSNS RWY 31",
        "LED ALS: -KMEM RWY 18L",
    ], bc.led_change_lines(new, prev)


def test_change_lines_say_no_change_and_first_build():
    same = _listed([("KSAV", "10")])
    assert bc.led_change_lines(same, _listed([("KSAV", "10")], cycle="2026-10-29")) == [
        "LED ALS: 1 airports, 1 runway ends (FAA NASR 2026-11-26); was 1 airports, 1 runway ends (FAA NASR 2026-10-29)",
        "LED ALS: no change"]
    first = bc.led_change_lines(same, None)       # the committed chase.json predates the LED list
    assert first[0].endswith("the committed chase.json has no LED list, so every one is new") and \
        first[1:] == ["LED ALS: +KSAV RWY 10"], first


def test_change_lines_cover_ir_marks_unnamed_runways_and_review_items():
    prev = _listed([("KSAV", "10"), ("KCMH", "28L")], ir={("KCMH", "28L")}, cycle="2026-10-29")
    new = _listed([("KSAV", "10"), ("KCMH", "28L"), ("KXYZ", None)], ir={("KSAV", "10")},
                  review=[("KTCM", "LED lights with an EFVS warning, approach lights not named")])
    assert bc.led_change_lines(new, prev)[1:] == [
        "LED ALS: +KXYZ (runway not named)",
        "LED ALS: KSAV RWY 10 now marked LED + IR (curated)",
        "LED ALS: KCMH RWY 28L no longer marked LED + IR",
        "LED ALS review: +KTCM — LED lights with an EFVS warning, approach lights not named",
    ], bc.led_change_lines(new, prev)


# ---- the curated infrared list ------------------------------------------------

def test_committed_ir_list_cites_a_source_on_every_row():
    rows = bc.read_led_ir(bc.LED_IR_CSV)
    assert set(rows) == {("KSAV", "10"), ("KLAS", "26L"), ("KCMH", "28L"), ("KMEM", "18L")}, set(rows)
    for key, r in rows.items():
        assert r["source"] and r["url"].startswith("https://www.faa.gov/") and r["asof"] == "2025-01-14", (key, r)


def test_ir_list_must_exist_and_be_well_formed():
    def refused(text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "led_ir_curated.csv"
            if text is not None:
                p.write_text(text)
            try:
                bc.read_led_ir(p)
            except bc.SourceError as e:
                return str(e)
        return None
    # building without the file would turn "LED + IR" into plain "LED" with no word said
    assert "is missing" in refused(None)
    assert "no column source" in refused("icao,end,asof,url\nKSAV,10,2025-01-14,https://x\n")
    assert "needs a runway end, a source and an asof date" in refused("icao,end,asof,source,url\nKSAV,10,,FAA list,\n")
    assert "needs a runway end" in refused("icao,end,asof,source,url\nKSAV,RWY 10,2025-01-14,FAA list,\n")
    assert refused("icao,end,asof,source,url\nKLAX,7R,2025-01-14,FAA list,\n") is None
    with tempfile.TemporaryDirectory() as d:        # "7R" is stored as the runway table spells it
        p = Path(d) / "x.csv"
        p.write_text("icao,end,asof,source,url\nKLAX,7R,2025-01-14,FAA list,\n")
        assert list(bc.read_led_ir(p)) == [("KLAX", "07R")]


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
