#!/usr/bin/env python3
"""Build deploy.json — per-airport expected chaseable hours for the next 14 days.

The DEPLOY planner's data. Days are counted from the service day, which flips
at 10:00Z (service_day), so a build on a US evening has the coming morning as
day 1. Three honesty tiers, labeled and separately logged:
  days 1-2   calibrated: sum of hourly P(sub-CAT-I) from the live 48 h forecast
             over the hours it covers (eh, with the hour it runs through in
             cov), plus a day-level chance of at least one such hour (p). The
             day-level chance is the hourly curve's any-hour product pushed
             through a map fitted on hindcasts (daycal.json, fit_daycal.py);
             the bare product ran about 1.7x high. Which map depends on the
             cycle hour, on how many dates ahead the day is, and on the
             field's area (pick_map); where no map passed its check for that
             combination the day keeps its forecast hours and carries no p.
  days 3-8   advisory: climatological hours/day x an NBE-ingredient factor
             (dew-point spread + wind from the extended blend; heuristic,
             UNFITTED — logged daily so it accrues its own verification)
  days 9-14  advisory: climatological hours/day x a CPC precip-outlook tilt
             (Above-normal moisture leans fog up, Below leans down; mild)
CPC is US-only; Canadian airports keep factor 1.0 there (climatology).

Inputs: forecast/out/current.json (engine, same CI run), NBE from the AWS
mirror, CPC shapefiles, climo.parquet, chase.json + airports.json, the
committed day map forecast/out/daycal.json (required: a missing or malformed
file fails the build rather than publishing the unmapped product again), the
committed scorecard forecast/out/shadow_report.json (each airport's standing
against the hourly forecast's public bar), and, with `--previous FILE`, the
planner payload that is live now.
Output: forecast/out/deploy.json (KV `deploy` via CI) and one deploylog per
build, forecast/out/deploylog/deploy_<cycle>_<built>.json.gz (R2 deploylogs/).

It runs on EVERY hourly forecast tick (forecast.yml), not once a day, and
every run writes a payload (assemble):
  - what this cycle supports is always fresh: eh, pk, win, ws, cov;
  - where this cycle hour has no checked day-level chance for a date, the last
    one published for the SAME DATE is carried from the previous payload, with
    the cycle it came from, for at most 24 h (carry_forward); else p is null;
  - a late cycle, a missing 26-48 h bulletin, or NBE / GEFS / CPC being
    unreachable is named in meta.gaps and in a ::warning line, never a reason
    to stop;
  - the build still fails, before anything is written, when its own input is
    broken: no current.json, an issuance with forecasts for under half the
    chase airports, a cycle too old to be this run's, a malformed daycal.json.
`--same-build A B` compares two payloads' meta.generated (the KV read-back in
forecast.yml) and exits 1 when they differ.
"""
import bisect
import gzip
import io
import json
import math
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# duckdb is imported inside main(): forecast/test_daycal.py and
# forecast/fit_daycal.py import the day-map helpers below, and the test must
# run on a bare standard-library Python.

sys.path.insert(0, str(Path(__file__).parent))
from parse_nbm import parse_collective  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "out"
BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
CPC = "https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst"


