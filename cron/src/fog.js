// Fog alerts for one private board, checked every 5 minutes.
//
// Why it exists (readiness review, 2026-11-24): the only fog alert was the
// CHASE panel's, which runs while that panel is open in a browser tab. With
// the laptop shut nothing told the pilot that a field on his board had gone
// below CAT I.
//
// What a tick does:
//   1. reads the board from KV (alerts:board). No board: one read, one log
//      line, done.
//   2. asks aviationweather.gov's JSON API for the latest report of ONLY those
//      stations, 40 ids per request. The 5,000-station cache file the site
//      reads is 1 MB and took 3.3 ms of CPU just to parse on a desktop core;
//      this Worker has 10 ms per invocation on the Free plan.
//   3. judges each station with the site's own rule, imported from
//      app/src/feed.ts so the phone and the board cannot disagree.
//   4. pushes when a station goes from clear to below CAT I.
//
// Rules carried over from the board (each one was a bug there first):
//   - a report over 75 minutes old is not current: it is neither clear nor
//     fog, and it changes nothing. The API hands back the latest report however
//     old it is (one was 7 days old in the 2026-11-25 capture).
//   - a tick that cannot read the reports runs NO fog check and leaves every
//     station's state alone, so recovery still alerts for a field that went
//     into fog meanwhile and does not re-alert for one that was in fog all
//     along.
//   - a station that joins the board already in fog does not alert. That
//     covers the first run after a board is stored and a lost state alike.
//   - silence must mean something: after about 15 minutes without readable
//     reports the phone is told once that alerts are paused, and once when
//     they are back.
//
// Privacy: the board is the owner's base and intent. It lives in KV only. No
// log line here carries a station id, the board's label or a message text;
// logs carry counts.

import {
  jsonVis, jsonCeil, subCat1, chunkIds, METAR_CHUNK,
  FEED_REPORT_MAX_AGE_MS, FEED_MIN_CURRENT_SHARE, FEED_MIN_VIS_FILL,
} from "../../app/src/feed.ts";
import { notify, notifyOff, sayOnce, holdingOff, KV_WRITE_GAP } from "./notify.js";

export const FOG_CRON = "*/5 * * * *";
export const BOARD_KEY = "alerts:board"; // written by the owner (README.md), read here
export const STATE_KEY = "alerts:state"; // written only here
export const TEST_KEY = "alerts:test";   // written by the owner to ask for one test message

// 40 ids per request -> 4 requests at most. Measured 2026-11-25 on a desktop
// core (test_alerts.mjs, 120 stations, 53 KB of real reports): 0.17 ms of CPU a
// tick once warm, 1.2 ms for the first tick of a new process.
export const MAX_BOARD_IDS = 160;
const ICAO = /^[A-Z0-9]{4}$/;
const AWC = "https://aviationweather.gov/api/data/metar";
// aviationweather.gov/data/api: "Set a custom user agent to prevent automated
// filtering inadvertently blocking valid traffic."
const UA = "fogatlas-cron (Cloudflare Worker; https://fogatlas.org)";
const FETCH_TIMEOUT_MS = 20_000;
// "about 15 minutes": the tick that finds reports unreadable for the 4th time
// in a row. 14, not 15, because ticks do not start on the exact second.
export const PAUSE_AFTER_MS = 14 * 60_000;
const DEFAULT_REPEAT_MIN = 60; // floor between two alerts for the same station
const DEFAULT_PRIORITY = 1;    // Pushover "high": passes its quiet hours
// The state is written only when something changes. One exception, so the
// daily heartbeat can tell a running check from a dead one: a time stamp
// every 55 minutes (27 writes a day of KV Free's 1,000).
export const STAMP_EVERY_MS = 55 * 60_000;
const MAX_LINES = 8;
const BOARD_URL = "https://fogatlas.org/#chase";

