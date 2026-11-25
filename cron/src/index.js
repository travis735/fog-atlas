// fogatlas-cron — a reliable clock for the repository's time-critical workflows.
//
// GitHub's own `schedule:` trigger is best-effort: in November 2026 the hourly
// cron in forecast.yml was starting 3 runs a day (median gap 7.8 h), so the
// "hourly" forecast was 8 hours old on average, the daily bake landed anywhere
// between 11Z and 18Z, and the twice-daily watchdog ran 5 to 8 hours late.
// Cloudflare Cron Triggers fire on time, so this Worker asks GitHub to start
// the workflows instead (workflow_dispatch). The GitHub crons stay in the
// workflow files as a fallback; duplicate runs are harmless (the bake is
// state-gated, verification pairs are keyed by NBM cycle, the watchdog only
// comments when its failing checks change).
//
// There is no fetch handler and workers_dev is off: nothing here is reachable
// over HTTP. GH_PAT is a fine-grained GitHub token limited to this repository
// with Actions read/write — set with `wrangler secret put`, never in this file
// or in wrangler.jsonc.
//
// Since 2026-11-25 the same clock can also reach the owner's phone (notify.js).
// clock.js, after every dispatch:
//   - a workflow start that GitHub refuses, once, and once when it works again
//   - the GitHub token's expiry, once a day in its last 14 days
//   - a heartbeat once a day, so that a silent phone means something
// fog.js, every 5 minutes:
//   - a field on a private board going below CAT I
// All of it is OFF until two more secrets exist (PUSHOVER_TOKEN and
// PUSHOVER_USER): without them every tick does exactly what it did before and
// logs one line saying that nothing is sent. README.md has the setup.

import { clockNotes, parseTokenExpiry, DAY_MS } from "./clock.js";
import { fogTick, FOG_CRON } from "./fog.js";

const API = "https://api.github.com";

// Which workflow each Cron Trigger starts. Keys must match wrangler.jsonc.
//  - forecast.yml gets force_bake=false: an hourly tick must follow the
//    workflow's once-a-day bake gate. A dispatch without that input means "a
//    person wants a redeploy" and runs the full page bake + site deploy.
//  - watchdog.yml runs at :50, 25 minutes after the forecast tick, so it sees
//    the cycle that tick just published.
const FORECAST = { workflow: "forecast.yml", inputs: { force_bake: "false" } };
export const JOBS = {
  "25 * * * *": FORECAST, // hourly forecast issuance
  "40 10 * * *": FORECAST, // a second chance for the daily bake (first run at/after 10Z bakes)
  "50 * * * *": { workflow: "watchdog.yml" }, // hourly live-site health check
};

