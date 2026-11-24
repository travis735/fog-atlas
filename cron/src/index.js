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
// over HTTP. The only secret is GH_PAT, a fine-grained GitHub token limited to
// this repository with Actions read/write — set with `wrangler secret put`,
// never in this file or in wrangler.jsonc.

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
  return { status: res.status, detail };
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

export async function dispatch(env, cron) {
  if (!env.GH_PAT) {
    throw new Error("GH_PAT secret is not set — run `npx wrangler secret put GH_PAT` in cron/");
  }
  const job = JOBS[cron];
  if (!job) {
    // a trigger added to wrangler.jsonc without a JOBS entry must not silently do nothing
    throw new Error(`no workflow is mapped to the cron trigger "${cron}" — add it to JOBS in cron/src/index.js`);
  }
  let out = await postDispatch(env, job);
  if (out.status !== 204 && out.status !== 401 && (await enableIfDisabled(env, job))) {
    out = await postDispatch(env, job);
  }
  const log = { event: "dispatch", cron, workflow: job.workflow, repo: env.GH_REPO, status: out.status };
  if (out.status !== 204) {
    console.error(JSON.stringify({ ...log, ok: false, detail: out.detail }));
    // throwing marks the invocation as failed in the dashboard and in Workers Logs
    throw new Error(`GitHub refused the ${job.workflow} dispatch: HTTP ${out.status} ${out.detail}`);
  }
  console.log(JSON.stringify({ ...log, ok: true }));
  return out.status;
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatch(env, controller.cron));
  },
};
