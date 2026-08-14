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

// ISO code → "Canada", not "· CA" (which reads as California next to a US map)
const regionNames = new Intl.DisplayNames(["en"], { type: "region" });
const countryCache = new Map<string, string>();
function countryName(code: string): string {
  let n = countryCache.get(code);
  if (!n) {
    try { n = regionNames.of(code) ?? code; } catch { n = code; }
    countryCache.set(code, n);
  }
  return n;
}

// view stashed by the webglcontextlost reload path, so recovery lands on the same spot
const savedView = ((): { lng: number; lat: number; zoom: number } | null => {
  try {
    const v = sessionStorage.getItem("fa-view");
    if (!v) return null;
    sessionStorage.removeItem("fa-view");
    return JSON.parse(v);
  } catch { return null; }
})();

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: savedView ? [savedView.lng, savedView.lat] : [15, 30],
  zoom: savedView?.zoom ?? 1.6,
  minZoom: 1,
  attributionControl: { compact: true },
});

// macOS GPU sleep/switch/driver resets kill the WebGL context; MapLibre never
// recovers on its own — the map stays black while the DOM chrome lives on.
// Give the browser a moment to restore, then reload (once visible) into the
// stashed view. Reloads are rate-limited in case the GPU is persistently gone.
map.getCanvas().addEventListener("webglcontextlost", () => {
  let restored = false;
  map.getCanvas().addEventListener("webglcontextrestored", () => { restored = true; }, { once: true });
  setTimeout(() => {
    if (restored) return;
    try {
      const past = (JSON.parse(sessionStorage.getItem("fa-ctx-reloads") ?? "[]") as number[])
        .filter((t) => Date.now() - t < 60_000);
      if (past.length >= 2) return;
      sessionStorage.setItem("fa-ctx-reloads", JSON.stringify([...past, Date.now()]));
      const c = map.getCenter();
      sessionStorage.setItem("fa-view", JSON.stringify({ lng: c.lng, lat: c.lat, zoom: map.getZoom() }));
    } catch { /* sessionStorage unavailable — still reload */ }
    const reload = () => setTimeout(() => location.reload(), 300);
    if (document.visibilityState === "visible") reload();
    else document.addEventListener("visibilitychange", reload, { once: true });
  }, 1500);
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

const FOGHEAT_OPACITY: any = ["interpolate", ["linear"], ["zoom"], 5, 0.9, 6.2, 0];
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
      "heatmap-opacity": FOGHEAT_OPACITY,
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
  else if (hash === "chase") openChase();
  else if (hash === "deploy") openDeploy();
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
      a.country.toLowerCase() === q ||
      countryName(a.country).toLowerCase().startsWith(q))
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
      <em>${countryName(a.country)} · ${Math.round(a.efvsHoursPerYear + a.belowHoursPerYear)} h/yr</em>
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
  closeChaseMap();
  closeDeployMap();
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
            <td><b>${a.icao}</b> ${a.name.length > 26 ? a.name.slice(0, 25) + "…" : a.name} <em style="color:var(--ink-dim)" title="${countryName(a.country)}">${a.country}</em></td>
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
  closeChaseMap();
  closeDeployMap();
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
    <h3>Fog chase (CHASE)</h3>
    <p class="note" style="margin-top:6px">A launch board for EFVS flight testing: airports filtered by per-runway infrastructure — approach light system, runway length, RVR sensors, and a <b>go-around height</b> tier: the lowest published minima height on the runway (ILS CAT I/II/III → 200/100/50 ft, LPV → ~250 ft, otherwise ≥350 ft). Tiers are proxies from FAA NASR + CIFP, not chart DAs, and cover US airports only for now. Distance and ETE are great-circle <b>still-air</b> at your cruise speed — no winds, no climb/descent. IN FOG NOW reads the same ~3-minute NOAA cache as NOW mode; LIKELY SOON ranks the top five by the experimental nowcast above. Alerts fire only while the tab is open — a static site cannot wake your phone. A scouting tool, not an ops release.</p>
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
  closeChaseMap();
  closeDeployMap();
  setSelected(null);
  history.replaceState(null, "", location.pathname);
});

let persistenceTable: Record<string, any> | null | undefined;
let nowcastModel: any | null | undefined;

// V3 forecast (shadow-aware): one /api/forecast fetch, cached 10 min
let fcCache: { at: number; data: any } | null = null;
async function getForecast(): Promise<any | null> {
  if (fcCache && Date.now() - fcCache.at < 600_000) return fcCache.data;
  try {
    const data = await (await fetch("/api/forecast")).json();
    fcCache = { at: Date.now(), data };
    return data;
  } catch { return null; }
}

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
  closeChaseMap();
  closeDeployMap();
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
    <p class="sub">${a.name} · ${countryName(a.country)}</p>
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
        <div class="v">—</div>
        <div class="k">HUD deck — no SA CAT I published at this airport</div></div>`}
      <div class="stat">
        <div class="v">${Math.round(a.efvsOppByEquip?.cat2 ?? 0)}</div>
        <div class="k">CAT II deck · floor ${a.floors?.cat2 ?? "—"} m</div></div>
      <div class="stat">
        <div class="v">${Math.round(a.efvsOppByEquip?.cat3 ?? 0)}</div>
        <div class="k">CAT III deck (typical 121) · floor ${a.floors?.cat3 ?? "—"} m</div></div>
    </div>
    <div id="live"></div>
    <div id="fcast"></div>
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

  // V3 forecast strip: calibrated percentages ONLY once meta.public is true
  // (the pre-registered bar); during shadow, show raw NBM LIFR-vis guidance,
  // clearly labeled — public NWS data, not our unverified model
  getForecast().then((fc) => {
    const f = fc?.airports?.[icao];
    const el = panelContent.querySelector("#fcast");
    if (!f || !el) return;
    const pub = Array.isArray(fc.meta?.publicAirports) && fc.meta.publicAirports.includes(icao);
    const vals: number[] = pub ? f.p.map((r: number[]) => r[0]) : f.liv.map((v: number | null) => v ?? 0);
    const peak = Math.max(...vals, 1);
    const bars = vals.map((v, i) =>
      `<div title="+${f.fhrs[i]} h — ${v}%" style="flex:1;align-self:flex-end;background:${pub ? "#9fd8ff" : "#3f76a3"};opacity:${v > 0 ? 0.9 : 0.22};height:${Math.max(2, Math.round((30 * v) / peak))}px"></div>`).join("");
    el.innerHTML = `
      <h3>Next 48 h — ${pub ? "P(vis &lt; 1 mi), calibrated" : "NBM guidance P(LIFR vis) · calibrated model in verification"}</h3>
      <div style="display:flex;gap:1px;height:32px;margin:8px 0 2px">${bars}</div>
      <div style="display:flex;justify-content:space-between;color:var(--ink-dim);font-size:9.5px">
        <span>+${f.fhrs[0]} h</span><span>peak ${Math.max(...vals)}% · cycle ${fc.meta.cycle.slice(5, 13)}z</span><span>+${f.fhrs[f.fhrs.length - 1]} h</span></div>
      <p class="note" style="margin-top:5px"><a href="/fog/${icao.toLowerCase()}/" target="_blank" rel="noopener">shareable forecast page</a> · <a href="/fog/scorecard/" target="_blank" rel="noopener">verification</a></p>`;
  });

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

// ---------- CHASE: fog-chasing launch board for EFVS testing ----------
interface ChaseEnd {
  e: string; len: number; als: string | null; rvr: string | null;
  ils: string | null; lpv: 0 | 1; tier: number;
  cur?: 0 | 1; // 1 = curated (AIP Canada/CFS research), not FAA NASR
}
interface ChasePrefs {
  base: string | null; speed: number; maxEteH: number;
  als: string[]; minLen: number; rvrReq: boolean; maxTier: number;
  alerts: boolean;
}

