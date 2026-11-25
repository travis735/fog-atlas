import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import * as Plot from "@observablehq/plot";
import "./style.css";
import {
  readFeed, feedThrown, feedTrackInit, feedTrackRead, feedView, feedDownAlertDue, feedBanner, feedDownWords,
  jsonCeil, jsonVis, subCat1, chunkIds, ageText, clockText,
  FEED_POLL_MS, FEED_RETRY_MS, FEED_TIMEOUT_MS, FEED_REPORT_MAX_AGE_MS, FEED_REASON_LABEL, AWC_ROW_CAP, METAR_CHUNK,
  type LiveOb, type FeedRead, type FeedTrack, type FeedView,
} from "./feed.ts";

// maplibre 6 finds its worker next to its own module URL, which the bundle
// rewrites — point it at the copy vite emits instead
maplibregl.setWorkerUrl(maplibreWorkerUrl);

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const MONTHS_S = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

interface Airport {
  icao: string; name: string; muni?: string; st?: string;
  lat: number; lon: number; country: string; tz: string;
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
  window: null as { start: string; through: string; hours: number } | null,
};

// climatology span for display: "2016–2026" once the archive extends past
// the original decade, with the exact data-through date where useful
const winSpan = () => state.window
  ? `${state.window.start.slice(0, 4)}–${state.window.through.slice(0, 4)}`
  : "2016–2025";

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
  if (data.window) {
    state.window = data.window;
    const tag = document.querySelector("#brand p");
    if (tag) {
      tag.textContent = `where the sky closes · ${winSpan()} METAR climatology`;
      (tag as HTMLElement).title = `data through ${data.window.through}`;
    }
  }

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
      if (deployOpen) return; // deploy mode owns map clicks (padded, orange-first)
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
let nowTimer: ReturnType<typeof setTimeout> | undefined;
let nowSeq = 0;
let nowShownAt: number | null = null; // when the glow on the map last came from a healthy read

// One read of /api/now for whoever asks (this layer and the CHASE board share
// a request that is already in flight). It never throws and never hands back
// a guess: the result is a healthy read or a failed one with a named reason
// (./feed.ts). cache: "no-store" because the response is max-age=180, the same
// as the poll period, so a poll could otherwise be answered by the browser
// cache and a "feed checked" time would be a lie.
let feedInFlight: Promise<FeedRead> | null = null;
function readLiveFeed(): Promise<FeedRead> {
  if (!feedInFlight) {
    const p = (async () => {
      // a request that hangs (headers or body) must become a failed read
      // before the next retry is due, or the board waits on it forever
      const ctl = new AbortController();
      const kill = setTimeout(() => ctl.abort(), FEED_TIMEOUT_MS);
      try {
        const r = await fetch("/api/now", { cache: "no-store", signal: ctl.signal });
        return readFeed(r.status, r.ok ? await r.text() : "",
          chaseData ? Object.keys(chaseData.airports) : null, Date.now());
      } catch (e) {
        return feedThrown(e);
      } finally {
        clearTimeout(kill);
      }
    })();
    feedInFlight = p;
    p.finally(() => { if (feedInFlight === p) feedInFlight = null; });
  }
  return feedInFlight;
}

// a failed read must not look like a clear sky: the last glow stays, dimmed
function nowGlowDim(dim: boolean) {
  if (!map.getLayer("now-glow")) return;
  map.setPaintProperty("now-glow", "circle-opacity", dim ? 0.18 : 0.55);
  map.setPaintProperty("now-core", "circle-opacity", dim ? 0.35 : 1);
}

async function refreshNow() {
  const seq = ++nowSeq;
  clearTimeout(nowTimer);
  // The read is held against the chase airports (feed.ts coverage gates). With
  // the list not loaded only the 3,000-station floor was left, and a file cut
  // to 70% of its lines passed it: "7 below CAT I now" where the whole file
  // gave 17. So the list is fetched here too, not only when CHASE opens. If
  // it will not load, the read goes ahead on the floor alone.
  await loadChaseData();
  const read = await readLiveFeed();
  if (seq !== nowSeq) return; // NOW was switched off, or restarted, while this read was in flight
  try {
    paintNow(read);
  } finally {
    // Scheduled in a finally: the next read used to be the last statement
    // after the painting, so one exception in between ended the layer's
    // polling for good with the chip still showing its last count.
    // 60 s while the feed is down, 180 s once it is healthy (same as the CHASE board).
    if (seq === nowSeq) nowTimer = setTimeout(refreshNow, read.ok ? FEED_POLL_MS : FEED_RETRY_MS);
  }
}

