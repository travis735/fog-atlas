// What the phone hears about the clock itself: a workflow start that failed,
// the same start working again, the GitHub token running out, and one
// heartbeat a day.
//
// Why it exists (readiness review, 2026-11-24): since that day one GitHub
// token starts both the hourly forecast and the hourly watchdog. If it expires
// (the default lifetime of a new fine-grained token is 30 days) both stop
// together, and the thing that would have raised the alarm is one of the two.
// A refused start only threw into Workers Logs, kept for 3 days.
//
// index.js calls clockNotes() after every dispatch, whatever its outcome.
// Nothing here can change that outcome: index.js catches whatever this throws.
//
// alerts:clock in KV, written only when something changes (a failure, a
// recovery, one warning a day, one heartbeat a day):
// { v: 1,
//   open:   { "forecast.yml": { since: ms, kind, status, told } }  failing now
//   log:    [[ms, "forecast.yml", status], ...]   failed starts, last 24 h
//   warned: "2026-12-10"   UTC day of the last token-expiry warning
//   beat:   "2026-12-10"   UTC day of the last heartbeat
//   fogDead: ms            the fog check was found not running (its last check); 0 = it runs
//   refused: null | { since, last }   Pushover refused a message (notify.js) }
//
// Every message is "said once" through notify.js's sayOnce: the state that
// says it was told is written before the push. When the state cannot be read
// or written the clock is the only memory, and a message goes out on every
// sixth hour only (4 a day instead of 24).

import { notify, notifyOff, sayOnce, holdingOff } from "./notify.js";
import { fogStatus, zTime } from "./fog.js";

export const CLOCK_KEY = "alerts:clock";
// The heartbeat rides the :50 tick: the first one at or after this UTC hour
// on a day that has had none (HEARTBEAT_UTC_HOUR in wrangler.jsonc).
export const HEARTBEAT_CRON = "50 * * * *";
const DEFAULT_HEARTBEAT_UTC_HOUR = 14;
export const TOKEN_WARN_DAYS = 14;
export const DAY_MS = 86_400_000;
const MAX_LOG = 60; // 49 starts a day; a full day of failures still fits

/**
 * GitHub's token-expiration header as a time in ms, or null. Two spellings
 * have been seen in the wild, "2026-12-24 07:00:00 UTC" and
 * "2026-12-24 02:00:00 -0500"; neither parses with Date.parse as written.
 */
export function parseTokenExpiry(value) {
  if (!value) return null;
  const m = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(UTC|GMT|Z|[+-]\d{2}:?\d{2})?$/i.exec(String(value).trim());
  let ms;
  if (m) {
    const zone = !m[3] || /^[UGZ]/i.test(m[3]) ? "Z" : m[3].length === 5 ? `${m[3].slice(0, 3)}:${m[3].slice(3)}` : m[3];
    ms = Date.parse(`${m[1]}T${m[2]}${zone}`);
  } else {
    ms = Date.parse(value);
  }
  return Number.isNaN(ms) ? null : ms;
}

const freshClock = () => ({ v: 1, open: {}, log: [], warned: "", beat: "", fogDead: 0, refused: null });
function readClock(raw) {
  try {
    const s = JSON.parse(raw);
    if (s?.v === 1 && s.open && typeof s.open === "object" && Array.isArray(s.log)) return { ...freshClock(), ...s };
  } catch { /* unreadable: start over */ }
  return freshClock();
}

// "the same failure" is the same workflow failing in the same way: a GitHub
// outage that turns into an expired token is news, a 502 that turns into a
// 503 is not, and neither is a 502 that turns into no answer at all. Those
// two were separate kinds until 2026-11-25, and an outage that alternated
// between them told the phone every hour: 24 pushes for one incident.
function failKind(status) {
  if (status === 401 || status === 403) return "token";
  if (!status || status >= 500) return "github";
  return `http ${status}`;
}

