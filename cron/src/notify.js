// The one path from this Worker to the owner's phone.
//
// Why it exists (readiness review, 2026-11-24): a refused dispatch only wrote
// a Workers log line (kept 3 days), the watchdog only opened a GitHub issue
// (which GitHub Mobile does not push), and the in-tab fog alert needs the tab
// open. Nothing reached a phone.
//
// Everything that wants to reach the phone calls notify(). Today notify()
// hands the message to Pushover; a native app's push can replace sendPushover()
// without touching a caller. The message is channel-neutral:
//   { kind, title, message, priority?, url?, urlTitle? }
// kind is a short name for the log line ("fog", "heartbeat", ...): the log
// never carries the title or the text, because a fog alert names the fields on
// the owner's private board.
//
// Two secrets switch it on, set with `wrangler secret put` and never written
// to a file: PUSHOVER_TOKEN (the Pushover application's API token) and
// PUSHOVER_USER (the user key). Until both exist notify() sends nothing and
// says so once per invocation.

const PUSHOVER = "https://api.pushover.net/1/messages.json";
// Pushover's documented limits (pushover.net/api, fetched 2026-11-24)
const MAX_TITLE = 250;
const MAX_MESSAGE = 1024;
const MAX_URL = 512;
const MAX_URL_TITLE = 100;
// priority 2 repeats until acknowledged and must say how often and for how long
const EMERGENCY_RETRY_S = 60; // Pushover's minimum is 30
const EMERGENCY_EXPIRE_S = 1800; // its maximum is 10800
const TIMEOUT_MS = 10_000;

export const pushConfigured = (env) => Boolean(env.PUSHOVER_TOKEN && env.PUSHOVER_USER);

const cut = (s, n) => {
  const t = String(s ?? "");
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
};

/** One line, once per invocation, when the secrets are missing. `run` is the invocation's own scratch object. */
export function notifyOff(env, run) {
  if (pushConfigured(env)) return false;
  if (run && !run.offLogged) {
    run.offLogged = true;
    console.log(JSON.stringify({
      event: "notify_off",
      reason: "PUSHOVER_TOKEN and PUSHOVER_USER are not both set: nothing is sent to the phone",
    }));
  }
  return true;
}

/**
 * Send one message to the phone. Never throws.
 * Returns { sent, retry, refused? }:
 *   sent    - Pushover accepted it
 *   retry   - it was NOT sent and the same message may work later (Pushover
 *             5xx, no connection, timeout)
 *   refused - it was NOT sent and Pushover said why (a 4xx): its API page says
 *             a refused request will be refused again and that repeating it
 *             gets the sender's address blocked. Not sent is not told: callers
 *             go through sayOnce(), which tries again an hour later.
 * There is no sleep-and-retry here.
 */
export async function notify(env, msg, run) {
  if (notifyOff(env, run)) return { sent: false, retry: false, off: true };
  try {
    return await sendPushover(env, msg);
  } catch (e) {
    // the alert path must never take the dispatch or the fog check down with it
    const name = e?.name === "TimeoutError" || e?.name === "AbortError" ? "timeout" : "network";
    console.error(JSON.stringify({ event: "notify", kind: msg.kind, ok: false, error: name }));
    return { sent: false, retry: true };
  }
}

