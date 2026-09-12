#!/usr/bin/env python3
"""Generate the static per-airport fog pages (/fog/{icao}/) + machine layer.

One page per atlas airport. Built for THREE readers at once:
  humans     the answer sentence, a facts box, the climatology story
  Google     query-shaped title/description, FAQPage/Dataset JSON-LD,
             daily-refreshed static answer + sitemap lastmod
  AI systems server-rendered plain-language answers (no JS required),
             a stable per-airport /fog/{icao}/data.json contract,
             /llms.txt site guide, explicit crawler welcome in robots.txt

The "tomorrow" answer is BAKED at build time from the newest forecast
issuance (forecast/out/current.json when running right after the engine,
else the live /api/forecast) — pages are rebuilt daily in CI so the static
HTML always carries a fresh answer for crawlers that never execute JS.

Shadow-mode honesty (unchanged law): pages never print model percentages
for an airport that hasn't cleared its verification bar — those get NWS
guidance wording; uncovered stations get climatology only.

Pages are DEPLOY-TIME ARTIFACTS (gitignored): every deploy path runs this
script first. All inputs fall back to committed copies so CI runners can
build without pipeline/out (same rule as build_chase/build_deploy).

Output: app/public/fog/{icao}/index.html + data.json, /fog/index.html,
/fog/_fog.js, /fog/scorecard/, sitemap.xml, robots.txt, llms.txt
"""
import csv
import json
import re
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
PIPE_OUT = HERE.parent / "pipeline" / "out"
APP_PUB = HERE.parent / "app" / "public"
APP_DATA = APP_PUB / "data"
FOG = APP_PUB / "fog"
SITE = "https://fogatlas.org"
# legacy fog-atlas.pages.dev visitors hop to the real domain (crawlers
# consolidate via the canonical tags; a host-based 301 isn't possible on
# pages.dev without metering every static request through Functions)
REDIRECT = ('<script>location.hostname.endsWith(".pages.dev")&&'
            'location.replace("https://fogatlas.org"+location.pathname+location.search+location.hash)</script>')
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

ALS_LABEL = {"ALSF2": "ALSF-2", "ALSF1": "ALSF-1", "MALSR": "MALSR", "SSALR": "SSALR",
             "MALSF": "MALSF", "MALS": "MALS", "SALS": "SALS", "SALSF": "SALSF",
             "ODALS": "ODALS", "RLLS": "RLLS", "OTHER": "other ALS"}

CSS = """
:root{--bg:#0b1016;--panel:#111823;--ink:#dce7f2;--dim:#8294a3;--accent:#9fd8ff;--hot:#c4eaff;--amber:#ffb347;--hair:#233240}
*{box-sizing:border-box;margin:0}body{background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,'Segoe UI',Roboto,Helvetica,sans-serif;padding:0 16px 60px}
main{max-width:680px;margin:0 auto}h1{font-size:1.35rem;letter-spacing:.02em;margin:26px 0 2px}h1 b{color:var(--hot)}
.sub{color:var(--dim);font-size:.85rem;margin-bottom:18px}
#answer{background:var(--panel);border:1px solid var(--hair);border-radius:12px;padding:16px 18px;font-size:1.05rem;margin:14px 0}
#answer b{color:var(--amber)}#answer .ok{color:#7fd49a}#answer .asof{display:block;color:var(--dim);font-size:.72rem;margin-top:8px}
#verdict{background:#0e1520;border:1px solid var(--hair);border-radius:10px;padding:10px 14px;font-size:.92rem;margin:10px 0}
#verdict b{color:var(--amber)}#verdict .ok{color:#7fd49a}
#strip{margin:14px 0 4px}#strip svg{width:100%;height:auto;display:block}
.striplab{color:var(--dim);font-size:.72rem;display:flex;justify-content:space-between}
.ctx{color:var(--dim);font-size:.85rem;margin:10px 0 26px}
h2{font-size:.8rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin:30px 0 8px;border-top:1px solid var(--hair);padding-top:22px}
table{border-collapse:collapse;width:100%;font-size:.82rem}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--hair)}th{color:var(--dim);font-weight:500}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:.88rem;background:var(--panel);border:1px solid var(--hair);border-radius:10px;padding:14px 16px;margin:8px 0}
dl.facts dt{color:var(--dim)}dl.facts dd{margin:0}
.note{color:var(--dim);font-size:.78rem;line-height:1.6;margin-top:10px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.blk{background:var(--panel);border:1px solid var(--hair);border-radius:10px;padding:12px 14px;margin:8px 0;font-size:.85rem}
.tag{display:inline-block;border:1px solid var(--hair);border-radius:5px;padding:1px 7px;font-size:.72rem;color:var(--dim);margin-left:6px}
.crumb{color:var(--dim);font-size:.78rem;margin-top:18px}.crumb a{color:var(--dim)}
table.months{max-width:320px;margin-top:8px}table.months td:last-child{text-align:right}
.rank td:first-child{white-space:nowrap}
"""

# current vendor-documented crawler/fetcher tokens (2026-09); "User-agent: *"
# already allows everything — these are an explicit welcome, not access control
AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot",
               "Claude-User", "PerplexityBot", "Perplexity-User", "Google-Extended",
               "Applebot", "Applebot-Extended", "Amazonbot", "DuckAssistBot", "CCBot",
               "meta-externalagent", "meta-externalfetcher"]

# the SPA root carries this tag in app/index.html; baked pages must emit the
# same one or the 6,700 search landing pages report zero traffic
BEACON = ('<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
          'data-cf-beacon=\'{"token": "0b01c59e4c9a4287a1a8016059328982"}\'></script>')
OG_IMAGE = f"{SITE}/og.png"
SCHEMA_VERSION = "2026-09"
# Central Valley stations whose winter fog is the named "tule fog" searchers ask for
TULE_FOG = {"KFAT", "KBFL", "KSMF", "KSCK", "KMOD", "KVIS", "KMCE", "KRDD", "KSAC", "KMHR", "KMYV", "KMER", "KPTV", "KHJO"}
# Bay Area stations where "fog" to a local means the marine stratus deck, not sub-CAT-I ground fog
MARINE_LAYER = {"KSFO", "KOAK", "KSJC"}
# municipality spellings OurAirports splits that searchers treat as one place
MUNI_ALIAS = {"Bangalore": "Bengaluru"}
# how the title question names a city when the municipality string is formal
TITLE_ALIAS = {"New Delhi": "Delhi", "Sydney (Mascot)": "Sydney"}
CAT_LABEL = {"CATIII": "CAT III", "CATII": "CAT II", "CATI": "CAT I", "NONE": "no ILS"}
CAUSE_LABEL = {"FG": "fog", "BR": "mist", "HZ": "haze", "FU": "smoke", "SN": "snow", "CEIL": "low ceiling"}


def trunc(s: str, n: int) -> str:
    """Word-boundary truncation for snippets ('Fresno Yosemite Internation' never ships)."""
    s = s.replace('"', "'")
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(" ,;:-—")
    return cut + "…"


def og_tags(url: str, title: str, desc: str) -> str:
    return (f'<meta property="og:type" content="website"><meta property="og:url" content="{url}">\n'
            f'<meta property="og:title" content="{trunc(title, 90)}">\n'
            f'<meta property="og:description" content="{trunc(desc, 190)}">\n'
            f'<meta property="og:image" content="{OG_IMAGE}"><meta property="og:image:width" content="1200">'
            f'<meta property="og:image:height" content="630">\n'
            f'<meta name="twitter:card" content="summary_large_image">')


def breadcrumb(items) -> tuple[str, dict]:
    """items: [(label, url)] — visible trail + BreadcrumbList node."""
    html = ' › '.join(f'<a href="{u}">{l}</a>' if u else l for l, u in items)
    node = {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": l, **({"item": u} if u else {})}
        for i, (l, u) in enumerate(items)]}
    return f'<nav class="crumb">{html}</nav>', node


def hav_nm(lat1, lon1, lat2, lon2) -> float:
    from math import radians, sin, cos, asin, sqrt
    p1, p2 = radians(lat1), radians(lat2)
    dl = radians(lon2 - lon1)
    h = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 3440.1 * 2 * asin(sqrt(h))


def nearby_index(atlas, k=5):
    """icao -> k nearest atlas airports (nm), bucketed by 2° cells."""
    cells: dict = {}
    for a in atlas:
        cells.setdefault((int(a["lat"] // 2), int(a["lon"] // 2)), []).append(a)
    out = {}
    for a in atlas:
        cx, cy = int(a["lat"] // 2), int(a["lon"] // 2)
        cand = [b for dx in (-1, 0, 1) for dy in (-1, 0, 1) for b in cells.get((cx + dx, cy + dy), [])
                if b["icao"] != a["icao"]]
        cand.sort(key=lambda b: hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]))
        out[a["icao"]] = [(b, round(hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]))) for b in cand[:k]]
    return out


def read_json(*candidates):
    for p in candidates:
        if p.exists():
            return json.load(open(p))
    raise FileNotFoundError(candidates)


