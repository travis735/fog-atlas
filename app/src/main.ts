import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import * as Plot from "@observablehq/plot";
import "./style.css";

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const MONTHS_S = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

interface Airport {
  icao: string; name: string; lat: number; lon: number; country: string; tz: string;
  catIls: string; catConfidence: string;
  efvsHoursPerYear: number; belowHoursPerYear: number;
  causes: Record<string, number>;
  grid: number[][]; // [month][hour] sub-CAT-I %
}

const state = { mon: 0, hr: 6, playing: false, airports: [] as Airport[] };

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const monEl = $<HTMLInputElement>("#mon");
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

function scrubIdx() { return state.mon * 24 + state.hr; }
function pctExpr(): any { return ["at", scrubIdx(), ["get", "g"]]; }

const GLOW_COLOR: any = (e: any) => ["interpolate", ["linear"], e,
  0, "#5d7589", 2, "#5e93bb", 8, "#7fc0e8", 25, "#c4eaff", 55, "#ffffff"];
const GLOW_OPACITY: any = (e: any) => ["interpolate", ["linear"], e,
  0, 0.05, 3, 0.45, 15, 0.75, 40, 0.95];

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
        g: a.grid.flat(),
      },
    })),
  };
  map.addSource("airports", { data: fc as any, type: "geojson" });

  const base: any = ["max", ["+", 3, ["*", 0.5, ["sqrt", ["get", "annual"]]]], 3.5];
  // grow dots as the map zooms in, or they get lost at street level.
  // NB: maplibre only allows ["zoom"] in a TOP-LEVEL interpolate, so the
  // per-layer multiplier must live inside the stops, not wrap the result.
  const radius = (mult: number): any => ["interpolate", ["linear"], ["zoom"],
    3, ["*", base, mult], 6, ["*", base, 1.8 * mult], 10, ["*", base, 3.2 * mult]];

  map.addLayer({
    id: "glow",
    type: "circle",
    source: "airports",
    paint: {
      "circle-radius": radius(2.6),
      "circle-blur": 1.4,
      "circle-color": GLOW_COLOR(pctExpr()),
      "circle-opacity": GLOW_OPACITY(pctExpr()),
    },
  });
  map.addLayer({
    id: "core",
    type: "circle",
    source: "airports",
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
    id: "sheen",
    type: "circle",
    source: "airports",
    paint: {
      "circle-radius": radius(0.42),
      "circle-blur": 0.7,
      "circle-color": "#ffffff",
      "circle-opacity": 0.65,
      "circle-translate": [-2, -2],
    },
  });

  const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: "tip" });
  map.on("mousemove", "core", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const f = e.features?.[0];
    if (!f) return;
    const g = JSON.parse(JSON.stringify(f.properties.g));
    const arr = typeof g === "string" ? JSON.parse(g) : g;
    const pct = arr[scrubIdx()];
    tip.setLngLat(e.lngLat)
      .setHTML(`<b>${f.properties.icao}</b> ${f.properties.name}<br>${MONTHS_S[state.mon]} ${String(state.hr).padStart(2, "0")}:00 local — <b>${pct}%</b> sub-CAT-I`)
      .addTo(map);
  });
  map.on("mouseleave", "core", () => { map.getCanvas().style.cursor = ""; tip.remove(); });
  map.on("click", "core", (e) => {
    const icao = e.features?.[0]?.properties.icao;
    if (icao) openAirport(icao);
  });

  if (location.hash.length > 1) openAirport(location.hash.slice(1).toUpperCase());
});

function applyScrub() {
  readout.textContent = `${MONTHS[state.mon]} · ${String(state.hr).padStart(2, "0")}:00`;
  if (!map.getLayer("glow")) return;
  map.setPaintProperty("glow", "circle-color", GLOW_COLOR(pctExpr()));
  map.setPaintProperty("glow", "circle-opacity", GLOW_OPACITY(pctExpr()));
  map.setPaintProperty("core", "circle-color", GLOW_COLOR(pctExpr()));
}

monEl.addEventListener("input", () => { state.mon = +monEl.value; applyScrub(); });
hrEl.addEventListener("input", () => { state.hr = +hrEl.value; applyScrub(); });

let timer: ReturnType<typeof setInterval> | undefined;
$("#play").addEventListener("click", () => {
  state.playing = !state.playing;
  $("#play").innerHTML = state.playing ? "&#10074;&#10074;" : "&#9654;";
  if (state.playing) {
    timer = setInterval(() => {
      state.hr = (state.hr + 1) % 24;
      if (state.hr === 0) state.mon = (state.mon + 1) % 12;
      hrEl.value = String(state.hr);
      monEl.value = String(state.mon);
      applyScrub();
    }, 300);
  } else clearInterval(timer);
});

