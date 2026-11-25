// Live-observation feed: ONE parser and ONE health check for NOAA AWC's
// metars.cache.csv (served to the browser by /api/now), shared by the CHASE
// board and the map's NOW layer.
//
// Why this file exists (readiness review, 2026-11-24): both readers took
// whatever /api/now answered, skipped every line that was not a quoted CSV
// row, and treated the empty result as "no fog". A 502 body, an HTML error
// page and a dropped connection all printed "nothing below CAT I within range
// right now" under a constant "obs ≤3 min old feed". Both readers also took
// columns by POSITION (visibility = 9th field after the raw text) although the
// file ships a header row: the same pattern as the FAA fixed-width incident
// and the NBM header incident earlier that month. One column inserted upstream
// and every airport reads clear, with every volume check still green.
//
// Rules:
//   - columns are found by NAME in the header row; a missing one is a failed
//     feed, never a guess
//   - a read is either healthy (every gate passed) or failed with ONE named
//     reason, and nothing downstream may print an all-clear from a failed read
//   - what is "current" is decided again at display time (feedView), so a
//     page whose timers stopped cannot keep calling old observations current
//
// Pure module: no DOM, no fetch, no clock of its own (callers pass `now`), and
// only erasable TypeScript, so `node app/test/test_feed.mjs` imports it as is.

export interface LiveOb {
  obsMs: number; vis: number | null; ceil: number | null;
  rvr: string | null; rvrFt: number | null; wx: string | null;
  sub: boolean; below: boolean;
  tempC: number | null; dewpC: number | null; wspd: number | null;
}

/** Every column the readers use, by header name. Two more copies of this list
 *  exist and must change with it: FEED_COLS in .github/workflows/watchdog.yml
 *  (check 9) and AWC_TRUTH_COLS in forecast/engine.py (a subset). */
export const FEED_SCALAR_COLUMNS = [
  "raw_text", "station_id", "observation_time", "temp_c", "dewpoint_c",
  "wind_speed_kt", "visibility_statute_mi", "wx_string",
] as const;
/** Repeated once per cloud layer (four of each on 2026-11-24). */
export const FEED_LAYER_COLUMNS = ["sky_cover", "cloud_base_ft_agl"] as const;
export const FEED_REQUIRED_COLUMNS: readonly string[] = [...FEED_SCALAR_COLUMNS, ...FEED_LAYER_COLUMNS];

// A ceiling is a broken or overcast layer, or an obscured sky. AWC writes a
// vertical-visibility report ("VV001") as cover OVX with the height as its
// base, in the CSV and in the JSON API alike; "VV" is what the IEM archive
// calls the same thing. The JSON readers used to look only for "VV" and so
// saw no ceiling at all in exactly the reports that matter most in fog.
export const CEILING_COVERS: readonly string[] = ["BKN", "OVC", "OVX", "VV"];

export const FEED_POLL_MS = 180_000;        // healthy cadence; the edge keeps the upstream file for 180 s
export const FEED_RETRY_MS = 60_000;        // cadence while reads are failing (the banner quotes this number)
export const FEED_TIMEOUT_MS = 45_000;      // a hung request must become a failed read before the next retry
export const FEED_OVERDUE_MS = FEED_POLL_MS + 120_000; // no finished read for this long: the page itself stopped polling
export const FEED_DOWN_ALERT_MS = 6 * 60_000; // armed alerts say "feed down" once it has been down this long
// Gates, with the healthy values measured on 2026-11-24 (22:24Z capture):
export const FEED_MIN_EXPECTED_SHARE = 0.85; // 905 to 911 of 937 chase airports had a row (96 to 97%)
export const FEED_MIN_STATIONS = 3000;       // 5,146 stations; also the only coverage gate when chase.json is not loaded
export const FEED_MIN_VIS_FILL = 0.70;       // 92.6% of stations carried a visibility
export const FEED_MAX_NEWEST_AGE_MS = 20 * 60_000; // newest report was 3 min old; every 5-min bin held 12+ reports
// A report older than this is not "current" for one airport (the board files
// the field under NO CURRENT REPORT). Hourly METARs are 0 to 65 min old.
export const FEED_REPORT_MAX_AGE_MS = 75 * 60_000;
// The gate above looks at the newest report among all ~5,000 stations, and the
// small 20-minute AWOS stations keep that fresh by themselves: at 00:21Z on
// 2026-11-25 only 60 of the 994 reports under 20 min old were chase airports.
// A stall of the chase airports' own reports therefore passed every gate, with
// "IN FOG NOW 0 of 4" under a healthy feed line and no alert able to fire. So
// the airports the board depends on get their own gate: this share of them
// must have a report under FEED_REPORT_MAX_AGE_MS old. Healthy: 896 to 912 of
// 937 (95.6 to 97.3%) in three captures and four overnight hours, 2026-11-24.
export const FEED_MIN_CURRENT_SHARE = 0.70;
const FUTURE_SLACK_MS = 5 * 60_000;          // one station with a wrong clock must not make a stuck file look fresh

