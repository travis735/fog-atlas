// LED approach lights on the map and on the CHASE and DEPLOY boards: which
// runway end a board shows, the three-way choice, and every word about it.
// Pure: no DOM, no fetch, no clock of its own (`now` is passed in), so
// app/test/test_led.mjs runs it under node.
//
// Why it exists (readiness review 2026-11-24, item 3): the owner flight-tests
// an infrared EFVS, and LED approach lights may not show on it. chase.json
// (pipeline/build_chase.py) carries "led":1 on each runway end an FAA airport
// remark names, "led_ir":1 where the FAA's list says the lamps have infrared
// emitters, and a top-level "led" block listing every LED airport, on the
// board or not.
//
// Three rules live here because the map and both boards must agree on them:
//  - ONE function picks the runway end a board shows (endShown). CHASE and
//    DEPLOY each carried their own copy of the filter; a term added to one and
//    not the other would have the planner rank a field the board hides.
//  - An end with no "led" key is "not flagged", never "conventional": the FAA
//    remark covers FAA-owned systems only and can follow the installation by
//    months. A curated Canadian end has no LED source at all. Nothing here
//    returns a word for "not LED".
//  - A build with no led block applies no LED choice (ledChoiceIn): "only LED
//    ends" against data with no flags would empty both boards without a word.

export interface ChaseEnd {
  e: string; len: number; als: string | null; rvr: string | null;
  ils: string | null; lpv: 0 | 1; tier: number;
  cur?: 0 | 1;  // 1 = curated (AIP Canada/CFS research), not FAA NASR: no LED data
  led?: 1;      // an FAA airport remark names this end as having LED approach lights
  led_ir?: 1;   // only beside led: the FAA's list says those lamps have infrared emitters
  // The airport has an FAA LED remark that names no runway the build could
  // place (a typo such as "RWY 63", or the 2025 wording that named none), and
  // this end has approach lights. Until 2026-11-25 such an airport was ringed
  // on the map while every row of it read "not flagged" and "avoid LED ends"
  // kept it.
  led_unk?: 1;
  // On the FAA's list of LED installations and no longer named by a remark
  // (KMEM 18L: listed 14 Jan 2025 as LED with infrared emitters, remark
  // removed May 2026). Not LED for the map or for "only LED ends", which show
  // what the FAA says today; left out by "avoid LED ends", and said on the row.
  led_was?: 1;
}

/** The saved CHASE filters an end is held against (main.ts's ChasePrefs has these). */
export interface EndFilters { als: readonly string[]; minLen: number; rvrReq: boolean; maxTier: number }

/** The ALS chips that name one system; the "OTHER" chip is every system not in this list. */
export const ALS_NAMED: readonly string[] = ["ALSF2", "ALSF1", "MALSR", "SSALR", "MALSF"];
export const ALS_LABEL: Record<string, string> = {
  ALSF2: "ALSF-2", ALSF1: "ALSF-1", MALSR: "MALSR", SSALR: "SSALR",
  MALSF: "MALSF", MALS: "MALS", SALS: "SALS", SALSF: "SALSF",
  ODALS: "ODALS", RLLS: "RLLS", OTHER: "other",
};

/** show all (default) / avoid LED ends / only LED ends */
export type LedChoice = "all" | "avoid" | "only";
export const LED_CHOICES: readonly LedChoice[] = ["all", "avoid", "only"];
export const LED_CHOICE_LABEL: Record<LedChoice, string> = {
  all: "show all", avoid: "avoid LED ends", only: "only LED ends",
};
/** Anything that is not one of the two filters is "show all": an old or hand-edited saved value must not hide airports. */
export const ledChoiceOf = (v: unknown): LedChoice => (v === "avoid" || v === "only" ? v : "all");

type LedFlags = { led?: unknown; led_ir?: unknown; led_unk?: unknown; led_was?: unknown };
/**
 * LED by what the FAA says today. LED + IR counts as LED: the lamps are LED,
 * and the infrared emitters rest on one FAA list of January 2025. So does an
 * end with approach lights at an airport whose LED remark names no runway.
 */
export const endHasLed = (e: LedFlags): boolean => e.led === 1 || e.led_ir === 1 || e.led_unk === 1;
/** What "avoid LED ends" steers clear of: the above, and an end the FAA has listed as LED and no longer has a remark for. */
export const endMayBeLed = (e: LedFlags): boolean => endHasLed(e) || e.led_was === 1;

