// Offline test for the cron Worker's dispatch logic. No dependencies:
//   node cron/test_dispatch.mjs
// fetch is replaced with a scripted fake, so nothing touches GitHub.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import worker, { dispatch, JOBS } from "./src/index.js";

const ENV = { GH_PAT: "test-token-not-real", GH_REPO: "travis735/fog-atlas", GH_REF: "main" };
const HOURLY = "25 * * * *";
const WATCHDOG = "50 * * * *";
const realFetch = globalThis.fetch;
const quiet = { log: console.log, error: console.error };

function script(responses) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method ?? "GET", headers: init.headers ?? {}, body: init.body });
    const next = responses.shift();
    assert.ok(next, `unexpected extra request: ${init.method ?? "GET"} ${url}`);
    return new Response(next.body ?? null, { status: next.status });
  };
  return calls;
}

const tests = {
  async "the hourly tick dispatches forecast.yml with force_bake false"() {
    const calls = script([{ status: 204 }]);
    assert.equal(await dispatch(ENV, HOURLY), 204);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].url, "https://api.github.com/repos/travis735/fog-atlas/actions/workflows/forecast.yml/dispatches");
    assert.equal(calls[0].headers.Authorization, "Bearer test-token-not-real");
    assert.ok(calls[0].headers["User-Agent"], "GitHub rejects requests without a User-Agent");
    // force_bake must be the STRING "false": the hourly tick follows the daily bake gate
    assert.deepEqual(JSON.parse(calls[0].body), { ref: "main", inputs: { force_bake: "false" } });
  },
  async "the 10:40Z tick is the same forecast dispatch"() {
    const calls = script([{ status: 204 }]);
    await dispatch(ENV, "40 10 * * *");
    assert.ok(calls[0].url.endsWith("/workflows/forecast.yml/dispatches"));
    assert.deepEqual(JSON.parse(calls[0].body).inputs, { force_bake: "false" });
  },
  async "the :50 tick dispatches watchdog.yml with no inputs"() {
    const calls = script([{ status: 204 }]);
    assert.equal(await dispatch(ENV, WATCHDOG), 204);
    assert.ok(calls[0].url.endsWith("/workflows/watchdog.yml/dispatches"));
    // watchdog.yml declares no inputs; GitHub answers 422 to unexpected ones
    assert.deepEqual(JSON.parse(calls[0].body), { ref: "main" });
  },
  async "every cron trigger in wrangler.jsonc has a job, and nothing else does"() {
    const cfg = readFileSync(new URL("./wrangler.jsonc", import.meta.url), "utf8");
    const block = cfg.slice(cfg.indexOf('"crons"'), cfg.indexOf("]", cfg.indexOf('"crons"')));
    const crons = [...block.matchAll(/^\s*"([^"]+)"/gm)].map((m) => m[1]).filter((c) => c !== "crons");
    assert.deepEqual([...crons].sort(), Object.keys(JOBS).sort());
  },
  async "an unmapped cron trigger fails loudly and sends nothing"() {
    const calls = script([]);
    await assert.rejects(dispatch(ENV, "0 0 * * *"), /no workflow is mapped/);
    assert.equal(calls.length, 0);
  },
  async "a missing secret fails before any request"() {
    const calls = script([]);
    await assert.rejects(dispatch({ ...ENV, GH_PAT: undefined }, HOURLY), /GH_PAT secret is not set/);
    assert.equal(calls.length, 0);
  },
  async "a bad token (401) fails loudly and does not probe further"() {
    const calls = script([{ status: 401, body: '{"message":"Bad credentials"}' }]);
    await assert.rejects(dispatch(ENV, HOURLY), /HTTP 401.*Bad credentials/);
    assert.equal(calls.length, 1);
  },
  async "a workflow disabled for inactivity is re-enabled and dispatched"() {
    const calls = script([
      { status: 422, body: '{"message":"Workflow is disabled"}' },
      { status: 200, body: '{"state":"disabled_inactivity"}' },
      { status: 204 },
      { status: 204 },
    ]);
    assert.equal(await dispatch(ENV, WATCHDOG), 204);
    assert.deepEqual(calls.map((c) => `${c.method} ${c.url.split("/workflows/")[1]}`),
      ["POST watchdog.yml/dispatches", "GET watchdog.yml", "PUT watchdog.yml/enable", "POST watchdog.yml/dispatches"]);
  },
  async "another failure on an active workflow is reported, not retried"() {
    const calls = script([{ status: 404, body: '{"message":"Not Found"}' }, { status: 200, body: '{"state":"active"}' }]);
    await assert.rejects(dispatch(ENV, HOURLY), /forecast\.yml dispatch: HTTP 404/);
    assert.equal(calls.length, 2);
  },
  async "the scheduled handler hands the dispatch to waitUntil"() {
    script([{ status: 204 }]);
    let waited;
    worker.scheduled({ cron: HOURLY }, ENV, { waitUntil: (p) => { waited = p; } });
    assert.ok(waited instanceof Promise);
    assert.equal(await waited, 204);
    assert.equal(worker.fetch, undefined, "the Worker must not expose an HTTP handler");
  },
};

let failed = 0;
console.log = console.error = () => {};
for (const [name, fn] of Object.entries(tests)) {
  try {
    await fn();
    quiet.log("ok  ", name);
  } catch (e) {
    failed++;
    quiet.log("FAIL", name, "-", e.message);
  }
}
globalThis.fetch = realFetch;
Object.assign(console, quiet);
console.log(failed ? "FAILED" : "passed");
process.exit(failed ? 1 : 0);
