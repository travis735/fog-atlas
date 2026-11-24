// fogatlas-cron — a reliable clock for the forecast workflow.
//
// GitHub's own `schedule:` trigger is best-effort: in November 2026 the hourly
// cron in forecast.yml was starting 3 runs a day (median gap 7.8 h), so the
// "hourly" forecast was 8 hours old on average and the daily bake landed
// anywhere between 11Z and 18Z. Cloudflare Cron Triggers fire on time, so this
// Worker asks GitHub to start the workflow instead (workflow_dispatch), every
// hour at :25 and once more at 10:40Z as a retry for the daily bake. The
// GitHub cron stays in forecast.yml as a fallback; duplicate runs on one NBM
// cycle are harmless (the bake is state-gated, verification pairs are keyed
// by cycle).
//
// There is no fetch handler and workers_dev is off: nothing here is reachable
// over HTTP. The only secret is GH_PAT, a fine-grained GitHub token limited to
// this repository with Actions read/write — set with `wrangler secret put`,
// never in this file or in wrangler.jsonc.

const API = "https://api.github.com";

function headers(env) {
  return {
    Authorization: `Bearer ${env.GH_PAT}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "fogatlas-cron (Cloudflare Worker; https://fogatlas.org)",
    "Content-Type": "application/json",
  };
}

async function postDispatch(env) {
  const res = await fetch(`${API}/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`, {
    method: "POST",
    headers: headers(env),
    body: JSON.stringify({ ref: env.GH_REF }),
  });
  // GitHub's error bodies are a few hundred bytes of JSON; cap the read anyway
  const detail = res.status === 204 ? "" : (await res.text()).slice(0, 500);
  return { status: res.status, detail };
}

// GitHub disables every scheduled workflow in a public repository after 60
// days without repository activity, and a disabled workflow cannot be
// dispatched either. If that is why the dispatch failed, turn it back on.
async function enableIfDisabled(env) {
  const base = `${API}/repos/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}`;
  const res = await fetch(base, { headers: headers(env) });
  if (!res.ok) {
    await res.body?.cancel();
    return false;
  }
  const { state } = await res.json();
  if (state === "active") return false;
  const put = await fetch(`${base}/enable`, { method: "PUT", headers: headers(env) });
  await put.body?.cancel();
  console.log(JSON.stringify({ event: "workflow_enabled", was: state, status: put.status }));
  return put.status === 204;
}

export async function dispatch(env, cron) {
  if (!env.GH_PAT) {
    throw new Error("GH_PAT secret is not set — run `npx wrangler secret put GH_PAT` in cron/");
  }
  let out = await postDispatch(env);
  if (out.status !== 204 && out.status !== 401 && (await enableIfDisabled(env))) {
    out = await postDispatch(env);
  }
  const log = { event: "dispatch", cron, workflow: env.GH_WORKFLOW, repo: env.GH_REPO, status: out.status };
  if (out.status !== 204) {
    console.error(JSON.stringify({ ...log, ok: false, detail: out.detail }));
    // throwing marks the invocation as failed in the dashboard and in Workers Logs
    throw new Error(`GitHub refused the dispatch: HTTP ${out.status} ${out.detail}`);
  }
  console.log(JSON.stringify({ ...log, ok: true }));
  return out.status;
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(dispatch(env, controller.cron));
  },
};
