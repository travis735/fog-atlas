// DEPLOY planner: which day a number is for, how old the plan is, and what
// stands behind a day-level chance. No DOM, no fetch, no clock of its own
// (callers pass `now`), and only erasable TypeScript, so
// `node app/test/test_plan.mjs` imports it as is.
//
// Why this file exists (readiness review 2026-11-24, finding 25): the panel
// never read the plan's own date. Day 1 was the literal word "tomorrow" and
// later days were weekdays counted from the browser clock, so from local
// midnight until the next build "GO ... tomorrow" was about the morning already
// under way and every weekday was one day late. A plan a day old printed the
// same "tomorrow", and the only clue to its age was a grey "data 2026-11-24".
// It was fetched once per page load and kept for the life of the tab.
//
// Rules:
//   - every day label comes from meta.day1 plus the index, never from the clock
//   - "tomorrow" and "today" are said only when the plan's date IS the
//     viewer's local tomorrow or today; otherwise the date stands alone
//   - a plan says how old it is; one without a build time or a first day is
//     treated as expired (unknown age is stale)
//   - a day the forecast covers in part says from or through which hour

const DAY_MS = 86_400_000;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const PLAN_REFETCH_MS = 15 * 60_000;   // a copy older than this is asked for again (the edge keeps one for 5 min)
export const PLAN_TICK_MS = 30_000;           // ages advance in place this often, as on the CHASE board
export const PLAN_TIMEOUT_MS = 30_000;        // a hung request must become a failed refresh
export const PLAN_OLD_MS = 3 * 3600_000;      // the plan is rebuilt every hour: three missed builds is a notice
export const PLAN_EXPIRED_MS = 26 * 3600_000; // more than a day and one late build: no verdict from it
/** A failed refresh is tried again after 1, 2, 4, 8 and then every 15 minutes. */
export const PLAN_RETRY_MS: readonly number[] = [60_000, 120_000, 240_000, 480_000];
/** The UTC hour a day's forecast must run through to count as the whole day:
 *  the 3-hourly bulletin ends a date at 21Z (forecast/build_deploy.py, `cov`). */
export const WHOLE_DAY_THROUGH_UTC = 21;
export const HOURLY_TIMEOUT_MS = 60_000;      // its body is larger than the plan's (about 830 KB against 490 KB); a hung request must become a failed one
export const HOURLY_OLD_MS = 3 * 3600_000;    // the hourly forecast is issued every hour too
export const HOURLY_DEAD_MS = 8 * 3600_000;   // build_deploy.py refuses a cycle this old; so does the launch time

// ---- dates ----

/** "2026-11-25" -> that date at 00Z in ms. Anything else -> null. */
export function isoDateMs(s: unknown): number | null {
  if (typeof s !== "string") return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return null;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3]);
  // 2026-02-31 parses to March: a date that does not round-trip is not a date
  return new Date(t).toISOString().slice(0, 10) === s ? t : null;
}

const isoOf = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** The date of plan day index `d` (0 = the plan's first day), from meta.day1. */
export function planDayIso(day1: unknown, d: number): string | null {
  const t = isoDateMs(day1);
  return t == null ? null : isoOf(t + d * DAY_MS);
}

/** The viewer's calendar date. `timeZone` is for tests; the page passes none. */
export function localIso(nowMs: number, timeZone?: string): string {
  // en-CA writes dates as YYYY-MM-DD
  return new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone }).format(new Date(nowMs));
}
export function localHour(nowMs: number, timeZone?: string): number {
  return +new Intl.DateTimeFormat("en-GB", { hour: "2-digit", hour12: false, timeZone }).format(new Date(nowMs)) % 24;
}

export type DayRel = "today" | "tomorrow" | "past" | "later";
/** Where a plan date sits against the viewer's own calendar. */
export function dayRel(iso: string, nowMs: number, timeZone?: string): DayRel {
  const day = isoDateMs(iso), today = isoDateMs(localIso(nowMs, timeZone));
  if (day == null || today == null) return "later";
  const n = Math.round((day - today) / DAY_MS);
  return n === 0 ? "today" : n === 1 ? "tomorrow" : n < 0 ? "past" : "later";
}

