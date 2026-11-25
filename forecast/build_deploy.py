#!/usr/bin/env python3
"""Build deploy.json — per-airport expected chaseable hours for the next 14 days.

The DEPLOY planner's data. Three honesty tiers, labeled and separately logged:
  days 1-2   calibrated: sum of hourly P(sub-CAT-I) from the live 48 h forecast
             (eh), plus a day-level chance of at least one such hour (p). The
             day-level chance is the hourly curve's any-hour product pushed
             through a map fitted on hindcasts (daycal.json, fit_daycal.py);
             the bare product ran about 1.7x high. A map is only used from the
             cycle hours and in the months it passed its check for
             (map_withheld): day 1 then carries no p, day 2 is not "cal".
  days 3-8   advisory: climatological hours/day x an NBE-ingredient factor
             (dew-point spread + wind from the extended blend; heuristic,
             UNFITTED — logged daily so it accrues its own verification)
  days 9-14  advisory: climatological hours/day x a CPC precip-outlook tilt
             (Above-normal moisture leans fog up, Below leans down; mild)
CPC is US-only; Canadian airports keep factor 1.0 there (climatology).

Inputs: forecast/out/current.json (engine, same CI run), NBE from the AWS
mirror, CPC shapefiles, climo.parquet, chase.json + airports.json, and the
committed day map forecast/out/daycal.json (required: a missing or malformed
file fails the build rather than publishing the unmapped product again).
Output: forecast/out/deploy.json (KV `deploy` + R2 deploylogs/ via CI).

Not every run writes it (bake_decision): a build from a cycle hour the day map
is not checked on leaves the live payload alone (a redeploy on a US evening
still ships the site's code), and a bake that could not make tomorrow's
day-level chance for want of a bulletin fails so the next hourly run retries.
`--write-anyway` writes the no-verdict payload regardless, for a local look.
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
# Day 2 is only partly inside a 48 h forecast: a cycle issued at hour h reaches
# h UTC of day 2 (the 10:25Z bake's 09Z cycle sees 00Z-09Z of it). The claim on
# screen is about the whole day, so there is one map per coverage end hour,
# each fitted against the whole-day outcome. The NBS rows that cover day 2 are
# valid at 00/03/06/09/12 UTC; the 12Z hindcast cannot emulate coverage past
# 12Z, so later hours are left out of the day-2 statistic rather than fed to a
# map that never saw them.
DAY2_COVER = (0, 3, 6, 9, 12)
# A day-level number is only published when the hourly curve covers the day
# the way the fitted ones did (23-24 hour-equivalents). An issuance without
# the 26-48 h bulletin leaves about half of tomorrow: eh is still summed from
# what is there, but no day-level chance is claimed from it.
DAY1_MIN_HOURS = 20


def day2_cover_hour(cycle_hour: int) -> int:
    """Coverage end hour (UTC, on day 2) of the day-2 statistic for a cycle issued at cycle_hour."""
    return max(c for c in DAY2_COVER if c <= min(int(cycle_hour), 12))


def daymap_key(dd: int, cover2: int) -> str:
    return "d1" if dd == 1 else f"d2h{cover2:02d}"


def map_cycle_hours(key: str) -> list[int]:
    """The cycle hours whose build would use this map."""
    return list(range(24)) if key == "d1" else [h for h in range(24) if daymap_key(2, day2_cover_hour(h)) == key]


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

    The checks past the breakpoints tie each map to its own receipt. A review
    (2026-11-24) hand-edited copies of this file and three edits loaded
    cleanly: one map carrying another's breakpoints, a map inflated 1.4x, and
    a held map flipped to ship=true beside a receipt that said it failed."""
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
    if not isinstance(dc, dict) or dc.get("version") != 2 or not isinstance(dc.get("maps"), dict):
        raise bad("expected {\"version\": 2, \"maps\": {...}}")
    for key in ["d1"] + [daymap_key(2, c) for c in DAY2_COVER]:
        m = dc["maps"].get(key)
        if not isinstance(m, dict) or not isinstance(m.get("ship"), bool):
            raise bad(f"map {key!r} is missing or has no true/false 'ship'")
        x, y = m.get("x"), m.get("y")
        ok = (isinstance(x, list) and isinstance(y, list) and len(x) == len(y) >= 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x + y)
              and all(0.0 <= v <= 1.0 for v in x + y)          # also rejects NaN
              and all(a < b for a, b in zip(x, x[1:]))
              and all(a <= b for a, b in zip(y, y[1:])))
        if not ok:
            raise bad(f"map {key!r} is not a set of rising breakpoints inside [0, 1]")
        # the receipt of the 12Z holdout, and the map it says it is a receipt for
        h = m.get("holdout")
        if not isinstance(h, dict) or not isinstance(h.get("rule_a_skill_over_0"), bool) \
                or h.get("rule_b_ge50_within_10") not in (True, False, None):
            raise bad(f"map {key!r} has no holdout receipt with the two rule results")
        if m["ship"] != (h["rule_a_skill_over_0"] and h["rule_b_ge50_within_10"] is not False):
            raise bad(f"map {key!r} says ship={m['ship']} but its own holdout receipt says otherwise")
        try:
            same = abs(apply_daymap(m, 0.5) - h["raw_050_maps_to"]) <= 0.00051 and y[-1] == h["top"]
        except (KeyError, TypeError):
            same = False
        if not same:
            raise bad(f"map {key!r} does not match its own receipt (what a bare 0.50 maps to, or its top value): "
                      f"the breakpoints are not the ones that were tested")
        # where it may be published from, and the check that says so
        pub, rfor, hours = m.get("publish_hours"), m.get("receipt_for_hour"), m.get("hours")
        if not (isinstance(pub, list) and isinstance(rfor, dict) and isinstance(hours, dict)
                and isinstance(m.get("hold_months"), list) and isinstance(m.get("areas"), list)
                and all(isinstance(v, int) and not isinstance(v, bool) for v in pub + m["hold_months"])):
            raise bad(f"map {key!r} is missing publish_hours, receipt_for_hour, hours, hold_months or areas")
        if pub and not m["ship"]:
            raise bad(f"map {key!r} did not ship but lists cycle hours to publish from")
        for hh in pub:
            src = rfor.get(str(hh))
            s = h if src == "12" else hours.get(src) if isinstance(src, str) else None
            if hh not in map_cycle_hours(key) or not isinstance(s, dict) or s.get("pass") is not True:
                raise bad(f"map {key!r} lists cycle hour {hh} to publish from without a passing check behind it")
    return dc