function headers(env) {
  return {
    Authorization: `Bearer ${env.GH_PAT}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "fogatlas-cron (Cloudflare Worker; https://fogatlas.org)",
    "Content-Type": "application/json",
  };
}

async function postDispatch(env, job) {
  const body = { ref: env.GH_REF, ...(job.inputs ? { inputs: job.inputs } : {}) };
  const res = await fetch(`${API}/repos/${env.GH_REPO}/actions/workflows/${job.workflow}/dispatches`, {
    method: "POST",
    headers: headers(env),
    body: JSON.stringify(body),
  });
  // GitHub's error bodies are a few hundred bytes of JSON; cap the read anyway
  const detail = res.status === 204 ? "" : (await res.text()).slice(0, 500);
  // sent with every answer to a token that has an expiry date
  const expires = res.headers.get("github-authentication-token-expiration");
  return { status: res.status, detail, expires };
}

// GitHub disables every scheduled workflow in a public repository after 60
// days without repository activity, and a disabled workflow cannot be
// dispatched either. If that is why the dispatch failed, turn it back on.
async function enableIfDisabled(env, job) {
  const base = `${API}/repos/${env.GH_REPO}/actions/workflows/${job.workflow}`;
  const res = await fetch(base, { headers: headers(env) });
  if (!res.ok) {
    await res.body?.cancel();
    return false;
  }
  const { state } = await res.json();
  if (state === "active") return false;
  const put = await fetch(`${base}/enable`, { method: "PUT", headers: headers(env) });
  await put.body?.cancel();
  console.log(JSON.stringify({ event: "workflow_enabled", workflow: job.workflow, was: state, status: put.status }));
  return put.status === 204;
}

// One dispatch, with what the phone path needs to know about it:
// { status: 204, expires } or an Error carrying .status, .detail and .expires.
async function startWorkflow(env, cron) {
  if (!env.GH_PAT) {
    throw new Error("GH_PAT secret is not set — run `npx wrangler secret put GH_PAT` in cron/");
  }
  const job = JOBS[cron];
  if (!job) {
    // a trigger added to wrangler.jsonc without a JOBS entry must not silently do nothing
    throw new Error(`no workflow is mapped to the cron trigger "${cron}" — add it to JOBS in cron/src/index.js`);
  }
  let out = await postDispatch(env, job);
  let expiresRaw = out.expires;
  if (out.status !== 204 && out.status !== 401 && (await enableIfDisabled(env, job))) {
    out = await postDispatch(env, job);
    expiresRaw = out.expires ?? expiresRaw;
  }
  // The token's expiry was unknown on 2026-11-24, the day it began to drive
  // both the forecast and the watchdog. GitHub states it on every answer.
  const expires = parseTokenExpiry(expiresRaw);
  // a date this code could not read is shown as GitHub wrote it, never dropped
  const expiresUnread = expires == null && expiresRaw ? String(expiresRaw).slice(0, 60) : null;
  const log = { event: "dispatch", cron, workflow: job.workflow, repo: env.GH_REPO, status: out.status };
  if (expires != null) {
    log.token_expires = new Date(expires).toISOString();
    log.token_days_left = Math.round(((expires - Date.now()) / DAY_MS) * 10) / 10;
  } else if (expiresUnread) {
    log.token_expires_unread = expiresUnread;
  }
  if (out.status !== 204) {
    console.error(JSON.stringify({ ...log, ok: false, detail: out.detail }));
    // throwing marks the invocation as failed in the dashboard and in Workers Logs
    throw Object.assign(
      new Error(`GitHub refused the ${job.workflow} dispatch: HTTP ${out.status} ${out.detail}`),
      { status: out.status, detail: out.detail, expires, expiresUnread },
    );
  }
  console.log(JSON.stringify({ ...log, ok: true }));
  return { status: out.status, expires, expiresUnread };
}

export async function dispatch(env, cron) {
  return (await startWorkflow(env, cron)).status;
}

// One :25, :40 or :50 tick: the dispatch exactly as before, then whatever the
// phone should hear about it (clock.js).
async function clockTick(env, cron, when) {
  const run = {};
  let out = null, err = null;
  try {
    out = await startWorkflow(env, cron);
  } catch (e) {
    err = e;
  }
  try {
    await clockNotes(env, cron, JOBS[cron]?.workflow ?? `the trigger "${cron}"`, when, out, err, run);
  } catch (e) {
    // the phone path must never change what the dispatch did or how it is reported
    console.error(JSON.stringify({ event: "clock_notes_failed", message: String(e?.message ?? e).slice(0, 200) }));
  }
  if (err) throw err;
  return out.status;
}

export default {
  async scheduled(controller, env, ctx) {
    // Every Cron Trigger in wrangler.jsonc lands here: the fog check, or a
    // workflow start (an unmapped trigger fails loudly in startWorkflow).
    const work = controller.cron === FOG_CRON
      ? fogTick(env)
      : clockTick(env, controller.cron, controller.scheduledTime ?? Date.now());
    // The first waitUntil promise to reject is what the Cron Trigger's Past
    // Events table records as the invocation's status.
    ctx.waitUntil(work);
    // The handler waits for it as well. The runtime's documented promise is to
    // wait for the HANDLER (up to 15 minutes); work left to waitUntil alone is
    // documented as good for 30 seconds after an HTTP response, with nothing
    // said for Cron Triggers, and a fog check against a slow
    // aviationweather.gov can take longer than that.
    await work;
  },
};