const WHAT_STOPS = {
  "forecast.yml": "The hourly forecast is not being issued.",
  "watchdog.yml": "The hourly check of the live site is not running.",
};

function failureMessage(name, status, detail, when) {
  let said = "";
  try { said = String(JSON.parse(detail).message ?? "").slice(0, 120); } catch { /* not JSON */ }
  const kind = failKind(status);
  const first = status
    ? `GitHub refused to start ${name} at ${zTime(when)}: HTTP ${status}${said ? ` (${said})` : ""}.`
    : `${name} could not be started at ${zTime(when)}: ${String(detail).slice(0, 160)}`;
  const why = kind === "token"
    ? "The GitHub token has expired, was revoked or lost a permission. Make a new one and store it as GH_PAT (cron/README.md)."
    : kind === "github"
    ? "It is tried again every hour."
    : "It is tried again every hour, and will keep failing until the cause is fixed.";
  return [first, WHAT_STOPS[name] ?? "", why, "You get one more message when it starts again."].filter(Boolean).join(" ");
}

function expiryText(expires, when) {
  const left = expires - when;
  const days = Math.floor(left / DAY_MS);
  const on = `${new Date(expires).toISOString().slice(0, 10)} at ${zTime(expires)}`;
  if (left <= 0) return { short: "has expired", long: `expired on ${on}` };
  if (days < 1) return { short: "expires today", long: `expires today, ${on}` };
  return { short: `expires in ${days} ${days === 1 ? "day" : "days"}`, long: `expires in ${days} ${days === 1 ? "day" : "days"}, on ${on}` };
}

// The fog check's state in a few words, for the heartbeat's title
const FOG_TITLE = {
  off: "fog alerts off", bad: "fog alerts OFF", new: "fog alerts not checked yet", dead: "fog alerts NOT RUNNING",
  paused: "fog alerts paused", refused: "fog alerts refused by Pushover", unread: "fog alerts unknown", running: "fog alerts running",
};

/** The daily heartbeat as a message. fog = fogStatus()'s answer, or { status: "unread", text }. */
function heartbeatMessage(clock, when, expires, expiresUnread, fog) {
  const since = when - DAY_MS;
  const failed = clock.log.filter(([t]) => t > since);
  const lines = ["The clock is alive."];
  if (!failed.length) {
    lines.push("No workflow start failed in the last 24 hours.");
  } else {
    const per = {};
    for (const [, name] of failed) per[name] = (per[name] ?? 0) + 1;
    const [t, name, status] = failed[failed.length - 1];
    lines.push(`${failed.length} workflow ${failed.length === 1 ? "start" : "starts"} failed in the last 24 hours: `
      + `${Object.entries(per).map(([k, v]) => `${k} ${v}`).join(", ")}. `
      + `Latest: ${name} at ${zTime(t)}${status ? ` (HTTP ${status})` : ""}.`);
  }
  const open = Object.entries(clock.open);
  if (open.length) {
    lines.push(`STILL FAILING: ${open.map(([k, v]) => `${k} since ${zTime(v.since)} on ${new Date(v.since).toISOString().slice(0, 10)}`).join(", ")}.`);
  }
  lines.push(expires != null
    ? `The GitHub token ${expiryText(expires, when).long}.`
    : expiresUnread ? `The GitHub token's expiry date could not be read: GitHub wrote "${expiresUnread}".`
    // A refused start usually carries no date. An accepted one without a date
    // is a token that does not expire OR a header this code does not know:
    // the header on the real token had not been seen when this was written,
    // so the line says what was observed and not "never expires".
    : open.length ? "The GitHub token's expiry date is not known." : "GitHub sent no expiry date with this start, so no expiry warning can be given.");
  lines.push(fog.text);
  // the review's rule, where the pilot sees it every day
  if (fog.status !== "off") lines.push("A fog push is a prompt to look, not a decision. No push does not mean no fog.");
  // judged on the status, never on words in a sentence that carries the owner's own label
  const bad = failed.length || open.length || !["off", "running"].includes(fog.status);
  return {
    kind: "heartbeat",
    // quiet when all is well (no sound), a normal push when something is not
    priority: bad ? 0 : -1,
    // the fog state is in the title: "fog alerts off" was the fourth line of a silent message
    title: `Fog Atlas: clock alive, ${bad && ["off", "running"].includes(fog.status) ? "something needs a look, " : ""}${FOG_TITLE[fog.status] ?? "fog alerts unknown"}`,
    message: lines.join("\n"),
  };
}