def apply_daymap(m: dict, raw: float) -> float:
    """Linear interpolation between the map's sorted breakpoints, held flat beyond both ends."""
    x, y = m["x"], m["y"]
    if raw <= x[0]:
        return y[0]
    if raw >= x[-1]:
        return y[-1]
    i = bisect.bisect_right(x, raw)          # x[i-1] <= raw < x[i]
    return y[i - 1] + (y[i] - y[i - 1]) * (raw - x[i - 1]) / (x[i] - x[i - 1])


def cal_day_stats(fhrs, psub, cyc: datetime, today: date, cover2: int) -> dict:
    """Days 1-2 of one airport's hourly below-CAT-I curve.

    fhrs/psub are the engine's forecast hours and integer percents. Returns
    {dd: {eh, noev, pk, used, win, ws}}: eh/win/ws exactly as they have always
    been built (every forecast hour of the day); noev, pk and used are the
    day-level statistic — every hour of day 1, hours up to cover2 UTC of
    day 2. fit_daycal.py calls this same function over the hindcast, so the
    map is fitted on exactly what is fed to it here."""
    peak = max(psub, default=0)
    out: dict[int, dict] = {}
    for fhr, p in zip(fhrs, psub):
        dt = cyc + timedelta(hours=fhr)
        dd = (dt.date() - today).days
        if not 1 <= dd <= 2:
            continue
        step = 1 if fhr <= 25 else 3
        s = out.setdefault(dd, {"eh": 0.0, "noev": 1.0, "pk": None, "used": 0, "win": 0, "ws": None})
        s["eh"] += step * p / 100.0
        if dd == 1 or dt.hour <= cover2:
            s["noev"] *= (1.0 - p / 100.0) ** step
            s["pk"] = p if s["pk"] is None else max(s["pk"], p)
            s["used"] += step
        if p >= max(15, peak * 0.4):
            s["win"] += step
            if s["ws"] is None:
                s["ws"] = fhr  # first in-window forecast hour
    return out