/** "Wed 25" */
export function dayShort(iso: string): string {
  const t = isoDateMs(iso);
  if (t == null) return iso;
  const d = new Date(t);
  return `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()}`;
}
/** "Wed 25 Nov" */
export function dayLong(iso: string): string {
  const t = isoDateMs(iso);
  return t == null ? iso : `${dayShort(iso)} ${MONTHS[new Date(t).getUTCMonth()]}`;
}
/** "07Z" */
export const zHour = (h: number) => `${String(h).padStart(2, "0")}Z`;

/** The hours of a date the forecast covers, when that is not the whole date. */
export interface DayPart { from?: number | null; through?: number | null }

/** "from 07Z", "through 09Z", "from 07Z through 16Z", or "" for a whole day. */
export function partWords(part?: DayPart | null): string {
  const bits: string[] = [];
  if (part?.from != null) bits.push(`from ${zHour(part.from)}`);
  if (part?.through != null) bits.push(`through ${zHour(part.through)}`);
  return bits.join(" ");
}

/**
 * Which hours of plan day index `d` one airport's numbers stand on.
 *   from     day 1 of a build made after 00Z of that date: only the hours after
 *            the cycle count (meta.days["1"].under_way and cover.from)
 *   through  the forecast stops before the date ends: the airport's own `cov`,
 *            or, in a payload from before `cov`, the day's cover_end_utc
 * Days 3 and later are whole days by construction.
 */
export function dayPart(
  d: number,
  day?: { under_way?: boolean; cover?: { from?: number | null; through?: number | null } | null } | null,
  cov?: number | null,
  coverEndUtc?: number | null,
): DayPart {
  if (d > 1) return {};
  const out: DayPart = {};
  if (day?.under_way && typeof day.cover?.from === "number") out.from = day.cover.from;
  const end = typeof cov === "number" ? cov : typeof coverEndUtc === "number" ? coverEndUtc : null;
  if (end != null && end < WHOLE_DAY_THROUGH_UTC) out.through = end;
  return out;
}

export interface DayWords {
  iso: string; rel: DayRel;
  short: string;   // "Wed 25"
  long: string;    // "Wed 25 Nov"
  part: string;    // "from 07Z", "through 09Z" or ""
  /** for a chip or a table row: "tomorrow, Wed 25", "tonight + Wed 25", "Fri 27" */
  label: string;
  /** to end a sentence with: "tomorrow (Wed 25 Nov)", "on Thu 26 Nov through 09Z" */
  phrase: string;
}

/**
 * How one plan date is named for this viewer at this moment.
 *
 * A date whose first hours are already behind the build (part.from set) is
 * never plain "tomorrow": on a US evening that date's UTC hours started this
 * afternoon, so it is "tonight and tomorrow", with the hour the claim starts.
 *
 * `plain`: the date alone, no today or tomorrow. For an expired plan, whose
 * first day can still be the viewer's "today" hours after its fog was over.
 */
export function dayWords(iso: string, nowMs: number, part?: DayPart | null, timeZone?: string, plain = false): DayWords {
  const rel = dayRel(iso, nowMs, timeZone);
  const short = dayShort(iso), long = dayLong(iso), pw = partWords(part);
  const underWay = part?.from != null;
  const evening = localHour(nowMs, timeZone) >= 17;
  let word = "", label = short;
  if (plain) { /* the date stands alone */ }
  else if (rel === "today") { word = "today"; label = `today, ${short}`; }
  else if (rel === "tomorrow" && !underWay) { word = "tomorrow"; label = `tomorrow, ${short}`; }
  else if (rel === "tomorrow") {
    word = evening ? "tonight and tomorrow" : "today and tomorrow";
    label = `${evening ? "tonight" : "today"} + ${short}`;
  }
  const phrase = word ? `${word} (${long}${pw ? `, ${pw}` : ""})` : `on ${long}${pw ? ` ${pw}` : ""}`;
  return { iso, rel, short, long, part: pw, label, phrase };
}

/** "Fri 27", "Fri 27 to Wed 2", "Wed 25, Fri 27 to Sun 29": runs of plan dates. */
export function dateSpans(isos: string[]): string {
  const ts = isos.map(isoDateMs);
  const runs: string[] = [];
  for (let i = 0; i < isos.length; i++) {
    let j = i;
    while (j + 1 < isos.length && ts[j + 1] != null && ts[j] != null && ts[j + 1]! - ts[j]! === DAY_MS) j++;
    runs.push(j > i ? `${dayShort(isos[i])} to ${dayShort(isos[j])}` : dayShort(isos[i]));
    i = j;
  }
  return runs.join(", ");
}

