#!/usr/bin/env python3
"""The watchdog's embedded script, run against what the builders really write.

Run: python3 forecast/test_watchdog.py   (standard library only)

.github/workflows/watchdog.yml carries its checks as a Python script inside a
YAML step, so nothing imported it and nothing tested it: its 60-odd scenarios
lived in a scratch folder. It copies three constants out of build_deploy.py
(the 10:00Z flip, the 24 h carry, the reasons a build gives for publishing no
day-level chance), one list out of pipeline/build_chase.py (the approach-light
codes) and reads about twenty payload fields by name; a rename in a builder
would have shown up only as a live watchdog failure (review, 2026-11-25).

This file takes the script out of the YAML as text and runs it, UNEDITED, in a
child Python whose clock, network and sleep are replaced, against:
  - a planner payload made here by build_deploy.assemble with the committed
    daycal.json, at two clocks (before and after the 10:00Z flip);
  - the chase.json the site ships (app/public/data/chase.json);
  - small stand-ins for the other endpoints.
So the seam it holds is watchdog <-> build_deploy.py <-> build_chase.py. The
cases after the healthy ones are the ways the script itself broke or passed
when it should not have (each failed before its fix; WATCHDOG_YML=<old file>
runs them against another copy of the workflow).
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
YML = Path(os.environ.get("WATCHDOG_YML") or REPO / ".github" / "workflows" / "watchdog.yml")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "pipeline"))
import build_deploy as bd  # noqa: E402

UTC = timezone.utc
CHASE = json.loads((REPO / "app" / "public" / "data" / "chase.json").read_text())
# the clock follows the runway data on disk, so a monthly refresh of chase.json does not age this test
CYCLE = datetime.fromisoformat(CHASE["meta"]["nasr_cycle"]).replace(tzinfo=UTC)
DAY = CYCLE + timedelta(days=20)


def probe_source() -> str:
    """The Python between `python3 - "$UA" > report.txt <<'PY'` and `PY`, as the runner's shell hands it over."""
    lines = YML.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.rstrip().endswith("<<'PY'"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "PY")
    return textwrap.dedent("\n".join(lines[start + 1:end])) + "\n"


SRC = probe_source()

# The child: the probe's text is not touched. datetime.now(), urlopen and
# time.sleep are replaced under it; everything else, the interpreter's own way
# of ending a script included, is real.
HARNESS = r'''
import datetime as _dt, io, json, sys, time, urllib.error, urllib.request
from urllib.parse import urlsplit
cfg = json.load(open(sys.argv[1]))
fixed = _dt.datetime.fromisoformat(cfg["now"])
class Clock(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
_dt.datetime = Clock
class Resp(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
calls = {}
def urlopen(req, timeout=None, **kw):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    u = urlsplit(url)
    key = u.path + ("?" + u.query if u.query else "")
    hit = cfg["routes"].get(key, cfg["routes"].get(u.path))
    calls[key] = calls.get(key, 0) + 1
    if isinstance(hit, list):                       # one answer per call, the last one repeated
        hit = hit[min(calls[key], len(hit)) - 1]
    if hit is None:
        raise urllib.error.HTTPError(url, 404, "not routed", None, None)
    if "error" in hit:
        raise urllib.error.URLError(hit["error"])
    if hit.get("status", 200) != 200:
        raise urllib.error.HTTPError(url, hit["status"], "stub", None, None)
    return Resp(open(hit["file"], "rb").read())
urllib.request.urlopen = urlopen
def sleep(s):
    if cfg.get("sleep_raises"):
        raise RuntimeError(cfg["sleep_raises"])
time.sleep = sleep
sys.argv = ["watchdog-probe", "test"]
exec(compile(open(cfg["probe"]).read(), "watchdog-probe.py", "exec"), {"__name__": "__main__"})
'''