export const LED_TAG_UNK = "LED, runway not named";
export const LED_TAG_WAS = "LED listed, no remark";
export type LedTag = "LED" | "LED + IR" | typeof LED_TAG_UNK | typeof LED_TAG_WAS;
/** Every tag, in the order a row's note lists them. */
export const LED_TAGS: readonly LedTag[] = ["LED", "LED + IR", LED_TAG_UNK, LED_TAG_WAS];
/** The tag a row prints beside a runway end, or null. Null is "not flagged" or "unknown", never "conventional". */
export const ledTag = (e: LedFlags): LedTag | null =>
  e.led_ir === 1 ? "LED + IR" : e.led === 1 ? "LED" : e.led_unk === 1 ? LED_TAG_UNK : e.led_was === 1 ? LED_TAG_WAS : null;

/** The infrastructure filters, as CHASE and DEPLOY both applied them before the LED choice existed. */
export function endMeetsFilters(e: ChaseEnd, f: EndFilters): boolean {
  return e.als != null &&
    (f.als.includes(e.als) || (f.als.includes("OTHER") && !ALS_NAMED.includes(e.als))) &&
    e.len >= f.minLen &&
    e.tier <= f.maxTier &&
    (!f.rvrReq || e.rvr != null);
}

/**
 * avoid: an end the FAA neither says is LED nor has listed as LED (a curated
 * end has no flag at all, so it stays). only: an end the FAA says is LED
 * today. An end that is only on the old list is in neither: avoiding means
 * avoiding the doubt, and asking for LED means asking for what is known.
 */
export const endMeetsLed = (e: ChaseEnd, choice: LedChoice): boolean =>
  choice === "all" || (choice === "only" ? endHasLed(e) : !endMayBeLed(e));

/** Every end of one airport that meets the filters and the LED choice, best first: lowest go-around tier, then longest. */
export function endsMeeting(ends: readonly ChaseEnd[] | null | undefined, f: EndFilters, choice: LedChoice): ChaseEnd[] {
  if (!Array.isArray(ends)) return [];
  return ends.filter((e) => endMeetsFilters(e, f) && endMeetsLed(e, choice))
    .sort((x, y) => x.tier - y.tier || y.len - x.len);
}

/**
 * The runway end a board shows for one airport, or null when the airport is
 * off the board. Under "avoid" an airport stays when another end still meets
 * the filters, on that end; it drops out when only LED ends do. "only" is the
 * reverse.
 */
export const endShown = (ends: readonly ChaseEnd[] | null | undefined, f: EndFilters, choice: LedChoice): ChaseEnd | null =>
  endsMeeting(ends, f, choice)[0] ?? null;

// ---- the led block of chase.json ----

export interface LedEnd { e: string; als: string | null; board: boolean; tag: LedTag; irAsof: string | null }
export interface LedAirport {
  id: string; name: string; st: string; lat: number; lon: number;
  atlas: boolean;       // in airports.json
  board: boolean;       // at least one of its LED ends is a CHASE runway end
  ends: LedEnd[];
  nonstd: boolean;      // the remark is not the FAA sentence (Pope AAF)
  rmk: string | null;   // with nonstd: the remark's own words
  rwyUnknown: boolean;  // the remark names no runway
}
export interface LedBlock {
  cycle: string | null;           // FAA effective date of the cycle the remarks were read from
  airports: LedAirport[];
  byId: Map<string, LedAirport>;
  nEnds: number;
  nIr: number;
  irAsof: string | null;          // date of the list the infrared emitters rest on
  // runway ends on the FAA's list of LED installations with no current remark (chase.json led.ir_unused)
  listed: { id: string; e: string; asof: string | null; board: boolean }[];
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const isoDay = (v: unknown): string | null => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : null);

/**
 * chase.json's led block, checked. Null when this build has none: a chase.json
 * from before 2026-11-25, or a block with no usable airport. The counts are
 * taken from the list itself, not from the block's n_ fields, so the number a
 * chip prints is the number of rings drawn.
 */
