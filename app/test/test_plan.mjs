// Offline test for the DEPLOY planner's date, age and status logic
// (app/src/plan.ts). No dependencies; Node strips the types itself (22.18+; CI
// pins 24.22.1):
//   node app/test/test_plan.mjs
//
// Why it exists (readiness review 2026-11-24, finding 25): the panel named
// day 1 "tomorrow" and every later day by a weekday counted from the browser
// clock, never from the plan's own meta.day1. From local midnight until the
// next build "GO ... tomorrow" was about the morning already under way and
// each weekday was a day late; a plan a day old printed the same "tomorrow";
// and the plan was fetched once per page load. Every case below runs on real
// payloads: clock-patched rehearsals of forecast/build_deploy.py on 2026-11-24
// cycles, the payload HEAD ae4df0b4 built, and the one the site served that day.
//
// Every time zone is passed in, so the result does not depend on the machine's.
//
// PLAN_MODULE=<path> swaps the module under test. It is how the cases were
// shown to FAIL against the old logic (a stub with main.ts's old rules: index 0
// is "tomorrow", later days are weekdays from the clock, no age, one fetch).
// PLAN_MAIN=<path> swaps the main.ts the last test reads.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { mock } from "node:test";   // node's own mock clock, for the one test that lets time pass

const plan = await import(process.env.PLAN_MODULE ?? "../src/plan.ts");
const {
  planDayIso, isoDateMs, dayWords, dayPart, partWords, dayShort, dayLong, dateSpans, localIso,
  planFreshness, planAgeLine, planBanner, ageWords, refetchDue, retryDelayMs, planProblem, refreshFailWords,
  chanceStatus, noChanceWords, wordOf, wordChance, departure, zoneClock, chainDeparture, launchChance, launchChanceWords,
  fogAhead, fogAheadWords, windowWords, zoneDayHour, singleFlight,
  PLAN_REFETCH_MS, PLAN_OLD_MS, PLAN_EXPIRED_MS, PLAN_TIMEOUT_MS, HOURLY_TIMEOUT_MS,
} = plan;

const FX = JSON.parse(readFileSync(new URL("./fixtures/plan-payloads-2026-11-24.json", import.meta.url), "utf8"));
const MAIN = readFileSync(process.env.PLAN_MAIN ?? new URL("../src/main.ts", import.meta.url), "utf8");
const LA = "America/Los_Angeles", NY = "America/New_York";
const at = (s) => Date.parse(s);
const MIN = 60_000, H = 3_600_000;

// The words the panel prints for plan day index d of one payload, for a viewer
// in `tz` at `now`: the same three calls main.ts makes (deployDayWords).
function words(pay, d, now, tz, icao = "KVIS") {
  const k = String(d + 1);
  const a = pay.airports[icao];
  const day = pay.meta.days?.[k] ?? pay.meta.daycal?.days?.[k];
  const part = a.tiers[d] === "cal" ? dayPart(d, day, a.cov?.[d], pay.meta.daycal?.days?.[k]?.cover_end_utc) : {};
  return dayWords(planDayIso(pay.meta.day1, d), now, part, tz);
}
const labels = (pay, now, tz, n = 3) => Array.from({ length: n }, (_, d) => words(pay, d, now, tz).label);

// Two fields of the 16Z forecast of 2026-11-25 as the site served it at 17:27Z
// (/api/forecast): forecast hours, and for each the chance of visibility under
// 1 mile, under 1/2 mile, under 1/4 mile (the fourth number is not read here).
const CYC16 = at("2026-11-25T16:00:00Z");
const FOG16 = {
  KMFR: {
    fhrs: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,29,32,35,38,41,44,47],
    p: [[4,2,0,3],[2,1,0,1],[1,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,1,0,1],[2,1,0,1],[2,1,0,1],[3,1,0,1],[4,2,0,2],[5,2,0,2],[5,2,0,3],[6,2,0,3],[8,4,0,4],[9,4,0,5],[25,12,0,13],[19,9,0,10],[11,5,0,5],[0,0,0,0],[3,2,0,2],[5,3,0,4],[11,10,0,8],[28,24,0,25],[44,38,0,37],[47,42,0,39]],
  },
  KCVO: {
    fhrs: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,29,32,35,38,41,44,47],
    p: [[2,1,0,1],[1,1,0,1],[1,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[1,0,0,0],[2,0,0,0],[2,1,0,1],[3,1,0,1],[4,2,0,2],[6,2,0,3],[6,2,0,3],[5,2,0,2],[6,3,0,3],[0,0,0,1],[2,1,0,2],[5,3,0,3],[10,4,0,5],[15,10,0,11],[26,23,0,26],[35,32,0,35]],
  },
};
// One NEXT 48 H line as the page prints it (main.ts fogCharacter), tags
// stripped, without the "usually lasts" part: the words, then the pill.
const fogLine = (w) => (w.quiet ? w.head : `${w.head} · ${w.when}${w.later ? ` · ${w.later}` : ""} [${w.pillWords}]`);
const fogAt = (f, cyc, now, tz = LA) => { const l = fogAhead(f.fhrs, f.p, cyc, now); return l && fogLine(fogAheadWords(l, tz)); };
// a made-up field: `ps` is the under-1-mile chance at each hour, or [1 mi, 1/2 mi, 1/4 mi]
const field = (fhrs, ps) => ({ fhrs, p: ps.map((x) => (Array.isArray(x) ? x : [x, 0, 0])) });
// main.ts fogCharacter ITSELF, run on the plan.ts under test: its HTML, not its
// source text. escHtml and fogCharacter are cut out of main.ts and their types
// stripped the way Node strips plan.ts's (node:module stripTypeScriptTypes,
// Node 22.13+). Reading the source alone let six wrong versions of it through:
// the quiet test inverted (a window line cut down to "fog (<1 mi) 25%"), an
// empty quiet line, red and amber swapped, "stays marginal" drawn red, and two
// hovers that named the wrong thing.
function realFogCharacter(persistenceTable = {}) {
  const cut = (from, to) => { const a = MAIN.indexOf(from), b = MAIN.indexOf(to, a); assert.ok(a >= 0 && b > a, `main.ts: "${from}" was not found`); return MAIN.slice(a, b); };
  const src = stripTypeScriptTypes([cut("const escHtml = (s: string) =>", "\n\n"), cut("function fogCharacter(", "\nfunction miniStrip(")].join("\n"));
  return new Function("fogAhead", "fogAheadWords", "zoneDayHour", "persistenceTable", `${src}\nreturn fogCharacter;`)(fogAhead, fogAheadWords, zoneDayHour, persistenceTable);
}
const FC16 = (airports) => ({ meta: { cycle: "2026-11-25T16:00:00Z" }, airports });
// what a row reads once the tags are gone
const rowText = (html) => html.replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");

// What a promise has come to once everything already queued has run: "pending",
// { value } or { error }. No test below awaits a promise of the code under test
// itself: against a version without the guard or the time limit some of them
// never settle, and the run would end with no verdict instead of a FAIL.
const outcome = async (p) => {
  let o = "pending";
  Promise.resolve(p).then((value) => { o = { value }; }, (error) => { o = { error }; });
  await new Promise((r) => setImmediate(r));
  return o;
};
// A run the test settles by hand: `ask` is the helper under test around it.
function handRun() {
  const runs = [];
  const ask = singleFlight(() => new Promise((answer, fail) => runs.push({ answer, fail })));
  return { ask, runs };
}
// A fetch the test answers by hand. Like the browser's, it rejects when the
// signal it was handed aborts, and so does a body that is still being read.
function handFetch() {
  const calls = [];
  const fetch = (url, opts) => new Promise((resolve, reject) => {
    const signal = opts?.signal;
    const aborted = () => new DOMException("The operation was aborted.", "AbortError");
    signal?.addEventListener("abort", () => reject(aborted()));
    calls.push({
      url, signal,
      answer: (data, ok = true) => resolve({ ok, status: ok ? 200 : 503, json: async () => data }),
      garbled: () => resolve({ ok: true, status: 200, json: async () => JSON.parse("<html>") }),
      // the status line and headers arrive; the body never finishes
      headersOnly: () => resolve({ ok: true, status: 200, json: () => new Promise((_, rej) => signal?.addEventListener("abort", () => rej(aborted()))) }),
      drop: () => reject(new TypeError("Failed to fetch")),
    });
  });
  return { fetch, calls };
}
// main.ts getForecast ITSELF, with the copy it keeps and the request behind
// it, cut out of main.ts and run on the plan.ts under test, as fogCharacter is
// above. The fetch, the timer and the clock are the test's own: `timers` holds
// every setTimeout it made, and timers[i].fn() is that time running out.
function realGetForecast() {
  const a = MAIN.indexOf("let fcCache:"), b = MAIN.indexOf("\nfunction setSelected(", a);
  assert.ok(a >= 0 && b > a, "main.ts: getForecast was not found");
  const net = handFetch(), timers = [], clock = { now: at("2026-11-25T17:47:00Z") };
  const setTimeout = (fn, ms) => timers.push({ fn, ms, cleared: false });   // the id is the count: 1, 2, ...
  const clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; };
  const getForecast = new Function("singleFlight", "HOURLY_TIMEOUT_MS", "PLAN_TIMEOUT_MS", "fetch", "setTimeout", "clearTimeout", "Date",
    `${stripTypeScriptTypes(MAIN.slice(a, b))}\nreturn getForecast;`)(singleFlight, HOURLY_TIMEOUT_MS, PLAN_TIMEOUT_MS, net.fetch, setTimeout, clearTimeout, { now: () => clock.now });
  return { getForecast, calls: net.calls, timers, clock };
}

