// Offline test for the live-observation feed reader (app/src/feed.ts). No
// dependencies; Node strips the types itself (22.18+; CI pins 24.22.1):
//   node app/test/test_feed.mjs
//
// Why it exists: until 2026-11-24 the CHASE board and the NOW layer printed
// "nothing below CAT I within range right now" for a 502, an HTML error page, a
// truncated file and a dropped connection alike, and read visibility from a
// fixed column number. Every case below is one of those, run against the real
// file as AWC served it at 2026-11-24T22:24:17Z (5,146 stations, newest report
// 22:21Z, 24 below CAT I) and the 937 chase.json airports of that day.
//
// FEED_MODULE=<path> swaps the module under test. It is how the cases were
// shown to FAIL against the old logic (a stub that parses by position and
// accepts any response, as main.ts did).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const feed = await import(process.env.FEED_MODULE ?? "../src/feed.ts");
const {
  readFeed, feedThrown, feedTrackInit, feedTrackRead, feedView, feedDownAlertDue, feedBanner, feedDownWords,
  splitCsvLine, jsonCeil, jsonVis, subCat1, chunkIds, ageText,
  FEED_REQUIRED_COLUMNS, FEED_POLL_MS, FEED_RETRY_MS, FEED_OVERDUE_MS, FEED_DOWN_ALERT_MS,
  FEED_MAX_NEWEST_AGE_MS, FEED_REASON_LABEL, METAR_CHUNK, AWC_ROW_CAP,
  FEED_REPORT_MAX_AGE_MS, FEED_MIN_CURRENT_SHARE,
} = feed;

const fx = (name) => new URL(`./fixtures/${name}`, import.meta.url);
const repo = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
const CSV = gunzipSync(readFileSync(fx("metars.cache-2026-11-24T2224Z.csv.gz"))).toString("utf8");
const CHASE = JSON.parse(readFileSync(fx("chase-ids-2026-11-24.json"), "utf8"));
const JSON_VV = JSON.parse(readFileSync(fx("metar-json-vv-2026-11-24.json"), "utf8"));
const NOW = Date.parse("2026-11-24T22:24:17Z"); // when the fixture was fetched
const MIN = 60_000;
const LINES = CSV.split("\n");
const HEADER = LINES[0];
const fogOf = (obs) => [...obs].filter(([, o]) => o.sub).map(([k]) => k).sort();

// insert one field into a CSV line just before the visibility column (index
// 10 on 2026-11-24), leaving the quoted raw text alone
const VIS_AT = HEADER.split(",").indexOf("visibility_statute_mi");
function insertBeforeVis(line, value) {
  if (!line) return line;
  const f = splitCsvLine(line);
  const quote = (s, i) => (i === 0 && line.startsWith('"') ? `"${s}"` : s);
  return [...f.slice(0, VIS_AT), value, ...f.slice(VIS_AT)].map(quote).join(",");
}

// The pre-2026-11-24 CHASE parser, verbatim from main.ts at fb899ce5, kept
// here only to show what each bad input USED to turn into.
function oldParse(text) {
  const out = new Map();
  for (const line of text.split("\n")) {
    if (!line.startsWith('"')) continue;
    const close = line.indexOf('",');
    if (close < 0) continue;
    const cols = line.slice(close + 2).split(",");
    const icao = cols[0];
    if (out.has(icao)) continue;
    const visRaw = cols[9];
    const vis = visRaw ? (visRaw.includes("+") ? 10 : parseFloat(visRaw)) : null;
    let ceil = null;
    for (const k of [21, 23, 25, 27]) {
      if (["BKN", "OVC", "OVX"].includes(cols[k])) {
        const b = parseFloat(cols[k + 1]);
        if (!Number.isNaN(b)) ceil = Math.min(ceil ?? Infinity, b);
      }
    }
    out.set(icao, { vis, ceil, sub: (vis != null && vis < 0.5) || (ceil != null && ceil < 200) });
  }
  return out;
}

