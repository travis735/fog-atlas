// Offline test for /api/deploy (app/functions/api/deploy.ts). No dependencies; KV and the edge cache
// are scripted fakes:
//   node app/test/test_deploy_api.mjs                      (the working tree's deploy.ts)
//   DEPLOY_TS=/path/to/deploy.ts node app/test/test_deploy_api.mjs   (another copy, e.g. the one at HEAD)
//
// Why it exists: the planner payload is rebuilt every hour since 2026-11-25 and this function held
// each copy for 30 minutes at the edge and 30 more in the browser; its 503 was cacheable for a
// minute; and a KV read that threw escaped as an unhandled exception.
import assert from "node:assert/strict";

const path = process.env.DEPLOY_TS ?? new URL("../functions/api/deploy.ts", import.meta.url).href;
const { onRequest } = await import(path);

let stored, kvCalls, pending;
function edge(hit) {
  stored = [];
  globalThis.caches = { default: {
    match: async () => hit ?? undefined,
    put: async (key, res) => { stored.push({ key: key.url, cc: res.headers.get("cache-control"), body: await res.text() }); },
  } };
}
function ctx(kv) {
  kvCalls = [];
  pending = [];
  return {
    request: new Request("https://fogatlas.org/api/deploy?x=1"),
    env: { FOGCAST: { get: async (key, type) => { kvCalls.push([key, type]); return kv(); } } },
    waitUntil: (p) => pending.push(p),
  };
}
const stream = (text) => new Response(text).body;
const PAYLOAD = JSON.stringify({ meta: { format: 2, generated: "2026-12-08T10:25:07Z" }, airports: {} });

const tests = {
  async "a payload is streamed from KV, 5 min at the edge and 1 min in the browser"() {
    edge(null);
    const r = await onRequest(ctx(() => stream(PAYLOAD)));
    assert.equal(r.status, 200);
    assert.equal(r.headers.get("cache-control"), "public, max-age=60, s-maxage=300");
    assert.equal(r.headers.get("content-type"), "application/json");
    assert.equal(r.headers.get("access-control-allow-origin"), "*");
    assert.deepEqual(kvCalls, [["deploy", "stream"]]);
    assert.equal(await r.text(), PAYLOAD);
    await Promise.all(pending);
    // one edge copy for every query string, with the same headers
    assert.equal(stored.length, 1);
    assert.equal(stored[0].key, "https://fogatlas.org/api/deploy");
    assert.equal(stored[0].cc, "public, max-age=60, s-maxage=300");
    assert.equal(stored[0].body, PAYLOAD);
  },
  async "an hourly rebuild is on screen within about 10 minutes"() {
    edge(null);
    const cc = (await onRequest(ctx(() => stream(PAYLOAD)))).headers.get("cache-control");
    const edgeTtl = Number(/s-maxage=(\d+)/.exec(cc)?.[1] ?? /max-age=(\d+)/.exec(cc)[1]);
    const browserTtl = Number(/(?:^|[ ,])max-age=(\d+)/.exec(cc)[1]);
    // worst case: KV propagation (60 s) + its own read cache (60 s) + edge copy + browser copy
    assert.ok(60 + 60 + edgeTtl + browserTtl <= 600, `${cc}: ${60 + 60 + edgeTtl + browserTtl} s`);
    assert.ok(edgeTtl >= 300, "one KV read per edge location per 5 minutes at most");
  },
  async "the edge copy is served without reading KV"() {
    edge(new Response(PAYLOAD, { headers: { "x-from": "edge" } }));
    const r = await onRequest(ctx(() => { throw new Error("KV must not be read"); }));
    assert.equal(r.headers.get("x-from"), "edge");
    assert.deepEqual(kvCalls, []);
  },
  async "no payload in KV is a 503 that is never cached"() {
    edge(null);
    const r = await onRequest(ctx(() => null));
    assert.equal(r.status, 503);
    assert.equal(r.headers.get("cache-control"), "no-store");
    assert.deepEqual(await r.json(), { error: "no deploy data yet" });
    await Promise.all(pending);
    assert.equal(stored.length, 0);
  },
  async "a KV read that throws is a 502 that is never cached"() {
    edge(null);
    const r = await onRequest(ctx(() => { throw new Error("KV GET failed: 500 Internal Server Error"); }));
    assert.equal(r.status, 502);
    assert.equal(r.headers.get("cache-control"), "no-store");
    assert.match((await r.json()).error, /^planner data unreadable: KV GET failed/);
    await Promise.all(pending);
    assert.equal(stored.length, 0);
  },
};

let fails = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { await fn(); console.log("ok  ", name); }
  catch (e) { fails++; console.log("FAIL", name, "-", e.message.split("\n")[0]); }
}
console.log(fails ? "FAILED" : "passed");
process.exit(fails ? 1 : 0);