export type FeedReason = "http" | "network" | "response" | "header" | "coverage" | "visibility" | "stale";

/** The reason class in two or three words, for places with no room for a sentence (the NOW chip). */
export const FEED_REASON_LABEL: Record<FeedReason | "paused", string> = {
  http: "server error",
  network: "no connection",
  // an HTML page or an empty answer is not a changed format: the NOW chip
  // called both "format changed" until 2026-11-24
  response: "bad response",
  header: "format changed",
  coverage: "incomplete data",
  visibility: "no visibility data",
  stale: "not updating",
  paused: "page was asleep",
};

export interface FeedStats {
  stations: number;
  expectHave: number | null; expectTotal: number | null; // chase airports with a row, of how many
  expectCurrent: number | null; // chase airports whose report is under FEED_REPORT_MAX_AGE_MS old
  visFill: number;   // share of stations whose report carries a visibility
  newestMs: number;  // newest observation time in the file (0 = none usable)
  badRows: number;   // rows whose field count did not match the header
}

export type FeedRead =
  | { ok: true; obs: Map<string, LiveOb>; stats: FeedStats }
  | {
      ok: false; reason: FeedReason;
      /** One plain sentence for the banner. Empty for "stale": that sentence quotes a clock time and is built at display time. */
      detail: string;
      /** Set only for "stale": a complete, well-formed file whose reports are old. It may be shown as last-known, never as current. */
      obs: Map<string, LiveOb> | null;
      stats: FeedStats | null;
    };

const RVR_RE = /\bR(\d{2}[LRC]?)\/([MP]?)(\d{4})(?:V[MP]?\d{4})?FT/g;
const num = (s: string) => { const v = parseFloat(s); return Number.isNaN(v) ? null : v; };
const fmtN = (n: number) => n.toLocaleString("en-US");
const quoted = (names: string[]) => names.map((n) => `"${n}"`).join(", ");

/** RFC 4180 fields of one line. raw_text is quoted and can hold commas (4 of 5,146 rows on 2026-11-24). */
export function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  const n = line.length;
  let i = 0;
  for (;;) {
    if (line[i] === '"') {
      let s = "";
      i++;
      while (i < n) {
        if (line[i] === '"') {
          if (line[i + 1] === '"') { s += '"'; i += 2; continue; }
          i++;
          break;
        }
        s += line[i++];
      }
      out.push(s);
      while (i < n && line[i] !== ",") i++;
    } else {
      const j = line.indexOf(",", i);
      if (j < 0) { out.push(line.slice(i)); break; }
      out.push(line.slice(i, j));
      i = j;
    }
    if (i >= n) break;
    i++; // the comma
    if (i === n) { out.push(""); break; }
  }
  return out;
}

interface FeedColumns {
  width: number;
  raw: number; id: number; time: number; temp: number; dewp: number; wspd: number; vis: number; wx: number;
  layers: [number, number][]; // [sky_cover, cloud_base_ft_agl] index pairs
}

