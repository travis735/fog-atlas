#!/usr/bin/env python3
"""NWS public point forecast layer for the fog pages (US airports only).

Why this exists: on 2026-09-26 KPDX fogged in (1/4–1/2 SM) while our page
said there was no fog signal in "NWS guidance" — true of the National Blend
of Models, but the NWS Portland public forecast had said "Patchy fog before
8am" all along. Readers heard "NWS says no fog". So the bake now reads the
NWS point forecast for every US airport and quotes fog wording VERBATIM,
attributed to the issuing office — the same way the pages already quote
the airport's own TAF. Never paraphrased into a probability.

Pieces (mirrors awc_batch / awc_with_last_good / taf_summary in build_pages):
  load_points          committed ICAO -> gridpoint lookup (nws_points.json)
  nws_batch            time-boxed, polite (<= 4 workers) fetch of each
                       gridpoint forecast, trimmed to the first 6 periods
  nws_with_last_good   reuse the CI-cached snapshot for stations the fresh
                       pass missed, while it is younger than 30 h
  nws_summary          does the forecast mention fog inside the horizon?
                       -> verbatim mentions + attribution + local stamps
  period_label         "Tonight" (issued Friday evening) -> "Friday night":
                       relative period names resolved to the day they mean
  phrase_for_prose     "Patchy fog before 8am" + "Saturday"
                       -> "patchy fog before 8am Saturday"

Stdlib only (urllib + concurrent.futures) — the bake runs on a bare runner.
"""
import gzip
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
NWS_POINTS = HERE / "nws_points.json"        # committed: {ICAO: {office, officeName, gridX, gridY, forecast, tz}}
NWS_CACHE = HERE / "out" / "nws_cache.json"  # gitignored; restored by actions/cache
UA = "fogatlas.org fog bake (github.com/travis735/fog-atlas)"
RETRY_SLEEP_S = 2  # pause before the one retry of a 5xx/timeout (tests set 0)

PERIOD_KEYS = ("name", "startTime", "endTime", "isDaytime", "shortForecast", "detailedForecast")

# Structured so the groups tell us the kind: optional coverage word (the NWS
# grid's "areas"/"patchy"/"widespread" — distinct values, kept distinct), an
# optional "dense", an optional "freezing"/"ice" (winter wording; without it
# "Patchy freezing fog" fell through to bare "fog" and "Widespread dense
# freezing fog" lost its "dense"). Word boundaries keep "foggy" out;
# smoke/haze never match. Case-insensitive: shortForecast is Title Case
# ("Patchy Fog then Sunny"), detailedForecast is sentence case.
FOG_RE = re.compile(r"\b(?:(areas of|patchy|widespread)\s+)?(?:(dense)\s+)?(?:(?:freezing|ice)\s+)?fog\b",
                    re.IGNORECASE)

SEVERITY = {"dense fog": 4, "widespread fog": 3, "areas of fog": 2, "patchy fog": 1, "fog": 0, "no fog": -1}
# period names that are relative, not proper nouns — lower-cased inside prose
RELATIVE_PERIODS = {"today", "tonight", "this", "overnight", "late", "rest", "early"}
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _parse(t) -> datetime | None:
    """ISO with offset ("2026-09-26T18:00:00-07:00") or our "…HH:MMZ" stamps -> aware UTC."""
    if not t or not isinstance(t, str):
        return None
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _tz(tz) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(tz)
    except Exception:
        return timezone.utc


def load_points() -> dict:
    """The committed ICAO -> gridpoint table. {} when missing or unreadable —
    the bake then simply has no NWS point-forecast layer (never raises)."""
    if not NWS_POINTS.exists():
        print(f"  nws: {NWS_POINTS.name} missing — no NWS point forecasts this bake")
        return {}
    try:
        doc = json.load(open(NWS_POINTS))
        return doc if isinstance(doc, dict) else {}
    except Exception as e:
        print(f"  nws: {NWS_POINTS.name} unreadable ({e}) — no NWS point forecasts this bake")
        return {}