function paintNow(read: FeedRead) {
  const chip = $("#now-chip");
  chip.hidden = false;
  if (read.ok) {
    const byIcao = new Map(state.airports.map((a) => [a.icao, a]));
    const feats: any[] = [];
    let reds = 0;
    for (const [icao, ob] of read.obs) {
      const a = ob.sub ? byIcao.get(icao) : undefined;
      if (!a) continue;
      if (ob.below) reds++;
      feats.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [a.lon, a.lat] },
        properties: { icao, sev: ob.below ? "red" : "amber" },
      });
    }
    (map.getSource("nowsrc") as any)?.setData({ type: "FeatureCollection", features: feats });
    nowShownAt = Date.now();
    nowGlowDim(false);
    chip.textContent = `${feats.length} below CAT I now${reds ? ` · ${reds} below 300 m` : ""}`;
  } else {
    // Until 2026-11-24 a 502 or an error page parsed to nothing and this chip
    // said "0 below CAT I now" over an emptied map. Say the feed is down and
    // why, and keep what was last known on the map, dimmed and timed.
    nowGlowDim(true);
    chip.textContent = `live feed unavailable — ${FEED_REASON_LABEL[read.reason]}` +
      (nowShownAt ? ` · map shows ${clockText(nowShownAt)}` : "");
  }
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
    refreshNow(); // schedules its own next read: 180 s after a healthy one, 60 s after a failed one
  } else {
    clearTimeout(nowTimer);
    nowSeq++; // a read still in flight must not repaint the chip or restart the timer
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
  panelToolSeq++; // a CHASE or DEPLOY that is still loading must not open over this
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
  panelToolSeq++; // a CHASE or DEPLOY that is still loading must not open over this
  closeChaseMap();
  closeDeployMap();
  setSelected(null);
  panelContent.innerHTML = `
    <h2>Methodology</h2>
    <p class="sub">what this map can and cannot tell you</p>
    <h3>Observation basis</h3>
    <p class="note" style="margin-top:6px">Routine hourly METARs ${winSpan()} (Iowa Environmental Mesonet archive), one observation per hour — the last routine report in each UTC hour. SPECIs are deliberately excluded so frequencies are an unbiased sample of hours. "% of hours" uses hours with a valid visibility report as the denominator; per-airport archive coverage is shown in each deep-dive.</p>
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
  panelToolSeq++; // a CHASE or DEPLOY that is still loading must not open over this
  closeChaseMap();
  closeDeployMap();
  setSelected(null);
  history.replaceState(null, "", location.pathname);
});

let persistenceTable: Record<string, any> | null | undefined;
let nowcastModel: any | null | undefined;

// V3 forecast (shadow-aware): one /api/forecast fetch, cached 10 min
let fcCache: { at: number; data: any } | null = null;
// Only a real issuance is returned or cached. /api/forecast answers 503 with a
// JSON body ({"error": ...}) when KV has nothing: that parsed fine, was cached
// for 10 minutes and threw "Cannot read properties of undefined (reading
// 'cycle')" in the DEPLOY launch plan, leaving an empty section under a GO.
async function getForecast(): Promise<any | null> {
  if (fcCache && Date.now() - fcCache.at < 600_000) return fcCache.data;
  try {
    const r = await fetch("/api/forecast");
    if (!r.ok) return null;
    const data = await r.json();
    if (!data?.meta?.cycle || !data.airports || typeof data.airports !== "object") return null;
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
  panelToolSeq++; // a CHASE or DEPLOY that is still loading must not open over this
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
    <p class="note">Sub-CAT-I = prevailing visibility below ~800 m or ceiling below 200 ft. Visibility is a climatological proxy for RVR — read as relative risk, not operating minima. Hours are local (${a.tz}). ${winSpan()} routine METARs, archive coverage ${detail.coveragePct}%. <a href="https://github.com/travis735/fog-atlas/blob/main/METHODOLOGY.md" target="_blank" rel="noopener">Full methodology</a>.</p>
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
  // an error status is not a report: the proxy answers 400/502 with text
  fetch(`/api/metar?ids=${icao}&hours=3`).then((r) => (r.ok ? r.json() : null)).then((arr) => {
    const ob = Array.isArray(arr) ? arr[0] : null;
    const liveEl = panelContent.querySelector("#live");
    if (!ob || !liveEl) return;
    // The same readers and the same two rules as the CHASE board (feed.ts), so
    // the two screens cannot disagree. This block had neither rule: on
    // 2026-11-25 the board listed KMER and KNYL under NO CURRENT REPORT and a
    // click on either row opened "RIGHT NOW … 7 SM, no ceiling · normal ops"
    // from a report 1 h 50 min old; a report with no visibility read
    // "? SM, no ceiling · normal ops".
    const parseVis = jsonVis;
    // AWC's JSON writes vertical visibility as cover "OVX" (this used to look
    // for "VV" and saw no ceiling)
    const ceilOf = (o: any) => jsonCeil(o.clouds);
    const vis = parseVis(ob.visib);
    const ceil = ceilOf(ob);
    const sub = subCat1(vis, ceil);
    const below = vis != null && vis < 0.19;
    const when = (String(ob.rawOb ?? "").match(/\d{6}Z/) ?? [""])[0];
    const obsMs = typeof ob.obsTime === "number" ? ob.obsTime * 1000 : 0;
    const ageMs = obsMs ? Date.now() - obsMs : null;
    const said = `${vis != null ? `${ob.visib} SM` : "visibility not reported"}${ceil != null ? `, ceiling ${ceil} ft` : vis != null ? ", no ceiling" : ""}`;
    if (ageMs == null || ageMs > FEED_REPORT_MAX_AGE_MS) {
      // over 75 min old: what it said is shown as history, with no verdict
      // and no nowcast — neither is about now
      liveEl.innerHTML = `
      <div class="live-line">NO CURRENT REPORT · last report ${escHtml(when || "time unknown")}${ageMs != null ? `, ${ageText(ageMs)} old` : ""} — it said ${escHtml(said)}.</div>`;
      return;
    }
    if (vis == null && !sub) {
      // no visibility and no ceiling under 200 ft: the report cannot say
      // whether the field is below CAT I, so nothing here may say it is not
      liveEl.innerHTML = `
      <div class="live-line">LATEST REPORT · ${escHtml(when)}, ${ageText(ageMs)} old — ${escHtml(said)}. It does not say whether the field is below CAT I.</div>`;
      return;
    }
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

    liveEl.innerHTML = `
      <div class="live-line">RIGHT NOW · ${escHtml(when)}, ${ageText(ageMs)} old — ${escHtml(said)} · ${verdict}.${lift}${nowcast}</div>`;
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
  base: string | null; baseSetAt: number | null; speed: number; maxEteH: number;
  als: string[]; minLen: number; rvrReq: boolean; maxTier: number;
  alerts: boolean;
}

const CHASE_DEFAULTS: ChasePrefs = {
  base: null, baseSetAt: null, speed: 250, maxEteH: 2,
  als: ["ALSF2", "ALSF1", "MALSR"], minLen: 5000, rvrReq: false, maxTier: 250,
  alerts: false,
};
const CHASE_ALS_CHIPS = ["ALSF2", "ALSF1", "MALSR", "SSALR", "MALSF", "OTHER"] as const;
const chasePrefs: ChasePrefs = (() => {
  try { return { ...CHASE_DEFAULTS, ...JSON.parse(localStorage.getItem("fa-chase") ?? "{}") }; }
  catch { return { ...CHASE_DEFAULTS }; }
})();
const saveChasePrefs = () => localStorage.setItem("fa-chase", JSON.stringify(chasePrefs));

// "Arcata, CA" — city/state for reading an identifier as a place
const placeTxt = (a?: Airport | null) =>
  a?.muni ? `${a.muni}${a.st ? ", " + a.st : ""}` : "";
const icaoPlace = (icao: string) => {
  const a = state.airports.find((x) => x.icao === icao);
  const p = placeTxt(a);
  return p ? `<b>${icao}</b> <span class="chase-badges">(${p})</span>` : `<b>${icao}</b>`;
};

// stale-base guard: a forgotten saved base silently skews both boards (every
// ETE, deploy-by time, and ranking radius hangs off it) — surface its age and
// nudge for re-confirmation when it's old or predates age tracking
function baseAge(): { label: string; stale: boolean } | null {
  if (!chasePrefs.base) return null;
  const t = chasePrefs.baseSetAt;
  if (t == null) return { label: "set a while ago", stale: true };
  const d = Math.floor((Date.now() - t) / 86400e3);
  const label = d < 1 ? "set today" : d < 14 ? `set ${d}d ago` : `set ${Math.round(d / 7)}w ago`;
  return { label, stale: d > 21 };
}

let chaseData: { meta: any; airports: Record<string, ChaseEnd[]> } | null | undefined;
let chaseOpen = false;

// chase.json: the airport list both boards and the feed's coverage gates stand
// on. One request at a time, shared by whoever asks, and a failure is not
// remembered: CHASE and DEPLOY each latched `null` after one failed fetch, so
// one dropped request left "IN FOG NOW 0 of 0 … no airports pass the filters
// within range" (and DEPLOY's "No expected fog within range of any base") on
// screen for the rest of the session. An error status or a body with no
// airports is a failure too.
let chaseDataLoading: Promise<boolean> | null = null;
function loadChaseData(): Promise<boolean> {
  if (chaseData) return Promise.resolve(true);
  chaseDataLoading ??= (async () => {
    try {
      const r = await fetch("/data/chase.json");
      const j = r.ok ? await r.json() : null;
      if (j?.airports && typeof j.airports === "object" && Object.keys(j.airports).length) chaseData = j;
    } catch { /* stays unloaded; the next caller tries again */ }
    if (!chaseData) chaseData = null;
    chaseDataLoading = null;
    return !!chaseData;
  })();
  return chaseDataLoading;
}

// CHASE and DEPLOY share one panel and both open after awaits. Whichever was
// asked for LAST owns it: clicking CHASE then DEPLOY on a slow first load used
// to settle on the CHASE panel under the #deploy hash (and the other way
// round, with the CHASE timers running under the planner).
let panelToolSeq = 0;

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
// The parser and the health gates are in ./feed.ts (shared with the NOW layer).
// chaseLive is "the observations on hand", NOT "current conditions": it keeps
// the last readable set through an outage. Whether it may be shown as current
// is feedView(chaseFeed, now).phase === "ok", asked again at every repaint.
let chaseLive: Map<string, LiveOb> | null = null;
let chaseFeed: FeedTrack = feedTrackInit();
let chaseTimer: ReturnType<typeof setTimeout> | undefined;      // next read: 180 s healthy, 60 s failed
let chaseTickTimer: ReturnType<typeof setInterval> | undefined; // 30 s: ages and banner advance in place
let chaseReadSeq = 0;
const CHASE_TICK_MS = 30_000;

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

// The warm start goes out in chunks, one after another. It used to be a single
// request for up to 150 airports, which failed twice over: /api/metar took one
// id only and answered 400 (from 2026-07-10 until 2026-11-24, swallowed by the
// catch below), and AWC caps a response at 400 reports, newest first, so even
// an accepted 150-airport request comes back with its oldest hour missing.
// Still one bounded batch per session: 4 requests for 150 airports, 12 at most.
async function warmChaseHist(cands: ChaseCand[]) {
  const ids = cands.slice(0, 150).map((c) => c.a.icao).filter((id) => /^[A-Z0-9]{4}$/.test(id));
  const queue = chunkIds(ids, METAR_CHUNK);
  for (let sent = 0; queue.length && sent < 12; sent++) {
    const chunk = queue.shift()!;
    try {
      const r = await fetch(`/api/metar?ids=${chunk.join(",")}&hours=3`);
      if (!r.ok || r.status === 204) continue; // those airports just start cold
      const arr = await r.json();
      if (!Array.isArray(arr)) continue;
      // a full response means AWC cut the oldest reports: ask again in halves
      if (arr.length >= AWC_ROW_CAP && chunk.length > 1) queue.push(...chunkIds(chunk, Math.ceil(chunk.length / 2)));
      for (const o of arr) {
        const icao = o?.icaoId;
        if (!icao || typeof o.obsTime !== "number") continue;
        const vis = jsonVis(o.visib), ceil = jsonCeil(o.clouds);
        pushHist(icao, { t: o.obsTime * 1000, vis, ceil, sub: subCat1(vis, ceil) });
      }
    } catch { /* trend features just start cold */ }
  }
}

// ---- tab-open alerts: notify + chime when an airport ENTERS stratum 1 ----
// Baseline is the first refresh after open/base/filter changes (no blast on
// open); only fresh-ob transitions alert. Static site: works while the tab
// lives, no server push — that's a documented limitation, not a bug.
let chaseFogPrev: Set<string> | null = null;
let chaseCandIds = new Set<string>(); // the candidates on the board as last drawn
let chimeCtx: AudioContext | null = null;

// A settings change moves airports in and out of the candidate list. The
// baseline must not then read "entered fog" for a field that was already in
// fog when it joined (no false pings when a filter moves). Until 2026-11-25
// this was done by throwing the whole baseline away, which also threw away
// what the pilot was waiting for: toggle a filter during an outage and the
// field that went into fog during it never alerted on recovery, because the
// first healthy read only set a new baseline. So the baseline is kept, and
// only the newcomers that are already below CAT I in the observations on hand
// are added to it.
function chaseRebaseline() {
  if (!chaseFogPrev) return; // no baseline yet: the first healthy read sets it, as before
  for (const c of chaseCandidates()) {
    if (!chaseCandIds.has(c.a.icao) && chaseLive?.get(c.a.icao)?.sub) chaseFogPrev.add(c.a.icao);
  }
}

type Chime = readonly (readonly [freq: number, at: number, dur: number])[];
const CHIME_FOG: Chime = [[880, 0, 0.14], [659, 0.16, 0.22]];
// the feed going down must not sound like fog arriving: three low, even notes
const CHIME_FEED_DOWN: Chime = [[330, 0, 0.18], [330, 0.26, 0.18], [330, 0.52, 0.18]];
const CHIME_FEED_BACK: Chime = [[523, 0, 0.12]];

function playChime(notes: Chime = CHIME_FOG) {
  if (!chimeCtx) return;
  const t0 = chimeCtx.currentTime;
  for (const [freq, at, dur] of notes) {
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

// A notification that cannot be shown must not take anything else down with
// it. Chrome for Android has no Notification constructor ("Illegal
// constructor. Use ServiceWorkerRegistration.showNotification() instead"), and
// when it threw here it threw out of refreshChaseLive before the next read was
// scheduled: the board stopped polling for good under a banner that said
// "Retrying every 60 seconds". The chime is separate, so it still sounds.
function chaseNotify(title: string, options: NotificationOptions, onclick?: () => void): boolean {
  try {
    const n = new Notification(title, options);
    if (onclick) n.onclick = onclick;
    return true;
  } catch {
    return false;
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
    chaseNotify(`${c.a.icao} below CAT I`, {
      body: `${ob?.vis != null ? `${ob.vis < 1 ? +ob.vis.toFixed(2) : Math.round(ob.vis)} SM` : ""}${ob?.rvr ? ` · R${ob.rvr} FT` : ""}${ob?.wx ? ` · ${ob.wx}` : ""} — ${eteStr(c.ete)} from ${chasePrefs.base}`,
      tag: `fa-chase-${c.a.icao}`,
    }, () => { window.focus(); openAirport(c.a.icao); });
    playChime();
  }
}

// With alerts armed the pilot is listening, not watching, and a dead feed is
// silent: no fog alert can fire and silence reads as "no fog". So the outage
// itself alerts, once (feedDownAlertDue: only after 6 minutes of failed reads),
// and once more when the feed is back. Never per tick.
function chaseFeedNotify(kind: "down" | "back") {
  if (!chasePrefs.alerts || !("Notification" in window) || Notification.permission !== "granted") return;
  chaseNotify(kind === "down" ? "Fog Atlas: live feed down" : "Fog Atlas: live feed is back", {
    body: kind === "down"
      ? `Fog alerts are not being checked. Not current since ${clockText(chaseFeed.failedSince ?? Date.now())}.`
      : "Fog alerts are being checked again.",
    tag: `fa-chase-feed-${kind}`,
  });
  playChime(kind === "down" ? CHIME_FEED_DOWN : CHIME_FEED_BACK);
}

function chaseFeedAlertCheck(now: number) {
  if (!feedDownAlertDue(chaseFeed, now, chasePrefs.alerts)) return;
  chaseFeed = { ...chaseFeed, downAlerted: true };
  chaseFeedNotify("down");
}

function maybeWarmChase() {
  if (chaseWarmed || !chasePrefs.base || !chaseData) return;
  chaseWarmed = true; // one bounded batch per session, never re-polled
  warmChaseHist(chaseCandidates()).then(renderChaseBoard);
}

// One read, then its own next one: 180 s after a healthy read, 60 s after a
// failed one (the banner quotes that number). Until 2026-11-24 this assigned
// whatever came back with no status check, and its catch was empty: a 502 or
// an error page became an empty board reading "nothing below CAT I within
// range right now", and a dropped connection froze the board as it stood.
async function refreshChaseLive() {
  const seq = ++chaseReadSeq;
  clearTimeout(chaseTimer);
  const started = Date.now();
  let healthy = false;
  try {
    // the airport list failed to load when the board opened: try it again
    // with every read, and redraw the settings (their counts) once it is in
    if (!chaseData && await loadChaseData() && seq === chaseReadSeq && chaseOpen) renderChase();
    const read = await readLiveFeed();
    if (seq !== chaseReadSeq || !chaseOpen) return; // the board closed, or a newer read took over
    healthy = read.ok;
    const now = Date.now();
    const step = feedTrackRead(chaseFeed, read, now);
    chaseFeed = step.track;
    if (step.adopt) chaseLive = step.adopt;
    // Each part below stands alone: an exception in the alerts must not stop
    // the board being painted, and neither may stop the next read (finally).
    try {
      if (step.runAlerts) {
        updateChaseHist();
        maybeWarmChase();
        if (chasePrefs.base) {
          chaseAlertCheck(chaseCandidates().filter((c) => {
            const ob = chaseLive!.get(c.a.icao);
            return ob?.sub && !obStale(ob);
          }));
        }
        if (step.recovered) chaseFeedNotify("back");
      }
      // a failed read runs NO fog-alert check and leaves chaseFogPrev alone: the
      // old code rebaselined on the empty read, so recovery fired an alert for
      // every airport that had been in fog all along
      chaseFeedAlertCheck(now);
    } catch (e) {
      console.error("chase alerts failed", e);
    }
    try {
      renderChaseBoard();
    } catch (e) {
      console.error("chase board failed to draw", e);
    }
  } finally {
    // The next read is scheduled here, whatever happened above. It used to be
    // the last statement of the function, so one throw anywhere before it (a
    // Notification constructor on Chrome for Android) ended the polling for
    // good: 0 requests after the feed came back, the banner still reading
    // "Retrying every 60 seconds". setInterval, which this replaced, survived
    // a throw; this must too.
    // A failed try counts from its START: a server that accepts the request
    // and never answers costs the 45 s timeout, and tries were 105 s apart
    // under a banner that says 60.
    // (a board with no airport list is on the retry cadence too: its banner
    // says the list is asked for again every 60 seconds)
    if (seq === chaseReadSeq && chaseOpen) {
      chaseTimer = setTimeout(refreshChaseLive,
        healthy && chaseData ? FEED_POLL_MS : Math.max(5_000, FEED_RETRY_MS - (Date.now() - started)));
    }
  }
}

// Every 30 s: ages and the banner advance where they stand (no re-render, so
// an opened QUIET list stays open and nothing moves under the pilot's thumb).
// The board is rebuilt only when its grouping actually changed, e.g. a report
// just passed 75 minutes or the reports on hand stopped being current.
function tickChase() {
  if (!chaseOpen) return;
  const now = Date.now();
  const view = feedView(chaseFeed, now);
  // the last read was healthy but the page noticed late (timers throttled, the
  // device slept): read again now instead of waiting out the timer
  if (view.phase === "down" && !view.retrying && !feedInFlight) refreshChaseLive();
  // Backstop for the retry loop itself: the banner says "Retrying every 60
  // seconds", so if no try has finished for a retry plus a full timeout and
  // none is in flight, the loop is dead (it died once, see refreshChaseLive)
  // and this tick starts it again.
  else if (view.phase === "down" && view.retrying && !feedInFlight
    && now - (view.lastTryAt ?? 0) > FEED_RETRY_MS + FEED_TIMEOUT_MS) refreshChaseLive();
  try { chaseFeedAlertCheck(now); } catch (e) { console.error("chase alerts failed", e); }
  const host = panelContent.querySelector<HTMLElement>("#chase-board");
  if (!host) return;
  if (chaseBoardModel(chaseCandidates(), now).sig !== chaseBoardSig) { renderChaseBoard(); return; }
  host.querySelectorAll<HTMLElement>(".chase-age[data-obs]").forEach((el) => {
    el.textContent = `${obAgeMin(+el.dataset.obs!, now)}m`;
  });
  const line = host.querySelector<HTMLElement>("#chase-feedline");
  if (line) line.textContent = chaseFeedLine(view, now);
  const banner = host.querySelector<HTMLElement>("#chase-feed-banner");
  if (banner && view.phase === "down") banner.innerHTML = chaseBannerInner(view, now);
}
// the moment the pilot looks at the tab again, not up to 30 s later
document.addEventListener("visibilitychange", () => { if (!document.hidden) tickChase(); });

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

const obAgeMin = (obsMs: number, now = Date.now()) => Math.max(0, Math.round((now - obsMs) / 60000));
const obAge = (ob: LiveOb) => obAgeMin(ob.obsMs);
const obStale = (ob: LiveOb) => !ob.obsMs || obAge(ob) > FEED_REPORT_MAX_AGE_MS / 60_000; // 75 min, the feed gate's own number
// a "current report": recent enough to act on AND it says whether the field
// is below CAT I. No row, a report over 75 min old, or one with no visibility
// (and no ceiling under 200 ft) is "unknown", never "quiet".
const obCurrent = (ob: LiveOb | undefined): boolean =>
  !!ob && !obStale(ob) && (ob.vis != null || ob.sub);

function liveLine(ob: LiveOb | undefined): string {
  if (!ob) return `<span class="chase-live dim">no current report</span>`;
  const bits = [
    ob.vis != null ? `${ob.vis < 1 ? +ob.vis.toFixed(2) : Math.round(ob.vis)} SM` : "vis ?",
    ob.rvr ? `R${ob.rvr} FT` : null,
    ob.ceil != null ? `ceil ${ob.ceil} ft` : null,
    ob.wx || null,
    // data-obs lets tickChase advance the age in place between renders
    ob.obsMs ? `<span class="chase-age" data-obs="${ob.obsMs}">${obAge(ob)}m</span>` : "time ?",
  ].filter(Boolean).join(" · ");
  const pill = ob.below
    ? `<span class="pill red">below 300 m</span>`
    : ob.sub
    ? `<span class="pill amber">EFVS window</span>`
    : "";
  return `<span class="chase-live${obStale(ob) ? " stale" : ""}${ob.sub && !obStale(ob) ? "" : " dim"}">${bits}</span> ${obStale(ob) ? `<span class="pill dim">stale</span>` : pill}`;
}

// ---- what the strata show: every candidate lands in exactly one group ----
// Feed healthy:  fog (current report, below CAT I) + quiet (current report,
//   not below) + oldFog (last report below CAT I but over 75 min old) +
//   unknown (no row, an old report, or no visibility). "n of N" is fog of
//   fog + quiet, so the two numbers cannot argue.
// Feed not healthy: nothing on hand is current. fog = below CAT I in the last
//   report on hand, quiet = every other candidate; neither is called "now".
interface ChaseBoard {
  view: FeedView;
  fog: ChaseCand[]; quiet: ChaseCand[]; oldFog: ChaseCand[]; unknown: ChaseCand[];
  sig: string; // changes exactly when the board must be rebuilt rather than patched
}
let chaseBoardSig = "";
let chaseQuietOpen = false;

function chaseBoardModel(cands: ChaseCand[], now: number): ChaseBoard {
  const view = feedView(chaseFeed, now);
  const fog: ChaseCand[] = [], quiet: ChaseCand[] = [], oldFog: ChaseCand[] = [], unknown: ChaseCand[] = [];
  let stale = 0;
  for (const c of cands) {
    const ob = chaseLive?.get(c.a.icao);
    if (ob && obStale(ob)) stale++;
    if (view.phase !== "ok") (ob?.sub ? fog : quiet).push(c);
    else if (obCurrent(ob)) (ob!.sub ? fog : quiet).push(c);
    else (ob?.sub ? oldFog : unknown).push(c);
  }
  if (view.phase !== "ok") {
    fog.sort((x, y) => +obStale(chaseLive!.get(x.a.icao)!) - +obStale(chaseLive!.get(y.a.icao)!) || x.ete - y.ete);
  }
  const ids = (l: ChaseCand[]) => l.map((c) => c.a.icao).join(",");
  const sig = [view.phase, view.phase === "down" ? view.reason : "", chasePrefs.base, chasePrefs.alerts,
    ids(fog), ids(oldFog), ids(unknown), quiet.length, stale].join("|");
  return { view, fog, quiet, oldFog, unknown, sig };
}

const escHtml = (s: string) =>
  s.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[ch]!);

// the line under IN FOG NOW: real values where "obs ≤3 min old feed" used to be a constant
function chaseFeedLine(view: FeedView, now: number): string {
  if (view.phase === "ok") {
    return `newest report in the feed ${ageText(now - view.newestMs)} old · feed checked ${clockText(view.checkedAt)}`;
  }
  return view.phase === "waiting" ? "reading live observations…" : "";
}

function chaseBannerInner(view: Extract<FeedView, { phase: "down" }>, now: number): string {
  const b = feedBanner(chaseFeed, view, now, { alertsArmed: chasePrefs.alerts });
  return `<b style="display:block;color:#ff8d84;letter-spacing:0.06em;margin-bottom:3px">${escHtml(b.title)}</b>` +
    b.lines.map((l) => `<div>${escHtml(l)}</div>`).join("");
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
function chaseApplyMap(cands: ChaseCand[], base: Airport | undefined, inFog: ChaseCand[] = [], dim = false) {
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
  // Not current: the glow is last-known, so it is dimmed the way the NOW
  // layer's is (nowGlowDim). It used to stay at full brightness through an
  // outage, then vanish at 75 minutes while the panel still listed the fields.
  map.setPaintProperty("chase-live-glow", "circle-opacity", dim ? 0.18 : 0.55);
  map.setPaintProperty("chase-live-core", "circle-opacity", dim ? 0.35 : 1);
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
  clearTimeout(chaseTimer);
  clearInterval(chaseTickTimer);
  chaseTimer = chaseTickTimer = undefined;
  chaseReadSeq++; // a read still in flight must not land on a closed board
  chaseFogPrev = null;
  chaseCandIds = new Set();
  // nothing polls while the board is closed, so nothing on hand may be shown
  // as current when it reopens: start again from "reading live observations"
  chaseLive = null;
  chaseFeed = feedTrackInit();
  chaseBoardSig = "";
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
  const mine = ++panelToolSeq;
  history.replaceState(null, "", "#chase");
  closeDeployMap();
  setSelected(null);
  await loadChaseData(); // a failure is shown on the board and retried with every read
  if (nowcastModel === undefined) {
    nowcastModel = null;
    try { nowcastModel = await (await fetch("/data/model_y2.json")).json(); } catch {}
  }
  if (mine !== panelToolSeq) return; // DEPLOY was opened while this was loading: it owns the panel
  chaseOpen = true;
  renderChase();
  panel.hidden = false;
  document.body.classList.add("panel-open");
  if (!chaseTickTimer) {
    refreshChaseLive(); // schedules its own next read
    chaseTickTimer = setInterval(tickChase, CHASE_TICK_MS);
  }
}

// The panel is drawn in two parts. renderChase() draws the settings and runs
// only when the pilot changes one (or opens the board). The strata live in
// #chase-board and are redrawn by renderChaseBoard() on every data refresh,
// so a refresh never touches a half-typed base, an open select or the focus.
function renderChase() {
  if (!chaseOpen) return;
  const base = state.airports.find((x) => x.icao === chasePrefs.base);
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

  // a settings change redraws everything: carry QUIET's open state and the
  // scroll position over (only when the panel already shows this board)
  const prevQuiet = panelContent.querySelector<HTMLDetailsElement>("#chase-board details.chase-quiet");
  if (prevQuiet) chaseQuietOpen = prevQuiet.open;
  const prevScroll = panelContent.querySelector("#chase-board") ? panel.scrollTop : null;

  panelContent.innerHTML = `
    <h2>Fog chase</h2>
    <p class="sub">launch board for EFVS testing — ${nUS} US airports (FAA NASR)${nCA ? ` + ${nCA} Canadian curated` : ""}${chaseData ? "" : " — <b style='color:#ff9a9a'>chase.json failed to load</b>"}</p>

    <div class="chase-setup">
      ${base
        ? `<span class="base-chip" title="${base.name}">BASE ${base.icao}<button id="chase-base-clear" title="change base">×</button></span>${(() => {
            const age = baseAge();
            if (!age) return "";
            return age.stale
              ? `<span class="chase-badges" style="align-self:center;color:#ffb347" title="every ETE and deploy-by time on both boards is computed from this base">⚠ ${age.label} — still current?</span><button class="equip-chip" id="chase-base-ok" title="yes, ${base.icao} is still my base — reset the reminder">✓ still ${base.icao}</button>`
              : `<span class="chase-badges" style="align-self:center" title="every ETE and deploy-by time on both boards is computed from this base">${age.label}</span>`;
          })()}`
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

    <div id="chase-board"></div>
    <p class="note">ETE is great-circle still-air — no winds, no climb/descent. Infrastructure: FAA NASR ${chaseData?.meta?.nasr_file?.match(/\d{4}-\d{2}-\d{2}/)?.[0] ?? ""} + CIFP LPV; minima heights are tier proxies, not chart DAs. ${chaseData?.meta?.ca ? `Canadian fields are curated from AIP Canada/CFS research (confidence-tagged) — not authoritative FAA data.` : "US airports only — curated Canadian fields arrive in a later build."}</p>
  `;

  // wiring — settings changes rebaseline the alert set (no false "entered fog"
  // pings when membership shifts because a filter moved)
  const rerender = () => { saveChasePrefs(); chaseRebaseline(); renderChase(); };
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
    chasePrefs.base = null; chasePrefs.baseSetAt = null; rerender();
  });
  panelContent.querySelector("#chase-base-ok")?.addEventListener("click", () => {
    chasePrefs.baseSetAt = Date.now(); saveChasePrefs(); renderChase();
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
        chasePrefs.baseSetAt = Date.now();
        saveChasePrefs();
        chaseRebaseline();
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

  renderChaseBoard();
  if (prevScroll != null) panel.scrollTop = prevScroll;
}

// The strata. Called on every data refresh, and by tickChase when the grouping
// changed. It keeps an opened QUIET list open and the panel's scroll position:
// the old single render replaced the whole panel every 3 minutes, collapsed
// QUIET and pulled the focus back to the base field.
function renderChaseBoard() {
  const host = panelContent.querySelector<HTMLElement>("#chase-board");
  if (!chaseOpen || !host) return;
  const base = state.airports.find((x) => x.icao === chasePrefs.base);
  const cands = chaseCandidates();
  const now = Date.now();
  const m = chaseBoardModel(cands, now);
  const { view } = m;
  const mon = new Date(now).getMonth();
  // keep what the pilot had open: read it off the board that is about to be replaced
  const quietEl = host.querySelector<HTMLDetailsElement>("details.chase-quiet");
  if (quietEl) chaseQuietOpen = quietEl.open;
  const ok = view.phase === "ok", down = view.phase === "down";
  const range = `within ${eteStr(chasePrefs.maxEteH)} at ${chasePrefs.speed} kt (${Math.round(chasePrefs.speed * chasePrefs.maxEteH)} nm still-air)`;

  const rowHtml = (c: ChaseCand, withLive: boolean, extra = "") => `
    <tr data-icao="${c.a.icao}">
      <td class="num" style="white-space:nowrap"><b>${eteStr(c.ete)}</b></td>
      <td class="num">${Math.round(c.nm)}</td>
      <td><b>${c.a.icao}</b> ${c.a.name.length > 20 ? c.a.name.slice(0, 19) + "…" : c.a.name}<br>
        <span class="chase-badges">${chaseRowBadges(c.end)}</span>${withLive ? `<br>${liveLine(chaseLive?.get(c.a.icao))}` : ""}${extra}</td>
      <td class="num" title="climatological hours below CAT I in ${MONTHS[mon]}">${monthClimH(c.a, mon)}h</td>
    </tr>`;
  const boardRows = (list: ChaseCand[], withLive: boolean) => list.map((c) => rowHtml(c, withLive)).join("");
  const boardTable = (rows: string, live: boolean) => `
    <table class="rank-table">
      <thead><tr><th class="num">ETE</th><th class="num">nm</th><th>airport · qualifying runway${live ? " · conditions" : ""}</th><th class="num" title="climatological hours below CAT I in ${MONTHS[mon]}">${MONTHS_S[mon]} ø</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  // the 2-hour nowcast is computed from CURRENT reports only: none while the feed is down
  const soon = !ok ? [] : m.quiet
    .flatMap((c) => {
      const p = nowcastPFor(c.a, chaseLive!.get(c.a.icao)!);
      return p == null ? [] : [{ c, p }];
    })
    .sort((x, y) => y.p - x.p)
    .slice(0, 5);
  const soonRows = soon.map(({ c, p }) => rowHtml(c, true, `
        <span class="pill ${p >= 0.2 ? "amber" : "dim"}" title="logistic nowcast trained 2016–23, validated 2024–25 (Brier −39% vs climatology, AUC 0.95). Experimental — not for operational use.">2 h: ${p < 0.01 ? "<1" : Math.round(p * 100)}%</span>`)).join("");

  const banner = down ? `
    <div id="chase-feed-banner" role="status" style="margin:18px 0 0;padding:10px 12px;border:1px solid #7a352e;border-left:3px solid #ff5a4d;border-radius:6px;background:#401a16;color:#ffd9d4;font-size:11.5px;line-height:1.55">${chaseBannerInner(view, now)}</div>` : "";

  const nCur = m.fog.length + m.quiet.length; // candidates with a current report (feed healthy)
  const kOld = m.oldFog.length, uNo = m.unknown.length;
  let strata: string;
  if (!chaseData) {
    // No airport list, no candidates: every count below would be "0 of 0" and
    // the empty-range sentence, printed under a healthy feed line as if the
    // board had looked. Say what is missing and print no count at all.
    // refreshChaseLive asks for the list again with every read.
    strata = `
    <div id="chase-list-banner" role="status" style="margin:18px 0 0;padding:10px 12px;border:1px solid #7a352e;border-left:3px solid #ff5a4d;border-radius:6px;background:#401a16;color:#ffd9d4;font-size:11.5px;line-height:1.55">
      <b style="display:block;color:#ff8d84;letter-spacing:0.06em;margin-bottom:3px">AIRPORT LIST DID NOT LOAD. THERE IS NO BOARD.</b>
      <div>The list of chase airports did not load, so the board cannot say which fields are in fog.</div>
      <div>Trying again every ${FEED_RETRY_MS / 1000} seconds. Reload the page if this stays.</div>
    </div>`;
  } else if (!base) {
    strata = `<p class="note" style="margin-top:18px">Set a base airport to build the chase board — candidates are ranked by still-air flight time at your cruise speed.</p>`;
  } else if (ok) {
    strata = `
      <div class="stratum-h"><b style="color:#ffb347">IN FOG NOW</b><span class="n">${m.fog.length}</span><span>of ${nCur} candidates with a current report · below CAT I</span></div>
      <div class="chase-badges" id="chase-feedline" style="white-space:normal">${chaseFeedLine(view, now)}</div>
      ${m.fog.length ? boardTable(boardRows(m.fog, true), true)
        : `<p class="note">${nCur
          ? `none of the ${nCur} candidates with a current report is below CAT I.`
          : cands.length ? `no candidate has a current report, so the board cannot say which fields are in fog.`
          : `no airports pass the filters within range.`}</p>`}
      ${kOld ? `
      <p class="note" style="margin-top:12px"><b style="color:#ffb347">${kOld} more</b> last reported below CAT I, but ${kOld === 1 ? "that report is" : "those reports are"} over 75 min old. Not counted above.</p>
      ${boardTable(boardRows(m.oldFog, true), true)}` : ""}
      <div class="stratum-h"><b style="color:var(--accent)">LIKELY SOON</b><span class="n">${soon.length}</span><span>highest P(below CAT I within 2 h) — model nowcast, top 5</span></div>
      ${soon.length ? boardTable(soonRows, true)
        : `<p class="note">${nowcastModel ? "no scoreable candidates (missing or stale observations)." : "nowcast model unavailable."}</p>`}
      <details class="chase-quiet"${chaseQuietOpen ? " open" : ""}>
        <summary class="stratum-h" style="cursor:pointer"><b>QUIET</b><span class="n">${m.quiet.length}</span><span>current report, not below CAT I${uNo ? ` · <span id="chase-unknown-n" style="color:#ffb347">${uNo} more with no current report</span>` : ""} · ${range}</span></summary>
        ${m.quiet.length ? boardTable(boardRows(m.quiet, true), true)
          : cands.length ? "" : `<p class="note">No airports pass the filters within range — widen the ALS set, lower the runway/go-around bars, or extend max ETE.</p>`}
        ${uNo ? `
        <div class="stratum-h"><b style="color:#ffb347">NO CURRENT REPORT</b><span class="n">${uNo}</span><span>unknown, not quiet · not reporting, report over 75 min old, or no visibility in it</span></div>
        ${boardTable(boardRows(m.unknown, true), true)}` : ""}
      </details>`;
  } else {
    // Feed down, or the first read still in flight. Nothing here may read as
    // current: no "NOW", no all-clear sentence, no zero that looks like one.
    const w = view.phase === "down" ? feedDownWords(view) : { tag: "", when: "" };
    strata = `
      <div class="stratum-h"><b style="color:#ffb347">${down ? "IN FOG, LAST KNOWN" : "IN FOG NOW"}</b><span class="n">${down ? (m.fog.length || "?") : "…"}</span><span>${down ? `not current · ${w.tag}` : ""}</span></div>
      ${down ? "" : `<div class="chase-badges" id="chase-feedline" style="white-space:normal">${chaseFeedLine(view, now)}</div>`}
      ${m.fog.length ? boardTable(boardRows(m.fog, true), true)
        : down ? `<p class="note">Unknown ${w.when}. The board cannot say which fields are in fog.</p>` : ""}
      <div class="stratum-h"><b style="color:var(--accent)">LIKELY SOON</b><span class="n">${down ? "?" : "…"}</span><span>${down ? `not calculated ${w.when}` : "highest P(below CAT I within 2 h) — model nowcast, top 5"}</span></div>
      <details class="chase-quiet"${chaseQuietOpen ? " open" : ""}>
        <summary class="stratum-h" style="cursor:pointer"><b>${down ? "OTHER CANDIDATES" : "CANDIDATES"}</b><span class="n">${m.quiet.length}</span><span>${down ? `conditions unknown ${w.when}` : "conditions not read yet"} · ${range}</span></summary>
        ${m.quiet.length ? boardTable(boardRows(m.quiet, down), down)
          : cands.length ? "" : `<p class="note">No airports pass the filters within range — widen the ALS set, lower the runway/go-around bars, or extend max ETE.</p>`}
      </details>`;
  }

  // keep where the pilot was looking (#panel is the scroller)
  const scroll = panel.scrollTop;
  host.innerHTML = banner + strata;
  panel.scrollTop = scroll;
  chaseBoardSig = m.sig;

  host.querySelectorAll("tr[data-icao]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const a = state.airports.find((x) => x.icao === (tr as HTMLElement).dataset.icao)!;
      map.flyTo({ center: [a.lon, a.lat], zoom: 7, duration: 1600 });
      openAirport(a.icao);
    }));

  // The map shows what the panel lists. Healthy: fields in fog on a current
  // report (stale fog reports stay off the glow). Not current: every
  // last-known fog field the panel lists, dimmed.
  chaseCandIds = new Set(cands.map((c) => c.a.icao));
  chaseApplyMap(cands, base, ok ? m.fog : down ? m.fog : [], !ok);
}