def day_chance(m: dict, s: dict) -> float:
    """The published day-level chance for one day's statistic s (from cal_day_stats).

    Never below the peak hourly chance: a day cannot be less likely to hold a
    fog hour than its likeliest hour, and pk is published beside p — without
    the floor the two could argue on screen. The holdout check in
    fit_daycal.py scores this floored value, not the bare map."""
    return max(apply_daymap(m, 1.0 - s["noev"]), s["pk"] / 100.0)


def map_withheld(m: dict, cyc: datetime, today: date, dd: int) -> str | None:
    """Why this map's day-level chance is not published by this build, or None when it is.

    "held"    the map did not pass its holdout check.
    "hour"    the cycle is not one the map was checked on. It was fitted on
              12Z cycles; scored on real 00Z cycles of 2025 the day-1 map ran
              15 points high at 50% or more (leads 24-47 h, nearly all
              three-hourly), and day 2 from real 09Z cycles missed the rule.
              A cycle from yesterday's UTC date (a 23Z cycle picked up after
              midnight) is the same unchecked reach.
    "season"  the target day falls in months where the map's own >= 0.5 group
              missed (day 2 in Nov-Feb 2025: shown 61%, observed 46%).
    """
    if not m["ship"]:
        return "held"
    if cyc.date() != today or cyc.hour not in m["publish_hours"]:
        return "hour"
    if (today + timedelta(days=dd)).month in m["hold_months"]:
        return "season"
    return None


