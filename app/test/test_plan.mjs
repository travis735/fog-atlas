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

const plan = await import(process.env.PLAN_MODULE ?? "../src/plan.ts");
const {
  planDayIso, isoDateMs, dayWords, dayPart, partWords, dayShort, dayLong, dateSpans, localIso,
  planFreshness, planAgeLine, planBanner, ageWords, refetchDue, retryDelayMs, planProblem, refreshFailWords,
  chanceStatus, noChanceWords, wordOf, wordChance, departure, zoneClock, chainDeparture, launchChance, launchChanceWords,
  PLAN_REFETCH_MS, PLAN_OLD_MS, PLAN_EXPIRED_MS,
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