const CHASE_DEFAULTS: ChasePrefs = {
  base: null, speed: 250, maxEteH: 2,
  als: ["ALSF2", "ALSF1", "MALSR"], minLen: 5000, rvrReq: false, maxTier: 250,
  alerts: false,
};
const CHASE_ALS_CHIPS = ["ALSF2", "ALSF1", "MALSR", "SSALR", "MALSF", "OTHER"] as const;
const chasePrefs: ChasePrefs = (() => {
  try { return { ...CHASE_DEFAULTS, ...JSON.parse(localStorage.getItem("fa-chase") ?? "{}") }; }
  catch { return { ...CHASE_DEFAULTS }; }
})();
const saveChasePrefs = () => localStorage.setItem("fa-chase", JSON.stringify(chasePrefs));

let chaseData: { meta: any; airports: Record<string, ChaseEnd[]> } | null | undefined;
let chaseOpen = false;

const NM_R = 3440.065;
const rad = (d: number) => (d * Math.PI) / 180;
function distNm(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 +
    Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * NM_R * Math.asin(Math.sqrt(h));
}
function destPoint(lat: number, lon: number, brgDeg: number, dNm: number): [number, number] {
  const d = dNm / NM_R, th = rad(brgDeg), p1 = rad(lat), l1 = rad(lon);
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(th));
  const l2 = l1 + Math.atan2(Math.sin(th) * Math.sin(d) * Math.cos(p1),
    Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [(((l2 * 180) / Math.PI + 540) % 360) - 180, (p2 * 180) / Math.PI];
}
const eteStr = (h: number) => {
  const m = Math.round(h * 60);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
};
// hours below CAT I in a given month, from the climatological month x hour grid
function monthClimH(a: Airport, mon: number): number {
  const days = [31, 28.2, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mon];
  const s = (a.grid[mon] ?? []).reduce((acc, p) => acc + (p ?? 0), 0) / 100;
  return Math.round(s * days);
}
const ALS_LABEL: Record<string, string> = {
  ALSF2: "ALSF-2", ALSF1: "ALSF-1", MALSR: "MALSR", SSALR: "SSALR",
  MALSF: "MALSF", MALS: "MALS", SALS: "SALS", SALSF: "SALSF",
  ODALS: "ODALS", RLLS: "RLLS", OTHER: "other",
};

interface ChaseCand { a: Airport; end: ChaseEnd; nm: number; ete: number }

// ---- live conditions: one AWC cache fetch feeds the whole board ----
interface LiveOb {
  obsMs: number; vis: number | null; ceil: number | null;
  rvr: string | null; rvrFt: number | null; wx: string | null;
  sub: boolean; below: boolean;
  tempC: number | null; dewpC: number | null; wspd: number | null;
}
let chaseLive: Map<string, LiveOb> | null = null;
let chaseTimer: ReturnType<typeof setInterval> | undefined;

const RVR_RE = /\bR(\d{2}[LRC]?)\/([MP]?)(\d{4})(?:V[MP]?\d{4})?FT/g;

// same CSV the NOW layer reads, but keep raw text (RVR lives there), obs
// time, and weather string per station instead of just the sub/below verdict
function parseChaseLiveCsv(text: string): Map<string, LiveOb> {
  const out = new Map<string, LiveOb>();
  for (const line of text.split("\n")) {
    if (!line.startsWith('"')) continue;
    const close = line.indexOf('",');
    if (close < 0) continue;
    const raw = line.slice(1, close);
    const cols = line.slice(close + 2).split(",");
    const icao = cols[0];
    if (out.has(icao)) continue; // cache lists newest first per station
    const visRaw = cols[9];
    const vis = visRaw ? (visRaw.includes("+") ? 10 : parseFloat(visRaw)) : null;
    let ceil: number | null = null;
    for (const k of [21, 23, 25, 27]) {
      if (["BKN", "OVC", "OVX"].includes(cols[k])) {
        const b = parseFloat(cols[k + 1]);
        if (!Number.isNaN(b)) ceil = Math.min(ceil ?? Infinity, b);
      }
    }
    let rvr: string | null = null, rvrFt: number | null = null;
    for (const m of raw.matchAll(RVR_RE)) {
      const ft = m[2] === "P" ? 6001 : parseInt(m[3], 10);
      if (rvrFt == null || ft < rvrFt) { rvrFt = ft; rvr = `${m[1]}/${m[2]}${m[3]}`; }
    }
    const below = vis != null && vis < 0.19;
    const sub = (vis != null && vis < 0.5) || (ceil != null && ceil < 200);
    const num = (s: string) => { const v = parseFloat(s); return Number.isNaN(v) ? null : v; };
    out.set(icao, {
      obsMs: Date.parse(cols[1]) || 0,
      vis: vis != null && !Number.isNaN(vis) ? vis : null,
      ceil, rvr, rvrFt, wx: cols[20] || null, sub, below,
      tempC: num(cols[4]), dewpC: num(cols[5]), wspd: num(cols[7]),
    });
  }
  return out;
}

// ---- rolling per-station history: feeds the nowcast trend features ----
// One /api/now snapshot every 3 min while the board is open; entries dedupe
// on obs time, so we accumulate actual METARs, not fetch ticks. A single
// bounded /api/metar batch warm-starts the 40-90 min lookback per session.
interface HistOb { t: number; vis: number | null; ceil: number | null; sub: boolean }
const chaseHist = new Map<string, HistOb[]>();
let chaseWarmed = false;

function pushHist(icao: string, ob: HistOb) {
  const h = chaseHist.get(icao) ?? [];
  if (!h.some((x) => Math.abs(x.t - ob.t) < 30_000)) {
    h.push(ob);
    h.sort((a, b) => a.t - b.t);
    while (h.length && h[0].t < Date.now() - 2.5 * 3600_000) h.shift();
    chaseHist.set(icao, h);
  }
}

function updateChaseHist() {
  if (!chaseLive || !chaseData) return;
  for (const [icao, ob] of chaseLive) {
    if (ob.obsMs && chaseData.airports[icao])
      pushHist(icao, { t: ob.obsMs, vis: ob.vis, ceil: ob.ceil, sub: ob.sub });
  }
}

async function warmChaseHist(cands: ChaseCand[]) {
  const ids = cands.slice(0, 150).map((c) => c.a.icao);
  if (!ids.length) return;
  try {
    const arr = await (await fetch(`/api/metar?ids=${ids.join(",")}&hours=3`)).json();
    for (const o of Array.isArray(arr) ? arr : []) {
      const icao = o.icaoId;
      if (!icao || o.obsTime == null) continue;
      const vis = o.visib == null ? null
        : typeof o.visib === "string" ? (o.visib.includes("+") ? 10 : parseFloat(o.visib)) : o.visib;
      const cs = (o.clouds ?? [])
        .filter((c: any) => ["BKN", "OVC", "VV"].includes(c.cover) && c.base != null)
        .map((c: any) => c.base);
      const ceil = cs.length ? Math.min(...cs) : null;
      pushHist(icao, {
        t: o.obsTime * 1000, vis, ceil,
        sub: (vis != null && vis < 0.5) || (ceil != null && ceil < 200),
      });
    }
  } catch { /* trend features just start cold */ }
}

// ---- tab-open alerts: notify + chime when an airport ENTERS stratum 1 ----
// Baseline is the first refresh after open/base/filter changes (no blast on
// open); only fresh-ob transitions alert. Static site: works while the tab
// lives, no server push — that's a documented limitation, not a bug.
let chaseFogPrev: Set<string> | null = null;
let chimeCtx: AudioContext | null = null;