def cal_tier(current: dict, today: date, daycal: dict):
    """Every airport's days 1-2 from the engine's issuance.

    Returns (days, info). days[(icao, dd)] = {eh, win, ws, pk, p, praw, cal}.
    cal says whether the day is published on the "cal" tier:
      day 1  always (as before). p is the mapped chance, or None when the
             day-1 map is withheld for this build (map_withheld) or the curve
             covers too little of the day.
      day 2  only when the map for this cycle's coverage is published by this
             build and every expected hour is present; otherwise the caller
             lets the day fall through to the lower tiers.
    praw is the unmapped product, kept for the deploylog only.
    info["why"][day] says why a day has no mapped chance: map_withheld's
    reason, or "hours" when the map would have been published but no airport
    had enough of the day (the 26-48 h bulletin did not arrive)."""
    cyc = datetime.fromisoformat(current["meta"]["cycle"].replace("Z", "+00:00"))
    cover2 = day2_cover_hour(cyc.hour)
    maps = {1: daycal["maps"]["d1"], 2: daycal["maps"][daymap_key(2, cover2)]}
    held = {dd: map_withheld(maps[dd], cyc, today, dd) for dd in (1, 2)}
    need2 = 3 * (cover2 // 3 + 1)
    days = {}
    for icao, f in current["airports"].items():
        for dd, s in cal_day_stats(f["fhrs"], [r[3] for r in f["p"]], cyc, today, cover2).items():
            m = maps[dd]
            full = s["used"] >= (DAY1_MIN_HOURS if dd == 1 else need2)
            mapped = held[dd] is None and full
            days[(icao, dd)] = {
                "eh": s["eh"], "win": s["win"], "ws": s["ws"], "pk": s["pk"],
                "p": round(day_chance(m, s), 3) if mapped else None,
                "praw": round(1.0 - s["noev"], 6) if s["pk"] is not None else None,
                "cal": dd == 1 or mapped,
            }
    applied = {str(dd): any(d["cal"] and d["p"] is not None for (_, k), d in days.items() if k == dd)
               for dd in (1, 2)}
    info = {"cover2": cover2, "key2": daymap_key(2, cover2), "cycle_hour": cyc.hour, "applied": applied,
            "why": {str(dd): None if applied[str(dd)] else (held[dd] or "hours") for dd in (1, 2)}}
    return days, info


# The daily bake is owed from 10Z (forecast.yml's bake gate): a build that runs
# before that hour was dispatched by a person.
BAKE_OWED_FROM_UTC = 10


def bake_decision(info: dict, cycle: str, now: datetime, d1_publish_hours: list[int]) -> tuple[str, str]:
    """What main() does with this issuance: ("write" | "wait" | "keep", why).

    write  publish deploy.json.
    wait   raise, so the bake fails before the site deploy and the bake gate
           tries again on the next hourly run. Two cases, both of which would
           otherwise leave every field with no day-level chance until
           tomorrow's bake while watchdog check 7 (which counts tiers) stayed
           green: the day-1 map was ready and no airport had enough of
           tomorrow to use it (the engine fetches the 26-48 h bulletin once,
           with no retry, and goes on without it); or the scheduled bake found
           only a cycle from before the first checked hour (the 09Z bulletin
           was late on the mirror and the engine fell back to 08Z).
    keep   do not write deploy.json at all, so CI uploads nothing and the live
           payload stays. This is a build from a cycle hour the day map is not
           checked on and that no later run today will improve: in practice a
           manual redeploy on a US evening (00Z-09Z). Its payload would carry
           no day-level chance, and its "day 1" would be the UTC date after
           the one the pilot is planning for (at 02Z on the 25th the build's
           tomorrow is the 26th; the morning bake's is the 25th). The morning
           bake's payload is the better one in both respects, and the site's
           code still deploys.
    A day-1 map that is held outright ("held") is written as it is: that is a
    committed state of daycal.json, not an accident of the hour."""
    why = info["why"]["1"]
    if why == "hours":
        return "wait", (f"cycle {cycle} has under {DAY1_MIN_HOURS} forecast hours of tomorrow at every airport (the "
                        f"26-48 h NBS bulletin did not arrive?). No day-level chance could be published for day 1, "
                        f"so deploy.json was not written; the next hourly run retries the bake.")
    if why == "hour":
        hh = info["cycle_hour"]
        if d1_publish_hours and now.hour >= BAKE_OWED_FROM_UTC and hh < min(d1_publish_hours) \
                and cycle[:10] == now.strftime("%Y-%m-%d"):
            return "wait", (f"the newest cycle on the mirror is {hh:02d}Z, before the first cycle hour the day map "
                            f"is checked on ({min(d1_publish_hours):02d}Z). deploy.json was not written; the next "
                            f"hourly run retries the bake with a later cycle.")
        return "keep", (f"built from the {hh:02d}Z cycle, which the day map is not checked on: this build would "
                        f"carry no day-level chance for tomorrow. deploy.json was NOT written and the live planner "
                        f"payload stays as it is until the next bake from a checked cycle.")
    return "write", ""


def airport_entry(icao: str, today: date, cal: dict, climo_win: dict, lower) -> dict:
    """One airport's 14 planner days as they go into deploy.json.

    cal is cal_tier's days; lower(icao, dd, dte) -> (eh, tier, p) supplies a
    day the calibrated tier does not cover. Pulled out of main() on
    2026-11-24: the review put the bare product back into the payload with a
    one-line edit in main() (pdays.append(c["praw"])) and every test passed,
    because no test ran the assembly. praw never appears here."""
    eh, tiers, pdays, pks, wins, wss = [], [], [], [], [], []
    for dd in range(1, 15):
        dte = today + timedelta(days=dd)
        c = cal.get((icao, dd))
        c = c if c and c["cal"] else None   # a day-2 the map cannot vouch for is not a "cal" day
        cw = climo_win.get((icao, dte.month), 0)
        wins.append((c["win"] or cw) if c else cw)
        wss.append(c["ws"] if c else None)
        pks.append(c["pk"] if c else None)   # the peak hour is only printed beside a "cal" day
        if c:
            eh.append(round(c["eh"], 2)); tiers.append("cal")
            pdays.append(c["p"])
            continue
        e, tier, p = lower(icao, dd, dte)
        eh.append(e); tiers.append(tier); pdays.append(p)
    return {"eh": eh, "tiers": tiers, "p": pdays, "win": wins, "ws": wss, "pk": pks}


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
                "rule_a_skill_over_0", "rule_b_ge50_within_10", "nov_feb")