def fetch(url, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


# ---- day-level chance for the "cal" days (daycal.json, fitted by fit_daycal.py) ----
# Until 2026-11 the day-level "chance of at least one below-CAT-I hour" was
# published as 1 - prod(1 - p_hour): consecutive fog hours treated as
# independent coin flips. Scored on the 2023-25 hindcast it ran about 1.7x high
# (shown 0.5-0.6 -> 29% observed, 0.95-1.0 -> 66%) and on the 2025 holdout it
# was worse than the per-airport monthly climatology, while that number alone
# set the planner's GO word. The product still ranks days well, so it stays as
# the INPUT; what is published is the product pushed through a monotone map
# fitted on 2023-24 and checked on 2025. The hourly curve, eh, win and ws were
# sound and are untouched.
DAYCAL = OUT / "daycal.json"

# ---- the service day ----
# The planner is rebuilt on every hourly forecast tick, and the pilot reads it
# on a US evening (22Z-08Z) to decide where to be for the next morning's fog.
# Counted from "UTC today", a build at 02Z on the 25th called the 26th "day 1":
# the morning he was planning for (the 25th) was in neither day 1 nor day 2.
# So days are counted from a SERVICE day that flips at 10:00Z, where the daily
# 10:25Z bake always flipped it: every build from 10:00Z on date D to 09:59Z
# on D+1 has day 1 = D+1 and day 2 = D+2. Ten hours is local midnight in
# Hawaii and 5 am in the East, so a US evening never sees the flip.
SERVICE_DAY_OFFSET_H = 10


def service_day(now: datetime) -> date:
    """The date the planner's days are counted from: the UTC date of (now - 10 h). Day k = this + k days."""
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return (now - timedelta(hours=SERVICE_DAY_OFFSET_H)).date()


# ---- what a cycle forecasts for a date ----
# The engine merges NBH (hourly, leads 1-25) with NBS (leads 26-48, valid at
# the three-hourly synoptic hours). What a cycle says about a date therefore
# depends on the cycle hour h and on j = date - cycle date in days, and on
# nothing else — not on which planner day the date happens to be. Statistics,
# maps and checks are all keyed by (h, j) (fit_daycal.py, version 3).
def covered_hours(cyc: datetime, target: date) -> list[int]:
    """UTC hours of `target` that a complete NBH+NBS issuance from `cyc` forecasts.

    Leads start at 1, so for the cycle's own date this is never an hour at or
    before the cycle hour: a day under way is judged on what is left of it."""
    out = []
    for f in range(1, 49):
        v = cyc + timedelta(hours=f)
        if v.date() == target and (f <= 25 or v.hour % 3 == 0):
            out.append(v.hour)
    return out


def expected_used(cyc: datetime, target: date) -> int:
    """Hour-equivalents of `target` in a complete issuance: 1 per hourly lead, 3 per NBS point (day_stats' `used`)."""
    n = 0
    for f in range(1, 49):
        v = cyc + timedelta(hours=f)
        if v.date() == target and (f <= 25 or v.hour % 3 == 0):
            n += 1 if f <= 25 else 3
    return n


# A day-level number is only published when the hourly curve covers the date
# the way the fitted and checked ones did. An issuance without the 26-48 h
# bulletin leaves about half of tomorrow: eh is still summed from what is
# there, but no day-level chance is claimed from it. Three hour-equivalents
# is the old "20 of 23" for tomorrow seen from a 09Z, 12Z, 15Z, 18Z or 21Z
# cycle (a whole issuance holds 22 to 24 of tomorrow, by cycle hour).
MAX_MISSING_HOURS = 3

# Cycle hours are checked in eight buckets of three, named by their first hour,
# which is the hour the check sample was issued at. All three hours of a
# bucket see every date through the same NBS valid hour.
BUCKETS = [f"h{h:02d}" for h in range(0, 24, 3)]


def bucket_of(cycle_hour: int) -> str:
    return f"h{int(cycle_hour) // 3 * 3:02d}"


def slot_kind(cycle_hour: int, j: int) -> str | None:
    """What a cycle issued at cycle_hour forecasts for the date j days after its own (None: nothing checked).

    under_way   j=0, 00Z-08Z: the hours after the cycle (day 1 of an evening build)
    ahead       j=1, 09Z-23Z: the whole date, 1-39 h ahead (day 1 of a daytime build)
    evening     j=1, 00Z-08Z: the whole date, 16-47 h ahead (day 2 of an evening build)
    after_next  j=2, 15Z-23Z: the date through 15Z, 18Z or 21Z, three-hourly only
    partial     j=2, 09Z-14Z: the date through 09Z or 12Z
    A 09Z-23Z cycle's own date is the service day itself, never a planner day;
    j=2 from a 00Z-08Z cycle is at most three NBS points."""
    h = int(cycle_hour)
    if j == 0:
        return "under_way" if h <= 8 else None
    if j == 1:
        return "ahead" if h >= 9 else "evening"
    if j == 2:
        return "after_next" if h >= 15 else "partial" if h >= 9 else None
    return None


def slot_key(cycle_hour: int, j: int) -> str:
    return f"{bucket_of(cycle_hour)}/j{j}"


# every (bucket, j) a check exists for; daycal.json must carry exactly these
SLOT_KEYS = [f"{b}/j{j}" for b in BUCKETS for j in (0, 1, 2) if slot_kind(int(b[1:]), j)]

# The maps a slot kind may use (fit_daycal.py's pre-registration). The partial
# maps are named by the UTC hour the coverage runs through.
SLOT_MAPS = {"under_way": ("d1u", "d1"), "ahead": ("d1",), "evening": ("d2f",), "after_next": ("d2f",),
             "partial": ("d2h09", "d2h12")}
POOLED_MAPS = ("d1", "d1u", "d2f", "d2h09", "d2h12")


def day1_cycle_hours(daycal: dict) -> list[int]:
    """Cycle hours whose own build (the tick one hour later) publishes a day-level chance for day 1."""
    return [h for h in range(24) if daycal["slots"][slot_key(h, 1 if h >= 9 else 0)]["map"]]


def area_of(icao: str, a: dict | None, areas) -> str | None:
    """The area a field belongs to: the first of `areas` ([(name, definition)])
    that lists it. a = the field's airports.json entry. A definition names
    fields ("icaos"), US states ("st") or a country. fit_daycal.py judges each
    area with this same function, so a field is filed here exactly as it was
    scored there."""
    for name, d in areas:
        if icao in d.get("icaos", ()):
            return name
        # "CA" is both California and Canada's country code: states only match US fields
        if a and a.get("country") == "US" and a.get("st") in d.get("st", ()):
            return name
        if a and "country" in d and a.get("country") == d["country"]:
            return name
    return None


def load_daycal(path: Path = DAYCAL) -> dict:
    """Read and validate the committed day map.

    Raises, naming the file, when it is missing or malformed. A quiet fallback
    to the unmapped product would republish the very number this file corrects,
    and nothing downstream could tell.

    The checks past the breakpoints tie each map to the receipts that use it.
    A review (2026-11-24) hand-edited copies of this file and three edits
    loaded cleanly: one map carrying another's breakpoints, a map inflated
    1.4x, and a held map flipped to ship=true beside a receipt that said it
    failed. Version 3 (2026-11-25) is keyed by slot — a bucket of cycle hours
    and how many dates ahead — so the same ties are checked per slot, and per
    area wherever an area's own map is used: it must be that area's map of
    the slot's own family, in the form (floored or not) its
    leave-one-year-out record passed in, with a passing check at that bucket."""
    def bad(why: str) -> RuntimeError:
        return RuntimeError(
            f"{path}: {why}. The day-level map for the DEPLOY planner's days 1-2 is a required, committed "
            f"input (regenerate it with forecast/fit_daycal.py). deploy.json was not written.")
    try:
        dc = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise bad("file not found") from None
    except (OSError, ValueError) as e:
        raise bad(f"unreadable ({e})") from None
    if not isinstance(dc, dict) or dc.get("version") != 3 or not all(
            isinstance(dc.get(k), t) for k, t in (("maps", dict), ("slots", dict), ("areas", list), ("regional", dict))):
        raise bad("expected {\"version\": 3, \"maps\": {...}, \"slots\": {...}, \"areas\": [...], \"regional\": {...}}")
    if dc.get("service_day_offset_h") != SERVICE_DAY_OFFSET_H:
        raise bad(f"it was fitted for a service day that flips {dc.get('service_day_offset_h')!r} h after 00Z; "
                  f"this build flips it at {SERVICE_DAY_OFFSET_H} h")
    if not all(isinstance(a, dict) and isinstance(a.get("name"), str) for a in dc["areas"]):
        raise bad("'areas' is not a list of named area definitions")
    area_names = [a["name"] for a in dc["areas"]]
    maps = dc["maps"]

    def months(v) -> bool:
        return isinstance(v, list) and all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12 for m in v)

    def band(g) -> bool:
        """A group of published values as the fitter writes one: a count, and what it showed and saw when it has rows."""
        return isinstance(g, dict) and isinstance(g.get("n"), int) and not isinstance(g.get("n"), bool) and (
            g["n"] == 0 or all(isinstance(g.get(k), (int, float)) and not isinstance(g.get(k), bool)
                               for k in ("shown", "obs")))

    def rules_ok(c, what: str) -> None:
        """A receipt's verdict must be the one its own two rule results give."""
        if not isinstance(c, dict) or not isinstance(c.get("rule_a_skill_over_0"), bool) \
                or c.get("rule_b_ge50_within_10") not in (True, False, None) or not isinstance(c.get("pass"), bool):
            raise bad(f"{what} has no receipt with the two rule results and a verdict")
        if c["pass"] and not (c["rule_a_skill_over_0"] and c["rule_b_ge50_within_10"] is not False):
            raise bad(f"{what} says it passed but its own rule results say otherwise")

    def tied(c: dict, key: str, what: str) -> None:
        """The receipt must be for the breakpoints that are in the file now."""
        m = maps.get(key)
        try:
            same = (c.get("map") == key and abs(apply_daymap(m, 0.5) - c["map_050"]) <= 0.00051
                    and m["y"][-1] == c["map_top"])
        except (KeyError, TypeError):
            same = False
        if not same:
            raise bad(f"{what}: map {key!r} does not match the receipt (what a bare 0.50 maps to, or its top "
                      f"value): the breakpoints are not the ones that were tested")

    for key in list(POOLED_MAPS) + sorted(k for k in maps if k not in POOLED_MAPS):
        m = maps.get(key)
        if not isinstance(m, dict):
            raise bad(f"map {key!r} is missing")
        x, y = m.get("x"), m.get("y")
        ok = (isinstance(x, list) and isinstance(y, list) and len(x) == len(y) >= 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x + y)
              and all(0.0 <= v <= 1.0 for v in x + y)          # also rejects NaN
              and all(a < b for a, b in zip(x, x[1:]))
              and all(a <= b for a, b in zip(y, y[1:])))
        if not ok:
            raise bad(f"map {key!r} is not a set of rising breakpoints inside [0, 1]")
        if not isinstance(m.get("floor"), bool):
            raise bad(f"map {key!r} does not say whether it is floored at the peak hourly chance")
        if key in POOLED_MAPS:
            if m["floor"] is not True:
                raise bad(f"pooled map {key!r} must be floored at the peak hourly chance")
        else:
            fam, _, area = key.partition("@")
            if fam not in POOLED_MAPS or area not in area_names or m.get("family") != fam or m.get("area") != area:
                raise bad(f"map {key!r} is neither a pooled map nor '<pooled map>@<area>' for a listed area")

    if sorted(dc["slots"]) != sorted(SLOT_KEYS):
        raise bad(f"'slots' must hold exactly {SLOT_KEYS}")
    for sk in SLOT_KEYS:
        s = dc["slots"][sk]
        rep, j = int(sk[1:3]), int(sk[-1])
        kind = slot_kind(rep, j)
        if not isinstance(s, dict) or s.get("kind") != kind or not months(s.get("hold_months")) \
                or not isinstance(s.get("tried"), list) or not s["tried"] or not isinstance(s.get("areas"), dict):
            raise bad(f"slot {sk!r} is missing its kind, hold_months, tried or areas")
        # Every slot lists every area (fit_daycal.py always writes all of them).
        # A slot that had lost its Central Valley entry loaded cleanly and gave
        # those fields the pooled number with a clean receipt: KVIS 79% from a
        # 02Z cycle where its own map gives 23% (gate review, 2026-11-25).
        if sorted(s["areas"]) != sorted(area_names):
            raise bad(f"slot {sk!r} does not list exactly the areas in 'areas': a field of a missing area would "
                      f"get the pooled number with no record of its own")
        for c in s["tried"]:
            rules_ok(c, f"slot {sk!r}")
        key = s.get("map")
        if key is None:
            # a slot with no map beside a candidate that passed: someone withheld (or lost) a number by hand
            if any(c["pass"] for c in s["tried"]) or s.get("check") is not None:
                raise bad(f"slot {sk!r} publishes nothing but one of its checks passed")
            if any(a.get("use") != "pooled" for a in s["areas"].values() if isinstance(a, dict)):
                raise bad(f"slot {sk!r} publishes nothing but lists an area map")
        else:
            allowed = SLOT_MAPS[kind] if kind != "partial" else (f"d2h{rep:02d}",)
            c = s.get("check")
            if key not in allowed:
                raise bad(f"slot {sk!r} names map {key!r}, which is not a map for that reach")
            rules_ok(c, f"slot {sk!r}")
            if c["pass"] is not True or c not in s["tried"]:
                raise bad(f"slot {sk!r} publishes map {key!r} without a passing check behind it")
            tied(c, key, f"slot {sk!r}")
            # the three groups the screen quotes the check by: one that is gone
            # would go out as a receipt with nothing to say at that level
            if not all(band(c.get(b)) for b in BANDS3):
                raise bad(f"slot {sk!r}: its check has lost one of its groups {BANDS3}")
            # the season rule: a passing sample whose own Nov-Feb group missed
            # (day 2 through 12Z: shown 61%, observed 46%) must carry the hold
            g = (c.get("nov_feb") or {}).get("ge50") or {}
            if g.get("n", 0) >= BAND_MIN_N and abs(g["shown"] - g["obs"]) > BAND_MAX_GAP \
                    and not {11, 12, 1, 2} <= set(s["hold_months"]):
                raise bad(f"slot {sk!r}: its check missed in November-February but the slot is not held for "
                          f"those months")
            # a day under way is also scored from the build on (fit_daycal.py,
            # version 3 amendment 2); the screen prints the larger miss, so a
            # file without it would quietly print the smaller one
            ab = c.get("after_build")
            if kind == "under_way" and not (isinstance(ab, dict) and all(band(ab.get(b)) for b in BANDS3)):
                raise bad(f"slot {sk!r} publishes a day under way without its record from the build on (after_build)")
        for name, a in s["areas"].items():
            if name not in area_names or not isinstance(a, dict) or a.get("use") not in ("regional", "pooled") \
                    or not months(a.get("hold_months")):
                raise bad(f"slot {sk!r}, area {name!r}: expected a listed area with use 'regional' or 'pooled'")
            # each area's record on this bucket's own sample (amendment 3): the
            # key must be there, as a record or as null for an area with no rows.
            # A file from before it would print the fit hour's record from every
            # bucket again (West Coast "11 points high" where 00Z cycles ran 24).
            own = a.get("own", False)
            if key is not None and not (own is None or (isinstance(own, dict) and all(band(own.get(b)) for b in BANDS3)
                                                        and isinstance(own.get("by_peak_hour"), dict))):
                raise bad(f"slot {sk!r}, area {name!r}: no record on the bucket's own sample ('own')")
            if a["use"] != "regional":
                # An area kept on the pooled map carries the record of how that
                # map did there (its flag), wherever the map's family was judged
                # by area. Without it West Coast from a 15Z cycle went from flagged
                # (0.651 shown, 0.541 observed, n 296) to not flagged, on the
                # bucket's own 79 rows alone.
                fam = dc["regional"].get(key)
                rec = fam.get(name) if isinstance(fam, dict) else None
                if fam is not None and not (isinstance(rec, dict) and isinstance(rec.get("flag"), dict)):
                    raise bad(f"slot {sk!r}, area {name!r}: it stays on pooled map {key!r} but the record of how "
                              f"that map did there (regional[{key!r}][{name!r}].flag) is missing")
                continue
            rk = f"{key}@{name}"
            verdict = (dc["regional"].get(key) or {}).get(name) or {}
            if a.get("map") != rk or rk not in maps:
                raise bad(f"slot {sk!r}, area {name!r}: an area map must be {rk!r} and be in 'maps'")
            form = verdict.get("form")
            chosen = (verdict.get("forms") or {}).get(form) or {}
            if verdict.get("pass") is not True or form not in ("floored", "unfloored") \
                    or maps[rk]["floor"] != (form == "floored") or chosen.get("pass") is not True \
                    or not isinstance(chosen.get("rules"), dict) or any(v is False for v in chosen["rules"].values()):
                raise bad(f"slot {sk!r}, area {name!r}: the area map is used but its leave-one-year-out record "
                          f"does not say it passed in that form")
            # rule (f): an unfloored map takes the pooled map's 50%-and-over
            # values away, so it is only used where those were judged and ran
            # high (Mountain West had one on a Brier margin of 0.00005)
            if form == "unfloored" and chosen["rules"].get("f_unfloored_only_where_pooled_ran_high") is not True:
                raise bad(f"slot {sk!r}, area {name!r}: an unfloored area map is used but its record does not say "
                          f"the pooled map ran high there (regional rule f)")
            rules_ok(a.get("check"), f"slot {sk!r}, area {name!r}")
            if a["check"]["pass"] is not True:
                raise bad(f"slot {sk!r}, area {name!r}: the area map is used without a passing check at this bucket")
            tied(a["check"], rk, f"slot {sk!r}, area {name!r}")
    return dc


def area_defs(daycal: dict) -> list[tuple[str, dict]]:
    """daycal.json's areas as area_of() wants them: [(name, definition)], first match wins."""
    return [(a["name"], a) for a in daycal["areas"]]


def apply_daymap(m: dict, raw: float) -> float:
    """Linear interpolation between the map's sorted breakpoints, held flat beyond both ends."""
    x, y = m["x"], m["y"]
    if raw <= x[0]:
        return y[0]
    if raw >= x[-1]:
        return y[-1]
    i = bisect.bisect_right(x, raw)          # x[i-1] <= raw < x[i]
    return y[i - 1] + (y[i] - y[i - 1]) * (raw - x[i - 1]) / (x[i] - x[i - 1])


def day_stats(fhrs, psub, cyc: datetime, targets, through: dict | None = None) -> dict:
    """One airport's hourly below-CAT-I curve, rolled up for each date in `targets`.

    fhrs/psub are the engine's forecast hours and integer percents. Returns
    {date: {eh, noev, pk, used, last, win, ws}}: eh/win/ws exactly as they have
    always been built (every forecast hour of the date); noev, pk and used are
    the day-level statistic over the same hours, and last is the UTC hour the
    forecast of that date runs through. Forecast hours start at lead 1, so a
    date under way only ever counts the hours after the cycle.
    fit_daycal.py calls this same function over the hindcast, so each map is
    fitted on exactly what is fed to it here. `through` ({date: UTC hour}) cuts
    the statistic off early; only the fitter uses it, to make a 12Z cycle
    stand in for one that reaches less far."""
    peak = max((p for f, p in zip(fhrs, psub) if f >= 1), default=0)
    want = set(targets)
    out: dict[date, dict] = {}
    for fhr, p in zip(fhrs, psub):
        if fhr < 1:
            continue    # the cycle hour itself is an analysis, not a forecast (the engine never sends one)
        dt = cyc + timedelta(hours=fhr)
        d = dt.date()
        if d not in want:
            continue
        step = 1 if fhr <= 25 else 3
        s = out.setdefault(d, {"eh": 0.0, "noev": 1.0, "pk": None, "used": 0, "last": None, "win": 0, "ws": None})
        s["eh"] += step * p / 100.0
        if through is None or dt.hour <= through.get(d, 23):
            s["noev"] *= (1.0 - p / 100.0) ** step
            s["pk"] = p if s["pk"] is None else max(s["pk"], p)
            s["used"] += step
            s["last"] = dt.hour
        if p >= max(15, peak * 0.4):
            s["win"] += step
            if s["ws"] is None:
                s["ws"] = fhr  # first in-window forecast hour
    return out