/** Find every column by name. Anything short of an exact, unambiguous match is a failure with a sentence saying what. */
export function resolveFeedColumns(headerLine: string): { cols: FeedColumns } | { problem: string; notATable?: true } {
  const names = splitCsvLine(headerLine.replace(/^\uFEFF/, "").replace(/\r$/, "")).map((s) => s.trim());
  const where = (name: string) => names.flatMap((h, i) => (h === name ? [i] : []));
  if (!FEED_REQUIRED_COLUMNS.some((c) => where(c).length)) {
    return { problem: "The feed did not return a table of weather reports.", notATable: true };
  }
  const missing = FEED_REQUIRED_COLUMNS.filter((c) => !where(c).length);
  if (missing.length) {
    return { problem: `The feed's layout changed: ${missing.length === 1 ? "column" : "columns"} ${quoted(missing)} ${missing.length === 1 ? "is" : "are"} missing. The board will not guess.` };
  }
  const twice = FEED_SCALAR_COLUMNS.filter((c) => where(c).length > 1);
  if (twice.length) {
    return { problem: `The feed's layout changed: ${twice.length === 1 ? "column" : "columns"} ${quoted([...twice])} ${twice.length === 1 ? "appears" : "appear"} more than once. The board will not guess.` };
  }
  const covers = where("sky_cover"), bases = where("cloud_base_ft_agl");
  const paired = covers.length === bases.length && covers.every((c, k) =>
    bases[k] > c && (k + 1 === covers.length || bases[k] < covers[k + 1]));
  if (!paired) {
    return { problem: `The feed's layout changed: the cloud layer columns ("sky_cover", "cloud_base_ft_agl") no longer pair up. The board will not guess.` };
  }
  const at = (name: string) => where(name)[0];
  return {
    cols: {
      width: names.length,
      raw: at("raw_text"), id: at("station_id"), time: at("observation_time"),
      temp: at("temp_c"), dewp: at("dewpoint_c"), wspd: at("wind_speed_kt"),
      vis: at("visibility_statute_mi"), wx: at("wx_string"),
      layers: covers.map((c, k) => [c, bases[k]] as [number, number]),
    },
  };
}

/** Lowest ceiling in a JSON METAR's `clouds` array (the /api/metar shape), or null. */
export function jsonCeil(clouds: unknown): number | null {
  let ceil: number | null = null;
  for (const c of Array.isArray(clouds) ? clouds : []) {
    if (c && CEILING_COVERS.includes(c.cover) && typeof c.base === "number" && !Number.isNaN(c.base)) {
      ceil = Math.min(ceil ?? Infinity, c.base);
    }
  }
  return ceil;
}

/** Visibility in statute miles from a JSON METAR's `visib` (a number, or a string such as "10+"), or null. */
export function jsonVis(v: unknown): number | null {
  if (v == null || v === "") return null;
  const x = typeof v === "string" ? (v.includes("+") ? 10 : parseFloat(v)) : typeof v === "number" ? v : NaN;
  return Number.isNaN(x) ? null : x;
}

export const subCat1 = (vis: number | null, ceil: number | null) =>
  (vis != null && vis < 0.5) || (ceil != null && ceil < 200);

/**
 * Parse one /api/now response and judge it. `expectIds` is the set of stations
 * the caller depends on (chase.json's airports); pass null when it is not loaded.
 */
