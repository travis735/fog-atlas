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
import json
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
"""

AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-Web",
               "anthropic-ai", "PerplexityBot", "Google-Extended", "Applebot-Extended",
               "CCBot", "cohere-ai", "meta-externalagent"]


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


def month_hours(grid, mon: int) -> int:
    days = [31, 28.2, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mon]
    return round(sum(grid[mon]) / 100.0 * days)


def peak_months(grid) -> list[int]:
    tot = [(m, sum(grid[m])) for m in range(12)]
    tot.sort(key=lambda x: -x[1])
    return [m for m, s in tot[:2] if s > 0]


def bake_answer(a, fc, covered: bool, now_utc):
    """(html, plain, machine) for the static answer block. Never prints model
    percentages unless the airport is on the public (bar-passed) list."""
    icao, tz = a["icao"], ZoneInfo(a["tz"])
    mon = now_utc.astimezone(tz).month - 1
    mh = month_hours(a["grid"], mon)
    clim_txt = (f"{MONTHS[mon]} typically brings about {mh} hour{'s' if mh != 1 else ''} of dense fog here"
                if mh >= 1 else f"{MONTHS[mon]} is usually fog-free here")

    fa = fc and fc.get("airports", {}).get(icao)
    if not covered:
        plain = f"No fog forecast feed exists for this station. {clim_txt} (10-year average)."
        return (plain, plain, {"covered": False, "public": False, "wording": plain})
    if not fa:  # covered, but absent from the newest issuance (feed initializing)
        plain = f"Forecast feed initializing for this station. {clim_txt} (10-year average)."
        return (plain, plain,
                {"covered": True, "public": False, "inIssuance": False, "wording": plain})

    public = icao in set(fc.get("meta", {}).get("publicAirports", []))
    cyc = datetime.fromisoformat(fc["meta"]["cycle"].replace("Z", "+00:00"))
    asof = f'<span class="asof">forecast cycle {fc["meta"]["cycle"]} · page rebuilt daily · live layers below update hourly</span>'
    horizon = [(fh, r[0]) for fh, r in zip(fa["fhrs"], fa["p"]) if fh <= 36]

    if public and horizon:
        peak_fh, peak_p = max(horizon, key=lambda t: t[1])
        peak_at = (cyc + timedelta(hours=peak_fh)).astimezone(tz)
        day = peak_at.strftime("%A")
        hr = peak_at.strftime("%-I %p").lower()
        thr = max(15, peak_p * 0.4)
        win = [fh for fh, p in horizon if p >= thr]
        machine = {"covered": True, "public": True, "cycle": fc["meta"]["cycle"],
                   "peakPct": peak_p, "peakAtLocal": peak_at.isoformat()}
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
            plain = (f"No, fog is unlikely in the next 36 hours — the calibrated forecast "
                     f"peaks at just {peak_p}%. {clim_txt}.")
            html = f'<span class="ok">Fog unlikely</span> through {day} (peak {peak_p}%). {clim_txt}.'
        machine["wording"] = plain
        return html + asof, plain, machine

    # covered but shadow: guidance wording, no model percentages (the bar rule)
    vis = [v for v in fa.get("vis", []) if v is not None]
    liv = [v for v in fa.get("liv", []) if v is not None]
    signal = (vis and min(vis) < 1.0) or (liv and max(liv) >= 40)
    if signal:
        plain = ("NWS guidance shows a fog signal here in the next 48 hours. This station's "
                 "calibrated percentages publish after live verification clears the accuracy bar. "
                 f"{clim_txt}.")
        html = f"<b>Fog possible</b> — NWS guidance shows a fog signal in the next 48 hours. {clim_txt}."
    else:
        plain = f"No fog signal in NWS guidance for the next 48 hours. {clim_txt}."
        html = f'<span class="ok">No fog signal</span> in NWS guidance for the next 48 hours. {clim_txt}.'
    return (html + asof, plain,
            {"covered": True, "public": False, "cycle": fc["meta"]["cycle"],
             "guidanceSignal": bool(signal), "wording": plain})


def jsonld(a, plain_answer, pk_txt, subH, window, med) -> str:
    icao, name = a["icao"], a["name"]
    url = f"{SITE}/fog/{icao.lower()}/"
    faq = [
        {"@type": "Question", "name": f"Will it be foggy at {name} tomorrow?",
         "acceptedAnswer": {"@type": "Answer", "text": plain_answer}},
        {"@type": "Question", "name": f"When is fog season at {name}?",
         "acceptedAnswer": {"@type": "Answer",
                            "text": f"Fog at {name} concentrates in {pk_txt}: about {subH} hours per year "
                                    f"fall below CAT I approach minima (visibility under about half a mile "
                                    f"or ceiling under 200 ft), based on {window['start'][:4]}–{window['through'][:4]} observations."}},
    ]
    if med:
        faq.append({"@type": "Question", "name": f"How long does fog usually last at {name}?",
                    "acceptedAnswer": {"@type": "Answer",
                                       "text": f"Once fog forms at {name} it typically lasts about {med['medianH']} "
                                               f"hour{'s' if med['medianH'] != 1 else ''} (middle half of events: "
                                               f"{med['p25H']}–{med['p75H']} h), from {med['n']} observed fog events."}})
    graph = [
        {"@type": "Airport", "@id": url + "#airport", "name": name, "icaoCode": icao,
         "geo": {"@type": "GeoCoordinates", "latitude": a["lat"], "longitude": a["lon"]},
         "address": {"@type": "PostalAddress", "addressCountry": a["country"]}},
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


def page(a, ends, covered, r10_by_mh, fc, window, pers, now_utc) -> tuple[str, dict]:
    icao, name = a["icao"], a["name"]
    grid = a["grid"]
    subH = round(a["efvsHoursPerYear"] + a["belowHoursPerYear"])
    pk = peak_months(grid)
    pk_txt = (" and ".join(MONTHS[m] for m in pk) if pk else "no month in particular")
    med = pers.get(icao)
    med = {"medianH": med["medianH"], "p25H": med["p25H"], "p75H": med["p75H"], "n": med["n"]} if med else None

    answer_html, answer_plain, machine = bake_answer(a, fc, covered, now_utc)

    if covered and icao in r10_by_mh:
        clim = [[round(100 * r10_by_mh[icao].get((m + 1, h), 0.0), 1) for h in range(24)] for m in range(12)]
        clim_label = "vis < 1 mile"
    else:
        clim = [[grid[m][h] for h in range(24)] for m in range(12)]
        clim_label = "below CAT I (~½ mi / 200 ft)"

    monthly = [month_hours(grid, m) for m in range(12)]
    facts = f"""
  <dl class="facts" id="facts">
    <dt>Fog hours per year</dt><dd data-fact="subCat1HoursPerYear">{subH} h below CAT I minima ({window['start'][:4]}–{window['through'][:4]} average)</dd>
    <dt>Fog season</dt><dd data-fact="fogSeason">{pk_txt}</dd>
    {f'<dt>Typical fog event</dt><dd data-fact="medianEventH">~{med["medianH"]} h once it forms (middle half {med["p25H"]}–{med["p75H"]} h, n={med["n"]})</dd>' if med else ''}
    <dt>Data through</dt><dd data-fact="dataThrough">{window['through']} · <a href="data.json">machine-readable data.json</a></dd>
  </dl>"""

    rwy_rows = "".join(
        f"<tr><td><b>{e['e']}</b></td><td>{ALS_LABEL.get(e['als'] or '', e['als'] or '—')}</td>"
        f"<td>{('CAT ' + e['ils']) if e['ils'] else ('LPV' if e['lpv'] else '—')}</td>"
        f"<td>{e['len']:,} ft</td><td>{'RVR' if e['rvr'] else '—'}</td></tr>"
        for e in (ends or []))
    rwy_html = f"""
  <h2>Runway infrastructure</h2>
  <table><tr><th>end</th><th>approach lights</th><th>best approach</th><th>length</th><th>RVR</th></tr>{rwy_rows}</table>
  <p class="note">From FAA NASR / curated AIP Canada research. Approach-light class and minima tier drive how low an approach can be flown — details in the <a href="{SITE}/#chase">chase board</a>.</p>""" if rwy_rows else ""

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} fog forecast — will it be foggy tomorrow? ({icao})</title>
<meta name="description" content="{answer_plain[:150].replace('"', "'")} 10-year fog climatology: {subH} low-visibility hours/yr, season peaks {pk_txt}.">
<link rel="canonical" href="{SITE}/fog/{icao.lower()}/">
<link rel="alternate" type="application/json" href="{SITE}/fog/{icao.lower()}/data.json" title="{icao} fog data (JSON)">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="{icao} fog forecast — {name}">
<meta property="og:description" content="{answer_plain[:190].replace('"', "'")}">
<script type="application/ld+json">{jsonld(a, answer_plain, pk_txt, subH, window, med)}</script>
<style>{CSS}</style>
</head><body><main>
<h1><b>{icao}</b> — {name}</h1>
<div class="sub">fog forecast &amp; climatology · <a href="{SITE}/#{icao}">full 10-year analysis →</a></div>
<div id="answer">{answer_html}</div>
<div id="verdict" hidden></div>
<div id="strip"></div>
<p class="ctx" id="ctx"></p>
{facts}
<h2>When this airport fogs in</h2>
<div class="blk">In a typical year {name} spends <b>{subH} hours</b> below CAT I approach minima (visibility under ~½ mile or ceiling under 200 ft), concentrated in <b>{pk_txt}</b>. The strip above shows the hour-by-hour pattern for the current month from ten years of weather observations.</div>
{rwy_html}
<h2>For flight operations</h2>
<p class="note">EFVS crews: the <a href="{SITE}/#chase">CHASE board</a> ranks airports by live fog status, approach lighting, go-around height and flight time from your base. Forecast probabilities publish here per-airport once the calibrated model beats climatology on live verification — receipts on the <a href="{SITE}/fog/scorecard/">scorecard</a>. <a href="{SITE}/#methodology">Methodology</a>.</p>
<p class="note">Sources: NOAA/NWS National Blend of Models guidance · METAR observations {window['start'][:4]}–{window['through'][:4]} (Iowa Environmental Mesonet) · FAA NASR. Machine access: <a href="/fog/{icao.lower()}/data.json">data.json</a> · <a href="/llms.txt">llms.txt</a>. Not for operational use.</p>
<script>window.__FOG={{icao:"{icao}",clim:{json.dumps(clim)},climLabel:"{clim_label}",covered:{str(covered).lower()},tz:"{a['tz']}"}}</script>
<script src="/fog/_fog.js" defer></script>
</main></body></html>"""

    data = {
        "icao": icao, "name": name, "lat": a["lat"], "lon": a["lon"],
        "country": a["country"], "tz": a["tz"],
        "updated": now_utc.strftime("%Y-%m-%dT%H:%MZ"),
        "window": {"start": window["start"], "through": window["through"]},
        "climatology": {
            "subCat1HoursPerYear": subH,
            "bandDefinition": "visibility < ~800 m OR ceiling < 200 ft",
            "fogSeasonPeakMonths": [MONTHS[m] for m in pk],
            "monthlyHours": monthly,
            "medianEventHours": med["medianH"] if med else None,
        },
        "forecast": machine,
        "links": {
            "page": f"{SITE}/fog/{icao.lower()}/",
            "liveForecastApi": f"{SITE}/api/forecast",
            "liveObservationsApi": f"{SITE}/api/metar?ids={icao}",
            "hourlyClimatologyDetail": f"{SITE}/data/detail/{icao}.json",
            "verification": f"{SITE}/fog/scorecard/",
            "methodology": f"{SITE}/#methodology",
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
- [Airport index](/fog/): all airports, linked
- Page pattern: /fog/{{icao_lowercase}}/ — e.g. /fog/ksfo/ — the first block is
  the current answer in plain language; a facts box carries climatology numbers
- Machine data: /fog/{{icao_lowercase}}/data.json — stable JSON per airport:
  identity (icao/lat/lon/tz), climatology (hours/yr, monthly hours, fog season,
  median event duration), the current baked forecast answer, and API links

## Live APIs (JSON unless noted)
- [/api/forecast](/api/forecast): hourly calibrated issuance, all covered
  airports — meta.cycle, meta.publicAirports (the verified list), per-airport
  fhrs + p rows [P(vis<1mi), P(<0.5), P(<0.25), P(below CAT I)] in percent
- [/api/metar?ids=ICAO](/api/metar): latest observations (NOAA AWC passthrough)
- [/api/scorecard](/api/scorecard): live verification — Brier skill vs
  climatology, pooled and per passed airport

## Reference
- [Verification & the publication bar](/fog/scorecard/)
- [Methodology](/#methodology) (in-app section; band definitions, honesty ledger)
- [Hourly climatology detail](/data/detail/KSFO.json): 12x24 month-by-hour grids per airport
- [Sitemap](/sitemap.xml)
"""


def main() -> None:
    atlas_doc = read_json(PIPE_OUT / "app" / "airports.json", APP_DATA / "airports.json")
    atlas = atlas_doc["airports"]
    window = atlas_doc.get("window", {"start": "2016-01-01", "through": "2025-12-31"})
    chase = read_json(APP_DATA / "chase.json")["airports"]
    stations = set(json.load(open(HERE / "stations.json")))
    pers = read_json(PIPE_OUT / "persistence.json", APP_DATA / "persistence.json")
    fc = load_forecast()
    now_utc = datetime.now(timezone.utc)

    import duckdb
    r10 = {}
    for icao, m, h, r in duckdb.connect().execute(
            f"SELECT icao, mon, hr, r10 FROM '{HERE / 'out' / 'climo.parquet'}'").fetchall():
        r10.setdefault(icao, {})[(m, h)] = r

    FOG.mkdir(parents=True, exist_ok=True)
    (FOG / "_fog.js").write_text(FOG_JS)
    n = n_baked = 0
    links = []
    for a in atlas:
        icao = a["icao"]
        d = FOG / icao.lower()
        d.mkdir(exist_ok=True)
        html, data = page(a, chase.get(icao), icao in stations, r10, fc, window, pers, now_utc)
        d.joinpath("index.html").write_text(html)
        d.joinpath("data.json").write_text(json.dumps(data, separators=(",", ":")))
        if data["forecast"].get("public"):
            n_baked += 1
        links.append((icao, a["name"], a["country"]))
        n += 1

    links.sort()
    idx_rows = "".join(f'<a href="/fog/{i.lower()}/" style="display:inline-block;width:5.2em">{i}</a>' for i, _, _ in links)
    (FOG / "index.html").write_text(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Airport fog forecasts — Fog Atlas</title>
<meta name="description" content="Will it be foggy tomorrow? Daily fog outlooks and 10-year fog climatology for {n} airports worldwide, with public verification.">
<link rel="canonical" href="{SITE}/fog/"><link rel="icon" type="image/svg+xml" href="/favicon.svg"><style>{CSS}</style></head><body><main>
<h1>Airport fog forecasts</h1>
<div class="sub">{n} airports · rebuilt daily · <a href="{SITE}/">the atlas →</a> · <a href="/llms.txt">machine guide</a></div>
<div class="note" style="line-height:2.2">{idx_rows}</div>
</main></body></html>""")

    sc = FOG / "scorecard"
    sc.mkdir(exist_ok=True)
    sc.joinpath("index.html").write_text(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forecast verification — Fog Atlas</title>
<meta name="description" content="How the Fog Atlas fog forecasts are scored: every issued probability is logged and verified against what actually happened.">
<link rel="canonical" href="{SITE}/fog/scorecard/"><link rel="icon" type="image/svg+xml" href="/favicon.svg"><style>{CSS}</style></head><body><main>
<h1>Forecast verification</h1>
<div class="sub">the receipt, not the promise</div>
<div class="blk">Every forecast this site issues is <b>logged at issuance</b> and later scored against what the airport's weather station actually reported. No forecast probability appears publicly for an airport until its calibrated model <b>beats that airport's own 10-year climatology</b> on Brier score over live verification — a pre-registered bar, not a vibe.</div>
<div class="blk" id="sc-status"><b>Status: shadow mode</b> — the engine has been issuing and logging hourly forecasts since <b>2026-07-21</b>. Scores publish here, per airport, as verification accumulates. Airports that never clear the bar simply never show percentages.</div>
<div id="sc-detail"></div>
<p class="note">Method: guidance from the NOAA/NWS National Blend of Models, recalibrated per airport against ten years of METAR truth at four thresholds (vis &lt; 1 mi / ½ mi / ¼ mi, and below-CAT-I). Verification obs come from the same live feed the maps use. Bar rule: at least 3,000 verified forecast/outcome pairs and 10 observed event-hours, then the calibrated model must beat that airport's own climatology on Brier score at both the public and the pro threshold. <a href="{SITE}/#methodology">Full methodology</a>.</p>
<script>
(async () => {{
  try {{
    const sc = await (await fetch("/api/scorecard")).json();
    if (!sc.thresholds) return;
    const bar = sc.bar || {{}};
    const pass = bar.pass || [];
    document.getElementById("sc-status").innerHTML =
      `<b>Status: ${{pass.length ? pass.length + " airports live" : "shadow mode"}}</b> — ` +
      `${{sc.runs}} hourly issuances scored through ${{(sc.generated || "").slice(0, 10)}}. ` +
      `${{pass.length ? "Airports below cleared the pre-registered bar and now show calibrated percentages; all others remain shadow." : "No airport has cleared the bar yet."}}`;
    const t = sc.thresholds.v10 || {{}};
    let html = `<div class="blk">Pooled, all airports: ${{(t.n || 0).toLocaleString()}} verified pairs · ` +
      `model Brier ${{(t.brier_model || 0).toFixed(5)}} vs climatology ${{(t.brier_clim || 0).toFixed(5)}} ` +
      `(<b>${{(t.skill_pct || 0).toFixed(1)}}% better</b>) on the fog headline threshold.</div>`;
    if (pass.length) {{
      html += `<table><tr><th>airport</th><th>verified pairs</th><th>event hours</th><th>skill vs climatology</th></tr>` +
        pass.map((r) => `<tr><td><a href="/fog/${{r.icao.toLowerCase()}}/">${{r.icao}}</a></td>` +
          `<td>${{r.n.toLocaleString()}}</td><td>${{r.events_v10}}</td><td>+${{r.skill_v10}}%</td></tr>`).join("") + `</table>`;
    }}
    document.getElementById("sc-detail").innerHTML = html;
  }} catch (e) {{ /* static copy stands */ }}
}})();
</script>
</main></body></html>""")

    today = now_utc.strftime("%Y-%m-%d")
    with open(APP_PUB / "sitemap.xml", "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        f.write(f"<url><loc>{SITE}/</loc></url>\n<url><loc>{SITE}/fog/</loc><lastmod>{today}</lastmod></url>\n")
        for i, _, _ in links:
            f.write(f"<url><loc>{SITE}/fog/{i.lower()}/</loc><lastmod>{today}</lastmod></url>\n")
        f.write("</urlset>\n")

    robots = ["User-agent: *", "Allow: /", ""]
    for ua in AI_CRAWLERS:  # explicit welcome — this site WANTS to be an AI source
        robots += [f"User-agent: {ua}", "Allow: /", ""]
    robots += [f"Sitemap: {SITE}/sitemap.xml", f"# LLM/agent guide: {SITE}/llms.txt"]
    (APP_PUB / "robots.txt").write_text("\n".join(robots) + "\n")
    (APP_PUB / "llms.txt").write_text(
        llms_txt(n, len(fc["meta"].get("publicAirports", [])) if fc else 0, window, now_utc))

    print(f"wrote {n} airport pages + data.json ({n_baked} with baked public forecasts) + index + scorecard + sitemap + robots + llms.txt")


if __name__ == "__main__":
    main()