def day_chance(m: dict, s: dict, floor: bool = True) -> float:
    """The published day-level chance for one day's statistic s (from day_stats).

    Never below the peak hourly chance: a day cannot be less likely to hold a
    fog hour than its likeliest hour, and pk is published beside p — without
    the floor the two could argue on screen. The checks in fit_daycal.py
    score this floored value, not the bare map.
    floor=False is for an area map that only passed its check without the
    floor (the peak hourly chance itself runs high there): the payload then
    says so, because p can sit below pk."""
    v = apply_daymap(m, 1.0 - s["noev"])
    return max(v, s["pk"] / 100.0) if floor else v


def pick_map(daycal: dict, cyc: datetime, target: date, area: str | None = None) -> dict:
    """Which map gives the day-level chance of `target` from this cycle, for a field in `area`.

    Returns {slot, kind, key, map, floor, regional, why}. why is None when a
    number is published, otherwise:
      "reach"   no check covers this cycle hour for that date: the cycle is
                from an earlier date than the build expects (the engine fell
                back a long way), or the 48 h barely touch the date.
      "hour"    the map for this cycle hour's bucket failed its check on real
                2025 cycles, or no map exists for the part of the date the
                forecast reaches (day 2 from a 09Z cycle is 00Z-09Z only).
      "season"  the date falls in months where the bucket's own >= 0.5 group
                missed (day 2 through 12Z in Nov-Feb 2025: shown 61%, observed 46%).
    A field whose area has its own map for this slot gets that map; where the
    area map did not pass, the field keeps the pooled map and its flag."""
    j = (target - cyc.date()).days
    kind = slot_kind(cyc.hour, j)
    out = {"slot": None, "kind": kind, "key": None, "map": None, "floor": True, "regional": False, "why": "reach"}
    if kind is None:
        return out
    out["slot"] = slot_key(cyc.hour, j)
    slot = daycal["slots"][out["slot"]]
    if slot["map"] is None:
        return dict(out, why="hour")
    if target.month in slot["hold_months"]:
        return dict(out, why="season")
    key = slot["map"]
    a = slot["areas"].get(area) if area else None
    if a and a["use"] == "regional" and target.month not in a["hold_months"]:
        key = a["map"]
        out["regional"] = True
    m = daycal["maps"][key]
    return dict(out, key=key, map=m, floor=m["floor"], why=None)


def cal_tier(current: dict, sd: date, daycal: dict, atlas: dict | None = None):
    """Every airport's days 1-2 from the engine's issuance, counted from service day `sd`.

    Returns (days, info). days[(icao, k)] = {eh, win, ws, pk, p, praw, ppool,
    cal, cov, used, why, area, map}:
      cal   the day has forecast hours and goes out on the "cal" tier: eh, pk,
            win and ws are the hourly forecast's, over the hours it covers.
            Until 2026-11-25 a day 2 without a published chance fell to
            climatology whole, the forecast's hours and window with it.
      cov   the UTC hour of that date the forecast runs through (23 or 21 for
            a whole day, 09 for day 2 seen from a 09Z cycle).
      p     the published day-level chance, or None; why says which:
            pick_map's reason, or "hours" when this airport's curve is missing
            more than MAX_MISSING_HOURS of what the cycle should cover.
      map   the map p came from ("d1", "d1u@Central Valley", ...).
      unc   the UTC hours of the date this cycle does not reach yet (a day 2
            seen in part), else an empty list.
      praw, ppool   the bare product and the pooled map's value, for the
            deploylog only (ppool differs from p where an area map was used).
    atlas ({icao: airports.json entry}) files each field under its area;
    without it every field gets the pooled map.
    info: the service day, the cycle hour and bucket, and per day its date,
    slot, map, why, and the hours covered."""
    cyc = datetime.fromisoformat(current["meta"]["cycle"].replace("Z", "+00:00"))
    targets = {sd + timedelta(days=k): k for k in (1, 2)}
    defs = area_defs(daycal)
    picks: dict[tuple, dict] = {}

    def pick(d: date, area: str | None) -> dict:
        if (d, area) not in picks:
            picks[(d, area)] = pick_map(daycal, cyc, d, area)
        return picks[(d, area)]

    need = {d: expected_used(cyc, d) - MAX_MISSING_HOURS for d in targets}
    # A day 2 seen in part (from a 09Z cycle: 00Z-09Z only) sums a third of its
    # hours into eh. The UTC hours the forecast does not reach yet are listed
    # here so the payload can carry their climatological hours beside eh (ehc).
    # That part of the date is three-hourly points, each summed as three hours
    # (day_stats), so the point at T stands for T, T+1, T+2 and the first hour
    # not reached is T+3; a forecast through 21Z is the whole date. Never for a
    # day under way: its missing hours are behind it.
    unreached = {d: list(range(covered_hours(cyc, d)[-1] + 3, 24))
                 if slot_kind(cyc.hour, (d - cyc.date()).days) in ("partial", "after_next") and covered_hours(cyc, d)
                 else [] for d in targets}
    days = {}
    for icao, f in current["airports"].items():
        area = area_of(icao, atlas.get(icao), defs) if atlas is not None else None
        for d, s in day_stats(f["fhrs"], [r[3] for r in f["p"]], cyc, targets).items():
            sel, pooled = pick(d, area), pick(d, None)
            full = s["used"] >= need[d]
            why = sel["why"] or (None if full else "hours")
            days[(icao, targets[d])] = {
                "eh": s["eh"], "win": s["win"], "ws": s["ws"], "pk": s["pk"],
                "p": round(day_chance(sel["map"], s, sel["floor"]), 3) if why is None else None,
                "praw": round(1.0 - s["noev"], 6) if s["pk"] is not None else None,
                "ppool": round(day_chance(pooled["map"], s), 3) if why is None else None,
                "cal": True, "cov": s["last"], "used": s["used"], "why": why, "area": area,
                "map": sel["key"] if why is None else None, "unc": unreached[d],
            }
    applied = {str(k): any(v["p"] is not None for (_, kk), v in days.items() if kk == k) for k in (1, 2)}
    info_days = {}
    for d, k in targets.items():
        sel = pick(d, None)
        hours = covered_hours(cyc, d)
        info_days[str(k)] = {
            "date": d.isoformat(), "j": (d - cyc.date()).days, "slot": sel["slot"], "kind": sel["kind"],
            "map": sel["key"], "why": None if applied[str(k)] else (sel["why"] or "hours"),
            "cover": {"from": hours[0], "through": hours[-1]} if hours else None,
            "under_way": sel["kind"] == "under_way", "expected_used": expected_used(cyc, d),
        }
    info = {"service_day": sd.isoformat(), "cycle_hour": cyc.hour, "bucket": bucket_of(cyc.hour),
            "days": info_days, "applied": applied, "why": {k: v["why"] for k, v in info_days.items()}}
    return days, info


# ---- an hourly build that never publishes less than the last one ----
# Until 2026-11-25 the planner was built once a day, by the first forecast tick
# at or after 10Z, and three stop-gaps guarded that one payload (bake_decision):
# "wait" failed the build so the next tick retried, "keep" wrote nothing so the
# live payload stayed, and --write-anyway overrode both. The pilot read it the
# evening before, 14 to 20 hours old, above an hourly curve that was fresh.
# Every hourly tick now rebuilds it and the rule is turned round: a build
# always publishes what its own cycle supports, and where this cycle hour has
# no checked day-level chance for a date it carries the last one published for
# that same date, saying which cycle it came from.
PAYLOAD_FORMAT = 2        # meta.format. A payload without it predates the hourly build: until 2026-11-25
#                           03Z its p on days 1-2 was the bare product, so nothing is ever carried from one.
CARRY_MAX_AGE_H = 24      # a carried chance, or a reused advisory day, is dropped once its source is this old
MAX_CYCLE_AGE_H = 8       # engine.latest_cycle looks back 6 h; an older current.json is not this run's
MIN_GEFS_MEMBERS = 8      # of 15: below this a lead's ensemble fractions are too coarse to feed the fitted tier
TIER_RANK = {"climo": 0, "cpc": 1, "nbe": 2, "fit": 3}   # the advisory tiers, least to most informed


def parse_utc(s) -> datetime | None:
    """A time out of a payload ("2026-11-24T09:00:00Z"), or None when it is not one."""
    if not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def utc_iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_issuance(current: dict, chase, now: datetime) -> datetime:
    """The cycle of an issuance this build may use; raises when the issuance is broken.

    An hourly build forgives everything upstream that can be late or missing
    (a bulletin, a cycle, NBE, GEFS, CPC): it publishes what it has and names
    the gap. It does not forgive its own input, because then the payload that
    is live is better than anything this run could write:
      - forecasts for under half the chase airports. From 2026-11-03 the
        engine published {"airports": {}} for 21 days and this script turned
        it into a planner on climatology, every run green;
      - a cycle more than MAX_CYCLE_AGE_H before the build, or after it. The
        engine never hands over either, so the file is a leftover from another
        run (or the clock is wrong) and its days would be labelled as today's.
    Raising stops main() before deploy.json is written, so CI uploads nothing."""
    meta = current.get("meta") if isinstance(current, dict) else None
    aps = current.get("airports") if isinstance(current, dict) else None
    cyc = parse_utc(meta.get("cycle")) if isinstance(meta, dict) else None
    if cyc is None or not isinstance(aps, dict):
        raise RuntimeError("current.json has no meta.cycle or no airports: the engine's issuance is malformed. "
                           "deploy.json was not written; the live planner payload stays.")
    got = sum(1 for i in chase if isinstance(aps.get(i), dict) and aps[i].get("fhrs"))
    floor = len(chase) // 2
    if got < floor:
        raise RuntimeError(f"current.json (cycle {meta['cycle']}) holds forecasts for {got} of {len(chase)} chase "
                           f"airports (floor {floor}). deploy.json was not written; the live planner payload stays.")
    age = (now - cyc).total_seconds() / 3600
    if not 0 <= age <= MAX_CYCLE_AGE_H:
        raise RuntimeError(f"current.json is from cycle {meta['cycle']}, {age:.1f} h before this build "
                           f"({now.strftime('%Y-%m-%d %H:%MZ')}); the engine looks back 6 h at most, so this is not "
                           f"this run's issuance. deploy.json was not written; the live planner payload stays.")
    return cyc


def read_previous(path, daycal: dict) -> tuple[dict | None, dict]:
    """The planner payload that is live now, if this build may carry from it.

    Returns (payload | None, note); note = {status, text, generated, cycle},
    status one of ok / absent / unreadable / malformed / old_format /
    other_daymap. Never raises: no previous payload only means nothing is
    carried, and the note goes out in meta.carry so the gap is on record.
    forecast.yml fetches it with `wrangler kv key get`, whose stdout has
    carried banner lines before the value on this project before, so the JSON
    is read from the first "{".
    old_format: a payload without meta.format. Until 2026-11-25 p on days 1-2
    was the bare any-hour product (about 1.7x high); carrying one of those
    would put the number this file exists to correct back on screen.
    other_daymap: built with another daycal.json. A carried chance must come
    from the day map that is committed now, or the receipts beside it lie."""
    def no(status: str, text: str) -> tuple[None, dict]:
        return None, {"status": status, "text": text, "generated": None, "cycle": None}

    if not path:
        return no("absent", "no previous planner payload was given to this build")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return no("absent", "no previous planner payload (first build, or the read from KV failed)")
    start = text.find("{")
    try:
        prev = json.JSONDecoder().raw_decode(text[start:])[0] if start >= 0 else None
    except ValueError:
        prev = None
    if prev is None:
        return no("unreadable", "the previous planner payload is not JSON")
    meta = prev.get("meta") if isinstance(prev, dict) else None
    if not isinstance(meta, dict) or not isinstance(prev.get("airports"), dict):
        return no("malformed", "the previous planner payload has no meta or no airports")
    if meta.get("format") != PAYLOAD_FORMAT:
        return no("old_format", f"the previous planner payload (generated {meta.get('generated')}) predates the "
                                f"hourly build (no meta.format {PAYLOAD_FORMAT})")
    try:
        date.fromisoformat(meta.get("day1"))
        dated = parse_utc(meta.get("generated")) is not None and parse_utc(meta.get("cycle")) is not None
    except (TypeError, ValueError):
        dated = False
    if not dated:
        return no("malformed", "the previous planner payload has no readable generated, cycle or day1")
    dc = meta.get("daycal")
    if not isinstance(dc, dict) or dc.get("version") != daycal.get("version") \
            or dc.get("map_file") != daycal.get("generated"):
        return no("other_daymap", f"the previous planner payload (generated {meta['generated']}) was built with "
                                  f"another day map than the one committed now")
    return prev, {"status": "ok", "generated": meta["generated"], "cycle": meta["cycle"],
                  "text": f"previous planner payload generated {meta['generated']} from cycle {meta['cycle']}"}