def _trim(doc: dict, fetched: str) -> dict:
    """Keep only what the page needs: issue stamps + the first 6 periods (NWS
    sends 14, ~7 days; the answer looks 48 h out) with their wording."""
    props = (doc or {}).get("properties") or {}
    periods = [{k: p.get(k) for k in PERIOD_KEYS} for p in (props.get("periods") or [])[:6]]
    return {"fetched": fetched,
            "updated": props.get("updateTime") or props.get("updated"),
            "generated": props.get("generatedAt"),
            "periods": periods}


# Accept-Encoding: gzip is LOAD-BEARING (same finding as nws_points.py,
# verified 2026-09-26): the api.weather.gov edge drops HTTP/1.1 requests that
# carry "Connection: close" (urllib always sends it) once the body is over
# ~9 KB — a gridpoint forecast is 12-14 KB — unless the client accepts gzip.
# Without it the bake fetched 0 of 901 forecasts; with it every one came back.
HEADERS = {"User-Agent": UA,
           "Accept": "application/geo+json, application/ld+json, application/json",
           "Accept-Encoding": "gzip"}


def _fetch_one(url: str, timeout_s: float) -> dict:
    """GET one gridpoint forecast. Retries a 5xx / timeout / network error
    once; a 4xx (bad gridpoint) fails immediately. Raises on final failure."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                body = r.read()
                enc = (getattr(r, "headers", None) or {}).get("Content-Encoding", "") or ""
                if enc.lower() == "gzip" or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                doc = json.loads(body)
            rec = _trim(doc, _stamp(datetime.now(timezone.utc)))
            if not rec["periods"]:
                # a 200 whose body is not a forecast (degraded / reshaped
                # response) must fail like a 5xx, so the station falls through
                # to the last-good snapshot instead of overwriting it with
                # nothing — the exact outage the cache exists for
                raise RuntimeError("no periods in response")
            return rec
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 1:
                raise
        except Exception:
            if attempt == 1:
                raise
        time.sleep(RETRY_SLEEP_S)
    raise RuntimeError("unreachable")


def nws_batch(points: dict, budget_s=300, workers=4, timeout_s=15) -> dict:
    """Latest NWS point forecast per US station, {icao: rec}. Best-effort AND
    time-bounded like awc_batch: a failed station just has no attributed NWS
    line (the climatology / guidance answer stands), and no NEW request
    starts after budget_s — in-flight ones finish. Never raises."""
    t0 = time.monotonic()
    ids = sorted(ic for ic, p in (points or {}).items() if isinstance(p, dict) and p.get("forecast"))
    out, errors, skipped = {}, [], 0
    if not ids:
        print(f"NWS: fetched 0 of 0 in 0s (0 failed)")
        return out

    def job(ic):
        if time.monotonic() - t0 > budget_s:
            return ic, None, "skipped"
        try:
            return ic, _fetch_one(points[ic]["forecast"], timeout_s), None
        except Exception as e:  # noqa: BLE001 — best-effort layer
            return ic, None, e

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for ic, rec, err in ex.map(job, ids):
                if rec is not None:
                    out[ic] = rec
                elif err == "skipped":
                    skipped += 1
                else:
                    errors.append((ic, err))
    except Exception as e:  # executor itself misbehaving — keep what we have
        print(f"  nws: batch aborted ({e}) — continuing with {len(out)} stations")
    for ic, err in errors[:3]:
        print(f"  nws {ic} failed: {err}")
    line = f"NWS: fetched {len(out)} of {len(ids)} in {time.monotonic() - t0:.0f}s ({len(errors)} failed)"
    if skipped:
        line += f" — time budget hit, {skipped} not attempted"
    print(line)
    if ids and 2 * len(out) < len(ids):   # fewer than half fetched
        # a run annotation: a 0-of-901 fetch is otherwise a plain green line,
        # and after two such bakes the 30 h last-good cache runs out silently
        print(f"::warning::NWS point forecasts: {line}")
    return out


def nws_with_last_good(fresh: dict, now_utc, max_age_h=30) -> dict:
    """api.weather.gov has bad hours; a bake during one must not strip the NWS
    line from 900 pages. Merge fresh over the last good snapshot (kept in the
    CI cache) for stations the fresh pass missed, only while that entry was
    fetched < max_age_h ago — the page stamps the issue time, so an older
    line is still honest. Then save the merge for the next bake."""
    cutoff = now_utc - timedelta(hours=max_age_h)
    merged = dict(fresh or {})
    used = dropped = 0
    if NWS_CACHE.exists():
        try:
            prev = json.load(open(NWS_CACHE))
            for ic, rec in (prev.get("nws") or {}).items():
                if ic in merged or not isinstance(rec, dict):
                    continue
                ts = _parse(rec.get("fetched"))
                if ts is None:
                    continue
                if ts >= cutoff:
                    merged[ic] = rec; used += 1
                else:
                    dropped += 1
            if used or dropped:
                print(f"  nws: reused {used} last-good entries from {prev.get('saved', '?')}"
                      + (f", dropped {dropped} older than {max_age_h} h" if dropped else ""))
        except Exception as e:
            print(f"  nws cache unreadable ({e})")
    try:
        NWS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NWS_CACHE.write_text(json.dumps({"saved": _stamp(now_utc), "nws": merged}, separators=(",", ":")))
    except Exception as e:
        print(f"  nws cache not saved ({e})")
    return merged


def _kind(text: str) -> str:
    """Most severe fog kind mentioned in one sentence, or None."""
    best = None
    for m in FOG_RE.finditer(text):
        cov = (m.group(1) or "").lower()
        k = ("dense fog" if m.group(2)
             else "widespread fog" if cov == "widespread"
             else "areas of fog" if cov == "areas of"
             else "patchy fog" if cov == "patchy"
             else "fog")
        if best is None or SEVERITY[k] > SEVERITY[best]:
            best = k
    return best


def _sentences(text: str) -> list[str]:
    return [s.strip().rstrip(".").strip() for s in re.split(r"\.\s+", text or "") if s.strip()]


def _mentions_in(text: str, period: dict) -> list[dict]:
    out = []
    for s in _sentences(text):
        k = _kind(s)
        if k:
            out.append({"period": period.get("name"), "start": period.get("startTime"),
                        "end": period.get("endTime"), "kind": k, "phrase": s})
    return out


def nws_summary(rec, tz, now_utc, horizon_h=48, points_entry=None) -> dict | None:
    """Reduce a point forecast to the one thing the page asks: does the NWS
    office's own forecast mention fog inside the horizon? Every mention is the
    NWS sentence verbatim (trailing period stripped), never a paraphrase.
    None when there is no forecast, or none of its periods reach the horizon."""
    if not rec or not rec.get("periods"):
        return None
    tz = _tz(tz)
    h_end = now_utc + timedelta(hours=horizon_h)
    considered = []
    for p in rec["periods"]:
        t0, t1 = _parse(p.get("startTime")), _parse(p.get("endTime"))
        if t0 is None or t1 is None:
            continue
        if t0 < h_end and t1 > now_utc:  # overlaps [now, now + horizon]
            considered.append((t1, p))
    if not considered:
        return None
    mentions = []
    for _, p in considered:
        found = _mentions_in(p.get("detailedForecast") or "", p)
        if not found:  # detailed missing (or silent on fog) — the short line still counts
            found = _mentions_in(p.get("shortForecast") or "", p)
        for m in found:  # "period" stays the NWS name verbatim; the label is what prose prints
            m["periodLabel"] = period_label(m["period"], m["start"], m["end"], tz)
        mentions.extend(found)
    verdict = max((m["kind"] for m in mentions), key=SEVERITY.__getitem__, default="no fog")

    last_end = max(t for t, _ in considered)
    issued = _parse(rec.get("updated")) or _parse(rec.get("generated")) or _parse(rec.get("fetched"))
    loc_issued = issued.astimezone(tz) if issued else None
    loc_end = last_end.astimezone(tz)
    pe = points_entry or {}
    return {"office": pe.get("office"), "officeName": pe.get("officeName"),
            "issued": _stamp(issued) if issued else None,
            "issuedLocal": (f"{loc_issued.strftime('%-I:%M %p').lower()} {loc_issued.strftime('%A')}"
                            if loc_issued else None),
            "issuedShort": (f"{loc_issued.strftime('%a')} {loc_issued.strftime('%-I:%M %p').lower()}"
                            if loc_issued else None),
            "fetched": rec.get("fetched"),
            "horizonEnd": _stamp(last_end),
            "horizonEndLocal": f"{loc_end.strftime('%A')} {loc_end.strftime('%-I %p').lower()}",
            "verdict": verdict, "mentions": mentions,
            "source": pe.get("forecast")}


def period_label(name, start, end, tz) -> str:
    """The NWS period name resolved to the day it means, in the airport's
    zone: "Tonight" -> "Friday night", "Overnight" -> "early Saturday",
    "Today" -> "Saturday", "This Afternoon" -> "Saturday afternoon";
    "Saturday Night" -> "Saturday night"; holidays pass through. Relative
    names are relative to the ISSUANCE, but the bake runs at ~10z (pre-dawn
    in every US zone west of Eastern) and the page is served for ~24 h, so
    a quoted "tonight" would read as the reader's coming night when the NWS
    meant the one already over — an NWS statement re-timed by our wording."""
    name = (name or "").strip()
    words = name.split()
    if not words:
        return name
    first = words[0].lower()
    t0, t1 = _parse(start), _parse(end)
    if first not in RELATIVE_PERIODS or t0 is None or t1 is None:
        return re.sub(r"\bNight$", "night", name)
    tz = _tz(tz)
    loc0, loc1 = t0.astimezone(tz), t1.astimezone(tz)
    rest = " ".join(words[1:]).lower()
    if first == "tonight":      # 18:00 (or later, when issued mid-evening) -> 06:00
        return f"{(loc0 - timedelta(hours=12)).strftime('%A')} night"
    if first == "overnight":    # issued after midnight: 0x:00 -> 06:00
        return f"early {loc1.strftime('%A')}"
    if first == "today" or (first == "rest" and rest.endswith("today")):
        return loc0.strftime("%A")
    if first == "this":         # "This Afternoon" -> "Saturday afternoon"
        return f"{loc0.strftime('%A')} {rest}".strip()
    return f"{first} {loc0.strftime('%A')}" + (f" {rest}" if rest else "")   # "late"/"early" X


def phrase_for_prose(m) -> str:
    """"Patchy fog before 8am" in period "Saturday" -> "patchy fog before 8am
    Saturday": first letter lower-cased (rest verbatim), then the period's
    resolved label ("Friday night", see period_label) unless the phrase
    already names it (or its weekday). A mention without a label (not from
    nws_summary) falls back to the raw name, lower-cased when relative."""
    phrase = (m.get("phrase") or "").strip().rstrip(".").strip()
    if phrase:
        phrase = phrase[0].lower() + phrase[1:]
    raw = (m.get("period") or "").strip()
    label = (m.get("periodLabel") or "").strip()
    period = label or raw
    if not period:
        return phrase
    if not phrase:
        return period
    low = phrase.lower()
    for name in {period, raw} - {""}:
        if re.search(r"\b" + re.escape(name.lower()) + r"\b", low):
            return phrase
    first = period.split()[0].lower()
    if first in WEEKDAYS and re.search(r"\b" + first + r"\b", low):
        return phrase
    if not label and first in RELATIVE_PERIODS:   # a raw "Tonight"; a label ("early Sunday") is already prose
        period = period.lower()
    return f"{phrase} {period}"


if __name__ == "__main__":  # smoke: fetch a few points, print their summaries
    import sys
    pts = load_points()
    pick = [i for i in sys.argv[1:] if i in pts] or sorted(pts)[:3]
    recs = nws_batch({i: pts[i] for i in pick}, budget_s=60)
    now = datetime.now(timezone.utc)
    for ic in pick:
        s = nws_summary(recs.get(ic), pts[ic].get("tz", "UTC"), now, points_entry=pts[ic])
        print(ic, json.dumps(s, indent=1) if s else "no forecast")