// ---- age ----

/** "under 1 min", "41 min", "3 h 20 min", "31 h", "4 days" */
export function ageWords(ms: number): string {
  const m = Math.max(0, Math.floor(ms / 60_000));
  if (m < 1) return "under 1 min";
  if (m < 120) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 10) return `${h} h ${m % 60} min`;
  return h < 48 ? `${h} h` : `${Math.floor(h / 24)} days`;
}

const clock = (ms: number, timeZone?: string) =>
  new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone }).format(new Date(ms));
/** "16:25Z" */
export const zClock = (ms: number) => {
  const d = new Date(ms);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}Z`;
};
/** "08:25 local (16:25Z)", with the date in front when it is not the viewer's today */
export function clockWords(ms: number, nowMs: number, timeZone?: string): string {
  const day = localIso(ms, timeZone);
  return `${day === localIso(nowMs, timeZone) ? "" : `${dayLong(day)} `}${clock(ms, timeZone)} local (${zClock(ms)})`;
}
const parseMs = (s: unknown): number | null => {
  if (typeof s !== "string") return null;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
};
/** "the 15Z forecast", with its date when that is not the UTC date of `refMs` */
export function cycleWords(cycle: unknown, refMs: number | null): string {
  const t = parseMs(cycle);
  if (t == null) return "";
  const other = refMs != null && isoOf(t) !== isoOf(refMs);
  return `the ${zHour(new Date(t).getUTCHours())} forecast${other ? ` of ${dayLong(isoOf(t))}` : ""}`;
}

export interface PlanMeta { generated?: unknown; cycle?: unknown; day1?: unknown }
export type FreshWhy = "age" | "day1" | "undated" | "clock";
export interface Freshness {
  /** ok: say the age. old: an amber notice. expired: a red banner and no verdict word. */
  level: "ok" | "old" | "expired";
  why: FreshWhy[];
  builtMs: number | null; ageMs: number | null; day1: string | null;
}

/**
 * How far the plan on hand can be trusted for its age alone.
 *   expired  built more than 26 h ago, or its first day is over (the UTC date
 *            after meta.day1 has begun), or it does not say when it was built
 *            or which day it starts on
 *   old      built more than 3 h ago (it is rebuilt every hour), or "built"
 *            more than 10 minutes in the future: this device's clock is off,
 *            and then "today" and "tomorrow" cannot be trusted either
 */
export function planFreshness(meta: PlanMeta | null | undefined, nowMs: number): Freshness {
  const builtMs = parseMs(meta?.generated);
  const day1 = isoDateMs(meta?.day1) == null ? null : (meta!.day1 as string);
  if (builtMs == null || day1 == null) return { level: "expired", why: ["undated"], builtMs, ageMs: null, day1 };
  const ageMs = nowMs - builtMs;
  const why: FreshWhy[] = [];
  if (ageMs > PLAN_EXPIRED_MS) why.push("age");
  if (nowMs >= isoDateMs(day1)! + DAY_MS) why.push("day1");
  if (why.length) return { level: "expired", why, builtMs, ageMs, day1 };
  if (ageMs < -10 * 60_000) return { level: "old", why: ["clock"], builtMs, ageMs: 0, day1 };
  return { level: ageMs > PLAN_OLD_MS ? "old" : "ok", why: ageMs > PLAN_OLD_MS ? ["age"] : [], builtMs, ageMs: Math.max(0, ageMs), day1 };
}

/** "Plan built 08:25 local (16:25Z) from the 15Z forecast · 25 min old" */
export function planAgeLine(meta: PlanMeta | null | undefined, nowMs: number, timeZone?: string): string {
  const builtMs = parseMs(meta?.generated);
  if (builtMs == null) return "This plan does not say when it was built.";
  const cyc = cycleWords(meta?.cycle, builtMs);
  return `Plan built ${clockWords(builtMs, nowMs, timeZone)}${cyc ? ` from ${cyc}` : ""} · ${ageWords(nowMs - builtMs)} old`;
}

/** The notice (old) or banner (expired) for a plan, as a title and plain lines. Null when the plan is current. */
export function planBanner(f: Freshness): { title: string; lines: string[] } | null {
  if (f.level === "ok") return null;
  // when it was built and from which cycle is the age line, printed just above
  if (f.level === "old") {
    if (f.why.includes("clock")) {
      return { title: "CHECK THIS DEVICE'S CLOCK.", lines: [
        "This plan says it was built later than this device's clock reads now.",
        "Ages on this panel, and the words today and tomorrow, may be wrong. The dates are the plan's own." ] };
    }
    return { title: `THIS PLAN IS ${ageWords(f.ageMs ?? 0).toUpperCase()} OLD.`, lines: [
      "A new plan is built every hour. The newer forecasts are not in this one." ] };
  }
  const lines: string[] = [];
  if (f.why.includes("undated")) lines.push("It does not say when it was built or which day it starts on.");
  if (f.why.includes("day1") && f.day1) lines.push(`Its first day, ${dayLong(f.day1)}, is over.`);
  if (f.why.includes("age")) lines.push(`It is ${ageWords(f.ageMs ?? 0)} old and nothing newer has arrived.`);
  lines.push("No GO, MARGINAL or SCRAP is shown from an expired plan.");
  return { title: "PLAN EXPIRED. DO NOT PLAN FROM IT.", lines };
}

// ---- refresh ----

/** True when the copy on hand (fetched at `fetchedAtMs`, null = none) should be asked for again. */
export const refetchDue = (fetchedAtMs: number | null, nowMs: number) =>
  fetchedAtMs == null || nowMs - fetchedAtMs >= PLAN_REFETCH_MS;

/** How long to wait after the n-th failed refresh in a row (n from 1). */
export const retryDelayMs = (failures: number) =>
  PLAN_RETRY_MS[Math.max(1, failures) - 1] ?? PLAN_REFETCH_MS;

/**
 * One run at a time, shared by whoever asks: a call made while a run is
 * pending gets that run's promise and starts nothing; once it has settled,
 * either way, the next call starts a new run. The hourly forecast had no such
 * guard (main.ts getForecast): the panel's 30 s tick asked for it again while
 * the first request was still pending, and with the route hung for 30 minutes
 * that was 44 requests where the live build made 2.
 */
export function singleFlight<T>(run: () => Promise<T>): () => Promise<T> {
  let pending: Promise<T> | null = null;
  // inside an async function a run that throws before it returns a promise is
  // a failed run like any other; the slot is freed before any caller reads the answer
  return () => (pending ??= (async () => run())().finally(() => { pending = null; }));
}

/**
 * Why a parsed /api/deploy body is not a plan, or null when it is one. The
 * function answers 503 with {"error": "no deploy data yet"}: that parsed, was
 * kept for the session and the panel printed "No expected fog within range of
 * any base" as if it had looked (2026-11-24).
 */
export function planProblem(j: unknown): string | null {
  if (j == null || typeof j !== "object" || Array.isArray(j)) return "the answer was not a plan";
  const o = j as { error?: unknown; airports?: unknown };
  if (o.airports == null || typeof o.airports !== "object" || Array.isArray(o.airports)) {
    return typeof o.error === "string" ? `the planner said "${o.error.slice(0, 80)}"` : "the answer was not a plan";
  }
  return Object.keys(o.airports).length ? null : "the plan held no airports";
}

/** "Refresh failed 18:40 local (HTTP 503). Still showing the plan built ... Next try 18:41 local." */
export function refreshFailWords(
  fail: { at: number; why: string }, nextAtMs: number | null, meta: PlanMeta | null | undefined, nowMs: number, timeZone?: string,
): string {
  const builtMs = parseMs(meta?.generated);
  const next = nextAtMs != null ? ` Next try ${clock(nextAtMs, timeZone)} local.` : "";
  if (!meta) return `The plan did not load (${fail.why}), tried ${clock(fail.at, timeZone)} local.${next}`;
  return `Refresh failed ${clock(fail.at, timeZone)} local (${fail.why}). Still showing the plan built ` +
    `${builtMs != null ? clockWords(builtMs, nowMs, timeZone) : "at a time it does not give"}.${next}`;
}

// ---- the day-level chance ----

export type ChanceStatus = "fresh" | "carried" | "none";
/**
 * Whose day-level chance an airport shows on plan day index `d`:
 *   none     the payload has no number
 *   carried  the number came from an earlier cycle, named in pfrom
 *   fresh    it is this build's own
 */
export function chanceStatus(a: { p?: (number | null)[]; pfrom?: (string | null)[] } | null | undefined, d: number): { status: ChanceStatus; from: string | null } {
  if (a?.p?.[d] == null) return { status: "none", from: null };
  const from = a.pfrom?.[d];
  return typeof from === "string" && from ? { status: "carried", from } : { status: "fresh", from: null };
}

interface Band { n?: number; shown?: number | null; obs?: number | null }
/**
 * Why this build has no day-level chance for a day, in a few words. `why` is
 * meta.days[k].chance.why (or meta.daycal.why[k] in an older payload); `failed`
 * is the check the map did not pass at this cycle hour.
 */
export function noChanceWords(why: string | null | undefined, cycleHour?: number | null, failed?: { ge50?: Band | null } | null, tooOld?: number | null): string {
  const hh = typeof cycleHour === "number" ? `the ${zHour(cycleHour)} forecast` : "this forecast";
  let s: string;
  switch (why) {
    case "hour": {
      const b = failed?.ge50;
      // tenths first: 0.598 - 0.493 is 10.499999999999998 in floating point and would print 10
      const g = b && typeof b.shown === "number" && typeof b.obs === "number" ? Math.round(Math.round(1000 * (b.shown - b.obs)) / 10) : null;
      s = g != null && Math.abs(g) > 2
        ? `built from ${hh}, the day-level chance ran about ${Math.abs(g)} points ${g > 0 ? "high" : "low"} at 50% or more on its check, so it is not published`
        : `the day-level chance is not checked for a plan built from ${hh}`;
      break;
    }
    case "season": s = "the day-level chance passed its check over the year and missed in November to February, so it is held in those months"; break;
    case "reach": s = `${hh} is older than any the day-level chance is checked on`; break;
    case "hours": s = `${hh} arrived without all of that day's hours`; break;
    default: s = "this build did not publish one";
  }
  return `${s[0].toUpperCase()}${s.slice(1)}.${tooOld ? " The last one published is more than 24 h old and was dropped." : ""}`;
}

