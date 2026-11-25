// Build the private board that the cron Worker checks for fog (src/fog.js).
//
//   node cron/make_board.mjs --base KXXX > "$TMPDIR/fogatlas-board.json"
//
// The board goes to STDOUT and nowhere else, on purpose: it names the owner's
// base and the fields he is watching, and this repository is public. Send it
// to a file OUTSIDE the repository, store it in KV (README.md), delete the
// file. A short summary goes to stderr.
//
// The fields are the ones the CHASE panel would list for the same base,
// filters and LED choice. Which runway end an airport is shown on (and so
// whether it is on the board at all) is the panel's own rule, endShown in
// app/src/led.ts, imported here: it was a copy of the filter in main.ts, and
// with the panel on "avoid LED ends" the phone board listed fields the panel
// hides at 90 of 105 sampled bases. Only the distance loop is still a copy
// (chaseCandidatesFor in app/src/main.ts lives with the map); test_alerts.mjs
// holds it to the board sizes the readiness review measured on the live site
// on 2026-11-24.
//
// Options (defaults are the CHASE panel's own):
//   --base KXXX          required; must be an airport of the atlas
//   --speed 250          knots, for the ETE shown in each alert
//   --max-ete-h 2        hours; with --speed this is the radius (500 nm)
//   --radius-nm N        instead of --max-ete-h
//   --als ALSF2,ALSF1,MALSR   approach light systems that qualify (OTHER = any other)
//   --min-len 5000       runway length, ft
//   --max-tier 250       lowest-minimums tier, as in the panel
//   --rvr                only runway ends with RVR equipment
//   --max 120            keep the nearest N fields
//   --led all            the panel's LED choice: all, avoid (LED ends) or only (LED ends)
//   --label "text"       a name for the board, shown in the daily heartbeat. None by
//                        default: whatever is written here goes to Pushover every day,
//                        and "<base>, 500 nm" would send the base with it.
//   --priority 1         Pushover priority of a fog alert, -2 to 2 (2 repeats until acknowledged)
//   --repeat-min 60      floor between two alerts for the same field
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { MAX_BOARD_IDS, readBoard } from "./src/fog.js";
import { endShown, ledChoiceOf } from "../app/src/led.ts";

export const BOARD_DEFAULTS = {
  speed: 250, maxEteH: 2, als: ["ALSF2", "ALSF1", "MALSR"], minLen: 5000, rvrReq: false, maxTier: 250, led: "all",
};
const NM_R = 3440.065;
const rad = (d) => (d * Math.PI) / 180;
function distNm(a, b) {
  const dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * NM_R * Math.asin(Math.sqrt(h));
}

/** The CHASE panel's candidate list for one base: [{ icao, nm, ete }] nearest first. */
export function chaseCandidates(airports, chase, baseIcao, prefs = {}) {
  const p = { ...BOARD_DEFAULTS, ...prefs };
  const base = airports.find((x) => x.icao === baseIcao);
  if (!base) return null;
  const out = [];
  for (const a of airports) {
    if (a.icao === base.icao) continue;
    // the panel's own rule: the saved filters and the LED choice
    if (!endShown(chase[a.icao], p, ledChoiceOf(p.led))) continue;
    const nm = distNm(base, a);
    const ete = nm / p.speed;
    if (ete <= p.maxEteH) out.push({ icao: a.icao, nm, ete });
  }
  return out.sort((x, y) => x.ete - y.ete);
}

/** The JSON stored under alerts:board. `dropped` counts fields beyond `max`. */
export function buildBoard(airports, chase, baseIcao, prefs = {}, o = {}) {
  const p = { ...BOARD_DEFAULTS, ...prefs };
  const all = chaseCandidates(airports, chase, baseIcao, p);
  if (!all) return null;
  // the Worker asks AWC by 4-character id; anything else on the list cannot be asked for
  const askable = all.filter((c) => /^[A-Z0-9]{4}$/.test(c.icao));
  const max = Math.min(o.max ?? 120, MAX_BOARD_IDS);
  const kept = askable.slice(0, max);
  const board = {
    v: 1,
    made: (o.now ?? new Date()).toISOString().slice(0, 19) + "Z",
    // no label unless one is asked for: it rides in the daily heartbeat, through Pushover
    ...(o.label ? { label: String(o.label) } : {}),
    priority: o.priority ?? 1,
    repeatMin: o.repeatMin ?? 60,
    speedKt: p.speed,
    ids: kept.map((c) => c.icao),
    eteMin: Object.fromEntries(kept.map((c) => [c.icao, Math.round(c.ete * 60)])),
  };
  return { board, found: all.length, unaskable: all.length - askable.length, dropped: askable.length - kept.length, kept };
}