$("#chase-btn").addEventListener("click", openChase);

// ---------- DEPLOY: where to base for the next two weeks ----------
// The /api/deploy payload (forecast/build_deploy.py -> KV `deploy`). Two shapes
// reach this code and both must render. Until 2026-11 there was no `pk` and no
// `meta.daycal`, and p on a "cal" day was the hourly curve's any-hour product,
// which ran about 1.7x high and still set the GO word. That shape stays in KV
// until the first bake after a deploy, so every field added since is optional.
// One group of published chances and what happened on those days. `dates` is
// the number of forecast dates behind the airport-days; `ok` (areas only) is
// the build's own verdict on the group: within 10 points, or null when it has
// under 50 airport-days.
interface DeployBand { n: number; shown: number | null; obs: number | null; dates?: number; ok?: boolean | null; sample?: string }
// A group of fields with its own record of the day map (forecast/fit_daycal.py).
// ok false: the pooled map misses there by more than 10 points, so the day is
// not shown as calibrated for these fields. Until 2026-11-25 the panel printed
// the pooled record ("58% of the time") beside Central Valley fields whose own
// record was 35%.
interface DeployArea {
  name: string; ok: boolean | null; high: boolean | null; years?: string; icaos?: string[];
  ge50?: DeployBand | null; b25_50?: DeployBand | null; ge75?: DeployBand | null;
}
interface DeployDayReceipt {
  cover_end_utc?: number | null; // day 2 only: p and pk use the hours up to this UTC hour
  cycle_hour?: number; checked_from?: number | null; sample?: string;
  n?: number; skill_pct?: number | null;
  ge50?: DeployBand | null; b25_50?: DeployBand | null; ge75?: DeployBand | null;
  areas?: DeployArea[];
}
interface DeployAirport {
  eh: number[]; tiers: string[];
  p?: (number | null)[];  // chance of at least one below-CAT-I hour that day
  win?: number[]; ws?: (number | null)[];
  pk?: (number | null)[]; // peak hourly P(below CAT I), integer percent, "cal" days only
}
interface DeployData {
  meta?: {
    generated?: string; cycle?: string; dayscale_skill?: number | null;
    daycal?: {
      fit?: string; test?: string;
      applied?: Record<string, boolean>;        // "1" / "2": the day map was applied to that day
      // why a day has no mapped chance: "hour" (built from a cycle hour the
      // map is not checked on), "held", "season", "hours"
      why?: Record<string, string | null>;
      cycle_hour?: number;
      days?: Record<string, DeployDayReceipt>;  // the check behind the map each day used
    };
  };
  airports: Record<string, DeployAirport>;
}
let deployData: DeployData | null | undefined;
let deployOpen = false;
let deployWindow: 1 | 7 | 14 = 14;