const tests = {
  "the real file is healthy and reads the same values the raw reports state"() {
    const r = readFeed(200, CSV, CHASE, NOW);
    assert.equal(r.ok, true, r.detail);
    assert.equal(r.stats.stations, 5146);
    assert.deepEqual([r.stats.expectHave, r.stats.expectTotal], [905, 937]);
    assert.equal(Math.round(r.stats.visFill * 1000), 926);
    assert.equal(new Date(r.stats.newestMs).toISOString(), "2026-11-24T22:21:00.000Z");
    assert.equal(r.stats.badRows, 0);
    assert.equal(fogOf(r.obs).length, 24);
    // METAR KSFO 242156Z 07004KT 10SM SCT033 BKN200 14/10
    assert.deepEqual(r.obs.get("KSFO"), {
      obsMs: Date.parse("2026-11-24T21:56:00Z"), vis: 10, ceil: 20000, rvr: null, rvrFt: null, wx: null,
      sub: false, below: false, tempC: 14.4, dewpC: 10, wspd: 4,
    });
    // SPECI KSMP 242217Z AUTO 1/4SM FZFG VV001
    const smp = r.obs.get("KSMP");
    assert.deepEqual([smp.vis, smp.ceil, smp.wx, smp.sub, smp.below], [0.25, 100, "FZFG", true, false]);
    // METAR ETHC 242120Z 06003KT 0100 FZFG VV001: below 300 m
    assert.equal(r.obs.get("ETHC").below, true);
    // METAR PAKU 242155Z 06022KT 1SM R06/P6000FT BLSN BR OVC008
    const aku = r.obs.get("PAKU");
    assert.deepEqual([aku.rvr, aku.rvrFt, aku.vis, aku.ceil, aku.wx, aku.sub], ["06/P6000", 6001, 1, 800, "BLSN BR", false]);
    // raw text with commas in it ("RMK A2985 TCU S,SW,W,NW") must not shift the row
    const pvk = r.obs.get("RPVK");
    assert.deepEqual([pvk.vis, pvk.ceil, pvk.tempC, pvk.wspd], [10, 7000, 25, 1]);
    // a byte-order mark or CRLF line ends in front of the header change nothing
    assert.equal(readFeed(200, String.fromCharCode(0xfeff) + CSV, CHASE, NOW).ok, true);
    assert.deepEqual([...readFeed(200, CSV.replaceAll("\n", "\r\n"), CHASE, NOW).obs], [...r.obs]);
    // and it agrees with the old positional reader on every station today
    const old = oldParse(CSV);
    assert.equal(old.size, r.obs.size);
    for (const [k, o] of old) {
      const n = r.obs.get(k);
      assert.deepEqual([n.vis, n.ceil, n.sub], [Number.isNaN(o.vis) ? null : o.vis, o.ceil, o.sub], k);
    }
  },

  "a 502 is a failed read, whatever its body says"() {
    const r = readFeed(502, "upstream unavailable", CHASE, NOW);
    assert.deepEqual([r.ok, r.reason], [false, "http"]);
    assert.match(r.detail, /HTTP 502/);
    // even a perfect body does not rescue a bad status
    assert.equal(readFeed(503, CSV, CHASE, NOW).reason, "http");
    // what the board used to make of the 502 body: an empty, truthy Map, printed as "0 in fog"
    assert.equal(oldParse("upstream unavailable").size, 0);
  },

  "error text, an HTML page and an empty body served with 200 are failed reads"() {
    const html = "<!DOCTYPE html>\n<html><head><title>Service Unavailable</title></head>\n<body><h1>503</h1></body></html>\n";
    for (const body of ["upstream unavailable", html, "", "\n\n", "[]", '{"error":"rate limited"}']) {
      const r = readFeed(200, body, CHASE, NOW);
      // "response", not "header": the NOW chip called an HTML page and an
      // empty answer "format changed", which they are not
      assert.deepEqual([r.ok, r.reason], [false, "response"], JSON.stringify(body.slice(0, 20)));
      assert.ok(r.detail.length > 10 && !/undefined|NaN/.test(r.detail), r.detail);
      assert.equal(r.obs, null);
    }
    assert.equal(FEED_REASON_LABEL.response, "bad response");
    // a file that lost its header row is not read by guessing where the columns are
    assert.equal(readFeed(200, LINES.slice(1).join("\n"), CHASE, NOW).reason, "response");
    // a table whose layout changed keeps the other label
    assert.equal(readFeed(200, CSV.replace("visibility_statute_mi", "visibility_sm"), CHASE, NOW).reason, "header");
    assert.equal(FEED_REASON_LABEL.header, "format changed");
  },

  "a body cut to 70% fails on chase coverage although 3,000+ stations remain"() {
    const cut = CSV.slice(0, Math.floor(CSV.length * 0.7));
    const r = readFeed(200, cut, CHASE, NOW);
    assert.deepEqual([r.ok, r.reason], [false, "coverage"]);
    assert.ok(r.stats.stations >= 3000, `only ${r.stats.stations} stations left`);
    assert.deepEqual([r.stats.stations, r.stats.expectHave], [3621, 334]);
    assert.equal(r.detail, "The feed is incomplete: 334 of 937 chase airports have a report (at least 797 needed).");
    // this is why the gate is coverage and not row count: without chase.json
    // only the global floor is left, and the same cut passes it
    assert.equal(readFeed(200, cut, null, NOW).ok, true);
    // a cut to 30% fails the floor too
    const r30 = readFeed(200, CSV.slice(0, Math.floor(CSV.length * 0.3)), null, NOW);
    assert.deepEqual([r30.ok, r30.reason], [false, "coverage"]);
    assert.match(r30.detail, /stations reported \(at least 3,000 needed\)/);
  },

  "a column inserted before visibility never reads as a healthy board with no fog"() {
    const base = readFeed(200, CSV, CHASE, NOW);
    assert.ok(fogOf(base.obs).length > 0, "the fixture must contain fog or this test proves nothing");
    // (a) the data rows gain a field and the header does not: rows no longer
    //     line up with the names, so nothing is read
    const rowsOnly = [HEADER, ...LINES.slice(1).map((l) => insertBeforeVis(l, "7"))].join("\n");
    const a = readFeed(200, rowsOnly, CHASE, NOW);
    assert.deepEqual([a.ok, a.reason], [false, "header"]);
    assert.equal(a.detail, "The feed's layout changed: rows have 45 fields but the header names 44. The board will not guess.");
    // (b) the header gains a name and the rows do not
    const headerOnly = [insertBeforeVis(HEADER, "wind_chill_c"), ...LINES.slice(1)].join("\n");
    assert.deepEqual([readFeed(200, headerOnly, CHASE, NOW).ok, readFeed(200, headerOnly, CHASE, NOW).reason], [false, "header"]);
    // (c) a properly named new column in header AND rows: the columns are found
    //     by name, so every observation is exactly what it was
    const both = [insertBeforeVis(HEADER, "wind_chill_c"), ...LINES.slice(1).map((l) => insertBeforeVis(l, "7"))].join("\n");
    const c = readFeed(200, both, CHASE, NOW);
    assert.equal(c.ok, true, c.detail);
    assert.deepEqual([...c.obs], [...base.obs]);
    assert.deepEqual(fogOf(c.obs), fogOf(base.obs));
    // the old reader on (a) and (c): thousands of stations, visibility taken
    // from the inserted field, not one airport in fog
    for (const text of [rowsOnly, both]) {
      const old = oldParse(text);
      assert.ok(old.size > 5000);
      assert.equal(fogOf(old).length, 0);
    }
  },

  "a renamed or duplicated column is a failed read that names the column"() {
    const renamed = CSV.replace("visibility_statute_mi", "visibility_sm");
    const r = readFeed(200, renamed, CHASE, NOW);
    assert.deepEqual([r.ok, r.reason], [false, "header"]);
    assert.equal(r.detail, `The feed's layout changed: column "visibility_statute_mi" is missing. The board will not guess.`);
    for (const col of FEED_REQUIRED_COLUMNS) {
      const x = readFeed(200, CSV.replace(HEADER, HEADER.split(",").map((h) => (h === col ? `${h}_x` : h)).join(",")), CHASE, NOW);
      assert.deepEqual([x.ok, x.reason], [false, "header"], col);
      assert.ok(x.detail.includes(`"${col}"`), `${col}: ${x.detail}`);
    }
    const twice = readFeed(200, CSV.replace(HEADER, HEADER.replace("altim_in_hg", "visibility_statute_mi")), CHASE, NOW);
    assert.deepEqual([twice.ok, twice.reason], [false, "header"]);
    assert.match(twice.detail, /"visibility_statute_mi" appears more than once/);
    // one sky_cover without its cloud_base_ft_agl partner
    const unpaired = readFeed(200, CSV.replace(HEADER, HEADER.replace("sky_cover,cloud_base_ft_agl,flight_category", "sky_cover,cloud_top_ft,flight_category")), CHASE, NOW);
    assert.deepEqual([unpaired.ok, unpaired.reason], [false, "header"]);
  },

  "a file that parses but carries almost no visibility is a failed read"() {
    // the header moves the visibility NAME onto the gust column (mostly empty):
    // same width, every volume check passes, only the fill rate can tell
    const swapped = HEADER.replace("wind_gust_kt,visibility_statute_mi", "visibility_statute_mi,wind_gust_kt");
    const r = readFeed(200, CSV.replace(HEADER, swapped), CHASE, NOW);
    assert.deepEqual([r.ok, r.reason], [false, "visibility"]);
    assert.match(r.detail, /^The feed is missing visibility: \d+% of reports carry one \(at least 70% needed\)\.$/);
  },

  "observations 3 hours old are a failed read, and one wrong clock cannot hide it"() {
    const later = NOW + 180 * MIN;
    const r = readFeed(200, CSV, CHASE, later);
    assert.deepEqual([r.ok, r.reason], [false, "stale"]);
    assert.ok(r.obs && r.obs.size === 5146, "a stuck file is still handed over for display as last-known");
    // 19 min old passes, 21 min old does not
    const newest = Date.parse("2026-11-24T22:21:00Z");
    assert.equal(readFeed(200, CSV, CHASE, newest + 19 * MIN).ok, true);
    assert.equal(readFeed(200, CSV, CHASE, newest + 21 * MIN).reason, "stale");
    // one station stamped an hour ahead of "now" does not make the file look fresh
    const skew = CSV.replace("KSFO,2026-11-24T21:56:00.000Z", "KSFO,2026-11-25T02:30:00.000Z");
    assert.notEqual(skew, CSV);
    assert.equal(readFeed(200, skew, CHASE, later).reason, "stale");
  },

  "a stall of the chase airports' own reports is a failed read although the file looks fresh"() {
    // Review, 2026-11-25: the newest report among ~5,000 stations stays under
    // 20 min old on the small 20-minute AWOS stations alone, so with every
    // chase airport's report an hour older the board read "IN FOG NOW 0 of 4"
    // under "newest report in the feed 1 min old", no banner, no alert.
    const chase = new Set(CHASE);
    const ID_AT = HEADER.split(",").indexOf("station_id"), T_AT = HEADER.split(",").indexOf("observation_time");
    const ageChase = (min) => [HEADER, ...LINES.slice(1).map((l) => {
      if (!l) return l;
      const f = splitCsvLine(l);
      if (!chase.has(f[ID_AT])) return l;
      const t = new Date(Date.parse(f[T_AT]) - min * MIN).toISOString();
      return l.replace(`,${f[ID_AT]},${f[T_AT]},`, `,${f[ID_AT]},${t},`);
    })].join("\n");
    const healthy = readFeed(200, CSV, CHASE, NOW);
    assert.deepEqual([healthy.ok, healthy.stats.expectCurrent], [true, 900]);   // 900 of 937 under 75 min old
    const r = readFeed(200, ageChase(60), CHASE, NOW);
    assert.deepEqual([r.ok, r.reason], [false, "stale"]);
    assert.equal(new Date(r.stats.newestMs).toISOString(), "2026-11-24T22:21:00.000Z", "the file as a whole still looks fresh");
    assert.equal(r.detail, `The feed has stopped updating for most chase airports: ${r.stats.expectCurrent} of 937 have a report under 75 minutes old (at least 656 needed). Other stations are still reporting.`);
    assert.ok(r.stats.expectCurrent < 100, `${r.stats.expectCurrent} chase airports still current`);
    assert.ok(r.obs && r.obs.size === 5146, "handed over for display as last-known, never as current");
    // the banner prints that sentence, and the read runs no fog-alert check
    const step = feedTrackRead(feedTrackRead(feedTrackInit(), healthy, NOW).track, r, NOW + 3 * MIN);
    assert.deepEqual([step.runAlerts, step.track.ok], [false, false]);
    const b = feedBanner(step.track, feedView(step.track, NOW + 4 * MIN), NOW + 4 * MIN, { alertsArmed: true, timeZone: "UTC" });
    assert.equal(b.lines[0], r.detail);
    assert.ok(b.lines.includes("Fog alerts are not being checked."));
    // 120 min older: not one chase airport is current
    assert.equal(readFeed(200, ageChase(120), CHASE, NOW).stats.expectCurrent, 0);
    // without the airport list there is nothing to hold the file against
    assert.equal(readFeed(200, ageChase(60), null, NOW).ok, true);
    // a slow hour is not a stall: every chase report 9 min older still passes (hourly METARs run to 65 min)
    assert.equal(readFeed(200, ageChase(9), CHASE, NOW).ok, true);
    assert.deepEqual([FEED_REPORT_MAX_AGE_MS, FEED_MIN_CURRENT_SHARE], [75 * MIN, 0.7]);
  },

  "a vertical-visibility report gives the same ceiling through the CSV and JSON readers"() {
    const csv = readFeed(200, CSV, CHASE, NOW).obs;
    assert.equal(JSON_VV.length, 2);
    for (const o of JSON_VV) {
      assert.match(o.rawOb, / VV001 /);
      assert.deepEqual(o.clouds, [{ cover: "OVX", base: 100 }], "AWC JSON writes VV as OVX");
      const ob = csv.get(o.icaoId);
      assert.equal(ob.obsMs, o.obsTime * 1000, "fixture rows must be the same report");
      assert.equal(jsonCeil(o.clouds), 100);
      assert.equal(jsonCeil(o.clouds), ob.ceil);
      assert.equal(jsonVis(o.visib), ob.vis);
      assert.equal(subCat1(jsonVis(o.visib), jsonCeil(o.clouds)), ob.sub);
    }
    // MMIO: 1/2SM VV001. Below CAT I by ceiling alone, which the old JSON
    // filter (BKN/OVC/"VV") could not see
    const mmio = JSON_VV.find((o) => o.icaoId === "MMIO");
    assert.equal(jsonVis(mmio.visib), 0.5);
    assert.equal(subCat1(jsonVis(mmio.visib), jsonCeil(mmio.clouds)), true);
    assert.equal(mmio.clouds.filter((c) => ["BKN", "OVC", "VV"].includes(c.cover)).length, 0);
    // other shapes
    assert.equal(jsonCeil([{ cover: "FEW", base: 100 }, { cover: "BKN", base: 900 }, { cover: "OVC", base: 400 }]), 400);
    assert.equal(jsonCeil([{ cover: "OVX", base: null }]), null);
    assert.equal(jsonCeil(undefined), null);
    assert.deepEqual([jsonVis("10+"), jsonVis("6+"), jsonVis(0.25), jsonVis(null), jsonVis(""), jsonVis("abc")], [10, 10, 0.25, null, null, null]);
  },

  "a thrown fetch is a failed read with its own reason"() {
    const drop = feedThrown(new TypeError("Failed to fetch"));
    assert.deepEqual([drop.ok, drop.reason, drop.obs], [false, "network", null]);
    assert.match(drop.detail, /could not reach the feed server/);
    assert.match(feedThrown(Object.assign(new Error("x"), { name: "TimeoutError" })).detail, /did not answer within 45 seconds/);
  },

  "a failed read keeps the observations on hand and leaves the alert baseline alone"() {
    const good = readFeed(200, CSV, CHASE, NOW);
    let t = feedTrackInit();
    assert.equal(feedView(t, NOW).phase, "waiting");
    let step = feedTrackRead(t, good, NOW);
    t = step.track;
    assert.deepEqual([step.adopt === good.obs, step.runAlerts, step.recovered], [true, true, false]);
    assert.deepEqual(feedView(t, NOW + MIN), { phase: "ok", newestMs: good.stats.newestMs, checkedAt: NOW });
    // 502, then a dropped connection, then an HTML page: nothing is adopted, no alert check runs
    const bad = [readFeed(502, "upstream unavailable", CHASE, 0), feedThrown(new TypeError("Failed to fetch")), readFeed(200, "<html>", CHASE, 0)];
    bad.forEach((read, i) => {
      step = feedTrackRead(t, read, NOW + (3 + i) * MIN);
      t = step.track;
      assert.deepEqual([step.adopt, step.runAlerts, step.recovered], [null, false, false]);
    });
    const v = feedView(t, NOW + 5 * MIN);
    assert.deepEqual([v.phase, v.reason, v.sinceMs, v.onHandNewestMs, v.retrying], ["down", "response", NOW + 3 * MIN, good.stats.newestMs, true]);
    // recovery
    step = feedTrackRead(t, readFeed(200, CSV, CHASE, NOW + 6 * MIN), NOW + 6 * MIN);
    assert.deepEqual([step.runAlerts, step.recovered, step.track.failedSince], [true, false, null]);
    assert.equal(feedView(step.track, NOW + 6 * MIN).phase, "ok");
  },

  "the very first read failing shows a failure, not an endless wait"() {
    const step = feedTrackRead(feedTrackInit(), readFeed(502, "upstream unavailable", CHASE, NOW), NOW);
    const v = feedView(step.track, NOW);
    assert.deepEqual([v.phase, v.reason, v.onHandNewestMs], ["down", "http", null]);
    const b = feedBanner(step.track, v, NOW, { alertsArmed: false, timeZone: "UTC" });
    assert.ok(b.lines.includes("No report has been received since this board opened."), b.lines.join(" | "));
  },

  "a stuck upstream is shown as last-known unless what is on hand is newer"() {
    const later = NOW + 180 * MIN;
    const stuck = readFeed(200, CSV, CHASE, later);
    // first open during the outage: nothing on hand, so the old file is better than an empty board
    let step = feedTrackRead(feedTrackInit(), stuck, later);
    assert.equal(step.adopt, stuck.obs);
    assert.deepEqual([step.runAlerts, step.track.ok, step.track.onHandNewestMs], [false, false, stuck.stats.newestMs]);
    // a file older than what the board already holds is not adopted
    const t = { ...feedTrackInit(), reads: 1, ok: true, lastTryAt: later, lastGoodAt: later, onHandNewestMs: stuck.stats.newestMs + 30 * MIN };
    step = feedTrackRead(t, stuck, later);
    assert.equal(step.adopt, null);
    assert.equal(step.track.onHandNewestMs, stuck.stats.newestMs + 30 * MIN);
  },

  "a page that stopped polling does not keep calling its last read current"() {
    const good = readFeed(200, CSV, CHASE, NOW);
    const t = feedTrackRead(feedTrackInit(), good, NOW).track;
    assert.equal(feedView(t, NOW + FEED_POLL_MS).phase, "ok");
    // the device slept for an hour: no read failed, but nothing was checked either
    const asleep = feedView(t, NOW + 60 * MIN);
    assert.deepEqual([asleep.phase, asleep.reason, asleep.sinceMs, asleep.retrying], ["down", "paused", NOW, false]);
    assert.equal(feedView(t, NOW + FEED_OVERDUE_MS + 1000).phase, "down");
    // reports on hand age past the limit between two polls
    const aged = { ...t, lastTryAt: NOW + 17 * MIN, lastGoodAt: NOW + 17 * MIN };
    const v = feedView(aged, good.stats.newestMs + FEED_MAX_NEWEST_AGE_MS + MIN);
    assert.deepEqual([v.phase, v.reason], ["down", "stale"]);
    // neither of those is a failed read, so neither raises the "feed down" alert by itself
    assert.equal(feedDownAlertDue(t, NOW + 60 * MIN, true), false);
  },

  "armed alerts say the feed is down once per outage, after 6 minutes, and once when it is back"() {
    const good = readFeed(200, CSV, CHASE, NOW);
    const bad = readFeed(502, "", CHASE, NOW);
    let t = feedTrackRead(feedTrackInit(), good, NOW).track;
    let fired = 0, recovered = 0;
    const tick = (now, armed = true) => {
      if (feedDownAlertDue(t, now, armed)) { fired++; t = { ...t, downAlerted: true }; }
    };
    // failed reads every 60 s for 20 minutes, with the 30 s repaint tick in between
    for (let s = 180; s <= 180 + 20 * 60; s += 30) {
      const now = NOW + s * 1000;
      if (s % 60 === 0) t = feedTrackRead(t, bad, now).track;
      tick(now);
      if (s === 180 + 6 * 60) assert.equal(fired, 0, "exactly 6 minutes is not yet 'more than 6'");
      if (s === 180 + 6 * 60 + 30) assert.equal(fired, 1);
    }
    assert.equal(fired, 1, "never a repeat every tick");
    const back = feedTrackRead(t, good, NOW + 30 * MIN);
    recovered += +back.recovered;
    t = back.track;
    recovered += +feedTrackRead(t, good, NOW + 33 * MIN).recovered;
    assert.equal(recovered, 1);
    // a second outage alerts again; an unarmed board never does; a 5-minute blip never does
    t = feedTrackRead(t, bad, NOW + 40 * MIN).track;
    tick(NOW + 45 * MIN, false);
    tick(NOW + 45 * MIN);
    assert.equal(fired, 1);
    tick(NOW + 47 * MIN, false);
    assert.equal(fired, 1);
    tick(NOW + 47 * MIN);
    assert.equal(fired, 2);
    assert.equal(FEED_DOWN_ALERT_MS, 6 * MIN);
  },

  "the banner says what, since when, how old, the real retry cadence, and never 'right now'"() {
    const good = readFeed(200, CSV, CHASE, NOW);
    const t0 = feedTrackRead(feedTrackInit(), good, NOW).track;
    const reads = {
      http: readFeed(502, "upstream unavailable", CHASE, NOW),
      network: feedThrown(new TypeError("Failed to fetch")),
      response: readFeed(200, "<html><body>Just a moment...</body></html>", CHASE, NOW),
      header: readFeed(200, CSV.replace("visibility_statute_mi", "visibility_sm"), CHASE, NOW),
      coverage: readFeed(200, CSV.slice(0, Math.floor(CSV.length * 0.7)), CHASE, NOW),
      visibility: readFeed(200, CSV.replace(HEADER, HEADER.replace("wind_gust_kt,visibility_statute_mi", "visibility_statute_mi,wind_gust_kt")), CHASE, NOW),
      stale: readFeed(200, CSV, CHASE, NOW + 180 * MIN),
    };
    for (const [reason, read] of Object.entries(reads)) {
      assert.equal(read.reason, reason);
      assert.ok(FEED_REASON_LABEL[reason], reason);
      const at = reason === "stale" ? NOW + 180 * MIN : NOW + 3 * MIN;
      const t = feedTrackRead(t0, read, at).track;
      const now = at + 9 * MIN;
      const b = feedBanner(t, feedView(t, now), now, { alertsArmed: true, timeZone: "America/Los_Angeles" });
      const text = [b.title, ...b.lines].join("\n");
      assert.equal(b.title, "LIVE FEED DOWN. THIS BOARD IS NOT CURRENT.");
      assert.doesNotMatch(text, /right now|this instant|nothing below|undefined|NaN|Invalid/i, text);
      assert.match(text, /Not current since \d\d:\d\d local \(\d\d:\d\dZ\), 9 min ago\./, text);
      assert.match(text, /Newest report received: 14:21 local \(22:21Z\), (15 min|3 h 12 min) old\./, text);
      assert.match(text, /Retrying every 60 seconds\. Last try \d\d:\d\d local\./, text);
      assert.match(text, /Fog alerts are not being checked\./, text);
    }
    assert.equal(FEED_RETRY_MS, 60_000, "the banner quotes the retry cadence; the two must be the same number");
    // stale: the sentence names the time of the newest report the feed offers
    const ts = feedTrackRead(t0, reads.stale, NOW + 180 * MIN).track;
    const bs = feedBanner(ts, feedView(ts, NOW + 180 * MIN), NOW + 180 * MIN, { alertsArmed: false, timeZone: "America/Los_Angeles" });
    assert.equal(bs.lines[0], "The feed has stopped updating: its newest report is from 14:21 local (22:21Z).");
    assert.ok(!bs.lines.includes("Fog alerts are not being checked."));
    // asleep: no failed read, so no "retrying every 60 seconds" claim
    const bp = feedBanner(t0, feedView(t0, NOW + 60 * MIN), NOW + 60 * MIN, { alertsArmed: false, timeZone: "America/Los_Angeles" });
    assert.equal(bp.lines[0], "This page has not checked the feed since 14:24 local (the tab or the device was asleep).");
    assert.ok(bp.lines.includes("Checking again now.") && !bp.lines.join(" ").includes("Retrying"));
    assert.equal(bp.title, "THIS BOARD IS NOT CURRENT.", "the page slept; the feed itself is not known to be down");
    assert.deepEqual(feedDownWords(feedView(t0, NOW + 60 * MIN)), { tag: "this page was asleep", when: "until the feed is read again" });
    assert.deepEqual(feedDownWords(feedView(ts, NOW + 180 * MIN)), { tag: "live feed down", when: "while the live feed is down" });
    assert.deepEqual([ageText(0), ageText(59_000), ageText(41 * MIN), ageText(182 * MIN)], ["under 1 min", "under 1 min", "41 min", "3 h 2 min"]);
  },

  "CSV fields: quotes, commas inside quotes, doubled quotes, empty tails"() {
    assert.deepEqual(splitCsvLine('"a,b",c,,d'), ["a,b", "c", "", "d"]);
    assert.deepEqual(splitCsvLine('"say ""hi""",x'), ['say "hi"', "x"]);
    assert.deepEqual(splitCsvLine("a,b,"), ["a", "b", ""]);
    assert.deepEqual(splitCsvLine("a"), ["a"]);
    assert.deepEqual(splitCsvLine('"unterminated,x,y'), ["unterminated,x,y"]);
    assert.equal(splitCsvLine(LINES[1]).length, HEADER.split(",").length);
  },

  "the warm-start batch is chunked under AWC's 400-row cap and the proxy's id cap"() {
    const ids = Array.from({ length: 150 }, (_, i) => `K${String(i).padStart(3, "0")}`);
    const chunks = chunkIds(ids, METAR_CHUNK);
    assert.equal(chunks.length, 4);
    assert.deepEqual(chunks.flat(), ids);
    assert.ok(chunks.every((c) => c.length <= METAR_CHUNK));
    assert.equal(AWC_ROW_CAP, 400);
    const maxIds = +repo("app/functions/api/metar.ts").match(/const MAX_IDS = (\d+);/)[1];
    assert.ok(METAR_CHUNK <= maxIds, `the board asks for ${METAR_CHUNK} ids per request but /api/metar accepts ${maxIds}`);
  },

  "the watchdog and the engine look for the same column names as the app"() {
    const yml = repo(".github/workflows/watchdog.yml");
    const list = yml.match(/FEED_COLS = \[([^\]]+)\]/);
    assert.ok(list, "FEED_COLS list not found in watchdog.yml");
    assert.deepEqual(JSON.parse(`[${list[1]}]`).sort(), [...FEED_REQUIRED_COLUMNS].sort());
    const py = repo("forecast/engine.py").match(/AWC_TRUTH_COLS = \(([^)]+)\)/);
    assert.ok(py, "AWC_TRUTH_COLS not found in forecast/engine.py");
    for (const col of JSON.parse(`[${py[1].replace(/,\s*$/, "")}]`)) assert.ok(FEED_REQUIRED_COLUMNS.includes(col), col);
    // and every one of them is in the real header
    for (const col of FEED_REQUIRED_COLUMNS) assert.ok(HEADER.split(",").includes(col), col);
    // the watchdog's copy of the chase-airport freshness gate uses the app's two numbers
    const gate = yml.match(/CUR_MAX_MIN, CUR_SHARE = (\d+), ([\d.]+)/);
    assert.ok(gate, "CUR_MAX_MIN, CUR_SHARE not found in watchdog.yml");
    assert.deepEqual([+gate[1] * MIN, +gate[2]], [FEED_REPORT_MAX_AGE_MS, FEED_MIN_CURRENT_SHARE]);
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    await fn();
    console.log("ok  ", name);
  } catch (e) {
    failed++;
    console.log("FAIL", name, "-", String(e.message).split("\n").slice(0, 6).join("\n       "));
  }
}
console.log(failed ? "FAILED" : "passed");
process.exit(failed ? 1 : 0);