const tests = {
  "day names come from the plan's date, not the clock (the review's three viewers)"() {
    // the payload the site served on 2026-11-24: day 1 is Wed 25 Nov
    const pay = FX["live-2026-11-24T1826Z"];
    assert.equal(pay.meta.day1, "2026-11-25");
    // 04:30 in New York and 02:00 in Los Angeles on Wed 25: day 1 is TODAY and
    // day 2 is tomorrow. The old panel said "tomorrow" for Wed 25, then "Fri"
    // for Thu 26 and "Sat" for Fri 27.
    assert.deepEqual(labels(pay, at("2026-11-25T09:30:00Z"), NY), ["today, Wed 25", "tomorrow, Thu 26", "Fri 27"]);
    assert.deepEqual(labels(pay, at("2026-11-25T10:00:00Z"), LA), ["today, Wed 25", "tomorrow, Thu 26", "Fri 27"]);
    // 17:00 in Los Angeles on Tue 24: tomorrow really is tomorrow
    assert.deepEqual(labels(pay, at("2026-11-25T01:00:00Z"), LA), ["tomorrow, Wed 25", "Thu 26", "Fri 27"]);
    assert.equal(words(pay, 0, at("2026-11-25T01:00:00Z"), LA).phrase, "tomorrow (Wed 25 Nov)");
    assert.equal(words(pay, 0, at("2026-11-25T09:30:00Z"), NY).phrase, "today (Wed 25 Nov)");
  },

  "the evening before, Pacific and Eastern: a day under way is tonight and tomorrow, from its hour"() {
    // built 03:25Z on Tue 24 Nov from the 02Z cycle: day 1 IS Tue 24, hours from 03Z
    const pay = FX["0325z"], now = at("2026-11-24T03:25:00Z");
    assert.equal(pay.meta.days["1"].under_way, true);
    for (const tz of [LA, NY]) {
      assert.equal(localIso(now, tz), "2026-11-23", tz); // still Monday evening for both
      const w = words(pay, 0, now, tz);
      assert.equal(w.phrase, "tonight and tomorrow (Tue 24 Nov, from 03Z)", tz);
      assert.equal(w.label, "tonight + Tue 24", tz);
      assert.equal(w.part, "from 03Z", tz);
      // never the bare word: that date's hours began before the build
      assert.ok(!/^tomorrow\b/.test(w.phrase) && !/^tomorrow\b/.test(w.label), w.phrase);
    }
    // day 2 from an evening cycle is a whole day
    assert.equal(words(pay, 1, now, LA).phrase, "on Wed 25 Nov");
    assert.equal(words(pay, 1, now, LA).part, "");
  },

  "after local midnight the same day is today"() {
    const pay = FX["0725z"], now = at("2026-11-24T07:25:00Z");
    // 02:25 Tuesday in New York
    assert.equal(words(pay, 0, now, NY).phrase, "today (Tue 24 Nov, from 07Z)");
    assert.equal(words(pay, 0, now, NY).label, "today, Tue 24");
    // 23:25 Monday in Los Angeles: still the evening before
    assert.equal(words(pay, 0, now, LA).phrase, "tonight and tomorrow (Tue 24 Nov, from 07Z)");
    // and the day after is named by its date, not "tomorrow", in New York
    assert.equal(words(pay, 1, now, NY).label, "tomorrow, Wed 25");
    assert.equal(words(pay, 1, now, LA).label, "Wed 25");
  },

  "after 10Z the plan moves to the next date and day 2 says how far its forecast reaches"() {
    const pay = FX["1025z"], now = at("2026-11-24T10:25:00Z");
    assert.equal(pay.meta.day1, "2026-11-25");
    for (const tz of [LA, NY]) { // 02:25 and 05:25 on Tuesday
      assert.equal(words(pay, 0, now, tz).phrase, "tomorrow (Wed 25 Nov)", tz);
      // a 09Z cycle sees Thu 26 only through 09Z: a part-day sum is labelled as one
      assert.equal(words(pay, 1, now, tz).phrase, "on Thu 26 Nov through 09Z", tz);
      assert.equal(words(pay, 1, now, tz).part, "through 09Z", tz);
    }
    // by 15Z the forecast reaches further
    assert.equal(words(FX["1625z"], 1, at("2026-11-24T16:25:00Z"), LA).part, "through 15Z");
    // an issuance that lost its 26-48 h bulletin stops day 1 at 16Z at this field
    assert.equal(words(FX["1625z-short-carried"], 0, at("2026-11-24T16:25:00Z"), LA).phrase, "tomorrow (Wed 25 Nov, through 16Z)");
  },

  "a date already behind the viewer gets no today or tomorrow"() {
    const pay = FX["1025z"], now = at("2026-11-27T18:00:00Z");
    const w = words(pay, 0, now, LA);
    assert.equal(w.rel, "past");
    assert.equal(w.label, "Wed 25");
    assert.equal(w.phrase, "on Wed 25 Nov");
    assert.equal(words(pay, 2, now, LA).label, "today, Fri 27");
  },

  "plan days count from day1 across a month and a year, and a bad date is no date"() {
    assert.equal(planDayIso("2026-11-25", 0), "2026-11-25");
    assert.equal(planDayIso("2026-11-25", 13), "2026-12-08");
    assert.equal(planDayIso("2026-12-25", 13), "2027-01-07");
    assert.equal(dayLong("2026-12-08"), "Tue 8 Dec");
    assert.equal(dayShort("2027-01-01"), "Fri 1");
    for (const bad of [undefined, null, "", "2026-11", "2026-02-31", "tomorrow", 20261125]) {
      assert.equal(planDayIso(bad, 0), null, String(bad));
      assert.equal(isoDateMs(bad), null, String(bad));
    }
    assert.equal(dateSpans(["2026-11-27", "2026-11-28", "2026-11-29", "2026-11-30", "2026-12-01", "2026-12-02"]), "Fri 27 to Wed 2");
    assert.equal(dateSpans(["2026-11-25", "2026-11-27", "2026-11-28"]), "Wed 25, Fri 27 to Sat 28");
    assert.equal(dateSpans(["2026-11-26"]), "Thu 26");
  },

  "which hours a day stands on: only days 1 and 2, only when short of the whole date"() {
    const under = { under_way: true, cover: { from: 7, through: 23 } };
    assert.deepEqual(dayPart(0, under, 23), { from: 7 });
    assert.deepEqual(dayPart(1, { under_way: false, cover: { from: 0, through: 9 } }, 9), { through: 9 });
    // 21Z, 22Z and 23Z are all the whole date (the 3-hourly bulletin ends at 21Z)
    for (const cov of [21, 22, 23]) assert.deepEqual(dayPart(1, { under_way: false }, cov), {});
    assert.deepEqual(dayPart(0, under, 16), { from: 7, through: 16 });
    // a payload from before `cov`: the day's cover_end_utc stands in
    assert.deepEqual(dayPart(1, undefined, undefined, 12), { through: 12 });
    assert.deepEqual(dayPart(1, undefined, undefined, null), {});
    // days 3 and later are whole days whatever is passed
    assert.deepEqual(dayPart(2, under, 9, 9), {});
    assert.equal(partWords({ from: 7, through: 16 }), "from 07Z through 16Z");
    assert.equal(partWords({}), "");
    assert.equal(partWords(null), "");
  },

  "a plan is current for 3 h, old until 26 h, and expired after that or once its first day is over"() {
    const meta = FX["1025z"].meta, built = at(meta.generated); // 2026-11-24T10:25Z, day 1 = Wed 25
    assert.equal(planFreshness(meta, built + 5 * MIN).level, "ok");
    assert.equal(planFreshness(meta, built + PLAN_OLD_MS - MIN).level, "ok");
    const old = planFreshness(meta, built + PLAN_OLD_MS + MIN);
    assert.deepEqual([old.level, old.why], ["old", ["age"]]);
    assert.equal(planFreshness(meta, built + PLAN_EXPIRED_MS - MIN).level, "old");
    const dead = planFreshness(meta, built + PLAN_EXPIRED_MS + MIN);
    assert.deepEqual([dead.level, dead.why], ["expired", ["age"]]);
    // a plan built 09:25Z on its own first day is 14 h 34 min old at the end of that day
    const late = { ...meta, generated: "2026-11-25T09:25:00Z" };
    assert.equal(planFreshness(late, at("2026-11-25T23:59:00Z")).level, "old");
    const over = planFreshness(late, at("2026-11-26T00:00:00Z"));
    assert.deepEqual([over.level, over.why], ["expired", ["day1"]]);
    // both at once, a week on
    assert.deepEqual(planFreshness(meta, at("2026-12-01T12:00:00Z")).why, ["age", "day1"]);
    // every real payload in the fixture is current 25 minutes after it was built
    for (const [name, pay] of Object.entries(FX)) {
      if (name.startsWith("_")) continue;
      assert.equal(planFreshness(pay.meta, at(pay.meta.generated) + 25 * MIN).level, "ok", name);
    }
  },

  "unknown age is stale: no build time or no first day is an expired plan"() {
    for (const meta of [undefined, null, {}, { generated: "2026-11-24T10:25:00Z" }, { day1: "2026-11-25" },
      { generated: "soon", day1: "2026-11-25" }, { generated: "2026-11-24T10:25:00Z", day1: "25 Nov" }]) {
      const f = planFreshness(meta, at("2026-11-24T10:30:00Z"));
      assert.deepEqual([f.level, f.why], ["expired", ["undated"]], String(JSON.stringify(meta)));
    }
    // a build time ahead of this device's clock: the clock is wrong, so say so
    const f = planFreshness(FX["1025z"].meta, at("2026-11-24T10:00:00Z"));
    assert.deepEqual([f.level, f.why, f.ageMs], ["old", ["clock"], 0]);
    assert.equal(planFreshness(FX["1025z"].meta, at("2026-11-24T10:20:00Z")).level, "ok"); // 5 min of skew is not a notice
  },

  "the age line says when the plan was built, from which cycle, and how old it is"() {
    const meta = FX["1625z"].meta; // built 16:25Z from the 15Z cycle
    assert.equal(planAgeLine(meta, at("2026-11-24T16:50:00Z"), LA), "Plan built 08:25 local (16:25Z) from the 15Z forecast · 25 min old");
    assert.equal(planAgeLine(meta, at("2026-11-24T16:50:00Z"), NY), "Plan built 11:25 local (16:25Z) from the 15Z forecast · 25 min old");
    // the next local day: the build's date goes in front
    assert.equal(planAgeLine(meta, at("2026-11-25T18:00:00Z"), LA), "Plan built Tue 24 Nov 08:25 local (16:25Z) from the 15Z forecast · 25 h old");
    // a cycle from the UTC date before the build is named with its date
    assert.equal(planAgeLine({ generated: "2026-11-25T00:25:00Z", cycle: "2026-11-24T23:00:00Z", day1: "2026-11-25" }, at("2026-11-25T00:40:00Z"), LA),
      "Plan built 16:25 local (00:25Z) from the 23Z forecast of Tue 24 Nov · 15 min old");
    assert.equal(planAgeLine({}, at("2026-11-25T00:40:00Z"), LA), "This plan does not say when it was built.");
    assert.deepEqual([30_000, 41 * MIN, 119 * MIN, 3 * H + 20 * MIN, 9 * H + 59 * MIN, 31 * H, 47 * H, 4 * 24 * H].map(ageWords),
      ["under 1 min", "41 min", "119 min", "3 h 20 min", "9 h 59 min", "31 h", "47 h", "4 days"]);
  },

  "the notice and the banner: amber past 3 h, red and no verdict when expired"() {
    const meta = FX["1625z"].meta, built = at(meta.generated);
    assert.equal(planBanner(planFreshness(meta, built + 10 * MIN)), null);
    assert.deepEqual(planBanner(planFreshness(meta, built + 3 * H + 20 * MIN)), {
      title: "THIS PLAN IS 3 H 20 MIN OLD.",
      lines: ["A new plan is built every hour. The newer forecasts are not in this one."],
    });
    const t2 = at("2026-11-26T02:00:00Z"); // 18:00 Wed in Los Angeles: day 1 (Wed 25, UTC) is over, the plan is 33 h old
    const b = planBanner(planFreshness(meta, t2));
    assert.equal(b.title, "PLAN EXPIRED. DO NOT PLAN FROM IT.");
    assert.deepEqual(b.lines, [
      "Its first day, Wed 25 Nov, is over.",
      "It is 33 h old and nothing newer has arrived.",
      "No GO, MARGINAL or SCRAP is shown from an expired plan.",
    ]);
    // an expired plan's first day can still be the viewer's "today": it gets its date and no such word
    assert.equal(localIso(t2, LA), "2026-11-25");
    assert.equal(dayWords(meta.day1, t2, {}, LA).label, "today, Wed 25");
    assert.equal(dayWords(meta.day1, t2, {}, LA, true).label, "Wed 25");
    assert.equal(dayWords(meta.day1, t2, {}, LA, true).phrase, "on Wed 25 Nov");
    const u = planBanner(planFreshness({}, t2));
    assert.deepEqual(u.lines, ["It does not say when it was built or which day it starts on.", "No GO, MARGINAL or SCRAP is shown from an expired plan."]);
    assert.equal(planBanner(planFreshness(meta, built - 20 * MIN)).title, "CHECK THIS DEVICE'S CLOCK.");
  },

  "a copy is asked for again after 15 minutes, and a failed refresh is retried and says so"() {
    const t = at("2026-11-24T17:00:00Z");
    assert.equal(refetchDue(null, t), true);
    assert.equal(refetchDue(t - PLAN_REFETCH_MS + 1000, t), false);
    assert.equal(refetchDue(t - PLAN_REFETCH_MS, t), true);
    assert.equal(refetchDue(t - 6 * H, t), true); // a tab left open all day
    assert.deepEqual([1, 2, 3, 4, 5, 6, 50].map(retryDelayMs), [1, 2, 4, 8, 15, 15, 15].map((m) => m * MIN));
    const meta = FX["1625z"].meta, fail = { at: at("2026-11-24T17:40:10Z"), why: "HTTP 503" };
    assert.equal(refreshFailWords(fail, fail.at + MIN, meta, fail.at, LA),
      "Refresh failed 09:40 local (HTTP 503). Still showing the plan built 08:25 local (16:25Z). Next try 09:41 local.");
    assert.equal(refreshFailWords(fail, fail.at + MIN, null, fail.at, LA),
      "The plan did not load (HTTP 503), tried 09:40 local. Next try 09:41 local.");
  },

  "only a real plan replaces the copy on hand"() {
    assert.equal(planProblem({ error: "no deploy data yet" }), 'the planner said "no deploy data yet"');
    assert.equal(planProblem({ meta: {}, airports: {} }), "the plan held no airports");
    for (const bad of [null, undefined, "", "<html>", 42, [], { airports: [] }, { airports: "KSFO" }, { meta: {} }]) {
      assert.equal(planProblem(bad), "the answer was not a plan", JSON.stringify(bad));
    }
    for (const [name, pay] of Object.entries(FX)) if (!name.startsWith("_")) assert.equal(planProblem(pay), null, name);
  },

  "whose day-level chance: this build's, one carried with its cycle, or none"() {
    assert.deepEqual(chanceStatus(FX["1025z"].airports.KVIS, 0), { status: "fresh", from: null });
    // day 2 has no day-level chance from any cycle hour in November to February
    assert.deepEqual(chanceStatus(FX["1025z"].airports.KVIS, 1), { status: "none", from: null });
    // the 15Z cycle arrived without its 26-48 h bulletin: the 09Z chance is carried, by name
    assert.deepEqual(chanceStatus(FX["1625z-short-carried"].airports.KVIS, 0), { status: "carried", from: "2026-11-24T09:00:00Z" });
    assert.equal(FX["1625z-short-carried"].airports.KVIS.p[0], FX["1025z"].airports.KVIS.p[0]); // the number is not changed
    // the same build with nothing to carry from
    assert.deepEqual(chanceStatus(FX["1625z-short-null"].airports.KVIS, 0), { status: "none", from: null });
    // payloads from before pfrom: a number is this build's own
    assert.deepEqual(chanceStatus(FX["head-0925z-daycal2"].airports.KVIS, 0), { status: "fresh", from: null });
    assert.deepEqual(chanceStatus(FX["live-2026-11-24T1826Z"].airports.KVIS, 0), { status: "fresh", from: null });
    assert.deepEqual(chanceStatus(undefined, 0), { status: "none", from: null });
    assert.deepEqual(chanceStatus({}, 0), { status: "none", from: null });
  },

  "why a build has no day-level chance, in a few words"() {
    const m = FX["0725z"].meta; // 06Z cycle: day 2's evening map failed its check
    assert.equal(m.days["2"].chance.why, "hour");
    assert.equal(noChanceWords(m.days["2"].chance.why, m.daycal.cycle_hour, m.daycal.days["2"].failed[0], m.days["2"].chance.too_old),
      "Built from the 06Z forecast, the day-level chance ran about 11 points high at 50% or more on its check, so it is not published.");
    const s = FX["1625z-short-null"].meta;
    assert.equal(noChanceWords(s.days["1"].chance.why, s.daycal.cycle_hour), "The 15Z forecast arrived without all of that day's hours.");
    assert.equal(noChanceWords("hour", 1), "The day-level chance is not checked for a plan built from the 01Z forecast.");
    assert.equal(noChanceWords("reach", 8), "The 08Z forecast is older than any the day-level chance is checked on.");
    assert.equal(noChanceWords("season", 12), "The day-level chance passed its check over the year and missed in November to February, so it is held in those months.");
    assert.equal(noChanceWords(undefined), "This build did not publish one.");
    assert.equal(noChanceWords("hours", 15, null, 3), "The 15Z forecast arrived without all of that day's hours. The last one published is more than 24 h old and was dropped.");
  },

  "a number that ran high in its area is held back before the bars; one that ran low is never raised"() {
    assert.deepEqual([0.5, 0.499, 0.25, 0.249, 0].map(wordOf), ["GO", "MARGINAL", "MARGINAL", "SCRAP", "SCRAP"]);
    // West Coast, evening cycles of 2025: shown 0.718, observed 0.450 at 50% or more
    assert.equal(wordOf(wordChance(0.62, 27)), "MARGINAL");
    assert.equal(wordOf(wordChance(0.80, 27)), "GO");
    // West Coast, daytime cycles 2023-25: 11 points high
    assert.equal(wordOf(wordChance(0.62, 11)), "GO");
    assert.equal(wordOf(wordChance(0.55, 11)), "MARGINAL");
    // Southeast ran 22 points LOW: the word is not raised
    assert.equal(wordChance(0.40, -22), 0.40);
    assert.equal(wordOf(wordChance(0.40, -22)), "MARGINAL");
    assert.equal(wordChance(0.40, null), 0.40);
    assert.equal(wordChance(0.10, 27), 0);
  },

  "a departure time behind the clock is passed, and a clock time names its zone"() {
    const by = at("2026-11-25T11:54:00Z"); // 03:54 PST
    assert.equal(departure(by, by - MIN).passed, false);
    assert.equal(departure(by, by + MIN).passed, true);
    assert.equal(zoneClock(by, LA), "Wed 03:54 PST");
    assert.equal(zoneClock(by, "America/Denver"), "Wed 04:54 MST");
    assert.equal(zoneClock(at("2026-11-25T08:05:00Z"), LA), "Wed 00:05 PST"); // not "24:05"
    assert.equal(zoneClock(at("2026-07-04T12:00:00Z"), NY), "Sat 08:00 EDT");
  },

  "a base row's departure is worked out over every hour of the fog window, not only its first"() {
    // 2026-11-25 review, 19:40 PST Monday (03:40Z Tue), row KLOL: KPDT's fog
    // under 1 mile runs from 19:00 PST Monday to 08:00 PST Tuesday. The row
    // took the first hour the BASE could reach and took the reposition leg off
    // it, so it printed "departure time passed (Mon 19:14 PST)" while leaving
    // as late as Tue 05:29 PST still arrived in fog. 26 of 28 such rows were wrong.
    const now = at("2026-11-24T03:40:00Z");
    const hours = Array.from({ length: 14 }, (_, i) => at("2026-11-24T03:00:00Z") + i * H);   // 03Z ... 16Z
    const chain = (2 * H) + 31 * MIN;                                                        // home to base, the turn, base to field
    let d = chainDeparture(hours, chain, now);
    assert.equal(d.kind, "open", "the window is open and can still be made: never 'passed'");
    assert.equal(zoneClock(d.byMs, LA), "Tue 05:29 PST");
    assert.equal(zoneClock(d.endMs, LA), "Tue 08:00 PST");
    assert.equal(zoneClock(d.openMs, LA), "Mon 23:00 PST", "the first hour of it the chain can still make");
    // a window that has not opened for this chain: leave by its opening less the chain and the 15 min buffer
    d = chainDeparture(hours, chain, at("2026-11-23T20:00:00Z"));
    assert.deepEqual([d.kind, zoneClock(d.byMs, LA), zoneClock(d.openMs, LA)], ["by", "Mon 16:14 PST", "Mon 19:00 PST"]);
    // 14 minutes before that departure the buffer is still whole; one minute after, the window is "open" for the chain
    assert.equal(chainDeparture(hours, chain, at("2026-11-24T00:00:00Z")).kind, "by");
    assert.equal(chainDeparture(hours, chain, at("2026-11-24T00:15:00Z")).kind, "open");
    // "missed" only when no hour of it can be made at all, with the last departure that could have
    d = chainDeparture(hours, chain, at("2026-11-24T13:30:00Z"));
    assert.deepEqual([d.kind, zoneClock(d.byMs, LA)], ["missed", "Tue 05:29 PST"]);
    assert.equal(chainDeparture(hours, chain, at("2026-11-24T13:29:00Z")).kind, "open", "one minute earlier it could");
    // tonight's window is gone and tomorrow night's is in the forecast: the row plans for that one
    const two = [...hours.slice(0, 3), ...[26, 27, 28].map((h) => at("2026-11-24T03:00:00Z") + h * H)];
    d = chainDeparture(two, chain, at("2026-11-24T06:00:00Z"));
    assert.deepEqual([d.kind, zoneClock(d.openMs, LA)], ["by", "Tue 21:00 PST"]);
    // three-hourly hours beyond the hourly ones are one window, not five
    const sparse = [0, 3, 6, 9].map((h) => at("2026-11-25T06:00:00Z") + h * H);
    d = chainDeparture(sparse, chain, at("2026-11-25T08:00:00Z"));
    assert.deepEqual([d.kind, zoneClock(d.endMs, LA)], ["open", "Wed 07:00 PST"]);
    assert.equal(chainDeparture([], chain, now).kind, "none");
    // main.ts: every base row goes through it, and "departure time passed" is gone from the page
    assert.match(MAIN, /const d = chainDeparture\(hours, \(repoH \+ 0\.5 \+ ete\) \* 3600e3, now\);/);
    assert.doesNotMatch(MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n"), /departure time passed/);
    assert.match(MAIN, /const x = deployDep\(v\.home!, c, fc, now\);/);
  },

  "a launch line prints the chance of the hour the launch reaches, never a peak behind the clock"() {
    // 2026-11-25 gate review, 16:40Z, base row KWMC. The 15Z forecast for fog under 1 mile at KVIS was
    // 92% at 16Z, 83% at 17Z, 44% at 18Z and 5% at 19Z. The first hour the launch could reach was 18Z,
    // and the box read "WHEELS UP NOW → KVIS · fog (<1 mi) 92%": the 16Z hour, 40 minutes behind the clock.
    const cyc = at("2026-11-24T15:00:00Z"), open = at("2026-11-24T18:00:00Z");
    const thr = Math.max(15, 92 * 0.4);                          // the launch plan's bar for this field (deployLaunch)
    let c = launchChance([1, 2, 3, 4, 5, 6], [92, 83, 44, 5, 0, 0], cyc, open, thr);
    assert.deepEqual(c, { at: 44, top: 44, topMs: open });
    assert.equal(launchChanceWords(c, open, LA), "44% at Tue 10:00 PST");
    // a window reached on its way up (03:40Z, base KNLC, KVIS): the hour reached, then the highest still ahead in it
    const open2 = at("2026-11-24T09:00:00Z");
    c = launchChance([5, 6, 7, 8, 9, 10, 11, 12], [20, 26, 33, 41, 52, 60, 64, 58], at("2026-11-24T02:00:00Z"), open2, 25.6);
    assert.deepEqual(c, { at: 33, top: 64, topMs: at("2026-11-24T13:00:00Z") });
    assert.equal(launchChanceWords(c, open2, LA), "33% at Tue 01:00 PST, peak 64% at Tue 05:00 PST");
    // never a higher hour of ANOTHER window (more than 3 h on) ...
    c = launchChance([1, 2, 3, 10, 11], [30, 28, 26, 80, 85], cyc, cyc + H, 20);
    assert.deepEqual([c.at, c.top], [30, 30]);
    // ... nor one past the plan's 30 hours: a row was anchored on KMCE's 26% hour under "peaks at 62%", 31 h out
    c = launchChance([28, 31], [26, 62], cyc, cyc + 28 * H, 24.8);
    assert.deepEqual([c.at, c.top], [26, 26]);
    // three-hourly points 3 h apart are one window
    c = launchChance([24, 27, 30], [30, 45, 50], cyc, cyc + 24 * H, 20);
    assert.deepEqual([c.at, c.top, c.topMs], [30, 50, cyc + 30 * H]);
    // an hour that is not in the forecast gets no number at all
    assert.equal(launchChanceWords(launchChance([1, 2], [50, 60], cyc, cyc + 9 * H, 20), cyc + 9 * H, LA), "");
    // main.ts: the box and every row's hover print these words; the field's peak only ranks the fields
    const code = MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    assert.match(code, /fog \(&lt;1 mi\) \$\{escHtml\(launchChanceWords\(best\.fog, best\.openMs, tz\)\)\}/);
    assert.match(code, /launchChanceWords\(launchChance\(f\.fhrs, p10, cyc, d\.openMs, thr\), d\.openMs, home\.tz\)/);
    assert.equal((code.match(/best\.p\b/g) ?? []).length, 1, "best.p is read once, to rank the fields");
    assert.doesNotMatch(code, /peaks at \$\{/);
    assert.doesNotMatch(code, /has the highest hourly chance of fog under 1 mile in reach/);
  },

  "a NEXT 48 H line prints its own window's chance and names its day (KMFR and KCVO, 16Z of 25 Nov)"() {
    // The live site, 2026-11-25 17:41Z. The line took the field's 48-hour peak
    // and printed it beside the FIRST run of hours at 40% of it, with clock
    // hours and no day: "KMFR fog (<1 mi) 47% · 08–09 local (≈2h), clears ~10 ·
    // again next night [deepens to <½ mi: 42%]". Thursday 08 and 09 PST were 25%
    // and 19%; the 47% and the 42% were Friday 07 PST, 47 hours out. WHEELS UP,
    // right above, said 25% at Thu 08:00 PST.
    const now = at("2026-11-25T17:41:00Z");
    assert.equal(fogAt(FOG16.KMFR, CYC16, now),
      "fog (<1 mi) 25% · Thu 08–09 local (≈2h), clears ~10 · later Fri 01–07: 47% [stays marginal]");
    const l = fogAhead(FOG16.KMFR.fhrs, FOG16.KMFR.p, CYC16, now);
    assert.deepEqual([l.peak, l.first.top, l.later.top, l.half, l.quarter], [47, 25, 47, 12, 0]);
    assert.deepEqual([l.first.fromMs, l.first.toMs, l.clearsMs].map((t) => new Date(t).toISOString().slice(0, 16)),
      ["2026-11-26T16:00", "2026-11-26T17:00", "2026-11-26T18:00"]);
    assert.deepEqual([l.later.fromMs, l.later.toMs].map((t) => new Date(t).toISOString().slice(0, 16)),
      ["2026-11-27T09:00", "2026-11-27T15:00"]);
    // "KCVO fog (<1 mi) 35% · 01–07 local (≈9h)" was Friday 01–07 PST, 39 hours
    // out, with nothing on the line to say so. The 35% is that window's own.
    assert.equal(fogAt(FOG16.KCVO, CYC16, now), "fog (<1 mi) 35% · Fri 01–07 local (≈9h) [deepens to <½ mi: 32%]");
    // the same hours at a field on Eastern time carry that field's days and hours
    assert.equal(fogAt(FOG16.KMFR, CYC16, now, NY),
      "fog (<1 mi) 25% · Thu 11–12 local (≈2h), clears ~13 · later Fri 04–10: 47% [stays marginal]");
    // Both fields, every 20 minutes from before the cycle to after its last
    // hour: each number printed is the highest of the hours of its own window,
    // and no hour that has ended is in any of them.
    for (const f of Object.values(FOG16)) {
      for (let t = CYC16 - 2 * H; t <= CYC16 + 50 * H; t += 20 * MIN) {
        const a = fogAhead(f.fhrs, f.p, CYC16, t);
        const ahead = f.fhrs.map((fh, i) => ({ ms: CYC16 + fh * H, r: f.p[i] })).filter((h) => h.ms + H > t);
        if (!ahead.length) { assert.equal(a, null); continue; }
        assert.equal(a.peak, Math.max(...ahead.map((h) => h.r[0])));
        if (!a.first) { assert.ok(a.peak < 15); continue; }
        for (const w of [a.first, a.later].filter(Boolean)) {
          const hs = ahead.filter((h) => h.ms >= w.fromMs && h.ms <= w.toMs);
          assert.ok(w.fromMs + H > t && hs.length > 0 && hs[0].ms === w.fromMs && hs.every((h) => h.r[0] >= a.thr));
          assert.equal(w.top, Math.max(...hs.map((h) => h.r[0])));
        }
        const hs = ahead.filter((h) => h.ms >= a.first.fromMs && h.ms <= a.first.toMs);
        assert.deepEqual([a.half, a.quarter], [Math.max(...hs.map((h) => h.r[1])), Math.max(...hs.map((h) => h.r[2]))]);
        assert.ok(!a.later || a.later.fromMs > a.first.toMs);
        assert.doesNotMatch(fogLine(fogAheadWords(a, LA)), /NaN|Infinity|undefined|\b24[–:]|–24\b/);
      }
    }
    // main.ts: the line is these words, on the clock the caller passes, and the old numbers are gone
    const code = MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    const fn = code.slice(code.indexOf("function fogCharacter("), code.indexOf("function miniStrip("));
    assert.match(fn, /^function fogCharacter\(a: Airport, fc: any, now: number\): string \| null \{/);
    assert.match(fn, /const ahead = fogAhead\(f\.fhrs, f\.p, Date\.parse\(fc\?\.meta\?\.cycle\), now\);\s*if \(!ahead\) return null;/);
    assert.match(fn, /const w = fogAheadWords\(ahead, a\.tz\);/);
    assert.match(fn, /\$\{escHtml\(w\.head\)\}<\/span> · \$\{escHtml\(w\.when\)\}/);
    assert.match(fn, /\$\{w\.later \? ` · <span title="[^"]*">\$\{escHtml\(w\.later\)\}<\/span>` : ""\}\$\{dur\}<\/span> \$\{sev\}/);
    assert.doesNotMatch(fn, /Math\.max|f\.p\.map|Date\.now|new Date\(/, "no number and no clock of its own");
    assert.equal((code.match(/fogCharacter\(/g) ?? []).length, 2, "one definition and one caller");
    assert.match(code, /const ch = fogCharacter\(x\.a, fc, now\);/);
    assert.doesNotMatch(code, /again next night/);
    // the pill says which window it is for
    assert.equal((fn.match(/of this window \(\$\{win\}\), not of the whole 48 h">\$\{escHtml\(w\.pillWords\)\}/g) ?? []).length, 2);
    assert.match(fn, /unlikely to get below ½ mile in this window \(\$\{win\}\)">\$\{escHtml\(w\.pillWords\)\}/);
    // the note under the list says what a line is; the sentence about the days past 48 h is as it was
    assert.match(code, /Each line is the first fog window still ahead at that field: [^`]*“later” is the strongest window after it[^`]*The pill is for the first window only\. Times in these lines are each field's local time\.\$\{v\.beyond48 \? ` Beyond 48 h in this plan: \$\{escHtml\(v\.beyond48\)\}` : ""\}<\/p>`;/);
    assert.doesNotMatch(code, /Window = hours with fog probability/);
  },

  "an hour that has ended is in no window, no number and no pill"() {
    const { fhrs, p } = FOG16.KMFR;
    // An hour is ahead until it ends, one hour after its valid time. 16Z
    // Thursday (08 PST, 25%) is in the line to 16:59:59Z and out of it at 17:00Z.
    assert.match(fogAt(FOG16.KMFR, CYC16, at("2026-11-26T16:59:59Z")), /^fog \(<1 mi\) 25% · Thu 08–09 local \(≈2h\), clears ~10 · later Fri 01–07: 47% /);
    assert.equal(fogAt(FOG16.KMFR, CYC16, at("2026-11-26T17:00:00Z")),
      "fog (<1 mi) 19% · Thu 09–09 local (≈1h), clears ~10 · later Fri 01–07: 47% [stays marginal]");
    // At 18:00Z all of Thursday's window has ended. The line is Friday's, with
    // Friday's numbers, and nothing is "later" any more.
    assert.equal(fogAt(FOG16.KMFR, CYC16, at("2026-11-26T18:00:00Z")), "fog (<1 mi) 47% · Fri 01–07 local (≈9h) [deepens to <½ mi: 42%]");
    const l = fogAhead(fhrs, p, CYC16, at("2026-11-26T18:00:00Z"));
    assert.deepEqual([l.first.fromFhr, l.first.toFhr, l.later, l.clearsMs], [41, 47, null, null]);
    // A peak behind the clock sets neither the number nor the bar: 90% and 80%
    // have ended, the highest hour still ahead is 20%, and that hour clears a
    // bar of 15 (it would not clear 40% of 90).
    const cyc = CYC16;
    assert.equal(fogAt(field([1, 2, 3, 4], [90, 80, 10, 20]), cyc, cyc + 3 * H), "fog (<1 mi) 20% · Wed 12–12 local (≈1h) [stays marginal]");
    assert.equal(fogAt(field([1, 2, 3, 4], [90, 80, 10, 20]), cyc, cyc), "fog (<1 mi) 90% · Wed 09–10 local (≈2h), clears ~11 [stays marginal]");
    // ... and the quiet line's peak is the highest still ahead
    assert.equal(fogAt(field([1, 2, 3, 4], [90, 80, 10, 12]), cyc, cyc + 3 * H), "quiet next 48 h (peak 12%)");
    // the pill: 70% under 1/4 mile was the hour that has ended
    const deep = field([1, 2, 3], [[90, 80, 70], [60, 10, 0], [50, 5, 0]]);
    assert.equal(fogAt(deep, cyc, cyc), "fog (<1 mi) 90% · Wed 09–11 local (≈3h) [deepens to <¼ mi: 70%]");
    assert.equal(fogAt(deep, cyc, cyc + 2 * H), "fog (<1 mi) 60% · Wed 10–11 local (≈2h) [stays marginal]");
    // no hour ahead is no line, as for a field with no forecast: never a line made of hours that have ended
    assert.equal(fogAhead(fhrs, p, CYC16, CYC16 + 48 * H - 1) === null, false, "the last hour (fhr 47) is ahead until it ends");
    assert.equal(fogAhead(fhrs, p, CYC16, CYC16 + 48 * H), null);
    assert.equal(fogAhead(fhrs, p, CYC16, CYC16 + 300 * H), null);
    assert.equal(fogAhead([], [], CYC16, CYC16), null);
    assert.equal(fogAhead(undefined, undefined, CYC16, CYC16), null);
    assert.equal(fogAhead(fhrs, undefined, CYC16, CYC16), null);
    assert.equal(fogAhead(fhrs, p, NaN, CYC16), null, "a forecast that does not say its cycle has no hour to place");
    // an hour without a number is not an hour: it is left out, and nothing is printed from it
    assert.equal(fogAt({ fhrs: [1, 2, 3], p: [[40, 30, 0], null, [50, null, undefined]] }, cyc, cyc),
      "fog (<1 mi) 50% · Wed 09–11 local (≈3h) [deepens to <½ mi: 30%]");
  },

  "a window carries the weekday of its first hour in the field's zone, and of its last when the date changes"() {
    // 06Z to 10Z on Thu 26 Nov: Wednesday night into Thursday on Pacific time,
    // all of it Thursday morning on Eastern
    const cyc = at("2026-11-26T00:00:00Z");
    const night = field([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], [0, 0, 0, 0, 0, 40, 50, 60, 50, 40, 0, 0]);
    assert.equal(fogAt(night, cyc, cyc, LA), "fog (<1 mi) 60% · Wed 22–Thu 02 local (≈5h), clears ~03 [stays marginal]");
    assert.equal(fogAt(night, cyc, cyc, NY), "fog (<1 mi) 60% · Thu 01–05 local (≈5h), clears ~06 [stays marginal]");
    // midnight is "00", as the first hour and as the last, and never "24"
    assert.equal(fogAt(field([7, 8, 9, 10], [0, 40, 50, 0]), cyc, cyc, LA), "fog (<1 mi) 50% · Thu 00–01 local (≈2h), clears ~02 [stays marginal]");
    assert.equal(fogAt(field([5, 6, 7, 8, 9], [0, 40, 50, 60, 0]), cyc, cyc, LA), "fog (<1 mi) 60% · Wed 22–Thu 00 local (≈3h), clears ~01 [stays marginal]");
    // one instant, two fields, two weekdays
    const t = at("2026-11-27T05:00:00Z");
    assert.deepEqual([zoneDayHour(t, LA), zoneDayHour(t, NY), zoneDayHour(t, "Pacific/Honolulu")], ["Thu 21", "Fri 00", "Thu 19"]);
    assert.equal(windowWords(at("2026-11-27T06:00:00Z"), at("2026-11-27T15:00:00Z"), LA), "Thu 22–Fri 07");
    assert.equal(windowWords(at("2026-11-27T06:00:00Z"), at("2026-11-27T15:00:00Z"), NY), "Fri 01–10");
    // a window of one hour: both ends in the list's first part, one hour after "later"
    assert.equal(windowWords(t, t, LA), "Thu 21–21");
    assert.equal(windowWords(t, t, LA, true), "Thu 21");
    assert.equal(windowWords(t, t + H, LA, true), "Thu 21–22");
    // every weekday is the field's own, hour by hour over a week (and across the end of daylight time, 1 Nov 2026)
    for (const tz of [LA, NY, "America/Anchorage", "Pacific/Honolulu", "America/St_Johns"]) {
      for (let ms = at("2026-10-30T00:00:00Z"); ms < at("2026-11-06T00:00:00Z"); ms += H) {
        const want = new Intl.DateTimeFormat("en-US", { weekday: "short", timeZone: tz }).format(new Date(ms));
        assert.match(zoneDayHour(ms, tz), new RegExp(`^${want} ([01]\\d|2[0-3])$`));
      }
    }
    // A field without a zone, or with one the browser does not know, does not
    // throw: the hours are then the viewer's, as zoneClock's are without one.
    const mine = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const ahead = fogAhead(night.fhrs, night.p, cyc, cyc);
    for (const tz of [undefined, null, "", "Mars/Olympus_Mons"]) {
      assert.equal(fogLine(fogAheadWords(ahead, tz)), fogLine(fogAheadWords(ahead, mine)));
      assert.equal(zoneDayHour(t, tz), zoneDayHour(t, mine));
      assert.equal(windowWords(t, t + 9 * H, tz), windowWords(t, t + 9 * H, mine));
    }
    assert.equal(zoneDayHour(NaN, LA), "");
    // main.ts: each bar of the 48 h strip under a row names its day too
    assert.match(MAIN, /const lab = zoneDayHour\(cyc\.getTime\(\) \+ fh \* 3600e3, a\.tz\);\s*return `<div title="\+\$\{fh\}h \(\$\{lab\}:00 local\) — \$\{p10\[i\]\}% fog"/);
    assert.equal(`+41h (${zoneDayHour(CYC16 + 41 * H, LA)}:00 local) — 15% fog`, "+41h (Fri 01:00 local) — 15% fog");
  },

  "windows on the 3-hourly hours, to the last hour, and the strongest one after the first"() {
    const cyc = CYC16;
    // 3-hourly points 3 h apart are one window; the length counts three hours for a 3-hourly start
    const sparse = field([26, 29, 32, 35, 38, 41, 44, 47], [0, 30, 40, 35, 0, 0, 50, 45]);
    assert.equal(fogAt(sparse, cyc, cyc), "fog (<1 mi) 40% · Thu 13–19 local (≈9h), clears ~22 · later Fri 04–07: 50% [stays marginal]");
    // a window that runs to the last hour of the forecast has no "clears": nothing says it does
    assert.equal(fogAt(field([41, 44, 47], [0, 50, 45]), cyc, cyc), "fog (<1 mi) 50% · Fri 04–07 local (≈6h) [stays marginal]");
    // 2 h apart is one window (the 15Z forecast steps 25, 27, 30); more than 3 h apart is two
    let l = fogAhead([24, 25, 27, 30], [[40], [40], [40], [40]], cyc, cyc);
    assert.deepEqual([l.first.fromFhr, l.first.toFhr, l.durH, l.later], [24, 30, 7, null]);
    l = fogAhead([1, 2, 6, 7], [[40], [40], [40], [40]], cyc, cyc);
    assert.deepEqual([l.first.fromFhr, l.first.toFhr, l.later.fromFhr, l.later.toFhr], [1, 2, 6, 7]);
    // "later" is one hour when the window is one hour, and it is printed whether higher than the first window ...
    assert.equal(fogAt(field([1, 2, 3, 4, 5, 6], [30, 30, 0, 0, 47, 0]), cyc, cyc),
      "fog (<1 mi) 30% · Wed 09–10 local (≈2h), clears ~11 · later Wed 13: 47% [stays marginal]");
    // ... or lower
    assert.equal(fogAt(field([1, 2, 3, 4, 5, 6], [60, 60, 0, 0, 30, 0]), cyc, cyc),
      "fog (<1 mi) 60% · Wed 09–10 local (≈2h), clears ~11 · later Wed 13: 30% [stays marginal]");
    // of several, the one holding the highest chance; the earliest on a tie
    assert.match(fogAt(field([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [50, 0, 30, 0, 45, 0, 45, 0, 20, 0]), cyc, cyc), / · later Wed 13: 45% /);
    assert.match(fogAt(field([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], [50, 0, 30, 0, 45, 0, 46, 0, 20, 0]), cyc, cyc), / · later Wed 15: 46% /);
    // a later window that crosses midnight names both days
    assert.match(fogAt(field([1, 2, 12, 13, 14, 15, 16, 17], [50, 0, 0, 30, 35, 40, 30, 0]), cyc, cyc), / · later Wed 21–Thu 00: 40% /);
    // no later window, no such part
    assert.equal(fogAt(field([1, 2, 3], [50, 60, 0]), cyc, cyc), "fog (<1 mi) 60% · Wed 09–10 local (≈2h), clears ~11 [stays marginal]");
    // under 15 at every hour ahead is the quiet line; 15 is a window
    assert.equal(fogAt(field([1, 2, 3], [5, 14, 3]), cyc, cyc), "quiet next 48 h (peak 14%)");
    assert.equal(fogAt(field([1, 2, 3], [5, 15, 3]), cyc, cyc), "fog (<1 mi) 15% · Wed 10–10 local (≈1h), clears ~11 [stays marginal]");
    assert.equal(fogAt(field([1, 2, 3], [0, 0, 0]), cyc, cyc), "quiet next 48 h (peak 0%)");
    // the bar is 40% of the highest chance ahead and never under 15: 39 of 100 is out, 40 is in
    assert.match(fogAt(field([1, 2, 3], [39, 100, 40]), cyc, cyc), /· Wed 10–11 local \(≈2h\) \[/);
  },

  "the pill is the first window's depth, never a later window's"() {
    const cyc = CYC16;
    // tonight's window stays shallow; tomorrow night's is the deep one. The old
    // pill took both maxima over all 48 hours and read "deepens to <¼ mi: 50%".
    const f = field([1, 2, 3, 4, 5, 6], [[40, 24, 0], [45, 20, 5], [0, 0, 0], [0, 0, 0], [80, 60, 50], [0, 0, 0]]);
    const l = fogAhead(f.fhrs, f.p, cyc, cyc);
    assert.deepEqual([l.first.top, l.half, l.quarter, l.later.top], [45, 24, 5, 80]);
    assert.deepEqual([fogAheadWords(l, LA).pill, fogAheadWords(l, LA).pillWords], ["dim", "stays marginal"]);
    // 25% or more under 1/4 mile in the first window is red; else 25% or more under 1/2 mile is amber
    const pill = (rows) => { const w = fogAheadWords(fogAhead(rows.map((_, i) => i + 1), rows, cyc, cyc), LA); return [w.pill, w.pillWords]; };
    assert.deepEqual(pill([[40, 30, 26], [45, 34, 25]]), ["red", "deepens to <¼ mi: 26%"]);
    assert.deepEqual(pill([[40, 30, 24], [45, 34, 20]]), ["amber", "deepens to <½ mi: 34%"]);
    assert.deepEqual(pill([[40, 25, 0]]), ["amber", "deepens to <½ mi: 25%"]);
    assert.deepEqual(pill([[40, 24, 24]]), ["dim", "stays marginal"]);
    // the two depths can come from different hours of the window, and each is that window's highest
    assert.deepEqual(pill([[40, 10, 30], [45, 50, 0]]), ["red", "deepens to <¼ mi: 30%"]);
  },

  "the pill is red at exactly 25% under 1/4 mile"() {
    // the red rung was tested at 26 and 24 only: ">= 26" and "> 25" both passed
    const pill = (rows) => { const w = fogAheadWords(fogAhead(rows.map((_, i) => i + 1), rows, CYC16, CYC16), LA); return [w.pill, w.pillWords]; };
    assert.deepEqual(pill([[40, 30, 25]]), ["red", "deepens to <¼ mi: 25%"]);
    assert.deepEqual(pill([[40, 30, 24]]), ["amber", "deepens to <½ mi: 30%"]);
  },

  "the bar is 40% of the highest chance ahead, unrounded: 16 is under 40% of 41"() {
    // 0.4 x 41 = 16.4. A bar rounded down, or to the nearest whole number, let
    // the 16% hour in and changed 301 and 86 real lines over six saved forecasts
    // (06Z KVIS at 14:22Z: "Wed 07–07 local (≈1h), clears ~08" became "Wed 07–08 local (≈2h), clears ~09").
    assert.equal(fogAt(field([1, 2, 3, 4], [16, 0, 41, 0]), CYC16, CYC16), "fog (<1 mi) 41% · Wed 11–11 local (≈1h), clears ~12 [stays marginal]");
    assert.equal(fogAt(field([1, 2, 3, 4], [17, 0, 41, 0]), CYC16, CYC16), "fog (<1 mi) 17% · Wed 09–09 local (≈1h), clears ~10 · later Wed 11: 41% [stays marginal]");
    // 0.4 x 44 = 17.6: 17 is out, 18 is in
    assert.match(fogAt(field([1, 2, 3, 4], [17, 0, 44, 0]), CYC16, CYC16), /^fog \(<1 mi\) 44% · Wed 11–11 /);
    assert.match(fogAt(field([1, 2, 3, 4], [18, 0, 44, 0]), CYC16, CYC16), /^fog \(<1 mi\) 18% · Wed 09–09 /);
  },

  "the length counts three hours from a start past forecast hour 25"() {
    // a point at forecast hour 25 or before stands for one hour, one after it for three
    assert.equal(fogAhead([24, 25, 26, 29], [[0], [40], [0], [0]], CYC16, CYC16).durH, 1);
    assert.equal(fogAhead([24, 25, 26, 29], [[0], [0], [40], [0]], CYC16, CYC16).durH, 3);
    assert.equal(fogAhead([24, 25, 26, 29], [[0], [0], [40], [40]], CYC16, CYC16).durH, 6);
  },

  "midnight is 00 in an engine that writes it 24"() {
    // Node, Chromium and Firefox write the midnight hour of a 24-hour clock as
    // "00"; an engine that resolves hour12: false to the 1-24 cycle writes "24".
    const real = Intl.DateTimeFormat.prototype.formatToParts;
    Intl.DateTimeFormat.prototype.formatToParts = function (d) { return real.call(this, d).map((x) => (x.type === "hour" && x.value === "00" ? { ...x, value: "24" } : x)); };
    try {
      const t = at("2026-11-27T08:00:00Z");   // Fri 00:00 PST
      assert.equal(zoneDayHour(t, LA), "Fri 00");
      assert.equal(windowWords(t - 2 * H, t, LA), "Thu 22–Fri 00");
      assert.equal(windowWords(t, t + H, LA), "Fri 00–01");
      assert.equal(fogAt(field([5, 6, 7, 8, 9], [0, 40, 50, 0, 0]), at("2026-11-26T00:00:00Z"), at("2026-11-26T00:00:00Z"), LA),
        "fog (<1 mi) 50% · Wed 22–23 local (≈2h), clears ~00 [stays marginal]");
    } finally { Intl.DateTimeFormat.prototype.formatToParts = real; }
  },

  "forecast hours out of order, or a depth that is missing, change nothing"() {
    // hours are put in order before any run is drawn
    const now = at("2026-11-25T17:41:00Z");
    const idx = FOG16.KMFR.fhrs.map((_, i) => i).reverse();
    const rev = { fhrs: idx.map((i) => FOG16.KMFR.fhrs[i]), p: idx.map((i) => FOG16.KMFR.p[i]) };
    assert.deepEqual(fogAhead(rev.fhrs, rev.p, CYC16, now), fogAhead(FOG16.KMFR.fhrs, FOG16.KMFR.p, CYC16, now));
    assert.equal(fogAt(rev, CYC16, now), "fog (<1 mi) 25% · Thu 08–09 local (≈2h), clears ~10 · later Fri 01–07: 47% [stays marginal]");
    // a missing depth is none, never NaN, whichever depth is missing
    const a = fogAhead([1, 2], [[40, undefined, 30], [50, 30]], CYC16, CYC16);
    assert.deepEqual([a.half, a.quarter], [30, 30]);
    const b = fogAhead([1, 2], [[40, 30], [50]], CYC16, CYC16);
    assert.deepEqual([b.half, b.quarter], [30, 0]);
  },

  "a line is worked out again when its earliest hour ends: the row carries that time"() {
    // fogAhead drops an hour the moment it ends. The screen did not. Built page,
    // 16Z forecast of 2026-11-25, base KEAT, field KMWH: filled at 17:59:40Z the
    // line read "fog (<1 mi) 81% · Wed 09–10 local (≈2h), clears ~11" with
    // "deepens to <½ mi: 60%", both from the 17Z hour, and it read the same at
    // 18:00:40Z, 18:05Z and 18:14:30Z. The panel fills this section again when a
    // time on it has passed ([data-until]) or after 15 minutes, and the list
    // rows carried no time.
    const until = (f, now) => fogAhead(f.fhrs, f.p, CYC16, now).untilMs;
    // 17:41Z: the earliest hour still ahead is 17Z, and it ends at 18:00Z
    assert.equal(until(FOG16.KMFR, at("2026-11-25T17:41:00Z")), at("2026-11-25T18:00:00Z"));
    assert.equal(until(FOG16.KMFR, at("2026-11-25T17:59:59Z")), at("2026-11-25T18:00:00Z"));
    assert.equal(until(FOG16.KMFR, at("2026-11-25T18:00:00Z")), at("2026-11-25T19:00:00Z"));
    // before the forecast's first hour it is that hour's end; on the 3-hourly
    // hours it is the end of the next hour that has a number (26 ended at +27 h, 29 ends at +30 h)
    assert.equal(until(FOG16.KMFR, CYC16 - 5 * H), CYC16 + 2 * H);
    assert.equal(until(FOG16.KMFR, CYC16 + 27 * H), CYC16 + 30 * H);
    // the quiet line has one too: its peak can be the hour that ends
    const quiet = field([1, 2, 3], [14, 3, 2]);
    assert.equal(fogAt(quiet, CYC16, CYC16), "quiet next 48 h (peak 14%)");
    assert.equal(until(quiet, CYC16), CYC16 + 2 * H);
    assert.equal(fogAt(quiet, CYC16, CYC16 + 2 * H), "quiet next 48 h (peak 3%)");
    // hours handed over out of order: still the earliest hour's end
    assert.equal(fogAhead([3, 2, 1], [[2], [3], [14]], CYC16, CYC16).untilMs, CYC16 + 2 * H);
    // What the redraw rests on, both fields, every 20 minutes: the time is
    // ahead of the clock, the line worked out now is the line at every instant
    // before it, and at that instant there is a new line with a later time, or none.
    for (const f of Object.values(FOG16)) {
      for (let t = CYC16 - 2 * H; t < CYC16 + 48 * H; t += 20 * MIN) {
        const a = fogAhead(f.fhrs, f.p, CYC16, t);
        assert.ok(a.untilMs > t, `the time is not ahead of ${new Date(t).toISOString()}`);
        assert.deepEqual(fogAhead(f.fhrs, f.p, CYC16, a.untilMs - 1), a);
        const next = fogAhead(f.fhrs, f.p, CYC16, a.untilMs);
        assert.ok(next === null || next.untilMs > a.untilMs);
      }
    }
    // main.ts: the line carries the time, once, on the quiet line as on a window line ...
    const fogCharacter = realFogCharacter();
    const fc = FC16({ KMFR: FOG16.KMFR, KQQQ: quiet });
    const stamps = (icao, now) => [...fogCharacter({ icao, tz: LA }, fc, now).matchAll(/ data-until="([^"]*)"/g)].map((m) => m[1]);
    assert.deepEqual(stamps("KMFR", at("2026-11-25T17:41:00Z")), [String(at("2026-11-25T18:00:00Z"))]);
    assert.deepEqual(stamps("KMFR", at("2026-11-26T18:00:00Z")), [String(at("2026-11-26T19:00:00Z"))]);
    assert.deepEqual(stamps("KQQQ", CYC16), [String(CYC16 + 2 * H)]);
    // ... and the panel's 30 s tick fills the section again once any time on the panel has passed
    const code = MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    assert.match(code, /const passed = \[\.\.\.panelContent\.querySelectorAll<HTMLElement>\("\[data-until\]"\)\]\.some\(\(el\) => \+el\.dataset\.until! < now\);\s*if \(passed \|\| now - deployLaunchAt > PLAN_REFETCH_MS\) fillDeployLaunch\(\);/);
    // the line is drawn inside the section the tick reads
    assert.match(code, /<td>\$\{ch\}<\/td><\/tr>/);
  },

  "fogCharacter's HTML: the right branch, the right colour, and hovers that name the first window"() {
    const fogCharacter = realFogCharacter({ KMFR: { medianH: 2 } });
    const A = (icao, tz = LA) => ({ icao, tz });
    // the window line (KMFR, 17:41Z)
    let h = fogCharacter(A("KMFR"), FC16({ KMFR: FOG16.KMFR }), at("2026-11-25T17:41:00Z"));
    assert.equal(rowText(h), "fog (<1 mi) 25% · Thu 08–09 local (≈2h), clears ~10 · later Fri 01–07: 47% · usually lasts ~2h once in stays marginal");
    assert.equal(h, `<span class="chase-live" data-until="${at("2026-11-25T18:00:00Z")}"><span title="highest chance of visibility under 1 mile among the hours of this window (Thu 08–09 local)">fog (&lt;1 mi) 25%</span>`
      + ' · Thu 08–09 local (≈2h), clears ~10 · <span title="the strongest fog window after this one, with its own day and its own highest chance">later Fri 01–07: 47%</span> · usually lasts ~2h once in</span>'
      + ' <span class="pill dim" title="unlikely to get below ½ mile in this window (Thu 08–09 local)">stays marginal</span>');
    // the quiet line
    h = fogCharacter(A("KQQQ"), FC16({ KQQQ: field([1, 2, 3], [5, 14, 3]) }), CYC16);
    assert.equal(h, `<span class="chase-live dim" data-until="${CYC16 + 2 * H}">quiet next 48 h (peak 14%)</span>`);
    // amber and red: class, hover and words agree, and each hover names the FIRST window
    h = fogCharacter(A("KAAA"), FC16({ KAAA: field([1, 2, 3, 6, 7], [[40, 30, 24], [45, 34, 20], [0, 0, 0], [90, 80, 70], [0, 0, 0]]) }), CYC16);
    assert.match(h, / <span class="pill amber" title="chance the fog deepens below ½ mile at the worst hour of this window \(Wed 09–10 local\), not of the whole 48 h">deepens to &lt;½ mi: 34%<\/span>$/);
    // a field with no persistence figure has no "usually lasts" part
    assert.equal(rowText(h), "fog (<1 mi) 45% · Wed 09–10 local (≈2h), clears ~11 · later Wed 14: 90% deepens to <½ mi: 34%");
    h = fogCharacter(A("KRRR"), FC16({ KRRR: field([1, 2, 3, 6, 7], [[40, 30, 26], [45, 34, 25], [0, 0, 0], [20, 0, 0], [0, 0, 0]]) }), CYC16);
    assert.match(h, / <span class="pill red" title="chance the fog deepens below ¼ mile at the worst hour of this window \(Wed 09–10 local\), not of the whole 48 h">deepens to &lt;¼ mi: 26%<\/span>$/);
    // no forecast at all, none for the field, and no hour ahead: no line
    assert.equal(fogCharacter(A("KMFR"), null, CYC16), null);
    assert.equal(fogCharacter(A("KZZZ"), FC16({ KMFR: FOG16.KMFR }), CYC16), null);
    assert.equal(fogCharacter(A("KMFR"), FC16({ KMFR: FOG16.KMFR }), CYC16 + 48 * H), null);
    // a field with no zone, or one the browser does not know, still gets a line
    // (in the viewer's hours, so no day or hour is named here)
    for (const tz of [undefined, "", "Mars/Olympus_Mons"]) {
      assert.match(rowText(fogCharacter(A("KMFR", tz), FC16({ KMFR: FOG16.KMFR }), CYC16)), /^fog \(<1 mi\) 25% · [A-Z][a-z]{2} \d\d–(?:[A-Z][a-z]{2} )?\d\d local \(≈2h\), clears ~\d\d · later /);
    }
  },

  "the word is never stronger than the line under it, and two numbers that disagree get no word"() {
    const code = MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    // A calibrated number is held back by the miss its own line states, as a
    // flagged one always was: 52% under "ran about 6 points high on days like
    // this" printed a green GO.
    assert.match(code, /const wp = c \? wordChance\(p, c\.hold\) : p;/);
    assert.equal(wordOf(wordChance(0.52, 6)), "MARGINAL");
    assert.equal(wordOf(wordChance(0.57, 6)), "GO");
    assert.equal(wordOf(wordChance(0.79, 30)), "MARGINAL", "a West Coast 79% from a 02Z cycle, whose own sample ran 30 points high");
    // A day under way is also scored from when the plan could be read, and the larger miss is the one printed.
    assert.match(code, /r\.under_way && r\.after_build \? pick\(r\.after_build/);
    assert.match(code, /counting fog after the plan was built/);
    // An area map that is not held up to the peak hour, under a peak hour that
    // would earn a stronger word: no GO, MARGINAL or SCRAP. "SCRAP - KVIS: 22%"
    // stood over "peak hourly chance 65%" and a launch plan to the same field.
    assert.match(code, /if \(k\.pk != null && wordOf\(k\.pk \/ 100\) !== word\) \{\s*word = "NO VERDICT"; color = GREY;/);
    assert.match(code, /They do not agree, so no GO, MARGINAL or SCRAP is given\./);
    // The same when that field is ANOTHER one in reach: base KNLC read a red "SCRAP - KSUU: 27% ... Read as
    // SCRAP, not MARGINAL" with KVIS 11 minutes away at a 56% peak hour and a launch plan (2026-11-25 gate).
    assert.match(code, /const capped = word === "SCRAP" && k\.peak && k\.peak\.icao !== k\.icao && wordOf\(k\.peak\.pk \/ 100\) !== "SCRAP" \? k\.peak : null;/);
    assert.match(code, /const off = !!capped && word === "SCRAP";/);
    assert.match(code, /if \(off\) \{ word = "NO VERDICT"; color = GREY; \}/);
    assert.match(code, /\$\{off \? " So no GO, MARGINAL or SCRAP is given\." : ""\}/);
    assert.equal((code.match(/c\.hold != null && !capped/g) ?? []).length, 2, 'both "Read as SCRAP" lines are left off');
    assert.doesNotMatch(code, /no strong fog day in window/);
    // the hourly forecast is not called calibrated over a field whose check it did not pass
    assert.doesNotMatch(code, /calibrated live forecast|calibrated hourly forecast/);
    // a part-covered day 2 adds the climatology of the hours past the forecast to a multi-day total
    assert.match(code, /\(\(d\.eh\[i\] \?\? 0\) \+ \(d\.ehc\?\.\[i\] \?\? 0\)\) \* usableFrac/);
    // a click on an LED ring with CHASE or DEPLOY open does not replace the panel
    assert.match(code, /const ledOwnsClick = \(point: \{ x: number; y: number \}\) => \(chaseOpen \|\| deployOpen\) && ledRingUnder\(point\);/);
    assert.equal((code.match(/if \(ledOwnsClick\((?:e|ev)\.point\)\) return;/g) ?? []).length, 3, "the atlas, NOW and CHASE dot handlers");
    assert.match(code, /if \(t\.length && !ledRingUnder\(ev\.point\)\) openAirport/);
  },

  async "one run at a time: a call made while a run is pending starts nothing and gets its answer"() {
    const { ask, runs } = handRun();
    const a = ask(), b = ask();
    assert.equal(runs.length, 1, "the second call started a run of its own");
    assert.equal(a, b);
    assert.equal(await outcome(a), "pending");
    const fc = { meta: { cycle: "2026-11-25T16:00:00Z" } };
    runs[0].answer(fc);
    assert.equal((await outcome(a)).value, fc);
    assert.equal((await outcome(b)).value, fc);
    // each helper has a slot of its own
    const other = handRun();
    const c = ask(), d = other.ask();
    assert.deepEqual([runs.length, other.runs.length], [2, 1]);
    assert.notEqual(c, d);
  },

  async "once a run has settled the next call starts a new one, and not before"() {
    const { ask, runs } = handRun();
    const first = ask();
    // a caller that asks again the moment it has its answer starts a run: it is never handed the one that ended
    const again = first.then(() => ask());
    for (let i = 0; i < 5; i++) { await outcome(null); assert.equal(ask(), first, "a call while the run was pending got another promise"); }
    assert.equal(runs.length, 1, "a run was started while the first was pending");
    runs[0].answer("16Z");
    assert.deepEqual(await outcome(first), { value: "16Z" });
    assert.equal(runs.length, 2, "the caller that asked again was handed the run that had ended");
    const second = ask();
    assert.notEqual(second, first);
    assert.equal(runs.length, 2, "that call and the one before it share the second run");
    runs[1].answer("17Z");
    assert.deepEqual([await outcome(second), await outcome(again)], [{ value: "17Z" }, { value: "17Z" }]);
    ask();
    assert.equal(runs.length, 3);
  },

  async "a run that rejects frees the slot: every caller waiting gets the failure and the next call starts again"() {
    const { ask, runs } = handRun();
    const a = ask(), b = ask();
    assert.equal(runs.length, 1, "the second call started a run of its own");
    const why = new Error("no connection");
    runs[0].fail(why);
    assert.deepEqual([await outcome(a), await outcome(b)], [{ error: why }, { error: why }]);
    const c = outcome(ask());
    assert.equal(runs.length, 2, "the failed run still holds the slot");
    runs[1].answer("17Z");
    assert.deepEqual(await c, { value: "17Z" });
  },

  async "a run that throws before it returns a promise does not leave the helper stuck"() {
    let n = 0;
    const why = new Error("threw at once");
    const ask = singleFlight(() => { if (++n === 1) throw why; return Promise.resolve(`run ${n}`); });
    let p;
    assert.doesNotThrow(() => { p = ask(); }, "the throw reached the caller instead of a rejected promise");
    assert.deepEqual(await outcome(p), { error: why });
    assert.deepEqual(await outcome(ask()), { value: "run 2" });
    assert.deepEqual(await outcome(ask()), { value: "run 3" });
  },

  async "a run still pending half an hour later still holds the slot: only its own end frees it"() {
    // The route hung for 30 minutes is 60 of the panel's 30 s ticks. A helper
    // that let go of the slot after a while, by a timer of its own or by the
    // clock, would be the defect again at one request per tick, and no other
    // test here lets time pass. The mock clock moves setTimeout, setInterval
    // and Date together; setImmediate, which `outcome` waits on, stays real.
    mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now: at("2026-11-25T18:00:00Z") });
    try {
      const { ask, runs } = handRun();
      const first = ask();
      for (let tick = 1; tick <= 60; tick++) {
        mock.timers.tick(30_000);
        await outcome(null);
        assert.equal(ask(), first, `tick ${tick}: a call while the run was pending got another promise`);
        assert.equal(runs.length, 1, `tick ${tick}: ${runs.length} runs were started while the first was pending`);
      }
      runs[0].answer("16Z");
      assert.deepEqual(await outcome(first), { value: "16Z" });
      assert.notEqual(ask(), first);
      assert.equal(runs.length, 2);
    } finally { mock.timers.reset(); }
  },

  async "a thousand calls in one tick are one run"() {
    const { ask, runs } = handRun();
    const all = Array.from({ length: 1000 }, () => ask());
    assert.equal(runs.length, 1, `${runs.length} runs were started`);
    assert.equal(new Set(all).size, 1);
    runs[0].answer("16Z");
    assert.deepEqual(await outcome(Promise.all(all)), { value: Array(1000).fill("16Z") });
    ask();
    assert.equal(runs.length, 2);
  },

  async "the hourly forecast: one request in flight however often the panel asks (44 requests where the live build made 2)"() {
    // 2026-11-25, headless Chromium, DEPLOY open at base KEAT, /api/forecast
    // made to hang from 17:48Z. The page's 10-minute copy had run out when the
    // 17Z hour ended at 18:00Z, so every 30 s tick from then on filled the
    // launch plan again, and every fill began a request of its own.
    const g = realGetForecast();
    const asks = [];
    for (let tick = 0; tick < 41; tick++) {              // 18:00:00Z to 18:20:00Z
      asks.push(g.getForecast());
      if (tick === 7) asks.push(g.getForecast());        // and an airport's page opened meanwhile (the other caller)
      await outcome(null);
    }
    assert.equal(g.calls.length, 1, `${g.calls.length} requests were begun while the first was pending`);
    assert.equal(g.calls[0].url, "/api/forecast");
    const fc = FC16({ KMFR: FOG16.KMFR });
    g.calls[0].answer(fc);
    // every caller gets the forecast itself, as before
    for (const p of asks) assert.equal((await outcome(p)).value, fc);
    assert.equal(g.calls.length, 1);
    // main.ts: the request is the helper's run, and both callers still go through getForecast
    const code = MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    assert.match(code, /const fetchForecast = singleFlight\(async \(\): Promise<any \| null> => \{/);
    assert.match(code, /async function getForecast\(\): Promise<any \| null> \{\s*if \(fcCache && Date\.now\(\) - fcCache\.at < 600_000\) return fcCache\.data;\s*return fetchForecast\(\);\s*\}/);
    assert.equal((code.match(/fetch\("\/api\/forecast"/g) ?? []).length, 1, "one place asks for /api/forecast");
    assert.equal((code.match(/fetchForecast\(\)/g) ?? []).length, 1, "and one place starts it");
    assert.equal((code.match(/getForecast\(\)/g) ?? []).length, 3, "one definition and two callers");
    assert.match(code, /\n  getForecast\(\)\.then\(\(fc\) => \{\n    const f = fc\?\.airports\?\.\[icao\];/);
    assert.match(code, /const fc = await getForecast\(\);\s*if \(seq !== deployFillSeq \|\| !deployOpen \|\| deployView !== v\) return;/);
    assert.match(/import \{([^}]*)\} from "\.\/plan\.ts";/.exec(MAIN)?.[1] ?? "", /\bsingleFlight\b/);
  },

  async "the forecast's 10-minute copy rule is as it was: when the copy runs out one request replaces it, and a failed one hands every caller null"() {
    const g = realGetForecast(), t0 = g.clock.now;
    const fc = FC16({ KMFR: FOG16.KMFR }), newer = { meta: { cycle: "2026-11-25T17:00:00Z" }, airports: { KMFR: FOG16.KMFR } };
    const first = g.getForecast();
    g.calls[0].answer(fc);
    assert.equal((await outcome(first)).value, fc);
    // under 10 minutes old: the copy, and no request
    g.clock.now = t0 + 10 * MIN - 1;
    assert.equal((await outcome(g.getForecast())).value, fc);
    assert.equal(g.calls.length, 1);
    // 10 minutes old: asked for again, once, however many ask, and nobody is handed the old copy meanwhile
    g.clock.now = t0 + 10 * MIN;
    const second = g.getForecast(), third = g.getForecast();
    assert.equal(g.calls.length, 2, "the copy had run out: one request, not none and not one for each caller");
    assert.equal(await outcome(third), "pending");
    // the new one is kept for 10 minutes from the moment it arrived
    g.clock.now = t0 + 10 * MIN + 40_000;
    g.calls[1].answer(newer);
    assert.equal((await outcome(second)).value, newer);
    assert.equal((await outcome(third)).value, newer);
    g.clock.now = t0 + 20 * MIN + 39_999;
    assert.equal((await outcome(g.getForecast())).value, newer);
    assert.equal(g.calls.length, 2);
    // A request that fails hands back null, whatever copy is on hand, and
    // keeps nothing. Each is one request for both callers, and the next call
    // asks again.
    g.clock.now = t0 + 20 * MIN + 40_000;
    const fails = {
      "an error status": (c) => c.answer({ error: "no forecast yet" }, false),
      "the 503's body under a 200": (c) => c.answer({ error: "no forecast yet" }),
      "a body with no airports": (c) => c.answer({ meta: { cycle: "2026-11-25T17:00:00Z" } }),
      "a body with no cycle": (c) => c.answer({ meta: {}, airports: {} }),
      "a body that is null": (c) => c.answer(null),
      "a body that is not JSON": (c) => c.garbled(),
      "no connection": (c) => c.drop(),
    };
    let n = 2;
    for (const [what, fail] of Object.entries(fails)) {
      const p = g.getForecast(), p2 = g.getForecast();
      assert.equal(g.calls.length, ++n, `before ${what}: one request was expected`);
      fail(g.calls[n - 1]);
      assert.deepEqual([await outcome(p), await outcome(p2)], [{ value: null }, { value: null }], what);
    }
    // the last of them kept nothing either: the next call asks again
    g.getForecast();
    assert.equal(g.calls.length, n + 1, "after no connection the next call did not ask: the failure was kept as the copy");
  },

  async "a forecast request with no whole body in HOURLY_TIMEOUT_MS is aborted and is a failed request"() {
    // one minute: twice the plan's limit, for a body of about 830 KB before compression
    assert.equal(HOURLY_TIMEOUT_MS, 60_000);
    const g = realGetForecast();
    // no answer at all
    const p = g.getForecast(), joined = g.getForecast();
    assert.equal(g.timers.length, 1, `one request, one time limit: ${g.timers.length} were set`);
    assert.equal(g.timers[0].ms, HOURLY_TIMEOUT_MS);
    assert.ok(g.calls[0].signal instanceof AbortSignal, "the request carries no abort signal");
    assert.equal(g.calls[0].signal.aborted, false);
    assert.equal(await outcome(p), "pending");
    g.timers[0].fn();
    assert.equal(g.calls[0].signal.aborted, true, "the limit ran out and the request was not aborted");
    // null, as for no connection, for the caller that began it and the one that joined it
    assert.deepEqual([await outcome(p), await outcome(joined)], [{ value: null }, { value: null }]);
    // the slot is free and nothing was kept: the next call is a new request with a limit of its own
    const q = g.getForecast();
    assert.deepEqual([g.calls.length, g.timers.length, g.timers[1]?.ms], [2, 2, HOURLY_TIMEOUT_MS]);
    assert.notEqual(g.calls[1].signal, g.calls[0].signal);
    // the status line and the headers arrive in time, the body never does: the same limit ends it
    g.calls[1].headersOnly();
    assert.equal(await outcome(q), "pending");
    assert.equal(g.timers[1].cleared, false, "the limit was lifted before the body had arrived");
    g.timers[1].fn();
    assert.deepEqual(await outcome(q), { value: null });
    // With a copy on hand that has run out, the limit still hands null to
    // everyone waiting, never the old copy (falling back to it is a decision
    // not taken here), and keeps nothing: the next call asks again. Once with
    // no answer at all, once with the body under way.
    const h = realGetForecast(), t0 = h.clock.now, fc = FC16({ KMFR: FOG16.KMFR });
    const had = h.getForecast();
    h.calls[0].answer(fc);
    assert.equal((await outcome(had)).value, fc);
    h.clock.now = t0 + 10 * MIN;
    for (const [i, when] of [[1, "before any answer"], [2, "with the body under way"]]) {
      const late = h.getForecast(), also = h.getForecast();
      assert.deepEqual([h.calls.length, h.timers.length], [i + 1, i + 1], `${when}: one new request with a limit of its own was expected (a copy that has run out, or the limit before it, answered instead)`);
      if (i === 2) { h.calls[i].headersOnly(); assert.equal(await outcome(late), "pending"); }
      h.timers[i].fn();
      assert.deepEqual([await outcome(late), await outcome(also)], [{ value: null }, { value: null }], `the limit ran out ${when} and the old copy was handed back`);
      h.clock.now += 1000;
    }
    h.getForecast();
    assert.equal(h.calls.length, 4, "after the limit ran out the next call did not ask: something was kept as the copy");
    // main.ts: the limit is plan.ts's constant, set the way the plan's own request sets its limit
    const code = MAIN.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    assert.match(code, /const ctl = new AbortController\(\);\s*const timer = setTimeout\(\(\) => ctl\.abort\(\), HOURLY_TIMEOUT_MS\);\s*try \{\s*const r = await fetch\("\/api\/forecast", \{ signal: ctl\.signal \}\);/);
    assert.match(/import \{([^}]*)\} from "\.\/plan\.ts";/.exec(MAIN)?.[1] ?? "", /\bHOURLY_TIMEOUT_MS\b/);
    assert.equal((code.match(/HOURLY_TIMEOUT_MS/g) ?? []).length, 2, "imported, and used once");
  },

  async "the forecast request's timer is cleared whatever happens to the request"() {
    const ends = {
      "a forecast": (c) => c.answer(FC16({ KMFR: FOG16.KMFR })),
      "an error status": (c) => c.answer({ error: "no forecast yet" }, false),
      "a body that is not a forecast": (c) => c.answer({ error: "no forecast yet" }),
      "a body that is not JSON": (c) => c.garbled(),
      "no connection": (c) => c.drop(),
      "the limit itself, before any answer": (c, t) => t.fn(),
      "the limit itself, with the body under way": async (c, t) => { c.headersOnly(); await outcome(null); t.fn(); },
    };
    for (const [what, end] of Object.entries(ends)) {
      const g = realGetForecast();
      const p = g.getForecast();
      assert.equal(g.timers.length, 1, `${what}: no time limit was set`);
      assert.equal(g.timers[0].cleared, false, what);
      await end(g.calls[0], g.timers[0]);
      assert.notEqual(await outcome(p), "pending", `the request did not end after ${what}`);
      assert.equal(g.timers[0].cleared, true, `the timer was left running after ${what}`);
      assert.equal(g.timers.length, 1, what);
    }
  },

  "the planner in main.ts names no day from the browser clock"() {
    const a = MAIN.indexOf("// ---------- DEPLOY: where to base for the next two weeks ----------");
    const b = MAIN.indexOf('$("#deploy-btn").addEventListener("click", openDeploy);');
    assert.ok(a > 0 && b > a, "the planner section was not found in main.ts");
    // code only: the comments tell the story of the old labels in the old words
    const planner = MAIN.slice(a, b).split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
    // the two lines that did it until 2026-11-25
    assert.ok(!/===\s*0\s*\?\s*"tomorrow"/.test(planner), 'index 0 is still the literal "tomorrow"');
    assert.ok(!/Date\.now\(\)\s*\+\s*\(d \+ 1\)\s*\*\s*864e5/.test(planner), "a weekday is still counted from Date.now()");
    assert.ok(!/toLocaleDateString\([^)]*weekday/.test(planner), "a weekday is still formatted from a clock date");
    assert.ok(!/["'`>]tomorrow["'`<]/.test(planner), 'a fixed "tomorrow" label is still in the planner');
    assert.ok(/planDayIso\(deployData\?\.meta\?\.day1/.test(planner), "meta.day1 is not read");
    assert.ok(!/data \$\{gen\}/.test(planner), 'the bare "data YYYY-MM-DD" is still printed');
    assert.ok(/planAgeLine\(/.test(planner) && /refetchDue\(/.test(planner), "the age line or the refetch is gone");
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
