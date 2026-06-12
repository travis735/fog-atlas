import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import * as Plot from "@observablehq/plot";
import "./style.css";

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const MONTHS_S = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

interface Airport {
  icao: string; name: string; lat: number; lon: number; country: string; tz: string;
  catIls: string; catConfidence: string; size?: string; coveragePct?: number;
  reliability?: string; ils?: string; lpv?: string; floorM?: number;
  efvsOppHoursPerYear?: number;
  floors?: { cat1: number; hud?: number; cat2: number; cat3: number };
  efvsOppByEquip?: { cat1: number; hud?: number; cat2: number; cat3: number };
  efvsHoursPerYear: number; belowHoursPerYear: number;
  causes: Record<string, number>;
  grid: number[][]; // [month][hour] sub-CAT-I %
}

const state = {
  months: [0] as number[], hr: 6, playing: false,
  ilsOnly: false, lpvOnly: false,
  equip: "cat1" as "cat1" | "hud" | "cat2" | "cat3",
  airports: [] as Airport[],
};

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const hrEl = $<HTMLInputElement>("#hr");
const readout = $("#readout");
const panel = $("#panel");
const panelContent = $("#panel-content");

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: [15, 30],
  zoom: 1.6,
  minZoom: 1,
  attributionControl: { compact: true },
});

// scrub value = mean over the selected month window at the scrubbed hour
function pctExpr(): any {
  const terms = state.months.map((m) => ["at", m * 24 + state.hr, ["get", "g"]]);
  if (terms.length === 1) return terms[0];
  return ["/", ["+", ...terms], terms.length];
}
function windowAvg(grid: number[]): number {
  const s = state.months.reduce((acc, m) => acc + (grid[m * 24 + state.hr] ?? 0), 0);
  return Math.round((s / state.months.length) * 10) / 10;
}
function windowLabel(): string {
  const ms = state.months;
  if (ms.length === 12) return "All year";
  if (ms.length === 1) return MONTHS[ms[0]];
  // detect a contiguous run, allowing wraparound (e.g. Nov-Feb)
  const set = new Set(ms);
  for (const start of ms) {
    let len = 1;
    while (len < ms.length && set.has((start + len) % 12)) len++;
    if (len === ms.length && !set.has((start + 11) % 12))
      return `${MONTHS_S[start]}–${MONTHS_S[(start + len - 1) % 12]}`;
  }
  return ms.map((m) => MONTHS_S[m]).join(", ");
}

const GLOW_COLOR: any = (e: any) => ["interpolate", ["linear"], e,
  0, "#5d7589", 2, "#5e93bb", 8, "#7fc0e8", 25, "#c4eaff", 55, "#ffffff"];
const GLOW_OPACITY: any = (e: any) => ["interpolate", ["linear"], e,
  0, 0.05, 3, 0.45, 15, 0.75, 40, 0.95];

// tame the halo at world zoom or dense regions (Europe) merge into a blob;
// zoom must be the OUTERMOST interpolate, data expr nests inside the stops
const glowOpacityZ = (): any => ["interpolate", ["linear"], ["zoom"],
  2, ["*", 0.55, GLOW_OPACITY(pctExpr())] as any,
  4.5, GLOW_OPACITY(pctExpr())];

const heatWeight = (): any => ["*", ["get", "reliable"],
  ["interpolate", ["linear"], pctExpr(), 0, 0, 1, 0.12, 3, 0.35, 12, 0.7, 40, 1]];