const pad2 = (n) => String(n).padStart(2, "0");
/** "03:15Z" */
export const zTime = (ms) => {
  const d = new Date(ms);
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}Z`;
};

// ---- the board ----

/** Parse alerts:board. A bare list of ids is a board too. Never throws. */
export function readBoard(raw) {
  let b;
  try { b = JSON.parse(raw); } catch { return { ok: false, why: "it is not valid JSON" }; }
  const o = b && typeof b === "object" && !Array.isArray(b) ? b : {};
  const list = Array.isArray(b) ? b : o.ids;
  if (!Array.isArray(list)) return { ok: false, why: 'it has no "ids" list' };
  const ids = [...new Set(list.map((x) => (typeof x === "string" ? x.trim().toUpperCase() : "")))];
  const bad = ids.filter((id) => !ICAO.test(id)).length;
  if (bad) return { ok: false, why: `${bad} of its ${ids.length} station ids are not four letters or digits` };
  if (ids.length > MAX_BOARD_IDS) {
    return { ok: false, why: `it lists ${ids.length} stations and the limit is ${MAX_BOARD_IDS}` };
  }
  const made = typeof o.made === "string" ? Date.parse(o.made) : NaN;
  const repeatMin = Number.isFinite(o.repeatMin) && o.repeatMin >= 0 ? Math.min(o.repeatMin, 1440) : DEFAULT_REPEAT_MIN;
  return {
    ok: true,
    ids,
    label: typeof o.label === "string" ? o.label.slice(0, 60) : "",
    madeMs: Number.isNaN(made) ? null : made,
    eteMin: o.eteMin && typeof o.eteMin === "object" ? o.eteMin : {},
    priority: [-2, -1, 0, 1, 2].includes(o.priority) ? o.priority : DEFAULT_PRIORITY,
    repeatMs: repeatMin * 60_000,
  };
}

// ---- the state ----
// { v: 1,
//   st:   { KAAA: "c" | "f" | "u" }  last judgement that said something:
//                                    clear, below CAT I, or nothing said yet
//   al:   { KAAA: ms }               when this station last alerted
//   down: null | { since: ms, kind, told, ok }   reports unreadable since;
//                                    ok = healthy ticks in a row since then
//   paused: ms                       when PAUSED was last pushed (3 h quiet)
//   bad:  null | "why"               the stored board cannot be used (told once)
//   bid:  "made|n|hash"              which board st and al belong to (boardId)
//   refused: null | { since, last }  Pushover refused a message; last try
//   ran:  ms                         a tick got as far as the board (hourly stamp)
//   at, cur, fog:                    at that time: stations with a current
//                                    report, stations below CAT I }
const freshState = () => ({ v: 1, st: {}, al: {}, down: null, paused: 0, bad: null, bid: "", refused: null, ran: 0, at: 0, cur: 0, fog: 0 });

/** Parse alerts:state. Anything unreadable is a fresh state: every station is then a newcomer and nothing alerts. */
export function readState(raw) {
  if (!raw) return freshState();
  try {
    const s = JSON.parse(raw);
    if (s?.v !== 1 || !s.st || typeof s.st !== "object") return freshState();
    return { ...freshState(), ...s, al: s.al && typeof s.al === "object" ? s.al : {} };
  } catch {
    return freshState();
  }
}

// ---- one report ----

// The board's RVR reader (RVR_RE in app/src/feed.ts, which does not export
// it). A copy; test_alerts.mjs holds it to the board's own answer on real
// reports.
const RVR_RE = /\bR(\d{2}[LRC]?)\/([MP]?)(\d{4})(?:V[MP]?\d{4})?FT/g;

/** The lowest RVR in a raw METAR as the board prints it ("29R/1200"), or null. */
export function lowestRvr(rawOb) {
  let rvr = null, rvrFt = null;
  for (const m of String(rawOb ?? "").matchAll(RVR_RE)) {
    const ft = m[2] === "P" ? 6001 : parseInt(m[3], 10);
    if (rvrFt == null || ft < rvrFt) { rvrFt = ft; rvr = `${m[1]}/${m[2]}${m[3]}`; }
  }
  return rvr;
}

/**
 * Judge one JSON METAR at time `now`: "f" below CAT I, "c" not below, "u" the
 * report does not say. The same three steps as the airport panel's JSON reader
 * in app/src/main.ts: over 75 minutes old is no verdict; no visibility and no
 * ceiling under 200 ft is no verdict; otherwise the board's subCat1.
 */
export function judge(o, now) {
  const obsMs = typeof o?.obsTime === "number" ? o.obsTime * 1000 : 0;
  const vis = jsonVis(o?.visib), ceil = jsonCeil(o?.clouds);
  const sub = subCat1(vis, ceil);
  const current = obsMs > 0 && now - obsMs <= FEED_REPORT_MAX_AGE_MS;
  return { s: !current ? "u" : sub ? "f" : vis != null ? "c" : "u", current, vis, ceil, obsMs };
}

/**
 * "KAAA 03:15Z: 0.25 SM, ceiling 100 ft, R29R/1200 FT, FG. ETE 0:32."
 * The visibility is written as AWC wrote it ("0.25", "1.75", "10+"), which is
 * what the airport panel prints; the RVR is in the board's own form.
 */
export function fogLine(id, o, j, eteMin) {
  const asWritten = typeof o.visib === "number" ? String(o.visib)
    : typeof o.visib === "string" && /^[0-9.+/ ]{1,8}$/.test(o.visib) ? o.visib : String(j.vis);
  const bits = [
    j.vis != null ? `${asWritten} SM` : "visibility not reported",
    j.ceil != null ? `ceiling ${j.ceil} ft` : null,
  ];
  const rvr = lowestRvr(o.rawOb);
  if (rvr) bits.push(`R${rvr} FT`);
  // upstream text: keep only what a METAR weather group can contain (as feed.ts does)
  const wx = typeof o.wxString === "string" ? o.wxString.replace(/[^A-Za-z0-9+\- ]/g, "").slice(0, 40) : "";
  if (wx) bits.push(wx);
  const ete = Number.isFinite(eteMin) && eteMin >= 0
    ? ` ETE ${Math.floor(eteMin / 60)}:${pad2(Math.round(eteMin) % 60)}.` : "";
  return `${id} ${zTime(j.obsMs)}: ${bits.filter(Boolean).join(", ")}.${ete}`;
}

// ---- reading the reports ----

/**
 * The latest report of every board station, or one named reason why not.
 * Fields are read by the names AWC ships (icaoId, obsTime, visib, clouds):
 * a report without a station name or a time is counted, never guessed at.
 */
async function fetchReports(ids) {
  const byId = new Map();
  const want = new Set(ids);
  let badRows = 0, rows = 0;
  for (const chunk of chunkIds(ids, METAR_CHUNK)) {
    let res;
    try {
      res = await fetch(`${AWC}?ids=${chunk.join(",")}&format=json`, {
        headers: { "User-Agent": UA, Accept: "application/json" },
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
    } catch {
      return { ok: false, kind: "network", detail: "aviationweather.gov could not be reached." };
    }
    // 204: "Request is valid however there is no data to be returned"
    if (res.status === 204) { await res.body?.cancel(); continue; }
    if (res.status !== 200) {
      await res.body?.cancel();
      return { ok: false, kind: "http", detail: `aviationweather.gov answered with an error (HTTP ${res.status}).` };
    }
    let arr;
    try {
      // bounded upstream: at most 400 reports per request, about 450 bytes each
      arr = JSON.parse(await res.text());
    } catch {
      return { ok: false, kind: "response", detail: "aviationweather.gov did not answer with a list of reports." };
    }
    if (!Array.isArray(arr)) {
      return { ok: false, kind: "response", detail: "aviationweather.gov did not answer with a list of reports." };
    }
    for (const o of arr) {
      if (!o || typeof o.icaoId !== "string" || typeof o.obsTime !== "number") { badRows++; continue; }
      rows++;
      if (!want.has(o.icaoId)) continue;
      const have = byId.get(o.icaoId);
      if (!have || o.obsTime > have.obsTime) byId.set(o.icaoId, o);
    }
  }
  // the "renamed field" case: reading such rows by guesswork would be a guess
  if (badRows > rows) {
    return { ok: false, kind: "format", detail: "The report format changed: reports carry no station name or no time." };
  }
  return { ok: true, byId };
}

/**
 * Volume checks beside the freshness check. Fresh-looking reports that say
 * nothing passed every check on this site for three weeks in November 2026.
 * The shares are the board's own (FEED_MIN_CURRENT_SHARE, FEED_MIN_VIS_FILL).
 */
function healthOf(n, current, withVis, withClouds) {
  // a small board may lose one station without calling the whole feed down
  const need = n >= 2 ? Math.min(Math.ceil(FEED_MIN_CURRENT_SHARE * n), n - 1) : 1;
  if (current < need) {
    return { ok: false, kind: "coverage", detail: `Only ${current} of ${n} fields have a report under ${FEED_REPORT_MAX_AGE_MS / 60_000} minutes old.` };
  }
  // 113 of 115 current reports carried a visibility in the 2026-11-25 capture
  const needVis = current >= 5 ? Math.ceil(FEED_MIN_VIS_FILL * current) : 1;
  if (withVis < needVis) {
    return { ok: false, kind: "visibility", detail: `Only ${withVis} of ${current} current reports carry a visibility.` };
  }
  // 115 of 115 carried a cloud list (it is empty under a clear sky). Without
  // it a ceiling under 200 ft goes unseen and nothing else here would notice.
  if (current >= 5 && withClouds < current / 2) {
    return { ok: false, kind: "format", detail: `The report format changed: only ${withClouds} of ${current} current reports carry a cloud list.` };
  }
  return { ok: true };
}

// "Said once" (notify.js, sayOnce): the state that says a message was told is
// written BEFORE the push, a push that did not go out takes it back, and when
// KV will not take the write a message goes out only on the first tick of a
// half hour. One field in fog used to be 288 pushes a day while KV refused writes.
export const TICK_MS = 5 * 60_000;
export const UNSAVED_EVERY_MS = 30 * 60_000;
const onSlot = (now, every) => Math.floor(now / TICK_MS) % Math.round(every / TICK_MS) === 0;
// An outage is over after two healthy ticks in a row, and a second PAUSED
// within three hours of the last is not sent (nor its "back"): a feed going
// down for 20 minutes and up for 5 was 20 PAUSED and 20 back in a day.
export const HEALTHY_TICKS_TO_CLEAR = 2;
export const PAUSED_QUIET_MS = 3 * 60 * 60_000;
// A check that has not run for this long knows nothing about the hours in
// between: its first tick is a baseline again and alerts for nothing. (The
// state is stamped every 55 minutes at least, so a shorter gap cannot be told
// from a quiet hour; a gap this side of the line still alerts on recovery.)
export const STALE_STATE_MS = 6 * 60 * 60_000;
async function putState(kv, state) {
  try {
    await kv.put(STATE_KEY, JSON.stringify(state));
    return true;
  } catch (e) {
    console.error(JSON.stringify({ event: "fog_state_write_failed", message: String(e?.message ?? e).slice(0, 200) }));
    return false;
  }
}

/** What the board is, for the state: a new board (other fields, or made at another time) starts from a baseline. */
export function boardId(board) {
  let h = 2166136261;                              // FNV-1a over the sorted ids
  for (const c of [...board.ids].sort().join(",")) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; }
  return `${board.madeMs ?? "undated"}|${board.ids.length}|${h.toString(16)}`;
}

/** The state written first, then the messages (notify.js, sayOnce). `after`: when this tick last wrote the key (KV: one write a second). */
const saveThenTell = async (env, kv, state, messages, now, run, mustSave, after = 0) => {
  const wait = after ? KV_WRITE_GAP.ms - (Date.now() - after) : 0;
  if (wait > 0 && (mustSave || messages.length)) await new Promise((done) => setTimeout(done, wait));
  return sayOnce(env, messages, {
    save: () => putState(kv, state), holder: state, now, run, slot: onSlot(now, UNSAVED_EVERY_MS), mustSave,
  });
};

// ---- the tick ----

export async function fogTick(env, now = Date.now()) {
  const run = {};
  // inert until the two secrets exist: no KV read, no request to anyone
  if (notifyOff(env, run)) return "off";
  const kv = env.FOGCAST;
  if (!kv) {
    console.error(JSON.stringify({ event: "fog_alerts_off", reason: "the FOGCAST KV binding is missing from wrangler.jsonc" }));
    return "off";
  }
  // one bulk read: the board, and the owner's request for a test message
  const got = await kv.get([BOARD_KEY, TEST_KEY]);
  if (got.get(TEST_KEY) != null) {
    const sent = await notify(env, {
      kind: "test", priority: 0,
      title: "Fog Atlas: test message",
      message: `The cron Worker can reach this phone. Sent ${zTime(now)}.`,
    }, run);
    // Deleted once Pushover has answered, whatever it said, so a refused
    // message is not sent every 5 minutes. Kept when Pushover could not be
    // reached at all: the owner asked for a test message and has not had one.
    if (!sent.retry) await kv.delete(TEST_KEY);
    console.log(JSON.stringify({ event: "fog_test", sent: sent.sent, kept: Boolean(sent.retry) }));
  }
  const boardRaw = got.get(BOARD_KEY);
  if (boardRaw == null) {
    console.log(JSON.stringify({ event: "fog_alerts_off", reason: "no board is stored under alerts:board" }));
    return "no-board";
  }

  const state = readState(await kv.get(STATE_KEY));
  const board = readBoard(boardRaw);
  const failIfUnsaved = (o) => {
    // a state that could not be written, or a message Pushover refused, must show in the dashboard
    if (!o.saved) throw new Error("the fog alert state could not be saved to KV (see fog_state_write_failed)");
    if (o.refused) throw new Error("Pushover refused a message (HTTP 4xx): it is not counted as sent and is tried again in an hour");
  };
  // Pushover refused a message within the last hour: nothing is asked of it on
  // this tick, nothing is marked as told, and nothing is written for it
  const holding = holdingOff(state, now);
  if (!board.ok) {
    console.error(JSON.stringify({ event: "fog_alerts_off", reason: `the stored board cannot be used: ${board.why}` }));
    if (state.bad !== board.why && !holding) {
      const was = state.bad;
      state.bad = board.why;
      failIfUnsaved(await saveThenTell(env, kv, state, [{
        msg: {
          kind: "board_bad", priority: 0,
          title: "Fog Atlas: fog alerts are OFF",
          message: `The stored board cannot be used: ${board.why}. No field is being checked. Store a new board (cron/README.md).`,
        },
        undo: () => { state.bad = was; },
      }], now, run, true));
    }
    return "bad-board";
  }
  const n = board.ids.length;
  if (!n) {
    console.log(JSON.stringify({ event: "fog_alerts_off", reason: "the stored board lists no stations" }));
    return "no-board";
  }

  let dirty = false;
  if (state.bad) { state.bad = null; dirty = true; }
  // A board stored again is a new start, whatever the old state says: another
  // board (other fields, or made at another time), or a check that has not
  // run for hours (the board was deleted and put back, the secrets were
  // removed for a few days). cron/README.md promises that the first check
  // after a board is stored pushes nothing; with the old state kept it pushed
  // "12 fields below CAT I" for fields that had been in fog for days.
  const bid = boardId(board);
  const stale = state.ran > 0 && now - state.ran > STALE_STATE_MS && !state.down;
  let wroteAt = 0;
  if ((state.bid && state.bid !== bid) || stale) {
    // The new start is written at once, before anything is read. A state
    // that only LOOKS hours old because KV has refused every write since must
    // not be thrown away: with it gone, every tick of that outage would be a
    // silent baseline. If this write fails the old state stands and the tick
    // goes on from it (and what it finds is said twice an hour, see sayOnce).
    const kept = { st: state.st, al: state.al, down: state.down, ran: state.ran, bid: state.bid };
    Object.assign(state, { st: {}, al: {}, down: null, ran: now, bid });
    wroteAt = Date.now();
    if (await putState(kv, state)) {
      console.log(JSON.stringify({ event: "fog_baseline", reason: stale ? "no check on record for over 6 hours" : "a new board is stored" }));
    } else {
      Object.assign(state, kept);
      wroteAt = 0;
    }
  }
  if (!state.bid) { state.bid = bid; dirty = true; }
  const stampDue = now - (state.ran || 0) >= STAMP_EVERY_MS;

  // ---- read and judge ----
  const read = await fetchReports(board.ids);
  const judged = new Map();
  let current = 0, withVis = 0, withClouds = 0, fogNow = 0;
  if (read.ok) {
    for (const id of board.ids) {
      const o = read.byId.get(id);
      if (!o) continue;
      const j = judge(o, now);
      judged.set(id, j);
      if (j.current) {
        current++;
        if (j.vis != null) withVis++;
        if (Array.isArray(o.clouds)) withClouds++;
      }
      if (j.s === "f") fogNow++;
    }
  }
  const health = read.ok ? healthOf(n, current, withVis, withClouds) : read;
  const messages = [];

  if (!health.ok) {
    // No fog check and no station state touched (see the header).
    if (!state.down) {
      state.down = { since: now, kind: health.kind, told: false, ok: 0 };
      dirty = true;
    } else {
      if (state.down.ok) { state.down.ok = 0; dirty = true; }   // one good tick in between does not end an outage
      const quiet = state.paused && now - state.paused < PAUSED_QUIET_MS;
      if (!state.down.told && !quiet && !holding && now - state.down.since >= PAUSE_AFTER_MS) {
        const was = state.paused;
        state.down.told = true; state.paused = now; dirty = true;
        messages.push({
          msg: {
            kind: "fog_paused", priority: Math.min(board.priority, 1),
            title: "Fog Atlas: fog alerts PAUSED",
            message: `No fog alert can fire. Weather reports have not been readable since ${zTime(state.down.since)}. ${health.detail} `
              + "Still trying every 5 minutes. Look at the board yourself. If this keeps happening you are not told again for 3 hours.",
            url: BOARD_URL, urlTitle: "Open the CHASE board",
          },
          undo: () => { state.down.told = false; state.paused = was; },
        });
      }
    }
    if (stampDue) { state.ran = now; dirty = true; }
    const o = await saveThenTell(env, kv, state, messages, now, run, dirty, wroteAt);
    console.error(JSON.stringify({
      event: "fog_tick", ok: false, reason: health.kind, stations: n, current,
      since: new Date(state.down.since).toISOString(), told: state.down.told, wrote: dirty && o.saved,
    }));
    failIfUnsaved(o);
    return "blind";
  }

  // ---- healthy: the outage, if there was one, is over after two such ticks in a row ----
  if (state.down) {
    const was = state.down;
    const ok = (was.ok ?? 0) + 1;
    if (ok < HEALTHY_TICKS_TO_CLEAR) {
      state.down = { ...was, ok };
      dirty = true;
    } else if (was.told && holding) {
      // "back" is owed and Pushover is refusing: the outage stays on record until it can be said
    } else {
      dirty = true;
      state.down = null;
      if (was.told) {
        messages.push({
          msg: {
            kind: "fog_resumed", priority: 0,
            title: "Fog Atlas: fog alerts are back",
            message: `Weather reports are readable again. They were not from ${zTime(was.since)} to ${zTime(now)}. `
              + "A field that went below CAT I in that time alerts now.",
          },
          undo: () => { state.down = { ...was, ok }; },     // not delivered: say it again on the next tick
        });
      }
    }
  }

  // ---- transitions ----
  const st = {}, al = {};
  const entered = [];
  for (const id of board.ids) {
    const prev = state.st[id];
    const j = judged.get(id);
    const s = j ? j.s : "u";
    const last = state.al[id];
    if (last != null && now - last < board.repeatMs) al[id] = last;
    if (prev === undefined) {
      // a newcomer (first run after a board is stored, or a lost state): its
      // first judgement is the baseline and never an alert
      st[id] = s;
      dirty = true;
    } else if (s === "u" || s === prev) {
      st[id] = prev; // a report that says nothing changes nothing
    } else if (s === "f" && al[id] == null) {
      if (holding) { st[id] = prev; continue; }   // not told and not asked: found again once Pushover is tried again
      entered.push(id);
      st[id] = "f"; al[id] = now;   // told, unless the push below does not go out (undo)
    } else {
      // went clear, or went back below CAT I inside the repeat floor: recorded, not announced
      st[id] = s;
      dirty = true;
    }
  }
  for (const id of Object.keys(state.st)) {
    if (!(id in st)) { dirty = true; break; } // a station left the board
  }

  if (entered.length) {
    const before = Object.fromEntries(entered.map((id) => [id, state.st[id]]));
    const lines = entered.slice(0, MAX_LINES).map((id) => fogLine(id, read.byId.get(id), judged.get(id), board.eteMin[id]));
    const more = entered.slice(MAX_LINES);
    if (more.length) lines.push(`and ${more.length} more: ${more.join(", ")}`);
    dirty = true;
    messages.push({
      msg: {
        kind: "fog", priority: board.priority,
        title: entered.length === 1 ? `${entered[0]} below CAT I` : `${entered.length} fields below CAT I`,
        message: lines.join("\n"),
        url: BOARD_URL, urlTitle: "Open the CHASE board",
      },
      // Pushover down for a moment, or refusing: these stations go back to what
      // they were, and a later tick finds the same change and tries again
      undo: () => { for (const id of entered) { st[id] = before[id]; delete al[id]; } },
    });
  }

  let out = { sent: [], saved: true, refused: false };
  if (dirty || stampDue) {
    state.st = st;
    state.al = al;
    state.ran = now; state.at = now; state.cur = current; state.fog = fogNow;
    out = await saveThenTell(env, kv, state, messages, now, run, true, wroteAt);
  }
  const alerted = entered.length && out.sent[messages.length - 1] ? entered.length : 0;
  console.log(JSON.stringify({
    event: "fog_tick", ok: true, stations: n, current, below: fogNow,
    entered: entered.length, alerted, wrote: (dirty || stampDue) && out.saved,
  }));
  failIfUnsaved(out);
  return "ok";
}

// ---- for the daily heartbeat (index.js) ----

/**
 * The state of the fog alerts for the heartbeat and the hourly look:
 * { status, text }. status is "off" (no board), "bad" (a board that cannot be
 * used), "new" (no check recorded yet), "dead" (no check on record for 90
 * minutes), "paused", "refused" (Pushover is refusing messages) or "running";
 * text is one or two plain sentences. The status is returned beside the words
 * because the words carry the owner's own label: the heartbeat used to look
 * for OFF or PAUSED in the sentence, and a board labelled "KOFF, 500 nm"
 * turned every heartbeat into "something needs a look". Reads two keys in one call.
 */
export async function fogStatus(kv, now) {
  const got = await kv.get([BOARD_KEY, STATE_KEY]);
  const boardRaw = got.get(BOARD_KEY);
  if (boardRaw == null) return { status: "off", text: "Fog alerts: off. No board is stored." };
  const board = readBoard(boardRaw);
  if (!board.ok) return { status: "bad", text: `Fog alerts: OFF. The stored board cannot be used: ${board.why}.` };
  const n = board.ids.length;
  if (!n) return { status: "off", text: "Fog alerts: off. The stored board lists no stations." };
  const state = readState(got.get(STATE_KEY));
  const days = board.madeMs == null ? null : Math.floor((now - board.madeMs) / 86_400_000);
  const age = days == null ? "board age unknown" : days < 1 ? "board made today" : `board made ${days} ${days === 1 ? "day" : "days"} ago`;
  const what = `${n} ${n === 1 ? "field" : "fields"}${board.label ? ` (${board.label})` : ""}, ${age}`;
  if (!state.ran) return { status: "new", text: `Fog alerts: NO CHECK RECORDED YET for ${what}.` };
  // stamps are at most 60 minutes apart; 90 allows for a few missed ticks
  if (now - state.ran > 90 * 60_000) {
    return { status: "dead", since: state.ran,
      text: `Fog alerts: NOT RUNNING. The last check on record is ${zTime(state.ran)} on ${new Date(state.ran).toISOString().slice(0, 10)}. Board: ${what}.` };
  }
  if (state.refused) {
    return { status: "refused",
      text: `Fog alerts: Pushover has REFUSED messages since ${zTime(state.refused.since)} on ${new Date(state.refused.since).toISOString().slice(0, 10)}. Board: ${what}.` };
  }
  if (state.down) {
    return { status: "paused", text: `Fog alerts: PAUSED. Weather reports have not been readable since ${zTime(state.down.since)}. Board: ${what}.` };
  }
  return { status: "running", text: `Fog alerts: running for ${what}. At ${zTime(state.at)}: ${state.cur} of ${n} had a current report, ${state.fog} below CAT I.` };
}

/** The sentence alone (kept for callers that only print it). */
export async function fogStatusLine(kv, now) {
  return (await fogStatus(kv, now)).text;
}