def day_receipt(daycal: dict, key: str, cycle_hour: int, atlas: dict, chase, applied: bool = True) -> dict:
    """What one day's published chance rests on, for the screen.

    The numbers are those of the check nearest this build's own cycle hour:
    until 2026-11-24 a 09Z build printed the 12Z holdout's (day 2: 185
    airport-days at 59% shown / 54% observed, where real 09Z cycles gave 96 at
    59% / 49%). `areas` lists every judged area with its own record and its
    fields, so the screen never prints the pooled record beside a field whose
    own area says otherwise."""
    m = daycal["maps"][key]
    src = m["receipt_for_hour"].get(str(cycle_hour)) if applied else None
    out = {"map": key, "shipped": m["ship"], "cover_end_utc": m.get("cover_end_utc"),
           "cycle_hour": cycle_hour, "checked_from": None if src is None else int(src)}
    if src is None:
        # this build did not use the map: no receipt goes out with it, so no
        # screen can print a check beside a number the check does not cover
        return out
    s = m["hours"][src] if src != "12" else m["holdout"]
    h = m["holdout"]
    out.update({k: s[k] for k in RECEIPT_KEYS if k in s},
               top=h.get("top"), raw_for_50=h.get("raw_for_50"), raw_050_maps_to=h.get("raw_050_maps_to"))
    # The top band is thin at any one cycle hour (46 airport-days from real
    # 09Z cycles) but said the same thing in every sample: shown 78%, observed
    # 62-68%. Below the rule's floor the 12Z holdout's group stands in, so a
    # 78% on screen is never vouched for by the whole >= 0.5 group alone.
    if (out.get("ge75") or {}).get("n", 0) < BAND_MIN_N and (h.get("ge75") or {}).get("n", 0) >= BAND_MIN_N:
        out["ge75"] = dict(h["ge75"], sample=h.get("sample"))
    defs = [(a["name"], a) for a in m["areas"]]
    members: dict[str, list] = {}
    for icao in chase:
        members.setdefault(area_of(icao, atlas.get(icao), defs), []).append(icao)
    out["areas"] = [{"name": a["name"], "ok": a["ok"], "high": a["high"], "years": a["show"],
                     **{b: dict(a["samples"][a["show"]][b], ok=band_ok(a["samples"][a["show"]][b]))
                        for b in ("ge50", "b25_50", "ge75")},
                     "icaos": sorted(members.get(a["name"], []))}
                    for a in m["areas"] if a.get("show")]
    return out


def latest_nbe(stations):
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
            p.write_bytes(raw)
            return t.replace(minute=0, second=0, microsecond=0), parse_collective(p, stations)
    raise RuntimeError("no NBE cycle found")


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


def cpc_factors(airports):
    """(icao) -> {(start,end): factor} from 6-10 and 8-14 day precip outlooks."""
    import shapefile
    out = {a: [] for a in airports}
    for prod in ("610prcp", "814prcp"):
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
        except Exception as e:
            print(f"  CPC {prod} unavailable: {e}")
    return out


# ---- V3.2 fitted day-scale tier (dayscale.json, ship-gated) ----
def load_dayscale():
    try:
        ds = json.load(open(OUT / "dayscale.json"))
        return ds if ds.get("ship") else None
    except FileNotFoundError:
        return None