map.on("load", async () => {
  const data = await (await fetch("/data/airports.json")).json();
  state.airports = data.airports;

  const fc = {
    type: "FeatureCollection",
    features: state.airports.map((a) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [a.lon, a.lat] },
      properties: {
        icao: a.icao,
        name: a.name,
        annual: a.efvsHoursPerYear + a.belowHoursPerYear,
        // tiers are FOG-driven, not size-driven: at world zoom the fog FIELD
        // carries the picture and only exceptional airports get a dot;
        // everything else resolves as you zoom in
        tier: (a.reliability ?? "ok") !== "ok" ? 2
            : a.efvsHoursPerYear + a.belowHoursPerYear >= 300 ? 0
            : a.efvsHoursPerYear + a.belowHoursPerYear >= 150 ? 1 : 2,
        // unreliable reporters are excluded from the fog field — their
        // frequencies are artifacts, not weather
        reliable: (a.reliability ?? "ok") === "ok" ? 1 : 0,
        ils: a.ils ?? "unknown",
        lpv: a.lpv ?? "unknown",
        g: a.grid.flat(),
      },
    })),
  };
  map.addSource("airports", { data: fc as any, type: "geojson" });

  // world-zoom fog field: a continuous luminous layer weighted by the
  // scrubbed fog % — fog banks, not bubble soup; fades out as dots fade in
  map.addLayer({
    id: "fogheat",
    type: "heatmap",
    source: "airports",
    maxzoom: 6.5,
    paint: {
      "heatmap-weight": heatWeight(),
      "heatmap-intensity": 0.7,
      "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 1, 13, 3, 22, 6, 46],
      "heatmap-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.9, 6.2, 0],
      "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"],
        0, "rgba(20,40,60,0)",
        0.25, "rgba(38,78,110,0.55)",
        0.5, "rgba(79,142,184,0.75)",
        0.75, "rgba(159,216,255,0.9)",
        1, "rgba(238,250,255,0.95)"],
    },
  });

  const base: any = ["max", ["min", ["+", 3, ["*", 0.5, ["sqrt", ["get", "annual"]]]], 12], 3.5];
  // size encodes annual hours at low/mid zoom, then CONVERGES to a fixed
  // marker size by z9 — at single-airport scale the size encoding carries
  // no information and a growing balloon just obscures the location.
  // NB: maplibre only allows ["zoom"] in a TOP-LEVEL interpolate, so the
  // per-layer multiplier must live inside the stops, not wrap the result.
  const radius = (mult: number): any => ["interpolate", ["linear"], ["zoom"],
    3, ["*", base, mult], 6, ["*", base, 1.55 * mult], 9, 10 * mult, 14, 12 * mult];

  // every analyzed airport gets a faint pinprick at world zoom — honest
  // "we have data here" presence, visually distinct from fog luminance
  map.addLayer({
    id: "presence",
    type: "circle",
    source: "airports",
    maxzoom: 6.2,
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 0.9, 4, 1.8, 6, 2.4],
      "circle-color": "#7d92a6",
      "circle-opacity": ["interpolate", ["linear"], ["zoom"], 3.2, 0.5, 5.4, 0.45, 6.1, 0],
    },
  });

  // no bubbles at world zoom — the fog field carries it; dots return on
  // zoom, and quiet airports (<60 h/yr) hold as pinpricks until z6
  for (const [suffix, tier, minzoom] of [["", 0, 3], ["2", 1, 4.2], ["3", 2, 6]] as const) {
    const filter: any = ["==", ["get", "tier"], tier];
    map.addLayer({
      id: "glow" + suffix, type: "circle", source: "airports", filter, minzoom,
      paint: {
        "circle-radius": radius(1.9),
        "circle-blur": 1.4,
        "circle-color": GLOW_COLOR(pctExpr()),
        "circle-opacity": glowOpacityZ(),
      },
    });
    map.addLayer({
      id: "core" + suffix, type: "circle", source: "airports", filter, minzoom,
      paint: {
        "circle-radius": radius(1),
        "circle-color": GLOW_COLOR(pctExpr()),
        "circle-opacity": 1,
        "circle-stroke-width": 0.7,
        "circle-stroke-color": "#8fa6bc",
        "circle-stroke-opacity": 0.6,
      },
    });
    // specular highlight offset up-left — cheap 3D-sphere read on every dot
    map.addLayer({
      id: "sheen" + suffix, type: "circle", source: "airports", filter, minzoom,
      paint: {
        "circle-radius": radius(0.42),
        "circle-blur": 0.7,
        "circle-color": "#ffffff",
        "circle-opacity": 0.65,
        "circle-translate": [-2, -2],
      },
    });
  }

  const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: "tip" });
  for (const layer of ["core", "core2", "core3"]) {
    map.on("mousemove", layer, (e) => {
      map.getCanvas().style.cursor = "pointer";
      const f = e.features?.[0];
      if (!f) return;
      const g = JSON.parse(JSON.stringify(f.properties.g));
      const arr = typeof g === "string" ? JSON.parse(g) : g;
      const pct = windowAvg(arr);
      tip.setLngLat(e.lngLat)
        .setHTML(`<b>${f.properties.icao}</b> ${f.properties.name}<br>${windowLabel()} ${String(state.hr).padStart(2, "0")}:00 local — <b>${pct}%</b> sub-CAT-I`)
        .addTo(map);
    });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; tip.remove(); });
    map.on("click", layer, (e) => {
      const icao = e.features?.[0]?.properties.icao;
      if (icao) openAirport(icao);
    });
  }

  const hash = location.hash.slice(1);
  if (hash === "rankings") openRankings();
  else if (hash === "methodology") openMethodology();
  else if (hash.length > 1) openAirport(hash.toUpperCase());
});

// "ILS only": hide airports KNOWN to have no ILS (US per NASR); intl
// unknowns stay — absence of evidence isn't absence of an ILS
const BASE_FILTERS: Record<string, any> = {
  fogheat: null, presence: null,
  glow: ["==", ["get", "tier"], 0], core: ["==", ["get", "tier"], 0], sheen: ["==", ["get", "tier"], 0],
  glow2: ["==", ["get", "tier"], 1], core2: ["==", ["get", "tier"], 1], sheen2: ["==", ["get", "tier"], 1],
  glow3: ["==", ["get", "tier"], 2], core3: ["==", ["get", "tier"], 2], sheen3: ["==", ["get", "tier"], 2],
};

function applyApproachFilters() {
  if (!map.getLayer("glow")) return;
  const conds: any[] = [];
  if (state.ilsOnly) conds.push(["!=", ["get", "ils"], "no"]);
  if (state.lpvOnly) conds.push(["!=", ["get", "lpv"], "no"]);
  for (const [id, base] of Object.entries(BASE_FILTERS)) {
    const all = [...(base ? [base] : []), ...conds];
    const f = all.length === 0 ? null : all.length === 1 ? all[0] : ["all", ...all];
    map.setFilter(id, f as any);
  }
}

$<HTMLInputElement>("#ils-only").addEventListener("change", (e) => {
  state.ilsOnly = (e.target as HTMLInputElement).checked;
  applyApproachFilters();
});
$<HTMLInputElement>("#lpv-only").addEventListener("change", (e) => {
  state.lpvOnly = (e.target as HTMLInputElement).checked;
  applyApproachFilters();
});

// ---------- NOW mode: live below-minima layer (worldwide, one fetch) ----------
let nowTimer: ReturnType<typeof setInterval> | undefined;