function playChime() {
  if (!chimeCtx) return;
  const t0 = chimeCtx.currentTime;
  for (const [freq, at, dur] of [[880, 0, 0.14], [659, 0.16, 0.22]] as const) {
    const o = chimeCtx.createOscillator();
    const g = chimeCtx.createGain();
    o.frequency.value = freq;
    g.gain.setValueAtTime(0, t0 + at);
    g.gain.linearRampToValueAtTime(0.07, t0 + at + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + dur);
    o.connect(g).connect(chimeCtx.destination);
    o.start(t0 + at); o.stop(t0 + at + dur + 0.05);
  }
}

function chaseAlertCheck(freshFog: ChaseCand[]) {
  const current = new Set(freshFog.map((c) => c.a.icao));
  const prev = chaseFogPrev;
  chaseFogPrev = current;
  if (!prev || !chasePrefs.alerts || !("Notification" in window) || Notification.permission !== "granted") return;
  for (const c of freshFog) {
    if (prev.has(c.a.icao)) continue;
    const ob = chaseLive?.get(c.a.icao);
    const n = new Notification(`${c.a.icao} below CAT I`, {
      body: `${ob?.vis != null ? `${ob.vis < 1 ? +ob.vis.toFixed(2) : Math.round(ob.vis)} SM` : ""}${ob?.rvr ? ` · R${ob.rvr} FT` : ""}${ob?.wx ? ` · ${ob.wx}` : ""} — ${eteStr(c.ete)} from ${chasePrefs.base}`,
      tag: `fa-chase-${c.a.icao}`,
    });
    n.onclick = () => { window.focus(); openAirport(c.a.icao); };
    playChime();
  }
}

function maybeWarmChase() {
  if (chaseWarmed || !chasePrefs.base || !chaseData) return;
  chaseWarmed = true; // one bounded batch per session, never re-polled
  warmChaseHist(chaseCandidates()).then(renderChase);
}

async function refreshChaseLive() {
  try {
    const text = await (await fetch("/api/now")).text();
    chaseLive = parseChaseLiveCsv(text);
    updateChaseHist();
    maybeWarmChase();
    if (chasePrefs.base) {
      chaseAlertCheck(chaseCandidates().filter((c) => {
        const ob = chaseLive!.get(c.a.icao);
        return ob?.sub && !obStale(ob);
      }));
    }
    renderChase();
  } catch { /* keep previous obs */ }
}

// port of the deep-dive nowcast feature builder, fed from the cache ob +
// rolling history instead of a per-airport /api/metar call
const tzFmtCache = new Map<string, Intl.DateTimeFormat>();
function nowcastPFor(a: Airport, ob: LiveOb): number | null {
  if (!nowcastModel || ob.vis == null) return null;
  const prev = (chaseHist.get(a.icao) ?? [])
    .filter((h) => { const dt = ob.obsMs - h.t; return dt > 2_400_000 && dt < 5_400_000; })
    .at(-1);
  let fmt = tzFmtCache.get(a.tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat("en-US", { timeZone: a.tz, hour: "numeric", month: "numeric", hour12: false });
    tzFmtCache.set(a.tz, fmt);
  }
  const parts = fmt.formatToParts(new Date());
  const mon = +(parts.find((p) => p.type === "month")?.value ?? 1);
  const hr = +(parts.find((p) => p.type === "hour")?.value ?? 0) % 24;
  const climP = Math.min(Math.max((a.grid[mon - 1]?.[hr] ?? 0) / 100, 1e-4), 1 - 1e-4);
  const tempF = ob.tempC != null ? ob.tempC * 1.8 + 32 : 59;
  const dewpF = ob.dewpC != null ? ob.dewpC * 1.8 + 32 : tempF - 10;
  const ceilFt = ob.ceil ?? 25000;
  const vis = ob.vis;
  const f: Record<string, number> = {
    vsby_c: Math.min(Math.max(vis, 0), 10),
    log_vis: Math.log1p(Math.min(Math.max(vis, 0), 10)),
    ceil_c: Math.min(ceilFt, 25000) / 1000,
    low_ceil: ceilFt < 1000 ? 1 : 0,
    spread: Math.min(Math.max(tempF - dewpF, -5), 40),
    tmpf_c: Math.min(Math.max(tempF, -40), 120),
    sknt_c: Math.min(Math.max(ob.wspd ?? 5, 0), 50),
    vis_trend_c: prev?.vis != null ? Math.min(Math.max(vis - prev.vis, -10), 10) : 0,
    sub_now: ob.sub ? 1 : 0,
    sub_prev_f: prev == null ? (ob.sub ? 1 : 0) : prev.sub ? 1 : 0,
    mon_sin: Math.sin((2 * Math.PI * mon) / 12),
    mon_cos: Math.cos((2 * Math.PI * mon) / 12),
    hr_sin: Math.sin((2 * Math.PI * hr) / 24),
    hr_cos: Math.cos((2 * Math.PI * hr) / 24),
    clim_logit: Math.log(climP / (1 - climP)),
  };
  let z = nowcastModel.intercept;
  for (const [k, c] of Object.entries(nowcastModel.coef)) z += (c as number) * (f[k] ?? 0);
  return 1 / (1 + Math.exp(-z));
}

const obAge = (ob: LiveOb) => Math.max(0, Math.round((Date.now() - ob.obsMs) / 60000));
const obStale = (ob: LiveOb) => !ob.obsMs || obAge(ob) > 75;

function liveLine(ob: LiveOb | undefined): string {
  if (!ob) return `<span class="chase-live dim">no current report</span>`;
  const bits = [
    ob.vis != null ? `${ob.vis < 1 ? +ob.vis.toFixed(2) : Math.round(ob.vis)} SM` : "vis ?",
    ob.rvr ? `R${ob.rvr} FT` : null,
    ob.ceil != null ? `ceil ${ob.ceil} ft` : null,
    ob.wx || null,
    `${obAge(ob)}m`,
  ].filter(Boolean).join(" · ");
  const pill = ob.below
    ? `<span class="pill red">below 300 m</span>`
    : ob.sub
    ? `<span class="pill amber">EFVS window</span>`
    : "";
  return `<span class="chase-live${obStale(ob) ? " stale" : ""}${ob.sub && !obStale(ob) ? "" : " dim"}">${bits}</span> ${obStale(ob) ? `<span class="pill dim">stale</span>` : pill}`;
}

function chaseCandidates(): ChaseCand[] {
  const base = state.airports.find((x) => x.icao === chasePrefs.base);
  if (!chaseData || !base) return [];
  const named: readonly string[] = CHASE_ALS_CHIPS.slice(0, -1);
  const sel = new Set(chasePrefs.als);
  const out: ChaseCand[] = [];
  for (const a of state.airports) {
    if (a.icao === base.icao) continue;
    const ends = chaseData.airports[a.icao];
    if (!ends) continue;
    const passing = ends.filter((e) =>
      e.als != null &&
      (sel.has(e.als) || (sel.has("OTHER") && !named.includes(e.als))) &&
      e.len >= chasePrefs.minLen &&
      e.tier <= chasePrefs.maxTier &&
      (!chasePrefs.rvrReq || e.rvr != null));
    if (!passing.length) continue;
    passing.sort((x, y) => x.tier - y.tier || y.len - x.len);
    const nm = distNm(base, a);
    const ete = nm / chasePrefs.speed;
    if (ete <= chasePrefs.maxEteH) out.push({ a, end: passing[0], nm, ete });
  }
  return out.sort((x, y) => x.ete - y.ete);
}