$("#close").addEventListener("click", () => {
  panel.hidden = true;
  history.replaceState(null, "", location.pathname);
});

async function openAirport(icao: string) {
  const a = state.airports.find((x) => x.icao === icao);
  if (!a) return;
  const detail = await (await fetch(`/data/detail/${icao}.json`)).json();
  history.replaceState(null, "", `#${icao}`);

  const cat3 = a.catIls === "CATIII" || a.catIls === "CATII";
  const causes = Object.entries(a.causes)
    .filter(([k]) => !["none", "other"].includes(k))
    .sort((x, y) => y[1] - x[1]);
  const causeLabel: Record<string, string> = { FG: "fog", BR: "mist", "HZ/FU": "haze / smoke", SN: "snow" };

  panelContent.innerHTML = `
    <h2>${a.icao}</h2>
    <p class="sub">${a.name} · ${a.country}</p>
    <div>
      <span class="badge ${cat3 ? "cat3" : "cat1"}">${a.catIls}</span>
      <span class="badge conf">${a.catConfidence}</span>
      ${!cat3 ? `<span class="badge cat1">high EFVS value — no CAT II/III fallback</span>` : ""}
    </div>
    <div class="stats">
      <div class="stat efvs"><div class="v">${Math.round(a.efvsHoursPerYear)}</div><div class="k">EFVS-recoverable hrs / yr (300–800 m)</div></div>
      <div class="stat"><div class="v">${Math.round(a.belowHoursPerYear)}</div><div class="k">below 300 m hrs / yr</div></div>
      <div class="stat"><div class="v">${detail.coveragePct}%</div><div class="k">archive coverage</div></div>
    </div>
    <h3 id="heatmap-title">When it closes — % of hours below CAT I, by month × local hour</h3>
    <div id="heatmap"></div>
    <h3>Cause of low visibility</h3>
    <div id="causes"></div>
    <p class="note">Prevailing visibility is a climatological proxy for RVR — read as relative risk, not operating minima. Hours are local (${a.tz}). 2016–2025 routine METARs. <a href="https://github.com/travis735/fog-atlas/blob/main/METHODOLOGY.md" target="_blank" rel="noopener">Full methodology</a>.</p>
  `;

  const cells: { hr: number; mon: string; pct: number }[] = [];
  for (let m = 0; m < 12; m++)
    for (let h = 0; h < 24; h++)
      cells.push({ hr: h, mon: MONTHS_S[m], pct: (detail.efvsGrid[m][h] ?? 0) + (detail.belowGrid[m][h] ?? 0) });

  // scale the ramp to this airport's own peak — a fixed worldwide domain
  // crushes everything but Delhi into the dark end
  const peak = Math.max(5, ...cells.map((c) => c.pct));
  $("#heatmap-title").textContent =
    `When it closes — % of hours below CAT I, by month × local hour (scale 0–${Math.ceil(peak)}%)`;
  const heat = Plot.plot({
    width: 386,
    height: 230,
    marginLeft: 34,
    style: { background: "transparent", color: "#5d6b78", fontSize: "9px" },
    x: { label: "local hour", ticks: [0, 6, 12, 18, 23] },
    y: { label: null, domain: MONTHS_S },
    color: {
      type: "sqrt",
      domain: [0, peak],
      range: ["#1d2a38", "#f2faff"],
      clamp: true,
    },
    marks: [Plot.cell(cells, { x: "hr", y: "mon", fill: "pct", inset: 0.4, tip: false })],
  });
  $("#heatmap").replaceChildren(heat);

  $("#causes").innerHTML = causes.map(([k, v]) => `
    <div style="display:flex;align-items:center;gap:10px;margin:5px 0;font-size:11px">
      <span style="width:74px;color:var(--ink-dim)">${causeLabel[k] ?? k}</span>
      <div style="flex:1;height:6px;background:#141b23;border-radius:3px;overflow:hidden">
        <div style="width:${v}%;height:100%;background:linear-gradient(90deg,#3e6b8a,#9fd8ff)"></div>
      </div>
      <span style="width:40px;text-align:right">${v}%</span>
    </div>`).join("");

  panel.hidden = false;
}

applyScrub();

// debug API
(window as any).__fogatlas = {
  state,
  map,
  setScrub(mon: number, hr: number) { state.mon = mon; state.hr = hr; monEl.value = String(mon); hrEl.value = String(hr); applyScrub(); },
  openAirport,
};