async function sendPushover(env, msg) {
  const priority = [-2, -1, 0, 1, 2].includes(msg.priority) ? msg.priority : 0;
  const body = {
    token: env.PUSHOVER_TOKEN,
    user: env.PUSHOVER_USER,
    title: cut(msg.title, MAX_TITLE),
    message: cut(msg.message, MAX_MESSAGE),
    priority,
  };
  if (priority === 2) {
    body.retry = EMERGENCY_RETRY_S;
    body.expire = EMERGENCY_EXPIRE_S;
  }
  if (msg.url && msg.url.length <= MAX_URL) {
    body.url = msg.url;
    if (msg.urlTitle) body.url_title = cut(msg.urlTitle, MAX_URL_TITLE);
  }
  const res = await fetch(PUSHOVER, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": "fogatlas-cron (Cloudflare Worker; https://fogatlas.org)" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  // a few hundred bytes of JSON; cap the read anyway
  const text = (await res.text()).slice(0, 2000);
  let answer = null;
  try { answer = JSON.parse(text); } catch { /* an error page: judged by the status alone */ }
  const sent = res.status === 200 && answer?.status === 1;
  const log = { event: "notify", kind: msg.kind, ok: sent, status: res.status };
  if (sent) {
    console.log(JSON.stringify(log));
    return { sent: true, retry: false };
  }
  // Pushover's own words for what was wrong ("user identifier is invalid").
  // They do not echo the keys today; scrub them anyway before logging.
  const errors = Array.isArray(answer?.errors)
    ? answer.errors.join("; ").split(env.PUSHOVER_TOKEN).join("[token]").split(env.PUSHOVER_USER).join("[user]").slice(0, 300)
    : undefined;
  const retry = res.status >= 500 || res.status === 408;
  console.error(JSON.stringify({ ...log, errors, will_retry: retry }));
  return retry ? { sent: false, retry } : { sent: false, retry: false, refused: true };
}

// ---- "said once" ----
// Every message this Worker sends is meant to be said once: a field below
// CAT I, alerts paused, a workflow that did not start, the day's heartbeat.
// "Once" needs somewhere to remember that it was said, and until 2026-11-25
// the order was push first, remember second. With KV refusing writes (its
// free daily quota is the whole account's and stays spent until 00:00 UTC) a
// review counted 347 pushes in a day: one field in fog every 5 minutes, both
// failed workflow starts every hour, ten heartbeats. The order is now:
//   1. the caller marks everything as told and its state is written;
//   2. then the pushes, in order;
//   3. a push that did not go out takes its "told" back (undo) and the state
//      is written again, never inside the same second (KV allows one write a
//      second to a key).
// When the first write fails the only memory left is the clock: a message
// then goes out only when `slot` says this tick may speak (fog: the first
// tick of a half hour; the hourly ticks: every sixth hour), says that it may
// repeat, and the caller fails the invocation so the dashboard shows it. Late
// and repeated at that pace is better than silent, and better than every tick.
//
// A message Pushover REFUSES is not counted as told either (it was: a revoked
// key or a spent monthly quota swallowed fog alerts and the heartbeat with
// nothing ever said). holder.refused = { since, last } records it; callers
// hold their messages back for REFUSED_RETRY_MS, and the first one Pushover
// accepts again says what was missed.
export const REFUSED_RETRY_MS = 60 * 60_000;
export const UNSAVED_NOTE = "(The alert state could not be saved, so this message may repeat.)";
// An object so the offline test can set the wait to 0.
export const KV_WRITE_GAP = { ms: 1100 };
const zHM = (ms) => {
  const d = new Date(ms);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}Z on ${d.toISOString().slice(0, 10)}`;
};

/** Is Pushover being left alone on this tick? (It refused a message under an hour ago.) */
export const holdingOff = (holder, now) => Boolean(holder?.refused) && now - holder.refused.last < REFUSED_RETRY_MS;

/**
 * messages: [{ msg, undo }], already marked as told in the caller's state;
 * undo() takes that mark back. save(): writes the state, resolves true or
 * false, never throws. holder: the state object that carries `refused`.
 * Returns { sent: [bool per message], saved, refused }.
 */
export async function sayOnce(env, messages, { save, holder, now, run, slot, mustSave = true }) {
  const out = { sent: messages.map(() => false), saved: true, refused: false };
  if (!messages.length && !mustSave) return out;
  const wroteAt = Date.now();
  out.saved = await save();
  let fix = false;
  for (let i = 0; i < messages.length; i++) {
    let { msg } = messages[i];
    // refused a moment ago in this same tick, or no memory and not a tick that may speak: not sent
    if (out.refused || (!out.saved && !slot)) {
      messages[i].undo();
      fix = true;
      continue;
    }
    if (!out.saved) msg = { ...msg, message: `${msg.message}\n${UNSAVED_NOTE}` };
    if (holder.refused) {
      msg = { ...msg, message: `Pushover refused messages from ${zHM(holder.refused.since)} until now. `
        + `What was sent in that time did not reach this phone.\n${msg.message}` };
    }
    const r = await notify(env, msg, run);
    if (r.sent) {
      out.sent[i] = true;
      if (holder.refused) { holder.refused = null; fix = true; }
      continue;
    }
    messages[i].undo();
    fix = true;
    if (r.refused) {
      out.refused = true;
      holder.refused = { since: holder.refused?.since ?? now, last: now };
    }
  }
  if (fix && out.saved) {
    const wait = KV_WRITE_GAP.ms - (Date.now() - wroteAt);
    if (wait > 0) await new Promise((done) => setTimeout(done, wait));
    out.saved = await save();
  }
  return out;
}