// expected chaseable hours over the window, discounted by reachability:
// a field only credits the fraction of its fog window that remains after
// the still-air transit (launch-at-onset model; +30 min margin)
function usableFrac(win: number | undefined, eteH: number): number {
  if (win == null || win <= 0) return 1; // no window info -> no discount
  return Math.min(Math.max((win - eteH - 0.5) / win, 0), 1);
}
function winEH(icao: string, eteH = 0): number {
  const d = deployData?.airports?.[icao];
  // one entry without its hours (never seen from build_deploy.py, but one was
  // enough to keep the whole panel from opening) counts as no fog there
  if (!d || !Array.isArray(d.eh)) return 0;
  let s = 0;
  for (let i = 0; i < Math.min(deployWindow, d.eh.length); i++)
    s += (d.eh[i] ?? 0) * usableFrac(d.win?.[i], eteH);
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
    // one map-level handler, orange-first with a generous hit pad: the user
    // is aiming at bases — a near-miss must select the base, never fall
    // through to a blue target dot (or the atlas layers) and leave the
    // planner. Blue targets need a deliberate, precise click.
    map.on("click", (ev) => {
      if (!deployOpen || !map.getLayer("deploy-base")) return;
      const hit = (layers: string[], p: number) => map.queryRenderedFeatures(
        [[ev.point.x - p, ev.point.y - p], [ev.point.x + p, ev.point.y + p]] as any,
        { layers });
      const bases = hit(["deploy-base"], 16);
      if (bases.length) {
        const nearest = bases.map((f: any) => {
          const [lon, lat] = f.geometry.coordinates;
          const pt = map.project([lon, lat]);
          return { icao: f.properties.icao, d: Math.hypot(pt.x - ev.point.x, pt.y - ev.point.y) };
        }).sort((a: any, b: any) => a.d - b.d)[0];
        const b = state.airports.find((x) => x.icao === nearest.icao);
        if (b) {
          renderDeploy(b);
          map.flyTo({ center: [b.lon, b.lat], zoom: 5.2, duration: 1400 });
        }
        return;
      }
      const t = hit(["deploy-eh"], 5);
      if (t.length) openAirport(t[0].properties.icao);
    });
    for (const lyr of ["deploy-base", "deploy-eh"]) {
      map.on("mouseenter", lyr, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", lyr, () => { map.getCanvas().style.cursor = ""; });
    }
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
  const mine = ++panelToolSeq;
  history.replaceState(null, "", "#deploy");
  closeChaseMap();
  setSelected(null);
  // the planner talks about the NEXT two weeks — scrub the underlying
  // climatology field/dots to the current month, not the January default
  setMonths([new Date().getMonth()]);
  await loadChaseData(); // a failure is said on the panel and tried again on the next open
  if (!deployData) {
    // Only a real payload counts. /api/deploy answers 503 with a JSON body
    // ({"error":"no deploy data yet"}) when KV is empty; that parsed, was kept
    // for the session, and the panel printed "No expected fog within range of
    // any base" under "data —" as if it had looked. A failure is not kept:
    // the next open asks again.
    deployData = null;
    try {
      const r = await fetch("/api/deploy");
      const j = r.ok ? await r.json() : null;
      if (j?.airports && typeof j.airports === "object" && Object.keys(j.airports).length) deployData = j;
    } catch { /* stays null: "deploy data unavailable" */ }
  }
  if (mine !== panelToolSeq) return; // CHASE was opened while this was loading: it owns the panel
  deployOpen = true;
  renderDeploy();
  panel.hidden = false;
  document.body.classList.add("panel-open");
}

// ---- what a day's number rests on ----
// Every day-level number on this panel (the verdict, the day cells, "fog likely
// <day>") is the payload's p[d]: the chance of at least one hour below CAT I
// that day at one field. What stands behind p differs by tier and by payload
// age. Until 2026-11 the panel printed them all as one bare "% likely
// chaseable": the tier reached the screen only as "(cal)" in a hover title, and
// a 93% that verified near 40% set the GO word with nothing on screen to say so.
//   cal    "cal" tier and meta.daycal.applied for that day: the hourly curve's
//          any-hour product pushed through the hindcast-fitted day map
//   area   the same number at a field whose own area missed the map's check
//          at this level (Central Valley fields shown at 50% or more fogged
//          35% of the time): a forecast, but not a calibrated one there
//   bound  "cal" tier without that: the bare any-hour product. It treats
//          consecutive fog hours as independent draws, so it is a ceiling on
//          the chance, not the chance. Old payloads carry it on days 1-2.
//   fit    days 3-8, the fitted day-scale model
//   nbe, cpc, climo   climatology times a rule-of-thumb factor, or climatology
//          alone: no forecast model stands behind the number
//   other  a tier name this build of the page does not know
type DeployBasis = "cal" | "area" | "bound" | "fit" | "nbe" | "cpc" | "climo" | "other";

// with a field and its chance, a "cal" day is also held against that field's
// own area record (daycalCheck)
function deployBasis(tier: string | undefined, d: number, icao?: string, p?: number | null): DeployBasis {
  if (tier === "cal") {
    if (!deployData?.meta?.daycal?.applied?.[String(d + 1)]) return "bound";
    return icao != null && p != null && !daycalCheck(d, icao, p).calibrated ? "area" : "cal";
  }
  return tier === "fit" || tier === "nbe" || tier === "cpc" || tier === "climo" ? tier : "other";
}
// only these two were scored against outcomes they were not fitted on
const deployChecked = (b: DeployBasis | null) => b === "cal" || b === "fit";

// one wording per basis, used by the verdict, the cells, the legend and the
// "?" tooltip, so the same day is never described two ways
function basisWords(b: DeployBasis): string {
  switch (b) {
    case "cal": return "calibrated forecast";
    case "area": return "forecast not calibrated for that field's area";
    case "bound": return deployData?.meta?.daycal
      ? "upper bound (this build did not correct it)"
      : "upper bound from an older build (not calibrated)";
    case "fit": return "fitted day-scale model";
    case "nbe": return "climatology × NBM-extended ingredients (not fitted)";
    case "cpc": return "climatology × CPC moisture outlook (no forecast)";
    case "climo": return "climatology only (no forecast)";
    default: return "source not known to this page";
  }
}

// The percent printed for a chance. Plain rounding prints "25%" for 0.248
// beside the word SCRAP and "50%" for 0.497 beside MARGINAL, and the day map
// has a flat stretch at 0.24-0.27 where that happens most days (2026-11-24:
// KRDD 0.250 MARGINAL, KGPI 0.248 SCRAP). A chance under a bar is never
// printed as the bar.
function deployPct(p: number): number {
  const r = Math.round(100 * p);
  return p < 0.25 ? Math.min(r, 24) : p < 0.5 ? Math.min(r, 49) : r;
}

const signedPct = (x: number) => `${x > 0 ? "+" : ""}${x.toFixed(1)}%`;

// The best in-reach field of one day. The verdict, the day cells and "fog
// likely <day>" all read this one function, so they cannot print different
// numbers for the same day.
interface DeployPick {
  p: number; icao: string;
  basis: DeployBasis | null;  // null: no in-reach field carries a chance for this day
  pk: number | null;          // peak hourly %, "cal" days of new payloads only
  eh: number | null;
  noP: number;                // in-reach fields whose chance is missing from the payload
  // the highest peak hour among those, for a day the build published no chance for
  hourly: { icao: string; pk: number; eh: number | null } | null;
}
function deployPick(c: DeployBase, d: number): DeployPick {
  const out: DeployPick = { p: 0, icao: "", basis: null, pk: null, eh: null, noP: 0, hourly: null };
  if (!deployData) return out;
  for (const x of c.contrib) {
    const da = deployData.airports[x.a.icao];
    if (!da) continue;
    // reachability: the field's fog window must outlast transit + 45 min
    const win = da.win?.[d];
    if (win != null && win > 0 && win <= x.nm / chasePrefs.speed + 0.75) continue;
    const p = da.p?.[d];
    const pk = da.pk?.[d] ?? null;
    if (p == null) {
      // a "cal" day can carry no chance (an issuance without the 26-48 h
      // bulletin, or a held day map). Missing is not zero: counting it as 0%
      // would print SCRAP from a number that was never made.
      out.noP++;
      if (pk != null && (!out.hourly || pk > out.hourly.pk)) out.hourly = { icao: x.a.icao, pk, eh: da.eh?.[d] ?? null };
      continue;
    }
    if (!out.basis || p > out.p) {
      out.p = p; out.icao = x.a.icao; out.basis = deployBasis(da.tiers?.[d], d, x.a.icao, p);
      out.pk = pk; out.eh = da.eh?.[d] ?? null;
    }
  }
  return out;
}

const deployPicks = (c: DeployBase) => Array.from({ length: deployWindow }, (_, d) => deployPick(c, d));

// first day in the window with a REACHABLE field at 50% or more —
// the operator's "when could we actually go" number
function firstLikely(picks: DeployPick[]): { label: string; p: number; icao: string; d: number; basis: DeployBasis } | null {
  for (let d = 0; d < picks.length; d++) {
    const k = picks[d];
    if (k.basis && k.p >= 0.5) {
      const label = d === 0 ? "tomorrow"
        : new Date(Date.now() + (d + 1) * 864e5).toLocaleDateString("en-US", { weekday: "short" });
      return { label, p: k.p, icao: k.icao, d, basis: k.basis };
    }
  }
  return null;
}

// What stands behind a mapped day-level chance p at one field, from the checks
// the payload carries for the map that day used (meta.daycal.days). It prints
// nothing the payload does not hold: a refit changes these numbers without a
// deploy.
//
// The line on screen says how far off the number ran, never a second
// percentage of fog. The first version printed "days shown at 50% or more (64%
// on average) had an hour below CAT I 58% of the time (427 airport-days)"
// under "50% chance": four percentages in three lines, the last one reading as
// a second chance for the same day. The counts are in the hover text.
//
// A field in a judged area is held against that area's own record, not the
// pooled one. calibrated false: the area missed at this level (more than 10
// points off, 50 airport-days or more), so the caller prints the word grey.
interface DaycalCheck { calibrated: boolean; text: string; title: string; area: DeployArea | null; gap: number | null }
type UsableBand = DeployBand & { shown: number; obs: number };
const usableBand = (b: DeployBand | null | undefined): b is UsableBand =>
  !!b && b.n >= 50 && typeof b.shown === "number" && typeof b.obs === "number"
  && Number.isFinite(b.shown) && Number.isFinite(b.obs);
const gapPoints = (b: UsableBand) => Math.round(100 * (b.shown - b.obs));
function gapWords(b: UsableBand): string {
  const g = gapPoints(b);
  return Math.abs(g) <= 2 ? "about right" : `about ${Math.abs(g)} points ${g > 0 ? "high" : "low"}`;
}
function daycalCheck(d: number, icao: string, p: number): DaycalCheck {
  const dc = deployData?.meta?.daycal;
  const r = dc?.days?.[String(d + 1)];
  const year = /\b(20\d\d)\b/.exec(dc?.test ?? "")?.[1];
  const checked = year ? `Checked on ${year}` : "Checked on a year it was not tuned on";
  const out: DaycalCheck = { calibrated: true, text: `Calibrated forecast${year ? `, checked on ${year}` : ""}.`, title: "", area: null, gap: null };
  if (!r) return out;
  const pc = (x: number) => Math.round(100 * x);
  const key = p >= 0.75 ? "ge75" : p >= 0.5 ? "ge50" : "b25_50";
  // the group the number falls in; a thin top group falls back to 50% or more
  const pick = (o: DeployDayReceipt | DeployArea): { b: UsableBand; level: string } | null => {
    const b = o[key];
    if (usableBand(b)) return { b, level: key === "ge75" ? "75% or more" : key === "ge50" ? "50% or more" : "25% to 49%" };
    return key === "ge75" && usableBand(o.ge50) ? { b: o.ge50, level: "50% or more" } : null;
  };
  const detail = (who: string, x: { b: UsableBand; level: string }, sample?: string) =>
    `${who} shown at ${x.level} averaged ${pc(x.b.shown)}% and had an hour below CAT I ${pc(x.b.obs)}% of the time ` +
    `(${x.b.n.toLocaleString("en-US")} airport-days${x.b.dates ? ` from ${x.b.dates} forecast dates` : ""}${sample ? `; ${sample}` : ""}).`;
  const about = [dc?.fit ? `The day-level chance was fitted on: ${dc.fit}.` : "",
    r.skill_pct != null && Number.isFinite(r.skill_pct)
      ? `Against climatology on its check: ${signedPct(r.skill_pct)} (Brier skill${r.n ? `, ${r.n.toLocaleString("en-US")} airport-days` : ""}).` : ""]
    .filter(Boolean).join(" ");
  const area = r.areas?.find((a) => Array.isArray(a.icaos) && a.icaos.includes(icao)) ?? null;
  out.area = area;
  const inBand = p >= 0.25;
  const ownLevel = key === "ge75" ? "75% or more" : key === "ge50" ? "50% or more" : "25% to 49%";
  // "days like this" is only said of a number inside the group that is quoted
  const likeWords = (level: string) => (inBand && level === ownLevel ? "on days like this" : `on days shown at ${level}`);
  const ranWords = (b: UsableBand) => (Math.abs(gapPoints(b)) <= 2 ? "was about right" : `ran ${gapWords(b)}`);
  if (area) {
    const x = pick(area);
    const yrs = area.years ? ` (${area.years})` : "";
    if (x) {
      // for the 50%-or-more group the fitter's own verdict on the area stands
      // (it looked at two samples); any other group speaks for itself
      const ok = x.level === "50% or more" ? area.ok !== false : x.b.ok !== false;
      const g = gapPoints(x.b);
      out.gap = g;
      if (ok) {
        if (inBand) out.text = `Calibrated forecast. At ${area.name} fields${yrs} it ${ranWords(x.b)} ${likeWords(x.level)}.`;
      } else {
        out.calibrated = false;
        out.text = `Not calibrated for ${area.name} fields: there it ${ranWords(x.b)} ${likeWords(x.level)}${yrs}. Expect the real chance to be ${g > 0 ? "lower" : "higher"}.`;
      }
      out.title = `${detail(`Days at ${area.name} fields`, x, area.years ? `${area.years}, weekly 12Z forecasts` : "")} ${about}`.trim();
      return out;
    }
    if (area.ok === false && usableBand(area.ge50)) {
      // the area missed where it could be judged, and this number sits where it could not
      const g = gapPoints(area.ge50);
      out.calibrated = false; out.gap = g;
      out.text = `Not calibrated for ${area.name} fields: there, days shown at 50% or more ran ${gapWords(area.ge50)}${yrs}.`;
      out.title = `${detail(`Days at ${area.name} fields`, { b: area.ge50, level: "50% or more" }, area.years ? `${area.years}, weekly 12Z forecasts` : "")} ${about}`.trim();
      return out;
    }
  }
  const x = inBand ? pick(r) : null;
  if (x) {
    out.gap = gapPoints(x.b);
    out.text = `Calibrated forecast. ${checked}: it ${ranWords(x.b)} ${likeWords(x.level)}.`;
    out.title = `${detail("Days", x, x.b.sample ?? r.sample)} ${about}`.trim();
  } else {
    out.title = [r.sample ? `Checked on: ${r.sample}.` : dc?.test ? `Checked on: ${dc.test}.` : "", about].filter(Boolean).join(" ");
  }
  return out;
}

// "days 3–8", "day 2", "days 1–2, 5"
function daySpans(days: number[]): string {
  const runs: string[] = [];
  for (let i = 0; i < days.length; i++) {
    let j = i;
    while (j + 1 < days.length && days[j + 1] === days[j] + 1) j++;
    runs.push(j > i ? `${days[i]}–${days[j]}` : `${days[i]}`);
    i = j;
  }
  return `${days.length > 1 ? "days" : "day"} ${runs.join(", ")}`;
}

// Which basis the payload carries on each day, over every airport in it, as
// runs of days. The "?" tooltip and the note under NEXT 48 H are both written
// from this. They used to be fixed strings, and by 2026-11 one said days 3-8
// were "advisory, unfitted", the note under the cells said "fitted", and a
// third said the fitted tier had not landed, while 836 of 937 airports were on it.
interface DeployTierSpan { from: number; to: number; parts: { words: string; share: number }[] }
function deployTierSpans(): DeployTierSpan[] {
  // an entry that is not an airport record (one null was enough to keep the
  // whole panel from opening) is left out
  const all = Object.entries(deployData?.airports ?? {}).filter(([, a]) => a && Array.isArray(a.tiers));
  const nDays = all.reduce((m, [, a]) => Math.max(m, a.tiers.length), 0);
  const days: { key: string; kinds: string[]; n: Map<string, number>; total: number }[] = [];
  for (let d = 0; d < nDays; d++) {
    const n = new Map<string, number>(); // keyed by the words shown, one wording per basis
    let total = 0;
    for (const [icao, a] of all) {
      if (a.tiers[d] == null) continue;
      const p = a.p?.[d];
      // A day the build published no chance for rests on nothing, whatever its
      // tier is called: a "cal" day without one is not an upper bound and not
      // a calibrated chance, and the Alaska and Hawaii fields on the
      // NBM-extended tier (90 of 100 on 2026-11-24) carry no number at all.
      const w = p == null
        ? (a.tiers[d] === "cal" ? "hourly forecast only (no day-level chance in this build)" : "no day-level chance in this plan")
        : basisWords(deployBasis(a.tiers[d], d, icao, p));
      n.set(w, (n.get(w) ?? 0) + 1); total++;
    }
    // a basis on under half a percent of airports is left out, so one stray
    // airport does not split a run of otherwise identical days
    const kinds = [...n].filter(([, c]) => c / total >= 0.005).sort((x, y) => y[1] - x[1]).map(([w]) => w);
    // a run never crosses from day 2 to day 3: that is the 48 h line the note
    // under NEXT 48 H speaks from
    days.push({ key: `${d < 2}|${kinds.join("|")}`, kinds, n, total });
  }
  const spans: DeployTierSpan[] = [];
  for (let d = 0; d < days.length; d++) {
    let e = d;
    while (e + 1 < days.length && days[e + 1].key === days[d].key) e++;
    const run = days.slice(d, e + 1);
    const total = run.reduce((s, x) => s + x.total, 0);
    const parts = days[d].kinds.map((words) => ({
      words, share: run.reduce((s, x) => s + (x.n.get(words) ?? 0), 0) / total,
    })).sort((x, y) => y.share - x.share);
    if (parts.length) spans.push({ from: d + 1, to: e + 1, parts });
    d = e;
  }
  return spans;
}
function deployTierText(spans: DeployTierSpan[], mainOnly = false): string {
  return spans.map((s) => {
    const days = s.from === s.to ? `Day ${s.from}` : `Days ${s.from}–${s.to}`;
    const parts = (mainOnly ? s.parts.slice(0, 1) : s.parts).map((p, i) =>
      `${p.words} at ${Math.round(100 * p.share)}%${i === 0 ? " of airports" : ""}`);
    return `${days}: ${parts.join(", ")}.`;
  }).join(" ");
}

// One day cell. Days that rest on a checked forecast are solid blue. Every
// other basis gets a dashed outline and no blue, so a run of climatology days
// cannot pass for a run of forecast days on this dark panel without hovering.
function deployCellStyle(basis: DeployBasis | null, p: number): string {
  const a = (lo: number, hi: number) => (lo + (hi - lo) * Math.min(Math.max(p, 0), 1)).toFixed(2);
  if (!basis) return "border:1px dotted #4a5a6a";
  if (deployChecked(basis)) return `background:${p >= 0.5 ? "#7fc0e8" : "#3f76a3"};opacity:${a(0.18, 1)}`;
  // a forecast that is not calibrated for its field's area: the forecast blue,
  // inside the amber dashed outline of a number not to be taken at face value
  if (basis === "area") return `border:1px dashed rgb(255,179,71);background:rgba(63,118,163,${a(0.12, 0.85)})`;
  const rgb = basis === "bound" ? "255,179,71" : "141,160,178";
  return `border:1px dashed rgb(${rgb});background:rgba(${rgb},${a(0.04, 0.6)})`;
}
// "ran about 30 points high at Central Valley fields"
function areaMissWords(icao: string, d: number, p: number): string {
  const c = daycalCheck(d, icao, p);
  if (!c.area) return basisWords("area");
  return `forecast not calibrated for ${c.area.name} fields` +
    (c.gap != null && Math.abs(c.gap) > 2 ? ` (ran about ${Math.abs(c.gap)} points ${c.gap > 0 ? "high" : "low"} there)` : "");
}
function deployCellTitle(k: DeployPick, d: number): string {
  const day = `day +${d + 1}`;
  if (!k.basis) return k.noP
    ? `${day}: no day-level chance in this plan for the fields in reach`
    : `${day}: no field in reach has a fog window long enough to reach`;
  let t = `${day}: ${k.icao} ${k.basis === "bound" ? "up to " : ""}${deployPct(k.p)}% chance of at least one hour below CAT I · ${k.basis === "area" ? areaMissWords(k.icao, d, k.p) : basisWords(k.basis)}`;
  if (k.basis === "cal" || k.basis === "area") {
    // day 2 reaches only as far as the forecast does: say which hours it saw
    const cover = deployData?.meta?.daycal?.days?.[String(d + 1)]?.cover_end_utc;
    if (cover != null) t += `, built from the hours up to ${String(cover).padStart(2, "0")}Z`;
    if (k.pk != null) t += ` · peak hourly chance ${k.pk}%${cover != null ? " in those hours" : ""}`;
  }
  return t;
}

// "deploy by HH:MM": latest home-base departure that still makes the chain
// home -> base (30 min turn) -> field, arriving as the field's first likely
// window opens (15 min buffer). Falls back to plain repositioning time when
// the window start isn't in the forecast range.
function deployBy(home: Airport, c: DeployBase, fl: ReturnType<typeof firstLikely>): string {
  const repoH = distNm(home, c.b) / chasePrefs.speed;
  const fallback = `reposition ${eteStr(repoH)}`;
  if (!fl || fl.d > 1 || !deployData?.meta?.cycle) return fallback;
  const da = deployData.airports[fl.icao];
  const ws = da?.ws?.[fl.d];
  if (ws == null) return fallback;
  const cycleMs = Date.parse(deployData.meta.cycle);
  const x = c.contrib.find((k) => k.a.icao === fl.icao);
  if (!x || Number.isNaN(cycleMs)) return fallback;
  const openMs = cycleMs + ws * 3600e3;
  const byMs = openMs - (x.nm / chasePrefs.speed + 0.25 + repoH + 0.5) * 3600e3;
  if (byMs < Date.now()) return `deploy now (${fl.icao} opens soon)`;
  const t = new Date(byMs).toLocaleString("en-US", { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: home.tz });
  return `deploy by ${t}`;
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
  const first = inWin.indexOf(true);
  // first CONTIGUOUS window only — spanning to the 48h-wide last foggy hour
  // bridges the daytime gap and reads as a 30h fog event
  let last = first;
  while (last + 1 < f.fhrs.length && inWin[last + 1] && f.fhrs[last + 1] - f.fhrs[last] <= 3) last++;
  const durH = Math.max(1, f.fhrs[last] - f.fhrs[first] + (f.fhrs[first] <= 25 ? 1 : 3));
  const more = inWin.slice(last + 1).some(Boolean);
  const p05 = Math.max(...f.p.map((r: number[]) => r[1]));
  const p025 = Math.max(...f.p.map((r: number[]) => r[2]));
  // one severity ladder, each rung labeled by its depth — never two bare "fog %"
  const sev = p025 >= 25
    ? `<span class="pill red" title="chance the fog deepens below ¼ mile at its worst hour">deepens to &lt;¼ mi: ${p025}%</span>`
    : p05 >= 25
    ? `<span class="pill amber" title="chance the fog deepens below ½ mile at its worst hour">deepens to &lt;½ mi: ${p05}%</span>`
    : `<span class="pill dim" title="unlikely to get below ½ mile">stays marginal</span>`;
  const per = persistenceTable?.[a.icao];
  const dur = per ? ` · usually lasts ~${per.medianH}h once in` : "";
  return `<span class="chase-live"><span title="chance of visibility under 1 mile — the headline fog event">fog (&lt;1 mi) ${peak}%</span> · ${String(hrOf(f.fhrs[first])).padStart(2, "0")}–${String(hrOf(f.fhrs[last])).padStart(2, "0")} local (≈${durH}h)` +
    `${last < f.fhrs.length - 1 ? `, clears ~${String(hrOf(f.fhrs[Math.min(last + 1, f.fhrs.length - 1)])).padStart(2, "0")}` : ""}${more ? " · again next night" : ""}${dur}</span> ${sev}`;
}

// tiny inline 48h probability strip for a field — the "peek" that replaces
// accidental navigation to the deep-dive
function miniStrip(a: Airport, fc: any): string {
  const f = fc?.airports?.[a.icao];
  if (!f) return "";
  const cyc = new Date(fc.meta.cycle);
  const p10: number[] = f.p.map((r: number[]) => r[0]);
  const peak = Math.max(...p10, 1);
  const bars = f.fhrs.map((fh: number, i: number) => {
    const t = new Date(cyc.getTime() + fh * 3600e3);
    const lab = t.toLocaleTimeString("en-US", { hour: "numeric", hour12: false, timeZone: a.tz });
    return `<div title="+${fh}h (${lab}:00 local) — ${p10[i]}% fog" style="flex:1;align-self:flex-end;background:#9fd8ff;opacity:${p10[i] > 0 ? 0.25 + 0.75 * p10[i] / peak : 0.12};height:${Math.max(2, Math.round(26 * p10[i] / peak))}px"></div>`;
  }).join("");
  return `<div style="display:flex;gap:1px;height:28px;margin:6px 0 2px">${bars}</div>
    <div style="display:flex;justify-content:space-between;color:var(--ink-dim);font-size:9px"><span>now</span><span>hourly P(vis&lt;1mi), 48 h</span><span>+48h</span></div>`;
}

function renderDeploy(ringBase?: Airport) {
  if (!deployOpen) return;
  const radius = Math.round(chasePrefs.speed * chasePrefs.maxEteH);
  const { targets, top } = deployData ? deployRank() : { targets: [], top: [] };
  // a selected base that a settings change dropped from the list is no longer
  // selected: the verdict fell back to the top base while the plan section
  // stayed keyed to the old one, and stayed empty
  if (ringBase && !top.some((c) => c.b.icao === ringBase!.icao)) ringBase = undefined;
  const gen = deployData?.meta?.generated?.slice(0, 10) ?? "—";
  const home = state.airports.find((x) => x.icao === chasePrefs.base);
  const tierSpans = deployTierSpans();
  const beyond48 = deployTierText(tierSpans.filter((s) => s.from >= 3), true);
  const rows = top.map((c, i) => {
    const picks = deployPicks(c);
    const fl = firstLikely(picks);
    // days the payload carries no chance for: "no strong fog day" must not be
    // said about a day nobody put a number on
    const blank = picks.filter((k) => !k.basis && k.noP).length;
    // the row says "likely" only on a number that was checked; any other basis
    // is named beside it, and the hover text gives the number and what it is
    const flText = fl
      ? `fog likely ${fl.label}${deployChecked(fl.basis) ? "" : fl.basis === "bound" ? " (upper bound)" : fl.basis === "area" ? " (not calibrated there)" : fl.basis === "other" ? " (unknown source)" : " (climatology)"}`
      : blank === picks.length ? "no day-level chance in this plan" : "no strong fog day in window";
    const flTitle = fl
      ? `${fl.icao}: ${fl.basis === "bound" ? "up to " : ""}${deployPct(fl.p)}% chance of at least one hour below CAT I that day. Basis: ${fl.basis === "area" ? areaMissWords(fl.icao, fl.d, fl.p) : basisWords(fl.basis)}. It is the first day in the window when a field in reach of ${c.b.icao} is at 50% or more.`
      : blank === picks.length
      ? `This plan carries no day-level chance for the fields in reach of ${c.b.icao} in this window.`
      : `No field in reach of ${c.b.icao} has a 50% or higher chance of at least one hour below CAT I on any day in the window.${blank ? ` ${blank} of ${picks.length} days have no day-level chance in this plan.` : ""}`;
    const dep = home
      ? (home.icao === c.b.icao
        ? `<span class="chase-badges">you're here</span>`
        : `<span class="chase-badges" title="latest departure from your CHASE base ${home.icao} (still-air, ${chasePrefs.speed} kt) to reach ${c.b.icao}, turn (30 min), and launch to the first likely field before its window opens (15 min buffer). Not factored into the ranking.">${deployBy(home, c, fl)}</span>`)
      : "";
    return `
    <tr data-icao="${c.b.icao}"${ringBase?.icao === c.b.icao ? ' style="background:#16202b"' : ""}>
      <td class="num">${i + 1}</td>
      <td><b>${c.b.icao}</b> ${c.b.name.length > 18 ? c.b.name.slice(0, 17) + "…" : c.b.name}${placeTxt(c.b) ? ` <span class="chase-badges">· ${placeTxt(c.b)}</span>` : ""}<br>
        <span class="chase-badges" title="destination fields with flight time from ${c.b.icao} (still-air at ${chasePrefs.speed} kt)">go to: ${c.contrib.slice(0, 3).map((x) => `${x.a.icao} in ${eteStr(x.nm / chasePrefs.speed)}`).join(" · ")}</span><br>
        <span class="chase-badges" title="${escHtml(flTitle)}">${flText}</span></td>
      <td class="num" title="expected chaseable fog hours at fields within ${radius} nm over the next ${deployWindow} days, counting only hours reachable before the fog lifts"><b>${c.s.toFixed(0)}h</b><br>${dep}</td>
    </tr>`;
  }).join("");

  // GO / MARGINAL / SCRAP verdict for the selected (or top) base: per day, the
  // best in-reach field's chance of at least one hour below CAT I; a day
  // counts when that chance is 50% or more
  const vb = (ringBase && top.find((c) => c.b.icao === ringBase!.icao)) || top[0];
  let verdictHtml = "";
  let launchNoteFor: { icao: string; word: string } | null = null; // one-day verdict only
  const hasP = !!vb && vb.contrib.some((x) => Array.isArray(deployData?.airports?.[x.a.icao]?.p));
  if (vb && deployData && hasP) {
    const picks = deployPicks(vb);
    const sk = deployData.meta?.dayscale_skill;
    const dc = deployData.meta?.daycal;
    const baseTxt = icaoPlace(vb.b.icao);
    // The fitted tier (days 3-8) carries one number: its Brier skill against
    // climatology over the whole test year. That is not the check days 1-2
    // got, and the words say so: its days at 50% or more were never checked
    // as a group in this payload (a review found them verifying at 32-38% on
    // days 6-8). "+2.2%" reads as "2.2 points more likely", so the number
    // stays in the hover text.
    const fitWords = "checked as a whole, not at the 50% level";
    const fitTitle = sk != null
      ? `Over its test year this tier scored ${signedPct(sk)} against climatology (Brier skill). Its days at 50% or more were not checked as a group.`
      : "Its days at 50% or more were not checked as a group.";
    const BOX = "background:#111823;border:1px solid #233240;border-radius:10px;padding:12px 14px;margin:12px 0";
    const WORD = "font-size:1.05rem;letter-spacing:.04em";
    const AMBER = "#ffb347", GREY = "#8da0b2";
    if (deployWindow === 1) {
      // night-before board: one day, one sentence — WHEELS UP below carries the rest.
      // Under the sentence go the two numbers that were each checked against
      // observations on their own (the peak hour and the expected hours), then
      // one line saying what the chance rests on.
      const k = picks[0];
      let [word, color] = k.p >= 0.5 ? ["GO", "#7fd49a"] : k.p >= 0.25 ? ["MARGINAL", "#ffb347"] : ["SCRAP", "#ff5a4d"];
      let head = "", facts = "", factsTitle = "", why = "", whyTitle = "", whyColor = "";
      const hourlyFacts = (icao: string, pk: number | null, eh: number | null) => {
        const bits: string[] = [];
        if (pk != null) bits.push(`peak hourly chance <b>${pk}%</b>`);
        if (eh != null) bits.push(`expected hours <b>${eh.toFixed(1)}</b>`);
        return bits.length ? `Below CAT I at ${icao} tomorrow: ${bits.join(" · ")}` : "";
      };
      const hourlyTitle = (pk: number | null) => `From the calibrated hourly forecast: ${pk != null ? "the highest chance in any single hour of the day, and " : ""}the hourly chances added up over the day.`;
      if (!k.basis && k.noP) {
        // nothing to put on the bars: say so instead of printing SCRAP from a blank
        word = "NO VERDICT"; color = GREY;
        head = `this plan has no day-level chance for tomorrow at the fields in reach of ${baseTxt}`;
        if (k.hourly) {
          facts = hourlyFacts(k.hourly.icao, k.hourly.pk, k.hourly.eh) + ". It has the highest peak hour in reach.";
          factsTitle = hourlyTitle(k.hourly.pk);
        }
        // the build says why (meta.daycal.why). "hour": it ran on a forecast
        // cycle the day-level chance has not passed its check on (it is
        // checked for 09Z to 21Z cycles; a redeploy on a US evening is not one)
        const hh = dc?.cycle_hour;
        why = (dc?.why?.["1"] === "hour" && Number.isInteger(hh)
          ? `This plan was built from the ${String(hh).padStart(2, "0")}Z forecast. The day-level chance is not checked for a plan built at that hour, so there is no GO, MARGINAL or SCRAP until the next build.`
          : "This build did not publish a day-level chance for tomorrow, so there is no GO, MARGINAL or SCRAP.") +
          (k.hourly ? " The numbers above are from the calibrated hourly forecast." : "");
        whyColor = AMBER;
      } else if (!k.basis) {
        head = `no field in reach of ${baseTxt} has a fog window long enough to reach tomorrow`;
      } else {
        const pct = deployPct(k.p);
        // the color belongs to a checked forecast; on any other basis the
        // word stays (same bars) but grey, so green alone never reads as a call
        if (!deployChecked(k.basis)) color = GREY;
        head = `${icaoPlace(k.icao)}: ${k.basis === "bound" ? "up to " : ""}${pct}% chance of at least one hour below CAT I tomorrow. Best field in reach of ${baseTxt}.`;
        if (k.basis === "cal" || k.basis === "area" || k.basis === "bound") {
          facts = hourlyFacts(k.icao, k.pk, k.eh);
          factsTitle = hourlyTitle(k.pk);
        }
        switch (k.basis) {
          case "cal":
          case "area": {
            // the same line for both: what the check behind this number found
            // for this field. "area" is the case where it found a miss.
            const c = daycalCheck(0, k.icao, k.p);
            why = c.text;
            whyTitle = c.title;
            if (k.basis === "area") whyColor = AMBER;
            break;
          }
          case "bound":
            why = (dc ? "Upper bound, not a calibrated chance: this build did not correct tomorrow's number."
              : "Upper bound from an older build of the planner, not a calibrated chance.") + " Expect the real chance to be lower.";
            whyTitle = "This number multiplies the hourly chances as if each hour were independent. Fog hours come in runs, so it overstates the chance of a fog day.";
            whyColor = AMBER;
            break;
          case "fit":
            why = `Fitted day-scale model (${fitWords}).`;
            whyTitle = fitTitle;
            break;
          case "nbe":
            why = `From climatology × NBM-extended ingredients, not fitted. There is no forecast model behind this number for ${k.icao}.`;
            whyColor = AMBER;
            break;
          case "cpc":
            why = `From climatology × the CPC moisture outlook. There is no forecast behind this number for ${k.icao}.`;
            whyColor = AMBER;
            break;
          case "climo":
            why = `From climatology. There is no forecast behind this number for ${k.icao}.`;
            whyColor = AMBER;
            break;
          default:
            why = "This page does not know what this number rests on. Do not plan on it.";
            whyColor = AMBER;
        }
      }
      verdictHtml = `<div id="deploy-verdict" data-word="${word}" data-basis="${k.basis ?? "none"}" style="${BOX}">
        <b style="color:${color};${WORD}">${word}</b>
        <span style="color:var(--ink)" title="GO at a 50% or higher chance, MARGINAL at 25% or higher, SCRAP below that. The chance is for at least one hour below CAT I that day at the best field in reach."> — ${head}</span>
        ${facts ? `<div style="margin-top:6px;font-size:11.5px;color:var(--ink)" title="${factsTitle}">${facts}</div>` : ""}
        ${why ? `<div class="note" style="margin-top:5px${whyColor ? `;color:${whyColor}` : ""}"${whyTitle ? ` title="${escHtml(whyTitle)}"` : ""}>${escHtml(why)}</div>` : ""}
        <div class="note" id="deploy-launch-note" style="margin-top:5px">reading the hourly forecast for a launch time…</div>
      </div>`;
      // filled in below once the hourly forecast is in: this line used to be a
      // fixed "launch plan below", printed over "no launch window" and over
      // an empty section alike
      launchNoteFor = { icao: k.icao || k.hourly?.icao || "", word };
    } else {
      const need = deployWindow >= 14 ? 3 : 2;
      const counted = picks.filter((k) => k.basis && k.p >= 0.5);
      const likely = counted.length;
      const wordOf = (n: number) => (n >= need ? "GO" : n >= 1 ? "MARGINAL" : "SCRAP");
      const word = wordOf(likely);
      // same rule as the one-day word: it keeps its color only when the days
      // that are checked forecasts would earn it on their own
      const unchecked = counted.filter((k) => !deployChecked(k.basis));
      const leans = wordOf(likely - unchecked.length) !== word;
      const color = leans ? GREY : word === "GO" ? "#7fd49a" : word === "MARGINAL" ? "#ffb347" : "#ff5a4d";
      const leansLine = leans
        ? `<div class="note" style="margin-top:5px;color:${AMBER}">${unchecked.length} of the ${likely} ${likely === 1 ? "day" : "days"} counted ${unchecked.length === 1 ? "is" : "are"} not a checked forecast: ${[...new Set(unchecked.map((k) => basisWords(k.basis!)))].join(", ")}.</div>`
        : "";
      const CELL = "flex:1;height:15px;border-radius:3px;box-sizing:border-box";
      const swatch = (b: DeployBasis | null) =>
        `<span style="display:inline-block;width:11px;height:9px;border-radius:2px;box-sizing:border-box;vertical-align:-1px;margin-right:5px;${deployCellStyle(b, 0.75)}"></span>`;
      // one legend line per basis among the cells drawn, with the days it
      // covers, so the note cannot claim a tier this base's cells do not show
      const order: DeployBasis[] = ["cal", "area", "bound", "fit", "nbe", "cpc", "climo", "other"];
      const testYear = /\b(20\d\d)\b/.exec(dc?.test ?? "")?.[1];
      const legend = order.map((b) => {
        const days = picks.flatMap((k, i) => (k.basis === b ? [i + 1] : []));
        if (!days.length) return { first: 99, html: "" };
        // plain words on the line; the skill numbers ("+9.7%" reads as "9.7
        // points more likely", and "holdout" is not a pilot's word) in the hover
        let words = basisWords(b), title = "";
        if (b === "fit") { words += ` (${fitWords})`; title = fitTitle; }
        if (b === "cal") {
          if (testYear) words += ` (checked on ${testYear}, a year it was not tuned on)`;
          title = days.map((d) => {
            const r = dc?.days?.[String(d)];
            return r?.skill_pct != null ? `Day ${d}: ${signedPct(r.skill_pct)} against climatology on its check (Brier skill${r.sample ? `; ${r.sample}` : ""}).` : "";
          }).filter(Boolean).join(" ");
        }
        if (b === "area") {
          // name the areas the cells stand on, with how far off each ran
          words = [...new Set(picks.flatMap((k, i) => (k.basis === "area" ? [areaMissWords(k.icao, i, k.p)] : [])))].join("; ");
        }
        return { first: days[0], html: `<div class="note" style="margin-top:2px${b === "bound" || b === "other" || b === "area" ? `;color:${AMBER}` : ""}"${title ? ` title="${escHtml(title)}"` : ""}>${swatch(b)}${daySpans(days)}: ${escHtml(words)}</div>` };
      });
      for (const missing of [true, false]) {
        const days = picks.flatMap((k, i) => (!k.basis && !!k.noP === missing ? [i + 1] : []));
        if (days.length) legend.push({ first: days[0], html: `<div class="note" style="margin-top:2px">${swatch(null)}${daySpans(days)}: ${missing
          ? "no day-level chance in this plan for the fields in reach" : "no field in reach has a fog window long enough to reach"}</div>` });
      }
      // in day order, the way the cells read left to right
      legend.sort((x, y) => x.first - y.first);
      verdictHtml = `<div id="deploy-verdict" data-word="${word}" style="${BOX}">
        <b style="color:${color};${WORD}">${word}</b>
        <span style="color:var(--ink)"> — ${likely} of ${deployWindow} days ${likely === 1 ? "has" : "have"} a field with a 50% or higher chance of at least one hour below CAT I, in reach of ${baseTxt}</span>
        ${leansLine}
        <div style="display:flex;gap:2px;margin-top:9px">${picks.map((k, i) =>
          `<div data-kind="${k.basis ?? "none"}" title="${escHtml(deployCellTitle(k, i))}" style="${CELL};${deployCellStyle(k.basis, k.p)}"></div>`).join("")}</div>
        <div id="deploy-daynums" style="display:flex;gap:2px;margin-top:2px">${picks.map((_, i) =>
          `<div style="flex:1;text-align:center;font-size:8.5px;line-height:1;color:var(--ink-dim)">${i + 1}</div>`).join("")}</div>
        <div class="note" style="margin-top:6px">Each cell is one day, numbered as in the lines below: the chance of at least one hour below CAT I at the best field in reach. Brighter = higher chance.</div>
        ${legend.map((l) => l.html).join("")}
      </div>`;
    }
  }

  panelContent.innerHTML = `
    <h2>Deploy planner</h2>
    <p class="sub">where to base for the fog — expected chaseable hours, ${deployWindow === 1 ? "tomorrow" : `next ${deployWindow} days`} · data ${gen}${deployData ? "" : " — <b style='color:#ff9a9a'>deploy data unavailable</b>"}</p>
    ${verdictHtml}
    <div class="chase-setup">
      ${([1, 7, 14] as const).map((w) => `<button class="equip-chip${deployWindow === w ? " on" : ""}" data-win="${w}">${w === 1 ? "tomorrow" : `next ${w} days`}</button>`).join("")}
      <label>cruise <input id="deploy-speed" type="number" min="60" max="600" step="10" value="${chasePrefs.speed}"/> kt</label>
      <label>max ETE <select id="deploy-ete">
        ${[1, 1.5, 2, 2.5, 3, 4].map((h) => `<option value="${h}"${h === chasePrefs.maxEteH ? " selected" : ""}>${eteStr(h)}</option>`).join("")}
      </select></label>
      <span style="color:var(--ink-dim)">= ${radius} nm reach · shared with <a href="#chase" id="deploy-tochase">CHASE</a></span>
      <span class="qtip" id="deploy-tiers-tip" title="${escHtml(`Airports must pass your saved CHASE filters. Chaseable = below CAT I.${tierSpans.length ? ` What each day's number rests on in this plan: ${deployTierText(tierSpans)} The CPC outlook covers the US only.` : ""}`)}">?</span>
    </div>
    ${(() => {
      const age = baseAge();
      if (!home) return `<p class="note" style="margin-top:6px;color:#ffb347">no CHASE base set — deploy-by departure times need one. <a href="#chase" id="deploy-basechange">set base</a></p>`;
      return `<p class="note" style="margin-top:6px" title="every deploy-by time in the table is the latest departure from this base">deploy-by times from your base <b>${home.icao}</b> · <span${age?.stale ? ' style="color:#ffb347"' : ""}>${age?.label ?? ""}${age?.stale ? " — still current?" : ""}</span> <a href="#chase" id="deploy-basechange">change</a>${age?.stale ? ` · <a href="#" id="deploy-baseok">✓ still ${home.icao}</a>` : ""}</p>`;
    })()}
    <div class="stratum-h"><b style="color:#e8b96a">BEST BASES</b><span class="n">${top.length}</span><span>ranked by expected chaseable hours within reach</span><span class="qtip" title="Click a base row (or its orange dot on the map) to ring its reach and expand the launch plan; click a blue dot for that airport's deep-dive. Dot size = expected chaseable hours. The daily build logs every day's numbers so each tier can be scored against what happened.">?</span></div>
    ${rows ? `<table class="rank-table"><thead><tr><th>#</th><th>base · top nearby fog</th><th class="num" title="expected chaseable fog hours summed across ALL reachable fields over the window — an inventory of opportunities, not one aircraft's itinerary. Each field counts only the fog-window hours remaining after still-air transit (launch-at-onset + 30 min margin), so a distant field whose fog lifts before arrival counts near zero. Contributor times are ETE from the base.">chaseable hrs</th></tr></thead><tbody>${rows}</tbody></table>`
      // "no expected fog" is only said when the planner looked. Without its
      // data, or without the airport list the bases are ranked from, it did
      // not look, and the same sentence used to be printed for both.
      : !deployData ? `<p class="note" id="deploy-nodata" style="color:#ffb347">The planner's data did not load, so no base can be ranked. Open DEPLOY again to retry, or reload the page.</p>`
      : !chaseData ? `<p class="note" id="deploy-nodata" style="color:#ffb347">The airport list did not load, so no base can be ranked. Open DEPLOY again to retry, or reload the page.</p>`
      : `<p class="note">No expected fog within range of any base under the current filters — widen the CHASE filters or the window.</p>`}
    <div id="deploy-48"></div>`;

  panelContent.querySelectorAll(".equip-chip[data-win]").forEach((b) =>
    b.addEventListener("click", () => {
      deployWindow = +(b as HTMLElement).dataset.win! as 7 | 14;
      renderDeploy();
    }));
  panelContent.querySelector("#deploy-tochase")?.addEventListener("click", (e) => {
    e.preventDefault(); openChase();
  });
  panelContent.querySelector("#deploy-basechange")?.addEventListener("click", (e) => {
    e.preventDefault(); openChase();
  });
  panelContent.querySelector("#deploy-baseok")?.addEventListener("click", (e) => {
    e.preventDefault(); chasePrefs.baseSetAt = Date.now(); saveChasePrefs(); renderDeploy(ringBase);
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
  const detailBase = ringBase ?? (deployWindow === 1 ? top[0]?.b : undefined);
  if (detailBase) {
    const sel = top.find((c) => c.b.icao === detailBase.icao);
    if (sel) {
      (async () => {
        if (persistenceTable === undefined) {
          persistenceTable = null;
          try { persistenceTable = await (await fetch("/data/persistence.json")).json(); } catch {}
        }
        const fc = await getForecast();
        const el = panelContent.querySelector("#deploy-48");
        const noteEl = panelContent.querySelector<HTMLElement>("#deploy-launch-note");
        if (!el) return;
        if (!fc) {
          // said, not skipped: the section used to stay empty under a verdict
          // that pointed at it
          const msg = "Hourly forecast unavailable. No wheels-up time can be computed.";
          if (noteEl) { noteEl.textContent = msg; noteEl.style.color = "#ffb347"; }
          el.innerHTML = `<div class="stratum-h"><b style="color:#9fd8ff">NEXT 48 H FROM ${sel.b.icao}</b><span></span><span>calibrated live forecast at the top fields in reach</span></div>
            <p class="note" style="color:#ffb347">${msg} Open DEPLOY again to retry.</p>`;
          return;
        }
        // the night-before answer: pick tomorrow's best reachable window and
        // back the wheels-up time off its opening minus the flight out
        let launch = "";
        {
          const cyc = new Date(fc.meta.cycle).getTime();
          let best: { p: number; icao: string; nm: number; ete: number; openMs: number; tz: string } | null = null;
          for (const x of sel.contrib.slice(0, 12)) {
            const f = fc.airports?.[x.a.icao];
            if (!f) continue;
            const p10: number[] = f.p.map((r: number[]) => r[0]);
            const peak = Math.max(...p10);
            if (peak < 40) continue;
            const thr = Math.max(15, peak * 0.4);
            const ete = x.nm / chasePrefs.speed;
            // first REACHABLE qualifying window: a window that already opened
            // (or opens before we could be airborne) must not disqualify the
            // field when the curve shows another one tonight/tomorrow —
            // otherwise the verdict says GO while the plan says no-launch
            let openMs = 0;
            for (let i = 0; i < f.fhrs.length; i++) {
              if (f.fhrs[i] > 30 || p10[i] < thr) continue;
              const o = cyc + f.fhrs[i] * 3600e3;
              if (o - Date.now() >= ete * 3600e3) { openMs = o; break; }
            }
            if (!openMs) continue;
            if (!best || peak > best.p) best = { p: peak, icao: x.a.icao, nm: x.nm, ete, openMs, tz: x.a.tz };
          }
          if (best) {
            const wheels = new Date(best.openMs - (best.ete + 0.25) * 3600e3);
            const fmt = (d: Date) => d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: best!.tz });
            launch = `<div style="background:#111823;border:1px solid #7a5a2e;border-radius:10px;padding:12px 14px;margin:10px 0">
              <b style="color:#e8b96a;letter-spacing:.05em">WHEELS UP ~${fmt(wheels)}</b>
              <span style="color:var(--ink)"> → ${icaoPlace(best.icao)} (${eteStr(best.ete)} out) · fog (&lt;1 mi) ${best.p}%</span>
              <div class="note" style="margin-top:4px">night-before plan: launch to arrive as the highest-probability reachable window opens (15 min buffer). Recheck the board before engine start — the forecast refreshes hourly.</div>
            </div>`;
          } else {
            launch = `<p class="note">no launch window in the next ~30 h from ${sel.b.icao} — no reachable field's <b>hourly</b> curve clears 40% at a catchable time (the verdict above is day-scale). Forecast refreshes hourly; recheck before committing.</p>`;
          }
          // The verdict box says which of the two the pilot will find below.
          // A day-level GO at KVIS sat over "no launch window" at every base
          // on 2026-11-24: its best hour for fog under 1 mile was 36%, and
          // the launch plan needs 40%.
          if (noteEl && launchNoteFor) {
            if (best) {
              noteEl.textContent = "launch plan below";
            } else {
              const f = launchNoteFor.icao ? fc.airports?.[launchNoteFor.icao] : null;
              const peak = f ? Math.max(...f.p.map((r: number[]) => r[0])) : null;
              noteEl.textContent = "No launch time yet: " + (peak != null && Number.isFinite(peak) && peak < 40
                ? `the best hour at ${launchNoteFor.icao} is ${peak}% for fog under 1 mile, and the launch plan needs 40%.`
                : "no field in reach clears the launch plan's 40% bar (fog under 1 mile) at an hour that can still be reached.");
              if (launchNoteFor.word === "GO" || launchNoteFor.word === "MARGINAL") noteEl.style.color = "#ffb347";
            }
          }
        }
        const lines = sel.contrib.slice(0, 5).map((x) => {
          const ch = fogCharacter(x.a, fc);
          return ch ? `<tr data-icao="${x.a.icao}" class="fpeek"><td><b>${x.a.icao}</b> <span class="chase-badges">${eteStr(x.nm / chasePrefs.speed)} out</span><br><span class="chase-badges">${x.a.name.length > 16 ? x.a.name.slice(0, 15) + "…" : x.a.name}${placeTxt(x.a) ? ` · ${placeTxt(x.a)}` : ""}</span></td><td>${ch}</td></tr>
            <tr class="fdetail" data-for="${x.a.icao}" hidden><td colspan="2">${miniStrip(x.a, fc)}
              <p class="note" style="margin-top:4px"><a href="/fog/${x.a.icao.toLowerCase()}/" target="_blank" rel="noopener">forecast page ↗</a> · <a href="#" class="fdive" data-icao="${x.a.icao}">full 10-yr analysis</a></p></td></tr>` : "";
        }).filter(Boolean).join("");
        el.innerHTML = `
          <div class="stratum-h"><b style="color:#9fd8ff">NEXT 48 H FROM ${sel.b.icao}</b><span></span><span>calibrated live forecast at the top fields in reach</span></div>
          ${launch}
          ${lines ? `<table class="rank-table"><tbody>${lines}</tbody></table>` : `<p class="note">no live forecast coverage at this base's top fields.</p>`}
          <p class="note">Window = hours with fog probability ≥40% of its peak; severity pills show the strongest threshold with ≥25% chance.${beyond48 ? ` Beyond 48 h in this plan: ${escHtml(beyond48)}` : ""}</p>`;
        // row click = expand the peek in place (no navigation, planner state kept)
        el.querySelectorAll("tr.fpeek").forEach((tr) =>
          tr.addEventListener("click", () => {
            const det = el.querySelector(`tr.fdetail[data-for="${(tr as HTMLElement).dataset.icao}"]`) as HTMLElement | null;
            if (det) det.hidden = !det.hidden;
          }));
        el.querySelectorAll("a.fdive").forEach((a2) =>
          a2.addEventListener("click", (ev) => {
            ev.preventDefault();
            openAirport((a2 as HTMLElement).dataset.icao!);
          }));
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