function main(argv) {
  const arg = (name, fallback) => {
    const i = argv.indexOf(`--${name}`);
    return i < 0 ? fallback : argv[i + 1];
  };
  const has = (name) => argv.includes(`--${name}`);
  const base = (arg("base") ?? "").toUpperCase();
  if (!base) {
    console.error('usage: node cron/make_board.mjs --base KXXX [options] > "$TMPDIR/fogatlas-board.json"   (options: top of this file)');
    return 2;
  }
  const num = (name, fallback) => {
    const v = arg(name);
    if (v === undefined) return fallback;
    const x = Number(v);
    if (!Number.isFinite(x)) throw new Error(`--${name} needs a number, got "${v}"`);
    return x;
  };
  const speed = num("speed", BOARD_DEFAULTS.speed);
  const radius = num("radius-nm", null);
  const prefs = {
    speed,
    maxEteH: radius != null ? radius / speed : num("max-ete-h", BOARD_DEFAULTS.maxEteH),
    als: (arg("als") ?? BOARD_DEFAULTS.als.join(",")).toUpperCase().split(",").filter(Boolean),
    minLen: num("min-len", BOARD_DEFAULTS.minLen),
    maxTier: num("max-tier", BOARD_DEFAULTS.maxTier),
    rvrReq: has("rvr"),
    led: arg("led", "all"),
  };
  if (!["all", "avoid", "only"].includes(prefs.led)) throw new Error(`--led needs all, avoid or only, got "${prefs.led}"`);
  const data = (name) => JSON.parse(readFileSync(new URL(`../app/public/data/${name}`, import.meta.url), "utf8"));
  const airports = data("airports.json").airports;
  const chase = data("chase.json").airports;
  const built = buildBoard(airports, chase, base, prefs, {
    max: num("max", 120),
    label: has("no-label") ? "" : arg("label"),   // --no-label is the default now and still accepted
    priority: num("priority", 1),
    repeatMin: num("repeat-min", 60),
  });
  if (!built) {
    console.error(`${base} is not an airport of the atlas (app/public/data/airports.json).`);
    return 1;
  }
  const { board, found, unaskable, dropped, kept } = built;
  if (!kept.length) {
    console.error(`No field passes these filters within ${Math.round(prefs.speed * prefs.maxEteH)} nm of ${base}. Nothing written.`);
    return 1;
  }
  const text = JSON.stringify(board);
  // the Worker's own reader must accept what this writes
  const check = readBoard(text);
  if (!check.ok) {
    console.error(`The Worker would refuse this board: ${check.why}. Nothing written.`);
    return 1;
  }
  const eteOf = (c) => {
    const m = Math.round(c.ete * 60); // as eteStr in app/src/main.ts
    return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
  };
  console.error(`${kept.length} fields within ${Math.round(prefs.speed * prefs.maxEteH)} nm of ${base} at ${prefs.speed} kt`
    + `${prefs.led === "avoid" ? ", avoiding LED ends" : prefs.led === "only" ? ", only LED ends" : ""} `
    + `(nearest ${kept[0].icao} ${eteOf(kept[0])}, farthest ${kept[kept.length - 1].icao} ${eteOf(kept[kept.length - 1])}).`);
  if (dropped) {
    console.error(`WARNING: ${found - unaskable} fields pass the filters. The ${dropped} farthest are NOT on this board `
      + `(--max ${kept.length}; the Worker's limit is ${MAX_BOARD_IDS}). Narrow the filters or raise --max.`);
  }
  if (unaskable) console.error(`${unaskable} fields left out: their id is not four letters or digits.`);
  if (process.stdout.isTTY) {
    console.error("The board itself goes to stdout. Send it to a file OUTSIDE this repository (README.md); it is not shown here.");
    return 0;
  }
  process.stdout.write(text + "\n");
  return 0;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (e) {
    console.error(e.message);
    process.exitCode = 2;
  }
}