async function putClock(kv, clock) {
  try {
    await kv.put(CLOCK_KEY, JSON.stringify(clock));
    return true;
  } catch (e) {
    console.error(JSON.stringify({ event: "clock_state_write_failed", message: String(e?.message ?? e).slice(0, 200) }));
    return false;
  }
}

/**
 * After one dispatch. `name` is the workflow (or a description of the trigger
 * when none is mapped), `out` the accepted dispatch or null, `err` the failure
 * or null; both may carry .expires (ms) and .expiresUnread.
 */
export async function clockNotes(env, cron, name, when, out, err, run) {
  // inert until the two secrets exist: no KV read, no request to anyone
  if (notifyOff(env, run)) return;
  const kv = env.FOGCAST;
  let clock = freshClock(), stateRead = false;
  if (kv) {
    try {
      clock = readClock(await kv.get(CLOCK_KEY));
      stateRead = true;
    } catch { /* KV unreadable: see below */ }
  }
  if (!stateRead) {
    console.error(JSON.stringify({ event: "clock_state_unread", reason: kv ? "KV read failed" : "the FOGCAST KV binding is missing" }));
  }
  const day = new Date(when).toISOString().slice(0, 10);
  const hourNow = new Date(when).getUTCHours();
  // With no memory (the state cannot be read, or will not be written) "once"
  // falls back to the clock: every sixth hour. It was "say it again rather
  // than say nothing", which was every hour: 24 pushes a day for a dead token.
  const slot = hourNow % 6 === 0;
  const holding = holdingOff(clock, when);
  const messages = [];
  let dirty = false;

  if (err) {
    const status = err.status ?? 0;
    const kind = failKind(status);
    clock.log = [...clock.log.filter(([t]) => t > when - DAY_MS), [when, name, status]].slice(-MAX_LOG);
    dirty = true;
    const was = clock.open[name];
    // Once per failure, and again only when it becomes another kind of failure.
    const news = !was || !was.told || was.kind !== kind;
    const entry = { since: was?.since ?? when, kind, status, told: !news };
    clock.open[name] = entry;
    if (news && !holding && (stateRead || slot)) {
      entry.told = true;
      messages.push({
        msg: {
          kind: "dispatch_failed", priority: 1,
          title: `Fog Atlas clock: ${name} did not start`,
          message: failureMessage(name, status, err.detail ?? err.message, when),
        },
        undo: () => { entry.told = false; },
      });
    }
  } else if (clock.open[name]) {
    const was = clock.open[name];
    if (!(was.told && holding)) {       // a recovery that is owed while Pushover refuses stays on record until it can be said
      delete clock.open[name];
      dirty = true;
      if (was.told) {
        messages.push({
          msg: {
            kind: "dispatch_recovered", priority: 0,
            title: `Fog Atlas clock: ${name} starts again`,
            message: `GitHub accepted the start of ${name} at ${zTime(when)}. It had failed since ${zTime(was.since)} on ${new Date(was.since).toISOString().slice(0, 10)}.`,
          },
          undo: () => { clock.open[name] = was; },   // not delivered: the next good tick says it
        });
      }
    }
  }

  const expires = out?.expires ?? err?.expires ?? null;
  if (expires != null && expires - when < TOKEN_WARN_DAYS * DAY_MS && clock.warned !== day && stateRead && !holding) {
    const text = expiryText(expires, when);
    const before = clock.warned;
    clock.warned = day;
    dirty = true;
    messages.push({
      msg: {
        kind: "token_expiry", priority: 0,
        title: `Fog Atlas clock: the GitHub token ${text.short}`,
        message: `The token that starts the forecast and the site check ${text.long}. After that nothing starts on time. `
          + "Make a new token and store it as GH_PAT (cron/README.md). This warning repeats once a day.",
      },
      undo: () => { clock.warned = before; },
    });
  }

  if (cron === HEARTBEAT_CRON && !stateRead && slot && kv) {
    // KV cannot be read: no heartbeat, no token warning, and the fog check
    // (which reads the same store) is throwing on every tick. That was
    // twelve hours of nothing in a review's run. Said on every sixth hour.
    messages.push({
      msg: {
        kind: "state_unread", priority: 0,
        title: "Fog Atlas: the alert state cannot be read",
        message: `The Worker could not read its state from Cloudflare KV at ${zTime(when)}. Fog alerts, the heartbeat and the token warning `
          + "all depend on it and are not running. It is tried again on every tick; this message repeats every 6 hours while it lasts.",
      },
      undo: () => {},
    });
  }

  let fog = null;
  if (cron === HEARTBEAT_CRON && stateRead) {
    try {
      fog = await fogStatus(kv, when);
    } catch {
      fog = { status: "unread", text: "Fog alerts: their state could not be read." };
    }
    // The 5-minute fog check, looked at once an hour. A check that had
    // stopped was only reported by the next day's heartbeat: up to 24 hours
    // with a board stored and nothing checking it.
    if (fog.status === "dead" && !clock.fogDead && !holding) {
      clock.fogDead = fog.since || when;
      dirty = true;
      messages.push({
        msg: {
          kind: "fog_dead", priority: 1,
          title: "Fog Atlas: fog alerts are NOT RUNNING",
          message: `${fog.text} No fog alert can fire until the 5-minute check runs again. Look at the board yourself. `
            + "You get one more message when it does.",
        },
        undo: () => { clock.fogDead = 0; },
      });
    } else if (clock.fogDead && !["dead", "unread"].includes(fog.status) && !holding) {
      const since = clock.fogDead;
      clock.fogDead = 0;
      dirty = true;
      if (!["off", "bad"].includes(fog.status)) {      // the board was removed meanwhile: nothing is owed
        messages.push({
          msg: {
            kind: "fog_alive", priority: 0,
            title: "Fog Atlas: fog alerts run again",
            message: `The 5-minute fog check is running again. It had not run since ${zTime(since)} on ${new Date(since).toISOString().slice(0, 10)}. `
              + "After a gap of more than 6 hours its first check alerts for nothing.",
          },
          undo: () => { clock.fogDead = since; },
        });
      }
    }
  }

  const set = Number(env.HEARTBEAT_UTC_HOUR ?? DEFAULT_HEARTBEAT_UTC_HOUR);
  const hour = Number.isInteger(set) && set >= 0 && set <= 23 ? set : DEFAULT_HEARTBEAT_UTC_HOUR;
  if (cron === HEARTBEAT_CRON && stateRead && clock.beat !== day && hourNow >= hour && !holding) {
    const before = clock.beat;
    clock.beat = day;
    dirty = true;
    messages.push({
      msg: heartbeatMessage(clock, when, expires, out?.expiresUnread ?? err?.expiresUnread ?? null, fog),
      undo: () => { clock.beat = before; },
    });
  }

  const o = await sayOnce(env, messages, {
    save: () => (stateRead ? putClock(kv, clock) : Promise.resolve(false)),
    holder: clock, now: when, run, slot, mustSave: dirty && stateRead,
  });
  // index.js logs what is thrown here and goes on: the dispatch's own outcome is never changed by it
  if (stateRead && !o.saved) throw new Error("the clock's alert state could not be saved to KV (see clock_state_write_failed)");
  if (o.refused) throw new Error("Pushover refused a message (HTTP 4xx): it is not counted as sent and is tried again in an hour");
}