def carry_forward(airports: dict, cal: dict, sd: date, prev: dict | None, now: datetime) -> dict:
    """Days 1-2: where this cycle publishes no day-level chance, the last one published for the same date.

    airports is {icao: airport_entry(...)} and is changed in place: p is
    filled where a value is carried, and every entry gains pfrom, two values
    for days 1-2: the cycle a carried p came from, or None (p is this build's
    own, or there is none). cal is cal_tier's days. eh, pk, win, ws and cov
    are never touched: they are always this cycle's.

    A value is carried when the previous payload has the SAME DATE on its
    "cal" tier with a chance, the cycle that chance came from is under
    CARRY_MAX_AGE_H old at `now`, and the receipt that went out with it is in
    that payload (a number without its check is not carried). Dates are
    matched through the two payloads' day 1, never by position: at the 10:00Z
    flip yesterday's day 2 is today's day 1. A chain keeps the first cycle:
    carried at 16Z from 14Z and again at 17Z, it still says 14Z and still
    expires 24 h after 14Z.
    The carried number is never changed. Where it sits below this cycle's
    peak hourly chance at a field whose map is floored at the peak hour, the
    field is listed in below_peak: the hourly forecast has risen since, and
    no check covers a day-level number at this cycle hour, so the screen has
    to say that rather than print the two as if they agreed.

    Returns {"1": t, "2": t}; t = {cal, fresh, none, too_old,
    carried: {cycle: {n, below_peak, receipt}}}, counted over the airports
    whose day is on the "cal" tier in this build."""
    tallies = {str(k): {"cal": 0, "fresh": 0, "none": 0, "too_old": 0, "carried": {}} for k in (1, 2)}
    pmeta = prev["meta"] if prev else {}
    pday1 = date.fromisoformat(pmeta["day1"]) if prev else None
    receipts: dict[tuple, dict | None] = {}

    def receipt_for(kp: int, origin: str, d: date) -> dict | None:
        if (kp, origin) not in receipts:
            if origin == pmeta.get("cycle"):
                r = ((pmeta.get("daycal") or {}).get("days") or {}).get(str(kp))
            else:
                was = (((pmeta.get("days") or {}).get(str(kp)) or {}).get("chance") or {}).get("carried") or []
                r = next((c.get("receipt") for c in was if isinstance(c, dict) and c.get("cycle") == origin), None)
            ok = isinstance(r, dict) and r.get("date") == d.isoformat() and r.get("map") and "sample" in r
            # which fields' map is NOT floored at the peak hour (p may sit below pk there by design)
            floors = {i: a.get("floor") is not False for a in (r.get("areas") or []) if isinstance(a, dict)
                      and a.get("regional") for i in (a.get("icaos") or [])} if ok else {}
            receipts[(kp, origin)] = {"receipt": r, "floors": floors} if ok else None
        return receipts[(kp, origin)]

    for icao, a in airports.items():
        a["pfrom"] = [None, None]
        for k in (1, 2):
            c = cal.get((icao, k))
            if not c or not c["cal"]:
                continue                     # not a forecast day at this field: nothing fresh to carry a chance onto
            t = tallies[str(k)]
            t["cal"] += 1
            if c["p"] is not None:
                t["fresh"] += 1
                continue
            got = None
            b = prev["airports"].get(icao) if prev else None
            d = sd + timedelta(days=k)
            i = (d - pday1).days if prev else -1
            if i in (0, 1) and isinstance(b, dict):      # only a day that was itself on the previous build's days 1-2
                try:
                    tier, p, pf = b["tiers"][i], b["p"][i], (b.get("pfrom") or [None, None])[i]
                except (KeyError, IndexError, TypeError):
                    tier = p = pf = None
                origin = pf if isinstance(pf, str) else pmeta["cycle"]
                ot = parse_utc(origin)
                if tier == "cal" and isinstance(p, (int, float)) and not isinstance(p, bool) and 0.0 <= p <= 1.0 \
                        and ot is not None and ot <= now:
                    if now - ot >= timedelta(hours=CARRY_MAX_AGE_H):
                        t["too_old"] += 1
                    else:
                        r = receipt_for(i + 1, origin, d)
                        got = (float(p), origin, r) if r else None
            if got is None:
                t["none"] += 1
                continue
            p, origin, r = got
            a["p"][k - 1], a["pfrom"][k - 1] = p, origin
            e = t["carried"].setdefault(origin, {"n": 0, "below_peak": [], "receipt": r["receipt"]})
            e["n"] += 1
            if r["floors"].get(icao, True) and c["pk"] is not None and c["pk"] / 100.0 > p + 1e-9:
                e["below_peak"].append(icao)
    return tallies


def days_meta(cal_info: dict, cycle: str, airports: dict, tallies: dict) -> dict:
    """meta.days: for days 1 and 2, the date, what this cycle forecasts of it, and where the chance came from.

    cover is the UTC hours a whole issuance of this cycle forecasts for the
    date (null: none); short counts the "cal" airports whose own forecast
    stops before cover.through (their cov says where).
    chance.status:
      fresh    this build's own cycle published a day-level chance (cycle =
               meta.cycle). A few fields can still be carried or null (n).
      carried  it published none, and at least one field carries the last
               one published for this date; cycle is the newest cycle carried
               from, why the reason this cycle published none.
      none     no field has a day-level chance; why says why this cycle
               published none, too_old how many had one that had expired.
    chance.carried lists each cycle carried from, newest first, with the
    receipt that went out with its numbers: the screen prints THAT check
    beside a carried chance, never this build's own (which has none)."""
    out = {}
    for k in ("1", "2"):
        d, t, idx = cal_info["days"][k], tallies[k], int(k) - 1
        through = (d["cover"] or {}).get("through")
        short = sum(1 for a in airports.values() if a["tiers"][idx] == "cal" and through is not None
                    and (a["cov"][idx] is None or a["cov"][idx] < through))
        carried = sorted(({"cycle": o, "n": e["n"], "below_peak": sorted(e["below_peak"]), "receipt": e["receipt"]}
                          for o, e in t["carried"].items()), key=lambda e: e["cycle"], reverse=True)
        n_carried = sum(e["n"] for e in carried)
        out[k] = {"date": d["date"], "kind": d["kind"], "under_way": d["under_way"], "cycle": cycle,
                  "cover": d["cover"], "short": short,
                  "chance": {"status": "fresh" if t["fresh"] else "carried" if n_carried else "none",
                             "cycle": cycle if t["fresh"] else carried[0]["cycle"] if carried else None,
                             "why": None if t["fresh"] else cal_info["why"][k],
                             "n": {"cal": t["cal"], "fresh": t["fresh"], "carried": n_carried, "none": t["none"]},
                             "too_old": t["too_old"], "carried": carried}}
    return out


