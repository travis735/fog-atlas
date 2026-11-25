// Offline test for the cron Worker's path to the phone: notify(), failed
// workflow starts, the token-expiry warning, the daily heartbeat and the fog
// alerts for a private board. No dependencies; Node strips the types of
// app/src/feed.ts itself (22.18+):
//   node cron/test_alerts.mjs
// fetch is a scripted fake and KV is a Map, so nothing touches GitHub,
// aviationweather.gov, Pushover or Cloudflare.
//
// Why it exists (readiness review, 2026-11-24): nothing reached the owner's
// phone. A refused dispatch wrote a log line kept for 3 days, the watchdog
// opened a GitHub issue that GitHub Mobile does not push, and the fog alert
// needed the CHASE panel open in a tab.
//
// The scenario cases drive the Worker through its one real entry point,
// scheduled(), so they can be run against another build of it:
//   CRON_SRC=/path/to/other/cron/src node cron/test_alerts.mjs
// That is how they were shown to FAIL against the Worker as committed at
// ae4df0b4 (no push after a refused start; the */5 trigger unmapped), and how
// single rules were shown to matter (a copy with one rule removed).
//
// The real reports are fixtures/metar-json-2026-11-25.json (the JSON API) and
// fixtures/metars-csv-2026-11-25.csv (the cache file of the same minute, the
// one the CHASE board reads), both as AWC served them at 03:20Z.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const SRC = process.env.CRON_SRC
  ? pathToFileURL(process.env.CRON_SRC.replace(/\/?$/, "/"))
  : new URL("./src/", import.meta.url);
const load = (name) => import(new URL(name, SRC)).catch(() => ({})); // a build without the file fails its cases, not the run
const index = await import(new URL("index.js", SRC));
const worker = index.default;
const { JOBS } = index;
const fog = await load("fog.js");
const note = await load("notify.js");
const clock = await load("clock.js");
const feed = await import("../app/src/feed.ts");
const { buildBoard, chaseCandidates } = await import("./make_board.mjs");

const FOG_CRON = "*/5 * * * *";
const HOURLY = "25 * * * *";
const WATCHDOG = "50 * * * *";
const GH = "api.github.com", AWC = "aviationweather.gov", PO = "api.pushover.net";
const ENV = { GH_PAT: "gh-token-not-real", GH_REPO: "example/repo", GH_REF: "main", HEARTBEAT_UTC_HOUR: "14" };
const PUSH = { PUSHOVER_TOKEN: "po-token-not-real", PUSHOVER_USER: "po-user-not-real" };
const MIN = 60_000, HOUR = 3_600_000, DAY = 86_400_000;
const T0 = Date.parse("2026-12-10T03:00:00Z");
const at = (iso) => Date.parse(iso);
const PUSH_OK = { status: 200, body: '{"status":1,"request":"r-1"}' };
const repo = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

const real = { fetch: globalThis.fetch, now: Date.now, log: console.log, error: console.error };
if (note.KV_WRITE_GAP) note.KV_WRITE_GAP.ms = 0; // offline: no need to wait out KV's one write a second
let LOGS = [];

// ---- fakes ----

/** Scripted network. Per host: a list of answers used in order, or a function of the request. */
function net(hosts) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const call = { url: String(url), host: new URL(String(url)).host, method: init.method ?? "GET", headers: init.headers ?? {}, body: init.body };
    calls.push(call);
    const h = hosts[call.host];
    assert.ok(h, `unexpected request to ${call.host}`);
    const next = typeof h === "function" ? h(call) : h.shift();
    assert.ok(next, `unexpected extra request: ${call.method} ${call.url}`);
    if (next instanceof Error) throw next;
    return new Response(next.body ?? null, { status: next.status ?? 200, headers: next.headers });
  };
  calls.to = (host) => calls.filter((c) => c.host === host);
  calls.pushes = () => calls.to(PO).map((c) => JSON.parse(c.body));
  return calls;
}

function fakeKV(init = {}) {
  const data = new Map(Object.entries(init));
  const ops = { reads: 0, keysRead: [], puts: [], deletes: [] };
  return {
    data, ops,
    json: (key) => JSON.parse(data.get(key)),
    async get(key) {
      ops.reads++;
      if (Array.isArray(key)) {
        ops.keysRead.push(...key);
        return new Map(key.map((k) => [k, data.get(k) ?? null]));
      }
      ops.keysRead.push(key);
      return data.get(key) ?? null;
    },
    async put(key, value) {
      assert.equal(typeof value, "string");
      ops.puts.push(key);
      data.set(key, value);
    },
    async delete(key) {
      ops.deletes.push(key);
      data.delete(key);
    },
  };
}

/** One Cron Trigger firing at `when`, through the Worker's real entry point. Resolves or rejects as the invocation would. */
async function fire(cron, env, when) {
  Date.now = () => when;
  try {
    let waited;
    const handler = worker.scheduled({ cron, scheduledTime: when }, env, { waitUntil: (p) => { waited = p; } });
    handler.catch(() => {}); // the handler fails as its work does; the cases assert on the work
    assert.ok(waited instanceof Promise, "scheduled() must hand its work to waitUntil");
    return await waited;
  } finally {
    Date.now = real.now;
  }
}

const events = (name) => LOGS.map((l) => { try { return JSON.parse(l); } catch { return {}; } }).filter((e) => e.event === name);

// ---- a small fake sky ----

const BOARD_IDS = (n) => Array.from({ length: n }, (_, i) => `K${String.fromCharCode(65 + Math.floor(i / 26))}${String.fromCharCode(65 + (i % 26))}X`);
/** A JSON METAR as AWC shapes it. vis: number | "10+" | null (key left out, as AWC does). */
function report(id, now, o = {}) {
  const obsMs = now - (o.ageMin ?? 10) * MIN;
  const d = new Date(obsMs);
  const z = `${String(d.getUTCDate()).padStart(2, "0")}${String(d.getUTCHours()).padStart(2, "0")}${String(d.getUTCMinutes()).padStart(2, "0")}Z`;
  const r = { icaoId: id, obsTime: Math.floor(obsMs / 1000), rawOb: `METAR ${id} ${z} ${o.raw ?? "00000KT"}`, clouds: o.clouds ?? [] };
  if (o.vis !== null) r.visib = o.vis ?? "10+";
  if (o.wx) r.wxString = o.wx;
  return r;
}
const CLEAR = {};
const FOG = { vis: 0.25, wx: "FG", clouds: [{ cover: "OVX", base: 100 }], raw: "00000KT 1/4SM R29R/1200V1800FT FG VV001 08/08 A3012" };
/** AWC's answer for whatever ids a request names, from `sky` (id -> options for report(), or absent). */
const awc = (sky, now) => (call) => {
  const u = new URL(call.url);
  const out = u.searchParams.get("ids").split(",").filter((id) => sky[id]).map((id) => report(id, now, sky[id]));
  return out.length ? { status: 200, body: JSON.stringify(out) } : { status: 204 };
};
const skyOf = (ids, special = {}) => Object.fromEntries(ids.map((id) => [id, special[id] ?? CLEAR]));
const boardJson = (ids, extra = {}) => JSON.stringify({ v: 1, made: "2026-12-08T03:00:00Z", label: "TEST, 500 nm", ids, ...extra });

const FX = JSON.parse(readFileSync(new URL("./fixtures/metar-json-2026-11-25.json", import.meta.url), "utf8"));
const FX_CSV = readFileSync(new URL("./fixtures/metars-csv-2026-11-25.csv", import.meta.url), "utf8");
const FX_NOW = Date.parse(FX.fetchedAt);
const fx = (id) => FX.reports.find((r) => r.icaoId === id);

// Fog ticks for a 120-station board fed with the real reports (renamed onto
// the board's ids, their times moved to `now` so they stay current). Returns
// CPU microseconds per tick, user + system, as Node counts them.
//   quiet: the sky does not change (the usual tick: parse, judge, no write)
//   busy:  24 fields go into fog on every other tick (a push and a state write)
async function cpuOfTicks(ticks, busy = false) {
  const ids = BOARD_IDS(120);
  const shift = Math.floor((T0 - FX_NOW) / 1000);
  const served = (id) => { const r = FX.reports[ids.indexOf(id) % 103]; return { ...r, icaoId: id, obsTime: r.obsTime + shift }; };
  const calm = (id) => ({ ...served(id), visib: "10+", clouds: [] });
  const chunks = feed.chunkIds(ids, feed.METAR_CHUNK);
  // the bodies are built once, outside the measured loop
  const bodies = new Map(chunks.map((c) => [c.join(","), [JSON.stringify(c.map(served)), JSON.stringify(c.map(calm))]]));
  const kv = fakeKV({ "alerts:board": boardJson(ids, { repeatMin: 0 }) });
  const env = { ...ENV, ...PUSH, FOGCAST: kv };
  let i = 0;
  net({
    [AWC]: (call) => ({ status: 200, body: bodies.get(new URL(call.url).searchParams.get("ids"))[busy && i % 2 ? 1 : 0] }),
    [PO]: () => PUSH_OK,
  });
  const before = process.cpuUsage();
  for (i = 0; i < ticks; i++) await fire(FOG_CRON, env, T0);
  const used = process.cpuUsage(before);
  return { micros: (used.user + used.system) / ticks, kv, bytes: [...bodies.values()].reduce((n, b) => n + b[0].length, 0) };
}

if (process.argv[2] === "--cpu-cold") {
  // A fresh process and a single tick: none of the Worker's code has run, as
  // in a new isolate. Node builds its own fetch classes on first use, which
  // cost about 10 ms here and are not the Worker's (the runtime has them
  // built in), so they are touched once first without running Worker code.
  console.log = console.error = () => {};
  if (process.argv[3] !== "--with-node-startup") {
    await new Response("[]", { status: 200, headers: { a: "b" } }).text();
    new URL("https://example.org/?ids=A,B").searchParams.get("ids");
    AbortSignal.timeout(1000);
  }
  const { micros } = await cpuOfTicks(1);
  globalThis.fetch = real.fetch;
  real.log(String(Math.round(micros)));
  process.exit(0);
}