export type Word = "GO" | "MARGINAL" | "SCRAP";
export const wordOf = (p: number): Word => (p >= 0.5 ? "GO" : p >= 0.25 ? "MARGINAL" : "SCRAP");

/**
 * The chance the verdict word is read from. A number in an area where days
 * like it ran HIGH on their check is held back by that many points before the
 * 50% and 25% bars are applied, so the word is never stronger than the line
 * printed under it. A number that ran low is never raised: this only weakens.
 * The percent on screen stays the published one.
 */
export function wordChance(p: number, gapPoints: number | null | undefined): number {
  return typeof gapPoints === "number" && gapPoints > 0 ? Math.max(0, p - gapPoints / 100) : p;
}

// ---- times a pilot acts on ----

/**
 * A departure time against the clock. A time already behind the clock is never
 * handed back as a time to act on: the caller prints `passed` instead.
 */
export function departure(byMs: number, nowMs: number): { passed: boolean; inMs: number } {
  return { passed: byMs < nowMs, inMs: byMs - nowMs };
}

/**
 * A base row's departure from the CHASE base, worked out over EVERY hour of
 * one field's fog window, not only its first.
 *
 * `hours`: the forecast hours (ms, ascending) at which the field clears the
 * launch plan's bar. `chainMs`: home to base, the turn there, and base to
 * field. Hours no more than `gapMs` apart are one window.
 *   by      the window has not opened for this chain: leave by `byMs` to be
 *           there as it opens (`openMs`), with the buffer
 *   open    it is open, or opens before the chain could be there, and leaving
 *           by `byMs` still arrives in its last hour (`endMs`)
 *   missed  no hour of any window can still be made; `byMs` was the last
 *           departure that could have
 *   none    the field has no such hour
 * Until 2026-11-25 the row took the first hour the BASE could reach and then
 * took the reposition leg off it. With a window open that time was behind the
 * clock for any base more than a few minutes from home: 26 of 28 rows read
 * "departure time passed" while hours of the same window could still be
 * reached (KPDT's fog ran to 08:00; leaving as late as 05:29 still arrived in it).
 */
