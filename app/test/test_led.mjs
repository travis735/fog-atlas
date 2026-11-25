// Offline test for the LED approach-light rules and words (app/src/led.ts). No
// dependencies; Node strips the types itself (22.18+; CI pins 24.22.1):
//   node app/test/test_led.mjs
//
// Why it exists (readiness review 2026-11-24, item 3): the owner flight-tests
// an infrared EFVS, and LED approach lights may not show on it. chase.json
// now flags the runway ends an FAA airport remark names. The map rings those
// airports, CHASE and DEPLOY rows tag the runway end, and a three-way choice
// (show all / avoid LED ends / only LED ends) filters both boards. The cases
// below hold that choice to its rules on the real 2026-10-29 list (47
// airports, 54 runway ends), and hold "show all" to the board the page drew
// before the choice existed.
//
// Two inputs: a frozen cut of chase.json (fixtures/chase-led-2026-10-29.json:
// the led block, every LED airport, every curated Canadian airport, 20
// ordinary US airports) for the cases with numbers in them, and the chase.json
// the page ships (app/public/data/chase.json) for the cases that must hold on
// whatever the monthly job writes next.
//
// LED_MODULE=<path> swaps the module under test. It is how the cases were
// shown to FAIL against the page as it was (a stub with HEAD's rule: the
// filters only, no LED flag read, no led block). LED_MAIN=<path> swaps the
// main.ts the source checks read.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const led = await import(process.env.LED_MODULE ?? "../src/led.ts");
const {
  ALS_NAMED, LED_CHOICES, LED_CHOICE_LABEL, LED_CHOICE_TITLE, LED_IR_COUNTS, LED_EFVS, LED_NO_DATA, LED_LIST_FAILED,
  ledChoiceOf, ledChoiceIn, endHasLed, endMayBeLed, ledTag, endMeetsFilters, endsMeeting, endShown,
  LED_TAG_UNK, LED_TAG_WAS, LED_WAS_COUNTS,
  readLedBlock, ledEndsAt, ledOtherWords, ledImpact, ledImpactWords, ledSetAge,
  dayWords, ledDataWords, ledTickTitle, ledChipWords, ledTagTitle, ledOtherTitle, ledLegend, ledPopupLines, ledMethodWords,
} = led;
const { readFeed } = await import("../src/feed.ts");

const fx = (name) => new URL(`./fixtures/${name}`, import.meta.url);
const repo = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
const FX = JSON.parse(readFileSync(fx("chase-led-2026-10-29.json"), "utf8"));
const LIVE = JSON.parse(repo("app/public/data/chase.json"));
const MAIN = readFileSync(process.env.LED_MAIN ?? new URL("../src/main.ts", import.meta.url), "utf8");
const NOW = Date.parse("2026-11-25T05:00:00Z");
const DAY = 86400e3;

// the CHASE defaults (main.ts CHASE_DEFAULTS)
const DEFAULTS = { als: ["ALSF2", "ALSF1", "MALSR"], minLen: 5000, rvrReq: false, maxTier: 250 };
const ANY = { als: ["ALSF2", "ALSF1", "MALSR", "SSALR", "MALSF", "OTHER"], minLen: 3000, rvrReq: false, maxTier: 400 };

// HEAD's rule, verbatim from main.ts at ae4df0b4 (chaseCandidates): the end a
// board showed before the LED choice existed. Kept here to hold "show all" to it.
function headShown(ends, prefs) {
  const named = ["ALSF2", "ALSF1", "MALSR", "SSALR", "MALSF"];
  const sel = new Set(prefs.als);
  const passing = ends.filter((e) =>
    e.als != null &&
    (sel.has(e.als) || (sel.has("OTHER") && !named.includes(e.als))) &&
    e.len >= prefs.minLen &&
    e.tier <= prefs.maxTier &&
    (!prefs.rvrReq || e.rvr != null));
  if (!passing.length) return null;
  passing.sort((x, y) => x.tier - y.tier || y.len - x.len);
  return passing[0];
}
// a board: airport -> the end shown, for every airport of a chase.json
function board(chase, prefs, choice) {
  const out = {};
  for (const [id, ends] of Object.entries(chase.airports)) {
    const e = endShown(ends, prefs, choice);
    if (e) out[id] = e.e;
  }
  return out;
}
const pairs = (b) => Object.entries(b).map(([id, e]) => ({ id, e }));
const words = (xs) => xs.join(" ").split(/\s+/).filter(Boolean).sort();