def issuance_gaps(current: dict, cyc: datetime, now: datetime) -> list[dict]:
    """What this issuance is missing that a normal tick has: [{code, hours, text}].

    late_cycle      the engine fell back: the cycle is older than the one a
                    tick at this time normally finds (the hour before).
    short_issuance  no forecast hour past 25: the 26-48 h bulletin did not
                    arrive (the engine fetches it once and goes on without).
    Neither stops the build. Until 2026-11-25 both did ("wait"), which was
    right for one build a day and would leave an hourly planner stale for as
    long as NOAA's mirror is slow."""
    gaps = []
    expected = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    late = int((expected - cyc).total_seconds() // 3600)
    if late >= 1:
        gaps.append({"code": "late_cycle", "hours": late,
                     "text": f"Built at {now.strftime('%H:%M')}Z from the {cyc.strftime('%H')}Z forecast: the "
                             f"{expected.strftime('%H')}Z forecast had not arrived."})
    longest = max((max(f.get("fhrs") or [0]) for f in current["airports"].values()), default=0)
    if longest <= 25:
        end = cyc + timedelta(hours=longest)
        # the panel prints this sentence as it is: its words ("forecast", a
        # weekday date), not the pipeline's ("cycle", "bulletin", an ISO date)
        gaps.append({"code": "short_issuance", "hours": longest,
                     "text": f"The {cyc.strftime('%H')}Z forecast arrived without its second day: it ends "
                             f"{end.strftime('%a')} {end.day} {end.strftime('%b')} at {end.strftime('%H')}Z."})
    return gaps


def reuse_advisory(airports: dict, sd: date, prev: dict | None, now: datetime, missing: list[str]) -> dict:
    """Days 3-14 when a live input (NBE, GEFS, CPC) could not be read this hour.

    The choice (2026-11-25): keep the previous payload's value for the same
    date where it is on a better tier than this build could make, for at most
    CARRY_MAX_AGE_H after the build that computed it; otherwise fall a tier,
    as the daily build always did. With one build a day a missed fetch cost
    one day of lower tiers and a person saw it. With 24 builds a day a single
    slow hour on NOAA's mirror would flip day 5 from the fitted tier to
    climatology and back an hour later, and those inputs only change four
    times a day anyway.
    airports is changed in place (eh, tiers, p of days 3-14 only; never a
    "cal" day). Dates are matched through the two payloads' day 1. Returns
    meta.advisory's {status, built, reused}:
      fresh     every input was read; nothing reused. built = this build.
      reused    some days carry the previous payload's values; built is when
                THOSE were computed (it survives a chain of reuses).
      degraded  an input was missing and nothing could be reused: the days
                are on whatever tier this build could make."""
    if not missing:
        return {"status": "fresh", "built": utc_iso(now), "reused": None}
    out = {"status": "degraded", "built": utc_iso(now), "reused": None}
    padv = (prev["meta"].get("advisory") or {}) if prev else {}
    built = parse_utc(padv.get("built"))
    if built is None or not timedelta(0) <= now - built < timedelta(hours=CARRY_MAX_AGE_H):
        return out
    shift = (sd + timedelta(days=1) - date.fromisoformat(prev["meta"]["day1"])).days
    n = fields = 0
    for icao, a in airports.items():
        b = prev["airports"].get(icao)
        took = False
        for idx in range(2, 14):
            i = idx + shift                  # the same date in the previous payload (one further on after a flip)
            if not 2 <= i < 14 or a["tiers"][idx] not in TIER_RANK:
                continue                     # a date the previous build did not have, or a forecast ("cal") day
            try:
                tier, eh, p = b["tiers"][i], b["eh"][i], b["p"][i]
            except (KeyError, IndexError, TypeError):
                continue
            if not isinstance(tier, str) or TIER_RANK.get(tier, -1) <= TIER_RANK[a["tiers"][idx]] \
                    or isinstance(eh, bool) or not isinstance(eh, (int, float)) \
                    or not (p is None or (isinstance(p, (int, float)) and not isinstance(p, bool))):
                continue
            a["eh"][idx], a["tiers"][idx], a["p"][idx] = eh, tier, p
            n += 1
            took = True
        fields += took
    if n:
        out = {"status": "reused", "built": padv["built"],
               "reused": {"values": n, "airports": fields, "from_generated": prev["meta"]["generated"]}}
    return out


# ---- each airport's standing against the hourly forecast's public bar ----
# The "cal" tier says the hourly forecast is calibrated. That was fitted and
# checked pooled over all airports; airport by airport the live record is
# scored monthly against a pre-registered bar (score_shadow.py, barcheck.yml)
# and about a third of the chase airports pass it, a third are scored and do
# not, and a third have had too little fog to say. The panel printed the same
# "calibrated" for all three (review 2026-11-24: of his top 15 fields by
# expected hours 7 passed, 5 failed, 3 had too few events).
SCORECARD = OUT / "shadow_report.json"
BAR_CODES = {
    "pass": "scored on enough forecast hours and fog, and the hourly forecast beat this airport's climatology on "
            "both measures",
    "fail": "scored on enough forecast hours and fog, and the hourly forecast did not beat this airport's "
            "climatology on one or both measures",
    "few": "too few scored forecast hours with fog at this airport, in the period scored so far, to say either "
           "way (or it is not in the scorecard)",
}
# The count behind "enough fog" is forecast-observation PAIRS, not fog hours:
# score_shadow.py adds one for every (cycle, lead) that forecast an hour which
# was then observed below 1 mile, so one fog hour seen by 15 forecasts counts
# 15 times. This text said "at least 10 observed hours" until a review
# (2026-11-25) counted them: 37,833 pairs were 2,158 distinct hours, and of 488
# airports over the floor 235 had one or two fog hours behind them.
BAR_PLAIN = ("An airport is scored once it has at least 3,000 forecast hours matched to an observation, at least "
             "10 of them for an hour observed with visibility below 1 mile. One fog hour counts once for every "
             "forecast that covered it, so 10 can be a single foggy morning. It passes when the hourly forecast's "
             "Brier score beats the airport's own climatology both for visibility below 1 mile and for below "
             "CAT I (visibility below 1/2 mile or ceiling below 200 ft).")


def load_scorecard(path: Path = SCORECARD) -> tuple[dict | None, str]:
    """The committed monthly scorecard, or (None, why). Never raises.

    The status is a label beside the forecast, not the forecast: a scorecard
    the monthly ritual left unreadable must not stop the hourly planner. It
    does have to hold a verdict for someone, though; a report whose pass and
    fail lists are both empty would label every field "few" and look fine."""
    try:
        r = json.loads(Path(path).read_text())
        bar = r["bar"]
        names = [x["icao"] for x in bar["pass"]] + [x["icao"] for x in bar["fail"]]
    except FileNotFoundError:
        return None, f"{path} not found"
    except (OSError, ValueError, KeyError, TypeError) as e:
        return None, f"{path} unreadable ({e!r})"
    if not names or not isinstance(bar.get("rule"), str) or not isinstance(r.get("generated"), str):
        return None, f"{path} has no scored airports, no rule or no date"
    return r, ""


def bar_block(report: dict | None, airports: dict, why: str = "") -> dict:
    """Adds `bar` to every airport entry (pass / fail / few, or None without a scorecard); returns meta.bar.

    counts is over every airport in the payload; counts_cal over those whose
    day 1 is on the "cal" tier, which is the group the panel calls calibrated."""
    if report is None:
        for a in airports.values():
            a["bar"] = None
        return {"available": False, "why": why}
    ok = {x["icao"] for x in report["bar"]["pass"]}
    bad = {x["icao"] for x in report["bar"]["fail"]}
    counts = {c: 0 for c in BAR_CODES}
    counts_cal = dict(counts)
    for icao, a in airports.items():
        a["bar"] = "pass" if icao in ok else "fail" if icao in bad else "few"
        counts[a["bar"]] += 1
        counts_cal[a["bar"]] += a["tiers"][0] == "cal"
    return {"available": True, "scored": report["generated"][:10], "generated": report["generated"],
            "runs": report.get("runs"), "rule": report["bar"]["rule"], "rule_plain": BAR_PLAIN,
            "codes": BAR_CODES, "counts": counts, "counts_cal": counts_cal,
            "source": "forecast/out/shadow_report.json (score_shadow.py, monthly)"}


def deploylog_name(cycle: str, now: datetime) -> str:
    """One deploylog per build, named by its cycle and its build time.

    It was one per day (deploy_<date>.json.gz), which 24 builds a day would
    have overwritten 23 times; the log is what the day-level chance of each
    cycle hour gets re-scored from. The build time is in the name as well
    because two builds can share a cycle: a person's dispatch beside the
    hourly tick, or the tick after a cycle that arrived late."""
    return f"deploy_{parse_utc(cycle).strftime('%Y%m%dT%H')}Z_{now.strftime('%Y%m%dT%H%M%S')}Z.json.gz"


def same_build(local_path, back_path) -> tuple[bool, str]:
    """Is the payload read back from KV the one this run just put there? (ok, what was found).

    forecast.yml calls this (--same-build) after the put. Cloudflare KV puts
    have failed without an error for this project before; the watchdog would
    only notice hours later, as an old planner."""
    try:
        mine = json.loads(Path(local_path).read_text())["meta"]["generated"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"the payload this run built is unreadable ({e!r})"
    try:
        text = Path(back_path).read_text(encoding="utf-8", errors="replace")
        live = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]["meta"]["generated"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return False, f"KV returned nothing readable for the key ({e!r}); this run built {mine}"
    return live == mine, f"KV holds a payload generated {live}; this run built {mine}"


def airport_entry(icao: str, sd: date, cal: dict, climo_win: dict, lower, fill=None) -> dict:
    """One airport's 14 planner days as they go into deploy.json.

    cal is cal_tier's days; lower(icao, dd, dte) -> (eh, tier, p) supplies a
    day the hourly forecast does not reach. Pulled out of main() on
    2026-11-24: the review put the bare product back into the payload with a
    one-line edit in main() (pdays.append(c["praw"])) and every test passed,
    because no test ran the assembly. praw and ppool never appear here.
    cov carries, for days 1-2, the UTC hour the forecast of that date runs
    through (null on a day with no forecast hours): eh, pk and win of a day 2
    seen from a 09Z cycle are 00Z-09Z only, and the screen has to say so.
    ehc, also for days 1-2: the climatological expected hours of the UTC hours
    a part-covered day 2 does not reach yet (fill(icao, date, hours)), null
    when nothing is missing or no fill was given. eh stays the forecast's own
    sum; a 7- or 14-day total adds ehc. Without it the 10:25Z build counted 31%
    of the West Coast's day-2 hours and 66% of the Southeast's (2026-11-24: KVIS
    day 2 read 0.75 h at 10:25Z and 2.70 h six hours later for the same date),
    and the ranking moved with the hour of the build."""
    eh, tiers, pdays, pks, wins, wss, cov, ehc = [], [], [], [], [], [], [], []
    for dd in range(1, 15):
        dte = sd + timedelta(days=dd)
        c = cal.get((icao, dd))
        c = c if c and c["cal"] else None
        cw = climo_win.get((icao, dte.month), 0)
        wins.append((c["win"] or cw) if c else cw)
        wss.append(c["ws"] if c else None)
        pks.append(c["pk"] if c else None)   # the peak hour is only printed beside a "cal" day
        if dd <= 2:
            cov.append(c["cov"] if c else None)
            extra = fill(icao, dte, c["unc"]) if c and fill and c.get("unc") else None
            ehc.append(round(extra, 2) if extra is not None else None)
        if c:
            eh.append(round(c["eh"], 2)); tiers.append("cal")
            pdays.append(c["p"])
            continue
        e, tier, p = lower(icao, dd, dte)
        eh.append(e); tiers.append(tier); pdays.append(p)
    return {"eh": eh, "tiers": tiers, "p": pdays, "win": wins, "ws": wss, "pk": pks, "cov": cov, "ehc": ehc}


# The ship rule's own floor and tolerance for judging a group of published
# values (fit_daycal.py: n >= 50, within 10 points). Used again wherever a
# band of an area's record is called "about right" or not.
BAND_MIN_N = 50
BAND_MAX_GAP = 0.10


def band_ok(g: dict | None) -> bool | None:
    """Did a band of published values verify within the rule's tolerance? None when it is too thin to say."""
    if not g or g.get("n", 0) < BAND_MIN_N:
        return None
    return abs(g["shown"] - g["obs"]) <= BAND_MAX_GAP


RECEIPT_KEYS = ("sample", "n", "n_clim", "cycles", "base", "brier", "brier_climo", "skill_pct", "raw_brier",
                "raw_skill_pct", "ge50", "b25_50", "ge75", "raw_ge50", "raw_b25_50",
                "rule_a_skill_over_0", "rule_b_ge50_within_10", "nov_feb",
                # a day under way only: the same chance against fog after the build is on screen
                "after_build")
BANDS3 = ("ge50", "b25_50", "ge75")
# The family whose fit hour looks at a date the way a slot kind does. Bucket
# h00 publishes a day under way through map d1 (the named fallback), whose own
# fit is 12Z cycles of the NEXT date; where the bucket's own sample is too thin
# to print, the under-way family's record stands in, never d1's.
KIND_FAMILY = {"under_way": "d1u"}


def area_record(flag: dict | None, own: dict | None) -> dict | None:
    """The pooled map's record in one area, for one bucket: the judged sample with the larger miss.

    flag is the fit hour's (fit_daycal.area_receipts: the test year and all
    years), own the bucket's own 2025 sample (fit_daycal.area_own). A sample
    is judged when its >= 0.5 group has BAND_MIN_N airport-days; ok is false
    when ANY judged sample misses by more than BAND_MAX_GAP. Until 2026-11-25
    only the fit hour's was read: a West Coast GO built from a 02Z cycle said
    "about 11 points high" (12Z cycles of the next date) while that bucket's
    own West Coast rows ran 24 points high. None when nothing can be judged."""
    cands = [(label, ((flag or {}).get("samples") or {}).get(label), None) for label in (flag or {}).get("judged") or []]
    if own:
        cands.append(("2025", own, own.get("sample")))
    cands = [c for c in cands if isinstance(c[1], dict) and (c[1].get("ge50") or {}).get("n", 0) >= BAND_MIN_N]
    if not cands:
        return None

    def gap(sample: dict) -> float:
        return sample["ge50"]["shown"] - sample["ge50"]["obs"]
    label, rec, words = max(cands, key=lambda c: abs(gap(c[1])))
    out = {"ok": all(abs(gap(c[1])) <= BAND_MAX_GAP for c in cands), "high": gap(rec) > 0, "years": label,
           **{b: dict(rec[b], ok=band_ok(rec[b])) for b in BANDS3}}
    if words:
        out["sample"] = words      # "30 real 00Z cycles of 2025, never fitted on": which cycles `years` means
    return out


def peak_hour_record(own: dict | None, family: dict | None) -> dict | None:
    """What days did by their peak hourly chance in one area, for an area map that is not held up to it.

    Per band, the larger of two samples of the same reach: the bucket's own
    2025 cycles, and the three years at the fit hour of the family that looks
    at a date the same way. Each band says which it is (sample)."""
    out = {}
    for b in BANDS3:
        cands = [dict(g, sample=words) for g, words in (
            (((own or {}).get("by_peak_hour") or {}).get(b), (own or {}).get("sample")),
            (((family or {}).get("by_peak_hour") or {}).get(b), (family or {}).get("years")))
            if isinstance(g, dict) and g.get("n")]
        out[b] = max(cands, key=lambda g: g["n"]) if cands else None
    return out if any(out.values()) else None


def day_receipt(daycal: dict, info_day: dict, cycle_hour: int, atlas: dict, chase, applied: bool = True) -> dict:
    """What one day's published chance rests on, for the screen.

    info_day is cal_tier's info["days"][k]. The numbers are those of the check
    on this build's own bucket of cycle hours: until 2026-11-24 a 09Z build
    printed the 12Z holdout's whatever cycle built it. `areas` lists every
    area that has something of its own to say, with its fields:
      regional true   the fields get the area's own map; ge50/b25_50/ge75 are
                      that map's leave-one-year-out record, top is the highest
                      value the map itself gives, and floor false means p can
                      sit below the peak hourly chance there;
      regional false  the fields keep the pooled map, and ok false means it
                      missed there by more than 10 points (the "not calibrated
                      for these fields" flag), with the area's own record.
    hourly, where present, is how the hourly forecast's expected hours and
    peak hour verified in that area. The screen never prints the pooled
    record beside a field whose own area says otherwise."""
    sk = info_day["slot"]
    slot = daycal["slots"].get(sk) if sk else None
    key = slot["map"] if slot else None
    cover = info_day.get("cover")
    out = {"date": info_day["date"], "slot": sk, "kind": info_day["kind"], "map": key, "shipped": key is not None,
           "cover": cover, "under_way": info_day["under_way"],
           # day 2 seen in part: p, pk and eh use the hours up to this UTC hour
           "cover_end_utc": cover["through"] if cover and info_day["kind"] in ("partial", "after_next") else None,
           "cycle_hour": cycle_hour, "checked_from": int(sk[1:3]) if applied and key else None}
    if not applied or key is None:
        # this build published no day-level chance for the day: no receipt goes
        # out with it, so no screen can print a check beside a number it does
        # not cover. The reason is in meta.daycal.why; where the reason is a
        # failed check, `failed` carries what that check found, so the screen
        # can say why in numbers ("ran 15 points high") instead of nothing.
        if slot and key is None:
            out["failed"] = [{k: c[k] for k in ("map", "sample", "n", "skill_pct", "ge50", "b25_50") if k in c}
                             for c in slot["tried"]]
        return out
    s, m = slot["check"], daycal["maps"][key]
    out.update({k: s[k] for k in RECEIPT_KEYS if k in s},
               top=m["y"][-1], raw_for_50=m.get("raw_for_50"), raw_050_maps_to=m.get("raw_050_maps_to"))
    # The top band is thin at any one cycle hour (46 airport-days from real
    # 09Z cycles) but said the same thing in every sample: shown 78%, observed
    # 62-68%. Below the rule's floor the group of the map's own fit hour
    # stands in, so a 78% on screen is never vouched for by the whole >= 0.5
    # group alone.
    home = (daycal["slots"].get(m.get("home") or "") or {}).get("check") or {}
    if (out.get("ge75") or {}).get("n", 0) < BAND_MIN_N and home.get("map") == key \
            and (home.get("ge75") or {}).get("n", 0) >= BAND_MIN_N:
        out["ge75"] = dict(home["ge75"], sample=home.get("sample"))
    members: dict[str, list] = {}
    for icao in chase:
        members.setdefault(area_of(icao, atlas.get(icao), area_defs(daycal)), []).append(icao)
    month = int(info_day["date"][5:7])
    verdicts = daycal["regional"].get(key) or {}
    # where the bucket's own sample is too thin, the record of the family that
    # looks at a date the same way (KIND_FAMILY), not blindly the map's own
    kin = KIND_FAMILY.get(info_day["kind"], key)
    kin_verdicts = daycal["regional"].get(kin) or {}
    kin_hourly = (daycal.get("hourly") or {}).get(kin) or {}
    hourly = {a["name"]: a for a in kin_hourly.get("areas", [])}
    areas = []
    for name, a in slot["areas"].items():
        v = verdicts.get(name) or {}
        own = a.get("own") if isinstance(a.get("own"), dict) else None
        e = {"name": name, "regional": a["use"] == "regional" and month not in a["hold_months"]}
        unfloored = False
        if e["regional"]:
            rm, oof = daycal["maps"][a["map"]], v["forms"][v["form"]]["oof"]
            unfloored = not rm["floor"]
            e.update(ok=True, high=None, years=v.get("years"), floor=rm["floor"], top=rm["y"][-1],
                     skill_pct=oof.get("skill_pct"), n=oof.get("n"),
                     **{b: dict(oof[b], ok=band_ok(oof[b])) for b in BANDS3})
            if unfloored:
                # p can sit below pk here: what days with a high peak hour did there
                e["by_peak_hour"] = peak_hour_record(own, kin_verdicts.get(name))
        else:
            rec = area_record(v.get("flag"), own)
            if rec:
                e.update(rec)
        # Expected hours against observed: this bucket's own cycles when they
        # hold enough fog to judge (100 observed hours), else the fit hour's of
        # the same reach. From 00Z cycles Central Valley expected hours ran
        # 1.87x the observed; the 12Z record printed beside them said 1.06.
        oeh = (own or {}).get("eh")
        if oeh and oeh.get("judged"):
            if oeh.get("runs") or unfloored:
                e["hourly"] = {"runs": oeh.get("runs"), "eh": oeh, "sample": own.get("sample")}
        else:
            h = hourly.get(name)
            if h and (h.get("runs") or unfloored):
                e["hourly"] = dict({k: h[k] for k in ("runs", "eh", "peak") if k in h}, sample=kin_hourly.get("sample"))
        if len(e) > 2:                      # an area with nothing of its own keeps the pooled receipt
            e["icaos"] = sorted(members.get(name, []))
            areas.append(e)
    out["areas"] = areas
    return out


def latest_nbe(stations):
    """(cycle, rows) of the newest NBE bulletin on the mirror, or (None, []) when none could be read.

    It raised when it found none, which stopped the whole build: right for
    one build a day with a person nearby, wrong for an hourly one, where days
    3-8 falling a tier is a gap to name (meta.gaps) and not a reason to
    withhold days 1-2."""
    now = datetime.now(timezone.utc)
    for back in range(1, 26):
        t = now - timedelta(hours=back)
        if t.hour not in (1, 7, 13, 19):  # NBE runs 4x daily
            continue
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        try:
            raw = fetch(f"{BASE}/blend.{d}/{h}/text/blend_nbetx.t{h}z")
        except Exception:
            continue
        if len(raw) > 5_000_000:
            p = HERE / "data" / "_nbe_run.txt"
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                return t.replace(minute=0, second=0, microsecond=0), parse_collective(p, stations)
            except Exception as e:
                print(f"  NBE {d}/{h}z unreadable: {e!r}")
    return None, []


def point_in_shape(lon, lat, shape) -> bool:
    pts = shape.points
    parts = list(shape.parts) + [len(pts)]
    inside = False
    for i in range(len(parts) - 1):
        ring = pts[parts[i]:parts[i + 1]]
        for j in range(len(ring)):
            x1, y1 = ring[j - 1]
            x2, y2 = ring[j]
            if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


CPC_PRODUCTS = ("610prcp", "814prcp")


def cpc_factors(airports):
    """({icao: [(start, end, factor)]}, products read) from the 6-10 and 8-14 day precip outlooks."""
    import shapefile
    out = {a: [] for a in airports}
    got = []
    for prod in CPC_PRODUCTS:
        try:
            z = zipfile.ZipFile(io.BytesIO(fetch(f"{CPC}/{prod}_latest.zip", 60)))
            tmp = HERE / "data" / f"_cpc_{prod}"
            tmp.mkdir(parents=True, exist_ok=True)
            z.extractall(tmp)
            shp = next(tmp.glob("*.shp"))
            r = shapefile.Reader(str(shp))
            fields = [f[0] for f in r.fields[1:]]
            for sh, rec in zip(r.shapes(), r.records()):
                d = dict(zip(fields, rec))
                cat, prob = d.get("Cat"), float(d.get("Prob") or 33)
                if cat not in ("Above", "Below"):
                    continue
                lean = (prob - 33.0) / 67.0
                f = 1.0 + 0.40 * lean if cat == "Above" else 1.0 - 0.30 * lean
                start = d["Start_Date"] if isinstance(d["Start_Date"], date) else date.fromisoformat(str(d["Start_Date"]))
                end = d["End_Date"] if isinstance(d["End_Date"], date) else date.fromisoformat(str(d["End_Date"]))
                for icao, (lon, lat) in airports.items():
                    if point_in_shape(lon, lat, sh):
                        out[icao].append((start, end, f))
            got.append(prod)
        except Exception as e:
            print(f"  CPC {prod} unavailable: {e}")
    return out, got


# ---- V3.2 fitted day-scale tier (dayscale.json, ship-gated) ----
def load_dayscale():
    try:
        ds = json.load(open(OUT / "dayscale.json"))
        return ds if ds.get("ship") else None
    except FileNotFoundError:
        return None


# GEFS forecast hours the fitted tier may read. fit_dayscale.py trained on 06Z
# cycles with f072..f192, so lead-day L was the ensemble valid at 06Z of the
# target date. f048 is here because a build between about 06Z and 10Z finds a
# 00Z cycle of the UTC date AFTER its service day: day 3 is then 48 h out.
GEFS_FHRS = (48, 72, 96, 120, 144, 168, 192)
GEFS_MAX_OFF_H = 12       # a lead further than this from 06Z of its date does not stand for that date


def gefs_leads(cycle: datetime, sd: date) -> dict[int, int]:
    """{planner day 3..8: GEFS forecast hour} — for each date, the lead valid nearest 06Z of that date.

    Until 2026-11-25 the leads were filed by position (f072 = day 3) while
    the days were counted from the service day. A build at 08:13Z on the 25th
    (service day the 24th, GEFS cycle the 25th 00Z) fed day 3, the 27th, the
    ensemble valid on the 28th 00Z: a day late, in every build between the
    00Z cycle arriving and the 10Z flip. Of 4,180 airport-days that moved the
    fitted chance by 10 points or more at 16 and across 50% at 7. Filed by
    valid time, the same lead feeds the same date whichever side of the flip
    the build is on. A tie (an 18Z cycle is 12 h from 06Z of two dates) goes
    to the lead valid ON the date."""
    out = {}
    for dd in range(3, 9):
        d = sd + timedelta(days=dd)
        want = datetime(d.year, d.month, d.day, 6, tzinfo=timezone.utc)
        best = min(GEFS_FHRS, key=lambda fh: (abs((cycle + timedelta(hours=fh) - want).total_seconds()),
                                              (cycle + timedelta(hours=fh)).date() != d))
        if abs((cycle + timedelta(hours=best) - want).total_seconds()) <= GEFS_MAX_OFF_H * 3600:
            out[dd] = best
    return out


def gefs_live(coords: dict, sd: date | None = None) -> tuple[dict, dict]:
    """({(icao, planner day): {gefs_f16, gefs_f80, gefs_lv}}, info) from the latest cycle on the mirror.

    sd is the service day the planner days are counted from (default: now's).
    info = {cycle, leads, members, hours}: the cycle used ("2026-11-24T00Z",
    None when none was reachable), the planner days that came back, how many
    of the 15 members each rests on, and the GEFS forecast hour each day read
    (gefs_leads). Never raises for something the mirror did:
    the probe and every index fetch used to (an HTTP 404 is an exception
    here), so a cycle still uploading or one missing member file stopped the
    whole planner. A lead with under MIN_GEFS_MEMBERS members is left out
    (the fitted tier's inputs are fractions of the ensemble) and that day
    falls to the NBE tier or to the previous build's value."""
    import numpy as np
    import eccodes as ec
    import concurrent.futures as cf
    MEMBERS = [f"gep{i:02d}" for i in range(1, 16)]
    info = {"cycle": None, "leads": [], "members": {}, "hours": {}}
    now = datetime.now(timezone.utc)
    sd = sd or service_day(now)
    cycle = None
    for back in range(6, 30, 6):
        t = (now - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        t = t.replace(hour=(t.hour // 6) * 6)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        try:
            probe = fetch(f"https://noaa-gefs-pds.s3.amazonaws.com/gefs.{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f072.idx", 30)
        except Exception:
            continue    # not on the mirror yet, or the mirror did not answer: try the cycle before
        if probe:
            cycle = (d, h)
            break
    if not cycle:
        return {}, info
    d, h = cycle
    info["cycle"] = f"{d[:4]}-{d[4:6]}-{d[6:]}T{h}Z"
    # planner day -> forecast hour, by valid time (gefs_leads); LEADS is the fetch list, "f072" -> the day it feeds
    by_day = gefs_leads(datetime(int(d[:4]), int(d[4:6]), int(d[6:]), int(h), tzinfo=timezone.utc), sd)
    LEADS = {f"f{fh:03d}": dd for dd, fh in by_day.items()}
    info["hours"] = {str(dd): fh for dd, fh in sorted(by_day.items())}
    icaos = list(coords)
    lats = np.array([coords[i][1] for i in icaos])
    lons = np.array([coords[i][0] % 360 for i in icaos])

    def one(mem, lead):
        base = f"https://noaa-gefs-pds.s3.amazonaws.com/gefs.{d}/{h}/atmos/pgrb2bp5/{mem}.t{h}z.pgrb2b.0p50.{lead}"
        try:
            idx = fetch(base + ".idx", 60)
        except Exception:
            return None
        if not idx:
            return None
        start = end = None
        lines = idx.decode().splitlines()
        for i, line in enumerate(lines):
            p = line.split(":")
            if len(p) > 4 and p[3] == "VIS" and p[4] == "surface":
                start = int(p[1])
                if i + 1 < len(lines):
                    end = int(lines[i + 1].split(":")[1]) - 1
                break
        if start is None:
            return None
        req = urllib.request.Request(base, headers={"Range": f"bytes={start}-{end if end else ''}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return (LEADS[lead], r.read())
        except Exception:
            return None

    grids: dict[int, list] = {}
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(lambda j: one(*j), [(m, l) for m in MEMBERS for l in LEADS]):
            if not res:
                continue
            lead, blob = res
            tmpf = HERE / "data" / "_gefs_live.grb2"
            tmpf.write_bytes(blob)
            try:
                with open(tmpf, "rb") as f:
                    gid = ec.codes_grib_new_from_file(f)
                lat1 = ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
                lon1 = ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
                dlat = ec.codes_get(gid, "jDirectionIncrementInDegrees")
                dlon = ec.codes_get(gid, "iDirectionIncrementInDegrees")
                ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
                vals = ec.codes_get_values(gid).reshape(nj, ni)
                rr = np.clip(np.round((lat1 - lats) / dlat).astype(int), 0, nj - 1)
                cc = np.clip(np.round((lons - lon1) / dlon).astype(int), 0, ni - 1)
                grids.setdefault(lead, []).append(vals[rr, cc])
                ec.codes_release(gid)
            except Exception:
                continue
    out = {}
    for lead, members in sorted(grids.items()):
        info["members"][str(lead)] = len(members)
        if len(members) < MIN_GEFS_MEMBERS:
            continue
        info["leads"].append(lead)
        m = np.stack(members)
        f16, f80 = (m < 1600).mean(axis=0), (m < 8000).mean(axis=0)
        lv = np.log1p(m / 1000.0).mean(axis=0)
        for j, icao in enumerate(icaos):
            out[(icao, lead)] = {"gefs_f16": float(f16[j]), "gefs_f80": float(f80[j]), "gefs_lv": float(lv[j]),
                                 "fh": by_day[lead]}
    print(f"GEFS live: cycle {d}/{h}z, {len(info['leads'])} leads, members per lead {info['members']}, "
          f"{len(out)} airport-leads")
    return out, info


def spread_wind_factor(spread_f, wsp_kt):
    if spread_f is None:
        f = 1.0
    elif spread_f <= 3: f = 1.6
    elif spread_f <= 6: f = 1.25
    elif spread_f <= 10: f = 1.0
    elif spread_f <= 15: f = 0.75
    else: f = 0.5
    if wsp_kt is not None:
        if wsp_kt <= 5: f *= 1.2
        elif wsp_kt <= 10: f *= 1.0
        elif wsp_kt <= 15: f *= 0.8
        else: f *= 0.6
    return min(max(f, 0.35), 2.0)


WHY_WORDS = {"hour": "the day map for this cycle hour failed its check, or the forecast reaches too little of that day",
             "reach": "no check covers that day from a cycle this old",
             "season": "its map missed its check in this season",
             "hours": "this cycle has too few of that day's forecast hours"}
ADVISORY_NAMES = {"nbe": "NBM extended guidance (days 3-8)", "gefs": "the GEFS ensemble (days 3-8, fitted tier)",
                  "cpc": "the CPC outlooks (days 9-14)"}
ADVISORY_RULE = (f"When NBM extended guidance, the GEFS ensemble or a CPC outlook cannot be read in a build, each of "
                 f"days 3-14 keeps the previous build's value for the same date where that is on a better tier, "
                 f"for at most {CARRY_MAX_AGE_H} h after the build that computed it; otherwise it falls a tier.")
CARRY_RULE = (f"Where this cycle hour has no checked day-level chance for a date, the last one published for the "
              f"same date and airport is carried, with the cycle it came from (pfrom), while that cycle is under "
              f"{CARRY_MAX_AGE_H} h old. The carried number is never changed. Expected hours, peak hour and "
              f"window are always this cycle's.")


def assemble(current: dict, now: datetime, daycal: dict, atlas: dict, chase, climo_win: dict, lower,
             prev: dict | None = None, prev_note: dict | None = None, scorecard: dict | None = None,
             scorecard_why: str = "no scorecard was given to this build", inputs: dict | None = None,
             dayscale_skill=None, fill=None) -> tuple[dict, dict, list[str]]:
    """One issuance -> (the deploy.json payload, its deploylog twin, warning lines).

    No clock, no network, no files: main() does those and hands everything
    in, so the whole assembly runs in a test (forecast/test_planner.py).
    `now` is the build time; the service day, every date and meta.generated
    come from it. chase is the airports to publish, lower(icao, dd, dte) ->
    (eh, tier, p) supplies a day the hourly forecast does not reach, prev /
    prev_note are read_previous's, inputs is what main() could read of the
    advisory sources: {"nbe": {ok, cycle, airports}, "gefs": {ok, cycle,
    leads, members} | None, "cpc": {ok, products}}. fill(icao, date, hours)
    -> climatological expected hours over those UTC hours of that date (or
    None): what airport_entry puts in ehc for a day 2 seen in part.

    Raises (nothing is written) only for a broken issuance: check_issuance,
    and a day 1 with forecast hours at under half the airports. Everything
    else that is late or missing becomes a line in meta.gaps and a warning."""
    cyc = check_issuance(current, chase, now)
    cycle = current["meta"]["cycle"]
    sd = service_day(now)
    cal, info = cal_tier(current, sd, daycal, atlas)
    n_cal1 = sum(1 for i in chase if cal.get((i, 1), {}).get("cal"))
    if n_cal1 < len(chase) // 2:
        raise RuntimeError(f"cycle {cycle} has forecast hours of day 1 ({info['days']['1']['date']}) at {n_cal1} of "
                           f"{len(chase)} chase airports (floor {len(chase) // 2}). deploy.json was not written; "
                           f"the live planner payload stays.")
    airports = {icao: airport_entry(icao, sd, cal, climo_win, lower, fill) for icao in chase}
    # the unmapped product of days 1-2, published or not, the pooled map's
    # value where an area map replaced it, and the map used: deploylog only,
    # so every slot (the ones that publish nothing too) can be re-scored
    # against what really happened
    logged = {icao: {k: [cal.get((icao, dd), {}).get(k) for dd in (1, 2)] for k in ("praw", "ppool", "map")}
              for icao in chase}
    tallies = carry_forward(airports, cal, sd, prev, now)
    inputs = inputs or {}
    missing = [k for k in ("nbe", "gefs", "cpc") if (inputs.get(k) or {}).get("ok") is False]
    advisory = dict(reuse_advisory(airports, sd, prev, now, missing), missing=missing, inputs=inputs,
                    rule=ADVISORY_RULE)
    bar = bar_block(scorecard, airports, scorecard_why)
    days = days_meta(info, cycle, airports, tallies)

    gaps = issuance_gaps(current, cyc, now)
    if missing:
        names = ", ".join(ADVISORY_NAMES[k] for k in missing)
        r = advisory["reused"]
        gaps.append({"code": "advisory", "missing": missing,
                     "text": f"Could not read {names} in this build: "
                             + (f"{r['values']} day values at {r['airports']} airports keep what the build of "
                                f"{advisory['built']} computed." if r else
                                f"those days are on a lower tier, and no build of the last {CARRY_MAX_AGE_H} h "
                                f"had better to keep.")})
    warnings = [g["text"] for g in gaps]
    c1 = days["1"]["chance"]
    if c1["status"] != "fresh":
        warnings.append(
            f"no checked day-level chance for day 1 ({days['1']['date']}) from the {cyc.strftime('%H')}Z cycle "
            f"({WHY_WORDS[c1['why']]}): "
            + (f"{c1['n']['carried']} fields carry the one from cycle {c1['cycle']}, {c1['n']['none']} have none."
               if c1["status"] == "carried" else "nothing could be carried; the planner shows NO VERDICT for day 1."))

    # what the day-level number rests on, for the screen: the receipts of the
    # map each day used, from the check on this build's own bucket of cycle
    # hours. `applied` false means this cycle published no chance that day
    # (`why` says why); a chance can still be on screen, carried from an
    # older cycle, and then ITS receipt is in meta.days[k].chance.carried.
    # map_file ties a payload to the daycal.json it was built with: the next
    # build only carries from a payload made with the same one.
    meta_daycal = {"version": daycal["version"], "map_file": daycal.get("generated"),
                   "fit": daycal.get("fit"), "test": daycal.get("test"),
                   "service_day": info["service_day"], "bucket": info["bucket"],
                   "applied": info["applied"], "why": info["why"], "cycle_hour": info["cycle_hour"],
                   "days": {k: day_receipt(daycal, info["days"][k], info["cycle_hour"], atlas, chase,
                                           info["applied"][k]) for k in ("1", "2")}}
    flip = datetime(sd.year, sd.month, sd.day, tzinfo=timezone.utc) + timedelta(days=1, hours=SERVICE_DAY_OFFSET_H)
    meta = {
        "format": PAYLOAD_FORMAT,
        "generated": utc_iso(now),
        "cycle": cycle,  # anchor for per-day ws forecast-hours; eh, pk, win, ws and cov are always from it
        # days are counted from the service day, which flips at 10:00Z: day k = service_day + k
        "service_day": sd.isoformat(),
        "day1": (sd + timedelta(days=1)).isoformat(),
        "next_flip": utc_iso(flip),   # a build after this moment has the next date as day 1
        "days": days,
        "carry": {"max_age_h": CARRY_MAX_AGE_H, "rule": CARRY_RULE,
                  "previous": prev_note or {"status": "absent", "generated": None, "cycle": None,
                                            "text": "no previous planner payload was given to this build"}},
        "gaps": gaps,
        "advisory": advisory,
        "bar": bar,
        "nbe_cycle": (inputs.get("nbe") or {}).get("cycle"),
        "daycal": meta_daycal,
        "tiers": {"cal": "calibrated hourly forecast over the hours it covers (cov); day-level chance, where one is "
                         "published, mapped on 2023-24 hindcasts and checked on 2025 cycles at the hour of the "
                         "cycle it came from (this build's, or pfrom when carried). ehc, on a day 2 the forecast "
                         "reaches only part of: the climatological expected hours of the hours past cov, to be "
                         "added to eh in a multi-day total",
                  "fit": "fitted day-scale model: GEFS ensemble vis + NBM-extended ingredients (holdout-verified)",
                  "nbe": "climatology x NBM-extended fog ingredients (advisory, unfitted)",
                  "cpc": "climatology x CPC moisture outlook (advisory)",
                  "climo": "climatology"},
        "dayscale_skill": dayscale_skill,
    }
    payload = {"meta": meta, "airports": airports}
    # praw, ppool and map ride in the log only: on the site either number
    # would be a second "chance" arguing with the published one
    log = {"meta": meta, "airports": {i: dict(a, **logged[i]) for i, a in airports.items()}}
    return payload, log, warnings


def main() -> None:
    argv = sys.argv[1:]
    if argv[:1] == ["--same-build"]:
        # the KV read-back in forecast.yml; the workflow retries and raises the error line itself
        ok, said = same_build(*argv[1:3])
        print(("planner payload verified in KV. " if ok else "KV read-back does not match. ") + said)
        sys.exit(0 if ok else 1)
    prev_path = argv[argv.index("--previous") + 1] if "--previous" in argv[:-1] else None

    import duckdb
    # first, before any fetch: without the day map there is nothing honest to
    # publish for days 1-2, and the run should go red in seconds, not minutes
    daycal = load_daycal()
    # committed copy (CI runners have no pipeline/out); identical content
    atlas = {a["icao"]: a for a in json.load(open(HERE.parent / "app" / "public" / "data" / "airports.json"))["airports"]}
    chase = json.load(open(HERE.parent / "app" / "public" / "data" / "chase.json"))["airports"]
    coords = {i: (atlas[i]["lon"], atlas[i]["lat"]) for i in chase if i in atlas}
    stations = set(coords)
    print(f"{len(coords)} chase airports")

    # second, still before any fetch: the engine's issuance from this same CI
    # run. A missing, empty or stale one stops here, and the payload that is
    # live stays (check_issuance).
    try:
        current = json.loads((OUT / "current.json").read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError(f"{OUT / 'current.json'} is missing or unreadable ({e!r}): the engine wrote no issuance "
                           f"in this run. deploy.json was not written; the live planner payload stays.") from None
    now = datetime.now(timezone.utc)
    check_issuance(current, coords, now)
    # every day below is counted from the service day (flips at 10:00Z), not
    # from the UTC date: `today` is the day before the planner's first day
    today = service_day(now)
    prev, prev_note = read_previous(prev_path, daycal)
    print(f"carry-forward source: {prev_note['text']}")
    if prev is None:
        print(f"::warning::deploy planner: nothing can be carried forward in this build: {prev_note['text']}")
    scorecard, scorecard_why = load_scorecard()
    if scorecard is None:
        print(f"::warning::deploy planner: no per-airport verification status in this payload: {scorecard_why}")

    # climatological sub-CAT-I hours/day by month, and the diurnal WINDOW
    # width (hours with rate >= half the peak hour) — how long fog typically
    # lasts once it's in, which bounds whether a distant base can reach it
    climo_day, climo_win = {}, {}
    con = duckdb.connect()
    for icao, mon, hrs in con.execute(
            f"SELECT icao, mon, sum(rsub) FROM '{OUT / 'climo.parquet'}' GROUP BY 1,2").fetchall():
        climo_day[(icao, mon)] = float(hrs)
    hourly = con.execute(
        f"SELECT icao, mon, hr, rsub FROM '{OUT / 'climo.parquet'}'").fetchall()
    by_im: dict[tuple, list] = {}
    climo_hr: dict[tuple, dict] = {}     # (icao, month) -> {UTC hour: climatological chance of that hour below CAT I}
    for icao, mon, hr, r in hourly:
        by_im.setdefault((icao, mon), []).append(float(r or 0))
        climo_hr.setdefault((icao, mon), {})[int(hr)] = float(r or 0)

    def fill(icao, dte, hours):
        """Climatological expected hours over `hours` (UTC) of dte: the same table the climatology tier sums over 24 h."""
        rates = climo_hr.get((icao, dte.month))
        return sum(rates.get(h, 0.0) for h in hours) if rates else None
    for k, rates in by_im.items():
        peak = max(rates)
        climo_win[k] = sum(1 for r in rates if r >= peak * 0.5) if peak > 0.001 else 0

    # The advisory sources of days 3-14. None of them may stop an hourly
    # build: each is read once, what could not be read is recorded in
    # `inputs`, and assemble() names it in meta.gaps and decides what those
    # days keep (reuse_advisory).
    # NBE per-day raw ingredients (fitted tier) + legacy factors (fallback)
    nbe_t, nbe_rows = latest_nbe(stations)
    nbe_f, nbe_raw = {}, {}
    for r in nbe_rows:
        dt = nbe_t + timedelta(hours=r["fhr"])
        dd = (dt.date() - today).days
        if not (3 <= dd <= 8):
            continue
        spread = (r["tmp"] - r["dpt"]) if r["tmp"] is not None and r["dpt"] is not None else None
        k = (r["icao"], dd)
        nbe_f.setdefault(k, []).append(spread_wind_factor(spread, r["wsp"]))
        if spread is not None:
            nbe_raw.setdefault(k, []).append((spread, r["wsp"] if r["wsp"] is not None else 5.0))
    n_nbe = len({k[0] for k in nbe_f})
    # under half the airports is "not read": a bulletin that parses to almost
    # nothing is a format change (Nov 2026: 0 airports for 21 days, and the
    # planner sat on climatology x CPC, all green). assemble() turns it into a
    # warning line; the watchdog's deploy-tier check is the alarm once the
    # last good build's values have aged out.
    inputs = {"nbe": {"ok": n_nbe >= len(stations) // 2, "airports": n_nbe,
                      "cycle": nbe_t.strftime("%Y-%m-%dT%H:%MZ") if nbe_t else None}}
    print(f"NBE cycle {nbe_t.isoformat() if nbe_t else 'not found'} — day-factors for {n_nbe} of {len(stations)} "
          f"airports" + ("" if inputs["nbe"]["ok"] else " (NBE bulletin format change or outage?)"))

    cpc, cpc_got = cpc_factors(coords)
    inputs["cpc"] = {"ok": len(cpc_got) == len(CPC_PRODUCTS), "products": cpc_got}
    dayscale = load_dayscale()
    gefs, gefs_info = gefs_live({i: coords[i] for i in coords}, today) if dayscale else ({}, None)
    # no dayscale.json that ships means no fitted tier at all: GEFS is then not an input, and not a gap
    inputs["gefs"] = dict(gefs_info, ok=len(gefs_info["leads"]) == 6) if gefs_info else None

    def sigmoid(z): return 1.0 / (1.0 + math.exp(-z))

    def lower(icao, dd, dte):
        """(eh, tier, p) for a day the calibrated tier does not cover."""
        base = climo_day.get((icao, dte.month), 0.0)
        clm = dayscale["climo_day"].get(icao, {}).get(str(dte.month)) if dayscale else None
        p_clim = clm[0] if clm else None
        # fitted day-scale tier: model P(any sub day) from live GEFS + NBE
        raw = nbe_raw.get((icao, dd))
        g = gefs.get((icao, dd))
        if dayscale and 3 <= dd <= 8 and raw and g and p_clim is not None:
            pc = min(max(p_clim, 1e-4), 1 - 1e-4)
            feats = {"clim_logit": math.log(pc / (1 - pc)),
                     "gefs_f16": g["gefs_f16"], "gefs_f80": g["gefs_f80"], "gefs_lv": g["gefs_lv"],
                     "nbe_spread": min(max(min(s for s, _ in raw), -5), 40),
                     "nbe_wsp": min(max(sum(w for _, w in raw) / len(raw), 0), 50),
                     # the lead the ensemble was read at, as fit_dayscale.py counted it (f072 = 3); a
                     # 48 h lead is nearer than anything it was fitted on and is entered as 3
                     "lead": max(3, g["fh"] // 24) if "fh" in g else dd}
            z = dayscale["intercept"] + sum(dayscale["coef"][k] * feats[k] for k in dayscale["features"])
            p_any = sigmoid(z)
            hrs_day = clm[1]
            ratio = min(max(p_any / pc, 0.3), 3.0)
            return round(hrs_day * ratio, 2), "fit", round(p_any, 3)
        f = 1.0
        tier = "climo"
        fs = nbe_f.get((icao, dd))
        if fs:
            f *= sum(fs) / len(fs); tier = "nbe"
        for (s, e, cf) in cpc.get(icao, []):
            if s <= dte <= e:
                f *= math.sqrt(cf) if tier == "nbe" else cf
                tier = tier if tier == "nbe" else "cpc"
        f = min(max(f, 0.3), 2.2)
        return round(base * f, 2), tier, (round(min(p_clim * f, 0.95), 3) if p_clim is not None else None)

    payload, log, warnings = assemble(
        current, now, daycal, atlas, coords, climo_win, lower, prev=prev, prev_note=prev_note,
        scorecard=scorecard, scorecard_why=scorecard_why, inputs=inputs,
        dayscale_skill=(dayscale or {}).get("test", {}).get("skill_pct"), fill=fill)
    meta, airports = payload["meta"], payload["airports"]
    for w in warnings:
        print(f"::warning::deploy planner: {w}")
    for k in ("1", "2"):
        d, c = meta["days"][k], meta["days"][k]["chance"]
        cover, n = d["cover"], c["n"]
        on_area = sum(1 for a in log["airports"].values() if "@" in (a["map"][int(k) - 1] or ""))
        print(f"day {k} = {d['date']} ({d['kind'] or 'no checked reach'}, cycle {meta['cycle']}"
              + (f", forecast {cover['from']:02d}Z-{cover['through']:02d}Z" if cover else ", no forecast hours")
              + f") on {n['cal']} airports — day-level chance {c['status']}"
              + (f" ({WHY_WORDS[c['why']]})" if c["why"] else "")
              + f": {n['fresh']} from this cycle ({on_area} on an area map), {n['carried']} carried"
              + (f" from {', '.join(e['cycle'] for e in c['carried'])}" if c["carried"] else "")
              + f", {n['none']} none" + (f" ({c['too_old']} had one older than {CARRY_MAX_AGE_H} h)" if c["too_old"] else "")
              + (f"; {d['short']} airports' forecast stops early" if d["short"] else ""))
    go = {k: sum(1 for a in log["airports"].values() if a["tiers"][0] == "cal" and (a[k][0] or 0) >= 0.5)
          for k in ("p", "ppool", "praw")}
    print(f"day 1 at or above 50%: {go['p']} fields (pooled map alone {go['ppool']}, bare product {go['praw']})")
    adv = meta["advisory"]
    print(f"days 3-14: {adv['status']}, computed {adv['built']}"
          + (f", {adv['reused']['values']} values reused" if adv["reused"] else "")
          + (f"; verification status: {meta['bar']['counts']}" if meta["bar"]["available"] else ""))

    logdir = OUT / "deploylog"
    logdir.mkdir(exist_ok=True)
    logname = deploylog_name(meta["cycle"], now)
    with gzip.open(logdir / logname, "wt") as f:
        json.dump(log, f, separators=(",", ":"))
    # deploy.json last and in one move: forecast.yml uploads it only when this
    # script exits 0, and a half-written file must never be what it finds
    # (the temporary copy sits in deploylog/, which git ignores)
    tmp = logdir / "deploy.json.tmp"
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(OUT / "deploy.json")
    size = (OUT / "deploy.json").stat().st_size // 1024
    print(f"deploy.json: {len(airports)} airports, {size}KB; deploylog {logname}")
    for probe in ("KVLD", "KMCN", "KSFO", "CYQI"):
        a = airports.get(probe)
        if a:
            print(f"  {probe}: 14d EH total {round(sum(a['eh']),1)}h  days: {[round(x,1) for x in a['eh']]}")


if __name__ == "__main__":
    main()