export function chainDeparture(hours: readonly number[], chainMs: number, nowMs: number, bufferMs = 15 * 60_000, gapMs = 3 * 3600e3):
  { kind: "by" | "open" | "missed" | "none"; byMs: number | null; openMs: number | null; endMs: number | null } {
  const hs = hours.filter((h) => Number.isFinite(h)).sort((x, y) => x - y);
  if (!hs.length) return { kind: "none", byMs: null, openMs: null, endMs: null };
  const runs: number[][] = [];
  for (const h of hs) {
    const last = runs[runs.length - 1];
    if (last && h - last[last.length - 1] <= gapMs) last.push(h); else runs.push([h]);
  }
  for (const run of runs) {
    const start = run[0], end = run[run.length - 1];
    if (start - chainMs - bufferMs >= nowMs) return { kind: "by", byMs: start - chainMs - bufferMs, openMs: start, endMs: end };
    if (end - chainMs >= nowMs) {
      return { kind: "open", byMs: end - chainMs, openMs: run.find((h) => h - chainMs >= nowMs) ?? end, endMs: end };
    }
  }
  const end = hs[hs.length - 1];
  return { kind: "missed", byMs: end - chainMs, openMs: null, endMs: end };
}

/** "Wed 03:54 PST": a clock time with its weekday and zone, in `timeZone`. */
export function zoneClock(ms: number, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone, timeZoneName: "short",
  }).formatToParts(new Date(ms));
  const get = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  // "24:05" is how some engines write five past midnight in a 24-hour clock
  return `${get("weekday")} ${get("hour") === "24" ? "00" : get("hour")}:${get("minute")} ${get("timeZoneName")}`.trim();
}

