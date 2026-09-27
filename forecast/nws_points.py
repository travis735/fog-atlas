#!/usr/bin/env python3
"""Resolve every US atlas airport to its NWS gridpoint public-forecast URL.

Run BY HAND, not in CI. The daily bake (build_pages.py via forecast/nws.py)
only READS the committed forecast/nws_points.json this script produces; the
lookup itself is one api.weather.gov/points call per airport (901 of them),
which is a one-off mapping that only changes when the atlas gains a US
airport or NWS redraws a grid. Re-run when either happens.

    python3 forecast/nws_points.py --out forecast/nws_points.json   # full rebuild
    python3 forecast/nws_points.py --missing                        # only airports absent
                                                                    # from the existing file; merged in

Shape (keys sorted by ICAO, one airport per line, nothing else in the file —
nws.load_points() iterates every key as an airport):

    {"KPDX": {"office": "PQR", "officeName": "Portland, OR", "gridX": 116, "gridY": 106,
              "forecast": "https://api.weather.gov/gridpoints/PQR/116,106/forecast",
              "tz": "America/Los_Angeles"}, ...}

"tz" is the ATLAS time zone (what the page already uses for local times);
when NWS's own timeZone disagrees it is recorded under "nwsTz". "cwa" is
recorded only when it differs from the grid office.

Politeness: 4 workers, 15 s timeout, one retry after 2 s on 5xx/timeouts,
and a hard budget (default 600 s) after which no new request starts. A 404
means the point is outside the NWS forecast grid (some Pacific territories);
those airports are skipped and listed, as are any that failed twice — run
again with --missing to pick them up.
"""
import argparse
import gzip
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).parent
PIPE_OUT = HERE.parent / "pipeline" / "out"
APP_DATA = HERE.parent / "app" / "public" / "data"
DEFAULT_OUT = HERE / "nws_points.json"

NWS = "https://api.weather.gov"
UA = "fogatlas.org fog bake (github.com/travis735/fog-atlas)"
# Accept-Encoding: gzip is LOAD-BEARING, not an optimisation: urllib always sends
# "Connection: close", and the api.weather.gov edge answers a close-connection
# HTTP/1.1 request for a body over ~9 KB (most /offices, some /gridpoints
# forecasts) by dropping the socket (RemoteDisconnected) unless the client
# accepts gzip. curl only "works" because it negotiates HTTP/2. Verified 2026-09-26.
HEADERS = {"User-Agent": UA, "Accept": "application/geo+json, application/ld+json, application/json",
           "Accept-Encoding": "gzip"}


def load_us_airports() -> list[dict]:
    """Atlas airports with country == "US" (pipeline output, else the committed app copy)."""
    for p in (PIPE_OUT / "app" / "airports.json", APP_DATA / "airports.json"):
        if p.exists():
            rows = json.load(open(p))["airports"]
            us = [a for a in rows if a.get("country") == "US" and a.get("icao")]
            print(f"airports: {len(us)} US of {len(rows)} in {p}")
            return us
    sys.exit("no airports.json found (pipeline/out/app or app/public/data)")


class Skip(Exception):
    """A lookup we will not retry: the reason is the message."""


def get_json(url: str, timeout_s=15, retries=1, pause_s=2.0):
    """GET url as JSON. Retries once on 5xx / timeouts / connection errors;
    raises Skip on 4xx (404 = outside the NWS grid) or after the last retry."""
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                body = r.read()
                if r.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Skip("404 outside NWS forecast grid")
            if 400 <= e.code < 500:
                raise Skip(f"HTTP {e.code}")
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(pause_s)
    raise Skip(f"failed after {retries + 1} tries ({last})")


def resolve_point(a: dict) -> dict:
    """One airport -> its nws_points entry (without officeName, filled later)."""
    url = f"{NWS}/points/{a['lat']:.4f},{a['lon']:.4f}"
    p = get_json(url).get("properties") or {}
    office, gx, gy, fc = p.get("gridId"), p.get("gridX"), p.get("gridY"), p.get("forecast")
    if not (office and fc and isinstance(gx, int) and isinstance(gy, int)):
        raise Skip(f"incomplete /points properties: gridId={office} gridX={gx} gridY={gy}")
    rec = {"office": office, "officeName": office, "gridX": gx, "gridY": gy, "forecast": fc, "tz": a["tz"]}
    if p.get("timeZone") and p["timeZone"] != a["tz"]:
        rec["nwsTz"] = p["timeZone"]
    if p.get("cwa") and p["cwa"] != office:
        rec["cwa"] = p["cwa"]
    return rec


