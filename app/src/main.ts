import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import * as Plot from "@observablehq/plot";
import "./style.css";
import {
  readFeed, feedThrown, feedTrackInit, feedTrackRead, feedView, feedDownAlertDue, feedBanner, feedDownWords,
  jsonCeil, jsonVis, subCat1, chunkIds, ageText, clockText, feedClockSkew,
  FEED_POLL_MS, FEED_RETRY_MS, FEED_TIMEOUT_MS, FEED_REPORT_MAX_AGE_MS, FEED_REASON_LABEL, AWC_ROW_CAP, METAR_CHUNK,
  type LiveOb, type FeedRead, type FeedTrack, type FeedView,
} from "./feed.ts";
import {
  planDayIso, dayWords, dayPart, dayShort, dayLong, dateSpans, zHour, localIso, localHour,
  planFreshness, planAgeLine, planBanner, ageWords, clockWords, cycleWords, zClock,
  refetchDue, retryDelayMs, planProblem, refreshFailWords,
  chanceStatus, noChanceWords, wordOf, wordChance, departure, zoneClock, chainDeparture, launchChance, launchChanceWords,
  PLAN_REFETCH_MS, PLAN_TICK_MS, PLAN_TIMEOUT_MS, HOURLY_OLD_MS, HOURLY_DEAD_MS,
  type DayPart, type DayWords, type ChanceStatus,
} from "./plan.ts";
import {
  ALS_LABEL, LED_CHOICES, LED_CHOICE_LABEL, LED_CHOICE_TITLE, LED_LIST_FAILED, LED_NO_DATA, LED_TAG_UNK, LED_TAG_WAS,
  ledChoiceOf, ledChoiceIn, endShown, ledTag, endHasLed, readLedBlock, ledEndsAt, ledOtherWords,
  ledImpact, ledImpactWords, ledSetAge, ledTickTitle, ledChipWords, ledTagTitle, ledOtherTitle,
  ledLegend, ledPopupLines, ledMethodWords,
  type ChaseEnd, type LedChoice, type LedBlock, type LedTag,
} from "./led.ts";

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
      if (ledOwnsClick(e.point)) return; // the LED ring's popup answers it; the board stays
      const icao = e.features?.[0]?.properties.icao;
      if (icao) openAirport(icao);
    });
  }

  // the LED tick needs the airport dots above (its rings sit around them); not
  // awaited, so a slow chase.json never holds up the panel the hash asks for
  initLed();

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
        const text = r.ok ? await r.text() : "";
        // a device clock that is off is this device's fault, not the feed's, and is said as such
        return (r.ok ? feedClockSkew(r.headers.get("date"), Date.now()) : null)
          ?? readFeed(r.status, text, chaseData ? Object.keys(chaseData.airports) : null, Date.now());
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

// The moment the pilot looks at the tab again: timers are throttled in a
// background tab and stop while the device sleeps, and the chip then showed a
// count hours old as current until its timer woke.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && nowTimer !== undefined && nowShownAt && Date.now() - nowShownAt > FEED_POLL_MS + CHASE_TICK_MS) refreshNow();
});

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
    const readAt = Date.now();
    for (const [icao, ob] of read.obs) {
      // The CHASE board's own rule: a report over 75 minutes old is not
      // "now". The chip counted every report in the file, so with five
      // stations stuck at 85 minutes it said "24 below CAT I now" beside a
      // board that said 19 and "5 more ... over 75 min old. Not counted above".
      if (!ob.obsMs || readAt - ob.obsMs > FEED_REPORT_MAX_AGE_MS) continue;
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
    nowShownAt = readAt;
    nowGlowDim(false);
    // the count carries the time it was read: after a sleep it stood as "now" until the 3-minute timer fired
    chip.textContent = `${feats.length} below CAT I${reds ? ` · ${reds} below 300 m` : ""} · read ${clockText(readAt)}`;
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
        if (ledOwnsClick(ev.point)) return;
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

// ---------- LED approach lights: a ring on every airport the FAA flags ----------
// The owner flight-tests an infrared EFVS, and LED approach lights may not
// show on it. This is an overlay like NOW (its own source and layers), not a
// hide-filter like ILS and LPV: CHASE and DEPLOY replace those filters while
// they are open, and the rings are wanted while chasing and while planning.
// The words and the runway-end rules are in ./led.ts.
let ledOn = false;       // the tick
let ledFailed = false;   // the tick was asked for and chase.json would not load
let ledSeq = 0;
let ledClicks = 0;       // ring clicks handled (debug API: a tick toggled ten times must still answer once)
let ledSeen: { of: object; block: LedBlock | null } | null = null;
// One popup, moved from ring to ring. closeOnClick is off on purpose: the
// popup's own click listener is added while a click is being handled, and the
// NEXT click then runs it after this code has reopened the popup on another
// ring, closing what was just opened. ledClick closes it instead.
const ledPopup = new maplibregl.Popup({ closeButton: true, closeOnClick: false, offset: 12, className: "tip led-pop", maxWidth: "300px" });

// chase.json's led block: undefined until chase.json is read, null when this
// build has none (a chase.json from before 2026-11-25)
function ledData(): LedBlock | null | undefined {
  if (!chaseData) return undefined;
  if (ledSeen?.of !== chaseData) ledSeen = { of: chaseData, block: readLedBlock(chaseData) };
  return ledSeen.block;
}
// the LED choice in effect on both boards ("show all" when this build has no LED data)
const ledChoice = (): LedChoice => ledChoiceIn(ledData(), chasePrefs.led);

// the page must work with storage blocked: the tick then lasts for the visit
const ledSaved = (): boolean => { try { return localStorage.getItem("fa-led") === "1"; } catch { return false; } };
const ledSave = (on: boolean) => { try { localStorage.setItem("fa-led", on ? "1" : "0"); } catch { /* not saved */ } };

// The ring hugs the airport dot: the dot's own radius (see `base` and
// `radius` in the load handler) plus a gap, so it never covers the dot, the
// NOW core (5.5 px) or the CHASE core, and a glow shows through its middle.
const ledRingRadius = (gap: number): any => ["interpolate", ["linear"], ["zoom"],
  1, 4 + gap - 4.5,
  3, ["+", ["get", "b"], gap],
  6, ["+", ["*", ["get", "b"], 1.55], gap],
  9, 10.5 + gap, 14, 13 + gap];

function ledLayers(block: LedBlock) {
  if (map.getSource("led-src")) return;
  const byIcao = new Map(state.airports.map((a) => [a.icao, a]));
  map.addSource("led-src", {
    type: "geojson",
    data: {
      type: "FeatureCollection",
      features: block.airports.map((l) => {
        // 11 of the 47 are not in the atlas: those sit on the FAA's own
        // reference point. An atlas airport sits on its dot.
        const a = byIcao.get(l.id);
        const annual = a ? a.efvsHoursPerYear + a.belowHoursPerYear : 0;
        return {
          type: "Feature",
          geometry: { type: "Point", coordinates: a ? [a.lon, a.lat] : [l.lon, l.lat] },
          properties: { id: l.id, b: Math.max(Math.min(3 + 0.5 * Math.sqrt(annual), 12), 3.5) },
        };
      }),
    } as any,
  });
  // a dark line under the violet one, so the ring still reads over a white-hot fog dot
  map.addLayer({
    id: "led-ring-case", type: "circle", source: "led-src",
    paint: {
      "circle-radius": ledRingRadius(3.6), "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 2, 3.1, 7, 3.8],
      "circle-stroke-color": "#0a0e13", "circle-stroke-opacity": 0.6,
    },
  });
  map.addLayer({
    id: "led-ring", type: "circle", source: "led-src",
    paint: {
      "circle-radius": ledRingRadius(4.5), "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 2, 1.3, 7, 2],
      "circle-stroke-color": "#c79bff", "circle-stroke-opacity": 0.95,
    },
  });
  // Handlers go on once, here, with the layers: the tick only switches
  // visibility, so toggling it cannot stack a second click handler.
  map.on("click", ledClick);
  map.on("mouseenter", "led-ring", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "led-ring", () => { map.getCanvas().style.cursor = ""; });
  // NOW, CHASE, DEPLOY and the selection marker add their layers on first
  // use, which puts them above the rings; a ring under a glow is washed out
  map.on("styledata", ledRaise);
}

function ledRaise() {
  if (!map.getLayer("led-ring")) return;
  const order = map.getLayersOrder();
  if (order[order.length - 1] === "led-ring" && order[order.length - 2] === "led-ring-case") return;
  map.moveLayer("led-ring-case");
  map.moveLayer("led-ring");
}

// Is an LED ring under this click? With CHASE or DEPLOY open the ring's popup
// is the whole answer to such a click: the dot handlers under it used to open
// the airport page as well, which replaced the board or the planner and lost
// the selected base (the popup carries its own link to the airport page).
function ledRingUnder(point: { x: number; y: number }): boolean {
  if (!ledOn || !ledData() || !map.getLayer("led-ring")) return false;
  const pad = 5;
  return map.queryRenderedFeatures([[point.x - pad, point.y - pad], [point.x + pad, point.y + pad]] as any, { layers: ["led-ring"] }).length > 0;
}
const ledOwnsClick = (point: { x: number; y: number }) => (chaseOpen || deployOpen) && ledRingUnder(point);

function ledClick(ev: maplibregl.MapMouseEvent) {
  const block = ledData();
  if (!ledOn || !block || !map.getLayer("led-ring")) return;
  const pad = 5; // a fingertip is wider than the ring's line
  const hits = map.queryRenderedFeatures(
    [[ev.point.x - pad, ev.point.y - pad], [ev.point.x + pad, ev.point.y + pad]] as any, { layers: ["led-ring"] });
  if (!hits.length) { ledPopup.remove(); return; }
  const near = hits.map((f: any) => {
    const [lon, lat] = f.geometry.coordinates as [number, number];
    const pt = map.project([lon, lat]);
    return { id: String(f.properties.id), lon, lat, d: Math.hypot(pt.x - ev.point.x, pt.y - ev.point.y) };
  }).sort((x, y) => x.d - y.d)[0];
  const a = block.byId.get(near.id);
  if (!a) return;
  ledClicks++;
  const atlas = state.airports.find((x) => x.icao === a.id);
  const name = atlas ? `${atlas.name}${placeTxt(atlas) ? ` · ${placeTxt(atlas)}` : ""}` : `${a.name}${a.st ? `, ${a.st}` : ""}`;
  const L = ledPopupLines(a, block.cycle, Date.now());
  ledPopup.setLngLat([near.lon, near.lat]).setHTML(
    `<div data-led="${escHtml(a.id)}"><b>${escHtml(a.id)}</b> ${escHtml(name)}</div>` +
    `<div class="led-pop-h">LED approach lights (FAA airport remark)</div>` +
    L.ends.map((e) => `<div class="led-pop-end">${escHtml(e)}</div>`).join("") +
    L.notes.map((n) => `<div class="led-pop-note">${escHtml(n)}</div>`).join("") +
    `<div class="led-pop-note">${escHtml(L.foot)}${atlas ? ` <a href="#${escHtml(a.id)}" class="led-open">10-year fog analysis</a>` : ""}</div>`,
  ).addTo(map);
  ledPopup.getElement()?.querySelector("a.led-open")?.addEventListener("click", (e) => {
    e.preventDefault();
    ledPopup.remove();
    openAirport(a.id);
  });
}

// The tick, the chip and the rings follow the data on hand. Called when the
// tick moves and whenever chase.json arrives (loadChaseData).
function ledApply() {
  const box = $<HTMLInputElement>("#led-show"), label = $("#led-toggle"), chip = $("#led-chip");
  if (!box || !label || !chip) return;
  const block = ledData();
  const now = Date.now();
  // an older chase.json has no led block: the tick is off and says why
  box.disabled = block === null;
  label.classList.toggle("off", block === null);
  label.title = ledTickTitle(block, now);
  if (block === null) box.checked = false;
  if (block !== undefined) ledFailed = false;
  // a methodology panel opened before chase.json arrived gets this build's numbers now
  const method = panelContent.querySelector("#led-method");
  if (method) method.textContent = ledMethodWords(block, now);
  const show = ledOn && !!block && !!map.getLayer("glow");
  if (show) ledLayers(block!);
  for (const id of ["led-ring-case", "led-ring"]) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", show ? "visible" : "none");
  }
  if (!show) ledPopup.remove();
  chip.classList.remove("old");
  if (show) {
    const c = ledChipWords(block!, now);
    chip.innerHTML = `<i class="led-dot"></i>${escHtml(c.text)}`;
    chip.classList.toggle("old", c.old);
    chip.hidden = false;
  } else if (ledFailed) {
    chip.textContent = LED_LIST_FAILED;
    chip.classList.add("old");
    chip.hidden = false;
  } else {
    chip.hidden = true;
  }
}

// Asks for chase.json when the tick needs it and it is not on hand. A failure
// unticks the box and says so; the saved choice is left alone, so the next
// visit tries again.
async function ledNeedData() {
  const box = $<HTMLInputElement>("#led-show");
  const mine = ++ledSeq;
  if (ledOn && ledData() === undefined) {
    await loadChaseData();
    if (mine !== ledSeq) return; // the tick moved again while this was loading
    if (ledData() === undefined) { ledOn = false; box.checked = false; ledFailed = true; }
  }
  ledApply();
}

function initLed() {
  const box = $<HTMLInputElement>("#led-show");
  if (ledSaved()) box.checked = true;
  ledOn = box.checked; // also a tick made before the map finished loading
  // chase.json is read at page load even with the tick off (26 KB gzipped
  // beside a 7 MB airports.json): only then can the tick say that an older
  // build has no LED data before anyone clicks it
  loadChaseData().then(() => ledNeedData(), () => {});
}

$<HTMLInputElement>("#led-show").addEventListener("change", (e) => {
  ledOn = (e.target as HTMLInputElement).checked;
  ledSave(ledOn);
  ledFailed = false;
  ledNeedData();
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
    <h3>LED approach lights (LED)</h3>
    <p class="note" id="led-method" style="margin-top:6px">${escHtml(ledMethodWords(ledData(), Date.now()))}</p>
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

// Which airport page is the one on screen. Every answer that comes back for
// an airport (its detail file, its METARs, the forecast) checks that it is
// still for the page that is open. Without it a slow answer for the airport
// opened BEFORE was drawn under the heading of the one opened after: KPDT's
// panel read "0.25 SM, ceiling 100 ft, below CAT I" and 2.5 s later, still
// under KPDT, "10+ SM, no ceiling, normal ops" (KGEG's report). /api/metar
// answers in 0.4 to 4 s, so this needed no fault.
let airportSeq = 0;
// the live line of the open airport page: redrawn every 30 s and read again every 3 minutes
let airportLive: { current: () => boolean; draw: () => void; load: () => void; loadedAt: () => number } | null = null;
const AIRPORT_LIVE_MS = 180_000;
function airportTick() {
  const t = airportLive;
  if (!t) return;
  if (!t.current()) { airportLive = null; return; }
  t.draw();
  if (Date.now() - t.loadedAt() >= AIRPORT_LIVE_MS) t.load();
}
setInterval(airportTick, 30_000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) airportTick(); });