/**
 * The chance a launch line prints for one field: that of the hour the launch
 * reaches (`at`), and the highest from that hour to the end of the same fog
 * window (`top`, at `topMs`). Never the forecast's 48-hour peak. Until
 * 2026-11-25 WHEELS UP printed that peak: at 16:40Z it read "fog (<1 mi) 92%"
 * for KVIS, which was the 16Z hour, behind the clock. The hour the launch
 * reached was 44% and the next 5%.
 *
 * `fhrs`, `p`: the field's forecast hours (ascending) and the chance in each.
 * The window is the launch plan's own: hours at `thr` or more, inside its
 * `maxFhr`, no more than `gapMs` apart (chainDeparture's rule).
 */
export function launchChance(fhrs: readonly number[], p: readonly number[], cycMs: number, openMs: number, thr: number,
  maxFhr = 30, gapMs = 3 * 3600e3): { at: number | null; top: number | null; topMs: number | null } {
  let at: number | null = null, top: number | null = null, topMs: number | null = null, last = openMs;
  for (let i = 0; i < fhrs.length; i++) {
    const t = cycMs + fhrs[i] * 3600e3;
    if (t < openMs || fhrs[i] > maxFhr || !(p[i] >= thr)) continue;
    if (t - last > gapMs) break;
    last = t;
    if (t === openMs) at = p[i];
    if (top == null || p[i] > top) { top = p[i]; topMs = t; }
  }
  return { at, top, topMs };
}

/** "44% at Tue 10:00 PST", with ", peak 90% at Tue 12:00 PST" when a later hour of that window is higher. */
export function launchChanceWords(c: ReturnType<typeof launchChance>, openMs: number, timeZone?: string): string {
  if (c.at == null) return "";
  return `${c.at}% at ${zoneClock(openMs, timeZone)}`
    + (c.top != null && c.topMs != null && c.top > c.at ? `, peak ${c.top}% at ${zoneClock(c.topMs, timeZone)}` : "");
}

// ---- the NEXT 48 H list ----

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** The weekday, two-digit clock hour and calendar date of an instant in `timeZone`. */
function zoneHour(ms: number, timeZone?: string): { day: string; hh: string; date: string } {
  if (!Number.isFinite(ms)) return { day: "", hh: "", date: "" };
  const opts = { weekday: "short", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false } as const;
  let f: Intl.DateTimeFormat;
  // No zone, or a name the engine does not know: the viewer's own zone, as
  // zoneClock does without one. A list line must never throw over it.
  try { f = new Intl.DateTimeFormat("en-US", { ...opts, timeZone: timeZone || undefined }); }
  catch { f = new Intl.DateTimeFormat("en-US", opts); }
  const parts = f.formatToParts(new Date(ms));
  const get = (t: string) => parts.find((x) => x.type === t)?.value ?? "";
  // "24" is how some engines write the midnight hour of a 24-hour clock
  return { day: get("weekday"), hh: get("hour") === "24" ? "00" : get("hour"), date: `${get("year")}-${get("month")}-${get("day")}` };
}