export function readFeed(status: number, text: string, expectIds: readonly string[] | null, now: number): FeedRead {
  const fail = (reason: FeedReason, detail: string, stats: FeedStats | null = null, obs: Map<string, LiveOb> | null = null): FeedRead =>
    ({ ok: false, reason, detail, obs, stats });

  if (status < 200 || status > 299) {
    return fail("http", `The feed server answered with an error (HTTP ${status}).`);
  }
  const lines = text.split("\n");
  const first = lines.findIndex((l) => l.trim() !== "");
  if (first < 0) return fail("response", "The feed came back empty.");
  const res = resolveFeedColumns(lines[first]);
  if ("problem" in res) return fail(res.notATable ? "response" : "header", res.problem);
  const cols = res.cols;

  const obs = new Map<string, LiveOb>();
  let rows = 0, badRows = 0, badWidth = 0;
  for (let k = first + 1; k < lines.length; k++) {
    let line = lines[k];
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (line.trim() === "") continue;
    const f = splitCsvLine(line);
    if (f.length !== cols.width) { badRows++; badWidth ||= f.length; continue; }
    const icao = f[cols.id].trim();
    if (!icao) { badRows++; continue; }
    rows++;
    if (obs.has(icao)) continue; // the cache lists the newest report first per station
    const visRaw = f[cols.vis];
    const vis0 = visRaw ? (visRaw.includes("+") ? 10 : parseFloat(visRaw)) : null;
    const vis = vis0 != null && !Number.isNaN(vis0) ? vis0 : null;
    let ceil: number | null = null;
    for (const [c, b] of cols.layers) {
      if (CEILING_COVERS.includes(f[c])) {
        const base = parseFloat(f[b]);
        if (!Number.isNaN(base)) ceil = Math.min(ceil ?? Infinity, base);
      }
    }
    let rvr: string | null = null, rvrFt: number | null = null;
    for (const m of f[cols.raw].matchAll(RVR_RE)) {
      const ft = m[2] === "P" ? 6001 : parseInt(m[3], 10);
      if (rvrFt == null || ft < rvrFt) { rvrFt = ft; rvr = `${m[1]}/${m[2]}${m[3]}`; }
    }
    obs.set(icao, {
      obsMs: Date.parse(f[cols.time]) || 0,
      vis, ceil, rvr, rvrFt,
      // upstream text goes into the panel as HTML: keep only what a METAR weather group can contain
      wx: f[cols.wx].replace(/[^A-Za-z0-9+\- ]/g, "").slice(0, 40) || null,
      sub: subCat1(vis, ceil),
      below: vis != null && vis < 0.19,
      tempC: num(f[cols.temp]), dewpC: num(f[cols.dewp]), wspd: num(f[cols.wspd]),
    });
  }

  let visFilled = 0, newestMs = 0, anyTime = false;
  for (const ob of obs.values()) {
    if (ob.vis != null) visFilled++;
    if (ob.obsMs) anyTime = true;
    if (ob.obsMs <= now + FUTURE_SLACK_MS && ob.obsMs > newestMs) newestMs = ob.obsMs;
  }
  const expectHave = expectIds ? expectIds.reduce((n, id) => n + (obs.has(id) ? 1 : 0), 0) : null;
  const expectCurrent = expectIds ? expectIds.reduce((n, id) => {
    const t = obs.get(id)?.obsMs;
    return n + (t && now - t <= FEED_REPORT_MAX_AGE_MS ? 1 : 0);
  }, 0) : null;
  const stats: FeedStats = {
    stations: obs.size,
    expectHave, expectTotal: expectIds ? expectIds.length : null, expectCurrent,
    visFill: obs.size ? visFilled / obs.size : 0,
    newestMs, badRows,
  };

  // rows that do not line up with the header are the "column inserted in the
  // data but not in the header" case: reading them by name would be a guess
  if (badRows > rows) {
    return fail("header", badWidth
      ? `The feed's layout changed: rows have ${fmtN(badWidth)} fields but the header names ${fmtN(cols.width)}. The board will not guess.`
      : "The feed's layout changed: its rows carry no station name. The board will not guess.", stats);
  }
  // Coverage of the airports the board depends on, not just row count: the
  // file is sorted newest first, so a body cut to 70% kept 3,621 stations
  // (above the floor) but only 334 of 937 chase airports.
  if (expectIds && expectIds.length && expectHave! < FEED_MIN_EXPECTED_SHARE * expectIds.length) {
    return fail("coverage", `The feed is incomplete: ${fmtN(expectHave!)} of ${fmtN(expectIds.length)} chase airports have a report (at least ${fmtN(Math.ceil(FEED_MIN_EXPECTED_SHARE * expectIds.length))} needed).`, stats);
  }
  if (obs.size < FEED_MIN_STATIONS) {
    return fail("coverage", `The feed is incomplete: ${fmtN(obs.size)} stations reported (at least ${fmtN(FEED_MIN_STATIONS)} needed).`, stats);
  }
  if (stats.visFill < FEED_MIN_VIS_FILL) {
    return fail("visibility", `The feed is missing visibility: ${Math.round(stats.visFill * 100)}% of reports carry one (at least ${Math.round(FEED_MIN_VIS_FILL * 100)}% needed).`, stats);
  }
  if (!newestMs) {
    return fail("stale", anyTime
      ? "The feed's report times are ahead of this device's clock. Check the device clock."
      : "The feed's report times cannot be read.", stats);
  }
  if (now - newestMs > FEED_MAX_NEWEST_AGE_MS) return fail("stale", "", stats, obs);
  // the file as a whole is fresh; are the airports the board depends on?
  if (expectIds && expectIds.length && expectCurrent! < FEED_MIN_CURRENT_SHARE * expectIds.length) {
    return fail("stale", `The feed has stopped updating for most chase airports: ${fmtN(expectCurrent!)} of ${fmtN(expectIds.length)} have a report under ${FEED_REPORT_MAX_AGE_MS / 60_000} minutes old (at least ${fmtN(Math.ceil(FEED_MIN_CURRENT_SHARE * expectIds.length))} needed). Other stations are still reporting.`, stats, obs);
  }
  return { ok: true, obs, stats };
}