export function readLedBlock(chase: unknown): LedBlock | null {
  const led = (chase as { led?: unknown } | null | undefined)?.led as Record<string, unknown> | null | undefined;
  if (!led || typeof led !== "object" || !Array.isArray(led.airports)) return null;
  const airports: LedAirport[] = [];
  const byId = new Map<string, LedAirport>();
  let nEnds = 0, nIr = 0, irAsof: string | null = null;
  for (const raw of led.airports as unknown[]) {
    const a = raw as Record<string, unknown> | null;
    if (!a || typeof a !== "object") continue;
    const id = str(a.id);
    const lat = a.lat, lon = a.lon;
    // an entry that cannot be placed cannot be ringed, so it is not counted either
    if (!id || byId.has(id) || typeof lat !== "number" || typeof lon !== "number" || !Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const ends: LedEnd[] = [];
    for (const rawEnd of Array.isArray(a.ends) ? a.ends as unknown[] : []) {
      const x = rawEnd as Record<string, unknown> | null;
      const e = str(x?.e);
      if (!x || !e || ends.some((y) => y.e === e)) continue;
      const ir = x.led_ir === 1;
      const asof = ir ? isoDay(x.ir_asof) : null;
      ends.push({ e, als: str(x.als) || null, board: x.board === 1, tag: ir ? "LED + IR" : "LED", irAsof: asof });
      if (ir) { nIr++; if (asof && (!irAsof || asof > irAsof)) irAsof = asof; }
    }
    nEnds += ends.length;
    const one: LedAirport = {
      id, name: str(a.name), st: str(a.st), lat, lon,
      atlas: a.atlas === 1, board: a.board === 1, ends,
      nonstd: a.nonstd === 1, rmk: str(a.rmk) || null, rwyUnknown: a.rwy_unknown === 1 || !ends.length,
    };
    airports.push(one);
    byId.set(id, one);
  }
  if (!airports.length) return null;
  const listed: LedBlock["listed"] = [];
  for (const raw of Array.isArray(led.ir_unused) ? led.ir_unused as unknown[] : []) {
    const x = raw as Record<string, unknown> | null;
    if (x && str(x.id) && str(x.e)) listed.push({ id: str(x.id), e: str(x.e), asof: isoDay(x.asof), board: x.board === 1 });
  }
  return { cycle: isoDay(led.cycle), airports, byId, nEnds, nIr, irAsof, listed };
}

/** The LED choice in effect: the saved one when this build has LED data, "show all" when it has none. */
export const ledChoiceIn = (block: LedBlock | null | undefined, saved: unknown): LedChoice =>
  (block ? ledChoiceOf(saved) : "all");

/**
 * The LED ends at one airport, for a row's tag and note: the led block's list
 * (it knows ends that are not CHASE runway ends), plus any end that carries
 * the flag without being in the list.
 */
export function ledEndsAt(block: LedBlock | null | undefined, id: string, ends: readonly ChaseEnd[] | null | undefined): { e: string; tag: LedTag }[] {
  const out: { e: string; tag: LedTag }[] = (block?.byId.get(id)?.ends ?? []).map((x) => ({ e: x.e, tag: x.tag }));
  for (const e of Array.isArray(ends) ? ends : []) {
    const tag = ledTag(e);
    if (tag && !out.some((x) => x.e === e.e)) out.push({ e: e.e, tag });
  }
  return out;
}

/**
 * The note beside the end a row shows, naming the OTHER ends at the airport
 * that have LED lights: "LED on 07R", "LED + IR on 28L", "also LED on 13".
 * Without it the ring on the map and a row with no tag would argue.
 */
export function ledOtherWords(shown: { e: string; led?: unknown; led_ir?: unknown } | null, ledEnds: readonly { e: string; tag: LedTag }[]): string {
  const others = ledEnds.filter((x) => x.e !== shown?.e);
  if (!others.length) return "";
  const parts = LED_TAGS.flatMap((tag) => {
    const es = others.filter((x) => x.tag === tag).map((x) => x.e);
    // "LED on 07R", "LED + IR on 28L"; the two rarer tags are sentences of their own
    return !es.length ? [] : tag === LED_TAG_UNK ? [`LED, runway not named: ${es.join(", ")} have approach lights`]
      : tag === LED_TAG_WAS ? [`${es.join(", ")} LED on the FAA's list, no current remark`] : [`${tag} on ${es.join(", ")}`];
  });
  return (shown && endHasLed(shown) ? "also " : "") + parts.join("; ");
}

// ---- how many airports the choice removed ----

export interface LedImpact {
  base: number;       // airports on the board under "show all"
  left: number;       // airports on it under the choice
  removed: string[];  // on it under "show all", off it under the choice
  moved: { id: string; from: string; to: string }[]; // still on it, on another runway end
}

/** `all` and `cur` are the same board under "show all" and under the choice: airport id and the end shown. */
export function ledImpact(all: readonly { id: string; e: string }[], cur: readonly { id: string; e: string }[]): LedImpact {
  const now = new Map(cur.map((c) => [c.id, c.e]));
  const removed: string[] = [];
  const moved: LedImpact["moved"] = [];
  for (const a of all) {
    const e = now.get(a.id);
    if (e == null) removed.push(a.id);
    else if (e !== a.e) moved.push({ id: a.id, from: a.e, to: e });
  }
  removed.sort();
  moved.sort((x, y) => (x.id < y.id ? -1 : x.id > y.id ? 1 : 0));
  return { base: all.length, left: cur.length, removed, moved };
}

const some = (xs: readonly string[], max: number, sep = ", "): string =>
  xs.length <= max ? xs.join(sep) : `${xs.slice(0, max - 2).join(sep)} and ${xs.length - (max - 2)} more`;
const airportsN = (n: number) => `${n.toLocaleString("en-US")} ${n === 1 ? "airport" : "airports"}`;

export const LED_IR_COUNTS = "Ends marked LED + IR count as LED for this choice.";
export const LED_WAS_COUNTS = "An end on the FAA's list of LED installations with no current remark is left out by avoid and not counted by only.";

/**
 * What the choice did to a board, in sentences printed next to the control, so
 * a short board is never a mystery. `scope` says which airports were counted:
 * "in range", "that meet your other filters", "that meet your CHASE filters".
 * `canada`: the airport list has curated Canadian fields.
 */
export function ledImpactWords(choice: LedChoice, im: LedImpact, scope: string, canada = false): { head: string; lines: string[] } {
  if (choice === "all") return { head: "", lines: [] };
  const lines: string[] = [];
  const n = im.removed.length;
  const of = `${n.toLocaleString("en-US")} of ${airportsN(im.base)} ${scope} removed`;
  if (choice === "avoid") {
    lines.push(!im.base ? `No airport ${scope} to remove.`
      : !n ? `${of}.`
      : `${of} (${some(im.removed, 8)}).`);
    if (im.moved.length) {
      lines.push(`${airportsN(im.moved.length)} ${im.moved.length === 1 ? "is" : "are"} shown on another runway end: ${
        some(im.moved.map((m) => `${m.id} ${m.to}, not ${m.from}`), 4, "; ")}.`);
    }
  } else {
    lines.push(!im.base ? `No airport ${scope} to remove.` : `${of}, ${im.left.toLocaleString("en-US")} left.`);
  }
  lines.push(LED_IR_COUNTS);
  if (canada) lines.push(`Canadian fields have no LED data: ${choice === "avoid" ? "they stay on the board" : "they are left out"}.`);
  return { head: choice === "avoid" ? "Avoiding LED ends" : "Only LED ends", lines };
}

/** The saved choice's age, worded and judged like the saved base's (main.ts baseAge): unknown age is stale. */
export function ledSetAge(setAt: number | null | undefined, now: number): { label: string; stale: boolean } {
  if (typeof setAt !== "number" || !Number.isFinite(setAt)) return { label: "set a while ago", stale: true };
  const d = Math.floor((now - setAt) / 86400e3);
  const label = d < 1 ? "set today" : d < 14 ? `set ${d}d ago` : `set ${Math.round(d / 7)}w ago`;
  return { label, stale: d > 21 };
}

// ---- dates ----

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2026-10-29" -> "29 Oct 2026". A calendar date as published: no time zone enters into it. */
export function dayWords(iso: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? "");
  return m && MON[+m[2] - 1] ? `${+m[3]} ${MON[+m[2] - 1]} ${m[1]}` : "";
}