// dim the world to the chase set: dots filter to candidates+base, the fog
// field recedes, and a still-air range ring circles the base
function chaseApplyMap(cands: ChaseCand[], base: Airport | undefined, inFog: ChaseCand[] = []) {
  if (!map.getLayer("glow")) return;
  if (!map.getSource("chase-src")) {
    map.addSource("chase-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addSource("chase-live-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "chase-ring", type: "line", source: "chase-src",
      filter: ["==", ["geometry-type"], "LineString"],
      paint: { "line-color": "#9fd8ff", "line-opacity": 0.5, "line-width": 1.2, "line-dasharray": [2, 2.5] },
    });
    map.addLayer({
      id: "chase-live-glow", type: "circle", source: "chase-live-src",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 10, 6, 17],
        "circle-blur": 1.1,
        "circle-color": ["match", ["get", "sev"], "red", "#ff5a4d", "#ffb347"],
        "circle-opacity": 0.55,
      },
    });
    map.addLayer({
      id: "chase-live-core", type: "circle", source: "chase-live-src",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 3.4, 6, 5.5],
        "circle-color": ["match", ["get", "sev"], "red", "#ff5a4d", "#ffb347"],
        "circle-stroke-width": 1, "circle-stroke-color": "#fff8",
      },
    });
    map.addLayer({
      id: "chase-base", type: "circle", source: "chase-src",
      filter: ["==", ["geometry-type"], "Point"],
      paint: {
        "circle-radius": 5, "circle-color": "#e8b96a",
        "circle-stroke-width": 1.4, "circle-stroke-color": "#fff8",
      },
    });
    map.on("click", "chase-live-core", (ev) => {
      const icao = ev.features?.[0]?.properties.icao;
      if (icao) openAirport(icao);
    });
  }
  (map.getSource("chase-live-src") as any).setData({
    type: "FeatureCollection",
    features: inFog.map((c) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [c.a.lon, c.a.lat] },
      properties: { icao: c.a.icao, sev: chaseLive?.get(c.a.icao)?.below ? "red" : "amber" },
    })),
  });
  const feats: any[] = [];
  if (base) {
    const radiusNm = chasePrefs.speed * chasePrefs.maxEteH;
    const ring = Array.from({ length: 97 }, (_, i) => destPoint(base.lat, base.lon, i * 3.75, radiusNm));
    feats.push(
      { type: "Feature", geometry: { type: "LineString", coordinates: ring }, properties: {} },
      { type: "Feature", geometry: { type: "Point", coordinates: [base.lon, base.lat] }, properties: {} },
    );
  }
  (map.getSource("chase-src") as any).setData({ type: "FeatureCollection", features: feats });

  const icaos = [...cands.map((c) => c.a.icao), ...(base ? [base.icao] : [])];
  const inList: any = ["in", ["get", "icao"], ["literal", icaos]];
  for (const [id, baseF] of Object.entries(BASE_FILTERS)) {
    if (id === "fogheat" || id === "presence") continue;
    map.setFilter(id, (baseF ? ["all", baseF, inList] : inList) as any);
  }
  map.setPaintProperty("fogheat", "heatmap-opacity",
    ["interpolate", ["linear"], ["zoom"], 5, 0.35, 6.2, 0]);
}

function chaseFitMap(base: Airport) {
  const radiusNm = chasePrefs.speed * chasePrefs.maxEteH;
  const b = new maplibregl.LngLatBounds();
  for (const brg of [0, 90, 180, 270]) b.extend(destPoint(base.lat, base.lon, brg, radiusNm));
  map.fitBounds(b, { padding: { top: 90, bottom: 110, left: 60, right: 490 }, duration: 1400 });
}

function closeChaseMap() {
  if (!chaseOpen) return;
  chaseOpen = false;
  clearInterval(chaseTimer);
  chaseTimer = undefined;
  chaseFogPrev = null;
  if (map.getLayer("glow")) {
    applyApproachFilters();
    map.setPaintProperty("fogheat", "heatmap-opacity", FOGHEAT_OPACITY);
  }
  (map.getSource("chase-src") as any)?.setData({ type: "FeatureCollection", features: [] });
  (map.getSource("chase-live-src") as any)?.setData({ type: "FeatureCollection", features: [] });
}

function chaseRowBadges(end: ChaseEnd): string {
  const parts = [
    `<b>${end.e}</b>`,
    ALS_LABEL[end.als ?? ""] ?? end.als,
    end.ils ? `CAT ${end.ils}` : end.lpv ? "LPV" : null,
    `${end.len.toLocaleString()} ft`,
    end.rvr ? (end.rvr === "Y" ? "RVR" : `RVR ${end.rvr}`) : null,
    end.cur ? `<span title="infrastructure curated from AIP Canada / CFS sources — confidence-tagged, not authoritative FAA data">curated</span>` : null,
  ].filter(Boolean);
  return parts.join(" · ");
}

async function openChase() {
  history.replaceState(null, "", "#chase");
  closeDeployMap();
  setSelected(null);
  if (chaseData === undefined) {
    chaseData = null; // only try once
    try { chaseData = await (await fetch("/data/chase.json")).json(); } catch {}
  }
  if (nowcastModel === undefined) {
    nowcastModel = null;
    try { nowcastModel = await (await fetch("/data/model_y2.json")).json(); } catch {}
  }
  chaseOpen = true;
  renderChase();
  panel.hidden = false;
  document.body.classList.add("panel-open");
  if (!chaseTimer) {
    refreshChaseLive();
    chaseTimer = setInterval(refreshChaseLive, 180_000);
  }
}