function parseNowCsv(text: string) {
  // first column (raw_text) is quoted and contains commas; strip it, then
  // the rest splits cleanly. AWC cache column order is fixed.
  const sub: { icao: string; sev: "amber" | "red" }[] = [];
  let asOf = "";
  for (const line of text.split("\n")) {
    if (!line.startsWith('"')) continue;
    const close = line.indexOf('",');
    if (close < 0) continue;
    const cols = line.slice(close + 2).split(",");
    const icao = cols[0];
    const visRaw = cols[9];
    const vis = visRaw ? (visRaw.includes("+") ? 10 : parseFloat(visRaw)) : NaN;
    let ceil = Infinity;
    for (const k of [21, 23, 25, 27]) {
      if (["BKN", "OVC", "OVX"].includes(cols[k])) {
        const b = parseFloat(cols[k + 1]);
        if (!Number.isNaN(b)) ceil = Math.min(ceil, b);
      }
    }
    const below = !Number.isNaN(vis) && vis < 0.19;
    const isSub = (!Number.isNaN(vis) && vis < 0.5) || ceil < 200;
    if (isSub) sub.push({ icao, sev: below ? "red" : "amber" });
    if (!asOf && cols[1]) asOf = cols[1];
  }
  return { sub, asOf };
}

async function refreshNow() {
  try {
    const text = await (await fetch("/api/now")).text();
    const { sub } = parseNowCsv(text);
    const byIcao = new Map(state.airports.map((a) => [a.icao, a]));
    const feats = sub.flatMap(({ icao, sev }) => {
      const a = byIcao.get(icao);
      return a ? [{
        type: "Feature",
        geometry: { type: "Point", coordinates: [a.lon, a.lat] },
        properties: { icao, sev },
      }] : [];
    });
    (map.getSource("nowsrc") as any)?.setData({ type: "FeatureCollection", features: feats });
    const chip = $("#now-chip");
    chip.hidden = false;
    const reds = feats.filter((f) => f.properties.sev === "red").length;
    chip.textContent = `${feats.length} below CAT I now${reds ? ` · ${reds} below 300 m` : ""}`;
  } catch { /* keep previous state */ }
}

$<HTMLInputElement>("#now-mode").addEventListener("change", (e) => {
  const on = (e.target as HTMLInputElement).checked;
  if (on) {
    if (!map.getSource("nowsrc")) {
      map.addSource("nowsrc", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "now-glow", type: "circle", source: "nowsrc",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 9, 6, 16],
          "circle-blur": 1.1,
          "circle-color": ["match", ["get", "sev"], "red", "#ff5a4d", "#ffb347"],
          "circle-opacity": 0.55,
        },
      });
      map.addLayer({
        id: "now-core", type: "circle", source: "nowsrc",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 3.2, 6, 5.5],
          "circle-color": ["match", ["get", "sev"], "red", "#ff5a4d", "#ffb347"],
          "circle-stroke-width": 1, "circle-stroke-color": "#fff8",
        },
      });
      map.on("click", "now-core", (ev) => {
        const icao = ev.features?.[0]?.properties.icao;
        if (icao) openAirport(icao);
      });
      map.on("mousemove", "now-core", () => { map.getCanvas().style.cursor = "pointer"; });
    }
    map.setLayoutProperty("now-glow", "visibility", "visible");
    map.setLayoutProperty("now-core", "visibility", "visible");
    refreshNow();
    nowTimer = setInterval(refreshNow, 180_000);
  } else {
    clearInterval(nowTimer);
    if (map.getLayer("now-glow")) {
      map.setLayoutProperty("now-glow", "visibility", "none");
      map.setLayoutProperty("now-core", "visibility", "none");
    }
    $("#now-chip").hidden = true;
  }
});

// ---------- search ----------
const searchInput = $<HTMLInputElement>("#search-input");
const searchResults = $("#search-results");
let searchSel = 0;

function runSearch() {
  const q = searchInput.value.trim().toLowerCase();
  if (q.length < 2) { searchResults.hidden = true; return; }
  const hits = state.airports
    .filter((a) =>
      a.icao.toLowerCase().includes(q) ||
      a.name.toLowerCase().includes(q) ||
      a.country.toLowerCase() === q)
    .sort((a, b) => {
      const ax = a.icao.toLowerCase() === q ? 0 : 1;
      const bx = b.icao.toLowerCase() === q ? 0 : 1;
      return ax - bx || (b.efvsHoursPerYear + b.belowHoursPerYear) - (a.efvsHoursPerYear + a.belowHoursPerYear);
    })
    .slice(0, 12);
  searchSel = 0;
  searchResults.innerHTML = hits.map((a, i) => `
    <div class="search-row${i === 0 ? " active" : ""}" data-icao="${a.icao}">
      <b>${a.icao}</b><span>${a.name}</span>
      <em>${a.country} · ${Math.round(a.efvsHoursPerYear + a.belowHoursPerYear)} h/yr</em>
    </div>`).join("");
  searchResults.hidden = hits.length === 0;
}

function pickSearch(icao: string) {
  const a = state.airports.find((x) => x.icao === icao);
  if (!a) return;
  searchResults.hidden = true;
  searchInput.value = "";
  map.flyTo({ center: [a.lon, a.lat], zoom: 6, duration: 1600 });
  openAirport(icao);
}