/** A fetch that threw (no connection, timeout, a body that broke off mid-stream) is a failed read with its own reason. */
export function feedThrown(e: unknown): FeedRead {
  const name = (e as { name?: string } | null)?.name;
  return {
    ok: false, reason: "network", obs: null, stats: null,
    detail: name === "TimeoutError" || name === "AbortError"
      ? `The feed server did not answer within ${FEED_TIMEOUT_MS / 1000} seconds.`
      : "This device could not reach the feed server. Check the connection.",
  };
}

// ---- what the board knows about the feed, across reads ----

export interface FeedTrack {
  reads: number;                  // reads finished since the board opened
  ok: boolean;                    // the last finished read passed every gate
  reason: FeedReason | null;
  detail: string;
  failNewestMs: number | null;    // for "stale": the newest report the feed offered
  failedSince: number | null;     // first failed read of the current run of failures
  lastTryAt: number | null;       // when the last read finished, good or bad
  lastGoodAt: number | null;      // when the last healthy read finished
  onHandNewestMs: number | null;  // newest report among the observations kept for display
  downAlerted: boolean;           // the one "feed down" alert of this outage has fired
}

export const feedTrackInit = (): FeedTrack => ({
  reads: 0, ok: false, reason: null, detail: "", failNewestMs: null,
  failedSince: null, lastTryAt: null, lastGoodAt: null, onHandNewestMs: null, downAlerted: false,
});

/**
 * Fold one finished read into the track.
 * `adopt` is the observation set the caller may now DISPLAY (null: keep what it
 * has). `runAlerts` is true only for a healthy read: a failed read must neither
 * check for new fog nor touch the alert baseline, or recovery re-fires an alert
 * for every airport that was already in fog. `recovered` is true once, on the
 * healthy read that ends an outage the pilot was alerted about.
 */
export function feedTrackRead(t: FeedTrack, read: FeedRead, now: number):
  { track: FeedTrack; adopt: Map<string, LiveOb> | null; runAlerts: boolean; recovered: boolean } {
  if (read.ok) {
    return {
      track: {
        reads: t.reads + 1, ok: true, reason: null, detail: "", failNewestMs: null,
        failedSince: null, lastTryAt: now, lastGoodAt: now,
        onHandNewestMs: read.stats.newestMs, downAlerted: false,
      },
      adopt: read.obs, runAlerts: true, recovered: t.downAlerted,
    };
  }
  // A stuck upstream still hands over a complete file. Showing it (marked not
  // current) beats an empty board, unless what is on hand is already newer.
  const adopt = read.reason === "stale" && read.obs && read.stats && read.stats.newestMs >= (t.onHandNewestMs ?? 0)
    ? read.obs : null;
  return {
    track: {
      ...t, reads: t.reads + 1, ok: false, reason: read.reason, detail: read.detail,
      failNewestMs: read.reason === "stale" && read.stats ? read.stats.newestMs || null : null,
      failedSince: t.failedSince ?? now, lastTryAt: now,
      onHandNewestMs: adopt ? read.stats!.newestMs : t.onHandNewestMs,
    },
    adopt, runAlerts: false, recovered: false,
  };
}

export type FeedView =
  | { phase: "waiting" }
  | { phase: "ok"; newestMs: number; checkedAt: number }
  | {
      phase: "down"; reason: FeedReason | "paused"; sinceMs: number;
      onHandNewestMs: number | null; lastTryAt: number | null;
      /** false when the last READ was healthy and the page only noticed late: a new read is due at once, not in 60 s */
      retrying: boolean;
    };

/**
 * What may be shown as current at time `now`. Asked again on every repaint: a
 * healthy read does not stay healthy by itself. If the page stopped polling
 * (tab frozen, device asleep) or the reports on hand aged past the limit, the
 * board is not current even though no read ever failed.
 */
export function feedView(t: FeedTrack, now: number): FeedView {
  if (!t.reads) return { phase: "waiting" };
  if (!t.ok) {
    return { phase: "down", reason: t.reason ?? "network", sinceMs: t.failedSince ?? now, onHandNewestMs: t.onHandNewestMs, lastTryAt: t.lastTryAt, retrying: true };
  }
  const newest = t.onHandNewestMs ?? 0, tried = t.lastTryAt ?? 0;
  if (now - tried > FEED_OVERDUE_MS) {
    return { phase: "down", reason: "paused", sinceMs: tried, onHandNewestMs: t.onHandNewestMs, lastTryAt: t.lastTryAt, retrying: false };
  }
  if (now - newest > FEED_MAX_NEWEST_AGE_MS) {
    return { phase: "down", reason: "stale", sinceMs: newest + FEED_MAX_NEWEST_AGE_MS, onHandNewestMs: t.onHandNewestMs, lastTryAt: t.lastTryAt, retrying: false };
  }
  return { phase: "ok", newestMs: newest, checkedAt: tried };
}