def load_forecast():
    """Newest issuance: local engine output when FRESH (<6 h — a leftover
    local file from an old run must not shadow the live API), else live."""
    local = None
    p = HERE / "out" / "current.json"
    if p.exists():
        local = json.load(open(p))
        cyc = datetime.fromisoformat(local["meta"]["cycle"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - cyc < timedelta(hours=6):
            return local
        print(f"  local current.json is stale ({local['meta']['cycle']}) — using live API")
    try:
        req = urllib.request.Request(f"{SITE}/api/forecast", headers={"User-Agent": "fogatlas-build"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception as e:
        print(f"  live forecast unavailable ({e}) — falling back to {'stale local' if local else 'none'}")
        return local


AWC = "https://aviationweather.gov/api/data"


def awc_batch(kind: str, icaos, per=50, budget_s=360) -> dict:
    """Latest TAF or METAR per station from the NOAA AWC data API (the same
    feed /api/metar proxies). Best-effort AND time-bounded: a failed batch
    just leaves those stations without the attributed line (the climatology
    answer stands), and the whole pass stops at `budget_s` — a throttled AWC
    once dragged the bake past the CI job timeout and cancelled the deploy."""
    import time
    out, t0, failed = {}, time.monotonic(), 0
    ids = sorted(icaos)
    for i in range(0, len(ids), per):
        if time.monotonic() - t0 > budget_s:
            print(f"  awc {kind}: time budget hit after {i}/{len(ids)} stations — continuing with what we have")
            break
        chunk = ids[i:i + per]
        url = f"{AWC}/{kind}?ids={','.join(chunk)}&format=json"
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "fogatlas-build (fogatlas.org)"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    for rec in json.load(r):
                        ic = rec.get("icaoId")
                        if ic and (ic not in out or kind == "taf" and rec.get("mostRecent")):
                            out[ic] = rec
                break
            except Exception as e:
                if attempt == 1:
                    failed += 1
                    if failed <= 3:
                        print(f"  awc {kind} batch {i // per} failed: {e}")
                else:
                    time.sleep(3)
        if failed >= 8:
            print(f"  awc {kind}: {failed} batches failed — AWC is throttling, stopping this pass")
            break
        time.sleep(0.3)
    print(f"  awc {kind}: {len(out)}/{len(ids)} stations in {time.monotonic() - t0:.0f}s")
    return out


AWC_CACHE = HERE / "out" / "awc_cache.json"


def awc_with_last_good(tafs: dict, obs: dict, now_utc, max_age_h=30) -> tuple[dict, dict]:
    """AWC 504s for an hour at a time; a bake during an outage must not strip
    the TAF/observation layer from 6,000 pages. Merge the last good snapshot
    (kept in the CI cache) for stations the fresh pass missed, only while the
    TAF issue time / observation time is younger than max_age_h — the page
    stamps that time, so an older line is still honest. Then save the merge."""
    cutoff = (now_utc - timedelta(hours=max_age_h)).timestamp()
    used = 0
    if AWC_CACHE.exists():
        try:
            prev = json.load(open(AWC_CACHE))
            for ic, rec in prev.get("taf", {}).items():
                if ic not in tafs:
                    t = rec.get("issueTime", "")
                    try:
                        ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if ts >= cutoff:
                        tafs[ic] = rec; used += 1
            for ic, rec in prev.get("metar", {}).items():
                if ic not in obs and (rec.get("obsTime") or 0) >= cutoff:
                    obs[ic] = rec; used += 1
            if used:
                print(f"  awc: reused {used} last-good entries from {prev.get('saved', '?')}")
        except Exception as e:
            print(f"  awc cache unreadable ({e})")
    AWC_CACHE.parent.mkdir(parents=True, exist_ok=True)
    AWC_CACHE.write_text(json.dumps({"saved": now_utc.strftime("%Y-%m-%dT%H:%MZ"), "taf": tafs, "metar": obs},
                                    separators=(",", ":")))
    return tafs, obs


def _vis_mi(v):
    if v is None:
        return None
    if isinstance(v, str):
        return 10.0 if v.endswith("+") else (float(v) if v.replace(".", "", 1).isdigit() else None)
    return float(v)


def taf_summary(rec, tz, horizon_h=30) -> dict | None:
    """Reduce a decoded TAF to the one thing the page asks: does the airport's
    own terminal forecast call for dense fog (vis < 1 mi) in the next ~30 h?"""
    if not rec or not rec.get("fcsts"):
        return None
    issue = rec.get("issueTime", "")
    try:
        t_issue = datetime.fromisoformat(issue.replace("Z", "+00:00"))
    except ValueError:
        return None
    end = t_issue + timedelta(hours=horizon_h)
    valid_to = datetime.fromtimestamp(rec["validTimeTo"], tz=timezone.utc) if rec.get("validTimeTo") else end
    end = min(end, valid_to)
    dense, possible, mist, min_vis = [], [], [], 10.0
    for f in rec["fcsts"]:
        t0 = datetime.fromtimestamp(f["timeFrom"], tz=timezone.utc)
        if t0 >= end:
            continue
        vis = _vis_mi(f.get("visib"))
        wx = f.get("wxString") or ""
        if vis is not None:
            min_vis = min(min_vis, vis)
        foggy = (vis is not None and vis < 1.0) or "FG" in wx.split()
        change = f.get("fcstChange") or ""
        if foggy:
            (possible if change in ("TEMPO", "PROB") or f.get("probability") else dense).append(t0)
        elif ("BR" in wx or "HZ" in wx) and vis is not None and vis < 6:
            mist.append((t0, vis))
    def when(t):
        loc = t.astimezone(tz)
        return f"{loc.strftime('%A')} {loc.strftime('%-I %p').lower()}"
    if dense:
        verdict, txt = "dense fog", f"calls for dense fog (visibility under 1 mile) from about {when(min(dense))}"
    elif possible:
        verdict, txt = "possible dense fog", f"carries a temporary/probability group with dense fog around {when(min(possible))}"
    elif mist:
        t0, v = min(mist, key=lambda x: x[1])
        m = round(v * 1609 / 100) * 100
        verdict, txt = "mist or haze", f"calls for mist or haze with visibility down to about {m:,} m — reduced, but not dense fog"
    else:
        verdict, txt = "no fog", "does not forecast dense fog"
    return {"issued": t_issue.strftime("%Y-%m-%dT%H:%MZ"),
            "validTo": valid_to.strftime("%Y-%m-%dT%H:%MZ"),
            "horizonEnd": end.strftime("%Y-%m-%dT%H:%MZ"),
            "verdict": verdict, "minVisMi": round(min_vis, 2) if min_vis < 10 else None,
            "sentence": f"The airport's own terminal forecast (TAF issued {t_issue.strftime('%H:%MZ')}) {txt} through {when(end)}.",
            "raw": rec.get("rawTAF")}


def obs_line(rec, tz) -> tuple[str, dict] | None:
    """Age-stamped latest observation for the baked page; _fog.js overwrites it live."""
    if not rec or not rec.get("obsTime"):
        return None
    t = datetime.fromtimestamp(rec["obsTime"], tz=timezone.utc)
    vis = _vis_mi(rec.get("visib"))
    wx = rec.get("wxString") or ""
    fog = (vis is not None and vis < 1.0) or "FG" in wx.split()
    loc = t.astimezone(tz)
    stamp = f"{loc.strftime('%-I:%M %p').lower()} {loc.strftime('%a %-d %b')} local ({t.strftime('%H:%MZ')})"
    vis_num = None if vis is None else ("10+" if vis >= 10 else f"{vis:g}")
    vis_txt = "visibility unknown" if vis_num is None else f"visibility {vis_num} mi"
    if fog:
        html = f"<b>Fog at last observation</b> — {vis_txt}{(' in ' + wx) if wx else ''}, observed {stamp}."
    else:
        html = f'<span class="ok">No fog at last observation</span> — {vis_txt}{(", " + wx) if wx else ""}, observed {stamp}.'
    html += ' <span class="asof">latest observation at page build · updates live below when JavaScript runs</span>'
    return html, {"time": t.strftime("%Y-%m-%dT%H:%MZ"), "visibilityMi": vis, "fog": fog, "weather": wx or None,
                  "raw": rec.get("rawOb")}


def month_hours(grid, mon: int) -> int:
    days = [31, 28.2, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mon]
    return round(sum(grid[mon]) / 100.0 * days)


def peak_months(grid) -> list[int]:
    tot = [(m, sum(grid[m])) for m in range(12)]
    tot.sort(key=lambda x: -x[1])
    return [m for m, s in tot[:2] if s > 0]


def clim_sentence(a, sp, mon: int) -> str:
    """The climatology-first answer every non-public page leads with."""
    mh = month_hours(a["grid"], mon)
    if mh >= 1:
        s = f"{MONTHS[mon]} averages about {mh} hour{'s' if mh != 1 else ''} of dense fog here (10-year average)"
    else:
        s = f"Dense fog (visibility under a mile) is rare here in {MONTHS[mon]} (10-year average)"
    if sp and not sp.get("low"):
        s += (f"; fog season {'runs' if 'through' in sp['seasonTxt'] else 'is'} {sp['seasonTxt']}, "
              f"mostly {sp['bandTxt']}")
        if sp.get("clearsBy"):
            s += f", usually lifting by {sp['clearsBy']}"
    return s + "."


def bake_answer(a, fc, covered: bool, now_utc, sp=None, taf=None):
    """(html, plain, machine) for the static answer block. Never prints model
    percentages unless the airport is on the public (bar-passed) list.
    Non-public pages lead with climatology and, where AWC has one, the
    airport's own TAF — attributed official data, never a Fog Atlas number."""
    icao, tz = a["icao"], ZoneInfo(a["tz"])
    mon = now_utc.astimezone(tz).month - 1
    clim_txt = clim_sentence(a, sp, mon)
    taf_txt = f" {taf['sentence']}" if taf else ""
    taf_m = {"taf": {k: v for k, v in taf.items() if k != "sentence"}} if taf else {}
    feed_note = (f"Fog Atlas publishes its own calibrated fog percentage only where it has beaten "
                 f"climatology on live verification; {icao} has no NWS model feed, so none is published here.")

    fa = fc and fc.get("airports", {}).get(icao)
    if not covered:
        plain = f"{clim_txt}{taf_txt} {feed_note}"
        lead = "Dense fog in the airport's own forecast" if taf and taf["verdict"] == "dense fog" else "Climatology answer"
        html = f'<b>{lead}</b> — {clim_txt}{taf_txt} <span class="asof">{feed_note}</span>'
        return html, plain, {"covered": False, "public": False, "wording": plain, **taf_m}
    if not fa:  # covered, but absent from the newest issuance (feed initializing)
        plain = f"{clim_txt}{taf_txt} This station's NWS model feed is initializing; a calibrated percentage publishes only after live verification."
        return (f"<b>Climatology answer</b> — {clim_txt}{taf_txt}", plain,
                {"covered": True, "public": False, "inIssuance": False, "wording": plain, **taf_m})

    public = icao in set(fc.get("meta", {}).get("publicAirports", []))
    cyc = datetime.fromisoformat(fc["meta"]["cycle"].replace("Z", "+00:00"))
    asof = f'<span class="asof">forecast cycle {fc["meta"]["cycle"]} · page rebuilt daily · live layers below update hourly</span>'
    horizon = [(fh, r[0]) for fh, r in zip(fa["fhrs"], fa["p"]) if fh <= 36]

    if public and horizon:
        peak_fh, peak_p = max(horizon, key=lambda t: t[1])
        peak_at = (cyc + timedelta(hours=peak_fh)).astimezone(tz)
        end_at = (cyc + timedelta(hours=max(fh for fh, _ in horizon))).astimezone(tz)
        day = peak_at.strftime("%A")
        hr = peak_at.strftime("%-I %p").lower()
        end_txt = f"{end_at.strftime('%A')} {end_at.strftime('%-I %p').lower()}"
        thr = max(15, peak_p * 0.4)
        win = [fh for fh, p in horizon if p >= thr]
        machine = {"covered": True, "public": True, "cycle": fc["meta"]["cycle"],
                   "peakPct": peak_p, "peakAtLocal": peak_at.isoformat(),
                   "horizonEndLocal": end_at.isoformat()}
        if peak_p >= 50:
            w0 = (cyc + timedelta(hours=win[0])).astimezone(tz)
            w1 = (cyc + timedelta(hours=win[-1])).astimezone(tz)
            plain = (f"Yes, fog is likely: {peak_p}% chance of fog (visibility under 1 mile) "
                     f"around {hr} {day}, with the fog window roughly "
                     f"{w0.strftime('%-I %p').lower()} to {w1.strftime('%-I %p').lower()} local time.")
            html = f"<b>Fog likely {day}</b> — {plain[len('Yes, fog is likely: '):]}"
            machine["window"] = {"from": w0.isoformat(), "to": w1.isoformat()}
        elif peak_p >= 20:
            plain = (f"Some chance of fog: the calibrated forecast peaks at {peak_p}% "
                     f"(visibility under 1 mile) around {hr} {day}.")
            html = f"<b>Some chance of fog</b> — peaks at {peak_p}% around {hr} {day}."
        else:
            plain = (f"No, fog is unlikely through {end_txt} — the calibrated forecast "
                     f"peaks at just {peak_p}%. {clim_txt}")
            html = f'<span class="ok">Fog unlikely</span> through {end_txt} (peak {peak_p}%). {clim_txt}'
        machine["wording"] = plain
        return html + asof, plain, machine

    # covered but shadow: guidance wording, no model percentages (the bar rule)
    vis = [v for v in fa.get("vis", []) if v is not None]
    liv = [v for v in fa.get("liv", []) if v is not None]
    signal = (vis and min(vis) < 1.0) or (liv and max(liv) >= 40)
    if signal:
        plain = (f"NWS guidance shows a fog signal here in the next 48 hours.{taf_txt} {clim_txt} "
                 "This station's calibrated percentages publish after live verification clears the accuracy bar.")
        html = f"<b>Fog possible</b> — NWS guidance shows a fog signal in the next 48 hours.{taf_txt} {clim_txt}"
    else:
        plain = f"No fog signal in NWS guidance for the next 48 hours.{taf_txt} {clim_txt}"
        html = f'<span class="ok">No fog signal</span> in NWS guidance for the next 48 hours.{taf_txt} {clim_txt}'
    return (html + asof, plain,
            {"covered": True, "public": False, "cycle": fc["meta"]["cycle"],
             "guidanceSignal": bool(signal), "wording": plain, **taf_m})


def jsonld(a, plain_answer, season_plain, subH, window, med, extra_faq=(), crumb=None, now_utc=None, iata=None) -> str:
    icao, name = a["icao"], a["name"]
    url = f"{SITE}/fog/{icao.lower()}/"
    faq = [
        {"@type": "Question", "name": f"Will it be foggy at {name} tomorrow?",
         "acceptedAnswer": {"@type": "Answer", "text": plain_answer}},
        {"@type": "Question", "name": f"When is fog season at {name}?",
         "acceptedAnswer": {"@type": "Answer",
                            "text": f"{season_plain} {name} records about {subH} hours per year below CAT I "
                                    f"approach minima ({window['start'][:4]}–{window['through'][:4]} observations)."}},
    ]
    if med:
        faq.append({"@type": "Question", "name": f"How long does fog usually last at {name}?",
                    "acceptedAnswer": {"@type": "Answer",
                                       "text": f"Once fog forms at {name} it typically lasts about {med['medianH']} "
                                               f"hour{'s' if med['medianH'] != 1 else ''} (middle half of events: "
                                               f"{med['p25H']}–{med['p75H']} h), from {med['n']} observed fog events."}})
    faq += [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": t}} for q, t in extra_faq]
    stamp = (now_utc or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    graph = [
        {"@type": "Airport", "@id": url + "#airport", "name": name, "icaoCode": icao,
         **({"iataCode": iata} if iata else {}),
         "geo": {"@type": "GeoCoordinates", "latitude": a["lat"], "longitude": a["lon"]},
         "address": {"@type": "PostalAddress", "addressCountry": a["country"]}},
        {"@type": "WebPage", "@id": url, "url": url, "dateModified": stamp,
         "isPartOf": {"@id": f"{SITE}/#site"}, "about": {"@id": url + "#airport"},
         **({"breadcrumb": crumb} if crumb else {})},
        {"@type": "FAQPage", "@id": url + "#faq", "mainEntity": faq},
        {"@type": "Dataset", "@id": url + "#data",
         "name": f"Fog climatology and calibrated fog forecast — {icao} {name}",
         "description": f"Hourly fog climatology ({window['start'][:4]}–{window['through'][:4]} METAR observations) "
                        f"and daily-refreshed calibrated fog forecast for {name}.",
         "url": url, "temporalCoverage": f"{window['start']}/{window['through']}",
         "isAccessibleForFree": True,
         "creator": {"@type": "Organization", "name": "Fog Atlas", "url": SITE},
         "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                           "contentUrl": url + "data.json"}],
         "isBasedOn": ["https://mesonet.agron.iastate.edu/", "https://www.weather.gov/mdl/nbm_home"]},
    ]
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, separators=(",", ":"))


CONF_LABEL = {"faa-nasr": "FAA NASR", "faa-c060": "FAA OpSpec C060", "aip": "national AIP", "assumed": "assumed"}


def capability_text(a, name) -> str:
    cat = a.get("catIls") or ""
    conf = CONF_LABEL.get(a.get("catConfidence") or "", a.get("catConfidence") or "")
    if cat == "NONE" or a.get("ils") == "no":
        s = f"{name} has no ILS on record"
        s += " but has LPV (GPS) approaches" if a.get("lpv") == "yes" else ""
        s += f" ({conf})" if conf else ""
        s += ", so real minima are typically above CAT I (LPV ~200–250 ft, LNAV/VNAV 350 ft or more) and the sub-CAT-I hours here understate the blocked time."
        return s
    lab = CAT_LABEL.get(cat, cat or "CAT I")
    s = f"{name} has {lab} ILS approaches"
    s += " plus LPV (GPS) approaches" if a.get("lpv") == "yes" else ""
    s += f" ({conf})" if conf else ""
    if cat in ("CATII", "CATIII"):
        s += "; suitably equipped airlines already land through most of its fog, so EFVS value here is for CAT I-limited operators."
    else:
        s += "; without CAT II/III, every sub-CAT-I hour is a diversion for a conventionally equipped aircraft."
    return s


def cause_text(a) -> str:
    causes = a.get("causes") or {}
    if not causes:
        return ""
    top = sorted(causes.items(), key=lambda kv: -kv[1])[:3]
    return ", ".join(f"{CAUSE_LABEL.get(k, k.lower())} {round(v)}%" for k, v in top if v >= 1)


def page(a, ends, covered, r10_by_mh, fc, window, pers, now_utc, city_link=None,
         iata=None, taf=None, obs=None, nearby=(), receipt=None) -> tuple[str, dict]:
    icao, name = a["icao"], a["name"]
    grid = a["grid"]
    tz = ZoneInfo(a["tz"])
    subH = round(a["efvsHoursPerYear"] + a["belowHoursPerYear"])
    pk = peak_months(grid)
    pk_txt = (" and ".join(MONTHS[m] for m in pk) if pk else "no month in particular")
    med = pers.get(icao)
    med = {"medianH": med["medianH"], "p25H": med["p25H"], "p75H": med["p75H"], "n": med["n"]} if med else None
    sp = season_profile(a)
    public_now = bool(fc and icao in set(fc.get("meta", {}).get("publicAirports", [])))
    taf_s = taf_summary(taf, tz) if (taf and not public_now) else None
    answer_html, answer_plain, machine = bake_answer(a, fc, covered, now_utc, sp=sp, taf=taf_s)
    season_html, season_plain = season_section(a, sp, med, name)
    ob = obs_line(obs, tz) if obs else None
    yrs = f"{window['start'][:4]}–{window['through'][:4]}"
    code = f"{icao} / {iata}" if iata else icao

    if icao in MARINE_LAYER:
        ml = (f"Bay Area \"fog\" is usually the marine layer — a low stratus deck at roughly 500–1,500 ft — while this page "
              f"measures dense ground fog (visibility under a mile or ceiling under 200 ft), which {icao} records only "
              f"about {subH} hours a year; summer arrival delays at SFO come from that low ceiling, not from visibility. ")
        answer_html = f"<b>Marine layer ≠ dense fog.</b> {ml}" + answer_html
        answer_plain = ml + answer_plain

    if covered and icao in r10_by_mh:
        clim = [[round(100 * r10_by_mh[icao].get((m + 1, h), 0.0), 1) for h in range(24)] for m in range(12)]
        clim_label = "vis < 1 mile"
    else:
        clim = [[grid[m][h] for h in range(24)] for m in range(12)]
        clim_label = "below CAT I (~½ mi / 200 ft)"

    monthly = [month_hours(grid, m) for m in range(12)]
    foggiest = max(range(12), key=lambda m: monthly[m])
    quietest = min(range(12), key=lambda m: monthly[m])
    cap_txt = capability_text(a, name)
    cause_txt = cause_text(a)
    efvs = a.get("efvsOppHoursPerYear")
    rel = a.get("reliability")
    cov = a.get("coveragePct")
    rel_txt = (f"{cov:g}% of possible hours observed" if cov is not None else "") + \
              (" · flagged: low coverage or suspect reporting — treat frequencies with care" if rel and rel != "ok" else "")

    facts = f"""
  <dl class="facts" id="facts">
    <dt>Fog hours per year</dt><dd data-fact="subCat1HoursPerYear">{subH} h below CAT I minima ({yrs} average)</dd>
    <dt>Fog season</dt><dd data-fact="fogSeason">{pk_txt}{' · the winter tule fog' if icao in TULE_FOG else ''}</dd>
    {f'<dt>Typical fog event</dt><dd data-fact="medianEventH">~{med["medianH"]} h once it forms (middle half {med["p25H"]}–{med["p75H"]} h, n={med["n"]})</dd>' if med else ''}
    {f'<dt>What causes it</dt><dd data-fact="causes">{cause_txt}</dd>' if cause_txt else ''}
    <dt>Approaches</dt><dd data-fact="capability">{CAT_LABEL.get(a.get('catIls') or '', 'CAT I')}{' ILS' if (a.get('catIls') or '') != 'NONE' else ''}{' · LPV' if a.get('lpv') == 'yes' else ''}{f" · {CONF_LABEL.get(a.get('catConfidence') or '', a.get('catConfidence') or '')}" if a.get('catConfidence') else ''}</dd>
    {f'<dt>EFVS-recoverable</dt><dd data-fact="efvsOppHoursPerYear">~{efvs:g} h/yr an EFVS-equipped aircraft at CAT I minima could fly</dd>' if efvs is not None else ''}
    {f'<dt>Observation record</dt><dd data-fact="reliability">{rel_txt}</dd>' if rel_txt else ''}
    <dt>Data through</dt><dd data-fact="dataThrough">{window['through']} · <a href="data.json">machine-readable data.json</a></dd>
  </dl>"""

    month_rows = "".join(f"<tr><td>{MONTHS[m]}</td><td>{monthly[m]} h</td></tr>" for m in range(12))
    months_html = (f'<table class="months"><tr><th>month</th><th>hours below CAT I</th></tr>{month_rows}</table>'
                   f'<p class="note">Foggiest month {MONTHS[foggiest]} (~{monthly[foggiest]} h); quietest {MONTHS[quietest]} (~{monthly[quietest]} h). {yrs} average.</p>')

    rwy_rows = "".join(
        f"<tr><td><b>{e['e']}</b></td><td>{ALS_LABEL.get(e['als'] or '', e['als'] or '—')}</td>"
        f"<td>{('CAT ' + e['ils']) if e['ils'] else ('LPV' if e['lpv'] else '—')}</td>"
        f"<td>{e['len']:,} ft</td><td>{'RVR' if e['rvr'] else '—'}</td></tr>"
        for e in (ends or []))
    rwy_html = f"""
  <h2>Runway infrastructure</h2>
  <table><tr><th>end</th><th>approach lights</th><th>best approach</th><th>length</th><th>RVR</th></tr>{rwy_rows}</table>
  <p class="note">From FAA NASR / curated AIP Canada research. Approach-light class and minima tier drive how low an approach can be flown — details in the <a href="{SITE}/#chase">chase board</a>.</p>""" if rwy_rows else ""

    tule_html = (f'<div class="blk">The winter fog here is the Central Valley\'s <b>tule fog</b> — dense radiation fog that forms on '
                 f'calm, clear nights after the first autumn rains and can sit for days. {icao} averages '
                 f'{monthly[11] + monthly[0]} hours below CAT I minima in December–January alone. The NWS Hanford office '
                 f'issues a seasonal fog severity index for the valley; this page adds the ten-year airport record.</div>'
                 if icao in TULE_FOG else "")

    near_html = ""
    if nearby:
        near_html = "<h2>Nearby airports</h2><p class=\"note\">" + " · ".join(
            f'<a href="/fog/{b["icao"].lower()}/">{b["icao"]} {b["name"]}</a> ({nm} nm, {round(b["efvsHoursPerYear"] + b["belowHoursPerYear"])} h/yr)'
            for b, nm in nearby) + "</p>"

    receipt_html = ""
    if receipt:
        receipt_html = (f'<div class="blk">Verification receipt: the calibrated forecast for {icao} beat climatology by '
                        f'<b>{receipt["skill_v10"]:+.1f}%</b> (Brier skill, visibility &lt; 1 mi) over {receipt["n"]:,} '
                        f'verified forecast/outcome pairs and {receipt["events_v10"]} observed fog hours, scored through '
                        f'{receipt["scoredThrough"]} — <a href="{SITE}/fog/scorecard/">full scorecard</a>.</div>')

    extra_faq = [
        (f"Which month is foggiest at {name}?",
         f"{MONTHS[foggiest]}, with about {monthly[foggiest]} hours below CAT I minima in a typical {MONTHS[foggiest]}; "
         f"the quietest month is {MONTHS[quietest]} (~{monthly[quietest]} h). {yrs} average."),
        (f"How many hours of fog does {name} get per year?",
         f"About {subH} hours per year below CAT I approach minima (visibility under about half a mile or ceiling under 200 ft), "
         f"{yrs} average. By month: " + ", ".join(f"{MONTHS_S[m]} {monthly[m]} h" for m in range(12)) + "."),
        (f"Does {name} have CAT II or CAT III approaches?", cap_txt),
    ]
    if sp.get("clearsBy"):
        extra_faq.append((f"What time does fog usually lift at {name}?",
                          f"Fog at {name} is mostly a {sp['bandTxt']} phenomenon and usually lifts by {sp['clearsBy']} local time "
                          f"({yrs} observations)."))
    if ob:
        extra_faq.append((f"Is it foggy at {name} right now?",
                          f"At the latest observation before this page was built ({ob[1]['time']}) visibility was "
                          f"{'unknown' if ob[1]['visibilityMi'] is None else str(ob[1]['visibilityMi']) + ' mi'}"
                          f"{' with fog reported' if ob[1]['fog'] else ' with no fog reported'}. Live observations: {SITE}/api/metar?ids={icao}."))
    if icao in MARINE_LAYER:
        extra_faq.append((f"Does this page cover the marine layer (Karl the Fog)?",
                          "Only where it drops to the ground. The Bay Area marine layer is a stratus deck that usually sits at "
                          "500–1,500 ft, which is why SFO records only about ten hours a year below CAT I approach minima; "
                          "the summer arrival delays come from that low ceiling (no paired visual approaches), not from visibility."))
    if icao in TULE_FOG:
        extra_faq.append((f"Is the fog at {name} tule fog?",
                          f"Yes — the Central Valley's winter radiation fog. {icao} averages {monthly[11] + monthly[0]} hours below "
                          f"CAT I minima in December and January combined; fog season here {'runs ' + sp['seasonTxt'] if sp.get('seasonTxt') else 'is winter'}."))

    crumb_html, crumb_node = breadcrumb([("Fog Atlas", f"{SITE}/"), ("Airports", f"{SITE}/fog/"), (code, None)])
    title = f"{code} fog forecast — will it be foggy at {name} tomorrow?"
    desc = f"{trunc(answer_plain, 150)} {subH} fog hours/yr, season peaks {pk_txt}."
    url = f"{SITE}/fog/{icao.lower()}/"

    html = f"""<!doctype html>
<html lang="en"><head>
{REDIRECT}
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{trunc(desc, 158)}">
<link rel="canonical" href="{url}">
<link rel="alternate" type="application/json" href="{url}data.json" title="{icao} fog data (JSON)">
<link rel="describedby" href="/llms.txt">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
{og_tags(url, f"{code} fog forecast — {name}", answer_plain)}
<script type="application/ld+json">{jsonld(a, answer_plain, season_plain, subH, window, med, extra_faq, crumb_node, now_utc, iata)}</script>
<style>{CSS}</style>
</head><body><main>
{crumb_html}
<h1><b>{code}</b> — {name}</h1>
<div class="sub">fog forecast &amp; climatology{f' · <a href="{city_link[1]}">{city_link[0]} city page</a>' if city_link else ''} · <a href="{SITE}/#{icao}">interactive 10-year analysis →</a></div>
<div id="answer">{answer_html}</div>
<div id="verdict"{'' if ob else ' hidden'}>{ob[0] if ob else ''}</div>
<div id="strip"></div>
<p class="ctx" id="ctx"></p>
{facts}
{receipt_html}
<h2>When this airport fogs in</h2>
<div class="blk">In a typical year {name} spends <b>{subH} hours</b> below CAT I approach minima (visibility under ~½ mile or ceiling under 200 ft), concentrated in <b>{pk_txt}</b>{f' — {cause_txt}' if cause_txt else ''}. The strip above shows the hour-by-hour pattern for the current month from ten years of weather observations.</div>
{tule_html}
{season_html}
{months_html}
<h2>Approach capability</h2>
<div class="blk">{cap_txt}{f' An EFVS-equipped aircraft flying to CAT I minima would recover about <b>{efvs:g} hours a year</b> here.' if efvs is not None else ''}</div>
{rwy_html}
{near_html}
<h2>For flight operations</h2>
<p class="note">EFVS crews: the <a href="{SITE}/#chase">CHASE board</a> ranks airports by live fog status, approach lighting, go-around height and flight time from your base. Forecast probabilities publish here per-airport once the calibrated model beats climatology on live verification — receipts on the <a href="{SITE}/fog/scorecard/">scorecard</a>.</p>
<p class="note">Sources: NOAA/NWS National Blend of Models guidance · NOAA Aviation Weather Center TAF/METAR · METAR observations {yrs} (Iowa Environmental Mesonet) · FAA NASR. <a href="{SITE}/methodology/">Methodology</a> · <a href="{SITE}/about/">About</a> · <a href="{SITE}/fog/">all airports</a> · <a href="{SITE}/fog/city/">by city</a>. Machine access: <a href="/fog/{icao.lower()}/data.json">data.json</a> · <a href="/llms.txt">llms.txt</a>. Not for operational use.</p>
<script>window.__FOG={{icao:"{icao}",clim:{json.dumps(clim)},climLabel:"{clim_label}",covered:{str(covered).lower()},tz:"{a['tz']}"}}</script>
<script src="/fog/_fog.js" defer></script>
{BEACON}
</main></body></html>"""

    data = {
        "schemaVersion": SCHEMA_VERSION,
        "icao": icao, "iata": iata, "name": name, "lat": a["lat"], "lon": a["lon"],
        "country": a["country"], "tz": a["tz"],
        "updated": now_utc.strftime("%Y-%m-%dT%H:%MZ"),
        "refreshCadence": "daily",
        "validThrough": (now_utc + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%MZ"),
        "window": {"start": window["start"], "through": window["through"]},
        "climatology": {
            "subCat1HoursPerYear": subH,
            "bandDefinition": "visibility < ~800 m OR ceiling < 200 ft",
            "fogSeasonPeakMonths": [MONTHS[m] for m in pk],
            "monthlyHours": monthly, "months": MONTHS,
            "foggiestMonth": MONTHS[foggiest],
            "causes": a.get("causes"),
            "medianEventHours": med["medianH"] if med else None,
            "season": {"summary": season_plain,
                       "months": [MONTHS[m] for m in sp.get("seasonMonths", [])],
                       "timeOfDay": sp.get("bandTxt"),
                       "typicallyClearsBy": sp.get("clearsBy")},
            "observationCoveragePct": cov, "reliability": rel,
        },
        "capability": {"ilsCategory": a.get("catIls"), "source": a.get("catConfidence"),
                       "ils": a.get("ils"), "lpv": a.get("lpv"),
                       "efvsRecoverableHoursPerYear": efvs, "summary": cap_txt},
        "forecast": {**machine,
                     "thresholdDefinition": "peakPct = calibrated P(visibility < 1 statute mile) in the worst hour of the next 36 h; "
                                            "published only for airports on the public (bar-passed) list — climatology above uses the CAT I band"},
        "currentObservation": ob[1] if ob else None,
        "verification": receipt,
        "nearby": [{"icao": b["icao"], "name": b["name"], "nm": nm,
                    "subCat1HoursPerYear": round(b["efvsHoursPerYear"] + b["belowHoursPerYear"])} for b, nm in nearby],
        "links": {
            "page": url,
            "liveForecastApi": f"{SITE}/api/forecast",
            "liveObservationsApi": f"{SITE}/api/metar?ids={icao}",
            "hourlyClimatologyDetail": f"{SITE}/data/detail/{icao}.json",
            "verification": f"{SITE}/fog/scorecard/",
            "methodology": f"{SITE}/methodology/",
            "about": f"{SITE}/about/",
            "index": f"{SITE}/fog/index.json",
        },
    }
    return html, data


FOG_JS = r"""
(async function () {
  const S = window.__FOG;
  const $ = (id) => document.getElementById(id);
  const mon = new Date().getMonth();
  const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];

  // climatology strip for the current month (always available, crawler-stable data)
  const rates = S.clim[mon];
  const peak = Math.max(...rates, 1);
  const bars = rates.map((r, h) => {
    const y = 46 - Math.round(44 * r / peak);
    return `<rect x="${h * 28 + 1}" y="${y}" width="26" height="${46 - y}" rx="2" fill="#3f76a3" opacity="${r > 0 ? 0.9 : 0.25}"><title>${String(h).padStart(2, "0")}:00 — ${r}% of ${MONTHS[mon]} hours</title></rect>`;
  }).join("");
  $("strip").innerHTML = `<svg viewBox="0 0 672 48" preserveAspectRatio="none">${bars}</svg>
    <div class="striplab"><span>midnight</span><span>${MONTHS[mon]} · typical hours with ${S.climLabel} (local time)</span><span>11 pm</span></div>`;
  const mh = rates.reduce((a, b) => a + b, 0) / 100 * 30;
  $("ctx").textContent = `${MONTHS[mon]} typically brings ${mh < 1 ? "under an hour" : Math.round(mh) + " hours"} of ${S.climLabel} conditions here.`;

  // live layer: current conditions + freshest forecast (the baked #answer
  // above is the daily crawler-stable version; this is the hourly one)
  let live = "";
  try {
    const arr = await (await fetch(`/api/metar?ids=${S.icao}&hours=3`)).json();
    const ob = arr && arr[0];
    if (ob) {
      const vis = typeof ob.visib === "string" ? parseFloat(ob.visib) || 10 : ob.visib;
      const foggy = vis != null && vis < 1.0;
      const when = (ob.rawOb || "").match(/\d{6}Z/) || [""];
      live = foggy
        ? `<b>Fog now</b> — visibility ${vis} mile${vis === 1 ? "" : "s"} at ${S.icao} (${when[0]}).`
        : `<span class="ok">No fog right now</span> — visibility ${vis == null ? "unknown" : vis + " mi"} at ${S.icao} (${when[0]}).`;
    }
  } catch (e) {}

  if (S.covered) try {
    const fc = await (await fetch("/api/forecast")).json();
    const a = fc.airports && fc.airports[S.icao];
    const isPublic = a && Array.isArray(fc.meta && fc.meta.publicAirports) && fc.meta.publicAirports.includes(S.icao);
    if (isPublic) {
      const peakP = Math.max(...a.p.map((r) => r[0]));
      const idx = a.p.findIndex((r) => r[0] === peakP);
      const at = new Date(new Date(fc.meta.cycle).getTime() + a.fhrs[idx] * 3600e3);
      const t = at.toLocaleString("en-US", { weekday: "short", hour: "numeric", timeZone: S.tz });
      live += peakP >= 20
        ? ` <b>Latest cycle:</b> fog peaks ${t} at ${peakP}%.`
        : ` Latest cycle: fog unlikely in the next 48 h (peak ${peakP}%).`;
    }
  } catch (e) {}

  if (live) { $("verdict").innerHTML = live; $("verdict").hidden = false; }
})();
"""


MONTHS_S = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def hour12(h: int) -> str:
    return "midnight" if h == 0 else "noon" if h == 12 else f"{h} AM" if h < 12 else f"{h - 12} PM"


def season_profile(a) -> dict:
    """Evergreen fog-season facts from the climatology grid: which months,
    what time of day, when it typically clears. All local time."""
    grid = a["grid"]
    monthly = [month_hours(grid, m) for m in range(12)]
    peak_m = max(range(12), key=lambda m: monthly[m])
    if monthly[peak_m] < 2:
        return {"monthly": monthly, "low": True}
    thr = max(2, monthly[peak_m] * 0.25)
    inseason = [m for m in range(12) if monthly[m] >= thr]
    # describe as a contiguous range when it is one (with Dec->Jan wrap)
    span = None
    if len(inseason) < 12:
        ext = sorted(inseason) + [m + 12 for m in sorted(inseason)]
        runs, cur = [], [ext[0]]
        for x in ext[1:]:
            if x == cur[-1] + 1:
                cur.append(x)
            else:
                runs.append(cur); cur = [x]
        runs.append(cur)
        best = max(runs, key=len)
        if len(best) == len(inseason):
            span = (best[0] % 12, best[-1] % 12)
    if len(inseason) == 12:
        season_txt = "year-round"
    elif len(inseason) >= 8:  # "September through July" is not a season
        season_txt = "most of the year"
    elif span and span[0] != span[1]:
        season_txt = f"{MONTHS[span[0]]} through {MONTHS[span[1]]}"
    elif len(inseason) == 1:
        season_txt = MONTHS[inseason[0]]
    else:
        season_txt = ", ".join(MONTHS[m] for m in sorted(inseason))

    # diurnal shape across the season months
    diurnal = [sum(grid[m][h] for m in inseason) for h in range(24)]
    ph = max(range(24), key=lambda h: diurnal[h])
    band = [h for h in range(24) if diurnal[h] >= diurnal[ph] * 0.5]
    allday = len(band) > 14
    # walk forward from the peak to the first hour it thins to a quarter
    clears = None
    if not allday:
        for i in range(1, 13):
            if diurnal[(ph + i) % 24] < diurnal[ph] * 0.25:
                clears = (ph + i) % 24
                break
    # contiguous band around the peak for "3-9 AM" phrasing
    lo = hi = ph
    while diurnal[(lo - 1) % 24] >= diurnal[ph] * 0.5 and (ph - lo) < 12:
        lo -= 1
    while diurnal[(hi + 1) % 24] >= diurnal[ph] * 0.5 and (hi - ph) < 12:
        hi += 1
    return {"monthly": monthly, "low": False, "seasonMonths": sorted(inseason),
            "seasonTxt": season_txt, "peakMonth": peak_m, "allDay": allday,
            "bandTxt": ("any hour of the day" if allday
                        else f"{hour12(lo % 24)}–{hour12(hi % 24)}"),
            "clearsBy": None if clears is None else hour12(clears)}


def season_svg(monthly) -> str:
    """Server-rendered 12-month bars — crawlers see the shape of the year."""
    peak = max(monthly) or 1
    bars = "".join(
        f'<rect x="{m * 56 + 4}" y="{60 - round(52 * v / peak)}" width="48" '
        f'height="{round(52 * v / peak)}" rx="3" fill="#3f76a3" opacity="{0.95 if v == peak else 0.75 if v else 0.25}">'
        f"<title>{MONTHS[m]}: ~{v} h</title></rect>"
        f'<text x="{m * 56 + 28}" y="72" text-anchor="middle" fill="#8294a3" font-size="10">{MONTHS_S[m]}</text>'
        for m, v in enumerate(monthly))
    return (f'<svg viewBox="0 0 672 76" role="img" aria-label="Fog hours by month" '
            f'style="width:100%;height:auto;display:block">{bars}</svg>')


def season_section(a, sp, med, subject: str) -> tuple[str, str]:
    """(html section, plain-language summary) — the summary also feeds the
    FAQ answer and data.json so humans, Google and AI read the same fact."""
    if sp["low"]:
        plain = (f"{subject} has no real fog season — dense fog is rare in every month "
                 f"(under 2 hours even in the peak month).")
        html = f"""
  <h2>Fog season</h2>
  {season_svg(sp["monthly"])}
  <div class="blk">{plain}</div>"""
        return html, plain
    st = sp["seasonTxt"]
    lead = (f"Fog occurs {st} at {subject}" if st in ("year-round", "most of the year")
            else f"Fog season {'runs' if 'through' in st else 'is'} {st}")
    bits = [f"{lead.replace(st, '<b>' + st + '</b>')}, peaking in {MONTHS[sp['peakMonth']]}"]
    plain_bits = [f"{lead}, peaking in {MONTHS[sp['peakMonth']]}"]
    tod = (f"fog here can hit at {sp['bandTxt']}" if sp["allDay"]
           else f"fog is mostly an early-hours <b>{sp['bandTxt']}</b> phenomenon")
    tod_p = tod.replace("<b>", "").replace("</b>", "")
    bits.append(tod)
    plain_bits.append(tod_p)
    if sp["clearsBy"]:
        bits.append(f"usually lifting by <b>{sp['clearsBy']}</b>")
        plain_bits.append(f"usually lifting by {sp['clearsBy']}")
    if med:
        bits.append(f"a typical event lasts about {med['medianH']} hour{'s' if med['medianH'] != 1 else ''} once it forms")
        plain_bits.append(f"a typical event lasts about {med['medianH']} hour{'s' if med['medianH'] != 1 else ''}")
    plain = "; ".join(plain_bits) + " (local time, 10-year average)."
    html = f"""
  <h2>Fog season</h2>
  {season_svg(sp["monthly"])}
  <div class="blk">{"; ".join(bits)} <span class="tag">local time · 10-yr average</span></div>"""
    return html, plain


SUFFIX_COUNTRIES = {"US", "CA", "AU"}  # region code disambiguates (Columbus x7 states)


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def load_municipalities() -> tuple[dict, dict]:
    """icao -> (municipality, iso_region, type) and icao -> IATA from the
    committed OurAirports dump. MUNI_ALIAS folds spellings searchers treat as
    one place (Bangalore/Bengaluru) so a city gets one page, not two."""
    out, iata = {}, {}
    with open(HERE.parent / "pipeline" / "data" / "ourairports.csv") as f:
        for r in csv.DictReader(f):
            icao = r["icao_code"] or r["gps_code"] or r["ident"]
            if r["municipality"]:
                out[icao] = (MUNI_ALIAS.get(r["municipality"], r["municipality"]), r["iso_region"], r["type"])
            if r["iata_code"] and len(r["iata_code"]) == 3:
                iata[icao] = r["iata_code"]
    return out, iata


def fc_status(a, fc, covered: bool) -> str:
    if not covered:
        return "climatology"
    if fc and a["icao"] in set(fc.get("meta", {}).get("publicAirports", [])):
        return "live forecast"
    return "verifying"


def city_page(city, stations, primary, fc, window, pers, r10, now_utc, taf=None, obs=None) -> tuple[str, dict]:
    """One page per municipality; multi-airport cities aggregate their stations.
    The answer comes from the city's primary station (public > covered > large)."""
    muni, region, country, slug = city
    st = region.split("-")[-1] if country in SUFFIX_COUNTRIES else ""
    disp = f"{muni}, {st}" if st else muni
    ask = TITLE_ALIAS.get(muni, muni)  # how a searcher names the place
    url = f"{SITE}/fog/city/{slug}/"
    a = primary
    tz = ZoneInfo(a["tz"])
    covered = a["_covered"]
    subH = round(a["efvsHoursPerYear"] + a["belowHoursPerYear"])
    pk = peak_months(a["grid"])
    pk_txt = (" and ".join(MONTHS[m] for m in pk) if pk else "no month in particular")
    med = pers.get(a["icao"])
    med = {"medianH": med["medianH"], "p25H": med["p25H"], "p75H": med["p75H"], "n": med["n"]} if med else None
    sp = season_profile(a)
    public_now = bool(fc and a["icao"] in set(fc.get("meta", {}).get("publicAirports", [])))
    taf_s = taf_summary(taf, tz) if (taf and not public_now) else None
    answer_html, answer_plain, machine = bake_answer(a, fc, covered, now_utc, sp=sp, taf=taf_s)
    ob = obs_line(obs, tz) if obs else None
    if a["icao"] in MARINE_LAYER:
        ml = (f"{muni}'s famous \"fog\" is usually the marine layer — a low stratus deck at roughly 500–1,500 ft — while this page "
              f"measures dense ground fog (visibility under a mile or ceiling under 200 ft), which {a['icao']} records only "
              f"about {subH} hours a year. ")
        answer_html = f"<b>Marine layer ≠ dense fog.</b> {ml}" + answer_html
        answer_plain = ml + answer_plain
    src = f"Measured at {a['name']} ({a['icao']})."
    answer_plain_city = f"{answer_plain} {src}"
    season_html, season_plain = season_section(a, sp, med, disp)
    monthly = [month_hours(a["grid"], m) for m in range(12)]
    foggiest = max(range(12), key=lambda m: monthly[m])
    crumb_html, crumb_node = breadcrumb([("Fog Atlas", f"{SITE}/"), ("Cities", f"{SITE}/fog/city/"), (disp, None)])

    rows = "".join(
        f'<tr><td><a href="/fog/{s["icao"].lower()}/"><b>{s["icao"]}</b></a></td>'
        f"<td>{s['name']}</td><td>{round(s['efvsHoursPerYear'] + s['belowHoursPerYear'])} h/yr</td>"
        f"<td>{fc_status(s, fc, s['_covered'])}</td></tr>"
        for s in stations)

    faq = [
        {"@type": "Question", "name": f"Will it be foggy in {ask} tomorrow?",
         "acceptedAnswer": {"@type": "Answer", "text": answer_plain_city}},
        {"@type": "Question", "name": f"When is fog season in {ask}?",
         "acceptedAnswer": {"@type": "Answer",
                            "text": f"{season_plain} Measured at {a['name']}: about {subH} hours per year "
                                    f"below CAT I approach minima ({window['start'][:4]}–{window['through'][:4]} average)."}},
        {"@type": "Question", "name": f"Which month is foggiest in {ask}?",
         "acceptedAnswer": {"@type": "Answer",
                            "text": f"{MONTHS[foggiest]}, with about {monthly[foggiest]} hours below CAT I minima at {a['icao']} in a "
                                    f"typical {MONTHS[foggiest]}. By month: " + ", ".join(f"{MONTHS_S[m]} {monthly[m]} h" for m in range(12)) + "."}},
    ]
    if med:
        faq.append({"@type": "Question", "name": f"How long does fog last in {ask}?",
                    "acceptedAnswer": {"@type": "Answer",
                                       "text": f"Once fog forms it typically lasts about {med['medianH']} "
                                               f"hour{'s' if med['medianH'] != 1 else ''} here (middle half of "
                                               f"events: {med['p25H']}–{med['p75H']} h), from {med['n']} observed events at {a['icao']}."}})
    if ob:
        faq.append({"@type": "Question", "name": f"Is it foggy in {ask} right now?",
                    "acceptedAnswer": {"@type": "Answer",
                                       "text": f"At the latest observation before this page was built ({ob[1]['time']}) {a['icao']} reported visibility "
                                               f"{'unknown' if ob[1]['visibilityMi'] is None else str(ob[1]['visibilityMi']) + ' mi'}"
                                               f"{' with fog' if ob[1]['fog'] else ' with no fog'}. Live observations: {SITE}/api/metar?ids={a['icao']}."}})
    if a["icao"] in MARINE_LAYER:
        faq.append({"@type": "Question", "name": "Does this page cover the marine layer (Karl the Fog)?",
                    "acceptedAnswer": {"@type": "Answer",
                                       "text": "Only where it drops to the ground. The Bay Area marine layer is a stratus deck that usually sits at "
                                               "500–1,500 ft, which is why SFO records only about ten hours a year below CAT I approach minima; "
                                               "summer arrival delays come from that low ceiling, not from visibility."}})
    graph = [
        {"@type": "City", "@id": url + "#city", "name": muni,
         "address": {"@type": "PostalAddress", "addressCountry": country,
                     **({"addressRegion": st} if st else {})}},
        {"@type": "WebPage", "@id": url, "url": url, "dateModified": now_utc.strftime("%Y-%m-%d"),
         "isPartOf": {"@id": f"{SITE}/#site"}, "about": {"@id": url + "#city"}, "breadcrumb": crumb_node},
        {"@type": "FAQPage", "@id": url + "#faq", "mainEntity": faq},
        {"@type": "Dataset", "@id": url + "#data",
         "name": f"Fog forecast and fog climatology — {disp}",
         "description": f"Daily fog outlook and {window['start'][:4]}–{window['through'][:4]} fog climatology for {disp}, "
                        f"measured at {len(stations)} weather station{'s' if len(stations) != 1 else ''}.",
         "url": url, "temporalCoverage": f"{window['start']}/{window['through']}",
         "isAccessibleForFree": True,
         "creator": {"@type": "Organization", "name": "Fog Atlas", "url": SITE},
         "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                           "contentUrl": url + "data.json"}]},
    ]

    month_rows = "".join(f"<tr><td>{MONTHS[m]}</td><td>{monthly[m]} h</td></tr>" for m in range(12))
    html = f"""<!doctype html>
<html lang="en"><head>
{REDIRECT}
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Will it be foggy in {ask} tomorrow? {disp} fog forecast</title>
<meta name="description" content="{trunc(answer_plain, 150)} Fog season peaks {pk_txt}.">
<link rel="canonical" href="{url}">
<link rel="alternate" type="application/json" href="{url}data.json" title="{disp} fog data (JSON)">
<link rel="describedby" href="/llms.txt">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
{og_tags(url, f"Will it be foggy in {ask} tomorrow? {disp} fog forecast", answer_plain)}
<script type="application/ld+json">{json.dumps({"@context": "https://schema.org", "@graph": graph}, separators=(",", ":"))}</script>
<style>{CSS}</style>
</head><body><main>
{crumb_html}
<h1>Fog in <b>{disp}</b></h1>
<div class="sub">daily fog outlook &amp; {window['start'][:4]}–{window['through'][:4]} climatology · <a href="{SITE}/">live world fog map</a></div>
<div id="answer">{answer_html}</div>
{f'<div id="verdict">{ob[0]}</div>' if ob else ''}
<p class="ctx">{src} {'Forecast percentages for this station have passed live verification.' if machine.get('public') else ('This station is in forecast verification — percentages publish when it clears the accuracy bar.' if covered else 'This station has climatology, its official terminal forecast and live observations.')}</p>
<h2>Measuring station{'s' if len(stations) != 1 else ''}</h2>
<table><tr><th>station</th><th>airport</th><th>fog hours/yr</th><th>forecast</th></tr>{rows}</table>
<p class="note">Fog is measured where instruments live — airports. Hours/yr = time below CAT I approach minima (visibility under ~½ mile or ceiling under 200 ft), the aviation definition of seriously dense fog.</p>
<h2>When {ask} fogs in</h2>
<div class="blk">Fog here concentrates in <b>{pk_txt}</b> — about <b>{subH} hours</b> in a typical year at {a['icao']}{f", usually lasting ~{med['medianH']} h once it forms" if med else ""}. Hour-by-hour patterns, live conditions and the forecast strip live on the <a href="/fog/{a['icao'].lower()}/">{a['icao']} station page</a>.</div>
{season_html}
<table class="months"><tr><th>month</th><th>hours below CAT I at {a['icao']}</th></tr>{month_rows}</table>
<p class="note"><a href="{SITE}/fog/city/">All cities</a> · <a href="{SITE}/fog/">all airports</a> · <a href="{SITE}/methodology/">methodology</a> · <a href="{SITE}/about/">about</a>. Machine access: <a href="{url}data.json">data.json</a> · <a href="/llms.txt">llms.txt</a> · <a href="{SITE}/fog/scorecard/">forecast verification</a>. Not for operational use.</p>
{BEACON}
</main></body></html>"""

    data = {
        "schemaVersion": SCHEMA_VERSION,
        "city": muni, "region": region, "country": country, "slug": slug,
        "updated": now_utc.strftime("%Y-%m-%dT%H:%MZ"),
        "refreshCadence": "daily",
        "validThrough": (now_utc + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%MZ"),
        "currentObservation": ob[1] if ob else None,
        "window": {"start": window["start"], "through": window["through"]},
        "stations": [{"icao": s["icao"], "name": s["name"],
                      "subCat1HoursPerYear": round(s["efvsHoursPerYear"] + s["belowHoursPerYear"]),
                      "forecast": fc_status(s, fc, s["_covered"])} for s in stations],
        "primaryStation": a["icao"],
        "forecast": machine,
        "climatology": {"subCat1HoursPerYear": subH,
                        "fogSeasonPeakMonths": [MONTHS[m] for m in pk],
                        "medianEventHours": med["medianH"] if med else None,
                        "season": {"summary": season_plain,
                                   "months": [MONTHS[m] for m in sp.get("seasonMonths", [])],
                                   "timeOfDay": sp.get("bandTxt"),
                                   "typicallyClearsBy": sp.get("clearsBy")}},
        "links": {"page": url,
                  "stationPages": [f"{SITE}/fog/{s['icao'].lower()}/" for s in stations],
                  "verification": f"{SITE}/fog/scorecard/",
                  "methodology": f"{SITE}/methodology/",
                  "index": f"{SITE}/fog/city/index.json"},
    }
    return html, data


def llms_txt(n_airports, n_public, window, now_utc) -> str:
    return f"""# Fog Atlas

> Fog climatology and verified fog forecasts for {n_airports} airports worldwide,
> built for EFVS flight operations and anyone asking "will it be foggy tomorrow?".
> Climatology: hourly METAR observations {window['start']} to {window['through']}
> (Iowa Environmental Mesonet). Forecasts: NOAA/NWS National Blend of Models,
> recalibrated per airport and verified against live observations. Forecast
> percentages are published ONLY for airports whose calibrated model beat that
> airport's own climatology on live verification ({n_public} airports currently);
> all issued forecasts are logged and scored — receipts at /fog/scorecard/.

Updated {now_utc.strftime("%Y-%m-%d")}. Pages rebuild daily; the forecast API updates hourly.
Citation: link the airport page. Not for operational use.

## Per-airport pages (start here)
- [Airport index]({SITE}/fog/): every airport, linked; pattern {SITE}/fog/{{icao_lowercase}}/ (e.g. {SITE}/fog/ksfo/). The first block is today's answer in plain language; a facts box carries the climatology and approach-capability numbers
- [Airport machine index]({SITE}/fog/index.json): one row per airport — icao, iata, name, city, country, lat/lon, tz, fog hours/yr, peak months, forecast status, page and data.json URLs
- [Example airport data]({SITE}/fog/ksfo/data.json): the per-airport JSON contract (schemaVersion, identity, climatology incl. monthly hours and causes, approach capability, today's baked answer with threshold definition, latest observation, verification receipt, nearby airports, validThrough)

## City pages
- [City index]({SITE}/fog/city/): every city with a measuring station; pattern {SITE}/fog/city/{{slug}}/ — US/CA/AU slugs carry a region suffix ({SITE}/fog/city/san-francisco-ca/); each page names its measuring stations and takes its answer from the city's primary station
- [City machine index]({SITE}/fog/city/index.json): slug, city, region, country, stations, primary station, page and data.json URLs
- [Example city data]({SITE}/fog/city/san-francisco-ca/data.json)

## Regions, rankings and capability lists
- [Tule fog — California Central Valley]({SITE}/fog/region/central-valley/) and [North India winter fog]({SITE}/fog/region/north-india/): season, member airports, aggregated monthly hours
- [Foggiest airports in the world]({SITE}/fog/foggiest-airports/) and [foggiest US airports]({SITE}/fog/foggiest-us-airports/): ranked by hours/yr below CAT I minima, quality-gated
- [CAT III airports]({SITE}/fog/cat-iii-airports/), [CAT II airports]({SITE}/fog/cat-ii-airports/), [EFVS credit explained]({SITE}/fog/efvs/)

## Live APIs (JSON)
- [Forecast issuance]({SITE}/api/forecast): hourly calibrated issuance for all covered airports — meta.cycle, meta.publicAirports (the verified list), per-airport fhrs + p rows [P(vis<1mi), P(<0.5), P(<0.25), P(below CAT I)] in percent; calibrated values for airports NOT in meta.publicAirports are unverified and must not be quoted as Fog Atlas forecasts
- [Latest observations]({SITE}/api/metar?ids=KSFO): NOAA AWC passthrough, ids=ICAO[,ICAO]
- [Verification scorecard]({SITE}/api/scorecard): Brier skill vs climatology, pooled and per airport

## Optional
- [Verification & the publication bar]({SITE}/fog/scorecard/)
- [Methodology]({SITE}/methodology/): band definitions, sources, honesty ledger
- [About]({SITE}/about/)
- [Hourly climatology detail]({SITE}/data/detail/KSFO.json): 12x24 month-by-hour grids per airport
- [Sitemap index]({SITE}/sitemap.xml): priority, airports, cities
"""


def md_to_html(md: str) -> str:
    """Enough Markdown for README/METHODOLOGY: headings, paragraphs, lists,
    pipe tables, bold, code spans, links, blockquotes."""
    import html as _h
    def inline(s):
        s = _h.escape(s, quote=False)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        s = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
        return s
    out, para, table, lst = [], [], [], None
    def flush():
        nonlocal para, table, lst
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>"); para = []
        if table:
            head, *body = table
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>" +
                       "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</table>")
            table = []
        if lst:
            tag = lst[0]
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in lst[1]) + f"</{tag}>"); lst = None
    for line in md.splitlines():
        s = line.rstrip()
        if not s:
            flush(); continue
        if s.startswith("#"):
            flush(); lvl = min(len(s) - len(s.lstrip("#")), 3)
            out.append(f"<h{lvl}>{inline(s.lstrip('#').strip())}</h{lvl}>"); continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue
            para and flush(); table.append(cells); continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)", s)
        if m:
            para and flush()
            tag = "ol" if m.group(2)[0].isdigit() else "ul"
            if lst and lst[0] != tag:
                flush()
            lst = lst or (tag, [])
            lst[1].append(m.group(3)); continue
        if s.startswith(">"):
            flush(); out.append(f"<blockquote class=\"blk\">{inline(s.lstrip('> '))}</blockquote>"); continue
        if lst:
            lst[1][-1] += " " + s.strip(); continue
        para.append(s.strip())
    flush()
    return "\n".join(out)


def shell(url, title, desc, crumbs, body, jsonld_graph=None, now_utc=None) -> str:
    """Common chrome for the non-airport pages."""
    crumb_html, crumb_node = breadcrumb(crumbs)
    graph = [{"@type": "WebPage", "@id": url, "url": url, "name": title,
              "dateModified": (now_utc or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
              "isPartOf": {"@id": f"{SITE}/#site"}, "breadcrumb": crumb_node}] + (jsonld_graph or [])
    return f"""<!doctype html>
<html lang="en"><head>
{REDIRECT}
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{trunc(desc, 158)}">
<link rel="canonical" href="{url}">
<link rel="describedby" href="/llms.txt">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
{og_tags(url, title, desc)}
<script type="application/ld+json">{json.dumps({"@context": "https://schema.org", "@graph": graph}, separators=(",", ":"))}</script>
<style>{CSS}</style>
</head><body><main>
{crumb_html}
{body}
<p class="note"><a href="{SITE}/">Live world fog map</a> · <a href="{SITE}/fog/">all airports</a> · <a href="{SITE}/fog/city/">by city</a> · <a href="{SITE}/fog/scorecard/">forecast verification</a> · <a href="{SITE}/methodology/">methodology</a> · <a href="{SITE}/about/">about</a> · <a href="/llms.txt">llms.txt</a>. Not for operational use.</p>
{BEACON}
</main></body></html>"""


def doc_page(slug, title, md_path, desc, now_utc, extra_html="") -> str:
    md = md_path.read_text()
    body = md_to_html(md).replace("<h1>", '<h1 style="margin-top:8px">', 1)
    return shell(f"{SITE}/{slug}/", title, desc, [("Fog Atlas", f"{SITE}/"), (title.split(" — ")[0], None)],
                 body + extra_html, now_utc=now_utc)


REGIONS = [
    {"slug": "central-valley", "name": "California Central Valley", "phenomenon": "tule fog",
     "title": "Tule fog forecast & season — Central Valley, California airports",
     "stations": ["KFAT", "KBFL", "KSMF", "KSCK", "KMOD", "KVIS", "KMCE", "KRDD", "KSAC", "KMHR", "KMYV", "KPTV"],
     "intro": ("Tule fog is the Central Valley's dense winter radiation fog: it forms on calm, clear nights after the first "
               "autumn rains, pools in the valley floor, and can last for days — the deadliest weather hazard on California "
               "highways and the reason valley airports lose whole mornings from November through February. The table below "
               "is the ten-year airport record; the National Weather Service Hanford office publishes a seasonal Fog Severity "
               "Index for the valley and official forecasts, and every airport page here links its own terminal forecast."),
     "faq": [("When is tule fog season?", "November through February, peaking in December and January; dense fog is rare in the valley from April to September."),
             ("Which Central Valley airport is foggiest?", "See the ranked table on this page — hours per year below CAT I approach minima, ten-year average.")]},
    {"slug": "north-india", "name": "North India", "phenomenon": "winter fog",
     "title": "Delhi and North India fog season — winter fog at Indian airports",
     "stations": ["VIDP", "VIAR", "VILK", "VICG", "VIBY", "VEAB", "VIJP", "VIDN", "VEPT", "VIGR", "VIAG", "VIBN"],
     "intro": ("Every December and January a dense fog blanket settles over the Indo-Gangetic plain, from Amritsar and "
               "Chandigarh through Delhi to Lucknow and Patna, cancelling and diverting flights for days at a time. The "
               "ten-year airport record below shows how many hours each airport spends below CAT I approach minima, "
               "when the season starts and ends, and which airports have the CAT III approaches that keep operating."),
     "faq": [("When is fog season in Delhi?", "Mid-December through January, with dense fog mostly between midnight and mid-morning; November and February are the shoulder months."),
             ("Which North Indian airports have CAT III?", "The table marks each airport's ILS category; Delhi (VIDP) operates CAT III, which is why it keeps landing when smaller fields close.")]},
]

DEMAND_CITIES = ["san-francisco-ca", "fresno-ca", "bakersfield-ca", "sacramento-ca", "stockton-ca", "modesto-ca",
                 "new-delhi", "lahore", "dubai", "abu-dhabi", "london", "houston-tx", "seattle-wa", "portland-or",
                 "halifax-ns", "st-john-s-nl", "auckland", "melbourne-vic", "sydney-mascot-nsw", "amritsar", "chandigarh",
                 "lucknow", "bengaluru", "mumbai", "kolkata", "karachi", "islamabad", "milan", "munich", "vancouver-bc",
                 "boston-ma", "chicago-il", "atlanta-ga", "new-orleans-la", "denver-co", "salt-lake-city-ut", "spokane-wa"]

CITY_ALIASES = {"delhi": "new-delhi", "sydney": "sydney-mascot-nsw", "sydney-nsw": "sydney-mascot-nsw",
                "melbourne": "melbourne-vic", "san-francisco": "san-francisco-ca", "bangalore": "bengaluru",
                "bombay": "mumbai", "calcutta": "kolkata", "st-johns-nl": "st-john-s-nl", "sf": "san-francisco-ca"}


def list_page(url, title, h1, sub, intro, headers, rows, monthly_agg, faq, desc, crumbs, now_utc, data_extra) -> tuple[str, dict]:
    """Region / ranking / capability list: query-shaped title, in-season line,
    ranked table that IS the content, aggregated 12-month chart, FAQ."""
    faq_nodes = [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": t}} for q, t in faq]
    graph = [{"@type": "FAQPage", "@id": url + "#faq", "mainEntity": faq_nodes},
             {"@type": "Dataset", "@id": url + "#data", "name": title, "description": desc, "url": url,
              "isAccessibleForFree": True, "creator": {"@type": "Organization", "name": "Fog Atlas", "url": SITE},
              "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": url + "data.json"}]}]
    season_line = ""
    if monthly_agg and max(monthly_agg) > 0:
        mon = now_utc.month - 1
        pk = max(range(12), key=lambda m: monthly_agg[m])
        state = "in season" if monthly_agg[mon] >= 0.25 * monthly_agg[pk] else "off season"
        season_line = (f'<div class="blk"><b>{MONTHS[mon]} is {state}</b> — these airports together average about '
                       f'{monthly_agg[mon]:,} hours below CAT I minima in {MONTHS[mon]}, against {monthly_agg[pk]:,} in {MONTHS[pk]}, the peak month.</div>'
                       f'{season_svg(monthly_agg)}')
    table = ("<table class=\"rank\"><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>" +
             "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
    faq_html = "".join(f"<h2>{q}</h2><div class=\"blk\">{t}</div>" for q, t in faq)
    body = f"""<h1>{h1}</h1>
<div class="sub">{sub}</div>
<div class="blk">{intro}</div>
{season_line}
{table}
<p class="note">Hours/yr = time below CAT I approach minima (visibility under ~½ mile or ceiling under 200 ft), ten-year average of hourly observations. "Forecast" shows whether the airport's calibrated Fog Atlas percentage has passed live verification; every other airport carries official guidance and climatology only.</p>
{faq_html}
<p class="note">Machine access: <a href="{url}data.json">data.json</a>.</p>"""
    data = {"schemaVersion": SCHEMA_VERSION, "title": title, "url": url,
            "updated": now_utc.strftime("%Y-%m-%dT%H:%MZ"), "monthlyHoursAggregate": monthly_agg, **data_extra}
    return shell(url, title, desc, crumbs, body, graph, now_utc), data


def row_link(a, fc):
    icao = a["icao"]
    st = ("live forecast" if fc and icao in set(fc.get("meta", {}).get("publicAirports", []))
          else "verifying" if a.get("_covered") else "guidance")
    return f'<a href="/fog/{icao.lower()}/">{icao}</a>', a["name"], a["country"], \
        f"{round(a['efvsHoursPerYear'] + a['belowHoursPerYear'])}", \
        " and ".join(MONTHS_S[m] for m in peak_months(a["grid"])) or "—", \
        CAT_LABEL.get(a.get("catIls") or "", "CAT I"), st


def quality_ok(a) -> bool:
    return (a.get("reliability") in (None, "ok") and (a.get("coveragePct") or 0) >= 70
            and a.get("size") in ("large", "medium"))


def bake_lists(atlas, by_icao, fc, now_utc) -> list[tuple[str, str, dict]]:
    """(url, html, data) for region, ranking and capability pages."""
    out = []
    hdr = ["airport", "name", "country", "fog h/yr", "peak", "ILS", "forecast"]
    for r in REGIONS:
        members = [by_icao[i] for i in r["stations"] if i in by_icao]
        if not members:
            continue
        members.sort(key=lambda a: -(a["efvsHoursPerYear"] + a["belowHoursPerYear"]))
        agg = [sum(month_hours(a["grid"], m) for a in members) for m in range(12)]
        url = f"{SITE}/fog/region/{r['slug']}/"
        rows = [row_link(a, fc) for a in members]
        desc = (f"{r['phenomenon'].capitalize()} at {len(members)} {r['name']} airports: hours below CAT I minima per year, "
                f"season months, ILS category and today's forecast status, from ten years of observations.")
        html, data = list_page(url, r["title"], f"{r['phenomenon'].capitalize()} — <b>{r['name']}</b>",
                               f"{len(members)} airports · ten-year record · rebuilt daily", r["intro"], hdr, rows, agg, r["faq"],
                               desc, [("Fog Atlas", f"{SITE}/"), ("Regions", None), (r["name"], None)], now_utc,
                               {"region": r["slug"], "stations": [{"icao": a["icao"], "name": a["name"],
                                                                    "subCat1HoursPerYear": round(a["efvsHoursPerYear"] + a["belowHoursPerYear"]),
                                                                    "monthlyHours": [month_hours(a["grid"], m) for m in range(12)],
                                                                    "ilsCategory": a.get("catIls"), "page": f"{SITE}/fog/{a['icao'].lower()}/"} for a in members]})
        out.append((url, html, data))

    gated = [a for a in atlas if quality_ok(a)]
    for slug, title, h1, pool, faq in [
        ("foggiest-airports", "Foggiest airports in the world — ranked by hours below approach minima",
         "Foggiest airports in the <b>world</b>", gated,
         [("How is 'foggiest' measured?", "Hours per year below CAT I approach minima (visibility under about half a mile or ceiling under 200 ft), from ten years of hourly observations; airports with thin or suspect observation records and small fields are excluded so the ranking reflects real operations."),
          ("Why isn't San Francisco on the list?", "SFO's famous fog is a marine stratus deck at 500–1,500 ft; it records only about ten hours a year below approach minima. Its delays come from the low ceiling, not from visibility.")]),
        ("foggiest-us-airports", "Foggiest airports in the United States — ranked by hours below approach minima",
         "Foggiest <b>US</b> airports", [a for a in gated if a["country"] == "US"],
         [("Which US airport has the most fog?", "See the ranked table — hours per year below CAT I minima, ten-year average, quality-gated to airports with reliable observation records."),
          ("When do US airports fog in?", "Coastal Pacific Northwest and New England fog peaks in summer (advection fog); the Central Valley, the Southeast and the Midwest peak from November to February (radiation fog).")]),
    ]:
        pool = sorted(pool, key=lambda a: -(a["efvsHoursPerYear"] + a["belowHoursPerYear"]))[:50]
        agg = [sum(month_hours(a["grid"], m) for a in pool) for m in range(12)]
        url = f"{SITE}/fog/{slug}/"
        rows = [(i + 1, *row_link(a, fc)) for i, a in enumerate(pool)]
        desc = f"The {len(pool)} airports with the most hours below CAT I approach minima per year, with peak months, ILS category and forecast status."
        html, data = list_page(url, title, h1, f"top {len(pool)} · sub-CAT-I hours per year · ten-year record",
                               "Ranked by hours per year below CAT I approach minima — the aviation definition of seriously dense fog — from ten years of hourly METAR observations. Quality-gated: only medium and large airports with at least 70% observation coverage and no suspect-reporting flag.",
                               ["#"] + hdr, rows, agg, faq, desc,
                               [("Fog Atlas", f"{SITE}/"), ("Rankings", None), (h1.replace("<b>", "").replace("</b>", ""), None)], now_utc,
                               {"ranking": slug, "airports": [{"rank": i + 1, "icao": a["icao"], "name": a["name"], "country": a["country"],
                                                               "subCat1HoursPerYear": round(a["efvsHoursPerYear"] + a["belowHoursPerYear"]),
                                                               "ilsCategory": a.get("catIls")} for i, a in enumerate(pool)]})
        out.append((url, html, data))

    for cat, slug, title in [("CATIII", "cat-iii-airports", "CAT III airports — every airport with CAT III ILS approaches, by country"),
                             ("CATII", "cat-ii-airports", "CAT II airports — every airport with CAT II ILS approaches, by country")]:
        pool = sorted([a for a in atlas if a.get("catIls") == cat], key=lambda a: (a["country"], -(a["efvsHoursPerYear"] + a["belowHoursPerYear"])))
        url = f"{SITE}/fog/{slug}/"
        rows = [row_link(a, fc) for a in pool]
        lab = CAT_LABEL[cat]
        desc = f"{len(pool)} airports with {lab} ILS approaches on record, grouped by country, with each airport's fog hours per year and source (FAA NASR, FAA OpSpec C060, national AIP)."
        faq = [(f"What does {lab} mean?", "ILS categories set how low an approach may continue: CAT I to a 200 ft decision height, CAT II to 100 ft, CAT III to 50 ft or lower with autoland. A CAT III airport keeps landing in fog that closes a CAT I field."),
               ("How was this list built?", "US airports from the FAA NASR ILS database; foreign airports from the FAA OpSpec C060 list of facilities approved for US-carrier CAT II/III operations, supplemented by national AIP research with a confidence tag on each airport page. An airport can have CAT II/III capability not used by US carriers and be missing here.")]
        html, data = list_page(url, title, f"<b>{lab}</b> airports", f"{len(pool)} airports · by country",
                               f"Every airport in the atlas whose best ILS category on record is {lab}. The fog hours column is why the category exists: it shows how much of the year each airport would otherwise spend below CAT I minima.",
                               hdr, rows, [sum(month_hours(a["grid"], m) for a in pool) for m in range(12)], faq, desc,
                               [("Fog Atlas", f"{SITE}/"), ("Capability", None), (lab, None)], now_utc,
                               {"category": cat, "airports": [{"icao": a["icao"], "name": a["name"], "country": a["country"],
                                                              "source": a.get("catConfidence"),
                                                              "subCat1HoursPerYear": round(a["efvsHoursPerYear"] + a["belowHoursPerYear"])} for a in pool]})
        out.append((url, html, data))

    pool = sorted([a for a in gated if (a.get("catIls") or "") not in ("CATII", "CATIII") and a.get("efvsOppHoursPerYear")],
                  key=lambda a: -a["efvsOppHoursPerYear"])[:50]
    url = f"{SITE}/fog/efvs/"
    rows = [(i + 1, f'<a href="/fog/{a["icao"].lower()}/">{a["icao"]}</a>', a["name"], a["country"],
             f"{a['efvsOppHoursPerYear']:g}", f"{round(a['efvsHoursPerYear'] + a['belowHoursPerYear'])}",
             CAT_LABEL.get(a.get("catIls") or "", "CAT I")) for i, a in enumerate(pool)]
    intro = ("An Enhanced Flight Vision System (EFVS) lets a suitably equipped aircraft continue an approach in visibility that would "
             "otherwise force a diversion, by imaging the runway environment with infrared sensors on a head-up display. Under FAA 14 CFR 91.176 "
             "and equivalent EASA rules the credit is worth the most at airports that are frequently fogbound <b>and</b> lack CAT II/III ILS — "
             "where a conventionally equipped aircraft has no lower minima to fall back on. This table ranks those airports by the hours per year "
             "an EFVS-equipped aircraft flying to CAT I minima could recover (visibility between roughly 300 m and the airport's CAT I floor). "
             "Validation: US DOT/BTS on-time data (13.9M departures, 2023–24) shows flights scheduled during EFVS-recoverable hours were "
             "weather-cancelled at 5.2× the baseline rate — read as validation that these hours are operationally hostile, not as a fog-specific cost model.")
    faq = [("What is EFVS operational credit?", "Regulatory permission (FAA 91.176, EASA equivalents) to descend below published minima and, with the right equipment and training, to land using the enhanced vision image instead of natural vision of the runway environment."),
           ("Where is EFVS worth the most?", "At airports with many sub-CAT-I hours and no CAT II/III ILS: without the deck-side credit those hours are diversions. The ranking above is that intersection, computed per airport from ten years of observations and published approach capability.")]
    html, data = list_page(url, "EFVS credit explained — where enhanced flight vision recovers the most fog hours",
                           "<b>EFVS</b> — where it earns its keep", f"top {len(pool)} CAT I / no-ILS airports by EFVS-recoverable hours",
                           intro, ["#", "airport", "name", "country", "EFVS-recoverable h/yr", "sub-CAT-I h/yr", "ILS"], rows,
                           [sum(month_hours(a["grid"], m) for a in pool) for m in range(12)], faq,
                           "Enhanced Flight Vision Systems explained, with the airports where the credit recovers the most fog hours per year — foggy fields without CAT II/III ILS.",
                           [("Fog Atlas", f"{SITE}/"), ("EFVS", None)], now_utc,
                           {"ranking": "efvs", "airports": [{"rank": i + 1, "icao": a["icao"], "name": a["name"], "country": a["country"],
                                                            "efvsRecoverableHoursPerYear": a["efvsOppHoursPerYear"],
                                                            "subCat1HoursPerYear": round(a["efvsHoursPerYear"] + a["belowHoursPerYear"])} for i, a in enumerate(pool)]})
    out.append((url, html, data))
    return out


def load_scorecard():
    """The newer of the live /api/scorecard (edge-cached for an hour on a
    fixed key — stale for up to an hour after barcheck.yml writes KV) and the
    committed report barcheck commits alongside it. The receipts page and the
    per-airport receipt lines render from this at bake, no JS required."""
    cands = []
    try:
        req = urllib.request.Request(f"{SITE}/api/scorecard", headers={"User-Agent": "fogatlas-build"})
        with urllib.request.urlopen(req, timeout=20) as r:
            sc = json.load(r)
            if sc.get("bar"):
                cands.append((sc.get("generated", ""), sc, "live"))
    except Exception as e:
        print(f"  live scorecard unavailable ({e})")
    p = HERE / "out" / "shadow_report.json"
    if p.exists():
        sc = json.load(open(p))
        cands.append((sc.get("generated", ""), sc, "committed"))
    if not cands:
        return None, None
    _, sc, src = max(cands, key=lambda c: c[0])
    return sc, src


def scorecard_page(sc, fc, now_utc) -> str:
    public = list(fc.get("meta", {}).get("publicAirports", [])) if fc else []
    if sc:
        gen = sc.get("generated", "")[:10]
        age = (now_utc.date() - datetime.fromisoformat(gen).date()).days if gen else None
        passes = sorted(sc["bar"].get("pass", []), key=lambda r: -r["skill_v10"])
        fails = sc["bar"].get("fail", [])
        pass_set = {r["icao"] for r in passes}
        holds = [i for i in public if i not in pass_set]
        t = sc.get("thresholds", {}).get("v10", {})
        stale = f' <b class="tag" style="color:var(--amber)">last refreshed {age} days ago</b>' if age is not None and age > 40 else ""
        status = (f'<b>Scored through {gen}</b> · {sc.get("runs", 0):,} hourly issuances · <b>{len(passes)} airports</b> pass the '
                  f'pre-registered bar · {len(fails)} fail · {sc["bar"].get("insufficient_n", "—")} still accumulating evidence · '
                  f'<b>{len(public)} airports</b> currently publish calibrated percentages'
                  f'{f" (includes {len(holds)} marginal airports the owner is holding pending re-judgment on a longer record)" if holds else ""}.{stale}')
        pooled = (f'<div class="blk">Pooled, all airports: {t.get("n", 0):,} verified pairs · model Brier {t.get("brier_model", 0):.5f} vs '
                  f'climatology {t.get("brier_clim", 0):.5f} (<b>{t.get("skill_pct", 0):.1f}% better</b>) on the fog headline threshold '
                  f'(visibility under 1 mile).</div>') if t else ""
        table = ("<table><tr><th>airport</th><th>verified pairs</th><th>fog hours observed</th><th>skill vs climatology (vis &lt; 1 mi)</th><th>skill (sub-CAT-I)</th></tr>" +
                 "".join(f'<tr><td><a href="/fog/{r["icao"].lower()}/">{r["icao"]}</a></td><td>{r["n"]:,}</td><td>{r["events_v10"]}</td>'
                         f'<td>{r["skill_v10"]:+.1f}%</td><td>{r.get("skill_sub", 0):+.1f}%</td></tr>' for r in passes) + "</table>")
    else:
        status = "Scorecard unavailable at build time — the live table below loads when JavaScript runs."
        pooled = table = ""
    sc_graph = [{"@type": "Dataset", "@id": f"{SITE}/fog/scorecard/#data", "name": "Fog Atlas forecast verification scorecard",
                 "description": "Per-airport Brier skill of the calibrated fog forecast versus climatology on live verification; the publication bar.",
                 "url": f"{SITE}/fog/scorecard/", "isAccessibleForFree": True,
                 "creator": {"@type": "Organization", "name": "Fog Atlas", "url": SITE},
                 "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{SITE}/api/scorecard"}]}]
    body = f"""<h1>Forecast verification</h1>
<div class="sub">the receipt, not the promise</div>
<div class="blk">Every forecast this site issues is <b>logged at issuance</b> and later scored against what the airport's weather station actually reported. No forecast probability appears publicly for an airport until its calibrated model <b>beats that airport's own 10-year climatology</b> on Brier score over live verification — a pre-registered bar, not a vibe.</div>
<div class="blk" id="sc-status">{status}</div>
<div id="sc-detail">{pooled}{table}</div>
<p class="note">Method: guidance from the NOAA/NWS National Blend of Models, recalibrated per airport against ten years of METAR truth at four thresholds (vis &lt; 1 mi / ½ mi / ¼ mi, and below-CAT-I). Verification obs come from the same live feed the maps use. Bar rule: at least 3,000 verified forecast/outcome pairs and 10 observed event-hours, then the calibrated model must beat that airport's own climatology on Brier score at both the public and the pro threshold. Marginal failures (within ±5% of zero) are held rather than whipsawed; decisively negative records are demoted. <a href="{SITE}/methodology/">Full methodology</a> · <a href="{SITE}/api/scorecard">scorecard JSON</a>.</p>
<script>
(async () => {{
  try {{
    const sc = await (await fetch("/api/scorecard")).json();
    if (!sc.thresholds || !(sc.bar && sc.bar.pass && sc.bar.pass.length)) return;
    const built = "{sc.get('generated', '')[:10] if sc else ''}";
    if ((sc.generated || "").slice(0, 10) <= built) return;  // static copy is as fresh
    document.getElementById("sc-status").innerHTML = `<b>Scored through ${{sc.generated.slice(0, 10)}}</b> (live) · ${{sc.runs}} hourly issuances · <b>${{sc.bar.pass.length}} airports</b> pass the bar.`;
  }} catch (e) {{ /* static copy stands */ }}
}})();
</script>"""
    return shell(f"{SITE}/fog/scorecard/", "Forecast verification — Fog Atlas",
                 "How the Fog Atlas fog forecasts are scored: every issued probability is logged and verified against what actually happened; per-airport receipts.",
                 [("Fog Atlas", f"{SITE}/"), ("Verification", None)], body, sc_graph, now_utc)


def write_404_and_redirects(iata_by_icao: dict, covered: set, atlas_icaos: set, slugs: set,
                            priority=(), by_size=None):
    by_size = by_size or {}
    """Real 404 (Pages otherwise SPA-falls-back every unknown path to the map
    shell with HTTP 200) + IATA/city alias 301s. _redirects is capped at 2,000
    static lines by Cloudflare, so covered airports go there; the full IATA map
    rides inline in 404.html for everything else."""
    lines = []
    for alias, target in CITY_ALIASES.items():  # few, and the ones searchers type — first
        if target in slugs and alias not in slugs:
            lines.append(f"/fog/city/{alias}/ /fog/city/{target}/ 301")
            lines.append(f"/fog/city/{alias} /fog/city/{target}/ 301")
    # Pages does NOT match the trailing-slash variant of a source (verified
    # live 2026-09-12: /fog/sfo → 301, /fog/sfo/ → 404), and the file is
    # capped at 2,000 lines: the airports people actually type get both
    # forms, the rest of the covered set gets the bare form, and the inline
    # map in 404.html rescues everything else client-side
    both = {i for i in covered if by_size.get(i) == "large"} | set(priority)
    for icao in sorted(both | covered):
        i = iata_by_icao.get(icao)
        if i and icao in atlas_icaos:
            lines.append(f"/fog/{i.lower()} /fog/{icao.lower()}/ 301")
            if icao in both:
                lines.append(f"/fog/{i.lower()}/ /fog/{icao.lower()}/ 301")
    if len(lines) > 1990:
        print(f"  _redirects: {len(lines)} lines exceeds the cap — truncating to 1990")
        lines = lines[:1990]
    (APP_PUB / "_redirects").write_text("\n".join(lines) + "\n")
    full = {v.lower(): k.lower() for k, v in iata_by_icao.items() if k in atlas_icaos}
    (APP_PUB / "404.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not found — Fog Atlas</title><meta name="robots" content="noindex">
<link rel="icon" type="image/svg+xml" href="/favicon.svg"><style>{CSS}</style>
<script>
(function(){{
  var p = location.pathname, m;
  if (p.indexOf('/fog/') === 0 && p !== p.toLowerCase()) {{ location.replace(p.toLowerCase() + location.search + location.hash); return; }}
  var M = {json.dumps(full, separators=(",", ":"))};
  var C = {json.dumps(CITY_ALIASES, separators=(",", ":"))};
  if ((m = p.match(/^\\/fog\\/([a-z0-9]{{3}})\\/?$/)) && M[m[1]]) {{ location.replace('/fog/' + M[m[1]] + '/'); return; }}
  if ((m = p.match(/^\\/fog\\/city\\/([a-z0-9-]+)\\/?$/)) && C[m[1]]) {{ location.replace('/fog/city/' + C[m[1]] + '/'); return; }}
}})();
</script></head><body><main>
<h1>Not found</h1>
<div class="sub">that page is not in the atlas</div>
<div class="blk">Airport pages live at <b>/fog/&lt;icao&gt;/</b> (lower-case ICAO, e.g. <a href="/fog/ksfo/">/fog/ksfo/</a>) and city pages at <b>/fog/city/&lt;city&gt;/</b> (e.g. <a href="/fog/city/san-francisco-ca/">/fog/city/san-francisco-ca/</a>). Three-letter IATA codes and common city spellings redirect automatically.</div>
<p class="note"><a href="/">Live world fog map</a> · <a href="/fog/">all airports</a> · <a href="/fog/city/">all cities</a> · <a href="/fog/scorecard/">forecast verification</a> · <a href="/llms.txt">machine-readable guide</a></p>
{BEACON}
</main></body></html>""")
    return len(lines)


def write_sitemaps(entries: dict, now_utc):
    """entries: name -> [(url, lastmod)]; sitemap.xml becomes an index with the
    priority child first so its coverage reports on its own in GSC/Bing."""
    today = now_utc.strftime("%Y-%m-%d")
    for name, urls in entries.items():
        with open(APP_PUB / f"sitemap-{name}.xml", "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
            for u, lm in urls:
                f.write(f"<url><loc>{u}</loc><lastmod>{lm}</lastmod></url>\n")
            f.write("</urlset>\n")
    with open(APP_PUB / "sitemap.xml", "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for name in entries:
            f.write(f"<sitemap><loc>{SITE}/sitemap-{name}.xml</loc><lastmod>{today}</lastmod></sitemap>\n")
        f.write("</sitemapindex>\n")


def main() -> None:
    atlas_doc = read_json(PIPE_OUT / "app" / "airports.json", APP_DATA / "airports.json")
    atlas = atlas_doc["airports"]
    window = atlas_doc.get("window", {"start": "2016-01-01", "through": "2025-12-31"})
    chase = read_json(APP_DATA / "chase.json")["airports"]
    stations = set(json.load(open(HERE / "stations.json")))
    pers = read_json(PIPE_OUT / "persistence.json", APP_DATA / "persistence.json")
    fc = load_forecast()
    now_utc = datetime.now(timezone.utc)
    public_set = set(fc.get("meta", {}).get("publicAirports", [])) if fc else set()

    import duckdb
    r10 = {}
    for icao, m, h, r in duckdb.connect().execute(
            f"SELECT icao, mon, hr, r10 FROM '{HERE / 'out' / 'climo.parquet'}'").fetchall():
        r10.setdefault(icao, {})[(m, h)] = r

    # cities: group atlas airports by municipality; slug collisions go to the
    # city with more stations, losers are skipped (logged) — slugs stay stable
    munis, iata_by_icao = load_municipalities()
    for a in atlas:
        a["_covered"] = a["icao"] in stations
    by_icao = {a["icao"]: a for a in atlas}
    nearby = nearby_index(atlas)

    # official attributed layers: TAFs for every non-public station (the
    # airport's own terminal forecast), latest METAR for every station
    all_icaos = [a["icao"] for a in atlas]
    tafs = awc_batch("taf", [i for i in all_icaos if i not in public_set])
    obs = awc_batch("metar", all_icaos)
    tafs, obs = awc_with_last_good(tafs, obs, now_utc)

    sc, sc_src = load_scorecard()
    receipts = {}
    if sc:
        for r in sc["bar"].get("pass", []):
            receipts[r["icao"]] = {**{k: r[k] for k in ("n", "events_v10", "skill_v10") if k in r},
                                   "skill_sub": r.get("skill_sub"), "scoredThrough": sc.get("generated", "")[:10]}
    groups: dict = {}
    for a in atlas:
        m = munis.get(a["icao"])
        if m:
            groups.setdefault((m[0], m[1], a["country"]), []).append(a)
    for g in groups.values():
        g.sort(key=lambda s: (not s["_covered"] or not (fc and s["icao"] in set(fc.get("meta", {}).get("publicAirports", []))),
                              not s["_covered"],
                              munis[s["icao"]][2] != "large_airport",
                              -(s["efvsHoursPerYear"] + s["belowHoursPerYear"])))
    slugs: dict = {}
    skipped = 0
    for key, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        muni, region, country = key
        base = slugify(muni)
        if not base:
            continue
        slug = f"{base}-{region.split('-')[-1].lower()}" if country in SUFFIX_COUNTRIES else base
        if slug in slugs:
            skipped += 1
            continue
        slugs[slug] = key
    city_of_icao = {s["icao"]: (key[0], f"{SITE}/fog/city/{slug}/")
                    for slug, key in slugs.items() for s in groups[key]}

    FOG.mkdir(parents=True, exist_ok=True)
    (FOG / "_fog.js").write_text(FOG_JS)
    today = now_utc.strftime("%Y-%m-%d")
    month1 = now_utc.strftime("%Y-%m-01")
    n = n_baked = n_taf = 0
    links, air_lastmod, air_index = [], {}, []
    for a in atlas:
        icao = a["icao"]
        d = FOG / icao.lower()
        d.mkdir(exist_ok=True)
        html, data = page(a, chase.get(icao), icao in stations, r10, fc, window, pers, now_utc,
                          city_link=city_of_icao.get(icao), iata=iata_by_icao.get(icao),
                          taf=tafs.get(icao), obs=obs.get(icao), nearby=nearby.get(icao, ()),
                          receipt=receipts.get(icao))
        d.joinpath("index.html").write_text(html)
        d.joinpath("data.json").write_text(json.dumps(data, separators=(",", ":")))
        if data["forecast"].get("public"):
            n_baked += 1
        if data["forecast"].get("taf"):
            n_taf += 1
        # honest lastmod: a page whose answer text changed today (forecast feed,
        # TAF or observation) says so; a station with none of those changes
        # only when the month sentence rolls over
        fresh = a["_covered"] or bool(data["forecast"].get("taf")) or bool(data.get("currentObservation"))
        air_lastmod[icao] = today if fresh else month1
        links.append((icao, a["name"], a["country"]))
        air_index.append({"icao": icao, "iata": iata_by_icao.get(icao), "name": a["name"],
                          "city": city_of_icao.get(icao, (None,))[0], "country": a["country"],
                          "lat": a["lat"], "lon": a["lon"], "tz": a["tz"],
                          "subCat1HoursPerYear": round(a["efvsHoursPerYear"] + a["belowHoursPerYear"]),
                          "fogSeasonPeakMonths": [MONTHS[m] for m in peak_months(a["grid"])],
                          "ilsCategory": a.get("catIls"),
                          "forecastStatus": "public" if icao in public_set else "verifying" if a["_covered"] else "none",
                          "page": f"{SITE}/fog/{icao.lower()}/", "data": f"{SITE}/fog/{icao.lower()}/data.json"})
        n += 1
    (FOG / "index.json").write_text(json.dumps({"schemaVersion": SCHEMA_VERSION, "updated": now_utc.strftime("%Y-%m-%dT%H:%MZ"),
                                                 "count": n, "airports": air_index}, separators=(",", ":")))

    city_links, city_lastmod, city_index, city_primary = [], {}, [], {}
    CITY = FOG / "city"
    CITY.mkdir(exist_ok=True)
    for slug, key in slugs.items():
        g = groups[key]
        d = CITY / slug
        d.mkdir(exist_ok=True)
        prim = g[0]["icao"]
        html, data = city_page((key[0], key[1], key[2], slug), g, g[0], fc, window, pers, r10, now_utc,
                               taf=tafs.get(prim), obs=obs.get(prim))
        d.joinpath("index.html").write_text(html)
        d.joinpath("data.json").write_text(json.dumps(data, separators=(",", ":")))
        city_links.append((key[0], key[1].split("-")[-1] if key[2] in SUFFIX_COUNTRIES else key[2], slug))
        city_lastmod[slug] = air_lastmod.get(prim, month1)
        city_primary[slug] = prim
        city_index.append({"slug": slug, "city": key[0], "region": key[1], "country": key[2],
                           "stations": [s["icao"] for s in g], "primaryStation": prim,
                           "page": f"{SITE}/fog/city/{slug}/", "data": f"{SITE}/fog/city/{slug}/data.json"})
    city_links.sort()
    (CITY / "index.json").write_text(json.dumps({"schemaVersion": SCHEMA_VERSION, "updated": now_utc.strftime("%Y-%m-%dT%H:%MZ"),
                                                  "count": len(city_index), "cities": city_index}, separators=(",", ":")))
    city_rows = "".join(f'<a href="/fog/city/{s}/" style="display:inline-block;min-width:11em">{m} <span class="tag">{r}</span></a>'
                        for m, r, s in city_links)
    (CITY / "index.html").write_text(shell(
        f"{SITE}/fog/city/", "City fog forecasts — will it be foggy tomorrow in your city? — Fog Atlas",
        f"Will it be foggy tomorrow? Daily fog outlooks for {len(city_links)} cities worldwide, measured at their airports and verified publicly.",
        [("Fog Atlas", f"{SITE}/"), ("Cities", None)],
        f"""<h1>City fog forecasts</h1>
<div class="sub">{len(city_links)} cities · rebuilt daily · <a href="/fog/">by airport</a> · <a href="/fog/city/index.json">machine index</a></div>
<div class="blk">Every city with an airport weather station gets a daily answer to "will it be foggy tomorrow?", a ten-year fog season profile and its stations' live observations. Regions and rankings: <a href="/fog/region/central-valley/">tule fog (Central Valley)</a> · <a href="/fog/region/north-india/">North India winter fog</a> · <a href="/fog/foggiest-airports/">foggiest airports</a>.</div>
<div class="note" style="line-height:2.2">{city_rows}</div>""", now_utc=now_utc))

    links.sort()
    idx_rows = "".join(f'<a href="/fog/{i.lower()}/" style="display:inline-block;width:5.2em">{i}</a>' for i, _, _ in links)
    (FOG / "index.html").write_text(shell(
        f"{SITE}/fog/", "Airport fog forecasts — daily outlook and 10-year fog climatology for every airport — Fog Atlas",
        f"Will it be foggy tomorrow? Daily fog outlooks and 10-year fog climatology for {n} airports worldwide, with public verification.",
        [("Fog Atlas", f"{SITE}/"), ("Airports", None)],
        f"""<h1>Airport fog forecasts</h1>
<div class="sub">{n} airports · rebuilt daily · <a href="/fog/city/">by city</a> · <a href="/fog/index.json">machine index</a> · <a href="/llms.txt">machine guide</a></div>
<div class="blk">Rankings and lists: <a href="/fog/foggiest-airports/">foggiest airports in the world</a> · <a href="/fog/foggiest-us-airports/">foggiest US airports</a> · <a href="/fog/cat-iii-airports/">CAT III airports</a> · <a href="/fog/cat-ii-airports/">CAT II airports</a> · <a href="/fog/efvs/">EFVS credit explained</a>. Regions: <a href="/fog/region/central-valley/">Central Valley tule fog</a> · <a href="/fog/region/north-india/">North India winter fog</a>.</div>
<div class="note" style="line-height:2.2">{idx_rows}</div>""", now_utc=now_utc))

    # region / ranking / capability pages, about + methodology, scorecard
    extra_urls = []
    for url, html, data in bake_lists(atlas, by_icao, fc, now_utc):
        d = APP_PUB / url[len(SITE) + 1:]
        d.mkdir(parents=True, exist_ok=True)
        d.joinpath("index.html").write_text(html)
        d.joinpath("data.json").write_text(json.dumps(data, separators=(",", ":")))
        extra_urls.append(url)
    root = HERE.parent
    about_extra = ("<h2>Who makes this</h2><p>Fog Atlas is built and run by Travis Danner as an open, public-data reference — "
                   f"the pipeline and methodology are on <a href=\"https://github.com/travis735/fog-atlas\">GitHub</a>. "
                   "Every forecast the site issues is logged and publicly scored on the <a href=\"/fog/scorecard/\">verification page</a>. "
                   "Not for operational use.</p>")
    for slug, title, path, desc, extra in [
        ("about", "About Fog Atlas — who makes it, what it shows, and its honest limits",
         root / "README.md", "What Fog Atlas is: ten years of airport weather observations distilled into fog climatology and verified fog forecasts, who makes it, its data sources and honest limitations.", about_extra),
        ("methodology", "Methodology & assumptions — how Fog Atlas measures and forecasts fog",
         root / "METHODOLOGY.md", "Every approximation in Fog Atlas, listed: observation basis, visibility and ceiling bands, persistence statistics, the nowcast and calibrated forecast models, cause attribution, EFVS opportunity, CAT II/III sources and the cancellation validation.", ""),
    ]:
        d = APP_PUB / slug
        d.mkdir(exist_ok=True)
        d.joinpath("index.html").write_text(doc_page(slug, title, path, desc, now_utc, extra))
        extra_urls.append(f"{SITE}/{slug}/")

    scd = FOG / "scorecard"
    scd.mkdir(exist_ok=True)
    scd.joinpath("index.html").write_text(scorecard_page(sc, fc, now_utc))

    demand_primaries = {city_primary[s] for s in DEMAND_CITIES if s in city_primary}
    n_redirects = write_404_and_redirects(iata_by_icao, stations | demand_primaries, set(by_icao), set(slugs),
                                          priority=public_set | demand_primaries,
                                          by_size={a["icao"]: a.get("size") for a in atlas})

    # sitemaps: priority child first (public cohort + their cities + demand
    # cities + lists + docs), then airports, then cities; lastmod is honest
    prio = [(f"{SITE}/", today), (f"{SITE}/fog/", today), (f"{SITE}/fog/city/", today), (f"{SITE}/fog/scorecard/", today)]
    prio += [(u, today) for u in extra_urls]
    prio_set = set()
    for icao in sorted(public_set):
        if icao in by_icao:
            prio.append((f"{SITE}/fog/{icao.lower()}/", air_lastmod[icao])); prio_set.add(icao)
            c = city_of_icao.get(icao)
            if c:
                prio.append((c[1], today))
    for slug in DEMAND_CITIES:
        if slug in city_primary:
            prio.append((f"{SITE}/fog/city/{slug}/", city_lastmod[slug]))
            p = city_primary[slug]
            if p not in prio_set:
                prio.append((f"{SITE}/fog/{p.lower()}/", air_lastmod[p])); prio_set.add(p)
    seen, prio_u = set(), []
    for u, lm in prio:
        if u not in seen:
            seen.add(u); prio_u.append((u, lm))
    write_sitemaps({
        "priority": prio_u,
        "airports": [(f"{SITE}/fog/{i.lower()}/", air_lastmod[i]) for i, _, _ in links if f"{SITE}/fog/{i.lower()}/" not in seen],
        "cities": [(f"{SITE}/fog/city/{s}/", city_lastmod[s]) for _, _, s in city_links if f"{SITE}/fog/city/{s}/" not in seen],
    }, now_utc)

    robots = ["User-agent: *", "Allow: /", ""]
    for ua in AI_CRAWLERS:  # explicit welcome — this site WANTS to be an AI source
        robots += [f"User-agent: {ua}", "Allow: /", ""]
    robots += [f"Sitemap: {SITE}/sitemap.xml", f"# LLM/agent guide: {SITE}/llms.txt"]
    (APP_PUB / "robots.txt").write_text("\n".join(robots) + "\n")
    (APP_PUB / "llms.txt").write_text(llms_txt(n, len(public_set), window, now_utc))

    # uniqueness metric: share of an airport page's words that are page-specific
    # (appear on <=10% of a sample) — printed so wording changes are measured
    import random
    sample = random.Random(7).sample(links, min(80, len(links)))
    docs = []
    for i, _, _ in sample:
        txt = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", (FOG / i.lower() / "index.html").read_text(), flags=re.S)
        docs.append(set(w for w in re.findall(r"[a-z]{3,}", txt.lower())))
    df = {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    shares = [sum(1 for w in d if df[w] <= len(docs) * 0.1) / max(1, len(d)) for d in docs]
    uniq = 100 * sorted(shares)[len(shares) // 2]
    summary = (f"wrote {n} airport pages ({n_baked} public forecasts, {n_taf} with TAF, {len(obs)} with observation) + "
               f"{len(city_links)} city pages ({skipped} slug collisions skipped) + {len(extra_urls)} list/doc pages + "
               f"scorecard ({sc_src or 'none'}) + {n_redirects} redirects + sitemap index ({len(prio_u)} priority URLs) + "
               f"robots + llms.txt · median page-specific word share {uniq:.1f}%")
    print(summary)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"- pages: {summary}\n")


if __name__ == "__main__":
    main()