/** A missed monthly refresh: the cycle on hand is normally under 60 days old (28-day cycles, one job a month). */
export const LED_DATA_OLD_MS = 75 * 86400e3;

/** "FAA data of 29 Oct 2026", and its age once a monthly refresh has been missed. */
export function ledDataWords(cycle: string | null, now: number): { words: string; old: boolean } {
  const day = dayWords(cycle);
  if (!day) return { words: "FAA data, date not given", old: true };
  const age = now - Date.parse(`${cycle}T00:00:00Z`);
  if (!(age > LED_DATA_OLD_MS)) return { words: `FAA data of ${day}`, old: false };
  return { words: `FAA data of ${day}, ${Math.round(age / (30.44 * 86400e3))} months old`, old: true };
}

// ---- the words ----

export const LED_EFVS = "An infrared EFVS may not see LED lights.";
export const LED_NO_DATA = "This build has no LED approach-light data.";
export const LED_LIST_FAILED = "The airport list did not load, so LED approach lights cannot be shown. Tick LED to try again.";

/** The top-bar tick's hover text: what it shows and its limits. */
export function ledTickTitle(block: LedBlock | null | undefined, now: number): string {
  if (block === null) return LED_NO_DATA;
  const limits = "Limits: FAA-owned systems in the US only, and a remark can follow the installation by months. No ring is not proof of incandescent lamps.";
  if (!block) return `Ring the airports where an FAA airport remark says a runway end has LED approach lights. ${LED_EFVS} ${limits}`;
  return `Ring the ${airportsN(block.airports.length)} where an FAA airport remark says a runway end has LED approach lights (${ledDataWords(block.cycle, now).words}). ${LED_EFVS} ${limits} Click a ring for its runway ends.`;
}