const tests = {
  "'show all' shows the end the page showed before the choice existed (every airport, six filter sets)"() {
    const sets = [
      DEFAULTS, ANY,
      { ...DEFAULTS, maxTier: 200 }, { ...DEFAULTS, rvrReq: true, minLen: 8000 },
      { als: ["OTHER"], minLen: 3000, rvrReq: false, maxTier: 400 }, { als: [], minLen: 5000, rvrReq: false, maxTier: 250 },
    ];
    let shown = 0;
    for (const chase of [LIVE, FX]) {
      for (const prefs of sets) {
        for (const [id, ends] of Object.entries(chase.airports)) {
          const was = headShown(ends, prefs), is = endShown(ends, prefs, "all");
          assert.equal(is?.e ?? null, was?.e ?? null, `${id} under ${JSON.stringify(prefs)}`);
          if (is) { shown++; assert.equal(is, ends.find((e) => e.e === is.e), `${id}: the end shown is one of the airport's own`); }
        }
      }
    }
    assert.ok(shown > 2000, `only ${shown} ends shown over all sets: the comparison ran on almost nothing`);
    // an airport that is not in chase.json is off the board, not a crash
    assert.equal(endShown(undefined, DEFAULTS, "all"), null);
    assert.deepEqual(endsMeeting(null, DEFAULTS, "avoid"), []);
    // a saved value this page does not know hides nothing
    for (const v of [undefined, null, "", "hide", "ALL", 1, {}]) assert.equal(ledChoiceOf(v), "all");
    assert.deepEqual([ledChoiceOf("avoid"), ledChoiceOf("only"), ledChoiceOf("all")], ["avoid", "only", "all"]);
  },

  "avoid: an airport stays on another end that meets the filters, and drops out when only LED ends do"() {
    const all = board(FX, DEFAULTS, "all"), avoid = board(FX, DEFAULTS, "avoid");
    const im = ledImpact(pairs(all), pairs(avoid));
    // the 21 airports where every end that meets the default filters is LED
    // (the data builder's contract lists the same 21)
    assert.deepEqual(im.removed, words(["KACY KAND KAUG KBGM KBIL KBZN KFOE KGLS KITH KMFR KMHK KMYR KONP KOTH KPOB KRDM KSAV KSHR KSMX KTMB KVLD"]));
    // Dallas Love: 31R is LED, 13L is not and meets the filters, so the board moves to 13L
    assert.deepEqual(im.moved, [{ id: "KDAL", from: "31R", to: "13L" }]);
    assert.equal(im.left, im.base - 21);
    // nothing joins the board, and every end shown under avoid carries no LED flag
    for (const [id, e] of Object.entries(avoid)) {
      assert.ok(id in all, `${id} is on the board under avoid and was not under show all`);
      const end = FX.airports[id].find((x) => x.e === e);
      assert.ok(!end.led && !end.led_ir, `${id} ${e} is LED and is shown under avoid`);
    }
    // an airport with an LED end that the filters never showed is untouched: KLAX stays on 25L
    assert.equal(all.KLAX, "25L");
    assert.equal(avoid.KLAX, "25L");
    // the rule is per runway end, not per airport: KIAH has three LED ends and stays on 27
    assert.equal(avoid.KIAH, "27");
  },

  "only: the reverse, on the LED end"() {
    const all = board(FX, DEFAULTS, "all"), only = board(FX, DEFAULTS, "only");
    assert.equal(Object.keys(only).length, 31);
    for (const [id, e] of Object.entries(only)) {
      const end = FX.airports[id].find((x) => x.e === e);
      assert.equal(end.led, 1, `${id} ${e} is shown under only and is not LED`);
      assert.ok(endMeetsFilters(end, DEFAULTS), `${id} ${e} does not meet the filters`);
    }
    const im = ledImpact(pairs(all), pairs(only));
    assert.equal(im.left, 31);
    assert.equal(im.removed.length, im.base - 31);
    // nine airports move from the end the filters preferred to their LED end
    assert.deepEqual(im.moved.map((m) => `${m.id} ${m.from}>${m.to}`), [
      "KAUS 18L>36R", "KCMH 10R>28L", "KIAD 19C>01C", "KIAH 27>33R", "KILM 24>35",
      "KJAX 08>14", "KLAX 25L>07R", "KMCO 36R>18R", "KOKC 35R>17L"]);
    // an LED end that fails the filters does not put its airport on the board:
    // KLAS 26L is MALSF, which the default ALS set leaves out
    assert.ok(!("KLAS" in only));
    assert.equal(board(FX, { ...DEFAULTS, als: [...DEFAULTS.als, "MALSF"] }, "only").KLAS, "26L");
    // avoid and only split the "show all" board between them: together they cover it, sharing the moved airports
    const avoid = board(FX, DEFAULTS, "avoid");
    for (const id of Object.keys(all)) assert.ok(id in avoid || id in only, `${id} is on neither board`);
  },

  "LED + IR counts as LED for the choice and keeps its own tag"() {
    const sav = FX.airports.KSAV.find((e) => e.e === "10");
    assert.deepEqual([sav.led, sav.led_ir], [1, 1]);
    assert.equal(ledTag(sav), "LED + IR");
    assert.equal(endHasLed(sav), true);
    // Savannah's only end that meets the filters is the LED + IR one: avoid drops the airport, only keeps it on that end
    assert.equal(endShown(FX.airports.KSAV, DEFAULTS, "avoid"), null);
    assert.equal(endShown(FX.airports.KSAV, DEFAULTS, "only"), sav);
    assert.equal(ledTag(endShown(FX.airports.KCMH, DEFAULTS, "only")), "LED + IR");
    assert.equal(ledTag(FX.airports.KLAX.find((e) => e.e === "07R")), "LED");
    // the three ends the list marks, and no other
    const ir = Object.entries(FX.airports).flatMap(([id, ends]) => ends.filter((e) => ledTag(e) === "LED + IR").map((e) => `${id} ${e.e}`));
    assert.deepEqual(ir.sort(), ["KCMH 28L", "KLAS 26L", "KSAV 10"]);
    // and the control says so
    assert.match(LED_CHOICE_TITLE, /Ends marked LED \+ IR count as LED for this choice\./);
    assert.ok(ledImpactWords("avoid", ledImpact([], []), "in range").lines.includes(LED_IR_COUNTS));
    assert.ok(ledImpactWords("only", ledImpact([], []), "in range").lines.includes(LED_IR_COUNTS));
  },

  "a curated Canadian end is unknown: avoid keeps it, only leaves it out, and nothing calls it conventional"() {
    const canada = Object.entries(LIVE.airports).filter(([, ends]) => ends.some((e) => e.cur));
    assert.equal(canada.length, LIVE.meta.ca);
    assert.ok(canada.length >= 30, `${canada.length} curated airports: too few to test on`);
    let kept = 0;
    for (const [id, ends] of canada) {
      const all = endShown(ends, ANY, "all");
      assert.equal(endShown(ends, ANY, "avoid"), all, `${id}: avoid must keep an end with no LED data`);
      assert.equal(endShown(ends, ANY, "only"), null, `${id}: only must not show an end with no LED data`);
      if (all) kept++;
      for (const e of ends) {
        assert.equal(ledTag(e), null);
        assert.equal(endHasLed(e), false);
      }
      assert.deepEqual(ledEndsAt(readLedBlock(LIVE), id, ends), []);
      assert.equal(ledOtherWords(all, ledEndsAt(readLedBlock(LIVE), id, ends)), "");
    }
    assert.ok(kept >= 30, `only ${kept} curated airports have an end with approach lights`);
    // An end with no flag gets no tag at all. The only time the page's own
    // words mention the other kind of lamp is to say a missing tag proves nothing.
    const block = readLedBlock(LIVE);
    const said = [
      LED_CHOICE_TITLE, LED_NO_DATA, LED_LIST_FAILED, LED_EFVS, ...Object.values(LED_CHOICE_LABEL),
      ledTickTitle(block, NOW), ledTickTitle(undefined, NOW), ledChipWords(block, NOW).text,
      ...Object.values(ledLegend(block, NOW)), ledMethodWords(block, NOW),
      ...ledImpactWords("avoid", ledImpact([], []), "in range", true).lines,
      ...ledImpactWords("only", ledImpact([], []), "in range", true).lines,
    ].join(" ");
    assert.doesNotMatch(said, /conventional|halogen|non-LED|not LED\b/i);
    for (const m of said.matchAll(/incandescent/gi)) {
      const around = said.slice(Math.max(0, m.index - 40), m.index + 30);
      assert.match(around, /not proof of incandescent|not "incandescent"/, `"incandescent" used outside a denial: …${around}…`);
    }
    // the words beside the control say what happens to Canadian fields under each choice
    assert.ok(ledImpactWords("avoid", ledImpact([], []), "in range", true).lines.includes("Canadian fields have no LED data: they stay on the board."));
    assert.ok(ledImpactWords("only", ledImpact([], []), "in range", true).lines.includes("Canadian fields have no LED data: they are left out."));
    assert.ok(!ledImpactWords("avoid", ledImpact([], []), "in range", false).lines.some((l) => /Canadian/.test(l)));
  },

  "a chase.json from before the led block: no LED data, no choice applied, nothing shown as LED"() {
    // the file as it was at HEAD ae4df0b4: no led block, no flags on the ends
    const old = {
      meta: LIVE.meta,
      airports: Object.fromEntries(Object.entries(LIVE.airports).map(([id, ends]) =>
        [id, ends.map(({ led: _l, led_ir: _i, ...rest }) => rest)])),
    };
    assert.equal(readLedBlock(old), null);
    assert.equal(readLedBlock(null), null);
    assert.equal(readLedBlock(undefined), null);
    // A saved "only LED ends" against data with no flags would empty both
    // boards without a word. With no LED data the choice in effect is "show all".
    for (const saved of ["all", "avoid", "only", "junk", undefined]) assert.equal(ledChoiceIn(null, saved), "all");
    assert.equal(ledChoiceIn(undefined, "only"), "all");
    const block = readLedBlock(LIVE);
    assert.deepEqual(["all", "avoid", "only", "junk"].map((s) => ledChoiceIn(block, s)), ["all", "avoid", "only", "all"]);
    assert.deepEqual(board(old, DEFAULTS, ledChoiceIn(readLedBlock(old), "only")), board(LIVE, DEFAULTS, "all"));
    assert.ok(Object.keys(board(old, DEFAULTS, "all")).length > 500);
    // the tick is told why it is off, and no row gets a tag or a note
    assert.equal(ledTickTitle(null, NOW), "This build has no LED approach-light data.");
    assert.equal(LED_NO_DATA, "This build has no LED approach-light data.");
    assert.deepEqual(ledEndsAt(null, "KLAX", old.airports.KLAX), []);
    assert.match(ledMethodWords(null, NOW), /This build has no LED approach-light data\./);
  },

  "a damaged led block cannot break the page, and what it cannot place it does not count"() {
    for (const bad of [{ led: {} }, { led: { airports: "x" } }, { led: { airports: [] } }, { led: 7 }, { led: null }, { led: { airports: [null, 3, "KLAX", {}] } }]) {
      assert.equal(readLedBlock(bad), null, JSON.stringify(bad));
    }
    const block = readLedBlock({
      led: {
        cycle: "29 Oct", n_airports: 99,
        airports: [
          { id: "KAAA", name: "ALPHA", st: "TX", lat: 30, lon: -97, atlas: 1, board: 1, ends: [{ e: "09", als: "MALSR", board: 1 }, { e: "09", als: "MALSR", board: 1 }, { als: "MALSR" }, null] },
          { id: "KAAA", lat: 31, lon: -98, ends: [{ e: "27" }] },             // listed twice: one ring
          { id: "KBBB", name: "NO PLACE", ends: [{ e: "18" }] },               // no position: cannot be ringed
          { id: "KCCC", lat: "33", lon: -90, ends: [{ e: "18" }] },            // a position that is not a number
          { id: "KDDD", lat: 35, lon: -80, ends: "36" },                       // ends is not a list
          { id: "KEEE", lat: 36, lon: -81, ends: [{ e: "04", led_ir: 1, ir_asof: "last year" }] },
          { lat: 1, lon: 1, ends: [] },                                        // no id
        ],
      },
    });
    assert.deepEqual(block.airports.map((a) => a.id), ["KAAA", "KDDD", "KEEE"]);
    assert.equal(block.nEnds, 2); // KAAA 09 once, KEEE 04; the nameless and repeated ends are not ends
    assert.equal(block.cycle, null); // not a date: said as "date not given", never printed as written
    assert.equal(block.byId.get("KDDD").rwyUnknown, true);
    assert.deepEqual(block.byId.get("KEEE").ends, [{ e: "04", als: null, board: false, tag: "LED + IR", irAsof: null }]);
    // the chip counts what can be ringed, not the block's own n_airports
    assert.equal(ledChipWords(block, NOW).text, "3 airports with LED approach lights · FAA data, date not given");
    assert.equal(ledChipWords(block, NOW).old, true);
    const pop = ledPopupLines(block.byId.get("KEEE"), block.cycle, NOW);
    assert.deepEqual(pop.ends, ["RWY 04 · light system not on record · LED + IR emitters"]);
    assert.ok(pop.notes.includes("IR emitters: an FAA list; nothing newer confirms it."));
    for (const s of [...pop.ends, ...pop.notes, pop.foot]) assert.doesNotMatch(s, /undefined|null|NaN/);
  },

  "the led block and the flags on the runway ends tell one story (the chase.json the page ships)"() {
    const block = readLedBlock(LIVE);
    assert.ok(block, "app/public/data/chase.json has no led block: the LED tick would be disabled on the live site");
    // every ring the map draws is one airport of the block, and the numbers the block states are the list's own
    assert.equal(block.airports.length, LIVE.led.n_airports);
    assert.equal(block.nEnds, LIVE.led.n_ends);
    assert.equal(block.nIr, LIVE.led.n_led_ir);
    assert.equal(block.cycle, LIVE.led.cycle);
    assert.ok(block.airports.length >= 30, `${block.airports.length} LED airports: the builder's own gate needs 30`);
    // an end flagged on the board is listed in the block as on the board, and the other way round
    const flagged = Object.entries(LIVE.airports).flatMap(([id, ends]) => ends.filter(endHasLed).map((e) => `${id} ${e.e} ${ledTag(e)}`)).sort();
    const listed = block.airports.flatMap((a) => a.ends.filter((e) => e.board).map((e) => `${a.id} ${e.e} ${e.tag}`)).sort();
    assert.deepEqual(flagged, listed);
    assert.equal(flagged.length, LIVE.led.n_ends_board);
    for (const a of block.airports) {
      assert.equal(a.board, a.ends.some((e) => e.board), `${a.id}: board flag against its ends`);
      assert.equal(a.board, a.id in LIVE.airports && LIVE.airports[a.id].some(endHasLed), `${a.id}: board flag against the board`);
      assert.ok(Math.abs(a.lat) <= 90 && Math.abs(a.lon) <= 180, `${a.id}: position`);
      // a curated end never carries the flag
      for (const e of LIVE.airports[a.id] ?? []) assert.ok(!(e.cur && endHasLed(e)), `${a.id} ${e.e}: curated and flagged`);
    }
    // the tag on a row and the popup on the ring name the same ends
    for (const a of block.airports.filter((x) => x.board)) {
      assert.deepEqual(ledEndsAt(block, a.id, LIVE.airports[a.id]).map((x) => `${x.e} ${x.tag}`), a.ends.map((x) => `${x.e} ${x.tag}`), a.id);
    }
  },

  "the count beside the control: removed, moved and left, in words"() {
    const all = board(FX, DEFAULTS, "all");
    const base = Object.keys(all).length;
    const avoid = ledImpactWords("avoid", ledImpact(pairs(all), pairs(board(FX, DEFAULTS, "avoid"))), "that meet your CHASE filters", true);
    assert.equal(avoid.head, "Avoiding LED ends");
    assert.deepEqual(avoid.lines, [
      `21 of ${base} airports that meet your CHASE filters removed (KACY, KAND, KAUG, KBGM, KBIL, KBZN and 15 more).`,
      "1 airport is shown on another runway end: KDAL 13L, not 31R.",
      "Ends marked LED + IR count as LED for this choice.",
      "Canadian fields have no LED data: they stay on the board.",
    ]);
    const only = ledImpactWords("only", ledImpact(pairs(all), pairs(board(FX, DEFAULTS, "only"))), "that meet your CHASE filters", true);
    assert.equal(only.head, "Only LED ends");
    assert.deepEqual(only.lines, [
      `${base - 31} of ${base} airports that meet your CHASE filters removed, 31 left.`,
      "Ends marked LED + IR count as LED for this choice.",
      "Canadian fields have no LED data: they are left out.",
    ]);
    // a small board, as CHASE draws it within range of a base
    const small = [{ id: "KSMF", e: "17R" }, { id: "KMFR", e: "14" }, { id: "KRDM", e: "23" }, { id: "KDAL", e: "31R" }];
    assert.deepEqual(ledImpactWords("avoid", ledImpact(small, [{ id: "KSMF", e: "17R" }, { id: "KDAL", e: "13L" }]), "in range").lines, [
      "2 of 4 airports in range removed (KMFR, KRDM).",
      "1 airport is shown on another runway end: KDAL 13L, not 31R.",
      "Ends marked LED + IR count as LED for this choice.",
    ]);
    assert.equal(ledImpactWords("avoid", ledImpact(small, small), "in range").lines[0], "0 of 4 airports in range removed.");
    assert.equal(ledImpactWords("avoid", ledImpact([small[0]], [small[0]]), "in range").lines[0], "0 of 1 airport in range removed.");
    assert.equal(ledImpactWords("only", ledImpact(small, [small[1]]), "in range").lines[0], "3 of 4 airports in range removed, 1 left.");
    assert.equal(ledImpactWords("only", ledImpact(small, []), "in range").lines[0], "4 of 4 airports in range removed, 0 left.");
    // an empty board is not blamed on the choice
    assert.equal(ledImpactWords("avoid", ledImpact([], []), "in range").lines[0], "No airport in range to remove.");
    assert.equal(ledImpactWords("only", ledImpact([], []), "in range").lines[0], "No airport in range to remove.");
    // "show all" removes nothing and says nothing
    assert.deepEqual(ledImpactWords("all", ledImpact(small, small), "in range"), { head: "", lines: [] });
    // the two numbers on the line cannot argue: removed + left = the board under "show all"
    for (const choice of ["avoid", "only"]) {
      const im = ledImpact(pairs(all), pairs(board(FX, DEFAULTS, choice)));
      assert.equal(im.removed.length + im.left, im.base, choice);
    }
    assert.deepEqual(LED_CHOICES, ["all", "avoid", "only"]);
    assert.deepEqual(LED_CHOICE_LABEL, { all: "show all", avoid: "avoid LED ends", only: "only LED ends" });
  },

  "a saved choice shows its age, and an unknown age is stale"() {
    assert.deepEqual(ledSetAge(NOW - 3600e3, NOW), { label: "set today", stale: false });
    assert.deepEqual(ledSetAge(NOW - 3 * DAY, NOW), { label: "set 3d ago", stale: false });
    assert.deepEqual(ledSetAge(NOW - 15 * DAY, NOW), { label: "set 2w ago", stale: false });
    assert.deepEqual(ledSetAge(NOW - 21 * DAY, NOW), { label: "set 3w ago", stale: false });
    assert.deepEqual(ledSetAge(NOW - 22 * DAY, NOW), { label: "set 3w ago", stale: true });
    assert.deepEqual(ledSetAge(NOW - 40 * DAY, NOW), { label: "set 6w ago", stale: true });
    // a choice with no date (hand-edited storage, a page from before the date was kept): stale, not "today"
    for (const v of [null, undefined, NaN, "yesterday"]) assert.deepEqual(ledSetAge(v, NOW), { label: "set a while ago", stale: true });
  },

  "a click on a ring: the airport's LED ends, their light system, LED + IR emitters, and the caveats"() {
    const block = readLedBlock(FX);
    assert.equal(block.airports.length, 47);
    assert.equal(block.nEnds, 54);
    assert.equal(block.nIr, 3);
    assert.equal(block.cycle, "2026-10-29");
    assert.equal(block.irAsof, "2025-01-14");
    const pop = (id) => ledPopupLines(block.byId.get(id), block.cycle, NOW);
    assert.deepEqual(pop("KSAV"), {
      ends: ["RWY 10 · MALSR · LED + IR emitters"],
      notes: ["IR emitters: FAA list of 14 Jan 2025; nothing newer confirms it.", "An infrared EFVS may not see LED lights."],
      foot: "FAA data of 29 Oct 2026.",
    });
    assert.deepEqual(pop("KIAH"), {
      ends: ["RWY 09 · MALSR", "RWY 15R · MALSR", "RWY 33R · MALSR"],
      notes: ["An infrared EFVS may not see LED lights."],
      foot: "FAA data of 29 Oct 2026.",
    });
    // Pope AAF: not the FAA sentence, so its own words are shown
    assert.deepEqual(pop("KPOB").ends, ["RWY 23 · ALSF-1"]);
    assert.equal(pop("KPOB").notes[0],
      'The FAA remark is not the usual sentence. It reads: "CAUTION: ALL TWY EDGE LGT AND RWY 05/23 EDGE AND APCH LGT EQUIPPED WITH NSTD LED FIXTURES. LED FIXTURES MAY NOT BE COMPATIBLE WITH ALL NVD OR ENHANCED FLIGHT VISION SYSTEMS."');
    // an airport that is not in the atlas, and one that is in it and not on the board, say so
    assert.equal(pop("KSGT").foot, "FAA data of 29 Oct 2026. Not in the fog atlas, so not on CHASE or DEPLOY.");
    assert.deepEqual(pop("KSGT").ends, ["RWY 36 · MALSF"]);
    assert.equal(pop("TISX").foot, "FAA data of 29 Oct 2026. Not a CHASE or DEPLOY airport.");
    // the eleven that are not in the atlas (they are ringed from the FAA's own reference point)
    assert.deepEqual(block.airports.filter((a) => !a.atlas).map((a) => a.id), words(["KBVO KELZ KHDC KIXD KLWC KOJC KOKM KOUN KPTN KSGT KSNL"]));
    // a remark that names no runway (the 2025 wording did this)
    const bare = readLedBlock({ led: { cycle: "2026-10-29", airports: [{ id: "KXXX", name: "X", st: "TX", lat: 30, lon: -97, atlas: 1, board: 0, ends: [], rwy_unknown: 1 }] } });
    // The popup used to end "Not a CHASE or DEPLOY airport.", which was false
    // for KVLD in the review's doctored build: an airport whose runway is not
    // known can be on the board, so nothing is claimed either way.
    assert.deepEqual(ledPopupLines(bare.byId.get("KXXX"), bare.cycle, NOW), {
      ends: [], notes: ["The FAA remark names no runway that could be placed. Every runway end here with approach lights is treated as LED.",
        "An infrared EFVS may not see LED lights."],
      foot: "FAA data of 29 Oct 2026.",
    });
    // every one of the 47 gives a popup with nothing broken in it
    for (const a of block.airports) {
      const p = ledPopupLines(a, block.cycle, NOW);
      assert.equal(p.ends.length, a.ends.length);
      for (const s of [...p.ends, ...p.notes, p.foot]) assert.doesNotMatch(s, /undefined|null|NaN|\[object/, `${a.id}: ${s}`);
    }
  },

  "the tick's title, the chip and the legend give the source, the limits and the data's date"() {
    const block = readLedBlock(FX);
    assert.equal(ledChipWords(block, NOW).text, "47 airports with LED approach lights · FAA data of 29 Oct 2026");
    assert.equal(ledChipWords(block, NOW).old, false);
    assert.equal(ledTickTitle(block, NOW),
      "Ring the 47 airports where an FAA airport remark says a runway end has LED approach lights (FAA data of 29 Oct 2026). " +
      "An infrared EFVS may not see LED lights. Limits: FAA-owned systems in the US only, and a remark can follow the installation by months. " +
      "No ring is not proof of incandescent lamps. Click a ring for its runway ends.");
    // before chase.json is read the title makes no count
    assert.equal(ledTickTitle(undefined, NOW),
      "Ring the airports where an FAA airport remark says a runway end has LED approach lights. An infrared EFVS may not see LED lights. " +
      "Limits: FAA-owned systems in the US only, and a remark can follow the installation by months. No ring is not proof of incandescent lamps.");
    assert.deepEqual(ledLegend(block, NOW), {
      led: "The FAA says this runway end has LED approach lights. An infrared EFVS may not see LED lights.",
      ir: "LED lamps with infrared emitters, per the FAA list of 14 Jan 2025; nothing newer confirms it.",
      unk: "", was: "",     // neither kind of end is in this cut of the list
      none: "No tag: the FAA has not flagged that end. It is not proof of incandescent lamps. Canadian fields have no LED data.",
      data: "FAA data of 29 Oct 2026.",
    });
    // Carried state shows its age: a list no monthly run has replaced says how old it is.
    // The cycle on hand is normally under 60 days old; 75 days is a missed run.
    assert.deepEqual(ledDataWords("2026-10-29", Date.parse("2026-12-30T00:00:00Z")), { words: "FAA data of 29 Oct 2026", old: false });
    assert.deepEqual(ledDataWords("2026-10-29", Date.parse("2027-01-13T00:00:01Z")), { words: "FAA data of 29 Oct 2026, 2 months old", old: true });
    assert.deepEqual(ledDataWords("2026-10-29", Date.parse("2027-05-01T00:00:00Z")), { words: "FAA data of 29 Oct 2026, 6 months old", old: true });
    assert.deepEqual(ledDataWords(null, NOW), { words: "FAA data, date not given", old: true });
    assert.equal(ledChipWords(block, Date.parse("2027-05-01T00:00:00Z")).old, true);
    assert.equal(dayWords("2025-01-14"), "14 Jan 2025");
    assert.equal(dayWords("2026-13-01"), "");
    assert.equal(dayWords("soon"), "");
    // the tag's hover text
    assert.equal(ledTagTitle("LED", "KLAX", "07R", null),
      "An FAA airport remark says runway 07R at KLAX has LED approach lights. An infrared EFVS may not see LED lights.");
    assert.equal(ledTagTitle("LED + IR", "KSAV", "10", "2025-01-14"),
      "An FAA airport remark says runway 10 at KSAV has LED approach lights. The FAA list of 14 Jan 2025 says they have infrared emitters; nothing newer confirms it.");
    assert.equal(ledOtherTitle("KLAX", false), "Another runway end at KLAX has LED approach lights (FAA airport remark). The end shown here is not flagged.");
    assert.equal(ledOtherTitle("KDAL", true), "More runway ends at KDAL have LED approach lights (FAA airport remark).");
  },

  "a row names the other LED end when the end it shows is not the LED one"() {
    const block = readLedBlock(FX);
    const note = (id, prefs = DEFAULTS, choice = "all") => {
      const shown = endShown(FX.airports[id], prefs, choice);
      return `${shown?.e ?? "-"}${ledTag(shown ?? {}) ? ` [${ledTag(shown)}]` : ""} | ${ledOtherWords(shown, ledEndsAt(block, id, FX.airports[id]))}`;
    };
    // the board shows 25L at KLAX and the LED lights are on 07R: without the note the ring and the row argue
    assert.equal(note("KLAX"), "25L | LED on 07R");
    assert.equal(note("KOKC"), "35R | LED on 17L, 35L");
    assert.equal(note("KIAH"), "27 | LED on 09, 15R, 33R");
    assert.equal(note("KCMH"), "10R | LED + IR on 28L");
    assert.equal(note("KDAL"), "31R [LED] | also LED on 31L");
    assert.equal(note("KDAL", DEFAULTS, "avoid"), "13L | LED on 31L, 31R");
    assert.equal(note("KMFR"), "14 [LED] | ");
    assert.equal(note("KSAV"), "10 [LED + IR] | ");
    assert.equal(note("KLAX", DEFAULTS, "only"), "07R [LED] | ");
    // an airport the FAA has not flagged gets no tag and no note
    assert.equal(note("KSEA").split(" | ")[1], "");
    assert.equal(ledOtherWords(null, []), "");
    // both kinds on other ends of one airport
    assert.equal(ledOtherWords({ e: "09" }, [{ e: "27", tag: "LED" }, { e: "18", tag: "LED + IR" }, { e: "36", tag: "LED" }]), "LED on 27, 36; LED + IR on 18");
    // an end flagged on the board and missing from the block still gets its note
    assert.deepEqual(ledEndsAt(block, "KSEA", [{ e: "16L", led: 1 }, { e: "34R" }]), [{ e: "16L", tag: "LED" }]);
  },

  "the LED choice cannot trip or hide the feed gate: the gate counts chase.json's whole airport list"() {
    // The list the gate counts is chase.json's "airports". The led block is a
    // separate key: the LED airports that are not on the board were not added
    // to that list (the gate would then wait on reports from fields it never
    // counted). On 2026-11-25 the list was the same 937 ids as the gate's own
    // fixture of the day it went live (chase-ids-2026-11-24.json).
    const ids = Object.keys(LIVE.airports);
    assert.equal(ids.length, LIVE.meta.airports);
    assert.ok(ids.length >= 900, `${ids.length} chase airports`);
    const block = readLedBlock(LIVE);
    const offBoard = block.airports.filter((x) => !x.board);
    assert.ok(offBoard.length >= 5, `${offBoard.length} LED airports off the board: nothing to test this on`);
    for (const a of offBoard) assert.ok(!(a.id in LIVE.airports), `${a.id} was added to the airport list`);
    // the real file of 2026-11-24 is read the same whatever the choice: the
    // choice is not an input of the reader, and no choice changes the list
    const CSV = gunzipSync(readFileSync(fx("metars.cache-2026-11-24T2224Z.csv.gz"))).toString("utf8");
    const at = Date.parse("2026-11-24T22:24:17Z");
    const read = readFeed(200, CSV, ids, at);
    assert.equal(read.ok, true);
    assert.equal(read.stats.expectTotal, ids.length);
    for (const choice of LED_CHOICES) {
      const shown = Object.keys(board(LIVE, DEFAULTS, choice));
      assert.ok(shown.length < ids.length);
      // a board cut to its 31 LED airports is still a board over a healthy feed
      assert.equal(readFeed(200, CSV, ids, at).stats.expectCurrent, read.stats.expectCurrent, choice);
    }
    // main.ts hands the gate that whole list, and the reader knows nothing of LED lights
    // (the body is read into `text` first since the device-clock check went in ahead of the reader, 2026-11-25)
    assert.match(MAIN, /readFeed\(r\.status, (?:r\.ok \? await r\.text\(\) : ""|text),\s*chaseData \? Object\.keys\(chaseData\.airports\) : null, Date\.now\(\)\)/);
    assert.equal(MAIN.match(/readFeed\(/g).length, 1, "a second call of readFeed in main.ts");
    assert.doesNotMatch(repo("app/src/feed.ts"), /\bled\b|chasePrefs|chaseCandidates/i);
  },

  "main.ts: one rule for both boards, the tick's handlers go on once, storage may be blocked"() {
    // CHASE and DEPLOY both go through endShown; neither keeps a copy of the filter
    const body = (name) => {
      const at = MAIN.indexOf(`function ${name}(`);
      assert.ok(at >= 0, `${name} not found in main.ts`);
      return MAIN.slice(at, MAIN.indexOf("\n}\n", at));
    };
    assert.match(body("chaseCandidatesFor"), /endShown\(chaseData\.airports\[a\.icao\], chasePrefs, choice\)/);
    assert.match(body("chaseQualifying"), /endShown\(ends, chasePrefs, choice\)/);
    assert.match(body("chaseCandidates"), /chaseCandidatesFor\(ledChoice\(\)\)/);
    assert.match(body("deployQualifying"), /chaseQualifying\(ledChoice\(\)\)/);
    assert.doesNotMatch(MAIN, /e\.tier <= chasePrefs\.maxTier/, "main.ts still carries its own copy of the runway-end filter");
    // the choice in effect is never the saved value read raw
    assert.match(MAIN, /const ledChoice = \(\): LedChoice => ledChoiceIn\(ledData\(\), chasePrefs\.led\);/);
    // the chips' named systems are the ones the rule calls named
    const chips = MAIN.match(/const CHASE_ALS_CHIPS = \[([^\]]+)\] as const;/);
    assert.ok(chips, "CHASE_ALS_CHIPS not found in main.ts");
    assert.deepEqual(JSON.parse(`[${chips[1]}]`), [...ALS_NAMED, "OTHER"]);
    // the ring's click handler is registered in one place, behind the "layers already added" guard
    assert.equal(MAIN.match(/map\.on\("click", ledClick\)/g)?.length, 1);
    const layers = body("ledLayers");
    assert.ok(layers.indexOf('if (map.getSource("led-src")) return;') >= 0 &&
      layers.indexOf('if (map.getSource("led-src")) return;') < layers.indexOf('map.on("click", ledClick)'));
    // the tick's handler only switches visibility: it adds no layer and no listener
    const tick = MAIN.slice(MAIN.indexOf('$<HTMLInputElement>("#led-show").addEventListener("change"'));
    assert.doesNotMatch(tick.slice(0, tick.indexOf("\n});")), /map\.on\(|addLayer|addSource/);
    // storage that throws must not stop a setting from taking effect
    assert.match(MAIN, /const saveChasePrefs = \(\) => \{ try \{ localStorage\.setItem\("fa-chase"/);
    assert.match(MAIN, /const ledSave = \(on: boolean\) => \{ try \{ localStorage\.setItem\("fa-led"/);
    assert.match(MAIN, /const ledSaved = \(\): boolean => \{ try \{ return localStorage\.getItem\("fa-led"\) === "1"; \} catch \{ return false; \} \};/);
  },

  "an LED airport whose runway could not be placed: every approach-light end is LED, on both boards"() {
    // 2026-11-25 review: two real FAA sentences changed to "RWY 63". The build
    // wrote; KVLD showed RWY 36 with no tag, "avoid LED ends" KEPT it, the row
    // note was empty, and the ring's popup said it was not a CHASE airport.
    // The builder now puts led_unk on every end there that has approach lights.
    const vld = [
      { e: "18", len: 9300, als: null, rvr: null, ils: null, lpv: 1, tier: 250 },
      { e: "36", len: 9300, als: "MALSR", rvr: "T", ils: "I", lpv: 1, tier: 200, led_unk: 1 },
      { e: "17", len: 5598, als: "MALSR", rvr: null, ils: null, lpv: 1, tier: 250, led_unk: 1 },
    ];
    assert.equal(endHasLed(vld[1]), true);
    assert.equal(ledTag(vld[1]), "LED, runway not named");
    assert.equal(ledTag(vld[0]), null);
    assert.equal(endShown(vld, DEFAULTS, "all").e, "36");
    assert.equal(endShown(vld, DEFAULTS, "avoid"), null, "avoid must drop an airport whose LED runway is not known");
    assert.equal(endShown(vld, DEFAULTS, "only").e, "36", "only keeps it: the airport does have LED approach lights");
    // without the flag (the file the review built) the same airport read as not LED: this is what changed
    const bare = vld.map(({ led_unk, ...e }) => e);
    assert.equal(endShown(bare, DEFAULTS, "avoid").e, "36");
    const block = readLedBlock({ led: { cycle: "2026-10-29", airports: [
      { id: "KVLD", name: "VALDOSTA RGNL", st: "GA", lat: 30.78, lon: -83.28, atlas: 1, board: 1, ends: [], rwy_unknown: 1,
        rmk: "PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY 63." }] } });
    const a = block.byId.get("KVLD");
    assert.equal(a.rwyUnknown, true);
    const pop = ledPopupLines(a, block.cycle, NOW);
    assert.deepEqual(pop.notes.slice(0, 2), [
      "The FAA remark names no runway that could be placed. Every runway end here with approach lights is treated as LED.",
      'The remark reads: "PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY 63."']);
    assert.equal(pop.foot, "FAA data of 29 Oct 2026.");
    assert.doesNotMatch(pop.foot, /Not a CHASE/);
    // the row: the end shown carries the tag, and the other approach-light end is named
    assert.deepEqual(ledEndsAt(block, "KVLD", vld), [{ e: "36", tag: LED_TAG_UNK }, { e: "17", tag: LED_TAG_UNK }]);
    assert.equal(ledOtherWords(vld[1], ledEndsAt(block, "KVLD", vld)), "also LED, runway not named: 17 have approach lights");
    assert.match(ledTagTitle(LED_TAG_UNK, "KVLD", "36", null),
      /^An FAA airport remark says KVLD has LED approach lights and names no runway this page could place\. Runway 36 has approach lights, so it is treated as LED\./);
    assert.match(ledLegend(block, NOW).unk, /names no runway that could be placed, so every end there with approach lights is treated as LED/);
  },

  "an end on the FAA's list with no current remark: said on the row, left out by avoid, not counted by only"() {
    // Same review: KMEM 18L is on the FAA list the build trusts for infrared
    // marks (14 Jan 2025, LED with IR emitters); its remark left the FAA data
    // in May 2026. The board showed it with no tag and "avoid LED ends" kept it.
    const mem = [
      { e: "18L", len: 9000, als: "MALSR", rvr: "TMR", ils: "I", lpv: 1, tier: 200, led_was: 1 },
      { e: "36R", len: 9000, als: "ALSF2", rvr: "TMR", ils: "IIIB", lpv: 1, tier: 50 },
      { e: "09", len: 8946, als: null, rvr: "TMR", ils: null, lpv: 1, tier: 250 },
    ];
    assert.equal(endHasLed(mem[0]), false, "not LED by what the FAA says today: no ring, not in only");
    assert.equal(endMayBeLed(mem[0]), true);
    assert.equal(endMayBeLed(mem[1]), false);
    assert.equal(ledTag(mem[0]), "LED listed, no remark");
    assert.equal(endShown(mem, DEFAULTS, "all").e, "36R");
    assert.equal(endShown([mem[0]], DEFAULTS, "all").e, "18L");
    assert.equal(endShown([mem[0]], DEFAULTS, "avoid"), null, "avoid leaves the doubt out");
    assert.equal(endShown([mem[0]], DEFAULTS, "only"), null, "only shows what is known to be LED");
    assert.equal(endShown(mem, DEFAULTS, "avoid").e, "36R");
    const block = readLedBlock({ led: { cycle: "2026-10-29",
      ir_unused: [{ id: "KMEM", e: "18L", asof: "2025-01-14", source: "FAA list", als: "MALSR", board: 1 }],
      airports: [{ id: "KSAV", name: "SAVANNAH", st: "GA", lat: 32.1, lon: -81.2, atlas: 1, board: 1,
        ends: [{ e: "10", als: "MALSR", board: 1, led_ir: 1, ir_asof: "2025-01-14" }] }] } });
    assert.deepEqual(block.listed, [{ id: "KMEM", e: "18L", asof: "2025-01-14", board: true }]);
    assert.equal(block.byId.has("KMEM"), false, "never a ring: the map shows what the FAA says now");
    // a row that shows 36R names the listed end beside it
    assert.equal(ledOtherWords(mem[1], ledEndsAt(block, "KMEM", mem)), "18L LED on the FAA's list, no current remark");
    // and the note's hover says the same thing: it read "(FAA airport remark)", which this end is the one not to have
    const memOthers = ledEndsAt(block, "KMEM", mem).filter((x) => x.e !== mem[1].e);
    assert.equal(ledOtherTitle("KMEM", false, memOthers),
      "Runway 18L at KMEM is on the FAA's list of LED installations. The FAA no longer publishes a remark for it, " +
      "and nothing says the lamps were changed. The end shown here is not flagged.");
    assert.ok(!/FAA airport remark/.test(ledOtherTitle("KMEM", false, memOthers)));
    // a remark end and a listed end beside each other: one sentence each
    assert.equal(ledOtherTitle("KXYZ", true, [{ e: "09", tag: "LED" }, { e: "27", tag: LED_TAG_WAS }, { e: "18", tag: LED_TAG_WAS }]),
      "More runway ends at KXYZ have LED approach lights (FAA airport remark). Runways 27, 18 at KXYZ are on the FAA's list of LED installations. " +
      "The FAA no longer publishes a remark for them, and nothing says the lamps were changed.");
    assert.equal(ledTagTitle(LED_TAG_WAS, "KMEM", "18L", "2025-01-14"),
      "The FAA's list of LED installations of 14 Jan 2025 has runway 18L at KMEM as LED with infrared emitters. " +
      "The FAA no longer publishes a remark for it, and nothing says the lamps were changed. " +
      "Avoid LED ends leaves this end out; only LED ends does not count it.");
    assert.equal(ledLegend(block, NOW).was,
      "On the FAA's list of LED installations of 14 Jan 2025, with no current FAA remark: avoid leaves it out, only does not count it.");
    assert.ok(LED_CHOICE_TITLE.includes(LED_WAS_COUNTS));
    assert.match(ledMethodWords(block, NOW), /KMEM 18L is on that list with no current remark: the row says so, avoid LED ends leaves it out, and only LED ends does not count it\./);
    // the shipped file: every end on the list with no remark carries the flag, and no end carries both
    const live = readLedBlock(LIVE);
    for (const x of live.listed.filter((x) => x.board)) {
      const end = LIVE.airports[x.id].find((e) => e.e === x.e);
      assert.equal(end.led_was, 1, `${x.id} ${x.e}`);
      assert.ok(!end.led && !end.led_unk, `${x.id} ${x.e} carries two LED flags`);
    }
    for (const [id, ends] of Object.entries(LIVE.airports)) {
      for (const e of ends) {
        assert.ok([e.led, e.led_unk, e.led_was].filter((v) => v === 1).length <= 1, `${id} ${e.e} carries two LED flags`);
        if (e.cur) assert.ok(!e.led && !e.led_unk && !e.led_was, `${id} ${e.e}: a curated end has no LED data`);
        if (e.led_unk) assert.ok(e.als && live.byId.get(id)?.rwyUnknown, `${id} ${e.e}: led_unk without the airport's remark`);
      }
    }
  },

  "the methodology paragraph: the source, what it does not cover, a planning aid"() {
    const t = ledMethodWords(readLedBlock(FX), NOW);
    assert.match(t, /the FAA airport remark "PILOTS CONDUCTING EFVS OPS; BE AWARE LED ALS IN USE RWY nn", read every month from the FAA's NASR data/);
    assert.match(t, /This build: 47 airports, 54 runway ends, FAA data of 29 Oct 2026\./);
    assert.match(t, /What it does not cover: approach lights the FAA does not own, anything outside the US \(Canadian fields have no LED data\), and LED systems the FAA has not yet written a remark for/);
    assert.match(t, /no tag means "not flagged by the FAA", not "incandescent"/);
    assert.match(t, /LED \+ IR means the FAA's list of 14 Jan 2025 says those LED lamps have infrared emitters; nothing newer confirms it\./);
    assert.match(t, /The tag is a planning aid, not a statement about what your sensor will see/);
    assert.doesNotMatch(t, /undefined|null|NaN/);
    // before chase.json is read the paragraph makes no claim about this build
    assert.doesNotMatch(ledMethodWords(undefined, NOW), /This build/);
    assert.match(MAIN, /<p class="note" id="led-method"[^>]*>\$\{escHtml\(ledMethodWords\(ledData\(\), Date\.now\(\)\)\)\}<\/p>/);
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