# ---- the fixtures ---------------------------------------------------------------------------------
def planner(build: datetime, cycle_hour: int, **edit) -> dict:
    """A planner payload as the hourly build writes it: 500 airports, the committed day map, a scorecard."""
    ids = [f"K{a}{b}{c}" for a in "ABCDE" for b in "ABCDEFGHIJ" for c in "ABCDEFGHIJ"]
    atlas = {i: {"st": "KS", "country": "US"} for i in ids}
    cyc = build.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)
    if cyc > build:
        cyc -= timedelta(days=1)
    hours = {}
    for f in range(1, 49):
        v = cyc + timedelta(hours=f)
        if f <= 25 or v.hour % 3 == 0:
            hours[f] = 30 if v.hour == 12 else 0
    fhrs = sorted(hours)
    cur = {"meta": {"cycle": cyc.strftime("%Y-%m-%dT%H:00:00Z")},
           "airports": {i: {"fhrs": fhrs, "p": [[0, 0, 0, hours[f]] for f in fhrs]} for i in ids}}
    report = {"generated": SCORED, "runs": 900, "bar": {"rule": "n>=3000 pairs AND >=10 v10 event-hours",
                                                        "pass": [{"icao": i} for i in ids[:200]],
                                                        "fail": [{"icao": i} for i in ids[200:350]]}}
    payload, _, _ = bd.assemble(cur, build, bd.load_daycal(), atlas, ids, {}, lambda icao, dd, dte: (1.0, "nbe", 0.1),
                                scorecard=report)
    payload = json.loads(json.dumps(payload))
    for path, value in edit.items():                # "meta.gaps" = 5
        node = payload
        *parents, last = path.split(".")
        for k in parents:
            node = node[k]
        node[last] = value
    return payload