/** The chip beside the tick while the rings are on. */
export function ledChipWords(block: LedBlock, now: number): { text: string; old: boolean } {
  const d = ledDataWords(block.cycle, now);
  return { text: `${airportsN(block.airports.length)} with LED approach lights · ${d.words}`, old: d.old };
}

/** Hover text of a row's tag. `asof`: the date of the FAA list behind an LED + IR or a listed-no-remark tag. */
export function ledTagTitle(tag: LedTag, icao: string, e: string, irAsof: string | null): string {
  if (tag === LED_TAG_UNK) {
    return `An FAA airport remark says ${icao} has LED approach lights and names no runway this page could place. ` +
      `Runway ${e} has approach lights, so it is treated as LED. ${LED_EFVS}`;
  }
  if (tag === LED_TAG_WAS) {
    return `${irAsof ? `The FAA's list of LED installations of ${dayWords(irAsof)}` : "An FAA list of LED installations"} has runway ${e} at ${icao} ` +
      `as LED with infrared emitters. The FAA no longer publishes a remark for it, and nothing says the lamps were changed. ` +
      `Avoid LED ends leaves this end out; only LED ends does not count it.`;
  }
  const base = `An FAA airport remark says runway ${e} at ${icao} has LED approach lights.`;
  if (tag === "LED") return `${base} ${LED_EFVS}`;
  return `${base} ${irAsof ? `The FAA list of ${dayWords(irAsof)}` : "An FAA list"} says they have infrared emitters; nothing newer confirms it.`;
}

/**
 * Hover text of a row's "LED on 07R" note. `others`: the ends the note names.
 * It says where each comes from: at KMEM the note read "18L LED on the FAA's
 * list, no current remark" and its hover said "(FAA airport remark)", the one
 * thing that end does not have.
 */
export function ledOtherTitle(icao: string, shownHasLed: boolean, others: readonly { e: string; tag: LedTag }[] = []): string {
  const was = others.filter((x) => x.tag === LED_TAG_WAS).map((x) => x.e);
  const parts: string[] = [];
  if (!others.length || others.some((x) => x.tag === "LED" || x.tag === "LED + IR")) {
    parts.push(shownHasLed ? `More runway ends at ${icao} have LED approach lights (FAA airport remark).`
      : `Another runway end at ${icao} has LED approach lights (FAA airport remark).`);
  }
  if (others.some((x) => x.tag === LED_TAG_UNK)) {
    parts.push(`An FAA airport remark says ${icao} has LED approach lights and names no runway this page could place, so every end here with approach lights is treated as LED.`);
  }
  if (was.length) {
    parts.push(`${was.length === 1 ? "Runway" : "Runways"} ${was.join(", ")} at ${icao} ${was.length === 1 ? "is" : "are"} on the FAA's list of LED installations. ` +
      `The FAA no longer publishes a remark for ${was.length === 1 ? "it" : "them"}, and nothing says the lamps were changed.`);
  }
  if (!shownHasLed) parts.push("The end shown here is not flagged.");
  return parts.join(" ");
}

/** Hover text of the three-way control. */
export const LED_CHOICE_TITLE =
  "LED approach lights, from FAA airport remarks. Avoid: an airport stays if another runway end meets your filters, " +
  "and that end is shown; it drops out when only LED ends do. Only: the reverse. " + LED_IR_COUNTS + " " + LED_WAS_COUNTS +
  " Canadian fields have no LED data: avoid keeps them, only leaves them out. Shared by CHASE and DEPLOY.";