searchInput.addEventListener("input", runSearch);
searchInput.addEventListener("keydown", (e) => {
  const rows = [...searchResults.querySelectorAll(".search-row")];
  if (e.key === "Enter" && rows.length) {
    pickSearch((rows[searchSel] as HTMLElement).dataset.icao!);
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    searchSel = (searchSel + (e.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length;
    rows.forEach((r, i) => r.classList.toggle("active", i === searchSel));
  } else if (e.key === "Escape") {
    searchResults.hidden = true;
    searchInput.blur();
  }
});
searchResults.addEventListener("click", (e) => {
  const row = (e.target as HTMLElement).closest(".search-row") as HTMLElement | null;
  if (row) pickSearch(row.dataset.icao!);
});
document.addEventListener("click", (e) => {
  if (!(e.target as HTMLElement).closest("#search")) searchResults.hidden = true;
});

// ---------- rankings ----------
let rankCatIOnly = true;

function openRankings() {
  history.replaceState(null, "", "#rankings");
  setSelected(null);
  // a 6%-coverage or anomalous-reporting station can't support a frequency
  // claim — keep them out of the league table (still on the map and search)
  const oppOf = (a: Airport) =>
    a.efvsOppByEquip?.[state.equip] ?? a.efvsOppHoursPerYear ?? a.efvsHoursPerYear;
  const floorOf = (a: Airport) => a.floors?.[state.equip] ?? a.floorM ?? 800;
  const rows = state.airports
    .filter((a) => (a.coveragePct ?? 100) >= 50 && (a.reliability ?? "ok") === "ok")
    .filter((a) => !state.ilsOnly || (a.ils ?? "unknown") !== "no")
    .filter((a) => !state.lpvOnly || (a.lpv ?? "unknown") !== "no")
    .filter((a) => !rankCatIOnly || (a.catIls !== "CATIII" && a.catIls !== "CATII"))
    .sort((a, b) => oppOf(b) - oppOf(a))
    .slice(0, 50);
  panelContent.innerHTML = `
    <h2>Where EFVS buys the most</h2>
    <p class="sub">Hours below the floor YOUR equipage can achieve at each airport, within EFVS range · ${state.airports.length} airports analyzed</p>
    <div class="rank-filter" style="gap:6px">
      operator equipage:
      ${(["cat1", "hud", "cat2", "cat3"] as const).map((e) => `
        <button class="equip-chip${state.equip === e ? " on" : ""}" data-equip="${e}"
          title="${{ cat1: "CAT I flight deck — typical Part 135/125/91: the EFVS retrofit audience",
                     hud: "HUD-equipped, SA CAT I authorized (FAA) / LTS CAT I (EASA) — DH 150ft, RVR ~1400. The EFVS prospect usually already owns this HUD",
                     cat2: "CAT II-capable deck and authorization",
                     cat3: "CAT III deck + training — typical Part 121 major" }[e]}">
          ${{ cat1: "CAT I (135/125)", hud: "HUD · SA CAT I", cat2: "CAT II", cat3: "CAT III (121)" }[e]}</button>`).join("")}
    </div>
    <label class="rank-filter">
      <input id="rank-cat1" type="checkbox" ${rankCatIOnly ? "checked" : ""} />
      only airports without CAT II/III (no autoland fallback — the strongest EFVS case)
    </label>
    <table class="rank-table">
      <thead><tr><th>#</th><th>airport</th><th class="num">opp h/yr</th><th class="num">floor</th><th>ILS</th></tr></thead>
      <tbody>
        ${rows.map((a, i) => `
          <tr data-icao="${a.icao}">
            <td class="num">${i + 1}</td>
            <td><b>${a.icao}</b> ${a.name.length > 26 ? a.name.slice(0, 25) + "…" : a.name} <em style="color:var(--ink-dim)">${a.country}</em></td>
            <td class="num efvs">${Math.round(oppOf(a))}</td>
            <td class="num">${floorOf(a)}m</td>
            <td>${a.catIls === "NONE" ? "—" : a.catIls === "CATIII" || a.catIls === "CATII" ? a.catIls.replace("CAT", "") : "I"}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    <p class="note">Opportunity = hours below the airport's own published minima (US: FAA ILS Master / CIFP LPV; intl: capability class) yet within EFVS range (≥300 m). Counting is conservative — partial visibility bins below the floor are excluded.</p>
  `;
  panelContent.querySelector("#rank-cat1")!.addEventListener("change", (e) => {
    rankCatIOnly = (e.target as HTMLInputElement).checked;
    openRankings();
  });
  panelContent.querySelectorAll(".equip-chip").forEach((b) =>
    b.addEventListener("click", () => {
      state.equip = (b as HTMLElement).dataset.equip as typeof state.equip;
      openRankings();
    }));
  panelContent.querySelectorAll("tr[data-icao]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const icao = (tr as HTMLElement).dataset.icao!;
      const a = state.airports.find((x) => x.icao === icao)!;
      map.flyTo({ center: [a.lon, a.lat], zoom: 6, duration: 1600 });
      openAirport(icao);
    }));
  panel.hidden = false;
  document.body.classList.add("panel-open");
}

$("#rankings-btn").addEventListener("click", openRankings);
$("#methodology").addEventListener("click", (e) => { e.preventDefault(); openMethodology(); });

// ---------- methodology ----------
function openMethodology() {
  history.replaceState(null, "", "#methodology");
  setSelected(null);
  panelContent.innerHTML = `
    <h2>Methodology</h2>
    <p class="sub">what this map can and cannot tell you</p>
    <h3>Observation basis</h3>
    <p class="note" style="margin-top:6px">Routine hourly METARs 2016–2025 (Iowa Environmental Mesonet archive), one observation per hour — the last routine report in each UTC hour. SPECIs are deliberately excluded so frequencies are an unbiased sample of hours. "% of hours" uses hours with a valid visibility report as the denominator; per-airport archive coverage is shown in each deep-dive.</p>
    <h3>The three bands (visibility OR ceiling)</h3>
    <p class="note" style="margin-top:6px">A CAT I approach needs visibility above minima AND ceiling above the ~200 ft decision height; both terms enter the bands.<br/>
    <b style="color:var(--ink)">Normal</b> — vis ≥ ½ SM (~800 m) and ceiling ≥ 200 ft.<br/>
    <b style="color:var(--accent)">EFVS-recoverable</b> — vis 300–800 m, or ceiling &lt; 200 ft with workable visibility (a thin low deck is exactly what EFVS sees through; FAA 91.176).<br/>
    <b style="color:var(--ink)">Below all</b> &lt; 300 m vis — CAT III autoland territory; ceiling-only events never land here.</p>
    <h3>Honest limitations</h3>
    <p class="note" style="margin-top:6px">METAR prevailing visibility is a proxy for RVR — RVR on a lit runway is often better, so the bands understate what's flyable; read them as a climatological index, not operating minima. Thresholds are global constants, not per-runway minima. CAT II/III flags are authoritative for the US (FAA CIFP), hand-curated internationally, and "assumed CAT I" elsewhere — confidence is shown per airport. The cause chart folds BR (mist) into fog: BR officially means vis ≥ 800 m, so BR on a sub-CAT-I observation is conservatively-coded fog.</p>
    <h3>Does it predict real cancellations?</h3>
    <p class="note" id="bts-note" style="margin-top:6px">Loading validation…</p>
    <h3>Why CAT II/III matters</h3>
    <p class="note" style="margin-top:6px">At a CAT III airport, suitably equipped airliners already land in fog. EFVS value concentrates where low visibility is frequent <i>and</i> CAT II/III is absent — use the rankings filter to see exactly that intersection.</p>
    <p class="note">Full methodology with sources: <a href="https://github.com/travis735/fog-atlas/blob/main/METHODOLOGY.md" target="_blank" rel="noopener">github.com/travis735/fog-atlas</a></p>
  `;
  panel.hidden = false;
  document.body.classList.add("panel-open");
  fetch("/data/bts_validation.json").then((r) => r.json()).then((v) => {
    const el = panelContent.querySelector("#bts-note");
    if (!el) return;
    el.innerHTML = `Yes — joined against US DOT/BTS on-time data (${v.window}, ` +
      `${(Object.values(v.bands) as any[]).reduce((s, b) => s + b.flights, 0).toLocaleString()} departures): ` +
      `flights scheduled during EFVS-recoverable hours at their origin were weather-cancelled at ` +
      `<b style="color:var(--accent)">${v.multiplier_efvs}× the baseline rate</b> ` +
      `(${v.bands.efvs.wxCancelPct}% vs ${v.bands.normal.wxCancelPct}%). ` +
      `Below-300m hours run ${v.multiplier_below}× — lower than the EFVS band because that exposure ` +
      `concentrates at CAT III hubs where autoland keeps operating: the no-fallback thesis, visible in cancellation data. ` +
      `BTS "weather" is generic (snow counts too) — read as validation, not a fog-specific cost model.`;
  }).catch(() => {});
}

function applyScrub() {
  readout.textContent = `${windowLabel()} · ${String(state.hr).padStart(2, "0")}:00`;
  renderChips();
  if (!map.getLayer("glow")) return;
  map.setPaintProperty("fogheat", "heatmap-weight", heatWeight());
  for (const s of ["", "2", "3"]) {
    map.setPaintProperty("glow" + s, "circle-color", GLOW_COLOR(pctExpr()));
    map.setPaintProperty("glow" + s, "circle-opacity", glowOpacityZ());
    map.setPaintProperty("core" + s, "circle-color", GLOW_COLOR(pctExpr()));
  }
}

// month chips: click = single month, drag = contiguous range,
// cmd/ctrl-click = toggle membership (covers wraparound windows like Nov-Jan)
const monthsEl = $("#months");
monthsEl.innerHTML = MONTHS_S.map((m, i) =>
  `<button class="mchip" data-m="${i}" title="${MONTHS[i]}">${m[0]}</button>`).join("");
const chips = [...monthsEl.querySelectorAll<HTMLElement>(".mchip")];

function renderChips() {
  chips.forEach((c, i) => c.classList.toggle("on", state.months.includes(i)));
}

let dragAnchor: number | null = null;
function setMonths(ms: number[]) {
  state.months = [...new Set(ms)].sort((a, b) => a - b);
  if (!state.months.length) state.months = [0];
  applyScrub();
}
monthsEl.addEventListener("pointerdown", (e) => {
  const m = (e.target as HTMLElement).dataset?.m;
  if (m === undefined) return;
  const i = +m;
  if (e.metaKey || e.ctrlKey) {
    setMonths(state.months.includes(i)
      ? state.months.filter((x) => x !== i) : [...state.months, i]);
  } else {
    dragAnchor = i;
    setMonths([i]);
  }
});
monthsEl.addEventListener("pointerover", (e) => {
  const m = (e.target as HTMLElement).dataset?.m;
  if (dragAnchor === null || m === undefined) return;
  const [lo, hi] = [Math.min(dragAnchor, +m), Math.max(dragAnchor, +m)];
  setMonths(Array.from({ length: hi - lo + 1 }, (_, k) => lo + k));
});
document.addEventListener("pointerup", () => { dragAnchor = null; });

hrEl.addEventListener("input", () => { state.hr = +hrEl.value; applyScrub(); });

let timer: ReturnType<typeof setInterval> | undefined;
$("#play").addEventListener("click", () => {
  state.playing = !state.playing;
  $("#play").innerHTML = state.playing ? "&#10074;&#10074;" : "&#9654;";
  if (state.playing) {
    timer = setInterval(() => {
      state.hr = (state.hr + 1) % 24;
      // single-month mode keeps the old tour-the-year feel; a window loops its hours
      if (state.hr === 0 && state.months.length === 1)
        state.months = [(state.months[0] + 1) % 12];
      hrEl.value = String(state.hr);
      applyScrub();
    }, 300);
  } else clearInterval(timer);
});

$("#close").addEventListener("click", () => {
  panel.hidden = true;
  document.body.classList.remove("panel-open");
  setSelected(null);
  history.replaceState(null, "", location.pathname);
});

let persistenceTable: Record<string, any> | null | undefined;
let nowcastModel: any | null | undefined;

// red selection marker on the airport whose deep-dive is open
function setSelected(a: Airport | null) {
  if (!map.getSource("selsrc")) {
    if (!a) return;
    map.addSource("selsrc", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "sel-ring", type: "circle", source: "selsrc",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 9, 9, 15],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-width": 1.6,
        "circle-stroke-color": "#ff5a4d",
        "circle-stroke-opacity": 0.9,
      },
    });
    map.addLayer({
      id: "sel-dot", type: "circle", source: "selsrc",
      paint: {
        "circle-radius": 3.5,
        "circle-color": "#ff5a4d",
        "circle-stroke-width": 1.2,
        "circle-stroke-color": "#ffffff",
      },
    });
  }
  (map.getSource("selsrc") as any).setData({
    type: "FeatureCollection",
    features: a ? [{ type: "Feature", geometry: { type: "Point", coordinates: [a.lon, a.lat] }, properties: {} }] : [],
  });
}

const SEASONS = ["DJF", "DJF", "MAM", "MAM", "MAM", "JJA", "JJA", "JJA", "SON", "SON", "SON", "DJF"];
const TODS = ["night", "morning", "afternoon", "evening"];

async function openAirport(icao: string) {
  const a = state.airports.find((x) => x.icao === icao);
  if (!a) return;
  if (persistenceTable === undefined) {
    persistenceTable = null; // only try once
    try { persistenceTable = await (await fetch("/data/persistence.json")).json(); } catch {}
  }
  const detail = await (await fetch(`/data/detail/${icao}.json`)).json();
  history.replaceState(null, "", `#${icao}`);
  setSelected(a);

  const cat3 = a.catIls === "CATIII" || a.catIls === "CATII";
  const noIls = a.catIls === "NONE";
  // BR officially means vis >= 800m, so BR on a sub-CAT-I ob is fog that was
  // coded conservatively — present them as one family
  const merged: Record<string, number> = { ...a.causes };
  merged.FG = Math.round(((merged.FG ?? 0) + (merged.BR ?? 0)) * 10) / 10;
  delete merged.BR;
  const causes = Object.entries(merged)
    .filter(([k, v]) => !["none", "other"].includes(k) && v > 0)
    .sort((x, y) => y[1] - x[1]);
  const causeLabel: Record<string, string> = { FG: "fog / mist", "HZ/FU": "haze / smoke", SN: "snow", CEIL: "low ceiling (stratus)" };

  panelContent.innerHTML = `
    <h2>${a.icao}</h2>
    <p class="sub">${a.name} · ${a.country}</p>
    <div>
      <span class="badge ${cat3 ? "cat3" : "cat1"}" title="${noIls
        ? "Verified: no ILS at this airport — RNAV/non-precision approaches only, so real minima sit ABOVE CAT I and these bands understate blocked hours"
        : cat3
        ? "This airport has CAT II/III ILS — suitably equipped airliners can already land in low visibility"
        : "Best available approach CAT I — visibility below ~800 m forces a missed approach without EFVS"}">${noIls ? "NO ILS" : cat3 ? a.catIls.replace("CAT", "CAT ") : "CAT I"}</span>
      <span class="badge conf" title="${{
        "faa-nasr": "ILS category from the FAA NASR database (authoritative, US)",
        "faa-c060": "On the FAA OpSpec C060 list of foreign CAT II/III facilities",
        aip: "ILS status researched from the national AIP / official sources and spot-audited",
        curated: "Capability confirmed from AIPs / FAA publications",
        verify: "Capability confirmed from AIPs / FAA publications",
        assumed: a.country === "US"
          ? "No ILS in the FAA NASR database — likely RNAV/non-precision approaches only"
          : "Not on the FAA C060 CAT II/III list — assumed CAT I; the airport may have capability not approved for US carriers",
        unknown: "Capability not yet determined for this airport",
      }[a.catConfidence] ?? ""}">${{
        "faa-nasr": "confirmed · FAA NASR",
        "faa-c060": "confirmed · FAA C060",
        aip: "confirmed · AIP / official",
        curated: "capability curated",
        verify: "capability curated",
        assumed: a.country === "US" ? "no ILS on record — RNAV only" : "assumed — not on FAA CAT II/III list",
        unknown: "capability unknown",
      }[a.catConfidence] ?? a.catConfidence}</span>
      ${!cat3 ? `<span class="badge cat1">high EFVS value — no CAT II/III fallback</span>` : ""}
    </div>
    ${(a.reliability ?? "ok") !== "ok" ? `
    <div class="warn-banner">${a.reliability === "low-coverage"
      ? `⚠ Archive coverage is only ${a.coveragePct}% — too thin to support frequency claims. Numbers below are shown for completeness, not for decisions.`
      : `⚠ Reporting anomaly detected: this station's low-visibility observations are dominated by literal-zero values with no diurnal structure — the signature of an encoding artifact, not weather. Treat all frequencies here as unreliable.`}</div>` : ""}
    <div class="stats" title="EFVS opportunity = hours/yr below the floor an operator can ACHIEVE here (flight deck × ground infrastructure binds), yet within EFVS range (≥300 m). US floors from FAA ILS Master published minima + CIFP LPV; international approximated from capability class.">
      <div class="stat efvs">
        <div class="v">${Math.round(a.efvsOppByEquip?.cat1 ?? a.efvsHoursPerYear)}</div>
        <div class="k">EFVS hrs / yr · CAT I deck (Part 135/125 — the EFVS buyer) · floor ${a.floors?.cat1 ?? 800} m</div></div>
      ${(a.floors?.hud ?? 999999) < (a.floors?.cat1 ?? 0) ? `
      <div class="stat">
        <div class="v">${Math.round(a.efvsOppByEquip?.hud ?? 0)}</div>
        <div class="k">HUD · SA CAT I published here · floor ${a.floors?.hud} m</div></div>` : `
      <div class="stat stat-na">
        <div class="v">${Math.round(a.efvsOppByEquip?.hud ?? a.efvsOppByEquip?.cat1 ?? 0)}</div>
        <div class="k">HUD deck — no SA CAT I published at this airport (same floor as CAT I)</div></div>`}
      <div class="stat">
        <div class="v">${Math.round(a.efvsOppByEquip?.cat2 ?? 0)}</div>
        <div class="k">CAT II deck · floor ${a.floors?.cat2 ?? "—"} m</div></div>
      <div class="stat">
        <div class="v">${Math.round(a.efvsOppByEquip?.cat3 ?? 0)}</div>
        <div class="k">CAT III deck (typical 121) · floor ${a.floors?.cat3 ?? "—"} m</div></div>
    </div>
    <div id="live"></div>
    <h3 id="heatmap-title">When it closes — % of hours below CAT I, by month × local hour</h3>
    <div id="heatmap"></div>
    <div id="heatmap-legend"></div>
    <div id="persist"></div>
    <h3>Cause of low visibility</h3>
    <div id="causes"></div>
    <p class="note">Sub-CAT-I = prevailing visibility below ~800 m or ceiling below 200 ft. Visibility is a climatological proxy for RVR — read as relative risk, not operating minima. Hours are local (${a.tz}). 2016–2025 routine METARs, archive coverage ${detail.coveragePct}%. <a href="https://github.com/travis735/fog-atlas/blob/main/METHODOLOGY.md" target="_blank" rel="noopener">Full methodology</a>.</p>
  `;

  const cells: { hr: number; mon: string; pct: number }[] = [];
  for (let m = 0; m < 12; m++)
    for (let h = 0; h < 24; h++)
      cells.push({ hr: h, mon: MONTHS_S[m], pct: (detail.efvsGrid[m][h] ?? 0) + (detail.belowGrid[m][h] ?? 0) });

  // fixed discrete bins: the shade is read against a labeled legend (not
  // eyeballed on a gradient) and means the same thing at every airport
  const BIN_EDGES = [0.5, 2, 5, 12, 25, 50];
  const BIN_COLORS = ["#1c2935", "#33506a", "#3f76a3", "#62a8d8", "#9fd4f2", "#dff3ff", "#ffffff"];
  const BIN_LABELS = ["<0.5", "0.5–2", "2–5", "5–12", "12–25", "25–50", "50+"];
  const peak = Math.max(...cells.map((c) => c.pct));
  $("#heatmap-title").textContent =
    `When it closes — % of hours below CAT I, by month × local hour (peak ${peak < 1 ? peak.toFixed(1) : Math.round(peak)}%)`;
  const heat = Plot.plot({
    width: 386,
    height: 230,
    marginLeft: 34,
    style: { background: "transparent", color: "#8294a3", fontSize: "9px" },
    x: { label: "local hour", ticks: [0, 6, 12, 18, 23] },
    y: { label: null, domain: MONTHS_S },
    color: { type: "threshold", domain: BIN_EDGES, range: BIN_COLORS },
    marks: [
      Plot.cell(cells, { x: "hr", y: "mon", fill: "pct", inset: 0.4 }),
      Plot.tip(cells, Plot.pointer({
        x: "hr", y: "mon",
        title: (d: any) => `${d.mon} ${String(d.hr).padStart(2, "0")}:00 — ${d.pct.toFixed(1)}% of hours`,
      })),
    ],
  });
  $("#heatmap").replaceChildren(heat);
  $("#heatmap-legend").innerHTML = BIN_LABELS.map((l, i) => `
    <span class="bin"><i style="background:${BIN_COLORS[i]}"></i>${l}</span>`).join("") +
    `<span class="bin-unit">% of hours</span>`;

  // ---- phase 2: persistence ("once it closes, how long?") ----
  const per = persistenceTable?.[icao];
  if (per) {
    const pts = [{ h: 0, p: 1 }, ...per.curve.map((p: number, i: number) => ({ h: i + 1, p }))];
    const persistChart = Plot.plot({
      width: 386, height: 120, marginLeft: 34,
      style: { background: "transparent", color: "#8294a3", fontSize: "9px" },
      x: { label: "hours after onset", ticks: [0, 2, 4, 6, 8] },
      y: { label: null, domain: [0, 1], tickFormat: (d: number) => `${Math.round(d * 100)}%` },
      marks: [
        Plot.areaY(pts, { x: "h", y: "p", fill: "#9fd8ff18", curve: "monotone-x" }),
        Plot.lineY(pts, { x: "h", y: "p", stroke: "#9fd8ff", strokeWidth: 1.5, curve: "monotone-x" }),
        Plot.tip(pts, Plot.pointerX({ x: "h", y: "p",
          title: (d: any) => `${Math.round(d.p * 100)}% of events still below CAT I ${d.h}h after onset` })),
      ],
    });
    $("#persist").innerHTML = `
      <h3>Once it closes — how long it stays closed</h3>
      <div class="stats" style="margin:10px 0">
        <div class="stat"><div class="v">${per.medianH}h</div><div class="k">median event</div></div>
        <div class="stat"><div class="v">${per.p25H}–${per.p75H}h</div><div class="k">middle 50%</div></div>
        <div class="stat"><div class="v">${per.n}</div><div class="k">events in 10 yrs</div></div>
      </div>
      <div id="persist-chart"></div>`;
    $("#persist-chart").replaceChildren(persistChart);
  } else {
    $("#persist").innerHTML = `
      <h3>Once it closes</h3>
      <p class="note" style="margin-top:6px">Fewer than 25 sub-CAT-I events in ten years — not enough to support persistence statistics.</p>`;
  }

  // ---- phase 2+3: live conditions + model nowcast via AWC proxy ----
  if (nowcastModel === undefined) {
    nowcastModel = null;
    try { nowcastModel = await (await fetch("/data/model_y2.json")).json(); } catch {}
  }
  fetch(`/api/metar?ids=${icao}&hours=3`).then((r) => r.json()).then((arr) => {
    const ob = arr?.[0];
    if (!ob || !panelContent.querySelector("#live")) return;
    const parseVis = (v: any) => v == null ? null
      : typeof v === "string" ? (v.includes("+") ? 10 : parseFloat(v)) : v;
    const ceilOf = (o: any) => {
      const cs = (o.clouds ?? [])
        .filter((c: any) => ["BKN", "OVC", "VV"].includes(c.cover) && c.base != null)
        .map((c: any) => c.base);
      return cs.length ? Math.min(...cs) : null;
    };
    const vis = parseVis(ob.visib);
    const ceil = ceilOf(ob);
    const sub = (vis != null && vis < 0.5) || (ceil != null && ceil < 200);
    const below = vis != null && vis < 0.19;
    const when = (ob.rawOb?.match(/\d{6}Z/) ?? [""])[0];
    let verdict: string;
    if (below) verdict = `<b style="color:#ff9a9a">below 300 m — beyond EFVS</b>`;
    else if (sub) verdict = `<b style="color:var(--accent)">below CAT I — EFVS-recoverable</b>`;
    else verdict = `<b style="color:#7fd49a">normal ops</b>`;
    let lift = "";
    if (sub && per) {
      const now = new Date();
      const key = `${SEASONS[now.getMonth()]}-${TODS[Math.floor(now.getHours() / 6)]}`;
      const c = per.buckets?.[key]?.curve ?? per.curve;
      lift = ` Historically, ${Math.round((1 - c[1]) * 100)}% of events here are over within 2 h (median ${per.medianH} h).`;
    }

    // model nowcast: P(sub-CAT-I in 2h) from the benchmarked logistic model
    let nowcast = "";
    if (nowcastModel && vis != null) {
      const prev = arr.find((o: any) =>
        ob.obsTime - o.obsTime > 2400 && ob.obsTime - o.obsTime < 5400);
      const pvis = prev ? parseVis(prev.visib) : null;
      const psub = prev
        ? ((pvis != null && pvis < 0.5) || ((ceilOf(prev) ?? 25000) < 200)) : null;
      const local = new Intl.DateTimeFormat("en-US",
        { timeZone: a.tz, hour: "numeric", month: "numeric", hour12: false })
        .formatToParts(new Date());
      const mon = +local.find((p) => p.type === "month")!.value;
      const hr = +local.find((p) => p.type === "hour")!.value % 24;
      const climP = Math.min(Math.max((a.grid[mon - 1]?.[hr] ?? 0) / 100, 1e-4), 1 - 1e-4);
      const tempF = ob.temp != null ? ob.temp * 1.8 + 32 : 59;
      const dewpF = ob.dewp != null ? ob.dewp * 1.8 + 32 : tempF - 10;
      const ceilFt = ceil ?? 25000;
      const f: Record<string, number> = {
        vsby_c: Math.min(Math.max(vis, 0), 10),
        log_vis: Math.log1p(Math.min(Math.max(vis, 0), 10)),
        ceil_c: Math.min(ceilFt, 25000) / 1000,
        low_ceil: ceilFt < 1000 ? 1 : 0,
        spread: Math.min(Math.max(tempF - dewpF, -5), 40),
        tmpf_c: Math.min(Math.max(tempF, -40), 120),
        sknt_c: Math.min(Math.max(ob.wspd ?? 5, 0), 50),
        vis_trend_c: pvis != null ? Math.min(Math.max(vis - pvis, -10), 10) : 0,
        sub_now: sub ? 1 : 0,
        sub_prev_f: psub == null ? (sub ? 1 : 0) : psub ? 1 : 0,
        mon_sin: Math.sin(2 * Math.PI * mon / 12),
        mon_cos: Math.cos(2 * Math.PI * mon / 12),
        hr_sin: Math.sin(2 * Math.PI * hr / 24),
        hr_cos: Math.cos(2 * Math.PI * hr / 24),
        clim_logit: Math.log(climP / (1 - climP)),
      };
      let z = nowcastModel.intercept;
      for (const [k, c] of Object.entries(nowcastModel.coef)) z += (c as number) * (f[k] ?? 0);
      const p = 1 / (1 + Math.exp(-z));
      nowcast = ` <span class="nowcast" title="Logistic nowcast trained on 2016–2023, validated on 2024–25 (Brier 39% better than climatology, AUC 0.95). Experimental — not for operational use.">Model: P(below CAT I within 2 h) ≈ <b>${p < 0.01 ? "<1" : Math.round(p * 100)}%</b></span>`;
    }

    panelContent.querySelector("#live")!.innerHTML = `
      <div class="live-line">RIGHT NOW · ${when} — ${ob.visib ?? "?"} SM${ceil != null ? `, ceiling ${ceil} ft` : ", no ceiling"} · ${verdict}.${lift}${nowcast}</div>`;
  }).catch(() => {});

  $("#causes").innerHTML = causes.map(([k, v]) => `
    <div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:11px">
      <span style="width:74px;color:var(--ink-dim)">${causeLabel[k] ?? k}</span>
      <div style="flex:1;height:6px;background:#141b23;border-radius:3px;overflow:hidden">
        <div style="width:${v}%;height:100%;background:linear-gradient(90deg,#3e6b8a,#9fd8ff)"></div>
      </div>
      <span style="width:40px;text-align:right">${v}%</span>
    </div>`).join("");

  panel.hidden = false;
  document.body.classList.add("panel-open");
}

applyScrub();

// debug API
(window as any).__fogatlas = {
  state,
  map,
  setScrub(mon: number, hr: number) { state.months = [mon]; state.hr = hr; hrEl.value = String(hr); applyScrub(); },
  setMonths,
  openAirport,
  openRankings,
  openMethodology,
};