function renderChase() {
  if (!chaseOpen) return;
  const base = state.airports.find((x) => x.icao === chasePrefs.base);
  const cands = chaseCandidates();
  const mon = new Date().getMonth();
  const nUS = chaseData?.meta?.us ?? (chaseData ? Object.keys(chaseData.airports).length : 0);
  const nCA = chaseData?.meta?.ca ?? 0;

  const alsChips = CHASE_ALS_CHIPS.map((k) => `
    <button class="equip-chip${chasePrefs.als.includes(k) ? " on" : ""}" data-als="${k}"
      title="${{ ALSF2: "2,400 ft high-intensity ALS with sequenced flashers — CAT II/III runways",
                 ALSF1: "2,400 ft high-intensity ALS, category I configuration",
                 MALSR: "1,400 ft medium-intensity ALS with runway alignment indicator lights — the standard CAT I system",
                 SSALR: "simplified short ALS with RAIL — ALSF-length centerline",
                 MALSF: "medium-intensity ALS with sequenced flashers",
                 OTHER: "any other approach light system (MALS, SALS/SALSF, ODALS, RLLS…)" }[k]}"
    >${ALS_LABEL[k]}</button>`).join("");

  const boardRows = (list: ChaseCand[], withLive: boolean) => list.map((c) => `
    <tr data-icao="${c.a.icao}">
      <td class="num" style="white-space:nowrap"><b>${eteStr(c.ete)}</b></td>
      <td class="num">${Math.round(c.nm)}</td>
      <td><b>${c.a.icao}</b> ${c.a.name.length > 20 ? c.a.name.slice(0, 19) + "…" : c.a.name}<br>
        <span class="chase-badges">${chaseRowBadges(c.end)}</span>${withLive ? `<br>${liveLine(chaseLive?.get(c.a.icao))}` : ""}</td>
      <td class="num" title="climatological hours below CAT I in ${MONTHS[mon]}">${monthClimH(c.a, mon)}h</td>
    </tr>`).join("");
  const boardTable = (rows: string, live: boolean) => `
    <table class="rank-table">
      <thead><tr><th class="num">ETE</th><th class="num">nm</th><th>airport · qualifying runway${live ? " · conditions" : ""}</th><th class="num" title="climatological hours below CAT I in ${MONTHS[mon]}">${MONTHS_S[mon]} ø</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  panelContent.innerHTML = `
    <h2>Fog chase</h2>
    <p class="sub">launch board for EFVS testing — ${nUS} US airports (FAA NASR)${nCA ? ` + ${nCA} Canadian curated` : ""}${chaseData ? "" : " — <b style='color:#ff9a9a'>chase.json failed to load</b>"}</p>

    <div class="chase-setup">
      ${base
        ? `<span class="base-chip" title="${base.name}">BASE ${base.icao}<button id="chase-base-clear" title="change base">×</button></span>`
        : `<span class="chase-base"><input id="chase-base-input" type="text" placeholder="set base airport…" autocomplete="off" spellcheck="false"/><div id="chase-base-results" hidden></div></span>`}
      <label>cruise <input id="chase-speed" type="number" min="60" max="600" step="10" value="${chasePrefs.speed}"/> kt</label>
      <label>max ETE <select id="chase-ete">
        ${[1, 1.5, 2, 2.5, 3, 4].map((h) => `<option value="${h}"${h === chasePrefs.maxEteH ? " selected" : ""}>${eteStr(h)}</option>`).join("")}
      </select></label>
      <button class="equip-chip${chasePrefs.alerts ? " on" : ""}" id="chase-alerts"
        title="While this tab is open: browser notification + chime when an airport on the board drops below CAT I. A static site can't wake your phone — leave the tab running.">♪ alerts</button>
    </div>

    <div class="chase-setup" style="margin-top:10px">
      ${alsChips}
    </div>
    <div class="chase-setup" style="margin-top:10px">
      <label>runway ≥ <select id="chase-len">
        ${[3000, 4000, 5000, 6000, 8000].map((l) => `<option value="${l}"${l === chasePrefs.minLen ? " selected" : ""}>${l.toLocaleString()} ft</option>`).join("")}
      </select></label>
      <label title="lowest published approach minima height on the runway end — a go-around above ~250 ft AGL can miss the lights entirely in real fog. Tier proxy: ILS CAT I/II/III → 200/100/50, LPV → 250, otherwise ≥350 (LNAV/circling).">go-around ≤ <select id="chase-tier">
        <option value="200"${chasePrefs.maxTier === 200 ? " selected" : ""}>200 ft (ILS)</option>
        <option value="250"${chasePrefs.maxTier === 250 ? " selected" : ""}>250 ft (ILS+LPV)</option>
        <option value="400"${chasePrefs.maxTier === 400 ? " selected" : ""}>any</option>
      </select></label>
      <button class="equip-chip${chasePrefs.rvrReq ? " on" : ""}" id="chase-rvr"
        title="only runways with RVR sensors — documented ground truth for test cards">RVR equipped</button>
    </div>

    ${!base ? `<p class="note" style="margin-top:18px">Set a base airport to build the chase board — candidates are ranked by still-air flight time at your cruise speed.</p>` : (() => {
      const inFog = cands.filter((c) => chaseLive?.get(c.a.icao)?.sub)
        .sort((x, y) => +obStale(chaseLive!.get(x.a.icao)!) - +obStale(chaseLive!.get(y.a.icao)!) || x.ete - y.ete);
      const quiet = cands.filter((c) => !chaseLive?.get(c.a.icao)?.sub);
      const soon = !chaseLive ? [] : cands
        .flatMap((c) => {
          const ob = chaseLive!.get(c.a.icao);
          if (!ob || ob.sub || obStale(ob)) return [];
          const p = nowcastPFor(c.a, ob);
          return p == null ? [] : [{ c, p }];
        })
        .sort((x, y) => y.p - x.p)
        .slice(0, 5);
      const soonRows = soon.map(({ c, p }) => `
        <tr data-icao="${c.a.icao}">
          <td class="num" style="white-space:nowrap"><b>${eteStr(c.ete)}</b></td>
          <td class="num">${Math.round(c.nm)}</td>
          <td><b>${c.a.icao}</b> ${c.a.name.length > 20 ? c.a.name.slice(0, 19) + "…" : c.a.name}<br>
            <span class="chase-badges">${chaseRowBadges(c.end)}</span><br>${liveLine(chaseLive?.get(c.a.icao))}
            <span class="pill ${p >= 0.2 ? "amber" : "dim"}" title="logistic nowcast trained 2016–23, validated 2024–25 (Brier −39% vs climatology, AUC 0.95). Experimental — not for operational use.">2 h: ${p < 0.01 ? "<1" : Math.round(p * 100)}%</span></td>
          <td class="num" title="climatological hours below CAT I in ${MONTHS[mon]}">${monthClimH(c.a, mon)}h</td>
        </tr>`).join("");
      return `
      <div class="stratum-h"><b style="color:#ffb347">IN FOG NOW</b><span class="n">${chaseLive ? inFog.length : "…"}</span><span>below CAT I this instant · obs ≤3 min old feed</span></div>
      ${!chaseLive ? `<p class="note">fetching live observations…</p>`
        : inFog.length ? boardTable(boardRows(inFog, true), true)
        : `<p class="note">nothing below CAT I within range right now.</p>`}
      <div class="stratum-h"><b style="color:var(--accent)">LIKELY SOON</b><span class="n">${chaseLive ? soon.length : "…"}</span><span>highest P(below CAT I within 2 h) — model nowcast, top 5</span></div>
      ${!chaseLive ? "" : soon.length ? boardTable(soonRows, true)
        : `<p class="note">${nowcastModel ? "no scoreable candidates (missing or stale observations)." : "nowcast model unavailable."}</p>`}
      <details class="chase-quiet">
        <summary class="stratum-h" style="cursor:pointer"><b>QUIET</b><span class="n">${quiet.length}</span><span>passing filters within ${eteStr(chasePrefs.maxEteH)} at ${chasePrefs.speed} kt (${Math.round(chasePrefs.speed * chasePrefs.maxEteH)} nm still-air)</span></summary>
        ${quiet.length ? boardTable(boardRows(quiet, false), false)
          : `<p class="note">No airports pass the filters within range — widen the ALS set, lower the runway/go-around bars, or extend max ETE.</p>`}
      </details>`;
    })()}
    <p class="note">ETE is great-circle still-air — no winds, no climb/descent. Infrastructure: FAA NASR ${chaseData?.meta?.nasr_file?.match(/\d{4}-\d{2}-\d{2}/)?.[0] ?? ""} + CIFP LPV; minima heights are tier proxies, not chart DAs. ${chaseData?.meta?.ca ? `Canadian fields are curated from AIP Canada/CFS research (confidence-tagged) — not authoritative FAA data.` : "US airports only — curated Canadian fields arrive in a later build."}</p>
  `;

  // wiring — settings changes rebaseline the alert set (no false "entered fog"
  // pings when membership shifts because a filter moved)
  const rerender = () => { saveChasePrefs(); chaseFogPrev = null; renderChase(); };
  panelContent.querySelector("#chase-alerts")?.addEventListener("click", async () => {
    if (!("Notification" in window)) return;
    if (!chasePrefs.alerts) {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return;
      chimeCtx ??= new AudioContext(); // user gesture — context stays usable later
      chimeCtx.resume();
      playChime();
      chasePrefs.alerts = true;
    } else {
      chasePrefs.alerts = false;
    }
    saveChasePrefs();
    renderChase();
  });
  panelContent.querySelector("#chase-base-clear")?.addEventListener("click", () => {
    chasePrefs.base = null; rerender();
  });
  const baseInput = panelContent.querySelector<HTMLInputElement>("#chase-base-input");
  const baseResults = panelContent.querySelector<HTMLElement>("#chase-base-results");
  baseInput?.addEventListener("input", () => {
    const q = baseInput.value.trim().toLowerCase();
    if (q.length < 2 || !baseResults) { if (baseResults) baseResults.hidden = true; return; }
    const hits = state.airports
      .filter((a) => a.icao.toLowerCase().includes(q) || a.name.toLowerCase().includes(q))
      .sort((a, b) => (a.icao.toLowerCase() === q ? 0 : 1) - (b.icao.toLowerCase() === q ? 0 : 1))
      .slice(0, 8);
    baseResults.innerHTML = hits.map((a) => `
      <div class="search-row" data-icao="${a.icao}"><b>${a.icao}</b><span>${a.name}</span></div>`).join("");
    baseResults.hidden = hits.length === 0;
    baseResults.querySelectorAll(".search-row").forEach((r) =>
      r.addEventListener("click", () => {
        chasePrefs.base = (r as HTMLElement).dataset.icao!;
        saveChasePrefs();
        chaseFogPrev = null;
        maybeWarmChase();
        renderChase();
        const b = state.airports.find((x) => x.icao === chasePrefs.base);
        if (b) chaseFitMap(b);
      }));
  });
  baseInput?.focus();
  panelContent.querySelector("#chase-speed")?.addEventListener("change", (e) => {
    chasePrefs.speed = Math.max(60, Math.min(600, +(e.target as HTMLInputElement).value || 250));
    rerender();
  });
  panelContent.querySelector("#chase-ete")?.addEventListener("change", (e) => {
    chasePrefs.maxEteH = +(e.target as HTMLSelectElement).value; rerender();
  });
  panelContent.querySelector("#chase-len")?.addEventListener("change", (e) => {
    chasePrefs.minLen = +(e.target as HTMLSelectElement).value; rerender();
  });
  panelContent.querySelector("#chase-tier")?.addEventListener("change", (e) => {
    chasePrefs.maxTier = +(e.target as HTMLSelectElement).value; rerender();
  });
  panelContent.querySelector("#chase-rvr")?.addEventListener("click", () => {
    chasePrefs.rvrReq = !chasePrefs.rvrReq; rerender();
  });
  panelContent.querySelectorAll(".equip-chip[data-als]").forEach((b) =>
    b.addEventListener("click", () => {
      const k = (b as HTMLElement).dataset.als!;
      chasePrefs.als = chasePrefs.als.includes(k)
        ? chasePrefs.als.filter((x) => x !== k) : [...chasePrefs.als, k];
      rerender();
    }));
  panelContent.querySelectorAll("tr[data-icao]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const a = state.airports.find((x) => x.icao === (tr as HTMLElement).dataset.icao)!;
      map.flyTo({ center: [a.lon, a.lat], zoom: 7, duration: 1600 });
      openAirport(a.icao);
    }));

  chaseApplyMap(cands, base, cands.filter((c) => {
    const ob = chaseLive?.get(c.a.icao);
    return ob?.sub && !obStale(ob); // stale fog obs stay off the map glow
  }));
}

$("#chase-btn").addEventListener("click", openChase);

// ---------- DEPLOY: where to base for the next two weeks ----------
let deployData: any | null | undefined;
let deployOpen = false;
let deployWindow: 7 | 14 = 14;

// expected chaseable hours over the window, discounted by reachability:
// a field only credits the fraction of its fog window that remains after
// the still-air transit (launch-at-onset model; +30 min margin)
function usableFrac(win: number | undefined, eteH: number): number {
  if (win == null || win <= 0) return 1; // no window info -> no discount
  return Math.min(Math.max((win - eteH - 0.5) / win, 0), 1);
}
function winEH(icao: string, eteH = 0): number {
  const d = deployData?.airports?.[icao];
  if (!d) return 0;
  let s = 0;
  for (let i = 0; i < Math.min(deployWindow, d.eh.length); i++)
    s += d.eh[i] * usableFrac(d.win?.[i], eteH);
  return s;
}

// same infrastructure filters the chase board saves — no base/distance term
function deployQualifying(): Set<string> {
  const out = new Set<string>();
  if (!chaseData) return out;
  const named: readonly string[] = CHASE_ALS_CHIPS.slice(0, -1);
  const sel = new Set(chasePrefs.als);
  for (const [icao, ends] of Object.entries(chaseData.airports)) {
    if ((ends as ChaseEnd[]).some((e) =>
      e.als != null &&
      (sel.has(e.als) || (sel.has("OTHER") && !named.includes(e.als))) &&
      e.len >= chasePrefs.minLen &&
      e.tier <= chasePrefs.maxTier &&
      (!chasePrefs.rvrReq || e.rvr != null))) out.add(icao);
  }
  return out;
}

interface DeployBase { b: Airport; s: number; contrib: { a: Airport; eh: number; nm: number }[] }

function deployRank(): { targets: { a: Airport; eh: number }[]; top: DeployBase[] } {
  const radius = chasePrefs.speed * chasePrefs.maxEteH;
  const qual = deployQualifying();
  const targets = state.airports
    .filter((a) => qual.has(a.icao))
    .map((a) => ({ a, eh: winEH(a.icao) }))
    .filter((t) => t.eh > 0.05);
  const scores: DeployBase[] = [];
  for (const b of state.airports) {
    let s = 0;
    const contrib: DeployBase["contrib"] = [];
    for (const t of targets) {
      if (Math.abs(t.a.lat - b.lat) > radius / 60 + 0.2) continue;
      const nm = distNm(b, t.a);
      if (nm <= radius) {
        const usable = winEH(t.a.icao, nm / chasePrefs.speed);
        if (usable > 0.05) { s += usable; contrib.push({ a: t.a, eh: usable, nm }); }
      }
    }
    if (s > 0.5) scores.push({ b, s, contrib: contrib.sort((x, y) => y.eh - x.eh) });
  }
  scores.sort((x, y) => y.s - x.s);
  const top: DeployBase[] = [];
  for (const c of scores) {
    if (top.some((t) => distNm(t.b, c.b) < 80)) continue;
    top.push(c);
    if (top.length >= 8) break;
  }
  return { targets, top };
}

function deployApplyMap(targets: { a: Airport; eh: number }[], top: DeployBase[], ringBase?: Airport) {
  if (!map.getLayer("glow")) return;
  if (!map.getSource("deploy-src")) {
    map.addSource("deploy-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addSource("deploy-ring-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({
      id: "deploy-eh", type: "circle", source: "deploy-src",
      filter: ["==", ["get", "kind"], "t"],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["get", "r"], 0, 2.5, 3, 8, 8, 16],
        "circle-color": "#9fd8ff", "circle-opacity": 0.55,
        "circle-stroke-width": 1, "circle-stroke-color": "#c4eaff88",
      },
    });
    map.addLayer({
      id: "deploy-base", type: "circle", source: "deploy-src",
      filter: ["==", ["get", "kind"], "b"],
      paint: {
        "circle-radius": 6, "circle-color": "#e8b96a",
        "circle-stroke-width": 1.6, "circle-stroke-color": "#fff9",
      },
    });
    map.addLayer({
      id: "deploy-ring", type: "line", source: "deploy-ring-src",
      paint: { "line-color": "#e8b96a", "line-opacity": 0.6, "line-width": 1.2, "line-dasharray": [2, 2.5] },
    });
    map.on("click", "deploy-eh", (ev) => {
      const icao = ev.features?.[0]?.properties.icao;
      if (icao) openAirport(icao);
    });
  }
  (map.getSource("deploy-src") as any).setData({
    type: "FeatureCollection",
    features: [
      ...targets.map((t) => ({
        type: "Feature", geometry: { type: "Point", coordinates: [t.a.lon, t.a.lat] },
        properties: { kind: "t", icao: t.a.icao, r: Math.sqrt(t.eh) },
      })),
      ...top.map((c) => ({
        type: "Feature", geometry: { type: "Point", coordinates: [c.b.lon, c.b.lat] },
        properties: { kind: "b", icao: c.b.icao },
      })),
    ],
  });
  const ring = ringBase
    ? [{ type: "Feature", geometry: { type: "LineString",
        coordinates: Array.from({ length: 97 }, (_, i) => destPoint(ringBase.lat, ringBase.lon, i * 3.75, chasePrefs.speed * chasePrefs.maxEteH)) }, properties: {} }]
    : [];
  (map.getSource("deploy-ring-src") as any).setData({ type: "FeatureCollection", features: ring });

  const icaos = [...targets.map((t) => t.a.icao), ...top.map((c) => c.b.icao)];
  const inList: any = ["in", ["get", "icao"], ["literal", icaos]];
  for (const [id, baseF] of Object.entries(BASE_FILTERS)) {
    if (id === "fogheat" || id === "presence") continue;
    map.setFilter(id, (baseF ? ["all", baseF, inList] : inList) as any);
  }
  map.setPaintProperty("fogheat", "heatmap-opacity",
    ["interpolate", ["linear"], ["zoom"], 5, 0.35, 6.2, 0]);
}

function closeDeployMap() {
  if (!deployOpen) return;
  deployOpen = false;
  if (map.getLayer("glow")) {
    applyApproachFilters();
    map.setPaintProperty("fogheat", "heatmap-opacity", FOGHEAT_OPACITY);
  }
  (map.getSource("deploy-src") as any)?.setData({ type: "FeatureCollection", features: [] });
  (map.getSource("deploy-ring-src") as any)?.setData({ type: "FeatureCollection", features: [] });
}

async function openDeploy() {
  history.replaceState(null, "", "#deploy");
  closeChaseMap();
  setSelected(null);
  // the planner talks about the NEXT two weeks — scrub the underlying
  // climatology field/dots to the current month, not the January default
  setMonths([new Date().getMonth()]);
  if (chaseData === undefined) {
    chaseData = null;
    try { chaseData = await (await fetch("/data/chase.json")).json(); } catch {}
  }
  if (deployData === undefined) {
    deployData = null;
    try { deployData = await (await fetch("/api/deploy")).json(); } catch {}
  }
  deployOpen = true;
  renderDeploy();
  panel.hidden = false;
  document.body.classList.add("panel-open");
}

// first day in the window with a REACHABLE field >=50% likely chaseable —
// the operator's "when could we actually go" number
function firstLikely(c: DeployBase): { label: string; p: number; icao: string } | null {
  if (!deployData) return null;
  for (let d = 0; d < deployWindow; d++) {
    let best = 0, bi = "";
    for (const x of c.contrib) {
      const da = deployData.airports[x.a.icao];
      const p = da?.p?.[d];
      const win = da?.win?.[d];
      if (win != null && win > 0 && win <= x.nm / chasePrefs.speed + 0.75) continue;
      if (p != null && p > best) { best = p; bi = x.a.icao; }
    }
    if (best >= 0.5) {
      const label = d === 0 ? "tomorrow"
        : new Date(Date.now() + (d + 1) * 864e5).toLocaleDateString("en-US", { weekday: "short" });
      return { label, p: best, icao: bi };
    }
  }
  return null;
}

// next-48h fog character at one airport, in ITS local time, from the live
// calibrated hourly curve: peak severity, the fog window, and burn-off
function fogCharacter(a: Airport, fc: any): string | null {
  const f = fc?.airports?.[a.icao];
  if (!f) return null;
  const cyc = new Date(fc.meta.cycle);
  const hrOf = (fhr: number) => +new Intl.DateTimeFormat("en-US",
    { timeZone: a.tz, hour: "numeric", hour12: false })
    .format(new Date(cyc.getTime() + fhr * 3600e3)) % 24;
  const p10 = f.p.map((r: number[]) => r[0]);
  const peak = Math.max(...p10);
  if (peak < 15) return `<span class="chase-live dim">quiet next 48 h (peak ${peak}%)</span>`;
  const inWin = f.fhrs.map((_: number, i: number) => p10[i] >= Math.max(15, peak * 0.4));
  const first = inWin.indexOf(true), last = inWin.lastIndexOf(true);
  const p05 = Math.max(...f.p.map((r: number[]) => r[1]));
  const p025 = Math.max(...f.p.map((r: number[]) => r[2]));
  const sev = p025 >= 25 ? `<span class="pill red">dense (¼ mi) ${p025}%</span>`
    : p05 >= 25 ? `<span class="pill amber">½ mi ${p05}%</span>`
    : `<span class="pill dim">marginal</span>`;
  const per = persistenceTable?.[a.icao];
  const dur = per ? ` · typ. event ${per.medianH}h` : "";
  return `<span class="chase-live">fog ${peak}% · window ~${String(hrOf(f.fhrs[first])).padStart(2, "0")}–${String(hrOf(f.fhrs[last])).padStart(2, "0")} local` +
    `${last < f.fhrs.length - 1 ? `, clears by ~${String(hrOf(f.fhrs[Math.min(last + 1, f.fhrs.length - 1)])).padStart(2, "0")}` : ""}${dur}</span> ${sev}`;
}

function renderDeploy(ringBase?: Airport) {
  if (!deployOpen) return;
  const radius = Math.round(chasePrefs.speed * chasePrefs.maxEteH);
  const { targets, top } = deployData ? deployRank() : { targets: [], top: [] };
  const gen = deployData?.meta?.generated?.slice(0, 10) ?? "—";
  const home = state.airports.find((x) => x.icao === chasePrefs.base);
  const rows = top.map((c, i) => {
    const fl = firstLikely(c);
    const dep = home
      ? (home.icao === c.b.icao
        ? `<span class="chase-badges">you're here</span>`
        : `<span class="chase-badges" title="one-time repositioning: still-air time from your CHASE base ${home.icao} at ${chasePrefs.speed} kt — shown for planning, not factored into the ranking">deploy in ${eteStr(distNm(home, c.b) / chasePrefs.speed)}</span>`)
      : "";
    return `
    <tr data-icao="${c.b.icao}"${ringBase?.icao === c.b.icao ? ' style="background:#16202b"' : ""}>
      <td class="num">${i + 1}</td>
      <td><b>${c.b.icao}</b> ${c.b.name.length > 22 ? c.b.name.slice(0, 21) + "…" : c.b.name}<br>
        <span class="chase-badges" title="destination fields with flight time from ${c.b.icao} (still-air at ${chasePrefs.speed} kt)">go to: ${c.contrib.slice(0, 3).map((x) => `${x.a.icao} in ${eteStr(x.nm / chasePrefs.speed)}`).join(" · ")}</span><br>
        <span class="chase-badges" title="the first day in the window when a reachable field is at least 50% likely to go below CAT I">${fl ? `fog likely ${fl.label}` : "no strong fog day in window"}</span></td>
      <td class="num" title="expected chaseable fog hours at fields within ${radius} nm over the next ${deployWindow} days, counting only hours reachable before the fog lifts"><b>${c.s.toFixed(0)}h</b><br>${dep}</td>
    </tr>`;
  }).join("");

  // GO / MARGINAL / SCRAP verdict for the selected (or top) base: per day,
  // the best in-reach field's P(chaseable); a day counts when that P >= 50%
  const vb = (ringBase && top.find((c) => c.b.icao === ringBase.icao)) || top[0];
  let verdictHtml = "";
  const hasP = !!vb && vb.contrib.some((x) => Array.isArray(deployData?.airports?.[x.a.icao]?.p));
  if (vb && deployData && hasP) {
    const cells: { best: number; bi: string; bt: string }[] = [];
    let likely = 0;
    for (let d = 0; d < deployWindow; d++) {
      let best = 0, bi = "", bt = "";
      for (const x of vb.contrib) {
        const da = deployData.airports[x.a.icao];
        const p = da?.p?.[d];
        // reachability: the field's fog window must outlast transit + 45 min
        const win = da?.win?.[d];
        if (win != null && win > 0 && win <= x.nm / chasePrefs.speed + 0.75) continue;
        if (p != null && p > best) { best = p; bi = x.a.icao; bt = da.tiers[d]; }
      }
      if (best >= 0.5) likely++;
      cells.push({ best, bi, bt });
    }
    const need = deployWindow >= 14 ? 3 : 2;
    const [word, color] = likely >= need ? ["GO", "#7fd49a"] : likely >= 1 ? ["MARGINAL", "#ffb347"] : ["SCRAP", "#ff5a4d"];
    const sk = deployData.meta?.dayscale_skill;
    verdictHtml = `<div style="background:#111823;border:1px solid #233240;border-radius:10px;padding:12px 14px;margin:12px 0">
      <b style="color:${color};font-size:1.05rem;letter-spacing:.04em">${word}</b>
      <span style="color:var(--ink)"> — ${likely} of ${deployWindow} days have a field ≥50% likely chaseable from <b>${vb.b.icao}</b></span>
      <div style="display:flex;gap:2px;margin-top:9px">${cells.map((c, i) =>
        `<div title="day +${i + 1}: ${c.bi || "no field"} ${(100 * c.best).toFixed(0)}% (${c.bt || "—"})" style="flex:1;height:15px;border-radius:3px;background:${c.best >= 0.5 ? "#7fc0e8" : "#3f76a3"};opacity:${(0.18 + 0.82 * c.best).toFixed(2)}"></div>`).join("")}</div>
      <div class="note" style="margin-top:6px">day cells = best in-reach field's P(chaseable) · ${sk != null ? `days 3–8 <b>fitted</b> (2025 holdout ${sk > 0 ? "+" : ""}${sk.toFixed(1)}% vs climatology)` : "days 3–8 advisory until the fitted tier ships"}</div>
    </div>`;
  }

  panelContent.innerHTML = `
    <h2>Deploy planner</h2>
    <p class="sub">where to base for the fog — expected chaseable hours, next ${deployWindow} days · data ${gen}${deployData ? "" : " — <b style='color:#ff9a9a'>deploy data unavailable</b>"}</p>
    ${verdictHtml}
    <div class="chase-setup">
      ${([7, 14] as const).map((w) => `<button class="equip-chip${deployWindow === w ? " on" : ""}" data-win="${w}">next ${w} days</button>`).join("")}
      <label>cruise <input id="deploy-speed" type="number" min="60" max="600" step="10" value="${chasePrefs.speed}"/> kt</label>
      <label>max ETE <select id="deploy-ete">
        ${[1, 1.5, 2, 2.5, 3, 4].map((h) => `<option value="${h}"${h === chasePrefs.maxEteH ? " selected" : ""}>${eteStr(h)}</option>`).join("")}
      </select></label>
      <span style="color:var(--ink-dim)">= ${radius} nm reach · shared with <a href="#chase" id="deploy-tochase">CHASE</a></span>
    </div>
    <p class="note" style="margin-top:8px">Airports must pass your saved CHASE filters. Chaseable = below CAT I. Tier honesty: days 1–2 <b>calibrated</b>; days 3–8 climatology × NBM-extended fog ingredients (<b>advisory, unfitted</b>); days 9–14 climatology × CPC moisture outlook (<b>advisory</b>, US only — Canadian airports stay pure climatology there).</p>
    <div class="stratum-h"><b style="color:#e8b96a">BEST BASES</b><span class="n">${top.length}</span><span>ranked by expected chaseable hours within reach</span></div>
    ${rows ? `<table class="rank-table"><thead><tr><th>#</th><th>base · top nearby fog</th><th class="num" title="expected chaseable hours within reach over the window · bottom line: next-48h portion (calibrated)">chaseable hrs</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="note">No expected fog within range of any base under the current filters — widen the CHASE filters or the window.</p>`}
    <div id="deploy-48"></div>
    <p class="note">Fields credit only the fog-window hours remaining after still-air transit (launch-at-onset + 30 min margin) — a distant field whose fog lifts before arrival counts near zero. Contributor times are ETE from the base.</p>
    <p class="note">Click a base to ring its reach on the map; click a blue dot for that airport's deep-dive. Dot size = expected chaseable hours. The daily build logs every advisory prediction — the extended tiers earn verification the same way the 48 h model did.</p>`;

  panelContent.querySelectorAll(".equip-chip[data-win]").forEach((b) =>
    b.addEventListener("click", () => {
      deployWindow = +(b as HTMLElement).dataset.win! as 7 | 14;
      renderDeploy();
    }));
  panelContent.querySelector("#deploy-tochase")?.addEventListener("click", (e) => {
    e.preventDefault(); openChase();
  });
  panelContent.querySelector("#deploy-speed")?.addEventListener("change", (e) => {
    chasePrefs.speed = Math.max(60, Math.min(600, +(e.target as HTMLInputElement).value || 250));
    saveChasePrefs(); renderDeploy(ringBase);
  });
  panelContent.querySelector("#deploy-ete")?.addEventListener("change", (e) => {
    chasePrefs.maxEteH = +(e.target as HTMLSelectElement).value;
    saveChasePrefs(); renderDeploy(ringBase);
  });
  panelContent.querySelectorAll("tr[data-icao]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const b = state.airports.find((x) => x.icao === (tr as HTMLElement).dataset.icao)!;
      renderDeploy(b);
      map.flyTo({ center: [b.lon, b.lat], zoom: 5.2, duration: 1400 });
    }));

  // selected base: expand its top fields into next-48h character lines
  // (live calibrated curve -> severity, window, burn-off; persistence medians)
  if (ringBase) {
    const sel = top.find((c) => c.b.icao === ringBase.icao);
    if (sel) {
      (async () => {
        if (persistenceTable === undefined) {
          persistenceTable = null;
          try { persistenceTable = await (await fetch("/data/persistence.json")).json(); } catch {}
        }
        const fc = await getForecast();
        const el = panelContent.querySelector("#deploy-48");
        if (!el || !fc) return;
        const lines = sel.contrib.slice(0, 5).map((x) => {
          const ch = fogCharacter(x.a, fc);
          return ch ? `<tr data-icao="${x.a.icao}"><td><b>${x.a.icao}</b> <span class="chase-badges">${Math.round(x.nm)} nm · ${eteStr(x.nm / chasePrefs.speed)}</span></td><td>${ch}</td></tr>` : "";
        }).filter(Boolean).join("");
        el.innerHTML = `
          <div class="stratum-h"><b style="color:#9fd8ff">NEXT 48 H FROM ${sel.b.icao}</b><span></span><span>calibrated live forecast at the top fields in reach</span></div>
          ${lines ? `<table class="rank-table"><tbody>${lines}</tbody></table>` : `<p class="note">no live forecast coverage at this base's top fields.</p>`}
          <p class="note">Window = hours with fog probability ≥40% of its peak; severity pills show the strongest threshold with ≥25% chance. Beyond 48 h the planner is climatology-guided until the fitted extended tier (V3.2) lands.</p>`;
        el.querySelectorAll("tr[data-icao]").forEach((tr) =>
          tr.addEventListener("click", () => openAirport((tr as HTMLElement).dataset.icao!)));
      })();
    }
  }

  deployApplyMap(targets, top, ringBase);
  if (!ringBase && top.length && !map.isMoving()) {
    const b = new maplibregl.LngLatBounds();
    top.forEach((c) => b.extend([c.b.lon, c.b.lat]));
    targets.slice(0, 40).forEach((t) => b.extend([t.a.lon, t.a.lat]));
    map.fitBounds(b, { padding: { top: 90, bottom: 110, left: 60, right: 490 }, duration: 1400, maxZoom: 6 });
  }
}

$("#deploy-btn").addEventListener("click", openDeploy);

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
  openChase,
  chasePrefs,
  refreshChaseLive,
};