async function openAirport(icao: string) {
  const a = state.airports.find((x) => x.icao === icao);
  if (!a) return;
  const mine = ++airportSeq;
  const tool = ++panelToolSeq; // a CHASE or DEPLOY that is still loading must not open over this
  // still the page the pilot asked for last: no later airport, and no CHASE, DEPLOY, list or close since
  const current = () => mine === airportSeq && tool === panelToolSeq;
  if (persistenceTable === undefined) {
    persistenceTable = null; // only try once
    try { persistenceTable = await (await fetch("/data/persistence.json")).json(); } catch {}
  }
  // The board or planner on screen is closed only once this page can be
  // drawn. It was closed first, with its timers, and when this request then
  // failed or hung nothing replaced it: 40 minutes later the CHASE board still
  // read "IN FOG NOW 1 ... feed checked 03:16 local" with no read since.
  let detail: any;
  try {
    const r = await fetch(`/data/detail/${icao}.json`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    detail = await r.json();
  } catch {
    if (current()) flash(`${icao} could not be opened: its page data did not load. What was on screen is still running.`);
    return;
  }
  if (!current()) return;
  closeChaseMap();
  closeDeployMap();
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
    if (!current()) return; // another page was opened while the model loaded: nothing below is for it
  }
  // What the report server last said for this airport: the reports, or why
  // there are none. Drawn by drawLive() now and again every 30 s, so the age
  // on screen advances and the line turns to NO CURRENT REPORT when the report
  // passes 75 minutes; read again every 3 minutes while the page is open. It
  // was written once: "RIGHT NOW · 8 min old · normal ops" read the same three
  // hours later.
  let liveGot: { arr: any[] | null; why: string } | null = null;
  let liveAt = 0;
  const drawLive = () => {
    const liveEl = current() ? panelContent.querySelector("#live") : null;
    if (!liveEl || !liveGot) return;
    const arr = liveGot.arr;
    if (!arr) {
      // an error status is not a report, and silence is not "normal ops": the line used to stay empty
      liveEl.innerHTML = `<div class="live-line">${escHtml(icao)} · live report unavailable: ${escHtml(liveGot.why)}.</div>`;
      return;
    }
    const ob = arr[0] ?? null;
    if (!ob) {
      liveEl.innerHTML = `<div class="live-line">${escHtml(icao)} · NO CURRENT REPORT · none received in the last 3 hours.</div>`;
      return;
    }
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
    const who = escHtml(icao); // the line names its airport: a time alone could not show whose report it was
    const said = `${vis != null ? `${ob.visib} SM` : "visibility not reported"}${ceil != null ? `, ceiling ${ceil} ft` : vis != null ? ", no ceiling" : ""}`;
    if (ageMs == null || ageMs > FEED_REPORT_MAX_AGE_MS) {
      // over 75 min old: what it said is shown as history, with no verdict
      // and no nowcast — neither is about now
      liveEl.innerHTML = `
      <div class="live-line">${who} · NO CURRENT REPORT · last report ${escHtml(when || "time unknown")}${ageMs != null ? `, ${ageText(ageMs)} old` : ""} — it said ${escHtml(said)}.</div>`;
      return;
    }
    if (vis == null && !sub) {
      // no visibility and no ceiling under 200 ft: the report cannot say
      // whether the field is below CAT I, so nothing here may say it is not
      liveEl.innerHTML = `
      <div class="live-line">${who} · LATEST REPORT · ${escHtml(when)}, ${ageText(ageMs)} old — ${escHtml(said)}. It does not say whether the field is below CAT I.</div>`;
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
      <div class="live-line">${who} · RIGHT NOW · ${escHtml(when)}, ${ageText(ageMs)} old — ${escHtml(said)} · ${verdict}.${lift}${nowcast}</div>`;
  };
  const loadLive = async () => {
    let arr: any[] | null = null, why = "";
    try {
      const r = await fetch(`/api/metar?ids=${icao}&hours=3`);
      // 204: aviationweather.gov has no report for the station in those hours (the proxy's real answer for one that stopped reporting)
      if (r.status === 204) arr = [];
      else if (r.ok) { const j = await r.json(); arr = Array.isArray(j) ? j.filter((o: any) => o?.icaoId === icao) : []; }
      else why = `the report server answered with an error (HTTP ${r.status})`;
    } catch {
      why = "the report server could not be reached";
    }
    if (!current()) return; // an answer for an airport that is no longer the one open
    liveGot = { arr, why };
    liveAt = Date.now();
    drawLive();
  };
  airportLive = { current, draw: drawLive, load: loadLive, loadedAt: () => liveAt || Date.now() };
  loadLive();

  // V3 forecast strip: calibrated percentages ONLY once meta.public is true
  // (the pre-registered bar); during shadow, show raw NBM LIFR-vis guidance,
  // clearly labeled — public NWS data, not our unverified model
  getForecast().then((fc) => {
    const f = fc?.airports?.[icao];
    const el = current() ? panelContent.querySelector("#fcast") : null;
    if (!f || !el) return;
    const pub = Array.isArray(fc.meta?.publicAirports) && fc.meta.publicAirports.includes(icao);
    // an issuance without the raw-guidance column draws no strip (it threw here and took the rest of the page with it)
    if (!pub && !Array.isArray(f.liv)) return;
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
// A runway end (ChaseEnd) and the rule for which end a board shows are in
// ./led.ts, shared with DEPLOY.
interface ChasePrefs {
  base: string | null; baseSetAt: number | null; speed: number; maxEteH: number;
  als: string[]; minLen: number; rvrReq: boolean; maxTier: number;
  alerts: boolean;
  // LED approach lights: show all / avoid LED ends / only LED ends, and when
  // that was chosen. A forgotten "only" is a board of a few airports, so the
  // choice shows its age where it takes effect, like the base.
  led: LedChoice; ledSetAt: number | null;
}

const CHASE_DEFAULTS: ChasePrefs = {
  base: null, baseSetAt: null, speed: 250, maxEteH: 2,
  als: ["ALSF2", "ALSF1", "MALSR"], minLen: 5000, rvrReq: false, maxTier: 250,
  alerts: false,
  led: "all", ledSetAt: null,
};
// the named chips are ALS_NAMED in ./led.ts, in this order (app/test/test_led.mjs holds the two together)
const CHASE_ALS_CHIPS = ["ALSF2", "ALSF1", "MALSR", "SSALR", "MALSF", "OTHER"] as const;
const chasePrefs: ChasePrefs = (() => {
  try {
    const p = { ...CHASE_DEFAULTS, ...JSON.parse(localStorage.getItem("fa-chase") ?? "{}") };
    p.led = ledChoiceOf(p.led); // a value this page does not know hides nothing
    return p;
  } catch { return { ...CHASE_DEFAULTS }; }
})();
// Saving must not throw: with storage blocked (private mode, a full quota) the
// throw came before the redraw, so a changed setting did nothing on screen.
const saveChasePrefs = () => { try { localStorage.setItem("fa-chase", JSON.stringify(chasePrefs)); } catch { /* lasts for the visit */ } };
// Two tabs each held the whole settings object and wrote all of it: a filter
// toggled in a tab opened earlier put its old base back and switched the
// other tab's alerts off, and that tab went on showing the base it no longer
// had saved. Every ETE and departure time hangs off the base. A save in
// another tab is now taken up here, and the open board is redrawn from it.
window.addEventListener("storage", (e) => {
  if (e.key !== "fa-chase" || e.newValue == null) return;
  try {
    const next = { ...CHASE_DEFAULTS, ...JSON.parse(e.newValue) };
    next.led = ledChoiceOf(next.led);
    // alerts are armed per tab (a tap in THIS tab made its sound and its permission): never switched on from another
    next.alerts = chasePrefs.alerts && next.alerts;
    Object.assign(chasePrefs, next);
  } catch { return; }
  if (chaseOpen) { chaseRebaseline(); renderChase(); } else if (deployOpen) renderDeploy(deployRing, true);
});

// One plain line over the map for a few seconds: something the pilot asked
// for did not happen, and why. (There was nowhere to say it: a failed airport
// page was an unhandled rejection and a board left dead on screen.)
let flashTimer: ReturnType<typeof setTimeout> | undefined;
function flash(text: string, ms = 9000) {
  let el = document.querySelector<HTMLElement>("#flash");
  if (!el) {
    el = document.createElement("div");
    el.id = "flash";
    el.setAttribute("role", "status");
    el.style.cssText = "position:fixed;left:50%;bottom:22px;transform:translateX(-50%);max-width:min(92vw,520px);z-index:50;"
      + "background:#33270f;color:#ffe2b0;border:1px solid #7a5a2e;border-radius:8px;padding:9px 13px;font-size:12px;line-height:1.5;box-shadow:0 4px 18px #0008";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { el!.hidden = true; }, ms);
}

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

let chaseData: { meta: any; airports: Record<string, ChaseEnd[]>; led?: unknown } | null | undefined;
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
    // The feed's own 45 s limit. Without one a request that never answered
    // held everything that waits on this list: CHASE never drew its board (its
    // "AIRPORT LIST DID NOT LOAD" state needs this to END), DEPLOY never
    // opened, and NOW, which only wants the list for its coverage gate, never
    // read the feed at all.
    const ctl = new AbortController();
    const kill = setTimeout(() => ctl.abort(), FEED_TIMEOUT_MS);
    try {
      const r = await fetch("/data/chase.json", { signal: ctl.signal });
      const j = r.ok ? await r.json() : null;
      if (j?.airports && typeof j.airports === "object" && Object.keys(j.airports).length) chaseData = j;
    } catch { /* stays unloaded; the next caller tries again */ } finally {
      clearTimeout(kill);
    }
    if (!chaseData) chaseData = null;
    chaseDataLoading = null;
    // The LED tick follows the list: only now can it say whether this build
    // has LED data. In its own try: a throw here would reject for CHASE,
    // DEPLOY and the NOW layer, which all wait on this promise.
    if (chaseData) { try { ledApply(); } catch (e) { console.error("LED tick failed to update", e); } }
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
// The board is not on screen and its feed is still being read, because alerts
// are armed. Until 2026-11-25 every way of leaving the board stopped the
// reads: opening an airport, clicking the fog alert itself (its click opens
// the airport), DEPLOY, the close button. The alerts chip stayed lit, 45
// minutes later two more fields had gone below CAT I, and nothing had read
// the feed since.
let chaseWatch = false;
const chaseActive = () => chaseOpen || chaseWatch;

// What an alert can do in THIS session. A browser lets a page notify only
// with permission and make sound only after a tap in this page load, so
// "alerts: on" in saved settings says nothing about either. After a reload
// the chip was lit, the chime was silent (no AudioContext), and on a browser
// without notifications nothing could fire at all.
const alertsCan = () => ({
  note: "Notification" in window && Notification.permission === "granted",
  sound: !!chimeCtx && chimeCtx.state === "running",
});
const alertsArmed = () => { const c = alertsCan(); return chasePrefs.alerts && (c.note || c.sound); };
// the top-bar button says so while the board is away
function chaseWatchMark() {
  const btn = document.querySelector<HTMLElement>("#chase-btn");
  if (!btn) return;
  btn.classList.toggle("watching", chaseWatch);
  btn.dataset.title ??= btn.title;
  btn.title = chaseWatch
    ? "Fog alerts are on: the live feed is still being read while this tab is open. Open CHASE to see the board or to turn them off."
    : btn.dataset.title;
  if (chaseWatch) btn.setAttribute("aria-label", "chase (fog alerts on)"); else btn.removeAttribute("aria-label");
}

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
  if (!prev || !alertsArmed()) return;
  const can = alertsCan();
  for (const c of freshFog) {
    if (prev.has(c.a.icao)) continue;
    const ob = chaseLive?.get(c.a.icao);
    // the chime does not wait on the notification: a browser without one still gets the sound
    if (can.note) chaseNotify(`${c.a.icao} below CAT I`, {
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
  if (!alertsArmed()) return;
  if (alertsCan().note) chaseNotify(kind === "down" ? "Fog Atlas: live feed down" : "Fog Atlas: live feed is back", {
    body: kind === "down"
      ? `Fog alerts are not being checked. Not current since ${clockText(chaseFeed.failedSince ?? Date.now())}.`
      : "Fog alerts are being checked again.",
    tag: `fa-chase-feed-${kind}`,
  });
  playChime(kind === "down" ? CHIME_FEED_DOWN : CHIME_FEED_BACK);
}

function chaseFeedAlertCheck(now: number) {
  if (!feedDownAlertDue(chaseFeed, now, alertsArmed())) return;
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
    if (seq !== chaseReadSeq || !chaseActive()) return; // the reads were stopped, or a newer read took over
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
    if (seq === chaseReadSeq && chaseActive()) {
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
  if (!chaseActive()) return;
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
  const sig = [view.phase, view.phase === "down" ? view.reason : "", chasePrefs.base, alertsArmed(),
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
  const b = feedBanner(chaseFeed, view, now, { alertsArmed: alertsArmed() });
  return `<b style="display:block;color:#ff8d84;letter-spacing:0.06em;margin-bottom:3px">${escHtml(b.title)}</b>` +
    b.lines.map((l) => `<div>${escHtml(l)}</div>`).join("");
}

// The board under the LED choice in effect. The feed's coverage gates do not
// read this list: they count chase.json's whole airport list (readLiveFeed),
// so no filter here, the LED choice included, can trip or hide a feed outage.
function chaseCandidates(): ChaseCand[] {
  return chaseCandidatesFor(ledChoice());
}

// `choice` is a parameter so the settings can say how many airports the LED
// choice removed: the same board under "show all", beside the one shown.
function chaseCandidatesFor(choice: LedChoice): ChaseCand[] {
  const base = state.airports.find((x) => x.icao === chasePrefs.base);
  if (!chaseData || !base) return [];
  const out: ChaseCand[] = [];
  for (const a of state.airports) {
    if (a.icao === base.icao) continue;
    // the saved filters and the LED choice, on one rule shared with DEPLOY (./led.ts)
    const end = endShown(chaseData.airports[a.icao], chasePrefs, choice);
    if (!end) continue;
    const nm = distNm(base, a);
    const ete = nm / chasePrefs.speed;
    if (ete <= chasePrefs.maxEteH) out.push({ a, end, nm, ete });
  }
  return out.sort((x, y) => x.ete - y.ete);
}

// Every chase airport that meets the saved filters under one LED choice, with
// the end shown: no base and no distance. DEPLOY's targets, and the count
// beside the LED choice when no base is set.
function chaseQualifying(choice: LedChoice): { id: string; e: string }[] {
  const out: { id: string; e: string }[] = [];
  for (const [id, ends] of Object.entries(chaseData?.airports ?? {})) {
    const end = endShown(ends, chasePrefs, choice);
    if (end) out.push({ id, e: end.e });
  }
  return out;
}

// ---- LED approach lights on a row ----
const ledPill = (tag: LedTag, icao: string, e: string) => {
  const block = ledData();
  // the date behind the tag: the infrared list's for LED + IR, and for an end
  // that is only on that list (no current remark) the list's own
  const asof = tag === LED_TAG_WAS ? block?.listed.find((x) => x.id === icao && x.e === e)?.asof
    : block?.byId.get(icao)?.ends.find((x) => x.e === e)?.irAsof;
  // amber, not violet, where the tag is not a plain FAA statement about this runway end
  const soft = tag === LED_TAG_WAS || tag === LED_TAG_UNK;
  return `<span class="pill led${soft ? " soft" : ""}" title="${escHtml(ledTagTitle(tag, icao, e, asof ?? null))}">${escHtml(tag)}</span>`;
};

// The tag for the end a row shows, and a note naming any OTHER end at the
// airport with LED lights. Without the note the ring on the map and a row
// with no tag argue: at KLAX the board shows 25L and the LED lights are on 07R.
function ledRowBits(icao: string, end: ChaseEnd | null): { pill: string; other: string } {
  const block = ledData();
  if (!block) return { pill: "", other: "" };
  const tag = end ? ledTag(end) : null;
  const ledEnds = ledEndsAt(block, icao, chaseData?.airports[icao]);
  const other = ledOtherWords(end, ledEnds);
  return {
    pill: tag && end ? ledPill(tag, icao, end.e) : "",
    other: other ? `<span class="led-other" title="${escHtml(ledOtherTitle(icao, !!end && endHasLed(end), ledEnds.filter((x) => x.e !== end?.e)))}">${escHtml(other)}</span>` : "",
  };
}

// The three-way choice, drawn the same in the CHASE settings and in DEPLOY.
// A build with no LED data shows it disabled on "show all" and leaves the
// saved value alone.
function ledSelectHtml(id: string): string {
  const block = ledData();
  const choice = ledChoice();
  const title = block ? LED_CHOICE_TITLE : block === null ? LED_NO_DATA : "The airport list did not load.";
  return `<label title="${escHtml(title)}">LED lights <select id="${id}"${block ? "" : " disabled"}>
        ${LED_CHOICES.map((c) => `<option value="${c}"${c === choice ? " selected" : ""}>${LED_CHOICE_LABEL[c]}</option>`).join("")}
      </select></label>`;
}

// The line next to the choice: what it did to this board and how old the
// saved choice is. A short board is never left unexplained, and a choice made
// weeks ago asks to be confirmed, as the saved base does.
function ledChoiceNote(id: string, okId: string, scope: string, all: { id: string; e: string }[], cur: { id: string; e: string }[]): string {
  const block = ledData();
  const saved = ledChoiceOf(chasePrefs.led);
  if (!block) {
    return block === null && saved !== "all"
      ? `<p class="note" id="${id}" style="margin-top:6px;color:#ffb347">Your saved LED choice (${LED_CHOICE_LABEL[saved]}) is not applied: this build has no LED data.</p>`
      : "";
  }
  if (saved === "all") return "";
  const w = ledImpactWords(saved, ledImpact(all, cur), scope, !!chaseData?.meta?.ca);
  const age = ledSetAge(chasePrefs.ledSetAt, Date.now());
  return `<p class="note" id="${id}" data-choice="${saved}" style="margin-top:6px"><b style="color:#d9bcff">${w.head}</b> · ${age.stale
    ? `<span style="color:#ffb347">⚠ ${age.label} — still wanted?</span> <a href="#" id="${okId}">✓ keep</a>`
    : age.label}. ${w.lines.map(escHtml).join(" ")}</p>`;
}

// What the tag means for an infrared EFVS, and what no tag does not mean.
function ledLegendHtml(id: string): string {
  const block = ledData();
  if (!block) return "";
  const L = ledLegend(block, Date.now());
  return `<p class="note" id="${id}" style="margin-top:8px"><span class="pill led">LED</span> ${escHtml(L.led)}${L.ir
    ? ` <span class="pill led">LED + IR</span> ${escHtml(L.ir)}` : ""}${L.unk
    ? ` <span class="pill led soft">${escHtml(LED_TAG_UNK)}</span> ${escHtml(L.unk)}` : ""}${L.was
    ? ` <span class="pill led soft">${escHtml(LED_TAG_WAS)}</span> ${escHtml(L.was)}` : ""} ${escHtml(L.none)} ${escHtml(L.data)}</p>`;
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
      if (ledOwnsClick(ev.point)) return;
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
  // A board whose reads have stopped must never stay on screen calling old
  // observations current. Whoever closes the board replaces the panel right
  // after; if that never happens (it did not, when the next panel's own
  // request failed), the board is at least gone.
  panelContent.querySelector("#chase-board")?.replaceChildren();
  // With alerts armed the reads, the observations on hand and the alert
  // baseline all stay: only the drawing stops (chaseWatch).
  chaseWatch = alertsArmed() && !!chasePrefs.base;
  chaseWatchMark();
  if (!chaseWatch) {
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
  }
  chaseBoardSig = "";
  if (map.getLayer("glow")) {
    applyApproachFilters();
    map.setPaintProperty("fogheat", "heatmap-opacity", FOGHEAT_OPACITY);
  }
  (map.getSource("chase-src") as any)?.setData({ type: "FeatureCollection", features: [] });
  (map.getSource("chase-live-src") as any)?.setData({ type: "FeatureCollection", features: [] });
}

function chaseRowBadges(end: ChaseEnd, icao: string): string {
  // LED approach lights: the tag sits on the light system of the end shown
  const led = ledRowBits(icao, end);
  const parts = [
    `<b>${end.e}</b>`,
    `${ALS_LABEL[end.als ?? ""] ?? end.als}${led.pill ? ` ${led.pill}` : ""}`,
    end.ils ? `CAT ${end.ils}` : end.lpv ? "LPV" : null,
    `${end.len.toLocaleString()} ft`,
    end.rvr ? (end.rvr === "Y" ? "RVR" : `RVR ${end.rvr}`) : null,
    // a curated end has no LED source: it is unknown, and the row must not read as "no LED"
    end.cur ? `<span title="infrastructure curated from AIP Canada / CFS sources — confidence-tagged, not authoritative FAA data. There is no LED data for Canadian fields: whether these approach lights are LED is unknown.">curated</span>` : null,
    led.other || null,
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
  chaseWatch = false; // the board is back on screen; the reads it kept up are the ones it shows
  chaseWatchMark();
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
      ${(() => {
        const can = alertsCan(), armed = alertsArmed();
        // saved as on, and this page load cannot give one yet: asks for the tap, in amber, instead of looking armed
        const asleep = chasePrefs.alerts && !armed;
        const label = armed ? `♪ alerts on${!can.sound ? " (no sound)" : !can.note ? " (sound only)" : ""}` : asleep ? "♪ alerts: tap to turn on" : "♪ alerts";
        const title = asleep
          ? "Alerts were on when this page was last open. A browser lets a page notify or make sound only after a tap in it: tap to turn them on again."
          : "While this tab is open: a notification and a chime when an airport on the board drops below CAT I. They go on when you open an airport or the planner, and stop when the tab closes or the device sleeps. A web page cannot wake your phone.";
        return `<button class="equip-chip${armed ? " on" : ""}" id="chase-alerts"${asleep ? ' style="border-color:#ffb347;color:#ffb347"' : ""} title="${title}">${label}</button>`;
      })()}
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
      ${ledSelectHtml("chase-led")}
    </div>
    ${(() => {
      // how many airports the LED choice took off THIS board: the board in
      // range when a base is set, every airport that meets the other filters
      // when none is
      const ids = (l: ChaseCand[]) => l.map((c) => ({ id: c.a.icao, e: c.end.e }));
      return base
        ? ledChoiceNote("chase-led-note", "chase-led-ok", "in range", ids(chaseCandidatesFor("all")), ids(chaseCandidates()))
        : ledChoiceNote("chase-led-note", "chase-led-ok", "that meet your other filters", chaseQualifying("all"), chaseQualifying(ledChoice()));
    })()}

    <div id="chase-board"></div>
    <p class="note">ETE is great-circle still-air — no winds, no climb/descent. Infrastructure: FAA NASR ${chaseData?.meta?.nasr_file?.match(/\d{4}-\d{2}-\d{2}/)?.[0] ?? ""} + CIFP LPV; minima heights are tier proxies, not chart DAs. ${chaseData?.meta?.ca ? `Canadian fields are curated from AIP Canada/CFS research (confidence-tagged) — not authoritative FAA data.` : "US airports only — curated Canadian fields arrive in a later build."}</p>
    ${ledLegendHtml("chase-led-legend")}
  `;

  // wiring — settings changes rebaseline the alert set (no false "entered fog"
  // pings when membership shifts because a filter moved)
  const rerender = () => { saveChasePrefs(); chaseRebaseline(); renderChase(); };
  panelContent.querySelector("#chase-alerts")?.addEventListener("click", async () => {
    if (alertsArmed()) {
      chasePrefs.alerts = false;
    } else {
      // This tap is the user gesture both need. Each is tried on its own: a
      // browser with no Notification API (the button used to do nothing there
      // and say nothing) still gets the chime, and one that will not start
      // audio still gets the notification.
      try {
        chimeCtx ??= new AudioContext();
        await chimeCtx.resume();
      } catch { /* no sound in this browser */ }
      if ("Notification" in window && Notification.permission !== "granted") {
        try { await Notification.requestPermission(); } catch { /* an old Safari: no promise, or no permission prompt */ }
      }
      const can = alertsCan();
      chasePrefs.alerts = can.note || can.sound;
      if (can.sound) playChime();
      if (!chasePrefs.alerts) {
        flash("This browser allows this page neither notifications nor sound, so no fog alert can be given here. Keep the board on screen.");
      }
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
  // the LED choice goes through the same rerender as every other filter, so
  // the alert baseline takes in the airports it brings onto the board
  panelContent.querySelector("#chase-led")?.addEventListener("change", (e) => {
    chasePrefs.led = ledChoiceOf((e.target as HTMLSelectElement).value);
    chasePrefs.ledSetAt = Date.now();
    rerender();
  });
  panelContent.querySelector("#chase-led-ok")?.addEventListener("click", (e) => {
    e.preventDefault(); chasePrefs.ledSetAt = Date.now(); saveChasePrefs(); renderChase();
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
        <span class="chase-badges">${chaseRowBadges(c.end, c.a.icao)}</span>${withLive ? `<br>${liveLine(chaseLive?.get(c.a.icao))}` : ""}${extra}</td>
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
// The /api/deploy payload (forecast/build_deploy.py -> KV `deploy`). Four
// shapes can reach this code and all must render, because a deploy of the page
// and a build of the payload never land together:
//   - until 2026-11: no `pk`, no `meta.daycal`; p on a "cal" day was the hourly
//     curve's any-hour product, which ran about 1.7x high and still set GO
//   - 2026-11-25 (ae4df0b4): `pk` and `meta.daycal` version 2, built once a day
//   - daycal version 3: `cov`, day 2 on the "cal" tier with p null, area maps
//   - format 2 (hourly build): `meta.days`, `pfrom`, `bar`, `gaps`, `advisory`
// So every field added since the first shape is optional.
// One group of published chances and what happened on those days. `dates` is
// the number of forecast dates behind the airport-days; `ok` (areas only) is
// the build's own verdict on the group: within 10 points, or null when it has
// under 50 airport-days.
interface DeployBand { n: number; shown: number | null; obs: number | null; dates?: number; ok?: boolean | null; sample?: string | null }
// A group of fields with its own record of the day map (forecast/fit_daycal.py).
// ok false: the pooled map misses there by more than 10 points, so the day is
// not shown as calibrated for these fields. Until 2026-11-25 the panel printed
// the pooled record ("58% of the time") beside Central Valley fields whose own
// record was 35%.
interface DeployArea {
  name: string; ok?: boolean | null; high?: boolean | null; years?: string; icaos?: string[];
  // which cycles `years` means when the record is the build hour's own ("30 real 00Z cycles of 2025, never fitted on")
  sample?: string;
  ge50?: DeployBand | null; b25_50?: DeployBand | null; ge75?: DeployBand | null;
  // daycal version 3. regional true: these fields are on the area's OWN day
  // map, checked with each year held out in turn, and the bands above are that
  // map's record. floor false: the map is not held up to the peak hourly
  // chance, so p can sit below pk; `top` is the highest value it gives and
  // by_peak_hour is what days with such a peak hour did.
  regional?: boolean; floor?: boolean; top?: number; skill_pct?: number | null; n?: number;
  by_peak_hour?: { ge50?: DeployBand | null; b25_50?: DeployBand | null; ge75?: DeployBand | null };
  // how the hourly forecast's expected hours ran against the observed there
  hourly?: { runs?: string | null; eh?: { ratio?: number | null }; sample?: string } | null;
}
interface DeployCover { from?: number | null; through?: number | null }
interface DeployDayReceipt {
  cover_end_utc?: number | null; // day 2 only: p and pk use the hours up to this UTC hour
  cycle_hour?: number; checked_from?: number | null; sample?: string;
  n?: number; skill_pct?: number | null;
  ge50?: DeployBand | null; b25_50?: DeployBand | null; ge75?: DeployBand | null;
  areas?: DeployArea[];
  under_way?: boolean; cover?: DeployCover | null;
  failed?: { ge50?: DeployBand | null }[]; // the check this cycle hour's map did not pass
  // a day under way only: the same chance scored against fog AFTER the plan
  // could be read (the check counts the hour before the build is on screen)
  after_build?: { ge50?: DeployBand | null; b25_50?: DeployBand | null; ge75?: DeployBand | null } | null;
}
// format 2: what the hourly build says about days 1 and 2
interface DeployDayMeta {
  date?: string; under_way?: boolean; cover?: DeployCover | null; short?: number;
  chance?: {
    status?: string; cycle?: string | null; why?: string | null; too_old?: number;
    // a chance this cycle did not make is carried from the cycle that did, with
    // the check that went out with it and the fields the forecast has since risen at
    carried?: { cycle?: string; n?: number; below_peak?: string[]; receipt?: DeployDayReceipt }[];
  };
}
interface DeployAirport {
  eh: number[]; tiers: string[];
  p?: (number | null)[];  // chance of at least one below-CAT-I hour that day
  win?: number[]; ws?: (number | null)[];
  pk?: (number | null)[]; // peak hourly P(below CAT I), integer percent, "cal" days only
  cov?: (number | null)[];    // days 1-2: the UTC hour of that date the forecast runs through
  // days 1-2: climatological expected hours of the UTC hours past `cov` (a day
  // 2 seen in part). eh stays the forecast's own sum; a multi-day total adds this.
  ehc?: (number | null)[];
  pfrom?: (string | null)[];  // days 1-2: the cycle a carried p came from
  bar?: string | null;        // "pass" | "fail" | "few": the hourly forecast against the public bar
}
interface DeployData {
  meta?: {
    format?: number;
    generated?: string; cycle?: string; day1?: string; dayscale_skill?: number | null;
    days?: Record<string, DeployDayMeta>;
    gaps?: { code?: string; text?: string }[];
    advisory?: { status?: string; built?: string; missing?: string[] };
    bar?: { scored?: string; runs?: number; rule_plain?: string; codes?: Record<string, string> };
    daycal?: {
      fit?: string; test?: string;
      applied?: Record<string, boolean>;        // "1" / "2": THIS cycle published a chance for that day
      // why this cycle published none: "hour", "season", "reach", "hours"
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
let deployRing: Airport | undefined;       // the base whose ring and launch plan are open
// Until 2026-11-25 the plan was fetched once and kept for the life of the page:
// a tab left open (as the CHASE alerts advise) showed yesterday's plan under
// today's labels. Now a copy older than 15 minutes is asked for again, on open
// and while the panel stays open, and a failed refresh keeps the last good
// copy, says so, and tries again.
let deployFetchedAt: number | null = null; // when the copy on hand arrived
let deployFail: { at: number; why: string; n: number } | null = null; // the last refresh failed; n in a row
let deployNextTryAt: number | null = null;
let deployFetching: Promise<boolean> | null = null;
let deployFetchTimer: ReturnType<typeof setTimeout> | undefined;
let deployTickTimer: ReturnType<typeof setInterval> | undefined;
let deploySig = "";                        // what the panel as drawn depends on besides the data (see deploySigAt)
let deployLaunchAt = 0;                    // when the launch times on screen were worked out
let deployFillSeq = 0;
const deployPeeks = new Set<string>();     // fields whose 48 h strip the pilot opened

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
  // A day 2 the hourly forecast reaches only in part (through 09Z from the
  // morning build) carries the climatological hours of the rest in ehc. Without
  // them the 10:25Z plan counted 31% of the West Coast's day-2 hours and 66% of
  // the Southeast's, and the ranking moved with the hour of the build.
  for (let i = 0; i < Math.min(deployWindow, d.eh.length); i++)
    s += ((d.eh[i] ?? 0) + (d.ehc?.[i] ?? 0)) * usableFrac(d.win?.[i], eteH);
  return s;
}
// does the plan add climatology for the hours of day d the forecast does not reach?
function deployDayFilled(d: number): boolean {
  if (d > 1) return false;
  for (const a of Object.values(deployData?.airports ?? {})) if (a?.ehc?.[d] != null) return true;
  return false;
}

// same infrastructure filters the chase board saves — no base/distance term
// (and the same LED choice: chaseQualifying runs the rule the board runs, so
// the planner cannot rank a field the board would hide)
function deployQualifying(): Set<string> {
  return new Set(chaseQualifying(ledChoice()).map((x) => x.id));
}

// DEPLOY rows name fields, not runways. Where a field has LED approach lights
// the row says which runway end meets the saved filters and where the LED
// lights are: "KMFR RWY 14 [LED]", "KLAX RWY 25L, LED on 07R". A field with
// none gets no line (which is "not flagged" or "unknown", never "no LED").
function deployLedNote(icao: string, withIcao: boolean): string {
  const end = endShown(chaseData?.airports[icao], chasePrefs, ledChoice());
  const { pill, other } = ledRowBits(icao, end);
  if (!pill && !other) return "";
  // the field, its runway and the tag stay on one line: a tag wrapped away from its runway reads as the next field's
  return `<span style="white-space:nowrap">${withIcao ? `${icao} ` : ""}${end ? `RWY <b>${end.e}</b>` : ""}${pill ? ` ${pill}` : ""}</span>${other ? `${end ? ", " : ""}${other}` : ""}`;
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
      // a blue dot inside an LED ring: the ring's popup answers the click and the planner stays
      if (t.length && !ledRingUnder(ev.point)) openAirport(t[0].properties.icao);
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
  // nothing polls while the panel is closed; the next open asks again if the
  // copy on hand is more than 15 minutes old
  clearTimeout(deployFetchTimer);
  clearInterval(deployTickTimer);
  deployFetchTimer = deployTickTimer = undefined;
  deployNextTryAt = null;
  deployFillSeq++; // a launch plan still being read must not land on a closed panel
  deployPeeks.clear();
  deployRing = undefined;
  deploySig = "";
  if (map.getLayer("glow")) {
    applyApproachFilters();
    map.setPaintProperty("fogheat", "heatmap-opacity", FOGHEAT_OPACITY);
  }
  (map.getSource("deploy-src") as any)?.setData({ type: "FeatureCollection", features: [] });
  (map.getSource("deploy-ring-src") as any)?.setData({ type: "FeatureCollection", features: [] });
}

async function openDeploy() {
  const mine = ++panelToolSeq;
  await loadChaseData(); // a failure is said on the panel and tried again on the next open
  // The first open waits for the plan. A later open shows the copy on hand at
  // once, with its age, and asks for a new one when that copy is more than 15
  // minutes old.
  const first = !deployData;
  if (first) await fetchDeploy();
  if (mine !== panelToolSeq) return; // CHASE was opened while this was loading: it owns the panel
  // The CHASE board is closed only now, when the planner is about to be
  // drawn. It was closed before the two requests above: while /api/deploy
  // stalled, a board with its timers stopped stayed on screen for 20 minutes.
  history.replaceState(null, "", "#deploy");
  closeChaseMap();
  setSelected(null);
  // the planner talks about the NEXT two weeks — scrub the underlying
  // climatology field/dots to the current month, not the January default
  setMonths([new Date().getMonth()]);
  deployOpen = true;
  clearInterval(deployTickTimer);
  deployTickTimer = setInterval(tickDeploy, PLAN_TICK_MS);
  // the next try is set before the panel is drawn, so a failed first load can say when it is
  const due = !first && refetchDue(deployFetchedAt, Date.now());
  if (!due) {
    scheduleDeployFetch(deployFail ? retryDelayMs(deployFail.n)
      : Math.max(0, (deployFetchedAt ?? 0) + PLAN_REFETCH_MS - Date.now()));
  }
  renderDeploy();
  panel.hidden = false;
  document.body.classList.add("panel-open");
  if (due) refreshDeploy();
}

// One request at a time, shared by whoever asks. Resolves true when a NEW
// build arrived. Only a real payload replaces the copy on hand: /api/deploy
// answers 503 with a JSON body ({"error":"no deploy data yet"}) when KV is
// empty; that parsed, was kept for the session, and the panel printed "No
// expected fog within range of any base" under "data —" as if it had looked.
// A failed or malformed answer keeps the last good copy and is said on screen.
function fetchDeploy(): Promise<boolean> {
  deployFetching ??= (async () => {
    const had = deployData;
    let why = "";
    const ctl = new AbortController();
    // a request that never answers would otherwise hold this slot for good and
    // stop every later refresh
    const timer = setTimeout(() => ctl.abort(), PLAN_TIMEOUT_MS);
    try {
      const r = await fetch("/api/deploy", { signal: ctl.signal });
      if (!r.ok) why = `HTTP ${r.status}`;
      else {
        let j: unknown = null;
        try { j = await r.json(); } catch { why = "the answer could not be read"; }
        if (!why) why = planProblem(j) ?? "";
        if (!why) deployData = j as DeployData;
      }
    } catch {
      why = ctl.signal.aborted ? `no answer in ${PLAN_TIMEOUT_MS / 1000} seconds` : "no connection";
    }
    clearTimeout(timer);
    const now = Date.now();
    if (why) deployFail = { at: now, why, n: (deployFail?.n ?? 0) + 1 };
    else { deployFail = null; deployFetchedAt = now; }
    if (!deployData) deployData = null;
    deployFetching = null;
    scheduleDeployFetch(deployFail ? retryDelayMs(deployFail.n) : PLAN_REFETCH_MS);
    // new: a plan where there was none, or one with another build time
    return !why && (!had || (deployData?.meta?.generated ?? null) !== (had.meta?.generated ?? null));
  })();
  return deployFetching;
}
function scheduleDeployFetch(delayMs: number) {
  clearTimeout(deployFetchTimer);
  deployNextTryAt = null;
  if (!deployOpen) return;
  deployNextTryAt = Date.now() + delayMs;
  deployFetchTimer = setTimeout(refreshDeploy, delayMs);
}
async function refreshDeploy() {
  if (!deployOpen) return;
  const fresh = await fetchDeploy();
  if (!deployOpen) return;
  // a new build redraws the panel where it stands; anything else only moves
  // the age and the refresh line
  if (fresh) renderDeploy(deployRing, true); else tickDeploy();
}

// What the panel as drawn depends on besides the payload: how stale the plan
// is, the viewer's calendar date ("today" and "tomorrow" move at local
// midnight) and whether it is evening ("tonight and tomorrow").
function deploySigAt(now: number): string {
  const f = planFreshness(deployData?.meta, now);
  return [deployData ? deployData.meta?.generated ?? "undated" : "none", f.level, f.why.join("+"), localIso(now), localHour(now) >= 17].join("|");
}
// Every 30 s: the plan's age, the refresh line and every age on the panel
// advance where they stand, as on the CHASE board, so the scroll position, an
// opened 48 h strip and a half-typed cruise speed are left alone. The panel is
// redrawn only when what it says would change: the plan crossed 3 h or 26 h,
// its first day ended, or the viewer's date moved.
function tickDeploy() {
  if (!deployOpen) return;
  const now = Date.now();
  // timers are throttled in a background tab and stop while the device sleeps:
  // a refresh that is overdue is asked for now, not when the timer wakes
  if (!deployFetching && deployNextTryAt != null && now > deployNextTryAt + PLAN_TICK_MS) refreshDeploy();
  if (deploySigAt(now) !== deploySig) {
    const el = document.activeElement as HTMLInputElement | null;
    // a cruise speed half typed is not thrown away; the next tick redraws
    const typing = !!el && el.id === "deploy-speed" && +el.value !== chasePrefs.speed;
    if (!typing) renderDeploy(deployRing, true);
    return;
  }
  const age = panelContent.querySelector<HTMLElement>("#deploy-age");
  if (age) age.textContent = planAgeLine(deployData?.meta, now);
  const fetchLine = panelContent.querySelector<HTMLElement>("#deploy-fetch");
  if (fetchLine) {
    fetchLine.textContent = deployFail ? refreshFailWords(deployFail, deployNextTryAt, deployData?.meta, now) : "";
    fetchLine.hidden = !deployFail;
  }
  const banner = panelContent.querySelector<HTMLElement>("#deploy-plan-banner");
  if (banner) banner.innerHTML = deployBannerInner(now);
  panelContent.querySelectorAll<HTMLElement>("[data-age-of]").forEach((el) => {
    el.textContent = ageWords(now - +el.dataset.ageOf!);
  });
  // a departure time that has passed must not stay on screen as a plan, and a
  // launch plan worked out 15 minutes ago is worked out again
  const passed = [...panelContent.querySelectorAll<HTMLElement>("[data-until]")].some((el) => +el.dataset.until! < now);
  if (passed || now - deployLaunchAt > PLAN_REFETCH_MS) fillDeployLaunch();
}
// the moment the pilot looks at the tab again, not up to 30 s later
document.addEventListener("visibilitychange", () => { if (!document.hidden) tickDeploy(); });

function deployBannerInner(now: number): string {
  const b = planBanner(planFreshness(deployData?.meta, now));
  if (!b) return "";
  return `<b style="display:block;letter-spacing:0.06em;margin-bottom:3px">${escHtml(b.title)}</b>` +
    b.lines.map((l) => `<div>${escHtml(l)}</div>`).join("");
}

// ---- which day, and which of its hours ----
// Plan day index d (0 = the plan's first day) is meta.day1 + d. Nothing below
// counts days from the browser clock.
const deployDayIso = (d: number) => planDayIso(deployData?.meta?.day1, d);
// The UTC hour most "cal" airports' forecast of that day runs through (their
// `cov`). Null when no airport is on the hourly forecast that day, or in a
// payload from before `cov`.
const deployCovCache = new WeakMap<DeployData, Map<number, { cal: number; cov: number | null }>>();
function deployDayCov(d: number): { cal: number; cov: number | null } {
  if (!deployData) return { cal: 0, cov: null };
  let memo = deployCovCache.get(deployData);
  if (!memo) deployCovCache.set(deployData, memo = new Map());
  let v = memo.get(d);
  if (!v) memo.set(d, v = deployDayCovOf(d));
  return v;
}
function deployDayCovOf(d: number): { cal: number; cov: number | null } {
  const n = new Map<number, number>();
  let cal = 0;
  for (const a of Object.values(deployData?.airports ?? {})) {
    if (a?.tiers?.[d] !== "cal") continue;
    cal++;
    const c = a.cov?.[d];
    if (typeof c === "number") n.set(c, (n.get(c) ?? 0) + 1);
  }
  const top = [...n].sort((x, y) => y[1] - x[1])[0];
  return { cal, cov: top ? top[0] : null };
}
// Which hours of day d the hourly numbers stand on: for one airport, or (no
// icao) for the day as most airports have it. A day on a climatology tier is a
// whole day whatever the forecast's reach.
function deployPart(d: number, icao?: string): DayPart {
  if (d > 1) return {};
  const k = String(d + 1);
  const receipt = deployData?.meta?.daycal?.days?.[k];
  const day = deployData?.meta?.days?.[k] ?? receipt;
  if (icao) {
    const a = deployData?.airports?.[icao];
    return a?.tiers?.[d] === "cal" ? dayPart(d, day, a.cov?.[d], receipt?.cover_end_utc) : {};
  }
  const { cal, cov } = deployDayCov(d);
  return cal ? dayPart(d, day, cov, receipt?.cover_end_utc) : {};
}
// An expired plan's days get their dates and no "today" or "tomorrow": its
// first day can still be the viewer's today hours after that morning's fog.
const deployPlain = (now: number) => planFreshness(deployData?.meta, now).level === "expired";
function deployDayWords(d: number, icao?: string, now = Date.now()): DayWords | null {
  const iso = deployDayIso(d);
  return iso ? dayWords(iso, now, deployPart(d, icao), undefined, deployPlain(now)) : null;
}
// The day as the CHANCE at one field speaks for it. A carried chance stands on
// the hours its own cycle covered, not on this cycle's: the 15Z cycle that
// lost its later bulletin stops at 16Z, and the 09Z chance it carries was for
// the whole date.
function deployChanceWords(d: number, icao: string, now = Date.now()): DayWords | null {
  const iso = deployDayIso(d);
  if (!iso) return null;
  const rc = deployReceipt(icao, d);
  const part = rc.status === "carried"
    ? dayPart(d, rc.receipt, rc.receipt?.cover?.through, rc.receipt?.cover_end_utc)
    : deployPart(d, icao);
  return dayWords(iso, now, part, undefined, deployPlain(now));
}
// for a row or a chip: "tomorrow, Wed 25"; "day 3" only when the plan gives no date
const deployDayLabel = (d: number, now = Date.now()) => deployDayWords(d, undefined, now)?.label ?? `day ${d + 1}`;

// The check that went out with the number an airport shows on day d: this
// build's own (meta.daycal.days) or, for a carried chance, the one the earlier
// cycle published with it. Never this build's check beside a carried number:
// it says "failed" or names another map.
function deployReceipt(icao: string, d: number): { status: ChanceStatus; from: string | null; receipt: DeployDayReceipt | null } {
  const k = String(d + 1);
  const st = chanceStatus(deployData?.airports?.[icao], d);
  if (st.status === "carried") {
    const c = deployData?.meta?.days?.[k]?.chance?.carried?.find((x) => x.cycle === st.from);
    return { ...st, receipt: c?.receipt ?? null };
  }
  return { ...st, receipt: deployData?.meta?.daycal?.days?.[k] ?? null };
}

// "passed", "not passed" or "too little fog to score": how the HOURLY forecast
// at one airport has verified against the public bar, in words, with the rule
// and the scorecard date behind a hover or a tap. Until 2026-11-25 the panel
// called every "cal" airport calibrated although 302 of the 926 had been
// scored and had not passed and 290 had too little fog to score.
function deployBarTag(icao: string, lead: string): string {
  const bar = deployData?.airports?.[icao]?.bar;
  if (bar !== "pass" && bar !== "fail" && bar !== "few") return "";
  const mb = deployData?.meta?.bar;
  // "too little fog to score" read as a statement about the airport (KVIS and
  // KFAT, in fog season); it is about the weeks scored so far
  const words = bar === "pass" ? "passed" : bar === "fail" ? "not passed" : "not scored yet: too little fog since scoring began";
  const tip = [
    `${icao}: ${mb?.codes?.[bar] ?? words}.`,
    mb?.rule_plain ? `The bar: ${mb.rule_plain}` : "",
    mb?.scored ? `Scorecard of ${mb.scored}${mb.runs ? ` (${mb.runs.toLocaleString("en-US")} forecast runs)` : ""}.` : "",
    "This is about the hourly forecast there (peak hour, expected hours), not the day-level chance.",
  ].filter(Boolean).join(" ");
  return `<span class="dtip" data-bar="${bar}" data-tip="${escHtml(tip)}" title="${escHtml(tip)}">${lead}${words}</span>`;
}
// a tap opens the same text a hover shows: a phone has no hover
function wireDeployTips(root: ParentNode) {
  root.querySelectorAll<HTMLElement>(".dtip[data-tip]").forEach((el) => {
    if (el.dataset.wired) return;
    el.dataset.wired = "1";
    el.addEventListener("click", (ev) => {
      ev.stopPropagation(); // the row under it selects a base or opens a strip
      const open = el.nextElementSibling;
      if (open?.classList.contains("dtip-line")) { open.remove(); return; }
      const line = document.createElement("span");
      line.className = "dtip-line";
      line.textContent = el.dataset.tip!;
      el.after(line);
    });
  });
}

// ---- what a day's number rests on ----
// Every day-level number on this panel (the verdict, the day cells, "fog likely
// <day>") is the payload's p[d]: the chance of at least one hour below CAT I
// that day at one field. What stands behind p differs by tier and by payload
// age. Until 2026-11 the panel printed them all as one bare "% likely
// chaseable": the tier reached the screen only as "(cal)" in a hover title, and
// a 93% that verified near 40% set the GO word with nothing on screen to say so.
//   cal    "cal" tier with a checked day-level chance: the hourly curve's
//          any-hour product pushed through the hindcast-fitted day map (the
//          pooled one, or the field's own area map). This build's own, or one
//          carried from an earlier cycle with the check that went out with it.
//   area   the pooled map's number at a field whose area missed that map's
//          check at this level (West Coast fields shown at 50% or more ran 11
//          to 27 points high): a forecast, but not a calibrated one there
//   bound  "cal" tier in a payload from before the day map, or a day an old
//          build did not map: the bare any-hour product. It treats consecutive
//          fog hours as independent draws, so it is a ceiling on the chance,
//          not the chance.
//   fit    days 3-8, the fitted day-scale model
//   nbe, cpc, climo   climatology times a rule-of-thumb factor, or climatology
//          alone: no forecast model stands behind the number
//   other  a tier name this build of the page does not know
type DeployBasis = "cal" | "area" | "bound" | "fit" | "nbe" | "cpc" | "climo" | "other";

// with a field and its chance, a "cal" day is also held against that field's
// own area record (daycalCheck)
function deployBasis(tier: string | undefined, d: number, icao?: string, p?: number | null): DeployBasis {
  if (tier === "cal") {
    const meta = deployData?.meta;
    // The hourly build (format 2) puts a number on a "cal" day only when it is
    // a checked chance, its own or a carried one, and meta.daycal.applied is
    // false on a carried day. Older builds wrote the bare product there
    // whenever applied was false.
    const mapped = (meta?.format ?? 0) >= 2 ? p != null : !!meta?.daycal?.applied?.[String(d + 1)];
    if (!mapped) return "bound";
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
  // The chance the 50% and 25% bars are held against. It is p, except at a
  // field whose area ran HIGH on the pooled map's check: there it is p less
  // that miss (wordChance), so a word is never stronger than the line that
  // says how far off the number ran. The percent printed is always p.
  wp: number;
  basis: DeployBasis | null;  // null: no in-reach field carries a chance for this day
  pk: number | null;          // peak hourly %, "cal" days of new payloads only
  eh: number | null;
  status: ChanceStatus;       // whose chance p is: this build's, or carried from the cycle in `from`
  from: string | null;
  noP: number;                // in-reach fields whose chance is missing from the payload
  // the highest peak hour among those, for a day the build published no chance for
  hourly: { icao: string; pk: number; eh: number | null } | null;
  // The in-reach field with the highest peak hour among those whose area map
  // is not held up to it (Central Valley, Mountain West): their chance stops
  // at 23% or 39% whatever the hourly forecast says, so such a field can never
  // be the best of the day, and the verdict names it on a line of its own.
  peak: { icao: string; p: number; pk: number; note: string; title: string } | null;
}
function deployPick(c: DeployBase, d: number): DeployPick {
  const out: DeployPick = { p: 0, wp: 0, icao: "", basis: null, pk: null, eh: null, status: "none", from: null, noP: 0, hourly: null, peak: null };
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
      // a "cal" day can carry no chance (day 2 in fog season, an issuance
      // without the 26-48 h bulletin). Missing is not zero: counting it as 0%
      // would print SCRAP from a number that was never made.
      out.noP++;
      if (pk != null && (!out.hourly || pk > out.hourly.pk)) out.hourly = { icao: x.a.icao, pk, eh: da.eh?.[d] ?? null };
      continue;
    }
    const basis = deployBasis(da.tiers?.[d], d, x.a.icao, p);
    const c = basis === "cal" || basis === "area" ? daycalCheck(d, x.a.icao, p, pk) : null;
    // held back by the miss its own line states, on a calibrated number too: a
    // 52% under "ran about 6 points high on days like this" printed a green GO
    const wp = c ? wordChance(p, c.hold) : p;
    if (c?.peakNote && pk != null && (!out.peak || pk > out.peak.pk)) out.peak = { icao: x.a.icao, p, pk, note: c.peakNote, title: c.peakTitle };
    // best by the chance the word is read from, so a number known to run high
    // does not hide a checked one just under it
    if (!out.basis || wp > out.wp || (wp === out.wp && p > out.p)) {
      const st = d < 2 ? chanceStatus(da, d) : { status: "fresh" as ChanceStatus, from: null };
      out.p = p; out.wp = wp; out.icao = x.a.icao; out.basis = basis;
      out.pk = pk; out.eh = da.eh?.[d] ?? null; out.status = st.status; out.from = st.from;
    }
  }
  return out;
}

const deployPicks = (c: DeployBase) => Array.from({ length: deployWindow }, (_, d) => deployPick(c, d));

// first day in the window with a REACHABLE field at 50% or more —
// the operator's "when could we actually go" number. The day's name comes from
// the plan's own date: it used to be the word "tomorrow" for index 0 and a
// weekday counted from the browser clock for the rest.
function firstLikely(picks: DeployPick[]): { label: string; p: number; icao: string; d: number; basis: DeployBasis; from: string | null } | null {
  for (let d = 0; d < picks.length; d++) {
    const k = picks[d];
    if (k.basis && k.wp >= 0.5) return { label: deployDayLabel(d), p: k.p, icao: k.icao, d, basis: k.basis, from: k.from };
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
interface DaycalCheck {
  calibrated: boolean;       // false: the pooled map missed in this field's area at this level
  text: string; title: string;
  area: DeployArea | null;
  gap: number | null;        // points the quoted group ran high (+) or low (-)
  // points to hold the number back by before the 50% and 25% bars are applied
  // (wordChance): set only when the miss was measured on days like this one
  hold: number | null;
  own: boolean;              // the field is on its area's own map
  // an area map that is not held up to the peak hour, under a peak hour that
  // is higher than the chance: what days with such a peak hour did there
  peakNote: string; peakTitle: string;
  ehNote: string;            // the area's expected hours ran high or low against the observed
}
// which area's record a field is held against: the first area that lists it
const deployAreaIndex = new WeakMap<DeployDayReceipt, Map<string, DeployArea>>();
function deployAreaOf(r: DeployDayReceipt, icao: string): DeployArea | null {
  let idx = deployAreaIndex.get(r);
  if (!idx) {
    idx = new Map();
    for (const a of r.areas ?? []) for (const i of Array.isArray(a.icaos) ? a.icaos : []) if (!idx.has(i)) idx.set(i, a);
    deployAreaIndex.set(r, idx);
  }
  return idx.get(icao) ?? null;
}
type UsableBand = DeployBand & { shown: number; obs: number };
const usableBand = (b: DeployBand | null | undefined): b is UsableBand =>
  !!b && b.n >= 50 && typeof b.shown === "number" && typeof b.obs === "number"
  && Number.isFinite(b.shown) && Number.isFinite(b.obs);
const gapPoints = (b: UsableBand) => Math.round(100 * (b.shown - b.obs));
function gapWords(b: UsableBand): string {
  const g = gapPoints(b);
  if (Math.abs(g) <= 2) return "about right";
  // Ten points is the line between "calibrated" and "not calibrated": 9.8 and
  // 10.3 both printed "about 10 points", one under each heading.
  const exact = Math.abs(Math.round(1000 * (b.shown - b.obs)) / 10);
  const n = exact >= 9.5 && exact < 10 ? "just under 10" : exact > 10 && exact < 10.5 ? "just over 10" : `about ${Math.abs(g)}`;
  return `${n} points ${g > 0 ? "high" : "low"}`;
}
function daycalCheck(d: number, icao: string, p: number, pk?: number | null): DaycalCheck {
  const dc = deployData?.meta?.daycal;
  // the check that went out with THIS number: a carried chance brings its own
  const r = deployReceipt(icao, d).receipt;
  const year = /\b(20\d\d)\b/.exec(dc?.test ?? "")?.[1];
  const checked = year ? `Checked on ${year}` : "Checked on a year it was not tuned on";
  const out: DaycalCheck = {
    calibrated: true, text: `Calibrated forecast${year ? `, checked on ${year}` : ""}.`, title: "",
    area: null, gap: null, hold: null, own: false, peakNote: "", peakTitle: "", ehNote: "",
  };
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
  const area = deployAreaOf(r, icao);
  out.area = area;
  const inBand = p >= 0.25;
  const ownLevel = key === "ge75" ? "75% or more" : key === "ge50" ? "50% or more" : "25% to 49%";
  // "days like this" is only said of a number inside the group that is quoted
  const likeWords = (level: string) => (inBand && level === ownLevel ? "on days like this" : `on days shown at ${level}`);
  const ranWords = (b: UsableBand) => (Math.abs(gapPoints(b)) <= 2 ? "was about right" : `ran ${gapWords(b)}`);
  const ratio = area?.hourly?.eh?.ratio;
  if (area && (area.hourly?.runs === "high" || area.hourly?.runs === "low") && typeof ratio === "number" && Number.isFinite(ratio)) {
    // beside the expected hours: at Central Valley fields they ran twice the
    // observed from evening cycles (294 h expected, 145 observed, 2025)
    out.ehNote = `At ${area.name} fields expected hours ran ${area.hourly.runs}: about ${ratio.toFixed(ratio < 1 ? 2 : 1)} times the observed${area.hourly.sample ? ` (${area.hourly.sample})` : ""}.`;
  }
  if (area?.regional) {
    // The field is on its area's own map (daycal version 3). The pooled map's
    // "not calibrated there" flag is gone and the line states the area's own
    // record: its group at this level when that has 50 airport-days, and the
    // highest value an unfloored map can give.
    out.own = true;
    const yrs = area.years ? ` (${area.years}, each year held out in turn)` : "";
    const top = area.floor === false && typeof area.top === "number" && Number.isFinite(area.top)
      ? ` The day chance there never goes above ${pc(area.top)}%.` : "";
    const x = pick(area);
    if (x && inBand) {
      out.gap = gapPoints(x.b);
      out.text = `Checked for ${area.name} fields${yrs}: it ${ranWords(x.b)} ${likeWords(x.level)}.${top}`;
    } else {
      out.text = `Checked for ${area.name} fields${yrs}.${inBand ? " Too few days at this level there to say how it ran." : ""}${top}`;
    }
    out.title = [
      x ? detail(`Days at ${area.name} fields`, x, area.years ? `${area.years}, each year held out in turn` : "") : "",
      area.skill_pct != null && Number.isFinite(area.skill_pct)
        ? `Against climatology at ${area.name} fields: ${signedPct(area.skill_pct)} (Brier skill${area.n ? `, ${area.n.toLocaleString("en-US")} airport-days` : ""}).` : "",
      dc?.fit ? `The day-level chance was fitted on: ${dc.fit}.` : "",
    ].filter(Boolean).join(" ");
    if (area.floor === false && pk != null && pk >= 25 && pk > pc(p)) {
      // Do not print the peak hour as if it agreed with the chance. The honest
      // line is what days with such a peak hour did there (by_peak_hour).
      const pkKey = pk >= 75 ? "ge75" : pk >= 50 ? "ge50" : "b25_50";
      let b = area.by_peak_hour?.[pkKey], level = pk >= 75 ? "75% or more" : pk >= 50 ? "50% or more" : "25% to 49%";
      if (pkKey === "ge75" && !usableBand(b) && area.by_peak_hour?.ge50?.n) { b = area.by_peak_hour.ge50; level = "50% or more"; }
      const head = `Peak hourly chance ${pk}% is higher than the day chance.`;
      // the record says which cycles it is: the build hour's own, or the three years at the fit hour
      const span = b?.sample ? ` (${b.sample.replace(/, never fitted on$/, "")})` : area.years ? ` (${area.years})` : "";
      out.peakNote = usableBand(b)
        ? `${head} ${area.name} days with a peak hour at ${level} had an hour below CAT I ${pc(b.obs)}% of the time${span}.`
        : b && b.n > 0 && typeof b.obs === "number"
        ? `${head} Of ${b.n} ${area.name} airport-days with a peak hour at ${level}, ${Math.round(b.obs * b.n)} had an hour below CAT I${span}: too few to lean on.`
        : `${head} No ${area.name} days with a peak hour at ${level} are in the record.`;
      out.peakTitle = `The day chance at ${area.name} fields comes from the area's own map, which is not held up to the peak hourly chance.` +
        (b && b.n > 0 ? ` ${b.n.toLocaleString("en-US")} airport-days${b.dates ? ` from ${b.dates} forecast dates` : ""}.` : "");
    }
    return out;
  }
  // version 2 quoted the weekly 12Z sample for every area; version 3 areas are
  // judged on the cycle hour's own sample and carry only the years
  const areaSample = (a: DeployArea) => a.sample ?? (a.years ? (a.regional === undefined ? `${a.years}, weekly 12Z forecasts` : a.years) : "");
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
        if (inBand) {
          out.text = `Calibrated forecast. At ${area.name} fields${yrs} it ${ranWords(x.b)} ${likeWords(x.level)}.`;
          if (g > 2 && x.level === ownLevel) out.hold = g;
        }
      } else {
        out.calibrated = false;
        if (g > 0 && inBand) out.hold = g;
        out.text = `Not calibrated for ${area.name} fields: there it ${ranWords(x.b)} ${likeWords(x.level)}${yrs}. Expect the real chance to be ${g > 0 ? "lower" : "higher"}.`;
      }
      out.title = `${detail(`Days at ${area.name} fields`, x, areaSample(area))} ${about}`.trim();
      return out;
    }
    if (area.ok === false && usableBand(area.ge50)) {
      // the area missed where it could be judged, and this number sits where it could not
      const g = gapPoints(area.ge50);
      out.calibrated = false; out.gap = g;
      out.text = `Not calibrated for ${area.name} fields: there, days shown at 50% or more ran ${gapWords(area.ge50)}${yrs}.`;
      out.title = `${detail(`Days at ${area.name} fields`, { b: area.ge50, level: "50% or more" }, areaSample(area))} ${about}`.trim();
      return out;
    }
  }
  let x = inBand ? pick(r) : null;
  // A day under way: its check counts fog at any hour after the forecast's
  // own hour, and the plan is on screen an hour and a half after that. Scored
  // only from when it could be read, the same numbers ran further off (03Z: 10
  // points high where the check said 9; 06Z: 11 where it said 7). The larger
  // miss is the one printed.
  const late = inBand && r.under_way && r.after_build ? pick(r.after_build as DeployDayReceipt) : null;
  const fromBuild = !!x && !!late && Math.abs(gapPoints(late.b)) > Math.abs(gapPoints(x.b));
  if (fromBuild) x = late;
  if (x) {
    out.gap = gapPoints(x.b);
    out.text = `Calibrated forecast. ${checked}: it ${ranWords(x.b)} ${likeWords(x.level)}${fromBuild ? ", counting fog after the plan was built" : ""}.`;
    out.title = `${detail("Days", x, x.b.sample ?? r.sample)} ${about}`.trim();
    if (out.gap > 2 && x.level === ownLevel) out.hold = out.gap;
  } else {
    out.title = [r.sample ? `Checked on: ${r.sample}.` : dc?.test ? `Checked on: ${dc.test}.` : "", about].filter(Boolean).join(" ");
  }
  return out;
}

// "Fri 27 to Wed 2", "Thu 26", "Wed 25, Fri 27": plan days (1-based) by their
// own dates, the way the cells are labelled. "days 3–8" only when the plan
// gives no date to count from.
function daySpans(days: number[]): string {
  const isos = days.map((n) => deployDayIso(n - 1));
  if (isos.every((x): x is string => x != null)) return dateSpans(isos);
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
        : basisWords(deployBasis(a.tiers[d], d, icao, p)) + (d < 2 && a.pfrom?.[d] ? ", day chance carried from an earlier forecast" : "");
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
    // by date, as the cells are labelled; "Days 3–8" only for a plan without one
    const isos = Array.from({ length: s.to - s.from + 1 }, (_, i) => deployDayIso(s.from - 1 + i));
    const days = isos.every((x): x is string => x != null) ? dateSpans(isos)
      : s.from === s.to ? `Day ${s.from}` : `Days ${s.from}–${s.to}`;
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
// "ran about 27 points high at West Coast fields"
function areaMissWords(icao: string, d: number, p: number): string {
  const c = daycalCheck(d, icao, p);
  if (!c.area) return basisWords("area");
  // a number at 50% or more that the area's own miss takes under 50% is not
  // counted as a likely day (wordChance): say so wherever the miss is named
  const held = p >= 0.5 && wordChance(p, c.hold) < 0.5;
  return `forecast not calibrated for ${c.area.name} fields` +
    (c.gap != null && Math.abs(c.gap) > 2 ? ` (ran about ${Math.abs(c.gap)} points ${c.gap > 0 ? "high" : "low"} there${held ? ", so not counted as 50% or more" : ""})` : "");
}
// Why this build has no day-level chance for day d, in a few words
// (plan.ts noChanceWords, from the reason the build itself gives).
function deployNoChanceWhy(d: number): string {
  if (d > 1) return "";
  const k = String(d + 1), meta = deployData?.meta;
  const ch = meta?.days?.[k]?.chance;
  const cyc = meta?.cycle ? new Date(meta.cycle).getUTCHours() : NaN;
  return noChanceWords(ch?.why ?? meta?.daycal?.why?.[k],
    meta?.daycal?.cycle_hour ?? (Number.isFinite(cyc) ? cyc : null),
    meta?.daycal?.days?.[k]?.failed?.[0], ch?.too_old);
}
// "KBQK 22%, expected hours 1.8 through 09Z": the two numbers of the hourly
// forecast for a day, with the hours they cover when that is not the whole day
function hourlyWords(icao: string, d: number, pk: number | null, eh: number | null): string {
  const part = deployDayWords(d, icao)?.part;
  const bits = [pk != null ? `peak hourly chance ${pk}%` : "", eh != null ? `expected hours ${eh.toFixed(1)}` : ""].filter(Boolean);
  return bits.length ? `${icao} ${bits.join(", ")}${part ? ` ${part}` : ""}` : "";
}
function deployCellTitle(k: DeployPick, d: number): string {
  const iso = deployDayIso(d);
  const day = iso ? dayLong(iso) : `day ${d + 1}`;
  if (!k.basis) {
    if (!k.noP) return `${day}: no field in reach has a fog window long enough to reach`;
    // a day with a forecast and no day-level chance: the hourly numbers and
    // the reason, never a guess at a chance
    const hw = k.hourly ? hourlyWords(k.hourly.icao, d, k.hourly.pk, k.hourly.eh) : "";
    return `${day}: no day-level chance from this build for the fields in reach. ${deployNoChanceWhy(d)}`.trim() +
      (hw ? ` Highest peak hour in reach: ${hw}.` : "");
  }
  // the hours the CHANCE stands on (a carried one stands on its own cycle's), as the one-day box says them
  const part = (d < 2 ? deployChanceWords(d, k.icao) : deployDayWords(d, k.icao))?.part;
  let t = `${day}: ${k.icao} ${k.basis === "bound" ? "up to " : ""}${deployPct(k.p)}% chance of at least one hour below CAT I${part ? ` ${part}` : ""} · ${k.basis === "area" ? areaMissWords(k.icao, d, k.p) : basisWords(k.basis)}`;
  if (k.status === "carried" && k.from) t += ` · day chance from ${cycleWords(k.from, Date.now())}`;
  if (k.basis === "cal" || k.basis === "area") {
    // a peak hour above the chance of an area map that is not held up to it is
    // never printed bare, as if the two agreed
    const c = daycalCheck(d, k.icao, k.p, k.pk);
    t += c.peakNote ? ` · ${c.peakNote}` : k.pk != null ? ` · peak hourly chance ${k.pk}%` : "";
  }
  return t;
}

// The launch the hourly forecast supports from one base: the field in reach
// whose fog (under 1 mile) peaks highest among those with a window that can
// still be reached, and the wheels-up time that arrives as that window opens
// (15 min buffer). WHEELS UP, the verdict's launch line and every base row's
// "deploy by" read this one function. They used to be two calculations (the
// row's from the plan's fog-window lead, the box's from the hourly forecast),
// and on 2026-11-24 every row printed "deploy by Wed 03:54" under a verdict
// that said there was no launch time.
// `p` is the field's peak in the whole forecast: fields are ranked by it and it
// is never printed, because that hour can be behind the clock. What is printed
// is `fog`: the chance of the hour the launch reaches (plan.ts launchChance).
interface DeployLaunch { p: number; fog: ReturnType<typeof launchChance>; icao: string; nm: number; ete: number; openMs: number; wheelsMs: number }
function deployLaunch(c: DeployBase, fc: any, now: number): DeployLaunch | null {
  const cyc = new Date(fc.meta.cycle).getTime();
  // an hourly forecast this old is not a plan (build_deploy.py refuses the same age)
  if (!(now - cyc < HOURLY_DEAD_MS)) return null;
  let best: DeployLaunch | null = null;
  for (const x of c.contrib.slice(0, 12)) {
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
      if (o - now >= ete * 3600e3) { openMs = o; break; }
    }
    if (!openMs) continue;
    if (!best || peak > best.p) best = { p: peak, fog: launchChance(f.fhrs, p10, cyc, openMs, thr), icao: x.a.icao, nm: x.nm, ete, openMs, wheelsMs: openMs - (ete + 0.25) * 3600e3 };
  }
  return best;
}

// A base row's departure line: from the CHASE base, through that base (30 min
// turn), to the field in its reach whose fog under 1 mile peaks highest among
// those the whole chain can still make. plan.ts chainDeparture works it out
// over every hour of that field's window: until 2026-11-25 it was WHEELS UP's
// first reachable hour less the reposition leg, which with a window open was
// behind the clock for almost every base (26 of 28 rows said "departure time
// passed" while hours of the same window could still be reached).
// `fc` undefined: the hourly forecast is not on hand. A time that has passed
// is said as passed, in the past tense, and only when no hour can be made.
function deployDep(home: Airport, c: DeployBase, fc: any | null | undefined, now: number): { text: string; title: string; until: number | null } {
  const repoH = distNm(home, c.b) / chasePrefs.speed;
  const repo = `reposition ${eteStr(repoH)}`;
  const repoTitle = `Reposition: still-air flight from your CHASE base ${home.icao} to ${c.b.icao} at ${chasePrefs.speed} kt.`;
  if (!fc) return { text: repo, title: `${repoTitle} No departure time: the hourly forecast is not on hand.`, until: null };
  const cyc = new Date(fc.meta.cycle).getTime();
  const none = { text: `no launch time yet · ${repo}`, until: null,
    title: `No field in reach of ${c.b.icao} clears the launch plan's 40% bar (fog under 1 mile) in the next 30 hours, so there is no departure time. ${repoTitle}` };
  // an hourly forecast this old is not a plan (build_deploy.py refuses the same age)
  if (!(now - cyc < HOURLY_DEAD_MS)) return none;
  type Row = { icao: string; peak: number; ete: number; d: ReturnType<typeof chainDeparture>; fog: string };
  let best: Row | null = null, missed: Row | null = null;
  for (const x of c.contrib.slice(0, 12)) {
    const f = fc.airports?.[x.a.icao];
    if (!f) continue;
    const p10: number[] = f.p.map((r: number[]) => r[0]);
    const peak = Math.max(...p10);
    if (peak < 40) continue;
    const thr = Math.max(15, peak * 0.4); // the launch plan's own bar (deployLaunch)
    const hours: number[] = [];
    for (let i = 0; i < f.fhrs.length; i++) if (f.fhrs[i] <= 30 && p10[i] >= thr) hours.push(cyc + f.fhrs[i] * 3600e3);
    const ete = x.nm / chasePrefs.speed;
    const d = chainDeparture(hours, (repoH + 0.5 + ete) * 3600e3, now);
    if (d.kind === "none") continue;
    // the hover gives the chance of the first hour this chain can still make, never the forecast's peak (deployLaunch)
    const w = d.openMs == null ? "" : launchChanceWords(launchChance(f.fhrs, p10, cyc, d.openMs, thr), d.openMs, home.tz);
    const row = { icao: x.a.icao, peak, ete, d, fog: w ? ` (Fog under 1 mile at ${x.a.icao} in the first hour you can still make: ${w}.)` : "" };
    if (d.kind === "missed") { if (!missed || peak > missed.peak) missed = row; continue; }
    if (!best || peak > best.peak) best = row;
  }
  if (!best && !missed) return none;
  const r = (best ?? missed)!;
  const chain = `from your CHASE base ${home.icao} (still-air, ${chasePrefs.speed} kt) to ${c.b.icao}, a 30 min turn there, and ${eteStr(r.ete)} on to ${r.icao}`;
  if (r.d.kind === "missed") {
    return { text: `too late for ${r.icao} from ${home.icao} (last departure ${zoneClock(r.d.byMs!, home.tz)})`, until: null,
      title: `The last departure ${chain} that reached ${r.icao} inside its fog window was ${zoneClock(r.d.byMs!, home.tz)}. It has passed, and no other field in reach of ${c.b.icao} has a window this chain can still make.` };
  }
  if (r.d.kind === "open") {
    return { text: `leave by ${zoneClock(r.d.byMs!, home.tz)} for ${r.icao} · its fog window lasts to about ${zoneClock(r.d.endMs!, home.tz)}`, until: r.d.byMs,
      title: `${r.icao}'s fog window is open, or opens before you could be there. The latest departure ${chain} that still arrives inside it is ${zoneClock(r.d.byMs!, home.tz)}; leaving sooner gets more of it. Times are in ${home.icao}'s zone.${r.fog} Not factored into the ranking.` };
  }
  return { text: `deploy by ${zoneClock(r.d.byMs!, home.tz)} for ${r.icao}`, until: r.d.byMs,
    title: `Latest departure ${chain}, arriving as its fog window opens (${zoneClock(r.d.openMs!, home.tz)}) with a 15 min buffer. The time is in ${home.icao}'s zone.${r.fog} Not factored into the ranking.` };
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

// "A", "A and B", "A, B and C"
const joinAnd = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
const lowerFirst = (s: string) => (s ? s[0].toLowerCase() + s.slice(1) : s);

// what the launch plan needs from the panel as last drawn (fillDeployLaunch)
let deployView: {
  top: DeployBase[]; sel: DeployBase | null; home: Airport | undefined;
  noteFor: { icao: string; word: string } | null; beyond48: string; expired: boolean;
} | null = null;

// `keep`: a redraw the pilot did not ask for (a new build arrived, the plan
// crossed an age line). It leaves the scroll position and the map where they are.
function renderDeploy(ringBase?: Airport, keep = false) {
  if (!deployOpen) return;
  const now = Date.now();
  const scroll = panel.scrollTop;
  // The 48 h section is filled in after the draw. On a redraw the pilot did
  // not ask for it keeps its height meanwhile, or a panel scrolled down into
  // it would jump up while it is empty.
  const hold48 = keep ? panelContent.querySelector<HTMLElement>("#deploy-48")?.offsetHeight ?? 0 : 0;
  const radius = Math.round(chasePrefs.speed * chasePrefs.maxEteH);
  const { targets, top } = deployData ? deployRank() : { targets: [], top: [] };
  // a selected base that a settings change dropped from the list is no longer
  // selected: the verdict fell back to the top base while the plan section
  // stayed keyed to the old one, and stayed empty
  if (ringBase && !top.some((c) => c.b.icao === ringBase!.icao)) ringBase = undefined;
  deployRing = ringBase;
  deploySig = deploySigAt(now);
  // More than 26 h old, or its first day over, or undated: a red banner and no
  // GO, MARGINAL or SCRAP. Until 2026-11-25 a plan a day old printed the same
  // "tomorrow" under a grey "data 2026-11-24".
  const fresh = planFreshness(deployData?.meta, now);
  const expired = !!deployData && fresh.level === "expired";
  const home = state.airports.find((x) => x.icao === chasePrefs.base);
  const tierSpans = deployTierSpans();
  const beyond48 = deployTierText(tierSpans.filter((s) => s.from >= 3), true);
  const AMBER = "#ffb347", GREY = "#8da0b2";
  const rows = top.map((c, i) => {
    const picks = deployPicks(c);
    const fl = firstLikely(picks);
    // days the payload carries no chance for: "no strong fog day" must not be
    // said about a day nobody put a number on
    const blank = picks.filter((k) => !k.basis && k.noP).length;
    // a field whose area map is not held up to the peak hour, under a peak hour that says more (Central Valley)
    const capped = picks.map((k) => k.peak).find((x) => x && wordOf(x.pk / 100) !== wordOf(x.p)) ?? null;
    // the row says "likely" only on a number that was checked; any other basis
    // is named beside it, and the hover text gives the number and what it is
    const flText = fl
      ? `fog likely ${fl.label}${deployChecked(fl.basis) ? (fl.from ? ` (day chance from ${cycleWords(fl.from, now)})` : "") : fl.basis === "bound" ? " (upper bound)" : fl.basis === "area" ? " (not calibrated there)" : fl.basis === "other" ? " (unknown source)" : " (climatology)"}`
      // "no strong fog day in window" sat beside "23h" of chaseable hours and
      // a launch time for the same field: it says only what was checked
      : blank === picks.length ? "no day-level chance in this plan"
      : capped ? `no day at 50% or more · ${capped.icao} day chance stops at ${deployPct(capped.p)}% (peak hour ${capped.pk}%)`
      : "no day at 50% or more in window";
    const flTitle = fl
      ? `${fl.icao}: ${fl.basis === "bound" ? "up to " : ""}${deployPct(fl.p)}% chance of at least one hour below CAT I ${deployChanceWords(fl.d, fl.icao, now)?.phrase ?? "that day"}. Basis: ${fl.basis === "area" ? areaMissWords(fl.icao, fl.d, fl.p) : basisWords(fl.basis)}. It is the first day in the window when a field in reach of ${c.b.icao} is at 50% or more.`
      : blank === picks.length
      ? `This plan carries no day-level chance for the fields in reach of ${c.b.icao} in this window.`
      : `No field in reach of ${c.b.icao} has a 50% or higher chance of at least one hour below CAT I on any day in the window.${blank ? ` ${blank} of ${picks.length} days have no day-level chance in this plan.` : ""}`;
    // The departure line is filled in from the hourly forecast once it is
    // read (fillDeployLaunch), by the same function that sets WHEELS UP. Until
    // then, and without a forecast, it is the plain repositioning time.
    const dep = !home || expired ? ""
      : home.icao === c.b.icao ? `<span class="chase-badges">you're here</span>`
      : (() => {
        const x = deployDep(home, c, undefined, now);   // filled in by fillDeployLaunch once the hourly forecast is read
        return `<span class="chase-badges deploy-dep" data-base="${c.b.icao}" title="${escHtml(x.title)}">${escHtml(x.text)}</span>`;
      })();
    // how the hourly forecast has verified at the fields this row names
    const named = [...new Set([...(fl ? [fl.icao] : []), ...c.contrib.slice(0, 3).map((x) => x.a.icao)])];
    const bars = named.map((icao) => deployBarTag(icao, `${icao} `)).filter(Boolean);
    // LED approach lights at the same fields (only those that have any)
    const leds = named.map((icao) => deployLedNote(icao, true)).filter(Boolean);
    return `
    <tr data-icao="${c.b.icao}"${ringBase?.icao === c.b.icao ? ' style="background:#16202b"' : ""}>
      <td class="num">${i + 1}</td>
      <td><b>${c.b.icao}</b> ${c.b.name.length > 18 ? c.b.name.slice(0, 17) + "…" : c.b.name}${placeTxt(c.b) ? ` <span class="chase-badges">· ${placeTxt(c.b)}</span>` : ""}<br>
        <span class="chase-badges" title="destination fields with flight time from ${c.b.icao} (still-air at ${chasePrefs.speed} kt)">go to: ${c.contrib.slice(0, 3).map((x) => `${x.a.icao} in ${eteStr(x.nm / chasePrefs.speed)}`).join(" · ")}</span>
        ${expired ? "" : `<br><span class="chase-badges deploy-fl" title="${escHtml(flTitle)}">${escHtml(flText)}</span>`}
        ${bars.length ? `<br><span class="chase-badges deploy-bars">hourly forecast check: ${bars.join(" · ")}</span>` : ""}
        ${leds.length ? `<br><span class="chase-badges deploy-led">${leds.join(" · ")}</span>` : ""}</td>
      <td class="num" title="expected chaseable fog hours at fields within ${radius} nm over the ${deployWindow === 1 ? "plan's first day" : `${deployWindow} days of the plan`}, counting only hours reachable before the fog lifts"><b>${c.s.toFixed(0)}h</b><br>${dep}</td>
    </tr>`;
  }).join("");

  // GO / MARGINAL / SCRAP verdict for the selected (or top) base: per day, the
  // best in-reach field's chance of at least one hour below CAT I; a day
  // counts when that chance is 50% or more
  const vb = (ringBase && top.find((c) => c.b.icao === ringBase!.icao)) || top[0];
  let verdictHtml = "";
  let launchNoteFor: { icao: string; word: string } | null = null; // one-day verdict only
  const hasP = !!vb && vb.contrib.some((x) => Array.isArray(deployData?.airports?.[x.a.icao]?.p));
  if (vb && deployData && hasP && !expired) {
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
    const WORD_TITLE = "GO at a 50% or higher chance, MARGINAL at 25% or higher, SCRAP below that. The chance is for at least one hour below CAT I that day at the best field in reach. A number from an area where days like it ran high on their check is held back by that many points before those bars are applied.";
    if (deployWindow === 1) {
      // one-day board: one day, one sentence — WHEELS UP below carries the rest.
      // Under the sentence go the two numbers that were each checked against
      // observations on their own (the peak hour and the expected hours), then
      // what the chance rests on.
      const k = picks[0];
      // the day as the chance speaks for it; a day with no chance is named without the forecast's last hour
      const noP0 = deployPart(0);
      const iso0 = deployDayIso(0);
      const dayPhrase = (k.icao ? deployChanceWords(0, k.icao, now)
        : iso0 ? dayWords(iso0, now, { from: noP0.from }, undefined, deployPlain(now)) : null)?.phrase ?? "on the plan's first day";
      let word: string = wordOf(k.wp);
      let color = word === "GO" ? "#7fd49a" : word === "MARGINAL" ? "#ffb347" : "#ff5a4d";
      let head = "";
      const lines: string[] = [];
      const note = (html: string, o: { color?: string; title?: string; cls?: string } = {}) =>
        `<div class="note${o.cls ? ` ${o.cls}` : ""}" style="margin-top:5px${o.color ? `;color:${o.color}` : ""}"${o.title ? ` title="${escHtml(o.title)}"` : ""}>${html}</div>`;
      // "Below CAT I at KVIS, Tue 24 from 07Z: peak hourly chance 65% · expected
      // hours 5.4": a part-day sum says which hours it is a sum over
      const hourlyFacts = (icao: string, pk: number | null, eh: number | null, tail = "") => {
        const bits: string[] = [];
        if (pk != null) bits.push(`peak hourly chance <b>${pk}%</b>`);
        if (eh != null) bits.push(`expected hours <b>${eh.toFixed(1)}</b>`);
        if (!bits.length) return "";
        const w = deployDayWords(0, icao, now);
        const title = `From the hourly forecast: ${pk != null ? "the highest chance in any single hour, and " : ""}the hourly chances added up${w?.part ? `, over the hours ${w.part} only` : " over the day"}.`;
        return `<div class="deploy-facts" style="margin-top:6px;font-size:11.5px;color:var(--ink)" title="${escHtml(title)}">Below CAT I at ${icao}${w ? `, ${w.short}${w.part ? ` ${w.part}` : ""}` : ""}: ${bits.join(" · ")}${tail}</div>`;
      };
      const barLine = (icao: string) => {
        const tag = deployBarTag(icao, `Hourly forecast at ${icao} against the public bar: `);
        return tag ? note(tag, { cls: "deploy-bar" }) : "";
      };
      if (!k.basis && k.noP) {
        // A forecast day with no day-level chance: the hourly numbers, why
        // there is no chance, and no word. SCRAP from a blank would be a guess.
        word = "NO VERDICT"; color = GREY;
        head = `no day-level chance ${dayPhrase} at the fields in reach of ${baseTxt}`;
        if (k.hourly) {
          lines.push(hourlyFacts(k.hourly.icao, k.hourly.pk, k.hourly.eh, ". It has the highest peak hour in reach."));
          lines.push(barLine(k.hourly.icao));
        }
        lines.push(note(escHtml(`${deployNoChanceWhy(0)} So there is no GO, MARGINAL or SCRAP for that day.${k.hourly ? " The numbers above are from the hourly forecast." : ""}`), { color: AMBER, cls: "deploy-why" }));
      } else if (!k.basis) {
        head = `no field in reach of ${baseTxt} has a fog window long enough to reach ${dayPhrase}`;
      } else {
        const pct = deployPct(k.p);
        // the color belongs to a checked forecast; on any other basis the
        // word stays (same bars) but grey, so green alone never reads as a call
        if (!deployChecked(k.basis)) color = GREY;
        head = `${icaoPlace(k.icao)}: ${k.basis === "bound" ? "up to " : ""}${pct}% chance of at least one hour below CAT I ${dayPhrase}. Best field in reach of ${baseTxt}.`;
        // SCRAP is not said over ANOTHER field in reach whose area map is not held
        // up to its peak hour, when that peak hour alone would earn a word. Such a
        // field can never be the best of the day (its chance stops at 22-23%), so
        // base KNLC read a red "SCRAP - KSUU: 27%" with KVIS 11 minutes away at a
        // 56% peak hour, a 39% record and a launch plan (gate review, 2026-11-25).
        // The word is taken off where the "Also in reach" line is printed, below.
        const capped = word === "SCRAP" && k.peak && k.peak.icao !== k.icao && wordOf(k.peak.pk / 100) !== "SCRAP" ? k.peak : null;
        if (k.status === "carried" && k.from) {
          // A chance this cycle did not make: say which cycle it is from and
          // how old that is. Where the newer forecast has risen above it, say
          // so and take the colour off the word: it is older than the numbers
          // beside it, and it is never raised on the peak hour alone.
          const fromMs = Date.parse(k.from);
          const risen = k.pk != null && !!deployData.meta?.days?.["1"]?.chance?.carried
            ?.some((x) => x.cycle === k.from && x.below_peak?.includes(k.icao));
          lines.push(note(
            `This day chance is from ${escHtml(cycleWords(k.from, now))}, <span data-age-of="${fromMs}">${ageWords(now - fromMs)}</span> old. ` +
            escHtml(`The newer forecast gave none: ${lowerFirst(deployNoChanceWhy(0))}`) +
            (risen ? ` <b>The forecast has risen since: peak hourly chance now ${k.pk}%.</b>` : ""),
            { color: risen ? AMBER : "", cls: "deploy-carried" }));
          if (risen) color = GREY;
        }
        if (k.basis === "cal" || k.basis === "area" || k.basis === "bound") lines.push(hourlyFacts(k.icao, k.pk, k.eh));
        let why = "", whyTitle = "", whyColor = "";
        switch (k.basis) {
          case "cal":
          case "area": {
            // the same line for both: what the check behind this number found
            // for this field. "area" is the case where it found a miss.
            const c = daycalCheck(0, k.icao, k.p, k.pk);
            if (c.ehNote) lines.push(note(escHtml(c.ehNote), { color: AMBER, cls: "deploy-ehnote" }));
            lines.push(barLine(k.icao));
            why = c.text;
            whyTitle = c.title;
            if (k.basis === "area") {
              whyColor = AMBER;
              // The word is never stronger than this line: a number that ran
              // high there is read from what is left after the miss.
              if (c.hold != null && !capped) {
                why = why.replace(/ Expect the real chance to be lower\.$/, "") + (wordOf(k.p) !== word
                  ? ` Read as ${word}, not ${wordOf(k.p)}.`
                  : ` Allowing for that it is still ${k.wp >= 0.5 ? "50% or more" : "25% or more"}.`);
              }
            } else if (c.hold != null && !capped && wordOf(k.p) !== word) {
              // a calibrated number whose own line says it ran high, by enough to cross a bar
              why += ` Read as ${word}, not ${wordOf(k.p)}.`;
            }
            lines.push(note(escHtml(why), { color: whyColor, title: whyTitle, cls: "deploy-why" }));
            if (c.peakNote) {
              // The peak hour says more than the area's map can. The line says
              // what days with such a peak hour did there, and where the peak
              // hour alone would earn a stronger word there is NO word: the
              // Central Valley map stops at 22-23%, just under MARGINAL, so
              // "SCRAP - KVIS: 22%" stood over "peak hourly chance 65%", 5.4
              // expected hours and a launch plan to the same field, on days
              // that fogged about one time in three.
              lines.push(note(escHtml(c.peakNote), { color: AMBER, title: c.peakTitle, cls: "deploy-peaknote" }));
              if (k.pk != null && wordOf(k.pk / 100) !== word) {
                word = "NO VERDICT"; color = GREY;
                head = `${icaoPlace(k.icao)}: day chance ${pct}% and peak hourly chance ${k.pk}% ${dayPhrase}. They do not agree, so no GO, MARGINAL or SCRAP is given. Best field in reach of ${baseTxt}.`;
              }
            }
            why = "";
            break;
          }
          case "bound":
            why = (dc ? "Upper bound, not a calibrated chance: this build did not correct that day's number."
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
        if (why) lines.push(note(escHtml(why), { color: whyColor, title: whyTitle, cls: "deploy-why" }));
        if (k.peak && k.peak.icao !== k.icao) {
          const off = !!capped && word === "SCRAP";   // not when the picked field's own peak hour already took the word off
          lines.push(note(`Also in reach: ${icaoPlace(k.peak.icao)}, day chance ${deployPct(k.peak.p)}%. ${escHtml(k.peak.note)}${off ? " So no GO, MARGINAL or SCRAP is given." : ""}`,
            { title: k.peak.title, cls: "deploy-also", color: off ? AMBER : "" }));
          if (off) { word = "NO VERDICT"; color = GREY; }
        }
      }
      verdictHtml = `<div id="deploy-verdict" data-word="${word}" data-basis="${k.basis ?? "none"}" data-status="${k.basis ? k.status : "none"}" style="${BOX}">
        <b style="color:${color};${WORD}">${word}</b>
        <span style="color:var(--ink)" title="${WORD_TITLE}"> — ${head}</span>
        ${lines.filter(Boolean).join("")}
        <div class="note" id="deploy-launch-note" style="margin-top:5px">reading the hourly forecast for a launch time…</div>
      </div>`;
      // filled in below once the hourly forecast is in: this line used to be a
      // fixed "launch plan below", printed over "no launch window" and over
      // an empty section alike
      launchNoteFor = { icao: k.icao || k.hourly?.icao || "", word };
    } else {
      const need = deployWindow >= 14 ? 3 : 2;
      // a day counts on the chance its word would be read from (DeployPick.wp)
      const counted = picks.filter((k) => k.basis && k.wp >= 0.5);
      const likely = counted.length;
      const wordOfDays = (n: number) => (n >= need ? "GO" : n >= 1 ? "MARGINAL" : "SCRAP");
      const word = wordOfDays(likely);
      // same rule as the one-day word: it keeps its color only when the days
      // that are checked forecasts would earn it on their own
      const unchecked = counted.filter((k) => !deployChecked(k.basis));
      const leans = wordOfDays(likely - unchecked.length) !== word;
      const color = leans ? GREY : word === "GO" ? "#7fd49a" : word === "MARGINAL" ? "#ffb347" : "#ff5a4d";
      const leansLine = leans
        ? `<div class="note" style="margin-top:5px;color:${AMBER}">${unchecked.length} of the ${likely} ${likely === 1 ? "day" : "days"} counted ${unchecked.length === 1 ? "is" : "are"} not a checked forecast: ${[...new Set(unchecked.map((k) => basisWords(k.basis!)))].join(", ")}.</div>`
        : "";
      const CELL = "flex:1;height:15px;border-radius:3px;box-sizing:border-box";
      const swatch = (b: DeployBasis | null) =>
        `<span style="display:inline-block;width:11px;height:9px;border-radius:2px;box-sizing:border-box;vertical-align:-1px;margin-right:5px;${deployCellStyle(b, 0.75)}"></span>`;
      // one legend line per basis among the cells drawn, with the dates it
      // covers, so the note cannot claim a tier this base's cells do not show
      const order: DeployBasis[] = ["cal", "area", "bound", "fit", "nbe", "cpc", "climo", "other"];
      const testYear = /\b(20\d\d)\b/.exec(dc?.test ?? "")?.[1];
      const carriedWords = (b: DeployBasis) => {
        const from = [...new Set(picks.flatMap((k) => (k.basis === b && k.from ? [k.from] : [])))];
        return from.length ? `, day chance from ${from.map((f) => cycleWords(f, now)).join(", ")}` : "";
      };
      const legend = order.map((b) => {
        const days = picks.flatMap((k, i) => (k.basis === b ? [i + 1] : []));
        if (!days.length) return { first: 99, html: "" };
        // plain words on the line; the skill numbers ("+9.7%" reads as "9.7
        // points more likely", and "holdout" is not a pilot's word) in the hover
        let words = basisWords(b), title = "";
        if (b === "fit") { words += ` (${fitWords})`; title = fitTitle; }
        if (b === "cal") {
          // a field on its area's own map is checked for that area, not on the pooled sample
          const own = [...new Set(picks.flatMap((k, i) => {
            if (k.basis !== "cal") return [];
            const c = daycalCheck(i, k.icao, k.p);
            return [c.own && c.area ? c.area.name : ""];
          }))];
          if (own.length && own.every(Boolean)) words += ` (checked for ${joinAnd(own)} fields)`;
          else if (testYear) words += ` (checked on ${testYear}, a year it was not tuned on)`;
          words += carriedWords(b);
          title = days.map((d) => {
            const k = picks[d - 1];
            const r = deployReceipt(k.icao, d - 1).receipt;
            const iso = deployDayIso(d - 1);
            return r?.skill_pct != null ? `${iso ? dayShort(iso) : `Day ${d}`}: ${signedPct(r.skill_pct)} against climatology on its check (Brier skill${r.sample ? `; ${r.sample}` : ""}).` : "";
          }).filter(Boolean).join(" ");
        }
        if (b === "area") {
          // name the areas the cells stand on, with how far off each ran
          words = [...new Set(picks.flatMap((k, i) => (k.basis === "area" ? [areaMissWords(k.icao, i, k.p)] : [])))].join("; ") + carriedWords(b);
        }
        return { first: days[0], html: `<div class="note" style="margin-top:2px${b === "bound" || b === "other" || b === "area" ? `;color:${AMBER}` : ""}"${title ? ` title="${escHtml(title)}"` : ""}>${swatch(b)}${daySpans(days)}: ${escHtml(words)}</div>` };
      });
      // A forecast day (day 1 or 2) without a day-level chance: its own line,
      // with why and the hourly numbers, and it is not counted either way.
      picks.forEach((k, i) => {
        if (k.basis || !k.noP || i > 1) return;
        const hw = k.hourly ? hourlyWords(k.hourly.icao, i, k.hourly.pk, k.hourly.eh) : "";
        legend.push({ first: i + 1, html: `<div class="note deploy-nochance" style="margin-top:2px">${swatch(null)}${escHtml(`${daySpans([i + 1])}: no day-level chance from this build, so it is not counted. ${deployNoChanceWhy(i)}${hw ? ` Highest peak hour in reach: ${hw}.` : ""}`)}</div>` });
      });
      for (const missing of [true, false]) {
        const days = picks.flatMap((k, i) => (!k.basis && !!k.noP === missing && !(missing && i < 2) ? [i + 1] : []));
        if (days.length) legend.push({ first: days[0], html: `<div class="note" style="margin-top:2px">${swatch(null)}${daySpans(days)}: ${missing
          ? "no day-level chance in this plan for the fields in reach" : "no field in reach has a fog window long enough to reach"}</div>` });
      }
      // in day order, the way the cells read left to right
      legend.sort((x, y) => x.first - y.first);
      // Under each cell: its weekday and day of month, from the plan's own
      // date, and in amber the hour a part-covered day starts or stops at.
      const partial = picks.some((_, i) => !!deployDayWords(i, undefined, now)?.part);
      const dayLabels = picks.map((_, i) => {
        const iso = deployDayIso(i);
        const [wd, dom] = iso ? dayShort(iso).split(" ") : [String(i + 1), ""];
        const part = deployPart(i);
        const marks = [part.from != null ? `from ${zHour(part.from)}` : "", part.through != null ? `through ${zHour(part.through)}` : ""].filter(Boolean)
          .map((m) => `<div class="deploy-partmark" style="white-space:nowrap;color:${AMBER}">${m}</div>`).join("");
        return `<div style="flex:1;min-width:0;text-align:center;font-size:8.5px;line-height:1.25;color:var(--ink-dim)"${iso ? ` title="${dayLong(iso)}"` : ""}><div>${wd}</div><div>${dom}</div>${marks}</div>`;
      }).join("");
      verdictHtml = `<div id="deploy-verdict" data-word="${word}" style="${BOX}">
        <b style="color:${color};${WORD}">${word}</b>
        <span style="color:var(--ink)" title="${WORD_TITLE}"> — ${likely} of ${deployWindow} days ${likely === 1 ? "has" : "have"} a field with a 50% or higher chance of at least one hour below CAT I, in reach of ${baseTxt}</span>
        ${leansLine}
        <div style="display:flex;gap:2px;margin-top:9px">${picks.map((k, i) =>
          `<div data-kind="${k.basis ?? "none"}" title="${escHtml(deployCellTitle(k, i))}" style="${CELL};${deployCellStyle(k.basis, k.p)}"></div>`).join("")}</div>
        <div id="deploy-daynums" style="display:flex;gap:2px;margin-top:3px">${dayLabels}</div>
        <div class="note" style="margin-top:6px">Each cell is one day of the plan, with its date under it: the chance of at least one hour below CAT I at the best field in reach. Brighter = higher chance.${partial ? " An amber hour under a date: the hourly forecast covers that day only from or through that hour (UTC)." : ""}</div>
        ${legend.map((l) => l.html).join("")}
      </div>`;
    }
  }

  // ---- above the verdict: which days, how old, and what this build lacked ----
  const w0 = deployDayWords(0, undefined, now);
  const lastIso = deployDayIso(deployWindow - 1);
  const windowWords = deployWindow === 1
    ? (w0?.phrase ?? "on the plan's first day")
    : w0 && lastIso ? `${w0.long} to ${dayLong(lastIso)}` : `over ${deployWindow} days`;
  const bannerInner = deployData ? deployBannerInner(now) : "";
  const BANNER = "margin:10px 0 0;padding:10px 12px;border-radius:6px;font-size:11.5px;line-height:1.55;";
  const bannerHtml = bannerInner
    ? `<div id="deploy-plan-banner" role="status" data-level="${fresh.level}" style="${BANNER}${fresh.level === "expired"
      ? "border:1px solid #7a352e;border-left:3px solid #ff5a4d;background:#401a16;color:#ffd9d4"
      : "border:1px solid #7a5a2e;border-left:3px solid #ffb347;background:#33270f;color:#ffe2b0"}">${bannerInner}</div>`
    : "";
  // what the build itself says it lacked (meta.gaps), and days 3-14 kept from
  // an earlier build or left on a weaker basis (meta.advisory)
  const gapLines: string[] = [];
  if (!expired) {
    for (const g of deployData?.meta?.gaps ?? []) {
      if (g?.code !== "advisory" && typeof g?.text === "string") gapLines.push(escHtml(g.text));
    }
    const adv = deployData?.meta?.advisory;
    const SRC: Record<string, string> = { nbe: "NBM extended guidance", gefs: "the GEFS ensemble", cpc: "the CPC outlooks" };
    const missing = joinAnd((adv?.missing ?? []).map((m) => SRC[m] ?? m));
    const advBuilt = adv?.built ? Date.parse(adv.built) : NaN;
    if (adv?.status === "reused" && Number.isFinite(advBuilt)) {
      gapLines.push(`Some days after the first two keep the numbers of the build of ${escHtml(clockWords(advBuilt, now))}, <span data-age-of="${advBuilt}">${ageWords(now - advBuilt)}</span> old: this build could not read ${escHtml(missing || "one of its sources")}.`);
    } else if (adv?.status === "degraded") {
      gapLines.push(`This build could not read ${escHtml(missing || "one of its sources")}. Later days stand on a weaker basis in this build: the ? beside the settings says which.`);
    }
  }
  // The plan's days flip at 10:00Z. For hours after that the fog morning in
  // progress (10Z to 16Z holds over half of a western field's fog hours) is in
  // none of the plan's days: said, with where to look instead.
  const day1Iso = deployDayIso(0);
  const utcNow = new Date(now);
  const morningOut = !expired && !!deployData && !!day1Iso && day1Iso > utcNow.toISOString().slice(0, 10) && utcNow.getUTCHours() < 18;
  // a part-covered day in the totals: said, not summed in silence
  const partNotes: string[] = [];
  for (let d = 0; d < Math.min(2, deployWindow); d++) {
    const part = deployPart(d), iso = deployDayIso(d);
    if (part.from == null && part.through == null) continue;
    const hrs = [part.from != null ? `from ${zHour(part.from)}` : "", part.through != null ? `through ${zHour(part.through)}` : ""].filter(Boolean).join(" ");
    const whyPart = part.from != null && part.through == null ? "its earlier hours were behind this build"
      : part.from == null ? "the hourly forecast does not reach further yet" : "";
    const name = iso ? dayShort(iso) : `day ${d + 1}`;
    // with ehc in the plan the rest of a part-covered day 2 is counted from climatology, and said so
    partNotes.push(part.from == null && deployDayFilled(d)
      ? `${name} counts the hourly forecast ${hrs} and climatology for the rest of that day`
      : `${name} counts only its hours ${hrs}${whyPart ? ` (${whyPart})` : ""}`);
  }

  panelContent.innerHTML = `
    <h2>Deploy planner</h2>
    <p class="sub">where to base for the fog — expected chaseable hours ${windowWords}${deployData ? "" : " — <b style='color:#ff9a9a'>deploy data unavailable</b>"}</p>
    ${deployData ? `<p class="note" id="deploy-age" style="margin-top:4px" title="This page asks for a new plan when the panel opens and every 15 minutes while it stays open. A plan is built every hour.">${escHtml(planAgeLine(deployData.meta, now))}</p>` : ""}
    <p class="note" id="deploy-fetch" role="status" style="margin-top:2px;color:${AMBER}"${deployFail ? "" : " hidden"}>${deployFail ? escHtml(refreshFailWords(deployFail, deployNextTryAt, deployData?.meta, now)) : ""}</p>
    ${bannerHtml}
    ${gapLines.map((g) => `<p class="note deploy-gap" style="margin-top:6px;color:${AMBER}">${g}</p>`).join("")}
    ${morningOut ? `<p class="note" id="deploy-morning" style="margin-top:6px">This plan starts ${escHtml(dayLong(day1Iso!))}. Fog today, ${escHtml(dayLong(utcNow.toISOString().slice(0, 10)))} (UTC), is not in it: for today use NEXT 48 H below and the CHASE board.</p>` : ""}
    ${verdictHtml}
    <div class="chase-setup">
      ${([1, 7, 14] as const).map((w) => `<button class="equip-chip${deployWindow === w ? " on" : ""}" data-win="${w}">${w === 1 ? escHtml(deployDayLabel(0, now)) : `next ${w} days`}</button>`).join("")}
      <label>cruise <input id="deploy-speed" type="number" min="60" max="600" step="10" value="${chasePrefs.speed}"/> kt</label>
      <label>max ETE <select id="deploy-ete">
        ${[1, 1.5, 2, 2.5, 3, 4].map((h) => `<option value="${h}"${h === chasePrefs.maxEteH ? " selected" : ""}>${eteStr(h)}</option>`).join("")}
      </select></label>
      <span style="color:var(--ink-dim)">= ${radius} nm reach · shared with <a href="#chase" id="deploy-tochase">CHASE</a></span>
      <span class="qtip" id="deploy-tiers-tip" title="${escHtml(`Airports must pass your saved CHASE filters. Chaseable = below CAT I.${tierSpans.length ? ` What each day's number rests on in this plan: ${deployTierText(tierSpans)} The CPC outlook covers the US only.` : ""}`)}">?</span>
      ${ledSelectHtml("deploy-led")}
    </div>
    ${(() => {
      const age = baseAge();
      if (expired) return ""; // an expired plan shows no departure times to speak of
      if (!home) return `<p class="note" style="margin-top:6px;color:#ffb347">no CHASE base set — deploy-by departure times need one. <a href="#chase" id="deploy-basechange">set base</a></p>`;
      return `<p class="note" style="margin-top:6px" title="every deploy-by time in the table is the latest departure from this base, in this base's time zone">deploy-by times from your base <b>${home.icao}</b> · <span${age?.stale ? ' style="color:#ffb347"' : ""}>${age?.label ?? ""}${age?.stale ? " — still current?" : ""}</span> <a href="#chase" id="deploy-basechange">change</a>${age?.stale ? ` · <a href="#" id="deploy-baseok">✓ still ${home.icao}</a>` : ""}</p>`;
    })()}
    ${ledChoiceNote("deploy-led-note", "deploy-led-ok", "that meet your CHASE filters", chaseQualifying("all"), chaseQualifying(ledChoice()))}
    <div class="stratum-h"><b style="color:#e8b96a">BEST BASES</b><span class="n">${top.length}</span><span>ranked by expected chaseable hours within reach</span><span class="qtip" title="Click a base row (or its orange dot on the map) to ring its reach and expand the launch plan; click a blue dot for that airport's deep-dive. Dot size = expected chaseable hours. The plan is rebuilt every hour, and every build logs its numbers so each tier can be scored against what happened.">?</span></div>
    ${rows && expired ? `<p class="note" id="deploy-expired-note" style="margin-top:8px;color:${AMBER}">This table is from the expired plan. Its hours and its ranking are old, and it shows no departure times.</p>` : ""}
    ${rows && !expired && partNotes.length ? `<p class="note" id="deploy-partial" style="margin-top:8px;color:${AMBER}">In the hours below, ${escHtml(partNotes.join("; "))}.</p>` : ""}
    ${rows ? `<table class="rank-table"${expired ? ' style="opacity:.6"' : ""}><thead><tr><th>#</th><th>base · top nearby fog</th><th class="num" title="expected chaseable fog hours summed across ALL reachable fields over the window — an inventory of opportunities, not one aircraft's itinerary. Each field counts only the fog-window hours remaining after still-air transit (launch-at-onset + 30 min margin), so a distant field whose fog lifts before arrival counts near zero. A day the hourly forecast covers in part counts the forecast for the hours it covers and, where the plan carries it, climatology for the rest (the note above the table says which). Contributor times are ETE from the base.">chaseable hrs</th></tr></thead><tbody>${rows}</tbody></table>`
      // "no expected fog" is only said when the planner looked. Without its
      // data, or without the airport list the bases are ranked from, it did
      // not look, and the same sentence used to be printed for both.
      : !deployData ? `<p class="note" id="deploy-nodata" style="color:#ffb347">The plan did not load, so no base can be ranked. This page keeps trying: the line under the title says when.</p>`
      : !chaseData ? `<p class="note" id="deploy-nodata" style="color:#ffb347">The airport list did not load, so no base can be ranked. Open DEPLOY again to retry, or reload the page.</p>`
      : `<p class="note">No expected fog within range of any base under the current filters — widen the CHASE filters or the window.</p>`}
    ${rows ? ledLegendHtml("deploy-led-legend") : ""}
    <div id="deploy-48"${hold48 ? ` style="min-height:${hold48}px"` : ""}></div>`;
  if (keep) panel.scrollTop = scroll;

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
  // the LED choice is the one CHASE saves: changing it here changes it there
  panelContent.querySelector("#deploy-led")?.addEventListener("change", (e) => {
    chasePrefs.led = ledChoiceOf((e.target as HTMLSelectElement).value);
    chasePrefs.ledSetAt = Date.now();
    saveChasePrefs(); renderDeploy(ringBase);
  });
  panelContent.querySelector("#deploy-led-ok")?.addEventListener("click", (e) => {
    e.preventDefault(); chasePrefs.ledSetAt = Date.now(); saveChasePrefs(); renderDeploy(ringBase, true);
  });
  panelContent.querySelectorAll("tr[data-icao]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const b = state.airports.find((x) => x.icao === (tr as HTMLElement).dataset.icao)!;
      renderDeploy(b);
      map.flyTo({ center: [b.lon, b.lat], zoom: 5.2, duration: 1400 });
    }));
  wireDeployTips(panelContent);

  // selected base: expand its top fields into next-48h character lines
  // (live calibrated curve -> severity, window, burn-off; persistence medians)
  const detailBase = ringBase ?? (deployWindow === 1 ? top[0]?.b : undefined);
  deployView = {
    top, home, expired, beyond48, noteFor: launchNoteFor,
    sel: (detailBase && top.find((c) => c.b.icao === detailBase.icao)) || null,
  };
  fillDeployLaunch();

  deployApplyMap(targets, top, ringBase);
  if (!keep && !ringBase && top.length && !map.isMoving()) {
    const b = new maplibregl.LngLatBounds();
    top.forEach((c) => b.extend([c.b.lon, c.b.lat]));
    targets.slice(0, 40).forEach((t) => b.extend([t.a.lon, t.a.lat]));
    map.fitBounds(b, { padding: { top: 90, bottom: 110, left: 60, right: 490 }, duration: 1400, maxZoom: 6 });
  }
}

// Everything on the panel that comes from the HOURLY forecast (/api/forecast):
// each base row's departure line, the verdict's launch line, WHEELS UP and the
// 48 h lines of the selected base. It runs after every draw and again from the
// tick when a time on screen has passed or the plan is 15 minutes old, and
// replaces only those parts, so the rest of the panel stays where it is.
async function fillDeployLaunch() {
  deployLaunchAt = Date.now();
  const v = deployView;
  // an expired plan gets no departure times: its ranking is not a plan
  // the height a redraw held the 48 h section at is let go once there is content, or nothing to wait for
  const release = () => { const held = panelContent.querySelector<HTMLElement>("#deploy-48"); if (held) held.style.minHeight = ""; };
  if (!deployOpen || !v || v.expired || !v.top.length) { release(); return; }
  const seq = ++deployFillSeq;
  if (v.sel && persistenceTable === undefined) {
    persistenceTable = null;
    try { persistenceTable = await (await fetch("/data/persistence.json")).json(); } catch {}
  }
  const fc = await getForecast();
  if (seq !== deployFillSeq || !deployOpen || deployView !== v) return;
  const now = Date.now();
  if (v.home) {
    panelContent.querySelectorAll<HTMLElement>(".deploy-dep[data-base]").forEach((dep) => {
      const c = v.top.find((x) => x.b.icao === dep.dataset.base);
      if (!c) return;
      const x = deployDep(v.home!, c, fc, now);
      dep.textContent = x.text;
      dep.title = x.title;
      if (x.until != null) dep.dataset.until = String(x.until); else delete dep.dataset.until;
    });
  }
  const el = panelContent.querySelector<HTMLElement>("#deploy-48");
  const noteEl = panelContent.querySelector<HTMLElement>("#deploy-launch-note");
  const sel = v.sel;
  if (!el || !sel) { release(); return; }
  if (!fc) {
    // said, not skipped: the section used to stay empty under a verdict
    // that pointed at it
    const msg = "Hourly forecast unavailable. No wheels-up time can be computed.";
    if (noteEl) { noteEl.textContent = msg; noteEl.style.color = "#ffb347"; }
    el.innerHTML = `<div class="stratum-h"><b style="color:#9fd8ff">NEXT 48 H FROM ${sel.b.icao}</b><span></span><span>hourly forecast at the top fields in reach</span></div>
      <p class="note" style="color:#ffb347">${msg} This page tries again within 15 minutes.</p>`;
    release();
    return;
  }
  // The hourly forecast shows its own age. "The forecast refreshes hourly" was
  // printed here as a fixed promise through three weeks in which it did not.
  const cycMs = Date.parse(fc.meta.cycle);
  const genMs = Date.parse(fc.meta.generated ?? "");
  // an issuance reaches the site about 85 minutes after its cycle hour
  const fcOld = Number.isFinite(genMs) ? now - genMs > HOURLY_OLD_MS : now - cycMs > HOURLY_OLD_MS + 90 * 60_000;
  const fcDead = !(now - cycMs < HOURLY_DEAD_MS);
  const fcLine = `<div class="note" id="deploy-fc-age" style="margin-top:2px${fcOld ? ";color:#ffb347" : ""}">Hourly forecast in use: ${escHtml(cycleWords(fc.meta.cycle, now))}${Number.isFinite(genMs)
    ? `, issued ${escHtml(clockWords(genMs, now))} · <span data-age-of="${genMs}">${ageWords(now - genMs)}</span> old.`
    : ` (it does not say when it was issued; that hour was <span data-age-of="${cycMs}">${ageWords(now - cycMs)}</span> ago).`}${fcOld ? " One is issued every hour: this one is behind." : ""}</div>`;
  // the one-day answer: the best reachable window and the wheels-up time
  // backed off its opening by the flight out
  const best = deployLaunch(sel, fc, now);
  let launch = "";
  if (best) {
    // The time is printed in the zone of the base the pilot launches from,
    // with the zone named and the same instant in Zulu. It used to be the
    // destination's clock with no label: a base in Nevada launching into
    // Oregon read an hour off.
    const tz = sel.b.tz;
    const late = departure(best.wheelsMs, now).passed;
    // once the wheels-up time is behind the clock it is not printed as a time
    // to act on; the tick asks again when even leaving now cannot make the window
    const until = late ? best.openMs - best.ete * 3600e3 : best.wheelsMs;
    launch = `<div id="deploy-wheels" data-until="${until}" data-late="${late}" style="background:#111823;border:1px solid #7a5a2e;border-radius:10px;padding:12px 14px;margin:10px 0">
      <b style="color:#e8b96a;letter-spacing:.05em">${late ? "WHEELS UP NOW" : `WHEELS UP ~${zoneClock(best.wheelsMs, tz)} (${zClock(best.wheelsMs)})`}</b>
      <span style="color:var(--ink)"> → ${icaoPlace(best.icao)} (${eteStr(best.ete)} out) · fog (&lt;1 mi) ${escHtml(launchChanceWords(best.fog, best.openMs, tz))}</span>
      <div class="note" style="margin-top:4px">${late
        ? `The planned wheels-up time, ${zoneClock(best.wheelsMs, tz)}, has passed. The first hour of ${best.icao}'s fog window that can still be reached is ${zoneClock(best.openMs, tz)} (${zClock(best.openMs)}): leaving now still makes it, without the 15 min buffer.`
        : `The time is ${sel.b.icao}'s, the base you launch from. It gets you to ${best.icao} for the first hour of its fog window that can still be reached, ${zoneClock(best.openMs, tz)} (${zClock(best.openMs)}), with a 15 min buffer.`} Recheck CHASE before engine start.</div>
      ${fcLine}
    </div>`;
  } else {
    launch = `<p class="note" id="deploy-nolaunch">${fcDead
      ? `No launch time from ${sel.b.icao}: the hourly forecast on hand is too old to plan a departure from.`
      : `No launch window in the next 30 h from ${sel.b.icao}: no reachable field's <b>hourly</b> chance of fog under 1 mile clears 40% at an hour that can still be reached. The verdict above is for the day as a whole.`}</p>${fcLine}`;
  }
  // The verdict box says which of the two the pilot will find below.
  // A day-level GO at KVIS sat over "no launch window" at every base
  // on 2026-11-24: its best hour for fog under 1 mile was 36%, and
  // the launch plan needs 40%.
  if (noteEl && v.noteFor) {
    noteEl.style.color = "";
    if (best) {
      // The launch plan takes the first window that can still be reached, and
      // after 10Z that is often the morning already under way, a day before
      // the one the verdict is for. With no date on either line, "WHEELS UP
      // ~02:45" under "GO ... tomorrow" read as tomorrow's 02:45.
      const day0 = deployDayIso(0), openIso = new Date(best.openMs).toISOString().slice(0, 10);
      const other = day0 && openIso !== day0 ? ` That is ${openIso < day0 ? "before" : "after"} the day this verdict is for.` : "";
      // The verdict is the day-level chance below CAT I; the launch plan is the
      // hourly chance of fog under 1 mile. When they name different fields the
      // line says so: "GO - KBTM" over "Launch plan below: KPDT" had nothing between them.
      // No number here: it was the field's peak in the whole forecast ("in reach
      // (92%)") and that hour can be behind the clock. WHEELS UP has the chance.
      const differs = v.noteFor.icao && v.noteFor.icao !== best.icao
        ? ` It is for ${best.icao}, not ${v.noteFor.icao}: the launch plan goes by the hourly chance of fog under 1 mile, the verdict by the day chance below CAT I.` : "";
      noteEl.textContent = `Launch plan below: ${best.icao}, be there ${zoneClock(best.openMs, sel.b.tz)}.${differs}${other}`;
      if (other) noteEl.style.color = "#ffb347";
    } else if (fcDead) {
      noteEl.textContent = "No launch time: the hourly forecast on hand is too old.";
      noteEl.style.color = "#ffb347";
    } else {
      const f = v.noteFor.icao ? fc.airports?.[v.noteFor.icao] : null;
      const peak = f ? Math.max(...f.p.map((r: number[]) => r[0])) : null;
      noteEl.textContent = "No launch time yet: " + (peak != null && Number.isFinite(peak) && peak < 40
        ? `the best hour at ${v.noteFor.icao} is ${peak}% for fog under 1 mile, and the launch plan needs 40%.`
        : "no field in reach clears the launch plan's 40% bar (fog under 1 mile) at an hour that can still be reached.");
      if (v.noteFor.word === "GO" || v.noteFor.word === "MARGINAL") noteEl.style.color = "#ffb347";
    }
  }
  const lines = sel.contrib.slice(0, 5).map((x) => {
    const ch = fogCharacter(x.a, fc);
    const bar = deployBarTag(x.a.icao, "hourly forecast check: ");
    const led = deployLedNote(x.a.icao, false); // LED approach lights at this field, if it has any
    return ch ? `<tr data-icao="${x.a.icao}" class="fpeek"><td><b>${x.a.icao}</b> <span class="chase-badges">${eteStr(x.nm / chasePrefs.speed)} out</span><br><span class="chase-badges">${x.a.name.length > 16 ? x.a.name.slice(0, 15) + "…" : x.a.name}${placeTxt(x.a) ? ` · ${placeTxt(x.a)}` : ""}</span>${bar ? `<br><span class="chase-badges">${bar}</span>` : ""}${led ? `<br><span class="chase-badges deploy-led">${led}</span>` : ""}</td><td>${ch}</td></tr>
      <tr class="fdetail" data-for="${x.a.icao}"${deployPeeks.has(x.a.icao) ? "" : " hidden"}><td colspan="2">${miniStrip(x.a, fc)}
        <p class="note" style="margin-top:4px"><a href="/fog/${x.a.icao.toLowerCase()}/" target="_blank" rel="noopener">forecast page ↗</a> · <a href="#" class="fdive" data-icao="${x.a.icao}">full 10-yr analysis</a></p></td></tr>` : "";
  }).filter(Boolean).join("");
  el.innerHTML = `
    <div class="stratum-h"><b style="color:#9fd8ff">NEXT 48 H FROM ${sel.b.icao}</b><span></span><span>hourly forecast at the top fields in reach</span></div>
    ${launch}
    ${lines ? `<table class="rank-table"><tbody>${lines}</tbody></table>` : `<p class="note">no live forecast coverage at this base's top fields.</p>`}
    <p class="note">Window = hours with fog probability ≥40% of its peak; severity pills show the strongest threshold with ≥25% chance. Times in these lines are each field's local time.${v.beyond48 ? ` Beyond 48 h in this plan: ${escHtml(v.beyond48)}` : ""}</p>`;
  release();
  // row click = expand the peek in place (no navigation, planner state kept)
  el.querySelectorAll("tr.fpeek").forEach((tr) =>
    tr.addEventListener("click", () => {
      const icao = (tr as HTMLElement).dataset.icao!;
      const det = el.querySelector(`tr.fdetail[data-for="${icao}"]`) as HTMLElement | null;
      if (!det) return;
      det.hidden = !det.hidden;
      // remembered, so the strip stays open when the launch plan is worked out again
      if (det.hidden) deployPeeks.delete(icao); else deployPeeks.add(icao);
    }));
  el.querySelectorAll("a.fdive").forEach((a2) =>
    a2.addEventListener("click", (ev) => {
      ev.preventDefault();
      openAirport((a2 as HTMLElement).dataset.icao!);
    }));
  wireDeployTips(el);
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
  // the LED tick as it stands: clicks counts ring clicks handled, so a test can
  // show that toggling the tick did not stack a second handler
  led: () => ({ on: ledOn, clicks: ledClicks, airports: ledData()?.airports.length ?? null, choice: ledChoice() }),
};
