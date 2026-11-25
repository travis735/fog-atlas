# fogatlas-cron

A Cloudflare Worker with Cron Triggers and nothing else: no HTTP handler, no
`workers.dev` hostname, no route.

| Trigger (UTC) | What it does |
| --- | --- |
| `25 * * * *` | starts `forecast.yml` (hourly forecast) |
| `40 10 * * *` | starts `forecast.yml` again (second chance for the daily bake) |
| `50 * * * *` | starts `watchdog.yml`; the first one at or after 14Z also sends the daily heartbeat |
| `*/5 * * * *` | checks a private board for fog and pushes to the phone |

`src/index.js` says why the clock exists. This file is about the phone.

## Nothing is sent until you switch it on

The phone path needs two Worker secrets. Without them every tick does what it
did before and logs one line (`notify_off`). The fog check also needs a board in
KV; without one it reads KV once, logs one line (`fog_alerts_off`) and stops.

| Name | Where | What |
| --- | --- | --- |
| `PUSHOVER_TOKEN` | Worker secret | the API token of a Pushover application you create |
| `PUSHOVER_USER` | Worker secret | your Pushover user key |
| `GH_PAT` | Worker secret | already set: the GitHub token that starts the workflows |
| `alerts:board` | KV, you write it | the fields to watch |
| `alerts:test` | KV, you write it | asks for one test message; the Worker deletes it |
| `alerts:state` | KV, the Worker writes it | what each field last said |
| `alerts:clock` | KV, the Worker writes it | failed starts, last warning, last heartbeat |

The KV namespace is the site's forecast store (`FOGCAST`,
`72edf0ce3b1944bca59e4aa47cb67cc4`). The Pages Functions read three fixed keys
from it (`current`, `deploy`, `scorecard`) and cannot serve an `alerts:` key;
`test_alerts.mjs` checks that.

## Setup, in order

All commands run from `cron/`.

1. Deploy the code. With no secrets it changes nothing except the log line.

   ```sh
   npx wrangler@4.167.0 deploy
   ```

   A new Cron Trigger can take up to 15 minutes to start firing.

2. In Pushover: create an account, install the app on the phone, create an
   application (its API token is `PUSHOVER_TOKEN`) and copy your user key
   (`PUSHOVER_USER`). Store both. Wrangler asks for the value; never put it on
   the command line or in a file here.

   **Buy the phone's licence when you create the account, and note the date.**
   Pushover is free for 30 days and then a one-time purchase per platform
   (pushover.net/pricing, read 2026-11-25: USD 4.99). An account opened for a
   rehearsal in early December reaches day 30 in the middle of the season. What
   the API answers after that was not checked. If it refuses, the Worker tries
   again every hour and the first message accepted says what was missed; if the
   phone simply stops showing messages, the only sign is a missing heartbeat.

   ```sh
   npx wrangler@4.167.0 secret put PUSHOVER_TOKEN
   npx wrangler@4.167.0 secret put PUSHOVER_USER
   ```

3. Ask for one test message. It arrives on the next 5-minute tick.

   ```sh
   npx wrangler@4.167.0 kv key put alerts:test 1 --namespace-id 72edf0ce3b1944bca59e4aa47cb67cc4 --remote
   ```

   The phone shows "Fog Atlas: test message". If it does not, watch the log
   while you ask again:

   ```sh
   npx wrangler@4.167.0 tail fogatlas-cron --format pretty
   ```

   `notify_off` means a secret is missing. `"event":"notify","ok":false` carries
   Pushover's own reason.

   Send one more with the phone in the state it is in at night (Focus, Do Not
   Disturb, silent switch). A fog alert is sent at Pushover priority 1, which
   passes Pushover's own quiet hours; whether the phone lets it sound is the
   phone's setting, and only a test shows it.

From here on the clock messages are live: a refused workflow start, the token
warning, and the daily heartbeat (the first :50 tick at or after 14Z; change
`HEARTBEAT_UTC_HOUR` in `wrangler.jsonc` and deploy).

## Renewing the GitHub token

The phone warns once a day when the token has under 14 days left, and at once
when GitHub refuses it (HTTP 401). In GitHub: Settings, Developer settings,
Fine-grained tokens. Regenerate the token or make a new one for this repository
only, with the Actions permission set to read and write. Then:

