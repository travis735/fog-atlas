// Offline test for the two Pages Functions the live board depends on. No
// dependencies; fetch is replaced with a scripted fake, nothing touches AWC:
//   node app/test/test_api.mjs
//
// Why it exists: /api/metar accepted exactly one id while the CHASE board had
// been sending up to 150 since 2026-07-10 (every call answered 400 and was
// swallowed), and a thrown upstream fetch in /api/now escaped as an unhandled
// exception instead of the 502 the board knows how to show.
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import { onRequestGet as now } from "../functions/api/now.ts";
import { onRequestGet as metar } from "../functions/api/metar.ts";

const realFetch = globalThis.fetch;
let calls = [];
function upstream(reply) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    if (reply instanceof Error) throw reply;
    return reply();
  };
}
const get = (path) => ({ request: new Request(`https://fogatlas.org${path}`) });
const CSV = "raw_text,station_id\n\"METAR KSFO 242156Z 07004KT 10SM\",KSFO\n";

const tests = {
  async "/api/now: a thrown upstream fetch is a 502 that is never cached"() {
    upstream(new TypeError("fetch failed"));
    const r = await now(get("/api/now"));
    assert.equal(r.status, 502);
    assert.equal(r.headers.get("cache-control"), "no-store");
    assert.match(await r.text(), /^upstream unavailable/);
  },
  async "/api/now: an upstream error status is a 502 that is never cached"() {
    for (const status of [403, 500, 503, 504]) {
      upstream(() => new Response("<html>error</html>", { status }));
      const r = await now(get("/api/now"));
      assert.equal(r.status, 502, String(status));
      assert.equal(r.headers.get("cache-control"), "no-store");
      assert.match(await r.text(), new RegExp(`^upstream unavailable: .*HTTP ${status}`));
    }
  },
  async "/api/now: success is the decompressed CSV, cacheable for 180 s as before"() {
    upstream(() => new Response(gzipSync(CSV), { status: 200, headers: { "content-type": "application/gzip" } }));
    const r = await now(get("/api/now"));
    assert.equal(r.status, 200);
    assert.equal(r.headers.get("cache-control"), "public, max-age=180");
    // text/plain so the edge compresses it: Cloudflare's list of compressible
    // types has text/plain and application/json but not text/csv, and the
    // feed went out uncompressed (985 KB a read) until 2026-11-25
    assert.equal(r.headers.get("content-type"), "text/plain; charset=utf-8");
    assert.equal(await r.text(), CSV);
    assert.equal(calls[0].url, "https://aviationweather.gov/data/cache/metars.cache.csv.gz");
    assert.deepEqual(calls[0].init.cf, { cacheTtl: 180, cacheEverything: true });
    // a runtime that already decoded the gzip is passed through untouched
    upstream(() => new Response(CSV, { status: 200, headers: { "content-encoding": "gzip" } }));
    assert.equal(await (await now(get("/api/now"))).text(), CSV);
  },
  async "/api/metar: one id still works, exactly as before"() {
    upstream(() => new Response('[{"icaoId":"KSFO"}]', { status: 200 }));
    const r = await metar(get("/api/metar?ids=KSFO"));
    assert.equal(r.status, 200);
    assert.equal(r.headers.get("cache-control"), "public, max-age=120");
    assert.equal(r.headers.get("content-type"), "application/json");
    assert.deepEqual(await r.json(), [{ icaoId: "KSFO" }]);
    assert.equal(calls[0].url, "https://aviationweather.gov/api/data/metar?ids=KSFO&hours=1&format=json");
  },
  async "/api/metar: a comma list is accepted (the board's warm-start form)"() {
    upstream(() => new Response("[]", { status: 200 }));
    assert.equal((await metar(get("/api/metar?ids=KSFO,KOAK&hours=3"))).status, 200);
    assert.equal(calls[0].url, "https://aviationweather.gov/api/data/metar?ids=KSFO,KOAK&hours=3&format=json");
    // %2C is the same list
    assert.equal((await metar(get("/api/metar?ids=KSMF%2CKSCK&hours=3"))).status, 200);
    assert.equal(calls.at(-1).url, "https://aviationweather.gov/api/data/metar?ids=KSMF,KSCK&hours=3&format=json");
    // 50 ids pass, hours are capped at 6
    const fifty = Array.from({ length: 50 }, (_, i) => `K${String(i).padStart(3, "0")}`).join(",");
    assert.equal((await metar(get(`/api/metar?ids=${fifty}&hours=99`))).status, 200);
    assert.ok(calls.at(-1).url.endsWith("&hours=6&format=json"));
  },
  async "/api/metar: anything that is not a list of ICAO ids is refused before any upstream request"() {
    upstream(() => new Response("[]", { status: 200 }));
    const fiftyOne = Array.from({ length: 51 }, (_, i) => `K${String(i).padStart(3, "0")}`).join(",");
    for (const q of ["", "?ids=", "?ids=ksfo", "?ids=KSFO,", "?ids=,KSFO", "?ids=KSFO,,KOAK", "?ids=KSFO,KOA", "?ids=KSFO;KOAK",
      "?ids=KSFO%20KOAK", "?ids=KSFO,KOAK%26format=raw", "?ids=KSFO,../x", `?ids=${fiftyOne}`]) {
      const r = await metar(get(`/api/metar${q}`));
      assert.equal(r.status, 400, q);
      assert.equal(r.headers.get("cache-control"), "no-store", q);
    }
    assert.equal(calls.length, 0, "a refused request must not reach AWC");
    assert.equal(await (await metar(get(`/api/metar?ids=${fiftyOne}`))).text(), "too many ids (max 50)");
  },
  async "/api/metar: upstream failures are passed on uncached"() {
    upstream(() => new Response("upstream timeout", { status: 504 }));
    let r = await metar(get("/api/metar?ids=KSFO,KOAK"));
    assert.equal(r.status, 504);
    assert.equal(r.headers.get("cache-control"), "no-store");
    upstream(new TypeError("fetch failed"));
    r = await metar(get("/api/metar?ids=KSFO,KOAK"));
    assert.equal(r.status, 502);
    assert.equal(r.headers.get("cache-control"), "no-store");
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
globalThis.fetch = realFetch;
console.log(failed ? "FAILED" : "passed");
process.exit(failed ? 1 : 0);