def gefs_live(coords: dict) -> dict:
    """(icao, lead_day) -> {gefs_f16, gefs_f80, gefs_lv} from the latest cycle."""
    import numpy as np
    import eccodes as ec
    import concurrent.futures as cf
    LEADS = {"f072": 3, "f096": 4, "f120": 5, "f144": 6, "f168": 7, "f192": 8}
    MEMBERS = [f"gep{i:02d}" for i in range(1, 16)]
    now = datetime.now(timezone.utc)
    cycle = None
    for back in range(6, 30, 6):
        t = (now - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        t = t.replace(hour=(t.hour // 6) * 6)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        probe = fetch(f"https://noaa-gefs-pds.s3.amazonaws.com/gefs.{d}/{h}/atmos/pgrb2bp5/gep01.t{h}z.pgrb2b.0p50.f072.idx", 30)
        if probe:
            cycle = (d, h)
            break
    if not cycle:
        return {}
    d, h = cycle
    icaos = list(coords)
    lats = np.array([coords[i][1] for i in icaos])
    lons = np.array([coords[i][0] % 360 for i in icaos])

    def one(mem, lead):
        base = f"https://noaa-gefs-pds.s3.amazonaws.com/gefs.{d}/{h}/atmos/pgrb2bp5/{mem}.t{h}z.pgrb2b.0p50.{lead}"
        idx = fetch(base + ".idx", 60)
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
    for lead, members in grids.items():
        m = np.stack(members)
        f16, f80 = (m < 1600).mean(axis=0), (m < 8000).mean(axis=0)
        lv = np.log1p(m / 1000.0).mean(axis=0)
        for j, icao in enumerate(icaos):
            out[(icao, lead)] = {"gefs_f16": float(f16[j]), "gefs_f80": float(f80[j]), "gefs_lv": float(lv[j])}
    print(f"GEFS live: cycle {d}/{h}z, {len(grids)} leads, {len(out)} airport-leads")
    return out


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


def main() -> None:
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
    for icao, mon, hr, r in hourly:
        by_im.setdefault((icao, mon), []).append(float(r or 0))
    for k, rates in by_im.items():
        peak = max(rates)
        climo_win[k] = sum(1 for r in rates if r >= peak * 0.5) if peak > 0.001 else 0

    # tier 1: calibrated 48 h from the engine's current.json (same CI run):
    # expected hours per day, and the mapped chance of at least one such hour
    current = json.loads((OUT / "current.json").read_text())
    today = datetime.now(timezone.utc).date()
    cal, cal_info = cal_tier(current, today, daycal)
    cover2 = cal_info["cover2"]
    n_cal = {dd: sum(1 for i in coords if cal.get((i, dd), {}).get("cal")) for dd in (1, 2)}
    go = {k: sum(1 for i in coords if (cal.get((i, 1), {}).get(k) or 0) >= 0.5) for k in ("p", "praw")}
    why_words = {"held": "its map did not pass the 2025 holdout check",
                 "hour": f"its map is not checked for a build from the {cal_info['cycle_hour']:02d}Z cycle",
                 "season": "its map missed its check in this season",
                 "hours": "this cycle has too few of that day's forecast hours"}
    why = cal_info["why"]
    what, said = bake_decision(cal_info, current["meta"]["cycle"], datetime.now(timezone.utc),
                               daycal["maps"]["d1"]["publish_hours"])
    if what == "wait":
        raise RuntimeError(said)
    if what == "keep" and "--write-anyway" not in sys.argv:
        print(f"::warning::deploy planner: {said}")
        return
    if why["1"]:
        # a held day-1 map, or --write-anyway (local inspection of the
        # no-verdict payload): the panel says NO VERDICT for tomorrow
        print(f"::warning::deploy planner: no day-level chance for day 1 — {why_words[why['1']]}. The planner "
              f"shows NO VERDICT for tomorrow while this payload is live.")
    print(f"day map: day 1 on {n_cal[1]} airports — "
          + (f"{go['p']} at or above 50% (the bare product said {go['praw']})" if not why["1"]
             else f"no day-level chance published: {why_words[why['1']]}"))
    print(f"day map: day 2 built from 00Z-{cover2:02d}Z (cycle {current['meta']['cycle']}), "
          + (f"calibrated on {n_cal[2]} airports" if n_cal[2] else
             f"not published as calibrated — {why_words[why['2']]}; day 2 falls to the climatology tiers"))

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
    print(f"NBE cycle {nbe_t.isoformat()} — day-factors for {n_nbe} airports")
    if n_nbe < len(stations) // 2:
        # the planner degrades to climatology x CPC rather than failing the
        # bake; say so where a human looks (run annotation) — the watchdog's
        # deploy-tier check is the alarm. Nov 2026: 0 airports for 21 days.
        print(f"::warning::deploy planner: NBE day-factors for only {n_nbe} of {len(stations)} airports — "
              f"days 3-8 fall back to the climatology/CPC tiers (NBE bulletin format change or outage?)")

    cpc = cpc_factors(coords)
    dayscale = load_dayscale()
    gefs = gefs_live({i: coords[i] for i in coords}) if dayscale else {}

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
                     "lead": dd}
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

    # the unmapped product of days 1-2, published or not: deploylog only, so
    # the map can be re-scored against what really happened
    praw = {icao: [cal.get((icao, dd), {}).get("praw") for dd in (1, 2)] for icao in coords}
    airports = {icao: airport_entry(icao, today, cal, climo_win, lower) for icao in coords}

    # what the day-level number rests on, for the screen: the receipts of the
    # map each day used, from the check nearest this build's cycle hour.
    # `applied` false on day 1 means p is null there (`why` says why); on
    # day 2 it means day 2 is not a "cal" day in this payload.
    meta_daycal = {"version": daycal["version"], "fit": daycal.get("fit"), "test": daycal.get("test"),
                   "applied": cal_info["applied"], "why": cal_info["why"], "cycle_hour": cal_info["cycle_hour"],
                   "days": {"1": day_receipt(daycal, "d1", cal_info["cycle_hour"], atlas, coords,
                                             cal_info["applied"]["1"]),
                            "2": day_receipt(daycal, cal_info["key2"], cal_info["cycle_hour"], atlas, coords,
                                             cal_info["applied"]["2"])}}

    meta = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cycle": current["meta"]["cycle"],  # anchor for per-day ws forecast-hours
        "day1": (today + timedelta(days=1)).isoformat(),
        "nbe_cycle": nbe_t.strftime("%Y-%m-%dT%H:%MZ"),
        "daycal": meta_daycal,
        "tiers": {"cal": "calibrated hourly forecast; day-level chance mapped on 2023-24 hindcasts, checked on 2025",
                  "fit": "fitted day-scale model: GEFS ensemble vis + NBM-extended ingredients (holdout-verified)",
                  "nbe": "climatology x NBM-extended fog ingredients (advisory, unfitted)",
                  "cpc": "climatology x CPC moisture outlook (advisory)",
                  "climo": "climatology"},
        "dayscale_skill": (load_dayscale() or {}).get("test", {}).get("skill_pct"),
    }
    out = {"meta": meta, "airports": airports}
    (OUT / "deploy.json").write_text(json.dumps(out, separators=(",", ":")))
    logdir = OUT / "deploylog"
    logdir.mkdir(exist_ok=True)
    with gzip.open(logdir / f"deploy_{today.isoformat()}.json.gz", "wt") as f:
        # praw rides in the log only: on the site it would be a second,
        # higher "chance" arguing with the published one
        json.dump({"meta": meta, "airports": {i: dict(a, praw=praw[i]) for i, a in airports.items()}},
                  f, separators=(",", ":"))
    size = (OUT / "deploy.json").stat().st_size // 1024
    print(f"deploy.json: {len(airports)} airports, {size}KB")
    for probe in ("KVLD", "KMCN", "KSFO", "CYQI"):
        a = airports.get(probe)
        if a:
            print(f"  {probe}: 14d EH total {round(sum(a['eh']),1)}h  days: {[round(x,1) for x in a['eh']]}")


if __name__ == "__main__":
    main()