/** "Fri 01": the weekday and clock hour of a forecast hour, in the FIELD's zone. */
export function zoneDayHour(ms: number, timeZone?: string): string {
  const z = zoneHour(ms, timeZone);
  return `${z.day} ${z.hh}`.trim();
}

/**
 * "Thu 08–09", or "Thu 22–Fri 07" when the last hour falls on another date in
 * `timeZone`: a fog window by the weekday of its first hour. `single`: a
 * one-hour window is "Fri 07", not "Fri 07–07".
 */
export function windowWords(fromMs: number, toMs: number, timeZone?: string, single = false): string {
  const a = zoneHour(fromMs, timeZone), b = zoneHour(toMs, timeZone);
  if (single && fromMs === toMs) return `${a.day} ${a.hh}`.trim();
  return `${a.day} ${a.hh}–${b.date === a.date ? "" : `${b.day} `}${b.hh}`.trim();
}

/** A run of forecast hours at or above a field's bar. `top` is the highest chance among ITS hours. */
export interface FogWindow { fromMs: number; toMs: number; fromFhr: number; toFhr: number; top: number }

export interface FogAhead {
  /** the highest chance of visibility under 1 mile among the hours that have not ended */
  peak: number;
  /** the bar a window's hours clear: max(15, 40% of `peak`) */
  thr: number;
  /** the first window still ahead; null when `peak` is under 15 (the quiet line) */
  first: FogWindow | null;
  /** its length in hours, as the list has always counted it */
  durH: number;
  /** the forecast hour after the first window; null when it runs to the last hour of the forecast */
  clearsMs: number | null;
  /** the strongest window after the first, or null */
  later: FogWindow | null;
  /** the highest chance under 1/2 mile and under 1/4 mile among the hours of the FIRST window */
  half: number; quarter: number;
  /** when the earliest hour counted here ends: the line is this one until then, and has to be worked out again from then on */
  untilMs: number;
}

/**
 * One line of the NEXT 48 H list: the first fog window STILL AHEAD at a field,
 * with numbers that belong to that window.
 *
 * Until 2026-11-25 the line took the field's peak over the whole 48 hours,
 * drew the window from the first run of hours at 40% of it, and printed that
 * peak beside the window, with clock hours and no day. At 17:41Z the 16Z
 * forecast read "KMFR fog (<1 mi) 47% · 08–09 local (≈2h), clears ~10 · again
 * next night". Thursday 08 and 09 PST were 25% and 19%; the 47%, and the
 * pill's 42%, were Friday 07 PST, 47 hours out, while WHEELS UP right above
 * said 25% at Thu 08:00 PST. "KCVO 35% · 01–07 local" was Friday, 39 hours
 * out, with no day on the line: 51 of 75 lines started 24 h or more ahead and
 * so read a day early. Hours already behind the clock were counted too.
 *
 * `fhrs`: the field's forecast hours, ascending. `p`: for each, the chance of
 * visibility under 1 mile, under 1/2 mile and under 1/4 mile.
 *   - an hour counts until it ends (its valid time plus one hour), and not after
 *   - `peak` is the highest under-1-mile chance among those hours; under 15 the
 *     line is the quiet one
 *   - a window is a run of them at max(15, 40% of peak) or more, each no more
 *     than `gapH` after the one before (hourly at short leads, 3-hourly later)
 *   - `first.top`, `half` and `quarter` are maxima over the FIRST window's own hours
 *   - `later` is the window after it with the highest `top`, the earliest on a tie
 *   - `untilMs` is the end of the earliest hour counted. Nothing here changes
 *     before it. The page puts it on the line, and its tick fills the list
 *     again once it has passed: without it a line kept an ended hour's numbers
 *     on screen for up to 15 minutes (KMWH read 81% from 18:00Z to 18:15Z on
 *     2026-11-25, the 17Z hour's chance)
 * null: no forecast hour is still ahead (or there is no forecast to read).
 */