def feed(now: datetime, bom: str = "", zone: str = "Z", stale=()) -> str:
    """The live observation file: every chase airport and 2,600 more stations, reported 4 minutes ago."""
    cols = ["raw_text", "station_id", "observation_time", "latitude", "longitude", "temp_c", "dewpoint_c",
            "wind_dir_degrees", "wind_speed_kt", "visibility_statute_mi", "wx_string", "sky_cover", "cloud_base_ft_agl"]
    t = (now - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%S") + zone
    rows = [",".join(cols)]
    for i in list(CHASE["airports"]) + [f"Z{n:04d}"[:4] + "" if False else f"X{n:03d}" for n in range(2600)]:
        when = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S") + zone if i in stale else t
        rows.append(f"{i} 251450Z 00000KT 10SM CLR 10/05 A3001,{i},{when},40.0,-100.0,10.0,5.0,0,0,10+,,CLR,")
    return bom + "\n".join(rows) + "\n"


SCORED = DAY.strftime("%Y-%m-01T12:00:00Z")


class Site:
    """The live site as a folder of files and a route table; .run() is one watchdog tick."""

    def __init__(self, now: datetime, payload: dict):
        self.now, self.dir = now, Path(tempfile.mkdtemp(prefix="wd_"))
        self.routes: dict = {}
        public = ["KAAA", "KAAB", "KAAC"]
        iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.put("/fog/ksfo/data.json", {"updated": iso, "forecast": {"public": None}})
        self.put("/api/forecast", {"meta": {"cycle": (now - timedelta(hours=1, minutes=50)).strftime("%Y-%m-%dT%H:00:00Z"),
                                            "publicAirports": public},
                                   "airports": {f"A{n:04d}": {} for n in range(1100)}})
        self.put("/api/scorecard", {"generated": SCORED, "bar": {"pass": [{"icao": i} for i in public], "fail": []}})
        for path in ("/a94ea7fb1a87935bb7c2ec7dc6976f62.txt", "/sitemap-priority.xml", "/about/"):
            self.put(path, "ok")
        self.routes["/fog/zzzz/"] = {"status": 404}
        self.put("/fog/ksfo/", "<html><script src='https://static.cloudflareinsights.com/beacon.min.js'></script></html>")
        self.put("/fog/kpdx/data.json", {"forecast": {"nws": {"fetched": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%MZ"),
                                                              "office": "PQR", "verdict": "fog"}}})
        for i in public:
            self.put(f"/fog/{i.lower()}/data.json", {"forecast": {"public": {"p": 10}}})
        self.put("/api/deploy", payload)
        self.put("/api/now", feed(now))
        self.put("/data/chase.json", CHASE)
        self.put("/api/metar?ids=KSFO,KDEN&hours=3", [{"icaoId": "KSFO"}, {"icaoId": "KDEN"}, {"icaoId": "KSFO"}])

    def file(self, body) -> dict:
        p = self.dir / f"{len(list(self.dir.iterdir())):03d}.bin"
        p.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        return {"file": str(p)}

    def put(self, path: str, body) -> None:
        self.routes[path] = self.file(body)

    def run(self, **cfg) -> tuple[int, list[str]]:
        (self.dir / "probe.py").write_text(SRC)
        (self.dir / "harness.py").write_text(HARNESS)
        (self.dir / "cfg.json").write_text(json.dumps({"now": self.now.isoformat(), "routes": self.routes,
                                                       "probe": str(self.dir / "probe.py"), **cfg}))
        p = subprocess.run([sys.executable, "-B", str(self.dir / "harness.py"), str(self.dir / "cfg.json")],
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout.splitlines()


def at(hour: int, minute: int = 50) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def site(hour: int = 10, **edit) -> Site:
    """A healthy site at the :50 tick of `hour`, its planner built at :25 from the cycle of the hour before."""
    now = at(hour)
    return Site(now, planner(now - timedelta(minutes=25), (hour - 1) % 24, **edit))


def keys(lines, kind="FAIL") -> list[str]:
    """The issue keys: the text before the first colon of each line of that kind."""
    return [l[5:].split(":")[0] for l in lines if l.startswith(kind)]


LABELS = ["bake", "forecast", "forecast", "scorecard", "IndexNow key file", "unknown /fog/ path is a real 404",
          "priority sitemap", "about page", "analytics beacon on baked pages", "nws layer", "deploy planner",
          "deploy age", "deploy dates", "deploy shape", "deploy day 1", "public pages", "live feed", "metar proxy",
          "runway data", "runway data age", "led list"]


# ---- the seam with the builders ---------------------------------------------------------------------
def test_the_constants_the_watchdog_copies_are_the_builders_own():
    def const(name):
        m = re.search(rf"^{name} = (.+?)(?:\s+#.*)?$", SRC, re.M)
        assert m, f"{name} is not assigned in the watchdog script"
        return ast.literal_eval(m.group(1))
    assert const("FLIP_H") == bd.SERVICE_DAY_OFFSET_H == 10
    assert const("CARRY_MAX_AGE_H") == bd.CARRY_MAX_AGE_H == 24
    why = ast.literal_eval(re.search(r"^WHY = (\{.*?\})$", SRC, re.M | re.S).group(1))
    assert why == bd.WHY_WORDS, "the watchdog's reasons for a missing day-level chance are not build_deploy.WHY_WORDS"
    import build_chase as bc
    als = ast.literal_eval(re.search(r"ALS_CODES = (\{.*?\})", SRC, re.S).group(1))
    assert als == set(bc.ALS_CODES), "the watchdog's approach-light codes are not build_chase.ALS_CODES"


def test_a_healthy_site_passes_every_check_on_both_sides_of_the_flip():
    """The planner payload is made by build_deploy.assemble right here and
    the runway file is the one the site ships: a field either builder renames
    turns a line below into a FAIL."""
    for hour in (7, 10, 16, 23):         # an evening build (day 1 under way), the first after the flip, two daytime
        rc, lines = site(hour).run()
        assert rc == 0 and keys(lines) == [], (hour, [l for l in lines if l.startswith("FAIL")])
        assert keys(lines, "ok  ") == LABELS, (hour, keys(lines, "ok  "))
        said = "\n".join(lines)
        assert "deploy shape: format 2" in said and "500 of 500 airports" in said, said
        assert re.search(r"led list: \d+ airports with LED approach lights \(floor 40\), \d+ LED runway ends on the board", said)
        assert re.search(r"runway data: 9\d\d US airports .* with approach lights \(floor 20%\)", said), said


def test_a_planner_that_stopped_or_counts_from_the_wrong_day_is_named():
    s = site(16)
    s.put("/api/deploy", planner(at(16) - timedelta(hours=5), 10))
    rc, lines = s.run()
    assert rc == 1 and keys(lines) == ["deploy age"], lines
    # yesterday's last build, still live three hours after the flip
    s = site(13)
    s.put("/api/deploy", planner(at(9, 25), 8))
    rc, lines = s.run()
    assert rc == 1 and "deploy dates" in keys(lines), lines
    # ... and inside the two hours the next build is given, it is not an alarm
    s = Site(at(10), planner(at(9, 25), 8))
    assert s.run()[0] == 0


# ---- the script itself --------------------------------------------------------------------------------
def test_a_payload_field_of_the_wrong_type_does_not_end_the_script():
    """meta.gaps = 5 ended it with a TypeError and an EMPTY report: the issue
    opened with no body and the phone said "could not finish"."""
    rc, lines = site(10, **{"meta.gaps": 5}).run()
    assert rc == 0 and keys(lines, "ok  ") == LABELS, lines


def test_whatever_ends_the_script_the_lines_gathered_so_far_are_printed():
    """One uncaught exception lost every line already made: the report was two
    print loops on the script's last lines. Here the feed fails once and the
    wait before its retry blows up, which nothing in the script catches."""
    s = site(10)
    s.routes["/api/now"] = [{"status": 502}, s.routes["/api/now"]]
    rc, lines = s.run(sleep_raises="the runner was told to stop")
    assert rc == 1, rc
    assert keys(lines, "ok  ") == LABELS[:LABELS.index("live feed")], "every check made before it is still reported"
    stop = [l for l in lines if l.startswith("FAIL")]
    assert len(stop) == 1 and re.match(r"FAIL watchdog: the probe stopped at its line \d+ \(RuntimeError: the runner was told to stop\)", stop[0]), stop


def test_every_failure_line_has_a_key_the_issue_step_can_carry():
    """The issue step keys a failure on the text before the first colon and
    hides that key in an HTML comment. "baked page unreadable — <urlopen error
    timed out>" had no colon, so the whole line was the key, and its ">" ended
    the comment early: the step never found its own marker and commented on
    one outage every hour."""
    s = site(10)
    for path in list(s.routes):
        if path.startswith("/fog/") or path in ("/about/", "/sitemap-priority.xml"):
            s.routes[path] = {"error": "timed out"}
    rc, lines = s.run()
    bad = [l for l in lines if l.startswith("FAIL")]
    assert rc == 1 and len(bad) >= 6, lines
    for l in bad:
        assert re.match(r"FAIL [^:<>]{3,40}: ", l), f"no usable key in: {l}"
    assert "baked page" in keys(lines)
    # the workflow builds the key with these very commands; the marker must survive its own grep
    step = YML.read_text()
    assert "sed -E 's/^FAIL ([^:]+):.*/\\1/' | tr -d '<>' | sort -u" in step, "the key is no longer stripped of < and >"
    key = "|".join(sorted({re.sub(r"[<>]", "", k) for k in keys(lines)}))
    marker = f"<!-- watchdog-key: {key} @ 2026-11-25 -->"
    assert re.findall(r"<!-- watchdog-key: [^>]* -->", "text\n" + marker) == [marker]


def test_the_feed_check_reads_a_marked_file_and_zone_less_times():
    """A byte-order mark ahead of the header failed here ("no column named
    raw_text") while the app and the engine read the same file; times without
    a zone suffix raised a TypeError that ended the script with no report."""
    s = site(10)
    s.put("/api/now", feed(s.now, bom="﻿"))        # on disk the three bytes EF BB BF, as a server would send them
    assert Path(s.routes["/api/now"]["file"]).read_bytes()[:3] == b"\xef\xbb\xbf"
    rc, lines = s.run()
    assert rc == 0 and "live feed" in keys(lines, "ok  "), [l for l in lines if "feed" in l]
    s = site(10)
    s.put("/api/now", feed(s.now, zone=""))
    rc, lines = s.run()
    assert rc == 0 and keys(lines, "ok  ") == LABELS, lines
    # the gate itself still bites: the chase airports' own reports three hours old
    s = site(10)
    s.put("/api/now", feed(s.now, stale=set(CHASE["airports"])))
    rc, lines = s.run()
    assert rc == 1 and keys(lines) == ["live feed age"], lines


def test_the_metar_proxy_must_answer_for_both_stations_and_gets_one_retry():
    s = site(10)
    s.put("/api/metar?ids=KSFO,KDEN&hours=3", [{"icaoId": "KSFO"}] * 3)
    rc, lines = s.run()
    assert rc == 1 and keys(lines) == ["metar proxy"], lines
    assert "returned 3 reports for KSFO (want reports for both)" in lines[-1] and "second try" in lines[-1], lines[-1]
    s = site(10)
    s.routes["/api/metar?ids=KSFO,KDEN&hours=3"] = [{"status": 502}, s.routes["/api/metar?ids=KSFO,KDEN&hours=3"]]
    rc, lines = s.run()
    assert rc == 0 and any(l.startswith("ok   metar proxy") and "on the second try" in l for l in lines), lines


def test_no_public_airport_beside_a_scorecard_that_has_passes_is_a_failure():
    s = site(10)
    fc = json.loads(Path(s.routes["/api/forecast"]["file"]).read_text())
    fc["meta"]["publicAirports"] = []
    s.put("/api/forecast", fc)
    rc, lines = s.run()
    assert rc == 1 and keys(lines) == ["public pages"], lines


def test_an_issuance_whose_run_logged_no_truth_observations_is_a_failure():
    """engine.py now says in the issuance how many observations it logged
    (meta.obs_recorded): a download that parsed to nothing was "obs recorded: 0"
    in a step log of a green run and nowhere this script could read."""
    def with_obs(n):
        s = site(10)
        fc = json.loads(Path(s.routes["/api/forecast"]["file"]).read_text())
        fc["meta"].update(obs_recorded=n, obs_stations=1215)
        s.put("/api/forecast", fc)
        return s.run()
    rc, lines = with_obs(1062)
    assert rc == 0 and any(l.startswith("ok   forecast truth: 1062 observations logged for 1215 forecast stations") for l in lines), lines[:4]
    rc, lines = with_obs(0)
    assert rc == 1 and keys(lines) == ["forecast truth"], lines
    assert with_obs(607)[0] == 1 and with_obs(608)[0] == 0      # half of 1,215 is the line


def test_runway_data_without_its_approach_lights_is_caught_on_the_served_file():
    """build_chase.py refuses to write such a file; this is the same gate on
    what is being served. A blank approach-light column kept 900 airports and
    46.6% with no approach, so the check passed, and the default CHASE board
    held 0 US airports."""
    for name, als in (("blank", lambda e: None), ("shifted", lambda e: "N" if e.get("als") else "Y" if e["len"] > 9000 else None)):
        doc = json.loads(json.dumps(CHASE))
        for ends in doc["airports"].values():
            for e in ends:
                if not e.get("cur"):
                    e["als"] = als(e)
        s = site(10)
        s.put("/data/chase.json", doc)
        rc, lines = s.run()
        assert rc == 1 and "runway data" in keys(lines), (name, lines[-4:])


def test_an_led_airport_with_no_runway_placed_still_counts_on_the_board():
    """build_chase.led_block gives such an airport no named end, led_unk on its
    approach-light ends, and board = 1. Counting only led: 1 the watchdog's
    own number of board airports fell one short of led.n_airports_board."""
    doc = json.loads(json.dumps(CHASE))
    row = next(a for a in doc["led"]["airports"] if a["board"] and len(a["ends"]) == 1 and a["ends"][0]["board"])
    for e in doc["airports"][row["id"]]:
        e.pop("led", None)
        e.pop("led_ir", None)
        if e.get("als"):
            e["led_unk"] = 1
    row.update(ends=[], rwy_unknown=1, board=1)
    doc["led"]["n_ends"] -= 1
    doc["led"]["n_ends_board"] -= 1
    s = site(10)
    s.put("/data/chase.json", doc)
    rc, lines = s.run()
    assert rc == 0 and "led list" in keys(lines, "ok  "), [l for l in lines if "led" in l]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", name, "-", repr(e)[:600])
    print(f"{'FAILED' if fails else 'passed'}")
    sys.exit(1 if fails else 0)