/** One alert per outage: only for real failed reads, only once they have lasted, only while alerts are armed. */
export const feedDownAlertDue = (t: FeedTrack, now: number, armed: boolean) =>
  armed && !t.ok && !t.downAlerted && t.failedSince != null && now - t.failedSince > FEED_DOWN_ALERT_MS;

// ---- copy ----

/** "under 1 min", "41 min", "3 h 2 min" */
export function ageText(ms: number): string {
  const m = Math.max(0, Math.floor(ms / 60_000));
  if (m < 1) return "under 1 min";
  if (m < 120) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

/** "14:32 local": the device's own clock, labelled so it is not read as Zulu. */
export function clockText(ms: number, timeZone?: string): string {
  return `${new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone })} local`;
}

/** "14:32 local (22:32Z)": for times a pilot will hold against a METAR. */
export function clockTextZ(ms: number, timeZone?: string): string {
  const d = new Date(ms);
  const z = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}Z`;
  return `${clockText(ms, timeZone)} (${z})`;
}

export const FEED_BANNER_TITLE = "LIVE FEED DOWN. THIS BOARD IS NOT CURRENT.";
// the page was asleep: the feed itself may be fine, so do not blame it
export const FEED_BANNER_TITLE_PAUSED = "THIS BOARD IS NOT CURRENT.";

/** Short phrases for the strata while the board is not current. */
export function feedDownWords(view: Extract<FeedView, { phase: "down" }>): { tag: string; when: string } {
  return view.reason === "paused"
    ? { tag: "this page was asleep", when: "until the feed is read again" }
    : { tag: "live feed down", when: "while the live feed is down" };
}

/** The banner, as plain text lines. Only call with a "down" view. */
export function feedBanner(
  t: FeedTrack, view: Extract<FeedView, { phase: "down" }>, now: number,
  o: { alertsArmed: boolean; timeZone?: string },
): { title: string; lines: string[] } {
  const tz = o.timeZone;
  let what: string;
  if (view.reason === "paused") {
    what = `This page has not checked the feed since ${clockText(view.sinceMs, tz)} (the tab or the device was asleep).`;
  } else if (view.reason === "stale" && !t.detail) {
    const newest = t.failNewestMs ?? view.onHandNewestMs;
    what = newest
      ? `The feed has stopped updating: its newest report is from ${clockTextZ(newest, tz)}.`
      : "The feed has stopped updating.";
  } else {
    what = t.detail;
  }
  const lines = [what, `Not current since ${clockTextZ(view.sinceMs, tz)}, ${ageText(now - view.sinceMs)} ago.`];
  lines.push(view.onHandNewestMs
    ? `Newest report received: ${clockTextZ(view.onHandNewestMs, tz)}, ${ageText(now - view.onHandNewestMs)} old.`
    : "No report has been received since this board opened.");
  lines.push(view.retrying
    ? `Retrying every ${FEED_RETRY_MS / 1000} seconds.${view.lastTryAt ? ` Last try ${clockText(view.lastTryAt, tz)}.` : ""}`
    : "Checking again now.");
  if (o.alertsArmed) lines.push("Fog alerts are not being checked.");
  return { title: view.reason === "paused" ? FEED_BANNER_TITLE_PAUSED : FEED_BANNER_TITLE, lines };
}

// ---- /api/metar batches ----

// AWC's data API answers at most 400 METARs per request, newest first, and
// drops the rest without saying so. Measured 2026-11-24: 150 stations x 3 h
// came back as exactly 400 entries with 130 of the 150 stations short of
// history; the same stations in three requests of 50 came back as 188 + 176 +
// 185 = 549 entries, complete (about 3.7 per station, up to 16 at a field
// sending SPECIs). 40 per request leaves room for 10 per station.
export const AWC_ROW_CAP = 400;
export const METAR_CHUNK = 40; // must not exceed MAX_IDS in app/functions/api/metar.ts

export function chunkIds(ids: readonly string[], size: number): string[][] {
  const out: string[][] = [];
  for (let i = 0; i < ids.length; i += Math.max(1, size)) out.push(ids.slice(i, i + Math.max(1, size)));
  return out;
}
