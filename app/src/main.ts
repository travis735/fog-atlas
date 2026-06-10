import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import * as Plot from "@observablehq/plot";
import "./style.css";

const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const MONTHS_S = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

interface Airport {
  icao: string; name: string; lat: number; lon: number; country: string; tz: string;
  catIls: string; catConfidence: string; size?: string; coveragePct?: number;
  reliability?: string;
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

  const base: any = ["max", ["+", 3, ["*", 0.5, ["sqrt", ["get", "annual"]]]], 3.5];
  // grow dots as the map zooms in, or they get lost at street level.
  // NB: maplibre only allows ["zoom"] in a TOP-LEVEL interpolate, so the
  // per-layer multiplier must live inside the stops, not wrap the result.
  const radius = (mult: number): any => ["interpolate", ["linear"], ["zoom"],
    3, ["*", base, mult], 6, ["*", base, 1.8 * mult], 10, ["*", base, 3.2 * mult]];

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
      const pct = arr[scrubIdx()];
      tip.setLngLat(e.lngLat)
        .setHTML(`<b>${f.properties.icao}</b> ${f.properties.name}<br>${MONTHS_S[state.mon]} ${String(state.hr).padStart(2, "0")}:00 local — <b>${pct}%</b> sub-CAT-I`)
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
  // a 6%-coverage or anomalous-reporting station can't support a frequency
  // claim — keep them out of the league table (still on the map and search)
  const rows = state.airports
    .filter((a) => (a.coveragePct ?? 100) >= 50 && (a.reliability ?? "ok") === "ok")
    .filter((a) => !rankCatIOnly || (a.catIls !== "CATIII" && a.catIls !== "CATII"))
    .sort((a, b) => b.efvsHoursPerYear - a.efvsHoursPerYear)
    .slice(0, 50);
  panelContent.innerHTML = `
    <h2>Where EFVS buys the most</h2>
    <p class="sub">Top airports by EFVS-recoverable hours (300–800 m) per year · ${state.airports.length} airports analyzed so far</p>
    <label class="rank-filter">
      <input id="rank-cat1" type="checkbox" ${rankCatIOnly ? "checked" : ""} />
      only airports without CAT II/III (no autoland fallback — the strongest EFVS case)
    </label>
    <table class="rank-table">
      <thead><tr><th>#</th><th>airport</th><th class="num">EFVS h/yr</th><th class="num">&lt;300 m</th><th>ILS</th></tr></thead>
      <tbody>
        ${rows.map((a, i) => `
          <tr data-icao="${a.icao}">
            <td class="num">${i + 1}</td>
            <td><b>${a.icao}</b> ${a.name.length > 26 ? a.name.slice(0, 25) + "…" : a.name} <em style="color:var(--ink-dim)">${a.country}</em></td>
            <td class="num efvs">${Math.round(a.efvsHoursPerYear)}</td>
            <td class="num">${Math.round(a.belowHoursPerYear)}</td>
            <td>${a.catIls === "CATIII" || a.catIls === "CATII" ? a.catIls.replace("CAT", "") : "I"}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    <p class="note">Ranked by hours below CAT I minima but within the EFVS-usable band. Airports still downloading are missing from this list until the next data pass.</p>
  `;
  panelContent.querySelector("#rank-cat1")!.addEventListener("change", (e) => {
    rankCatIOnly = (e.target as HTMLInputElement).checked;
    openRankings();
  });
  panelContent.querySelectorAll("tr[data-icao]").forEach((tr) =>
    tr.addEventListener("click", () => {
      const icao = (tr as HTMLElement).dataset.icao!;
      const a = state.airports.find((x) => x.icao === icao)!;
      map.flyTo({ center: [a.lon, a.lat], zoom: 6, duration: 1600 });
      openAirport(icao);
    }));
  panel.hidden = false;
}

$("#rankings-btn").addEventListener("click", openRankings);
$("#methodology").addEventListener("click", (e) => { e.preventDefault(); openMethodology(); });

// ---------- methodology ----------
function openMethodology() {
  history.replaceState(null, "", "#methodology");
  panelContent.innerHTML = `
    <h2>Methodology</h2>
    <p class="sub">what this map can and cannot tell you</p>
    <h3>Observation basis</h3>
    <p class="note" style="margin-top:6px">Routine hourly METARs 2016–2025 (Iowa Environmental Mesonet archive), one observation per hour — the last routine report in each UTC hour. SPECIs are deliberately excluded so frequencies are an unbiased sample of hours. "% of hours" uses hours with a valid visibility report as the denominator; per-airport archive coverage is shown in each deep-dive.</p>
    <h3>The three bands</h3>
    <p class="note" style="margin-top:6px"><b style="color:var(--ink)">Normal</b> ≥ ½ SM (~800 m) — typical CAT I visibility minima.<br/>
    <b style="color:var(--accent)">EFVS-recoverable</b> 300–800 m — below CAT I but within the range where EFVS operations (FAA 91.176) commonly remain workable.<br/>
    <b style="color:var(--ink)">Below all</b> &lt; 300 m — CAT III autoland territory.</p>
    <h3>Honest limitations</h3>
    <p class="note" style="margin-top:6px">METAR prevailing visibility is a proxy for RVR — RVR on a lit runway is often better, so the bands understate what's flyable; read them as a climatological index, not operating minima. Thresholds are global constants, not per-runway minima. CAT II/III flags are authoritative for the US (FAA CIFP), hand-curated internationally, and "assumed CAT I" elsewhere — confidence is shown per airport. The cause chart folds BR (mist) into fog: BR officially means vis ≥ 800 m, so BR on a sub-CAT-I observation is conservatively-coded fog.</p>
    <h3>Why CAT II/III matters</h3>
    <p class="note" style="margin-top:6px">At a CAT III airport, suitably equipped airliners already land in fog. EFVS value concentrates where low visibility is frequent <i>and</i> CAT II/III is absent — use the rankings filter to see exactly that intersection.</p>
    <p class="note">Full methodology with sources: <a href="https://github.com/travis735/fog-atlas/blob/main/METHODOLOGY.md" target="_blank" rel="noopener">github.com/travis735/fog-atlas</a></p>
  `;
  panel.hidden = false;
}

function applyScrub() {
  readout.textContent = `${MONTHS[state.mon]} · ${String(state.hr).padStart(2, "0")}:00`;
  if (!map.getLayer("glow")) return;
  map.setPaintProperty("fogheat", "heatmap-weight", heatWeight());
  for (const s of ["", "2", "3"]) {
    map.setPaintProperty("glow" + s, "circle-color", GLOW_COLOR(pctExpr()));
    map.setPaintProperty("glow" + s, "circle-opacity", glowOpacityZ());
    map.setPaintProperty("core" + s, "circle-color", GLOW_COLOR(pctExpr()));
  }
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
  // BR officially means vis >= 800m, so BR on a sub-CAT-I ob is fog that was
  // coded conservatively — present them as one family
  const merged: Record<string, number> = { ...a.causes };
  merged.FG = Math.round(((merged.FG ?? 0) + (merged.BR ?? 0)) * 10) / 10;
  delete merged.BR;
  const causes = Object.entries(merged)
    .filter(([k, v]) => !["none", "other"].includes(k) && v > 0)
    .sort((x, y) => y[1] - x[1]);
  const causeLabel: Record<string, string> = { FG: "fog / mist", "HZ/FU": "haze / smoke", SN: "snow" };

  panelContent.innerHTML = `
    <h2>${a.icao}</h2>
    <p class="sub">${a.name} · ${a.country}</p>
    <div>
      <span class="badge ${cat3 ? "cat3" : "cat1"}" title="${cat3
        ? "This airport has CAT II/III ILS — suitably equipped airliners can already land in low visibility"
        : "Best available approach assumed CAT I — visibility below ~800 m forces a missed approach without EFVS"}">${cat3 ? a.catIls.replace("CAT", "CAT ") : "CAT I"}</span>
      <span class="badge conf" title="${{
        curated: "Capability confirmed from AIPs / FAA publications",
        verify: "Capability confirmed from AIPs / FAA publications",
        assumed: "Not yet curated — assumed CAT I; treat the badge as provisional",
        unknown: "Capability not yet determined for this airport",
      }[a.catConfidence] ?? ""}">${{
        curated: "capability curated",
        verify: "capability curated",
        assumed: "capability assumed — not yet curated",
        unknown: "capability unknown",
      }[a.catConfidence] ?? a.catConfidence}</span>
      ${!cat3 ? `<span class="badge cat1">high EFVS value — no CAT II/III fallback</span>` : ""}
    </div>
    ${(a.reliability ?? "ok") !== "ok" ? `
    <div class="warn-banner">${a.reliability === "low-coverage"
      ? `⚠ Archive coverage is only ${a.coveragePct}% — too thin to support frequency claims. Numbers below are shown for completeness, not for decisions.`
      : `⚠ Reporting anomaly detected: this station's low-visibility observations are dominated by literal-zero values with no diurnal structure — the signature of an encoding artifact, not weather. Treat all frequencies here as unreliable.`}</div>` : ""}
    <div class="stats">
      <div class="stat efvs"><div class="v">${Math.round(a.efvsHoursPerYear)}</div><div class="k">EFVS-recoverable hrs / yr (300–800 m)</div></div>
      <div class="stat"><div class="v">${Math.round(a.belowHoursPerYear)}</div><div class="k">below 300 m hrs / yr</div></div>
      <div class="stat"><div class="v">${detail.coveragePct}%</div><div class="k">archive coverage</div></div>
    </div>
    <h3 id="heatmap-title">When it closes — % of hours below CAT I, by month × local hour</h3>
    <div id="heatmap"></div>
    <div id="heatmap-legend"></div>
    <h3>Cause of low visibility</h3>
    <div id="causes"></div>
    <p class="note">Prevailing visibility is a climatological proxy for RVR — read as relative risk, not operating minima. Hours are local (${a.tz}). 2016–2025 routine METARs. <a href="https://github.com/travis735/fog-atlas/blob/main/METHODOLOGY.md" target="_blank" rel="noopener">Full methodology</a>.</p>
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
  openRankings,
  openMethodology,
};
