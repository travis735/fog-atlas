#!/usr/bin/env python3
"""Generate the static per-airport fog pages (/fog/{icao}/) + sitemap.

One page per atlas airport (3,394). Crawler-stable content: name, the
climatology story, runway infrastructure (where chase.json knows it), and
methodology links. Live content (current conditions, forecast strip) hydrates
client-side from /api/metar + /api/forecast via /fog/_fog.js.

Shadow-mode honesty: pages NEVER print model probabilities while
/api/forecast meta.public is false — the client shows NWS-guidance-derived
wording + climatology until the per-airport bar clears.

Output: app/public/fog/{icao}/index.html (lowercase), /fog/index.html,
/fog/_fog.js, app/public/sitemap.xml, app/public/robots.txt
"""
import json
import math
from pathlib import Path

HERE = Path(__file__).parent
APP_PUB = HERE.parent / "app" / "public"
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
#verdict{background:var(--panel);border:1px solid var(--hair);border-radius:12px;padding:16px 18px;font-size:1.05rem;margin:14px 0}
#verdict b{color:var(--amber)}#verdict .ok{color:#7fd49a}
#strip{margin:14px 0 4px}#strip svg{width:100%;height:auto;display:block}
.striplab{color:var(--dim);font-size:.72rem;display:flex;justify-content:space-between}
.ctx{color:var(--dim);font-size:.85rem;margin:10px 0 26px}
h2{font-size:.8rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);margin:30px 0 8px;border-top:1px solid var(--hair);padding-top:22px}
table{border-collapse:collapse;width:100%;font-size:.82rem}td,th{text-align:left;padding:5px 8px;border-bottom:1px solid var(--hair)}th{color:var(--dim);font-weight:500}
.note{color:var(--dim);font-size:.78rem;line-height:1.6;margin-top:10px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.blk{background:var(--panel);border:1px solid var(--hair);border-radius:10px;padding:12px 14px;margin:8px 0;font-size:.85rem}
.tag{display:inline-block;border:1px solid var(--hair);border-radius:5px;padding:1px 7px;font-size:.72rem;color:var(--dim);margin-left:6px}
"""


def month_hours(grid, mon: int) -> int:
    days = [31, 28.2, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mon]
    return round(sum(grid[mon]) / 100.0 * days)


def peak_months(grid) -> list[int]:
    tot = [(m, sum(grid[m])) for m in range(12)]
    tot.sort(key=lambda x: -x[1])
    return [m for m, s in tot[:2] if s > 0]


def page(a, ends, covered: bool, r10_by_mh) -> str:
    icao, name = a["icao"], a["name"]
    grid = a["grid"]
    subH = round(a["efvsHoursPerYear"] + a["belowHoursPerYear"])
    pk = peak_months(grid)
    pk_txt = (" and ".join(MONTHS[m] for m in pk) if pk else "no month in particular")
    # per-month hourly rates for the embedded climatology strip (current month
    # chosen client-side): public threshold where we have it, else sub-CAT-I
    if covered and icao in r10_by_mh:
        clim = [[round(100 * r10_by_mh[icao].get((m + 1, h), 0.0), 1) for h in range(24)] for m in range(12)]
        clim_label = "vis < 1 mile"
    else:
        clim = [[grid[m][h] for h in range(24)] for m in range(12)]
        clim_label = "below CAT I (~½ mi / 200 ft)"

    rwy_rows = "".join(
        f"<tr><td><b>{e['e']}</b></td><td>{ALS_LABEL.get(e['als'] or '', e['als'] or '—')}</td>"
        f"<td>{('CAT ' + e['ils']) if e['ils'] else ('LPV' if e['lpv'] else '—')}</td>"
        f"<td>{e['len']:,} ft</td><td>{'RVR' if e['rvr'] else '—'}</td></tr>"
        for e in (ends or []))
    rwy_html = f"""
  <h2>Runway infrastructure</h2>
  <table><tr><th>end</th><th>approach lights</th><th>best approach</th><th>length</th><th>RVR</th></tr>{rwy_rows}</table>
  <p class="note">From FAA NASR / curated AIP Canada research. Approach-light class and minima tier drive how low an approach can be flown — details in the <a href="{SITE}/#chase">chase board</a>.</p>""" if rwy_rows else ""

    fc_note = ("" if covered else
               "<p class='note'>No NWS guidance feed exists for this station — this page shows measured climatology and live conditions only.</p>")

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{icao} — {name} fog forecast · tonight, tomorrow &amp; this week</title>
<meta name="description" content="Will there be fog at {name} ({icao})? Hour-by-hour fog outlook, live conditions, and a 10-year fog climatology — {subH} low-visibility hours in a typical year, peaking in {pk_txt}.">
<link rel="canonical" href="{SITE}/fog/{icao.lower()}/">
<meta property="og:title" content="{icao} fog forecast — {name}">
<meta property="og:description" content="Hour-by-hour fog outlook and 10-year climatology for {name}.">
<style>{CSS}</style>
</head><body><main>
<h1><b>{icao}</b> — {name}</h1>
<div class="sub">fog forecast &amp; climatology · <a href="{SITE}/#{icao}">full 10-year analysis →</a></div>
<div id="verdict">Loading current conditions…</div>
<div id="strip"></div>
<p class="ctx" id="ctx"></p>
{fc_note}
<h2>When this airport fogs in</h2>
<div class="blk">In a typical year {name} spends <b>{subH} hours</b> below CAT I approach minima (visibility under ~½ mile or ceiling under 200 ft), concentrated in <b>{pk_txt}</b>. The strip above shows the hour-by-hour pattern for the current month from ten years of weather observations.</div>
{rwy_html}
<h2>For flight operations</h2>
<p class="note">EFVS crews: the <a href="{SITE}/#chase">CHASE board</a> ranks airports by live fog status, approach lighting, go-around height and flight time from your base. Forecast probabilities publish here per-airport once the calibrated model beats climatology on live verification — until then this page shows NWS guidance wording and measured climatology only. <a href="{SITE}/#methodology">Methodology &amp; verification</a>.</p>
<p class="note">Sources: NOAA/NWS National Blend of Models guidance · 10 years of METAR observations (Iowa Environmental Mesonet) · FAA NASR. Not for operational use.</p>
<script>window.__FOG={{icao:"{icao}",clim:{json.dumps(clim)},climLabel:"{clim_label}",covered:{str(covered).lower()},tz:"{a['tz']}"}}</script>
<script src="/fog/_fog.js" defer></script>
</main></body></html>"""


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

  // live conditions
  try {
    const arr = await (await fetch(`/api/metar?ids=${S.icao}&hours=3`)).json();
    const ob = arr && arr[0];
    if (ob) {
      const vis = typeof ob.visib === "string" ? parseFloat(ob.visib) || 10 : ob.visib;
      const foggy = vis != null && vis < 1.0;
      const when = (ob.rawOb || "").match(/\d{6}Z/) || [""];
      $("verdict").innerHTML = foggy
        ? `<b>Fog now</b> — visibility ${vis} mile${vis === 1 ? "" : "s"} at ${S.icao} (${when[0]}).`
        : `<span class="ok">No fog right now</span> — visibility ${vis == null ? "unknown" : vis + " mi"} at ${S.icao} (${when[0]}).`;
    }
  } catch (e) { $("verdict").textContent = "Live conditions unavailable."; }

  // forecast layer — probabilities only when the engine says public
  if (!S.covered) return;
  try {
    const fc = await (await fetch("/api/forecast")).json();
    const a = fc.airports && fc.airports[S.icao];
    if (!a) return;
    if (fc.meta && fc.meta.public === true) {
      const peakP = Math.max(...a.p.map((r) => r[0]));
      const idx = a.p.findIndex((r) => r[0] === peakP);
      const at = new Date(new Date(fc.meta.cycle).getTime() + a.fhrs[idx] * 3600e3);
      const t = at.toLocaleString("en-US", { weekday: "short", hour: "numeric", timeZone: S.tz });
      $("verdict").innerHTML += peakP >= 20
        ? ` <b>Fog likely ${t}</b> — ${peakP}% chance of visibility under a mile.`
        : ` Next 48 h: fog unlikely (peak ${peakP}%).`;
    } else {
      // shadow mode: NWS guidance wording, no model percentages
      const minVis = Math.min(...a.vis.filter((v) => v != null));
      const maxLiv = Math.max(...a.liv.filter((v) => v != null), 0);
      if (minVis < 1 || maxLiv >= 40)
        $("verdict").innerHTML += " NWS guidance suggests <b>fog is possible in the next 48 hours</b> — calibrated probabilities publish here after verification.";
    }
  } catch (e) { /* forecast layer optional */ }
})();
"""


def main() -> None:
    atlas = json.load(open(HERE.parent / "pipeline" / "out" / "app" / "airports.json"))["airports"]
    chase = json.load(open(HERE.parent / "app" / "public" / "data" / "chase.json"))["airports"]
    stations = set(json.load(open(HERE / "stations.json")))
    import duckdb
    r10 = {}
    for icao, m, h, r in duckdb.connect().execute(
            f"SELECT icao, mon, hr, r10 FROM '{HERE / 'out' / 'climo.parquet'}'").fetchall():
        r10.setdefault(icao, {})[(m, h)] = r

    FOG.mkdir(parents=True, exist_ok=True)
    (FOG / "_fog.js").write_text(FOG_JS)
    n = 0
    links = []
    for a in atlas:
        icao = a["icao"]
        d = FOG / icao.lower()
        d.mkdir(exist_ok=True)
        d.joinpath("index.html").write_text(
            page(a, chase.get(icao), icao in stations, r10))
        links.append((icao, a["name"], a["country"]))
        n += 1

    links.sort()
    idx_rows = "".join(f'<a href="/fog/{i.lower()}/" style="display:inline-block;width:5.2em">{i}</a>' for i, _, _ in links)
    (FOG / "index.html").write_text(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Airport fog forecasts — Fog Atlas</title>
<meta name="description" content="Hour-by-hour fog outlooks and 10-year fog climatology for {n} airports worldwide.">
<link rel="canonical" href="{SITE}/fog/"><style>{CSS}</style></head><body><main>
<h1>Airport fog forecasts</h1>
<div class="sub">{n} airports · <a href="{SITE}/">the atlas →</a></div>
<div class="note" style="line-height:2.2">{idx_rows}</div>
</main></body></html>""")

    sc = FOG / "scorecard"
    sc.mkdir(exist_ok=True)
    sc.joinpath("index.html").write_text(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Forecast verification — Fog Atlas</title>
<meta name="description" content="How the Fog Atlas fog forecasts are scored: every issued probability is logged and verified against what actually happened.">
<link rel="canonical" href="{SITE}/fog/scorecard/"><style>{CSS}</style></head><body><main>
<h1>Forecast verification</h1>
<div class="sub">the receipt, not the promise</div>
<div class="blk">Every forecast this site issues is <b>logged at issuance</b> and later scored against what the airport's weather station actually reported. No forecast probability appears publicly for an airport until its calibrated model <b>beats that airport's own 10-year climatology</b> on Brier score over live verification — a pre-registered bar, not a vibe.</div>
<div class="blk"><b>Status: shadow mode</b> — the engine has been issuing and logging hourly forecasts since <b>2026-07-21</b>. Scores publish here, per airport, as verification accumulates (typically 3–4 weeks). Airports that never clear the bar simply never show percentages.</div>
<p class="note">Method: guidance from the NOAA/NWS National Blend of Models, recalibrated per airport against ten years of METAR truth at four thresholds (vis &lt; 1 mi / ½ mi / ¼ mi, and below-CAT-I). Verification obs come from the same live feed the maps use. <a href="{SITE}/#methodology">Full methodology</a>.</p>
</main></body></html>""")

    with open(APP_PUB / "sitemap.xml", "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        f.write(f"<url><loc>{SITE}/</loc></url>\n<url><loc>{SITE}/fog/</loc></url>\n")
        for i, _, _ in links:
            f.write(f"<url><loc>{SITE}/fog/{i.lower()}/</loc></url>\n")
        f.write("</urlset>\n")
    (APP_PUB / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n")
    print(f"wrote {n} airport pages + index + sitemap")


if __name__ == "__main__":
    main()