export function fogAhead(fhrs: readonly number[], p: readonly (readonly number[])[], cycMs: number, nowMs: number, gapH = 3): FogAhead | null {
  if (!Array.isArray(fhrs) || !Array.isArray(p)) return null;
  const hs: { fhr: number; ms: number; r: readonly number[] }[] = [];
  for (let i = 0; i < fhrs.length; i++) {
    const fhr = fhrs[i], r = p[i];
    if (!isNum(fhr) || !Array.isArray(r) || !isNum(r[0])) continue;   // an hour without a number is not an hour
    const ms = cycMs + fhr * 3600e3;
    if (ms + 3600e3 > nowMs) hs.push({ fhr, ms, r });
  }
  if (!hs.length) return null;
  hs.sort((x, y) => x.fhr - y.fhr);
  const untilMs = hs[0].ms + 3600e3;
  const peak = Math.max(...hs.map((h) => h.r[0]));
  const thr = Math.max(15, peak * 0.4);
  if (peak < 15) return { peak, thr, first: null, durH: 0, clearsMs: null, later: null, half: 0, quarter: 0, untilMs };
  const runs: [number, number][] = [];   // first and last index in `hs`
  for (let i = 0; i < hs.length; i++) {
    if (hs[i].r[0] < thr) continue;
    const run = runs[runs.length - 1];
    if (run && run[1] === i - 1 && hs[i].fhr - hs[i - 1].fhr <= gapH) run[1] = i; else runs.push([i, i]);
  }
  const top = (k: number, [a, b]: [number, number]) => Math.max(...hs.slice(a, b + 1).map((h) => (isNum(h.r[k]) ? h.r[k] : 0)));
  const win = (run: [number, number]): FogWindow => ({
    fromMs: hs[run[0]].ms, toMs: hs[run[1]].ms, fromFhr: hs[run[0]].fhr, toFhr: hs[run[1]].fhr, top: top(0, run),
  });
  const first = win(runs[0]);
  let later: FogWindow | null = null;
  for (const run of runs.slice(1)) {
    const w = win(run);
    if (!later || w.top > later.top) later = w;
  }
  return {
    peak, thr, first, later,
    // an hourly point stands for one hour and a 3-hourly one for three
    durH: Math.max(1, first.toFhr - first.fromFhr + (first.fromFhr <= 25 ? 1 : 3)),
    clearsMs: runs[0][1] < hs.length - 1 ? hs[runs[0][1] + 1].ms : null,
    half: top(1, runs[0]), quarter: top(2, runs[0]),
    untilMs,
  };
}

export interface FogAheadWords {
  quiet: boolean;
  /** "fog (<1 mi) 25%", or "quiet next 48 h (peak 6%)" */
  head: string;
  /** "Thu 08–09": the first window, by its day in the field's zone */
  window: string;
  /** "Thu 08–09 local (≈2h), clears ~10" */
  when: string;
  /** "later Fri 01–07: 47%", or "" without a later window */
  later: string;
  /** red: under 1/4 mile at 25% or more in the first window; amber: under 1/2 mile; dim: neither */
  pill: "red" | "amber" | "dim";
  /** "deepens to <¼ mi: 31%", "deepens to <½ mi: 34%", "stays marginal" */
  pillWords: string;
}

/** The words of one NEXT 48 H line, as plain text, with every time in `timeZone` (the field's). */
export function fogAheadWords(a: FogAhead, timeZone?: string): FogAheadWords {
  if (!a.first) return { quiet: true, head: `quiet next 48 h (peak ${a.peak}%)`, window: "", when: "", later: "", pill: "dim", pillWords: "" };
  const window = windowWords(a.first.fromMs, a.first.toMs, timeZone);
  const clears = a.clearsMs == null ? "" : `, clears ~${zoneHour(a.clearsMs, timeZone).hh}`;
  const later = a.later ? `later ${windowWords(a.later.fromMs, a.later.toMs, timeZone, true)}: ${a.later.top}%` : "";
  // one severity ladder, each rung labeled by its depth: never two bare "fog %"
  const [pill, pillWords]: [FogAheadWords["pill"], string] = a.quarter >= 25 ? ["red", `deepens to <¼ mi: ${a.quarter}%`]
    : a.half >= 25 ? ["amber", `deepens to <½ mi: ${a.half}%`] : ["dim", "stays marginal"];
  return { quiet: false, head: `fog (<1 mi) ${a.first.top}%`, window, when: `${window} local (≈${a.durH}h)${clears}`, later, pill, pillWords };
}