/** The legend under a board: what the tag means for an infrared EFVS, and what no tag does not mean. */
export function ledLegend(block: LedBlock, now: number): { led: string; ir: string; unk: string; was: string; none: string; data: string } {
  const wasDay = block.listed.find((x) => x.asof)?.asof ?? null;
  return {
    led: `The FAA says this runway end has LED approach lights. ${LED_EFVS}`,
    ir: block.nIr
      ? `LED lamps with infrared emitters, per ${block.irAsof ? `the FAA list of ${dayWords(block.irAsof)}` : "an FAA list"}; nothing newer confirms it.`
      : "",
    // only when the data holds one: neither exists in most months
    unk: block.airports.some((a) => a.rwyUnknown)
      ? "The FAA remark for the airport names no runway that could be placed, so every end there with approach lights is treated as LED."
      : "",
    was: block.listed.some((x) => x.board)
      ? `On ${wasDay ? `the FAA's list of LED installations of ${dayWords(wasDay)}` : "an FAA list of LED installations"}, with no current FAA remark: avoid leaves it out, only does not count it.`
      : "",
    none: "No tag: the FAA has not flagged that end. It is not proof of incandescent lamps. Canadian fields have no LED data.",
    data: `${ledDataWords(block.cycle, now).words}.`,
  };
}

/** What a click on a ring shows: the airport, its LED runway ends with their light system, and the caveats that apply. */
export function ledPopupLines(a: LedAirport, cycle: string | null, now: number): { ends: string[]; notes: string[]; foot: string } {
  const ends = a.ends.map((x) =>
    `RWY ${x.e} · ${x.als ? ALS_LABEL[x.als] ?? x.als : "light system not on record"}${x.tag === "LED + IR" ? " · LED + IR emitters" : ""}`);
  const notes: string[] = [];
  if (a.rwyUnknown) {
    notes.push("The FAA remark names no runway that could be placed. Every runway end here with approach lights is treated as LED.");
    if (a.rmk && !a.nonstd) notes.push(`The remark reads: "${a.rmk}"`);
  }
  const irDates = [...new Set(a.ends.filter((x) => x.tag === "LED + IR").map((x) => x.irAsof))];
  if (irDates.length) {
    notes.push(`IR emitters: ${irDates[0] ? `FAA list of ${dayWords(irDates[0])}` : "an FAA list"}; nothing newer confirms it.`);
  }
  if (a.nonstd && a.rmk) notes.push(`The FAA remark is not the usual sentence. It reads: "${a.rmk}"`);
  notes.push(LED_EFVS);
  // an airport whose runway is not known can still be a CHASE airport: the block cannot say, so nothing is claimed
  const where = !a.atlas ? " Not in the fog atlas, so not on CHASE or DEPLOY."
    : !a.board && !a.rwyUnknown ? " Not a CHASE or DEPLOY airport." : "";
  return { ends, notes, foot: `${ledDataWords(cycle, now).words}.${where}` };
}

/** The methodology paragraph: the source, what it does not cover, and that the tag is a planning aid. */
export function ledMethodWords(block: LedBlock | null | undefined, now: number): string {
  const have = block
    ? ` This build: ${airportsN(block.airports.length)}, ${block.nEnds} runway ends, ${ledDataWords(block.cycle, now).words}.`
    : block === null ? ` ${LED_NO_DATA}` : "";
  const irList = block?.irAsof ? `the FAA's list of ${dayWords(block.irAsof)}` : "an FAA list of LED installations";
  return "The LED tick, the LED tag on CHASE and DEPLOY rows and the LED choice in the CHASE settings all come from one source: " +
    "the FAA airport remark \"PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY nn\", read every month from the FAA's NASR data " +
    "(plus one differently worded remark, at Pope AAF)." + have +
    " What it does not cover: approach lights the FAA does not own, anything outside the US (Canadian fields have no LED data), " +
    "and LED systems the FAA has not yet written a remark for; a remark can follow the installation by two months or more. " +
    "So no tag means \"not flagged by the FAA\", not \"incandescent\". " +
    `LED + IR means ${irList} says those LED lamps have infrared emitters; nothing newer confirms it. ` +
    (block?.listed.length
      ? `${block.listed.map((x) => `${x.id} ${x.e}`).join(", ")} ${block.listed.length === 1 ? "is" : "are"} on that list with no current remark: ` +
        "the row says so, avoid LED ends leaves it out, and only LED ends does not count it. "
      : "") +
    "The tag is a planning aid, not a statement about what your sensor will see: read the Chart Supplement and the NOTAMs for the runway before you fly it on EFVS.";
}