const tests = {
  // ---------------- notify() ----------------
  async "notify: without the two secrets it sends nothing, says so once per invocation, and does not throw"() {
    const calls = net({});
    const run = {};
    for (const env of [ENV, { ...ENV, PUSHOVER_TOKEN: "only-one" }, { ...ENV, PUSHOVER_USER: "only-one" }]) {
      LOGS = [];
      const run1 = {};
      assert.deepEqual(await note.notify(env, { kind: "t", title: "a", message: "b" }, run1), { sent: false, retry: false, off: true });
      assert.equal((await note.notify(env, { kind: "t", title: "a", message: "b" }, run1)).off, true);
      assert.equal(events("notify_off").length, 1, "one line per invocation, not one per message");
    }
    assert.equal(calls.length, 0);
    assert.equal(note.pushConfigured({ ...ENV, ...PUSH }), true);
    void run;
  },
  async "notify: one POST to Pushover's message API with the secrets and the message"() {
    const calls = net({ [PO]: [PUSH_OK, PUSH_OK] });
    const env = { ...ENV, ...PUSH };
    assert.deepEqual(await note.notify(env, { kind: "t", title: "Title", message: "Body", priority: 1, url: "https://fogatlas.org/#chase", urlTitle: "Open" }, {}),
      { sent: true, retry: false });
    assert.equal(calls[0].url, "https://api.pushover.net/1/messages.json");
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].headers["Content-Type"], "application/json");
    assert.deepEqual(JSON.parse(calls[0].body), {
      token: "po-token-not-real", user: "po-user-not-real", title: "Title", message: "Body", priority: 1,
      url: "https://fogatlas.org/#chase", url_title: "Open",
    });
    // Pushover refuses priority 2 without retry and expire, and messages over 1024 characters
    await note.notify(env, { kind: "t", title: "T".repeat(400), message: "M".repeat(3000), priority: 2 }, {});
    const big = JSON.parse(calls[1].body);
    assert.equal(big.priority, 2);
    assert.ok(big.retry >= 30 && big.expire <= 10800, "emergency priority needs retry >= 30 s and expire <= 3 h");
    assert.equal(big.message.length, 1024);
    assert.equal(big.title.length, 250);
    // the secrets and the text never reach a log line
    assert.ok(!LOGS.join("\n").includes("po-token-not-real") && !LOGS.join("\n").includes("po-user-not-real") && !LOGS.join("\n").includes("Body"));
  },
  async "notify: a refusal (4xx) is logged and not retried; a 5xx, a timeout and a dropped connection ask for a retry; none throws"() {
    const env = { ...ENV, ...PUSH };
    net({ [PO]: [{ status: 400, body: '{"user":"invalid","errors":["user identifier po-user-not-real is invalid"],"status":0}' }] });
    assert.deepEqual(await note.notify(env, { kind: "t", title: "a", message: "b" }, {}), { sent: false, retry: false, refused: true });
    const bad = events("notify")[0];
    assert.equal(bad.ok, false);
    assert.equal(bad.status, 400);
    assert.ok(bad.errors.includes("is invalid") && !bad.errors.includes("po-user-not-real"), "the error is logged with the key scrubbed");
    net({ [PO]: [{ status: 429, body: '{"status":0,"errors":["monthly limit reached"]}' }] });
    assert.deepEqual(await note.notify(env, { kind: "t", title: "a", message: "b" }, {}), { sent: false, retry: false, refused: true });
    net({ [PO]: [{ status: 502, body: "<html>bad gateway</html>" }] });
    assert.deepEqual(await note.notify(env, { kind: "t", title: "a", message: "b" }, {}), { sent: false, retry: true });
    net({ [PO]: [new TypeError("fetch failed")] });
    assert.deepEqual(await note.notify(env, { kind: "t", title: "a", message: "b" }, {}), { sent: false, retry: true });
    net({ [PO]: [Object.assign(new Error("timed out"), { name: "TimeoutError" })] });
    assert.deepEqual(await note.notify(env, { kind: "t", title: "a", message: "b" }, {}), { sent: false, retry: true });
    // a 200 that does not say status 1 is not a delivery
    net({ [PO]: [{ status: 200, body: '{"status":0}' }] });
    assert.equal((await note.notify(env, { kind: "t", title: "a", message: "b" }, {})).sent, false);
  },

  // ---------------- the clock ----------------
  async "clock, inert: without the Pushover secrets a tick is the dispatch and nothing else"() {
    const kv = fakeKV();
    const calls = net({ [GH]: [{ status: 204 }] });
    assert.equal(await fire(HOURLY, { ...ENV, FOGCAST: kv }, T0 + 25 * MIN), 204);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "https://api.github.com/repos/example/repo/actions/workflows/forecast.yml/dispatches");
    assert.equal(kv.ops.reads, 0, "no KV read");
    assert.equal(kv.ops.puts.length, 0, "no KV write");
    assert.equal(events("notify_off").length, 1, "one clear line says the phone is off");
    // a failing dispatch is still exactly the old failure
    LOGS = [];
    const failing = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }] });
    await assert.rejects(fire(HOURLY, { ...ENV, FOGCAST: kv }, T0 + 85 * MIN), /HTTP 401.*Bad credentials/);
    assert.equal(failing.length, 1);
    assert.equal(kv.ops.reads + kv.ops.puts.length, 0);
    assert.equal(events("notify_off").length, 1);
  },
  async "clock: a refused start reaches the phone once, not again every hour, and once more when it works again"() {
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    const refused = () => [{ status: 404, body: '{"message":"Not Found"}' }, { status: 200, body: '{"state":"active"}' }];
    // 03:25Z: refused, after the existing re-enable probe
    let calls = net({ [GH]: refused(), [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, T0 + 25 * MIN), /forecast\.yml dispatch: HTTP 404/);
    assert.deepEqual(calls.map((c) => c.host), [GH, GH, PO], "dispatch, the re-enable probe, then one push");
    const first = calls.pushes()[0];
    assert.equal(first.title, "Fog Atlas clock: forecast.yml did not start");
    assert.equal(first.priority, 1);
    assert.match(first.message, /^GitHub refused to start forecast\.yml at 03:25Z: HTTP 404 \(Not Found\)\./);
    assert.match(first.message, /The hourly forecast is not being issued\./);
    assert.match(first.message, /one more message when it starts again/);
    // 04:25Z and 05:25Z: the same failure. Nothing is pushed.
    for (const h of [1, 2]) {
      calls = net({ [GH]: refused() });
      await assert.rejects(fire(HOURLY, env, T0 + 25 * MIN + h * HOUR), /HTTP 404/);
      assert.equal(calls.to(PO).length, 0, "no second push for the same failure");
    }
    assert.equal(kv.json("alerts:clock").open["forecast.yml"].since, T0 + 25 * MIN);
    // 06:25Z: accepted again
    calls = net({ [GH]: [{ status: 204 }], [PO]: [PUSH_OK] });
    assert.equal(await fire(HOURLY, env, T0 + 25 * MIN + 3 * HOUR), 204);
    const back = calls.pushes();
    assert.equal(back.length, 1);
    assert.equal(back[0].title, "Fog Atlas clock: forecast.yml starts again");
    assert.match(back[0].message, /accepted the start of forecast\.yml at 06:25Z\. It had failed since 03:25Z on 2026-12-10\./);
    assert.deepEqual(kv.json("alerts:clock").open, {});
    // 07:25Z: all quiet. No push, and the state is not written again.
    const writes = kv.ops.puts.length;
    calls = net({ [GH]: [{ status: 204 }] });
    assert.equal(await fire(HOURLY, env, T0 + 25 * MIN + 4 * HOUR), 204);
    assert.equal(calls.to(PO).length, 0);
    assert.equal(kv.ops.puts.length, writes, "a quiet tick writes nothing");
    assert.ok(kv.ops.puts.every((k) => k === "alerts:clock"));
  },
  async "clock: each workflow has its own failure, and a different kind of failure is said again"() {
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    // 03:25Z GitHub is down (502) for the forecast
    let calls = net({ [GH]: [{ status: 502, body: "bad gateway" }, { status: 502 }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, T0 + 25 * MIN), /HTTP 502/);
    assert.match(calls.pushes()[0].message, /HTTP 502\. The hourly forecast is not being issued\. It is tried again every hour\./);
    // 03:50Z the watchdog is refused too: its own message
    calls = net({ [GH]: [{ status: 503 }, { status: 503 }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(WATCHDOG, env, T0 + 50 * MIN), /watchdog\.yml dispatch: HTTP 503/);
    assert.equal(calls.pushes()[0].title, "Fog Atlas clock: watchdog.yml did not start");
    assert.match(calls.pushes()[0].message, /The hourly check of the live site is not running\./);
    // 04:25Z still a 5xx (503 now): the same kind, so nothing new
    calls = net({ [GH]: [{ status: 503 }, { status: 503 }] });
    await assert.rejects(fire(HOURLY, env, T0 + 85 * MIN), /HTTP 503/);
    assert.equal(calls.to(PO).length, 0);
    // 05:25Z the token has died: that is news, and it says what to do
    calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, T0 + 145 * MIN), /HTTP 401/);
    assert.match(calls.pushes()[0].message, /HTTP 401 \(Bad credentials\).*token has expired, was revoked or lost a permission.*GH_PAT/);
    // the 10:40Z second-chance tick is the same workflow: no new message
    calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }] });
    await assert.rejects(fire("40 10 * * *", env, at("2026-12-10T10:40:00Z")), /HTTP 401/);
    assert.equal(calls.to(PO).length, 0);
    assert.deepEqual(Object.keys(kv.json("alerts:clock").open).sort(), ["forecast.yml", "watchdog.yml"]);
  },
  async "clock: a missing GH_PAT and an unmapped trigger are failures the phone hears about too"() {
    const kv = fakeKV();
    let calls = net({ [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, { ...ENV, ...PUSH, GH_PAT: undefined, FOGCAST: kv }, T0 + 25 * MIN), /GH_PAT secret is not set/);
    assert.equal(calls.to(GH).length, 0);
    assert.match(calls.pushes()[0].message, /^forecast\.yml could not be started at 03:25Z: GH_PAT secret is not set/);
    calls = net({ [PO]: [PUSH_OK] });
    await assert.rejects(fire("0 0 * * *", { ...ENV, ...PUSH, FOGCAST: fakeKV() }, T0), /no workflow is mapped/);
    assert.equal(calls.to(GH).length, 0);
    assert.equal(calls.pushes().length, 1);
  },
  async "clock: Pushover failing never changes what the dispatch did, and an undelivered message is tried again"() {
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    // a good dispatch stays good while Pushover is unreachable (the heartbeat is due on this tick)
    let calls = net({ [GH]: [{ status: 204 }], [PO]: [new TypeError("fetch failed")] });
    assert.equal(await fire(WATCHDOG, env, at("2026-12-10T14:50:00Z")), 204);
    assert.equal(calls.to(PO).length, 1);
    assert.equal(kv.json("alerts:clock").beat, "", "the heartbeat was not delivered, so it is not recorded as sent");
    // a refused dispatch still fails with GITHUB's error while Pushover answers 500
    calls = net({ [GH]: [{ status: 404, body: '{"message":"Not Found"}' }, { status: 200, body: '{"state":"active"}' }], [PO]: [{ status: 500 }] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-10T15:25:00Z")), /GitHub refused the forecast\.yml dispatch: HTTP 404/);
    assert.equal(kv.json("alerts:clock").open["forecast.yml"].told, false);
    // next hour, still refused: the message that never arrived is sent now
    calls = net({ [GH]: [{ status: 404, body: '{"message":"Not Found"}' }, { status: 200, body: '{"state":"active"}' }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-10T16:25:00Z")), /HTTP 404/);
    assert.equal(calls.pushes().length, 1);
    assert.equal(kv.json("alerts:clock").open["forecast.yml"].told, true);
    assert.equal(kv.json("alerts:clock").open["forecast.yml"].since, at("2026-12-10T15:25:00Z"));
  },
  async "clock: KV failing never changes what the dispatch did; a failure is still pushed"() {
    const kv = fakeKV();
    kv.get = async () => { throw new Error("KV is down"); };
    kv.put = async () => { throw new Error("KV is down"); };
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    let calls = net({ [GH]: [{ status: 204 }] });
    assert.equal(await fire(HOURLY, env, T0 + 25 * MIN), 204);
    assert.equal(calls.to(PO).length, 0);
    // With no state to consult "once" falls back to the clock: every sixth hour
    // (06:25Z here), not every hour. It was every hour: 24 pushes a day for a dead token.
    calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }] });
    await assert.rejects(fire(HOURLY, env, T0 + 85 * MIN), /HTTP 401/);
    assert.equal(calls.to(PO).length, 0, "04:25Z is not a sixth hour");
    let said = 0;
    for (let h = 5; h < 29; h++) {
      calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }], [PO]: () => PUSH_OK });
      await assert.rejects(fire(HOURLY, env, at("2026-12-10T00:25:00Z") + h * HOUR), /HTTP 401/);
      said += calls.to(PO).length;
    }
    assert.equal(said, 4, "24 hours of a dead token with KV down: four pushes, at 06, 12, 18 and 00Z");
    // no binding at all behaves the same
    calls = net({ [GH]: [{ status: 204 }] });
    assert.equal(await fire(HOURLY, { ...ENV, ...PUSH }, T0 + 145 * MIN), 204);
    // KV reads but will not write (the daily write quota is spent, say): the
    // write throws inside the phone path, and the dispatch is reported as it went
    const full = fakeKV();
    full.put = async () => { throw new Error("KV PUT failed: 429 Too Many Requests"); };
    LOGS = [];
    calls = net({ [GH]: [{ status: 204 }] });
    assert.equal(await fire(WATCHDOG, { ...ENV, ...PUSH, FOGCAST: full }, at("2026-12-10T14:50:00Z")), 204, "a good dispatch stays good");
    // the state is written BEFORE the push: it could not be, so the heartbeat waits for a sixth hour
    assert.equal(calls.to(PO).length, 0);
    assert.match(events("clock_state_write_failed")[0].message, /KV PUT failed/);
    assert.match(events("clock_notes_failed")[0].message, /could not be saved to KV/);
    let beats = 0;
    for (const hh of ["15", "16", "17", "18", "19", "20", "21", "22", "23"]) {
      calls = net({ [GH]: [{ status: 204 }], [PO]: () => PUSH_OK });
      assert.equal(await fire(WATCHDOG, { ...ENV, ...PUSH, FOGCAST: full }, at(`2026-12-10T${hh}:50:00Z`)), 204);
      for (const b of calls.pushes()) {
        beats++;
        assert.equal(b.title, "Fog Atlas: clock alive, fog alerts off");
        assert.ok(b.message.endsWith("(The alert state could not be saved, so this message may repeat.)"));
      }
    }
    assert.equal(beats, 1, "one heartbeat that day (at 18:50Z), not one on every :50 tick as before");
    calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, { ...ENV, ...PUSH, FOGCAST: full }, at("2026-12-10T15:25:00Z")), /GitHub refused the forecast\.yml dispatch: HTTP 401/);
  },
  async "clock: the token's expiry is logged on every dispatch and warned about once a day under 14 days"() {
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    const ok = (expires) => ({ status: 204, headers: expires ? { "GitHub-Authentication-Token-Expiration": expires } : {} });
    // 30 days out: logged, no push
    let calls = net({ [GH]: [ok("2027-01-09 03:25:00 UTC")] });
    await fire(HOURLY, env, T0 + 25 * MIN);
    assert.equal(calls.to(PO).length, 0);
    let log = events("dispatch").at(-1);
    assert.equal(log.token_expires, "2027-01-09T03:25:00.000Z");
    assert.equal(log.token_days_left, 30);
    assert.equal(kv.ops.puts.length, 0);
    // 10 days and a bit out: one warning
    calls = net({ [GH]: [ok("2026-12-20 07:00:00 UTC")], [PO]: [PUSH_OK] });
    await fire(HOURLY, env, T0 + 25 * MIN);
    const warn = calls.pushes()[0];
    assert.equal(warn.title, "Fog Atlas clock: the GitHub token expires in 10 days");
    assert.match(warn.message, /expires in 10 days, on 2026-12-20 at 07:00Z\..*store it as GH_PAT/);
    assert.equal(events("dispatch").at(-1).token_days_left, 10.1);
    // the rest of that UTC day: silent (also on the watchdog's tick)
    for (const [cron, t] of [[WATCHDOG, T0 + 50 * MIN], [HOURLY, T0 + 85 * MIN], [HOURLY, at("2026-12-10T23:25:00Z")]]) {
      calls = net({ [GH]: [ok("2026-12-20 07:00:00 UTC")] });
      await fire(cron, { ...env, HEARTBEAT_UTC_HOUR: "23" }, t);
      assert.equal(calls.to(PO).length, 0);
    }
    // the next UTC day: once more. GitHub's other spelling of the same instant parses too.
    calls = net({ [GH]: [ok("2026-12-20 02:00:00 -0500")], [PO]: [PUSH_OK] });
    await fire(HOURLY, env, at("2026-12-11T00:25:00Z"));
    assert.equal(calls.pushes()[0].title, "Fog Atlas clock: the GitHub token expires in 9 days");
    // a failed dispatch that still carries the header warns as well
    calls = net({ [GH]: [{ status: 404, body: "{}", headers: { "GitHub-Authentication-Token-Expiration": "2026-12-20 07:00:00 UTC" } },
      { status: 200, body: '{"state":"active"}' }], [PO]: [PUSH_OK, PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-12T00:25:00Z")), /HTTP 404/);
    assert.deepEqual(calls.pushes().map((p) => p.title), ["Fog Atlas clock: forecast.yml did not start", "Fog Atlas clock: the GitHub token expires in 8 days"]);
    // a date this code cannot read is logged as written and never guessed at
    // (this tick also ends the failure above: that one message, and no warning)
    LOGS = [];
    calls = net({ [GH]: [ok("some day soon")], [PO]: [PUSH_OK] });
    await fire(HOURLY, env, at("2026-12-12T01:25:00Z"));
    assert.equal(events("dispatch")[0].token_expires_unread, "some day soon");
    assert.deepEqual(calls.pushes().map((p) => p.title), ["Fog Atlas clock: forecast.yml starts again"]);
    assert.equal(clock.parseTokenExpiry("2026-12-20 07:00:00 UTC"), at("2026-12-20T07:00:00Z"));
    assert.equal(clock.parseTokenExpiry("2026-12-20 02:00:00 -0500"), at("2026-12-20T07:00:00Z"));
    assert.equal(clock.parseTokenExpiry("2026-12-20T07:00:00Z"), at("2026-12-20T07:00:00Z"));
    assert.equal(clock.parseTokenExpiry(null), null);
    assert.equal(clock.parseTokenExpiry("some day soon"), null);
  },
  async "heartbeat: once a day on the :50 tick at the set hour, saying whether a start failed in the last 24 hours"() {
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    const ok = () => [{ status: 204, headers: { "GitHub-Authentication-Token-Expiration": "2027-03-01 00:00:00 UTC" } }];
    // before the hour, and on another trigger at the hour: nothing
    for (const [cron, iso] of [[WATCHDOG, "2026-12-10T13:50:00Z"], [HOURLY, "2026-12-10T14:25:00Z"]]) {
      const calls = net({ [GH]: ok() });
      await fire(cron, env, at(iso));
      assert.equal(calls.to(PO).length, 0, `${cron} at ${iso}`);
    }
    assert.equal(kv.ops.puts.length, 0);
    // 14:50Z: the heartbeat
    let calls = net({ [GH]: ok(), [PO]: [PUSH_OK] });
    assert.equal(await fire(WATCHDOG, env, at("2026-12-10T14:50:00Z")), 204);
    let beat = calls.pushes()[0];
    assert.equal(beat.title, "Fog Atlas: clock alive, fog alerts off", "the fog state is in the title, not on the fourth line");
    assert.equal(beat.priority, -1, "quiet when all is well");
    assert.deepEqual(beat.message.split("\n"), [
      "The clock is alive.",
      "No workflow start failed in the last 24 hours.",
      "The GitHub token expires in 80 days, on 2027-03-01 at 00:00Z.",
      "Fog alerts: off. No board is stored.",
    ]);
    // later the same day: not again
    calls = net({ [GH]: ok() });
    await fire(WATCHDOG, env, at("2026-12-10T15:50:00Z"));
    assert.equal(calls.to(PO).length, 0);
    assert.equal(kv.ops.puts.length, 1, "one write a day for the heartbeat");
    // a refused start at 03:25Z the next day, recovered at 04:25Z ...
    calls = net({ [GH]: [{ status: 502 }, { status: 502 }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-11T03:25:00Z")));
    calls = net({ [GH]: ok(), [PO]: [PUSH_OK] });
    await fire(HOURLY, env, at("2026-12-11T04:25:00Z"));
    // ... and the 14:50Z tick was missed: the heartbeat goes out on the next :50 tick, and reports the failure
    calls = net({ [GH]: ok(), [PO]: [PUSH_OK] });
    await fire(WATCHDOG, env, at("2026-12-11T16:50:00Z"));
    beat = calls.pushes()[0];
    assert.equal(beat.title, "Fog Atlas: clock alive, something needs a look, fog alerts off");
    assert.equal(beat.priority, 0);
    assert.equal(beat.message.split("\n")[1], "1 workflow start failed in the last 24 hours: forecast.yml 1. Latest: forecast.yml at 03:25Z (HTTP 502).");
    // two days later the failure is older than 24 hours and no longer counted
    calls = net({ [GH]: ok(), [PO]: [PUSH_OK] });
    await fire(WATCHDOG, env, at("2026-12-13T14:50:00Z"));
    assert.equal(calls.pushes()[0].message.split("\n")[1], "No workflow start failed in the last 24 hours.");
    // a start that is failing right now is named
    calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }], [PO]: [PUSH_OK, PUSH_OK] });
    await assert.rejects(fire(WATCHDOG, env, at("2026-12-14T14:50:00Z")));
    beat = calls.pushes()[1];
    assert.match(beat.message, /1 workflow start failed in the last 24 hours: watchdog\.yml 1\. Latest: watchdog\.yml at 14:50Z \(HTTP 401\)\./);
    assert.match(beat.message, /STILL FAILING: watchdog\.yml since 14:50Z on 2026-12-14\./);
    assert.match(beat.message, /The GitHub token's expiry date is not known\./);
  },
  async "heartbeat: says what the fog alerts are doing, with the board's age"() {
    const ids = ["KAAX", "KABX", "KACX"];
    const beatAt = at("2026-12-10T14:50:00Z");
    const line = async (kvInit) => {
      const calls = net({ [GH]: [{ status: 204 }], [PO]: () => PUSH_OK });
      await fire(WATCHDOG, { ...ENV, ...PUSH, FOGCAST: fakeKV(kvInit) }, beatAt);
      const p = calls.pushes().find((x) => x.title.startsWith("Fog Atlas: clock alive"));
      return { fog: p.message.split("\n")[3], rest: p.message.split("\n").slice(4), priority: p.priority, title: p.title, all: calls.pushes() };
    };
    const state = (o) => JSON.stringify({ v: 1, st: { KAAX: "f", KABX: "c", KACX: "c" }, al: {}, down: null, bad: null, ran: beatAt - 5 * MIN, at: beatAt - 5 * MIN, cur: 3, fog: 1, ...o });
    let got = await line({ "alerts:board": boardJson(ids), "alerts:state": state({}) });
    assert.equal(got.fog, "Fog alerts: running for 3 fields (TEST, 500 nm), board made 2 days ago. At 14:45Z: 3 of 3 had a current report, 1 below CAT I.");
    assert.deepEqual(got.rest, ["A fog push is a prompt to look, not a decision. No push does not mean no fog."]);
    assert.equal(got.priority, -1);
    assert.equal(got.title, "Fog Atlas: clock alive, fog alerts running");
    // The heartbeat judged "something needs a look" by finding OFF or PAUSED in
    // the sentence, which carries the owner's label: a board labelled
    // "KOFF, 500 nm" made every heartbeat loud. It is judged on the status now.
    got = await line({ "alerts:board": boardJson(ids, { label: "KOFF, 500 nm, NOT RUNNING" }), "alerts:state": state({}) });
    assert.equal(got.priority, -1);
    assert.equal(got.title, "Fog Atlas: clock alive, fog alerts running");
    // no label, no date: the board's age is said to be unknown, not left out
    got = await line({ "alerts:board": JSON.stringify(["KAAX"]), "alerts:state": state({ cur: 1, fog: 1 }) });
    assert.equal(got.fog, "Fog alerts: running for 1 field, board age unknown. At 14:45Z: 1 of 1 had a current report, 1 below CAT I.");
    // the 5-minute tick has stopped: said in capitals, and the heartbeat is no longer quiet
    got = await line({ "alerts:board": boardJson(ids), "alerts:state": state({ ran: beatAt - 3 * HOUR, at: beatAt - 3 * HOUR }) });
    assert.equal(got.fog, "Fog alerts: NOT RUNNING. The last check on record is 11:50Z on 2026-12-10. Board: 3 fields (TEST, 500 nm), board made 2 days ago.");
    assert.equal(got.priority, 0);
    assert.equal(got.title, "Fog Atlas: clock alive, fog alerts NOT RUNNING");
    got = await line({ "alerts:board": boardJson(ids), "alerts:state": state({ down: { since: beatAt - 40 * MIN, kind: "http", told: true } }) });
    assert.equal(got.fog, "Fog alerts: PAUSED. Weather reports have not been readable since 14:10Z. Board: 3 fields (TEST, 500 nm), board made 2 days ago.");
    got = await line({ "alerts:board": boardJson(ids) });
    assert.equal(got.fog, "Fog alerts: NO CHECK RECORDED YET for 3 fields (TEST, 500 nm), board made 2 days ago.");
    got = await line({ "alerts:board": "{not json" });
    assert.equal(got.fog, "Fog alerts: OFF. The stored board cannot be used: it is not valid JSON.");
    got = await line({});
    assert.equal(got.fog, "Fog alerts: off. No board is stored.");
    assert.deepEqual(got.rest, []);
  },
  async "heartbeat: a start accepted without an expiry date is not called a token that never expires"() {
    // GitHub's header on the real token had not been seen by anyone when this
    // was written. "GitHub reports no expiry date for the token" read as
    // "never expires" for a header that is merely missing or renamed, and the
    // 14-day warning would then never fire.
    const calls = net({ [GH]: [{ status: 204 }], [PO]: [PUSH_OK] });
    await fire(WATCHDOG, { ...ENV, ...PUSH, FOGCAST: fakeKV() }, at("2026-12-10T14:50:00Z"));
    assert.equal(calls.pushes()[0].message.split("\n")[2], "GitHub sent no expiry date with this start, so no expiry warning can be given.");
  },
  async "clock: the 5-minute fog check is looked at every hour, and a stopped one is said once"() {
    // A fog check that had stopped was only reported by the next day's
    // heartbeat: up to 24 hours with a board stored and nothing checking it.
    const ids = ["KAAX", "KABX", "KACX"];
    const t0 = at("2026-12-10T16:50:00Z");
    const state = (ran) => JSON.stringify({ v: 1, st: { KAAX: "c", KABX: "c", KACX: "c" }, al: {}, down: null, bad: null, ran, at: ran, cur: 3, fog: 0 });
    const kv = fakeKV({ "alerts:board": boardJson(ids), "alerts:state": state(t0 - 10 * MIN),
      "alerts:clock": JSON.stringify({ v: 1, open: {}, log: [], warned: "", beat: "2026-12-10" }) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    let calls = net({ [GH]: [{ status: 204 }] });
    await fire(WATCHDOG, env, t0);
    assert.equal(calls.to(PO).length, 0, "running: nothing to say");
    // no check on record for two hours
    calls = net({ [GH]: [{ status: 204 }], [PO]: [PUSH_OK] });
    await fire(WATCHDOG, env, t0 + 2 * HOUR);
    assert.equal(calls.pushes().length, 1);
    assert.equal(calls.pushes()[0].title, "Fog Atlas: fog alerts are NOT RUNNING");
    assert.equal(calls.pushes()[0].priority, 1);
    assert.match(calls.pushes()[0].message, /^Fog alerts: NOT RUNNING\. The last check on record is 16:40Z on 2026-12-10\..*No fog alert can fire until the 5-minute check runs again\./);
    // still stopped an hour later: not said again
    calls = net({ [GH]: [{ status: 204 }] });
    await fire(WATCHDOG, env, t0 + 3 * HOUR);
    assert.equal(calls.to(PO).length, 0);
    // on another trigger the fog state is not read at all
    calls = net({ [GH]: [{ status: 204 }] });
    const reads = kv.ops.reads;
    await fire(HOURLY, env, t0 + 3 * HOUR + 35 * MIN);
    assert.equal(kv.ops.reads - reads, 1, "the :25 tick reads the clock state and nothing else");
    // it runs again: said once
    kv.data.set("alerts:state", state(t0 + 4 * HOUR - 5 * MIN));
    calls = net({ [GH]: [{ status: 204 }], [PO]: [PUSH_OK] });
    await fire(WATCHDOG, env, t0 + 4 * HOUR);
    assert.equal(calls.pushes()[0].title, "Fog Atlas: fog alerts run again");
    assert.match(calls.pushes()[0].message, /It had not run since 16:40Z on 2026-12-10\./);
    calls = net({ [GH]: [{ status: 204 }] });
    await fire(WATCHDOG, env, t0 + 5 * HOUR);
    assert.equal(calls.to(PO).length, 0);
  },
  async "clock: an outage that alternates between GitHub's 5xx and no answer is one failure, said once"() {
    // 5xx and "no answer" were two kinds of failure, and a change of kind is
    // told again: 24 hours of a GitHub outage that alternated between them
    // was 24 pushes at priority 1 for one workflow.
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    let pushes = 0;
    for (let h = 0; h < 24; h++) {
      const down = h % 2 ? [new TypeError("fetch failed")] : [{ status: 502 }, { status: 502 }];
      const calls = net({ [GH]: down, [PO]: () => PUSH_OK });
      await assert.rejects(fire(HOURLY, env, at("2026-12-10T00:25:00Z") + h * HOUR));
      pushes += calls.to(PO).length;
    }
    assert.equal(pushes, 1);
    assert.equal(kv.json("alerts:clock").open["forecast.yml"].since, at("2026-12-10T00:25:00Z"));
    // an expired token after that IS another failure
    const calls = net({ [GH]: [{ status: 401, body: '{"message":"Bad credentials"}' }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-11T00:25:00Z")));
    assert.match(calls.pushes()[0].message, /The GitHub token has expired, was revoked or lost a permission/);
  },
  async "clock: a heartbeat Pushover refuses is not counted as sent; it is tried again an hour later and says what was missed"() {
    // A 4xx from Pushover (a revoked key, a lapsed licence, the monthly quota)
    // set beat = today: the day's heartbeat was gone and nothing ever said so.
    const kv = fakeKV();
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    const refuse = { status: 429, body: '{"status":0,"errors":["application has exceeded its monthly quota"]}' };
    let calls = net({ [GH]: [{ status: 204 }], [PO]: [refuse] });
    LOGS = [];
    assert.equal(await fire(WATCHDOG, env, at("2026-12-10T14:50:00Z")), 204, "the dispatch is reported as it went");
    assert.match(events("clock_notes_failed")[0].message, /Pushover refused a message/);
    let c = kv.json("alerts:clock");
    assert.equal(c.beat, "", "not sent is not told");
    assert.deepEqual(c.refused, { since: at("2026-12-10T14:50:00Z"), last: at("2026-12-10T14:50:00Z") });
    // 35 minutes later, another trigger with something to say: Pushover is left alone
    calls = net({ [GH]: [{ status: 502 }, { status: 502 }] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-10T15:25:00Z")));
    assert.equal(calls.to(PO).length, 0);
    assert.equal(kv.json("alerts:clock").open["forecast.yml"].told, false);
    // the next :50 tick is an hour after the refusal: asked again, and accepted
    calls = net({ [GH]: [{ status: 204 }], [PO]: [PUSH_OK] });
    await fire(WATCHDOG, env, at("2026-12-10T15:50:00Z"));
    const beat = calls.pushes()[0];
    assert.ok(beat.title.startsWith("Fog Atlas: clock alive"));
    assert.ok(beat.message.startsWith("Pushover refused messages from 14:50Z on 2026-12-10 until now. What was sent in that time did not reach this phone.\nThe clock is alive."));
    c = kv.json("alerts:clock");
    assert.equal(c.beat, "2026-12-10");
    assert.equal(c.refused, null);
    // and the failed start that was held back is said on its next tick
    calls = net({ [GH]: [{ status: 502 }, { status: 502 }], [PO]: [PUSH_OK] });
    await assert.rejects(fire(HOURLY, env, at("2026-12-10T16:25:00Z")));
    assert.equal(calls.pushes()[0].title, "Fog Atlas clock: forecast.yml did not start");
  },

  // ---------------- fog alerts ----------------
  async "fog, inert: without the secrets a tick reads nothing and asks nobody"() {
    const kv = fakeKV({ "alerts:board": boardJson(["KAAX"]) });
    const calls = net({});
    assert.equal(await fire(FOG_CRON, { ...ENV, FOGCAST: kv }, T0), "off");
    assert.equal(calls.length, 0);
    assert.equal(kv.ops.reads + kv.ops.puts.length, 0);
    assert.equal(LOGS.length, 1);
    assert.equal(events("notify_off").length, 1);
  },
  async "fog, inert: with no board stored a tick is one KV read and one log line"() {
    const kv = fakeKV();
    const calls = net({});
    assert.equal(await fire(FOG_CRON, { ...ENV, ...PUSH, FOGCAST: kv }, T0), "no-board");
    assert.equal(calls.length, 0, "no request to anyone");
    assert.equal(kv.ops.reads, 1, "one read");
    assert.ok(kv.ops.keysRead.every((k) => k.startsWith("alerts:")));
    assert.equal(kv.ops.puts.length + kv.ops.deletes.length, 0);
    assert.equal(LOGS.length, 1);
    assert.equal(events("fog_alerts_off")[0].reason, "no board is stored under alerts:board");
    // a board with an empty list is the same thing
    const empty = fakeKV({ "alerts:board": '{"ids":[]}' });
    assert.equal(await fire(FOG_CRON, { ...ENV, ...PUSH, FOGCAST: empty }, T0), "no-board");
    assert.equal(empty.ops.puts.length, 0);
  },
  async "fog: the first run after a board is stored pushes nothing, whatever is in fog"() {
    const ids = BOARD_IDS(120);
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KACX: FOG, KBBX: FOG }), T0) });
    assert.equal(await fire(FOG_CRON, { ...ENV, ...PUSH, FOGCAST: kv }, T0), "ok");
    assert.equal(calls.to(PO).length, 0, "no push on the first run");
    // only those stations, 40 per request, from the JSON API, with a named user agent
    const asked = calls.to(AWC);
    assert.equal(asked.length, 3);
    assert.deepEqual(asked.flatMap((c) => new URL(c.url).searchParams.get("ids").split(",")), ids);
    for (const c of asked) {
      const u = new URL(c.url);
      assert.equal(u.origin + u.pathname, "https://aviationweather.gov/api/data/metar");
      assert.equal(u.searchParams.get("format"), "json");
      assert.ok(u.searchParams.get("ids").split(",").length <= 40);
      assert.match(c.headers["User-Agent"], /fogatlas/);
    }
    const st = kv.json("alerts:state");
    assert.deepEqual(Object.keys(st.st), ids);
    assert.deepEqual(ids.filter((id) => st.st[id] === "f"), ["KAAX", "KACX", "KBBX"]);
    assert.equal(st.cur, 120);
    assert.equal(st.fog, 3);
    assert.deepEqual(kv.ops.puts, ["alerts:state"]);
    assert.equal(kv.ops.reads, 2, "the board (with the test request) and the state");
  },
  async "fog: a field that goes from clear to below CAT I is pushed once, batched, with what the report says and when"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX", "KAFX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids, { eteMin: { KABX: 32, KAEX: 95 } }) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    // 5 minutes later three fields are below CAT I, each in its own way
    const t1 = T0 + 5 * MIN;
    const sky = skyOf(ids, {
      KABX: FOG,                                                                     // visibility and vertical visibility
      KADX: { vis: "10+", clouds: [{ cover: "FEW", base: 100 }, { cover: "BKN", base: 100 }], ageMin: 3 }, // ceiling only
      KAEX: { vis: 0.25, clouds: [], wx: "FZFG", ageMin: 1 },                        // visibility only
    });
    let calls = net({ [AWC]: awc(sky, t1), [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, t1), "ok");
    const push = calls.pushes();
    assert.equal(push.length, 1, "one message for the three fields");
    assert.equal(push[0].title, "3 fields below CAT I");
    assert.equal(push[0].priority, 1);
    assert.equal(push[0].url, "https://fogatlas.org/#chase");
    assert.deepEqual(push[0].message.split("\n"), [
      "KABX 02:55Z: 0.25 SM, ceiling 100 ft, R29R/1200 FT, FG. ETE 0:32.",
      "KADX 03:02Z: 10+ SM, ceiling 100 ft.",
      "KAEX 03:04Z: 0.25 SM, FZFG. ETE 1:35.",
    ]);
    assert.deepEqual(kv.json("alerts:state").st, { KAAX: "c", KABX: "f", KACX: "c", KADX: "f", KAEX: "f", KAFX: "c" });
    // the same sky 5 and 10 minutes later: nothing more is pushed and nothing is written
    const writes = kv.ops.puts.length;
    for (const t of [t1 + 5 * MIN, t1 + 10 * MIN]) {
      calls = net({ [AWC]: awc(sky, t) });
      await fire(FOG_CRON, env, t);
      assert.equal(calls.to(PO).length, 0);
    }
    assert.equal(kv.ops.puts.length, writes, "an unchanged sky writes nothing");
    // one field alone has its id as the title
    const t4 = t1 + 15 * MIN;
    calls = net({ [AWC]: awc({ ...sky, KAFX: FOG }, t4), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, t4);
    assert.equal(calls.pushes()[0].title, "KAFX below CAT I");
  },
  async "fog: a report over 75 minutes old is neither fog nor clear"() {
    const ids = BOARD_IDS(10);
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids, { KABX: FOG }), T0) });
    await fire(FOG_CRON, env, T0);
    // KAAX: its newest report says fog but is 76 minutes old -> no alert.
    // KABX: was in fog; its newest report says clear but is 3 hours old -> still in fog as far as anyone knows.
    const t1 = T0 + 5 * MIN;
    let calls = net({ [AWC]: awc(skyOf(ids, { KAAX: { ...FOG, ageMin: 76 }, KABX: { ageMin: 180 } }), t1) });
    await fire(FOG_CRON, env, t1);
    assert.equal(calls.to(PO).length, 0, "an old fog report is not an alert");
    assert.equal(kv.json("alerts:state").st.KAAX, "c");
    assert.equal(kv.json("alerts:state").st.KABX, "f", "an old clear report does not end the fog");
    // KAAX at exactly 75 minutes is still current (the board's own limit) and alerts
    const t2 = T0 + 10 * MIN;
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: { ...FOG, ageMin: 75 }, KABX: FOG }), t2), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, t2);
    assert.equal(calls.pushes()[0].title, "KAAX below CAT I");
    // KABX stops reporting for hours and comes back still in fog: it never left, so no second alert
    const t3 = T0 + 6 * HOUR;
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), t3) });
    await fire(FOG_CRON, env, t3);
    assert.equal(calls.to(PO).length, 0);
  },
  async "fog: a report that does not say (no visibility, no low ceiling) changes nothing; the first one that does can alert"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    // baseline: KAAX has no visibility in its report, KABX has no report at all
    const none = { vis: null };
    net({ [AWC]: awc({ ...skyOf(ids, { KAAX: none }), KABX: undefined }, T0) });
    await fire(FOG_CRON, env, T0);
    assert.deepEqual(kv.json("alerts:state").st, { KAAX: "u", KABX: "u", KACX: "c", KADX: "c", KAEX: "c" });
    // KACX loses its visibility sensor: it was clear and stays "clear as last known", no write
    const writes = kv.ops.puts.length;
    let calls = net({ [AWC]: awc({ ...skyOf(ids, { KAAX: none, KACX: none }), KABX: undefined }, T0 + 5 * MIN) });
    await fire(FOG_CRON, env, T0 + 5 * MIN);
    assert.equal(kv.ops.puts.length, writes);
    // KABX's first report ever says fog: the pilot has not been told, so he is
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: none, KABX: FOG }), T0 + 10 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 10 * MIN);
    assert.equal(calls.pushes()[0].title, "KABX below CAT I");
    // a report with no visibility but vertical visibility 100 ft does say: below CAT I
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: { vis: null, clouds: [{ cover: "OVX", base: 100 }] }, KABX: FOG }), T0 + 15 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 15 * MIN);
    assert.deepEqual(calls.pushes()[0].message.split("\n"), ["KAAX 03:05Z: visibility not reported, ceiling 100 ft."]);
  },
  async "fog: a floor of 60 minutes between two alerts for the same field"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    const tick = async (min, special, pushes) => {
      const calls = net({ [AWC]: awc(skyOf(ids, special), T0 + min * MIN), [PO]: pushes });
      await fire(FOG_CRON, env, T0 + min * MIN);
      return calls.pushes();
    };
    await tick(0, {}, []);
    assert.equal((await tick(5, { KAAX: FOG }, [PUSH_OK])).length, 1);   // 03:05 into fog: alert
    await tick(15, {}, []);                                             // 03:15 clear
    assert.equal((await tick(25, { KAAX: FOG }, [])).length, 0);        // 03:25 fog again, 20 min after the alert: silent
    assert.equal(kv.json("alerts:state").st.KAAX, "f");
    await tick(40, { KAAX: FOG }, []);                                  // still fog: silent
    await tick(50, {}, []);                                             // clear
    assert.equal((await tick(70, { KAAX: FOG }, [PUSH_OK])).length, 1); // 04:10, 65 min after the alert: alert
    // the board can set its own floor
    kv.data.set("alerts:board", boardJson(ids, { repeatMin: 0 }));
    await tick(75, {}, []);
    assert.equal((await tick(80, { KAAX: FOG }, [PUSH_OK])).length, 1);
  },
  async "fog: a board stored again starts from a baseline: its first check pushes nothing"() {
    // cron/README.md: "the first check after a board is stored pushes
    // nothing". With the old state kept, three ways of storing a board pushed
    // "12 fields below CAT I" at once for fields that had been in fog for days
    // (and a priority-2 board would have repeated that every 60 s for half an hour).
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    // (a) the board is replaced by another one 5 minutes later: KAEX leaves,
    // KZZX (in fog) joins, and KABX went into fog in the same 5 minutes
    const next = ["KAAX", "KABX", "KACX", "KADX", "KZZX"];
    kv.data.set("alerts:board", boardJson(next, { made: "2026-12-10T03:04:00Z" }));
    const t1 = T0 + 5 * MIN;
    let calls = net({ [AWC]: awc(skyOf(next, { KZZX: FOG, KABX: FOG }), t1) });
    await fire(FOG_CRON, env, t1);
    assert.equal(calls.to(PO).length, 0, "a new board: every field is a newcomer");
    assert.deepEqual(kv.json("alerts:state").st, { KAAX: "c", KABX: "f", KACX: "c", KADX: "c", KZZX: "f" });
    assert.equal(events("fog_baseline").at(-1).reason, "a new board is stored");
    // ... and a field that goes into fog AFTER that alerts as always
    calls = net({ [AWC]: awc(skyOf(next, { KZZX: FOG, KABX: FOG, KACX: FOG }), t1 + 5 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, t1 + 5 * MIN);
    assert.equal(calls.pushes()[0].title, "KACX below CAT I");
    // the same fields made at another time are a new board too (make_board.mjs stamps each one)
    kv.data.set("alerts:board", boardJson(next, { made: "2026-12-10T03:20:00Z" }));
    calls = net({ [AWC]: awc(skyOf(next, { KZZX: FOG, KABX: FOG, KACX: FOG, KADX: FOG }), t1 + 10 * MIN) });
    await fire(FOG_CRON, env, t1 + 10 * MIN);
    assert.equal(calls.to(PO).length, 0);
    // (b) the board is deleted for three days and the very same JSON is stored again, 3 of its fields in fog
    const board = kv.data.get("alerts:board");
    kv.data.delete("alerts:board");
    net({});
    assert.equal(await fire(FOG_CRON, env, t1 + 15 * MIN), "no-board");
    kv.data.set("alerts:board", board);
    const t3 = t1 + 3 * DAY;
    calls = net({ [AWC]: awc(skyOf(next, { KAAX: FOG, KABX: FOG, KACX: FOG }), t3) });
    await fire(FOG_CRON, env, t3);
    assert.equal(calls.to(PO).length, 0, "the old state is three days old: this is a first check");
    assert.equal(events("fog_baseline").at(-1).reason, "no check on record for over 6 hours");
    // (c) the Pushover secrets are removed for three days and set again
    net({});
    assert.equal(await fire(FOG_CRON, { ...ENV, FOGCAST: kv }, t3 + 5 * MIN), "off");
    calls = net({ [AWC]: awc(skyOf(next, { KAAX: FOG, KADX: FOG, KZZX: FOG }), t3 + 3 * DAY) });
    await fire(FOG_CRON, env, t3 + 3 * DAY);
    assert.equal(calls.to(PO).length, 0);
    // A gap of a few hours is NOT a new start: the check was blind (Cloudflare
    // did not fire it, KV could not be read), and a field that went into fog
    // meanwhile is exactly what the phone is for.
    const t4 = t3 + 3 * DAY + 5 * MIN;
    net({ [AWC]: awc(skyOf(next, { KAAX: FOG, KADX: FOG, KZZX: FOG }), t4) });
    await fire(FOG_CRON, env, t4);
    calls = net({ [AWC]: awc(skyOf(next, { KAAX: FOG, KADX: FOG, KZZX: FOG, KABX: FOG }), t4 + 4 * HOUR), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, t4 + 4 * HOUR);
    assert.equal(calls.pushes()[0].title, "KABX below CAT I");
    // a lost state is a first run: nothing is pushed
    kv.data.delete("alerts:state");
    calls = net({ [AWC]: awc(skyOf(next, { KZZX: FOG, KABX: FOG, KAAX: FOG }), t4 + 5 * HOUR) });
    await fire(FOG_CRON, env, t4 + 5 * HOUR);
    assert.equal(calls.to(PO).length, 0);
    // and so is an unreadable one
    kv.data.set("alerts:state", "{broken");
    calls = net({ [AWC]: awc(skyOf(next, { KZZX: FOG, KABX: FOG, KAAX: FOG }), t4 + 5 * HOUR + 5 * MIN) });
    await fire(FOG_CRON, env, t4 + 5 * HOUR + 5 * MIN);
    assert.equal(calls.to(PO).length, 0);
  },
  async "fog: reports unreadable for 15 minutes pushes 'paused' once, and 'back' once; fog that arrived meanwhile alerts then"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX", "KAFX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0) });
    await fire(FOG_CRON, env, T0);
    const down = () => ({ status: 502, body: "<html>Bad Gateway</html>" });
    // 03:05, 03:10, 03:15: three failed ticks, no push
    for (const m of [5, 10, 15]) {
      const calls = net({ [AWC]: down });
      assert.equal(await fire(FOG_CRON, env, T0 + m * MIN), "blind");
      assert.equal(calls.to(PO).length, 0, `no push after ${m - 5} minutes`);
      assert.equal(calls.to(AWC).length, 1, "the first failed request ends the read");
    }
    assert.equal(kv.ops.puts.filter((k) => k === "alerts:state").length, 2, "baseline, then one write when the outage began");
    // 03:20: 15 minutes without reports
    let calls = net({ [AWC]: down, [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 20 * MIN);
    const paused = calls.pushes();
    assert.equal(paused.length, 1);
    assert.equal(paused[0].title, "Fog Atlas: fog alerts PAUSED");
    assert.equal(paused[0].message, "No fog alert can fire. Weather reports have not been readable since 03:05Z. "
      + "aviationweather.gov answered with an error (HTTP 502). Still trying every 5 minutes. Look at the board yourself. "
      + "If this keeps happening you are not told again for 3 hours.");
    // an hour more of it: not one more push, not one more write
    const writes = kv.ops.puts.length;
    for (let m = 25; m <= 80; m += 5) {
      calls = net({ [AWC]: down });
      await fire(FOG_CRON, env, T0 + m * MIN);
      assert.equal(calls.to(PO).length, 0);
    }
    assert.ok(kv.ops.puts.length - writes <= 1, "at most the hourly time stamp");
    // the station states were not touched by any failed tick
    assert.deepEqual(kv.json("alerts:state").st, { KAAX: "f", KABX: "c", KACX: "c", KADX: "c", KAEX: "c", KAFX: "c" });
    // 04:25: reports are back. KABX went into fog during the outage; KAAX was in fog all along.
    const t = T0 + 85 * MIN;
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), t), [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, t), "ok");
    // the fog alert does not wait; "back" is said after a second healthy tick
    assert.deepEqual(calls.pushes().map((p) => p.title), ["KABX below CAT I"]);
    assert.equal(kv.json("alerts:state").down.ok, 1);
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), t + 5 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, t + 5 * MIN);
    const back = calls.pushes();
    assert.deepEqual(back.map((p) => p.title), ["Fog Atlas: fog alerts are back"]);
    assert.match(back[0].message, /They were not from 03:05Z to 04:30Z\./);
    assert.equal(kv.json("alerts:state").down, null);
    // a blip shorter than 15 minutes is never mentioned
    calls = net({ [AWC]: down });
    await fire(FOG_CRON, env, t + 10 * MIN);
    for (const m of [15, 20]) {
      calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), t + m * MIN) });
      await fire(FOG_CRON, env, t + m * MIN);
      assert.equal(calls.to(PO).length, 0);
    }
    assert.equal(kv.json("alerts:state").down, null);
  },
  async "fog: a feed that comes and goes is one PAUSED, not one every half hour"() {
    // PAUSED and "back" acted on a single tick each way. A feed down for 20
    // minutes and up for 5, all day, was 20 PAUSED and 20 back; reports
    // hovering at the 70% line were 24 and 23.
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX", "KAFX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    const titles = [];
    const day = async (isUp, from, ticks) => {
      for (let i = 0; i < ticks; i++) {
        const t = from + i * 5 * MIN;
        const calls = net({ [AWC]: isUp(i) ? awc(skyOf(ids), t) : () => ({ status: 502 }), [PO]: () => PUSH_OK });
        await fire(FOG_CRON, env, t);
        titles.push(...calls.pushes().map((p) => p.title));
      }
    };
    // down 20 minutes (4 ticks), up 5 (1 tick), for 24 hours
    await day((i) => i % 5 === 4, T0 + 5 * MIN, 288);
    assert.deepEqual(titles, ["Fog Atlas: fog alerts PAUSED"], "one healthy tick in between does not end an outage");
    // then healthy for good: "back" once, after the second healthy tick
    await day(() => true, T0 + 5 * MIN + 288 * 5 * MIN, 3);
    assert.deepEqual(titles.slice(1), ["Fog Atlas: fog alerts are back"]);
    // 30 minutes down, 30 up, for 12 hours: the second PAUSED inside 3 hours is not sent, nor its "back"
    titles.length = 0;
    await day((i) => Math.floor(i / 6) % 2 === 1, T0 + 2 * DAY, 144);
    const paused = titles.filter((x) => x.includes("PAUSED")).length, back = titles.filter((x) => x.includes("back")).length;
    assert.ok(paused >= 3 && paused <= 4, `${paused} PAUSED in 12 hours (it was 12)`);
    assert.equal(back, paused, "each PAUSED that was said gets its 'back'");
  },
  async "fog: an answer that says about nothing is a failed read, never an all-clear"() {
    const ids = BOARD_IDS(20);
    const start = async () => {
      const kv = fakeKV({ "alerts:board": boardJson(ids) });
      const env = { ...ENV, ...PUSH, FOGCAST: kv };
      net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0) });
      await fire(FOG_CRON, env, T0);
      return { kv, env, before: kv.data.get("alerts:state") };
    };
    const all = ids.map((id) => report(id, T0 + 5 * MIN, id === "KABX" ? FOG : CLEAR));
    const cases = {
      "an empty list": [{ status: 200, body: "[]" }, "coverage", /Only 0 of 20 fields have a report under 75 minutes old\./],
      "204 No Content": [{ status: 204 }, "coverage", /Only 0 of 20/],
      "an HTML error page with status 200": [{ status: 200, body: "<html><body>Service unavailable</body></html>" }, "response", /did not answer with a list of reports/],
      "a JSON object, not a list": [{ status: 200, body: '{"error":"rate limited"}' }, "response", /did not answer with a list/],
      "HTTP 429": [{ status: 429, body: "Too Many Requests" }, "http", /HTTP 429/],
      "a dropped connection": [new TypeError("fetch failed"), "network", /could not be reached/],
      "a timeout": [Object.assign(new Error("t"), { name: "TimeoutError" }), "network", /could not be reached/],
      "only 13 of 20 stations": [{ status: 200, body: JSON.stringify(all.slice(0, 13)) }, "coverage", /Only 13 of 20/],
      "every report 3 hours old": [{ status: 200, body: JSON.stringify(ids.map((id) => report(id, T0 + 5 * MIN, { ageMin: 180 }))) }, "coverage", /Only 0 of 20/],
      "the station field renamed": [{ status: 200, body: JSON.stringify(all.map(({ icaoId, ...r }) => ({ ...r, stationId: icaoId }))) }, "format", /no station name or no time/],
      "the time field renamed": [{ status: 200, body: JSON.stringify(all.map(({ obsTime, ...r }) => ({ ...r, obsTimeUtc: obsTime }))) }, "format", /no station name or no time/],
      "the visibility field renamed": [{ status: 200, body: JSON.stringify(all.map(({ visib, ...r }) => ({ ...r, visibility: visib }))) }, "visibility", /Only 0 of 20 current reports carry a visibility\./],
      "the cloud list renamed": [{ status: 200, body: JSON.stringify(all.map(({ clouds, ...r }) => ({ ...r, sky: clouds }))) }, "format", /only 0 of 20 current reports carry a cloud list/],
    };
    for (const [name, [answer, kind, sentence]] of Object.entries(cases)) {
      const { kv, env, before } = await start();
      LOGS = [];
      let calls = net({ [AWC]: () => answer });
      assert.equal(await fire(FOG_CRON, env, T0 + 5 * MIN), "blind", name);
      assert.equal(calls.to(PO).length, 0, name);
      assert.equal(events("fog_tick")[0].reason, kind, name);
      const after = kv.json("alerts:state");
      assert.deepEqual(after.st, JSON.parse(before).st, `${name}: no station state may change on a failed read`);
      assert.equal(after.down.since, T0 + 5 * MIN, name);
      // 15 minutes of it: the pause message carries the reason in a sentence
      for (const m of [10, 15]) { net({ [AWC]: () => answer }); await fire(FOG_CRON, env, T0 + m * MIN); }
      calls = net({ [AWC]: () => answer, [PO]: [PUSH_OK] });
      await fire(FOG_CRON, env, T0 + 20 * MIN);
      assert.match(calls.pushes()[0].message, sentence, name);
    }
    // 14 of 20 current is the floor (the board's own 70%), and 14 passes
    const { kv, env } = await start();
    const calls = net({ [AWC]: () => ({ status: 200, body: JSON.stringify(all.slice(0, 14)) }), [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, T0 + 5 * MIN), "ok");
    assert.equal(calls.pushes()[0].title, "KABX below CAT I");
    assert.equal(kv.json("alerts:state").down, null);
  },
  async "fog: the state is written only when something changes, never twice in a tick, and stamped once an hour"() {
    const ids = BOARD_IDS(120);
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    let last = 0;
    const perTick = [];
    // 24 hours of an unchanging sky: 288 ticks
    for (let i = 0; i < 288; i++) {
      const t = T0 + i * 5 * MIN;
      net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), t) });
      await fire(FOG_CRON, env, t);
      perTick.push(kv.ops.puts.length - last);
      last = kv.ops.puts.length;
    }
    assert.ok(perTick.every((n) => n <= 1), "never two writes in a tick (KV allows one a second to a key)");
    assert.equal(kv.ops.puts.length, 27, "the baseline and a time stamp every 55 minutes: 27 writes in 24 hours");
    assert.equal(kv.ops.reads, 2 * 288, "two KV read calls a tick");
    assert.equal(kv.ops.deletes.length, 0);
    assert.ok(kv.data.get("alerts:state").length < 4000, "the state for 120 stations stays small");
  },
  async "fog: a stored board that cannot be used is said once, not every 5 minutes"() {
    const kv = fakeKV({ "alerts:board": "{oops" });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    let calls = net({ [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, T0), "bad-board");
    assert.equal(calls.to(AWC).length, 0);
    assert.equal(calls.pushes()[0].title, "Fog Atlas: fog alerts are OFF");
    assert.match(calls.pushes()[0].message, /^The stored board cannot be used: it is not valid JSON\. No field is being checked\./);
    calls = net({});
    await fire(FOG_CRON, env, T0 + 5 * MIN);
    assert.equal(calls.length, 0);
    // a different problem is new: too many stations
    kv.data.set("alerts:board", JSON.stringify({ ids: BOARD_IDS(161) }));
    calls = net({ [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 10 * MIN);
    assert.match(calls.pushes()[0].message, /it lists 161 stations and the limit is 160/);
    for (const [raw, why] of [
      ['{"stations":["KAAX"]}', 'it has no "ids" list'],
      ['{"ids":["KAAX","KSFO, KOAK","ksea!"]}', "2 of its 3 station ids are not four letters or digits"],
    ]) {
      assert.equal(fog.readBoard(raw).why, why);
    }
    // lower case and a bare list are accepted; duplicates collapse
    assert.deepEqual(fog.readBoard('["kaax","KABX","KAAX"]').ids, ["KAAX", "KABX"]);
    // a good board clears the complaint and starts with a silent baseline
    const ids = ["KAAX", "KABX", "KACX"];
    kv.data.set("alerts:board", boardJson(ids));
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0 + 15 * MIN) });
    assert.equal(await fire(FOG_CRON, env, T0 + 15 * MIN), "ok");
    assert.equal(calls.to(PO).length, 0);
    assert.equal(kv.json("alerts:state").bad, null);
  },
  async "fog: Pushover down for a moment loses no alert; a refusal is not repeated every 5 minutes"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    // 03:05 KAAX goes into fog while Pushover answers 502
    let calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0 + 5 * MIN), [PO]: [{ status: 502 }] });
    assert.equal(await fire(FOG_CRON, env, T0 + 5 * MIN), "ok", "the tick itself does not fail");
    assert.equal(kv.json("alerts:state").st.KAAX, "c", "not recorded as told");
    // 03:10 Pushover is back: the same change is found again and delivered
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0 + 10 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 10 * MIN);
    assert.equal(calls.pushes()[0].title, "KAAX below CAT I");
    assert.equal(kv.json("alerts:state").st.KAAX, "f");
    // 03:15 KABX goes into fog and Pushover REFUSES (a bad key). It was recorded
    // as told, so the refusal was not repeated, and KABX was never announced.
    const refuse = { status: 400, body: '{"status":0,"errors":["application token is invalid"]}' };
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), T0 + 15 * MIN), [PO]: [refuse] });
    await assert.rejects(fire(FOG_CRON, env, T0 + 15 * MIN), /Pushover refused a message/, "the dashboard shows the invocation as failed");
    let st = kv.json("alerts:state");
    assert.equal(st.st.KABX, "c", "not sent is not told");
    assert.deepEqual(st.refused, { since: T0 + 15 * MIN, last: T0 + 15 * MIN });
    // for the next hour Pushover is not asked again (repeating a refused request gets the sender blocked), and nothing is written
    const writes = kv.ops.puts.length;
    for (let m = 20; m <= 70; m += 5) {
      calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), T0 + m * MIN) });
      assert.equal(await fire(FOG_CRON, env, T0 + m * MIN), "ok");
      assert.equal(calls.to(PO).length, 0);
    }
    assert.ok(kv.ops.puts.length - writes <= 1, "at most the hourly time stamp");
    // an hour after the refusal it is tried again; still refused
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), T0 + 75 * MIN), [PO]: [refuse] });
    await assert.rejects(fire(FOG_CRON, env, T0 + 75 * MIN), /Pushover refused/);
    assert.deepEqual(kv.json("alerts:state").refused, { since: T0 + 15 * MIN, last: T0 + 75 * MIN });
    // the key is fixed: the next try is accepted, and says what was missed
    calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), T0 + 135 * MIN), [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, T0 + 135 * MIN), "ok");
    const late = calls.pushes()[0];
    assert.equal(late.title, "KABX below CAT I");
    assert.ok(late.message.startsWith("Pushover refused messages from 03:15Z on 2026-12-10 until now. What was sent in that time did not reach this phone.\nKABX "));
    st = kv.json("alerts:state");
    assert.equal(st.st.KABX, "f");
    assert.equal(st.refused, null);
  },
  async "fog: while KV will not take a write, a field in fog is said twice an hour, not twelve times"() {
    // The order was push first, remember second: with every put failing (the
    // account's free daily write quota is spent until 00:00 UTC) one field in
    // fog was 288 pushes in a day, one every 5 minutes. The state is written
    // first now; when it cannot be, the only memory left is the clock.
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);                                  // the baseline is saved; then KV stops taking writes
    kv.put = async () => { throw new Error("KV PUT failed: 429 Too Many Requests"); };
    const when = [];
    let failed = 0;
    for (let i = 1; i <= 288; i++) {
      const t = T0 + i * 5 * MIN;
      const calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), t), [PO]: () => PUSH_OK });
      await fire(FOG_CRON, env, t).catch((e) => { failed++; assert.match(e.message, /could not be saved to KV/); });
      for (const p of calls.pushes()) {
        when.push(t);
        assert.equal(p.title, "KAAX below CAT I");
        assert.ok(p.message.endsWith("(The alert state could not be saved, so this message may repeat.)"));
      }
    }
    assert.equal(failed, 288, "every such tick shows as a failed invocation");
    assert.equal(when.length, 48, "twice an hour for 24 hours (it was 288)");
    assert.ok(when[0] - T0 <= 30 * MIN, "the first one within half an hour");
    assert.ok(when.every((t) => new Date(t).getUTCMinutes() % 30 === 0));
  },
  async "fog: a priority-2 board alerts at priority 2; PAUSED never does"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX", "KAFX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids, { priority: 2 }) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    let calls = net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0 + 5 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 5 * MIN);
    const p = calls.pushes()[0];
    assert.equal(p.priority, 2);
    assert.equal(p.retry, 60);      // Pushover: an emergency message must say how often it repeats
    assert.equal(p.expire, 1800);   // and for how long
    for (const m of [10, 15, 20, 25]) {
      calls = net({ [AWC]: () => ({ status: 502 }), [PO]: () => PUSH_OK });
      await fire(FOG_CRON, env, T0 + m * MIN);
    }
    assert.equal(calls.pushes()[0].title, "Fog Atlas: fog alerts PAUSED");
    assert.equal(calls.pushes()[0].priority, 1, "an outage of the feed is not an emergency that repeats every minute");
  },
  async "fog: the newest of two reports for one field is the one judged, and a 3-field board may lose one"() {
    const ids = ["KAAX", "KABX", "KACX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    // AWC answers with two reports for KAAX: an older clear one and a newer one in fog (a SPECI), older first and then newer first
    for (const [m, order] of [[5, (a, b) => [a, b]], [70, (a, b) => [b, a]]]) {
      const t = T0 + m * MIN;
      const older = report("KAAX", t, { ageMin: 40 }), newer = report("KAAX", t, { ...FOG, ageMin: 5 });
      const body = JSON.stringify([...order(older, newer), report("KABX", t), report("KACX", t)]);
      const calls = net({ [AWC]: [{ status: 200, body }], [PO]: [PUSH_OK] });
      if (m === 70) {       // clear in between, so that the second order has a change to find
        net({ [AWC]: awc(skyOf(ids), T0 + 65 * MIN) });
        await fire(FOG_CRON, env, T0 + 65 * MIN);
        net({ [AWC]: [{ status: 200, body }], [PO]: [PUSH_OK] });
        const c2 = net({ [AWC]: [{ status: 200, body }], [PO]: [PUSH_OK] });
        await fire(FOG_CRON, env, t);
        assert.equal(c2.pushes()[0].title, "KAAX below CAT I");
        continue;
      }
      await fire(FOG_CRON, env, t);
      assert.equal(calls.pushes()[0].title, "KAAX below CAT I");
    }
    // one of three fields stops reporting: the other two are still checked (the 70% rule would need all three)
    const t = T0 + 3 * HOUR;
    net({ [AWC]: awc(skyOf(["KAAX", "KABX"]), t - 5 * MIN) });
    assert.equal(await fire(FOG_CRON, env, t - 5 * MIN), "ok");
    const calls = net({ [AWC]: awc(skyOf(["KAAX", "KABX"], { KABX: FOG }), t), [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, t), "ok");
    assert.equal(calls.pushes()[0].title, "KABX below CAT I");
    // two of three gone is a feed that cannot be trusted
    net({ [AWC]: awc(skyOf(["KAAX"]), t + 5 * MIN) });
    assert.equal(await fire(FOG_CRON, env, t + 5 * MIN), "blind");
  },
  async "fog: more fields than fit are counted, and the message stays inside Pushover's limit"() {
    const ids = BOARD_IDS(120);
    const kv = fakeKV({ "alerts:board": boardJson(ids) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    const calls = net({ [AWC]: awc(skyOf(ids, Object.fromEntries(ids.slice(0, 60).map((id) => [id, FOG]))), T0 + 5 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 5 * MIN);
    const p = calls.pushes()[0];
    assert.equal(p.title, "60 fields below CAT I");
    const lines = p.message.split("\n");
    assert.equal(lines.length, 9);
    assert.match(lines[8], /^and 52 more: KAIX, KAJX, /);
    assert.ok(p.message.length <= 1024);
    assert.equal(Object.values(kv.json("alerts:state").st).filter((s) => s === "f").length, 60);
  },
  async "fog: one test message on request, with or without a board"() {
    const kv = fakeKV({ "alerts:test": "1" });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    let calls = net({ [PO]: [PUSH_OK] });
    assert.equal(await fire(FOG_CRON, env, T0 + 5 * MIN), "no-board");
    assert.equal(calls.pushes()[0].title, "Fog Atlas: test message");
    assert.equal(calls.pushes()[0].message, "The cron Worker can reach this phone. Sent 03:05Z.");
    assert.equal(kv.data.has("alerts:test"), false, "the request is removed, so it is sent once");
    assert.equal(kv.ops.reads, 1, "still one read with no board");
    calls = net({});
    await fire(FOG_CRON, env, T0 + 10 * MIN);
    assert.equal(calls.length, 0);
    // refused by Pushover: still removed, or it would be refused again every 5 minutes
    kv.data.set("alerts:test", "1");
    calls = net({ [PO]: [{ status: 400, body: '{"status":0,"errors":["user identifier is invalid"]}' }] });
    await fire(FOG_CRON, env, T0 + 15 * MIN);
    assert.equal(kv.data.has("alerts:test"), false);
    // Pushover could not be reached at all: the request is kept, and the next tick tries again
    kv.data.set("alerts:test", "1");
    calls = net({ [PO]: [{ status: 502 }] });
    await fire(FOG_CRON, env, T0 + 20 * MIN);
    assert.equal(kv.data.has("alerts:test"), true, "the owner asked for a test message and has not had one");
    calls = net({ [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 25 * MIN);
    assert.equal(calls.pushes()[0].title, "Fog Atlas: test message");
    assert.equal(kv.data.has("alerts:test"), false);
  },
  async "fog: no log line names a station, the board's label or what a message said"() {
    const ids = ["KAAX", "KABX", "KACX", "KADX", "KAEX", "KAFX"];
    const kv = fakeKV({ "alerts:board": boardJson(ids, { label: "KSECRETBASE, 500 nm" }) });
    const env = { ...ENV, ...PUSH, FOGCAST: kv };
    LOGS = [];
    net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, env, T0);
    net({ [AWC]: awc(skyOf(ids, { KAAX: FOG }), T0 + 5 * MIN), [PO]: [PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 5 * MIN);
    for (const m of [10, 15, 20, 25]) {
      net({ [AWC]: () => ({ status: 502 }), [PO]: [PUSH_OK] });
      await fire(FOG_CRON, env, T0 + m * MIN);
    }
    net({ [AWC]: awc(skyOf(ids, { KAAX: FOG, KABX: FOG }), T0 + 30 * MIN), [PO]: [PUSH_OK, PUSH_OK] });
    await fire(FOG_CRON, env, T0 + 30 * MIN);
    net({ [GH]: [{ status: 204 }], [PO]: [PUSH_OK] });
    await fire(WATCHDOG, env, at("2026-12-10T14:50:00Z"));
    const text = LOGS.join("\n");
    assert.ok(LOGS.length >= 8, "the scenario logged");
    for (const word of [...ids, "KSECRETBASE", "below CAT I", "SM", "po-token-not-real", "po-user-not-real", "gh-token-not-real"]) {
      assert.ok(!text.includes(word), `a log line contains "${word}"`);
    }
    // counts are there, so the log is still worth reading
    assert.deepEqual(events("fog_tick").filter((e) => e.ok).map((e) => [e.stations, e.current, e.below, e.alerted]),
      [[6, 6, 0, 0], [6, 6, 1, 1], [6, 6, 2, 1]]);
  },

  // ---------------- the below-CAT-I rule, on real reports ----------------
  async "rule: the Worker's verdict equals the CHASE board's on 101 real reports of the same minute"() {
    // The board reads the cache file; the Worker reads the JSON API. Both were
    // captured within one minute. The board's reader refuses a file with
    // under 3,000 stations (its volume gate, FEED_MIN_STATIONS), so the 103
    // real rows are padded with copies of one of them under made-up station
    // ids; only real rows are compared.
    const lines = FX_CSV.split("\n").filter(Boolean);
    const proto = feed.splitCsvLine(lines.find((l) => l.includes("10+")));
    const padding = Array.from({ length: feed.FEED_MIN_STATIONS }, (_, i) => `"${proto[0]}",ZZ${String(i).padStart(4, "0")},${proto.slice(2).join(",")}`);
    const site = feed.readFeed(200, [...lines, ...padding].join("\n"), null, FX_NOW);
    assert.equal(site.ok, true, site.detail);
    let compared = 0;
    const seen = { f: 0, c: 0, u: 0, ovx: 0, plus: 0, noVis: 0, old: 0, rvr: 0 };
    for (const o of FX.reports) {
      const s = site.obs.get(o.icaoId);
      if (!s || s.obsMs !== o.obsTime * 1000) continue; // 2 stations had a newer report in one of the two captures
      compared++;
      // the board's own three steps (obStale / obCurrent in app/src/main.ts)
      const stale = !s.obsMs || FX_NOW - s.obsMs > feed.FEED_REPORT_MAX_AGE_MS;
      const expected = stale ? "u" : s.sub ? "f" : s.vis != null ? "c" : "u";
      const j = fog.judge(o, FX_NOW);
      assert.equal(j.s, expected, `${o.icaoId}: ${o.rawOb}`);
      assert.equal(j.vis, s.vis, `${o.icaoId} visibility`);
      assert.equal(j.ceil, s.ceil, `${o.icaoId} ceiling`);
      assert.equal(fog.lowestRvr(o.rawOb), s.rvr, `${o.icaoId} RVR`);
      seen[j.s]++;
      if ((o.clouds ?? []).some((c) => c.cover === "OVX")) seen.ovx++;
      if (typeof o.visib === "string" && o.visib.includes("+")) seen.plus++;
      if (!("visib" in o)) seen.noVis++;
      if (stale) seen.old++;
      if (s.rvr) seen.rvr++;
    }
    assert.equal(compared, 101);
    // the capture really holds every kind of report the rule has to get right
    assert.deepEqual(seen, { f: 24, c: 53, u: 24, ovx: 15, plus: 26, noVis: 14, old: 12, rvr: 5 });
  },
  async "rule: named real reports — vertical visibility, 10+, missing fields, the limits themselves, and age"() {
    const j = (id, now = FX_NOW) => fog.judge(fx(id), now);
    // ESNV: "0350 ... VV001" -> 0.22 SM, vertical visibility 100 ft as cover OVX
    assert.deepEqual(fx("ESNV").clouds, [{ cover: "OVX", base: 100 }]);
    assert.deepEqual([j("ESNV").s, j("ESNV").vis, j("ESNV").ceil], ["f", 0.22, 100]);
    // PAOM: 1 3/4SM but BKN001 -> below CAT I on the ceiling alone
    assert.deepEqual([j("PAOM").s, j("PAOM").vis, j("PAOM").ceil], ["f", 1.75, 100]);
    assert.equal(fog.fogLine("PAOM", fx("PAOM"), j("PAOM"), 95), "PAOM 03:11Z: 1.75 SM, ceiling 100 ft, R28/6000 FT, BR. ETE 1:35.");
    // EGAA: "0300 ... VV///": no height, so the visibility decides
    assert.deepEqual([j("EGAA").s, j("EGAA").vis, j("EGAA").ceil], ["f", 0.19, null]);
    // KPDT: "1/2SM ... VV002" sits ON both limits (0.5 is not under 0.5, 200 is not under 200)
    assert.match(fx("KPDT").rawOb, /1\/2SM R26\/2200V5500FT FG VV002/);
    assert.deepEqual([j("KPDT").s, j("KPDT").vis, j("KPDT").ceil], ["c", 0.5, 200]);
    // a "10+" and a "6+" visibility are clear, not unreadable
    const high = (r) => FX_NOW - r.obsTime * 1000 < 60 * MIN && !(r.clouds ?? []).some((c) => c.base < 200);
    const plus = FX.reports.find((r) => r.visib === "10+" && high(r));
    const six = FX.reports.find((r) => r.visib === "6+" && high(r));
    assert.deepEqual([fog.judge(plus, FX_NOW).s, fog.judge(plus, FX_NOW).vis], ["c", 10]);
    assert.deepEqual([fog.judge(six, FX_NOW).s, fog.judge(six, FX_NOW).vis], ["c", 10]);
    // CXDK: no visibility, no cloud list at all -> the report does not say
    assert.ok(!("visib" in fx("CXDK")) && !("clouds" in fx("CXDK")));
    assert.equal(j("CXDK", fx("CXDK").obsTime * 1000 + 10 * MIN).s, "u");
    // the API hands back the latest report however old: 7 days, 3 days, 17 hours
    for (const [id, minutes] of [["CBBC", 10460], ["KADH", 4730], ["KDAG", 1050]]) {
      assert.equal(Math.floor((FX_NOW - fx(id).obsTime * 1000) / MIN), minutes, id);
      assert.deepEqual([j(id).s, j(id).current], ["u", false], `${id} is ${minutes} minutes old and must not read as clear`);
    }
    // the same real fog report: current at 75 minutes, no verdict one millisecond later
    const esnv = fx("ESNV").obsTime * 1000;
    assert.equal(fog.judge(fx("ESNV"), esnv + 75 * MIN).s, "f");
    assert.equal(fog.judge(fx("ESNV"), esnv + 75 * MIN + 1).s, "u");
    // not reports at all
    for (const junk of [null, undefined, {}, { icaoId: "KAAX" }, { icaoId: "KAAX", obsTime: "1795575000", visib: 0.1 }]) {
      assert.equal(fog.judge(junk, FX_NOW).s, "u");
    }
    // FEW and SCT are not a ceiling; BKN, OVC and OVX under 200 ft are
    const mk = (visib, clouds) => fog.judge({ icaoId: "KAAX", obsTime: FX_NOW / 1000 - 60, visib, clouds }, FX_NOW).s;
    assert.equal(mk("10+", [{ cover: "FEW", base: 100 }, { cover: "SCT", base: 100 }]), "c");
    for (const cover of ["BKN", "OVC", "OVX"]) assert.equal(mk("10+", [{ cover, base: 100 }]), "f", cover);
    assert.equal(mk("10+", [{ cover: "OVC", base: 200 }]), "c");
    assert.equal(mk(0.49, []), "f");
    assert.equal(mk(0.5, []), "c");
  },
  async "rule: the Worker uses the board's own functions and numbers, not copies"() {
    const src = readFileSync(new URL("fog.js", SRC), "utf8");
    const imported = src.match(/import \{([^}]+)\} from "\.\.\/\.\.\/app\/src\/feed\.ts";/);
    assert.ok(imported, "fog.js imports the rule from app/src/feed.ts");
    for (const name of ["jsonVis", "jsonCeil", "subCat1", "FEED_REPORT_MAX_AGE_MS", "FEED_MIN_CURRENT_SHARE", "FEED_MIN_VIS_FILL", "METAR_CHUNK", "chunkIds"]) {
      assert.ok(imported[1].includes(name), `${name} comes from feed.ts`);
    }
    assert.equal(feed.FEED_REPORT_MAX_AGE_MS, 75 * MIN);
    assert.equal(feed.METAR_CHUNK, 40);
    // the one copy: the RVR pattern, which feed.ts does not export
    const theirs = repo("app/src/feed.ts").match(/const RVR_RE = (\/.+\/g);/)[1];
    const ours = src.match(/const RVR_RE = (\/.+\/g);/)[1];
    assert.equal(ours, theirs, "RVR_RE in cron/src/fog.js has drifted from app/src/feed.ts");
  },

  // ---------------- configuration ----------------
  async "config: every Cron Trigger in wrangler.jsonc has a handler, and nothing else does"() {
    const cfg = repo("cron/wrangler.jsonc");
    const block = cfg.slice(cfg.indexOf('"crons"'), cfg.indexOf("]", cfg.indexOf('"crons"')));
    const crons = [...block.matchAll(/^\s*"([^"]+)"/gm)].map((m) => m[1]).filter((c) => c !== "crons");
    assert.equal(crons.length, 4);
    assert.ok(crons.length <= 5, "Workers Free allows 5 Cron Triggers per account");
    assert.equal(fog.FOG_CRON, FOG_CRON);
    assert.deepEqual([...crons].sort(), [...Object.keys(JOBS), FOG_CRON].sort());
    assert.ok(crons.includes(clock.HEARTBEAT_CRON), "the heartbeat rides a trigger that exists");
    assert.equal(JOBS[clock.HEARTBEAT_CRON].workflow, "watchdog.yml");
    // each one does its own thing: the fog trigger starts no workflow, the others check no fog
    for (const cron of crons) {
      const kv = fakeKV();
      const calls = net({ [GH]: [{ status: 204 }] });
      const out = await fire(cron, { ...ENV, ...PUSH, FOGCAST: kv, HEARTBEAT_UTC_HOUR: "23" }, T0);
      if (cron === FOG_CRON) {
        assert.equal(out, "no-board");
        assert.equal(calls.length, 0);
      } else {
        assert.equal(out, 204);
        assert.ok(calls[0].url.endsWith(`/workflows/${JOBS[cron].workflow}/dispatches`));
      }
    }
    // a trigger nobody mapped fails loudly
    net({ [PO]: [PUSH_OK] });
    await assert.rejects(fire("*/7 * * * *", { ...ENV, ...PUSH, FOGCAST: fakeKV() }, T0), /no workflow is mapped/);
  },
  async "config: the handler itself waits for its work, and fails when the work fails"() {
    // waitUntil alone is documented as good for 30 seconds; a fog check
    // against a slow aviationweather.gov can take longer
    const slow = (answer) => { globalThis.fetch = async () => { await new Promise((r) => setTimeout(r, 30)); return new Response(answer.body ?? null, { status: answer.status }); }; };
    for (const cron of [HOURLY, FOG_CRON]) {
      LOGS = [];
      slow(cron === HOURLY ? { status: 204 } : { status: 502 });
      const kv = fakeKV({ "alerts:board": boardJson(["KAAX", "KABX"]) });
      let waited;
      const env = cron === HOURLY ? { ...ENV, FOGCAST: kv } : { ...ENV, ...PUSH, FOGCAST: kv };
      await worker.scheduled({ cron, scheduledTime: T0 }, env, { waitUntil: (p) => { waited = p; } });
      // by the time the handler has returned, the work is done: its log line is written
      assert.equal(events(cron === HOURLY ? "dispatch" : "fog_tick").length, 1, `${cron}: the handler returned before its work finished`);
      assert.ok(waited instanceof Promise);
      await waited;
    }
    slow({ status: 401, body: '{"message":"Bad credentials"}' });
    let waited;
    await assert.rejects(worker.scheduled({ cron: HOURLY, scheduledTime: T0 }, ENV, { waitUntil: (p) => { waited = p; } }), /HTTP 401/);
    await assert.rejects(waited, /HTTP 401/, "the same failure is what waitUntil records");
  },
  async "config: no HTTP surface, no secret in the file, and the KV keys stay under alerts:"() {
    assert.deepEqual(Object.keys(worker), ["scheduled"], "the Worker exports a scheduled handler and nothing else");
    assert.equal(worker.fetch, undefined);
    // the entry module exports what the Worker deployed on 2026-11-24 exports, and no more
    assert.deepEqual(Object.keys(index).sort(), ["JOBS", "default", "dispatch"]);
    const cfg = repo("cron/wrangler.jsonc");
    const bare = cfg.replace(/^\s*\/\/.*$/gm, "").replace(/,\s*\/\/.*$/gm, ",").replace(/"\s*\/\/.*$/gm, '"');
    assert.match(bare, /"workers_dev":\s*false/);
    assert.match(bare, /"preview_urls":\s*false/);
    for (const key of ["routes", "route", "PUSHOVER_TOKEN", "PUSHOVER_USER", "GH_PAT"]) {
      assert.ok(!bare.includes(`"${key}"`), `wrangler.jsonc must not set "${key}"`);
    }
    // the same namespace the site's forecast lives in
    const id = bare.match(/"binding":\s*"FOGCAST",\s*"id":\s*"([0-9a-f]{32})"/)[1];
    assert.ok(repo(".github/workflows/forecast.yml").includes(`KV_NAMESPACE: ${id}`));
    assert.ok(repo("app/wrangler.toml").includes(`id = "${id}"`));
    // every key this Worker touches starts with "alerts:"
    assert.deepEqual([fog.BOARD_KEY, fog.STATE_KEY, fog.TEST_KEY, clock.CLOCK_KEY],
      ["alerts:board", "alerts:state", "alerts:test", "alerts:clock"]);
    for (const file of ["index.js", "clock.js", "fog.js", "notify.js"]) {
      const src = readFileSync(new URL(file, SRC), "utf8");
      for (const m of src.matchAll(/\bkv\.(get|put|delete|list|getWithMetadata)\(([^)]*)/g)) {
        assert.match(m[2], /^\[?[A-Z]+_KEY\b/, `${file}: kv.${m[1]}(${m[2]} must use a named alerts: key`);
      }
      assert.ok(!/\.list\(/.test(src), `${file} must not list keys`);
    }
    // no Pages Function can serve them: each reads fixed keys, none takes a key from the request
    for (const fn of ["forecast.ts", "deploy.ts", "scorecard.ts", "metar.ts", "now.ts"]) {
      const src = repo(`app/functions/api/${fn}`);
      const reads = [...src.matchAll(/FOGCAST\.(\w+)\(([^,)]*)/g)];
      for (const [, method, arg] of reads) {
        assert.equal(method, "get", `${fn} only reads`);
        assert.match(arg, /^"(current|deploy|scorecard)"$/, `${fn} reads a fixed public key, got ${arg}`);
      }
      if (["metar.ts", "now.ts"].includes(fn)) assert.equal(reads.length, 0, `${fn} does not touch KV`);
    }
    const fns = spawnSync("ls", [fileURLToPath(new URL("../app/functions/api/", import.meta.url))], { encoding: "utf8" }).stdout.trim().split("\n").sort();
    assert.deepEqual(fns, ["deploy.ts", "forecast.ts", "metar.ts", "now.ts", "scorecard.ts"], "a new Pages Function: check that it cannot serve alerts: keys, then add it here");
  },

  // ---------------- the board helper ----------------
  async "helper: the board filter, one rule at a time"() {
    // a base and eleven fields due north of it (1 degree of latitude is 60 nm), each there for one rule
    const A = (icao, lat) => ({ icao, lat, lon: -100 });
    const end = (o = {}) => ({ e: "17", len: 8000, als: "ALSF2", rvr: "T", ils: "I", lpv: 0, tier: 200, ...o });
    const airports = [A("KBAS", 40), A("KC12", 40.1), A("KTWO", 40.2), A("KNEA", 40.5), A("KNAL", 41), A("KODA", 42), A("KSHT", 43),
      A("KTIR", 44), A("KNRV", 45), A("KNCH", 46), A("KFAR", 47.4), A("KOUT", 48.5), A("C12", 40.3)];
    const chase = {
      KBAS: [end()],                                   // the base itself
      KC12: [end({ als: "MALSR", tier: 250 })],
      KTWO: [end({ als: null }), end({ e: "35", als: "ALSF1" })], // one end fails, the other passes
      KNEA: [end()],
      KNAL: [end({ als: null })],                      // no approach lights
      KODA: [end({ als: "ODALS" })],                   // lights, but not a selected system
      KSHT: [end({ len: 4999 })],                      // runway too short
      KTIR: [end({ tier: 400 })],                      // minimums not low enough
      KNRV: [end({ rvr: null })],                      // no RVR equipment: fine unless it is asked for
      // KNCH is an airport of the atlas with no chase record
      KFAR: [end()],                                   // 444 nm: in range
      KOUT: [end()],                                   // 510 nm: out of range
      C12: [end()],                                    // not a 4-character id: AWC cannot be asked for it
    };
    const list = (prefs) => chaseCandidates(airports, chase, "KBAS", prefs).map((c) => c.icao);
    assert.deepEqual(list(), ["KC12", "KTWO", "C12", "KNEA", "KNRV", "KFAR"], "defaults: ALSF-2, ALSF-1 or MALSR, 5,000 ft, tier 250, within 2 h at 250 kt; nearest first");
    assert.deepEqual(list({ rvrReq: true }), ["KC12", "KTWO", "C12", "KNEA", "KFAR"]);
    assert.deepEqual(list({ als: ["OTHER"] }), ["KODA"], "OTHER is every system that has no chip of its own");
    assert.deepEqual(list({ als: ["ALSF2"] }), ["C12", "KNEA", "KNRV", "KFAR"]);
    assert.deepEqual(list({ minLen: 4000 }), ["KC12", "KTWO", "C12", "KNEA", "KSHT", "KNRV", "KFAR"]);
    assert.deepEqual(list({ maxTier: 400 }), ["KC12", "KTWO", "C12", "KNEA", "KTIR", "KNRV", "KFAR"]);
    assert.deepEqual(list({ maxTier: 200 }), ["KTWO", "C12", "KNEA", "KNRV", "KFAR"]);
    assert.deepEqual(list({ maxEteH: 2.1 }), ["KC12", "KTWO", "C12", "KNEA", "KNRV", "KFAR", "KOUT"]);
    assert.deepEqual(list({ speed: 160 }), ["KC12", "KTWO", "C12", "KNEA", "KNRV"], "the same 2 hours reach 320 nm at 160 kt");
    assert.equal(chaseCandidates(airports, chase, "KZZZ"), null, "an unknown base is refused, not answered with an empty board");
    // the board itself
    const built = buildBoard(airports, chase, "KBAS", {}, { now: new Date("2026-12-08T03:00:00Z") });
    // No label unless one is asked for. The default was "<base>, <radius> nm",
    // which the daily heartbeat then sent to Pushover every day: the base was
    // to reach no outside service.
    assert.deepEqual(built.board, {
      v: 1, made: "2026-12-08T03:00:00Z", priority: 1, repeatMin: 60, speedKt: 250,
      ids: ["KC12", "KTWO", "KNEA", "KNRV", "KFAR"],
      eteMin: { KC12: 1, KTWO: 3, KNEA: 7, KNRV: 72, KFAR: 107 },
    });
    assert.deepEqual([built.found, built.unaskable, built.dropped], [6, 1, 0]);
    // the Worker accepts what the helper writes, and reads it the same way
    const read = fog.readBoard(JSON.stringify(built.board));
    assert.deepEqual([read.ok, read.ids, read.madeMs, read.priority, read.repeatMs, read.label],
      [true, built.board.ids, at("2026-12-08T03:00:00Z"), 1, 60 * MIN, ""]);
    assert.ok(!JSON.stringify(built.board).includes("KBAS"), "the base is nowhere in the board");
    assert.equal(buildBoard(airports, chase, "KBAS", {}, { label: "north board" }).board.label, "north board");
    // more fields than asked for: the nearest are kept and the rest are counted, never dropped in silence
    const cut = buildBoard(airports, chase, "KBAS", {}, { max: 2 });
    assert.deepEqual([cut.board.ids, cut.dropped], [["KC12", "KTWO"], 3]);
    assert.equal(buildBoard(airports, chase, "KBAS", {}, { label: "" }).board.label, undefined, "--no-label (now the default) is still accepted");
    // The LED choice is the panel's own rule (endShown in app/src/led.ts). The
    // helper had its own copy of the filter and no LED term: with the panel on
    // "avoid LED ends" the phone board listed fields the panel hides.
    const led = { ...chase, KC12: [end({ als: "MALSR", tier: 250, led: 1 })],                   // its only end is LED
      KNEA: [end({ led: 1 }), end({ e: "35", als: "ALSF1" })],                                   // LED on 17, not on 35
      KFAR: [end({ led_unk: 1 })], KNRV: [end({ rvr: null, led_was: 1 })] };                     // runway not named; on the old list only
    const listLed = (choice) => chaseCandidates(airports, led, "KBAS", { led: choice }).map((c) => c.icao);
    assert.deepEqual(listLed("all"), ["KC12", "KTWO", "C12", "KNEA", "KNRV", "KFAR"]);
    assert.deepEqual(listLed("avoid"), ["KTWO", "C12", "KNEA"], "KNEA stays on its other end; an unnamed-runway field and a listed end are avoided");
    assert.deepEqual(listLed("only"), ["KC12", "KNEA", "KFAR"], "an end that is only on the old list is not counted as LED");
    assert.deepEqual(listLed("nonsense"), listLed("all"), "a choice this code does not know hides nothing");
    assert.equal(buildBoard(airports, chase, "KBAS", {}, { max: 9999 }).board.ids.length, 5);
  },
  async "helper: on the atlas's own data it lists what the CHASE panel lists"() {
    const airports = JSON.parse(repo("app/public/data/airports.json")).airports;
    const chaseFile = JSON.parse(repo("app/public/data/chase.json"));
    const chase = chaseFile.airports;
    const n = (base, prefs) => chaseCandidates(airports, chase, base, prefs).length;
    // Board sizes the readiness review measured on the live site on 2026-11-24
    // (default filters, 250 kt, 2 h). They belong to that day's airport data:
    // after a NASR refresh they may move, and the comparison is skipped.
    const measured = [["KSEA", {}, 47], ["KSFO", {}, 52], ["KDEN", {}, 87], ["KORD", {}, 165], ["KORD", { maxEteH: 4 }, 422]];
    if (chaseFile.meta?.nasr_file === "28DaySubscription_Effective_2026-10-29.zip") {
      for (const [base, prefs, size] of measured) assert.equal(n(base, prefs), size, base);
    } else {
      SKIPPED.push(`board sizes not compared with the 2026-11-24 review: chase.json is now from ${chaseFile.meta?.nasr_file}`);
    }
    // whatever the data: a big hub's default board is over the Worker's limit and is cut, with a count
    const all = n("KORD");
    assert.ok(all > fog.MAX_BOARD_IDS, "KORD's default board is larger than the Worker's limit");
    const big = buildBoard(airports, chase, "KORD", {}, { max: 500 });
    assert.deepEqual([big.board.ids.length, big.dropped], [fog.MAX_BOARD_IDS, all - fog.MAX_BOARD_IDS]);
    assert.equal(buildBoard(airports, chase, "KORD").dropped, all - 120, "120 unless asked otherwise");
    const b = buildBoard(airports, chase, "KDEN").board;
    assert.equal(b.ids.length, Math.min(n("KDEN"), 120));
    assert.ok(b.ids.length > 20 && !b.ids.includes("KDEN"));
    const etes = b.ids.map((id) => b.eteMin[id]);
    assert.deepEqual(etes, [...etes].sort((x, y) => x - y), "nearest first");
    assert.ok(etes.at(-1) <= 120);
    assert.equal(fog.readBoard(JSON.stringify(b)).ok, true);
  },
  async "helper: the board goes to stdout only, and nothing board-shaped is tracked in the repository"() {
    const script = fileURLToPath(new URL("./make_board.mjs", import.meta.url));
    const run = spawnSync(process.execPath, [script, "--base", "KDEN"], { encoding: "utf8" });
    assert.equal(run.status, 0, run.stderr);
    const out = JSON.parse(run.stdout);
    assert.ok(out.ids.length > 20 && out.label === undefined && !run.stdout.includes("KDEN"), "no label by default: the base is not in the board");
    assert.match(run.stderr, new RegExp(`^${out.ids.length} fields within 500 nm of KDEN at 250 kt \\(nearest ${out.ids[0]} \\d:\\d\\d, farthest ${out.ids.at(-1)} \\d:\\d\\d\\)\\.$`, "m"));
    // options reach the board
    const opt = spawnSync(process.execPath, [script, "--base", "kden", "--radius-nm", "200", "--speed", "200", "--no-label", "--priority", "2", "--repeat-min", "30", "--als", "alsf2,malsr"], { encoding: "utf8" });
    assert.equal(opt.status, 0, opt.stderr);
    const o2 = JSON.parse(opt.stdout);
    assert.deepEqual([o2.label, o2.priority, o2.repeatMin, o2.speedKt], [undefined, 2, 30, 200]);
    assert.ok(o2.ids.length > 0 && o2.ids.length < out.ids.length && Math.max(...Object.values(o2.eteMin)) <= 60);
    // the LED choice, as the panel applies it; and a label only when asked for
    const led = spawnSync(process.execPath, [script, "--base", "KDEN", "--led", "avoid", "--label", "north board"], { encoding: "utf8" });
    assert.equal(led.status, 0, led.stderr);
    const o3 = JSON.parse(led.stdout);
    assert.equal(o3.label, "north board");
    assert.ok(o3.ids.length < out.ids.length && o3.ids.every((id) => out.ids.includes(id)), "avoiding LED ends only ever removes fields");
    assert.match(led.stderr, / at 250 kt, avoiding LED ends \(nearest /);
    assert.equal(spawnSync(process.execPath, [script, "--base", "KDEN", "--led", "maybe"], { encoding: "utf8" }).status, 2);
    assert.equal(spawnSync(process.execPath, [script, "--base", "ZZZZ"], { encoding: "utf8" }).status, 1);
    assert.equal(spawnSync(process.execPath, [script], { encoding: "utf8" }).status, 2);
    assert.ok(!/writeFile|appendFile|createWriteStream/.test(readFileSync(script, "utf8")), "the helper writes no file");
    const ignore = readFileSync(new URL("./.gitignore", import.meta.url), "utf8");
    for (const pattern of ["*board*.json", ".dev.vars"]) assert.ok(ignore.split("\n").includes(pattern), `cron/.gitignore lists ${pattern}`);
  },

  // ---------------- cost ----------------
  async "cost: one fog tick for a 120-station board, against the Free plan's 10 ms of CPU"() {
    await cpuOfTicks(100); // warm up
    const quiet = await cpuOfTicks(400);
    const busy = await cpuOfTicks(400, true);
    assert.equal(Object.keys(quiet.kv.json("alerts:state").st).length, 120);
    assert.equal(quiet.kv.ops.puts.length, 1, "a quiet run writes its baseline and nothing else");
    assert.ok(busy.kv.ops.puts.length >= 399, "the busy run wrote on every tick");
    // a new process and a single tick: nothing is warm, as in a new isolate
    const cold = spawnSync(process.execPath, [fileURLToPath(import.meta.url), "--cpu-cold"], { encoding: "utf8", env: process.env });
    assert.equal(cold.status, 0, cold.stderr);
    COST = { quietMs: quiet.micros / 1000, busyMs: busy.micros / 1000, coldMs: Number(cold.stdout.trim()) / 1000, bytes: quiet.bytes };
    // a wide margin, not the limit itself
    assert.ok(quiet.micros < 2000, `a quiet tick used ${COST.quietMs.toFixed(2)} ms of CPU`);
    assert.ok(busy.micros < 3000, `a busy tick used ${COST.busyMs.toFixed(2)} ms of CPU`);
    // subrequests (50 allowed): 3 to AWC, plus at most 2 pushes. KV: 2 read calls, at most 1 write.
    const ids = BOARD_IDS(120);
    const kv2 = fakeKV({ "alerts:board": boardJson(ids) });
    const calls = net({ [AWC]: awc(skyOf(ids), T0) });
    await fire(FOG_CRON, { ...ENV, ...PUSH, FOGCAST: kv2 }, T0);
    assert.equal(calls.length, 3);
    assert.equal(kv2.ops.reads + kv2.ops.puts.length, 3);
    // the largest board allowed: 160 stations, 4 requests
    const max = BOARD_IDS(fog.MAX_BOARD_IDS);
    const calls160 = net({ [AWC]: awc(skyOf(max), T0) });
    await fire(FOG_CRON, { ...ENV, ...PUSH, FOGCAST: fakeKV({ "alerts:board": boardJson(max) }) }, T0);
    assert.equal(calls160.length, 4);
  },
};

let COST = null;
const SKIPPED = []; // comparisons a case could not make, said out loud at the end
let failed = 0;
const only = process.argv[2] && !process.argv[2].startsWith("--") ? process.argv[2] : null;
console.log = console.error = (...a) => { LOGS.push(a.join(" ")); };
for (const [name, fn] of Object.entries(tests)) {
  if (only && !name.includes(only)) continue;
  LOGS = [];
  try {
    await fn();
    real.log("ok  ", name);
  } catch (e) {
    failed++;
    real.log("FAIL", name, "-", String(e.message).split("\n").slice(0, 12).join("\n     "));
  }
}
globalThis.fetch = real.fetch;
Date.now = real.now;
console.log = real.log;
console.error = real.error;
if (COST) {
  console.log(`cost: one fog tick, 120 stations, ${Math.round(COST.bytes / 1024)} KB of reports: `
    + `${COST.quietMs.toFixed(2)} ms CPU when nothing changes, ${COST.busyMs.toFixed(2)} ms with a push and a state write (means of 400), `
    + `${COST.coldMs.toFixed(2)} ms for the first tick of a new process. Limit: 10 ms.`);
}
for (const s of SKIPPED) console.log("note:", s);
console.log(failed ? "FAILED" : "passed");
process.exit(failed ? 1 : 0);