```sh
npx wrangler@4.167.0 secret put GH_PAT
```

The next tick uses it. Every dispatch logs the expiry date GitHub reports
(`token_expires`, `token_days_left`).

## The board

The board is your base and the fields you are watching. This repository is
public: the board lives in KV and nowhere else. `make_board.mjs` writes it to
stdout only; send it to a file outside the repository and delete the file.

```sh
node make_board.mjs --base KXXX > "$TMPDIR/fogatlas-board.json"
npx wrangler@4.167.0 kv key put alerts:board --path "$TMPDIR/fogatlas-board.json" --namespace-id 72edf0ce3b1944bca59e4aa47cb67cc4 --remote
rm "$TMPDIR/fogatlas-board.json"
```

`make_board.mjs` lists the fields the CHASE panel lists for the same base and
filters, nearest first, and prints a one-line summary. Defaults are the
panel's: 250 kt, 2 hours (500 nm), ALSF-2 / ALSF-1 / MALSR, 5,000 ft, tier 250.
Options are at the top of the file, for example:

```sh
node make_board.mjs --base KXXX --radius-nm 300 --als ALSF2,ALSF1 --rvr --max 100 > "$TMPDIR/fogatlas-board.json"
```

It keeps the nearest 120 fields unless told otherwise (`--max`, at most 160,
the Worker's limit) and warns when it leaves any out.

A put can fail without saying so. Read it back (this prints the board in your
terminal):

```sh
npx wrangler@4.167.0 kv key get alerts:board --text --namespace-id 72edf0ce3b1944bca59e4aa47cb67cc4 --remote
```

If the CHASE panel is on "avoid LED ends" or "only LED ends", pass the same
choice (`--led avoid`, `--led only`): the panel keeps it in the browser, and the
board is made here.

Store a new board after every change of base, filters or LED choice. The
heartbeat states the board's age. A board has no label unless you give it one
(`--label "north board"`): whatever the label says goes to Pushover every day,
so leave the base out of it. Fog alerts name the fields that went into fog and
the flight time to each, which also passes through Pushover.

Remove the board, which stops the fog check, and the state kept for it:

```sh
npx wrangler@4.167.0 kv key delete alerts:board --namespace-id 72edf0ce3b1944bca59e4aa47cb67cc4 --remote
npx wrangler@4.167.0 kv key delete alerts:state --namespace-id 72edf0ce3b1944bca59e4aa47cb67cc4 --remote
```

Switch the whole phone path off:

```sh
npx wrangler@4.167.0 secret delete PUSHOVER_TOKEN
```

The board can also be written by hand. Only `ids` is required:

```json
{ "v": 1, "made": "2026-12-08T03:00:00Z", "label": "KXXX, 500 nm",
  "priority": 1, "repeatMin": 60,
  "ids": ["KAAA", "KBBB"], "eteMin": { "KAAA": 12, "KBBB": 31 } }
```

`priority` is Pushover's: 1 passes its quiet hours, 2 repeats every minute for
30 minutes until acknowledged, 0 is a normal push. `repeatMin` is the floor
between two alerts for the same field.

## What the phone gets

| Title | When | Priority |
| --- | --- | --- |
| `KAAA below CAT I` or `3 fields below CAT I` | a field went from clear to below CAT I | the board's (1) |
| `Fog Atlas: fog alerts PAUSED` | reports unreadable for 15 minutes; sent once | 1 |
| `Fog Atlas: fog alerts are back` | reports readable again; sent once | 0 |
| `Fog Atlas: fog alerts are OFF` | the stored board cannot be used; sent once | 0 |
| `Fog Atlas clock: forecast.yml did not start` | GitHub refused a start; once per failure | 1 |
| `Fog Atlas clock: forecast.yml starts again` | the same start works again | 0 |
| `Fog Atlas clock: the GitHub token expires in 9 days` | once a day in the last 14 days | 0 |
| `Fog Atlas: clock alive` | once a day | -1 (no sound) |
| `Fog Atlas: clock alive, something needs a look` | once a day, when something is wrong | 0 |
| `Fog Atlas: test message` | you asked for it | 0 |

A fog line reads `KAAA 03:15Z: 0.25 SM, ceiling 100 ft, R29R/1200 FT, FG. ETE 0:32.`
The time is the observation's.

## The fog rule

Below CAT I is the board's rule, imported from `app/src/feed.ts`: visibility
under 1/2 statute mile, or a BKN, OVC or OVX (vertical visibility) base under
200 ft. A report over 75 minutes old is neither fog nor clear.

- The first check after a board is stored pushes nothing: fields already in fog
  are the baseline. That holds for every way of storing one: a first board, a
  new board in place of another (other fields, or made at another time), the
  same board put back after it was deleted, and the secrets set again after
  days without them (a check that has not run for over 6 hours starts over). A
  lost state is a first check. A gap of a few hours is not: fields that went
  into fog in it alert when the check runs again.
- A field alerts when it goes from clear, or from never having said, to below
  CAT I. Not again until it has been clear and at least `repeatMin` minutes have
  passed since its last alert.
- A report that does not say (too old, or no visibility and no low ceiling)
  changes nothing.
- When reports cannot be read, or too few fields have a current one, no fog
  check runs. After 15 minutes the phone is told once ("PAUSED"), and once more
  when reports have been readable for two checks in a row ("back"). A feed
  that keeps coming and going is not announced again for 3 hours. A field that
  went into fog meanwhile alerts as soon as reports are back.
- Every message is recorded before it is sent. If Cloudflare KV will not take
  the write, a fog alert is still sent, on the first check of each half hour,
  and says that it may repeat. If Pushover refuses a message (a wrong key, an
  expired licence, the monthly quota) it is not counted as sent: it is tried
  again an hour later, and the first message accepted says since when
  messages were refused. Both show as failed invocations in the dashboard.
- Once an hour, on the :50 tick, the Worker looks at whether the 5-minute check
  is still running. If no check is on record for 90 minutes the phone is told
  once ("fog alerts are NOT RUNNING"), and once when it runs again.

A push is a prompt to look, not a decision. No push does not mean no fog: the
heartbeat says whether the check is running, paused or stopped.

## Tests

```sh
node test_dispatch.mjs
node test_alerts.mjs
npx wrangler@4.167.0 deploy --dry-run
```

`test_alerts.mjs` runs the Worker through `scheduled()` with a scripted
network and a Map for KV, holds the fog rule to the CHASE board's own reader on
real reports (`fixtures/`), and measures the CPU of a fog tick.

## Limits this was built inside (Workers Free, KV Free; docs read 2026-11-24)

| Limit | Allowed | Used |
| --- | --- | --- |
| CPU per Cron Trigger invocation | 10 ms | Measured on a desktop core only (Apple M-series, Node): 0.2 ms a fog tick for 120 fields once warm; 1.2 to 2.2 ms for the first tick of a new process, which at one tick every 5 minutes is probably most ticks; 2.4 to 3.8 ms for the worst first tick (160 fields into fog at once plus a test message). Not measured on Cloudflare: read `cpuTime` in Workers Logs after the first day. |
| Subrequests per invocation | 50 | 3 to aviationweather.gov for 120 fields (4 for 160), plus at most 3 pushes (a test message, "back" and a fog alert in one tick) |
| Cron Triggers per account | 5 | 4 |
| KV reads per day | 100,000 | under 1,000 |
| KV writes per day | 1,000 | about 30 on a quiet day; one per tick with something to record (288 at the very worst), a second one only in a tick where a push did not go out or a new board was found; plus about 55 from the forecast |
| KV writes to one key | 1 per second | 1 per tick; a second write in the same tick waits 1.1 s after the first |
| aviationweather.gov | 100 requests a minute | 3 every 5 minutes |
| Pushover | 10,000 messages a month | one a day plus alerts |

## What it does not do

- It does not watch the forecast, only observations.
- KV can serve a value up to a minute old after a write elsewhere. Ticks are 5
  minutes apart, so this should not show; if it ever does, the symptom is one
  repeated alert.
- If a KV write fails after a push went out, the next tick pushes the same
  fields again. The log line is `fog_state_write_failed`.
- The station list goes to aviationweather.gov as a query (40 ids a request),
  as it does when the site's airport panel asks for a report. Fog alerts and the
  heartbeat pass through Pushover.