def resolve_office_name(office: str) -> str:
    """'PQR' -> 'Portland, OR'. The /offices response carries `name` at the
    top level (it is JSON-LD, not GeoJSON); accept properties.name too. Five
    offices name themselves "NWS Phoenix" etc.; the page says "the NWS {name}
    forecast", so a leading "NWS " is dropped to avoid "the NWS NWS Phoenix"."""
    try:
        o = get_json(f"{NWS}/offices/{office}", retries=2)
        name = (o.get("name") or (o.get("properties") or {}).get("name") or office).strip()
        return re.sub(r"^NWS\s+", "", name) or office
    except Skip as e:
        print(f"  office {office}: {e} — using the code as its name (re-run with --missing to repair)")
        return office


def resolve_all(airports: list[dict], workers=4, budget_s=600) -> tuple[dict, dict]:
    """Points then offices. Returns (points {icao: rec}, skipped {icao: reason})."""
    t0 = time.monotonic()
    points, skipped = {}, {}
    pending = sorted(airports, key=lambda a: a["icao"])
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        # Hand out work in small waves so the budget check actually gates new requests.
        i = 0
        while i < len(pending) or futs:
            while i < len(pending) and len(futs) < workers * 2:
                if time.monotonic() - t0 > budget_s:
                    for a in pending[i:]:
                        skipped[a["icao"]] = "not attempted: time budget hit"
                    i = len(pending)
                    break
                a = pending[i]; i += 1
                futs[ex.submit(resolve_point, a)] = a["icao"]
            if not futs:
                break
            done = next(as_completed(list(futs)))
            icao = futs.pop(done)
            try:
                points[icao] = done.result()
            except Skip as e:
                skipped[icao] = str(e)
            except Exception as e:  # never let one airport kill the run
                skipped[icao] = f"unexpected {type(e).__name__}: {e}"
            n = len(points) + len(skipped)
            if n % 100 == 0:
                print(f"  points: {n}/{len(pending)} in {time.monotonic() - t0:.0f}s ({len(skipped)} skipped)")
    print(f"points: {len(points)} resolved, {len(skipped)} skipped in {time.monotonic() - t0:.0f}s")

    offices = sorted({r["office"] for r in points.values()})
    names = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for office, name in zip(offices, ex.map(resolve_office_name, offices)):
            names[office] = name
    for r in points.values():
        r["officeName"] = names.get(r["office"], r["office"])
    print(f"offices: {len(offices)} resolved in {time.monotonic() - t0:.0f}s total")
    return points, skipped


def dump(points: dict) -> str:
    """Sorted by ICAO, one compact airport per line — small diffs when re-run."""
    lines = [f"{json.dumps(k)}:{json.dumps(points[k], separators=(',', ':'), ensure_ascii=False)}"
             for k in sorted(points)]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output file (default {DEFAULT_OUT})")
    ap.add_argument("--missing", action="store_true",
                    help="only resolve airports absent from --out, merge into it")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--budget", type=float, default=600, help="seconds; no new request starts past this")
    args = ap.parse_args()

    airports = load_us_airports()
    existing = {}
    if args.missing:
        if args.out.exists():
            existing = json.load(open(args.out))
        before = len(airports)
        airports = [a for a in airports if a["icao"] not in existing]
        print(f"--missing: {len(existing)} already in {args.out}; {len(airports)} of {before} to resolve")
        if not airports:
            print("nothing to do")
            return

    points, skipped = resolve_all(airports, workers=args.workers, budget_s=args.budget)
    merged = {**existing, **points}
    # --missing also repairs entries whose office name never resolved (still the bare code).
    unnamed = sorted({r["office"] for r in existing.values() if r.get("officeName", r["office"]) == r["office"]})
    if unnamed:
        print(f"--missing: re-resolving {len(unnamed)} office names still bare: {' '.join(unnamed)}")
        names = {o: resolve_office_name(o) for o in unnamed}
        for r in merged.values():
            if r["office"] in names:
                r["officeName"] = names[r["office"]]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(dump(merged))

    print(f"\nwrote {len(merged)} airports -> {args.out}")
    print(f"offices: {len({r['office'] for r in merged.values()})}")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for ic in sorted(skipped):
            print(f"  {ic}: {skipped[ic]}")
    else:
        print("skipped: none")
    kpdx = merged.get("KPDX")
    print(f"KPDX: {json.dumps(kpdx)}")
    if "KPDX" in {a["icao"] for a in airports} or kpdx:
        ok = bool(kpdx) and (kpdx["office"], kpdx["gridX"], kpdx["gridY"]) == ("PQR", 116, 106)
        print("sanity KPDX -> PQR 116,106:", "OK" if ok else "FAILED")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
