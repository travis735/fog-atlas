#!/usr/bin/env python3
"""Fit the day-level map for the DEPLOY planner's days 1-2 (the "cal" tier).

Why this exists: build_deploy.py published, for days 1-2, the chance of at
least one below-CAT-I hour as 1 - prod(1 - p_hour) — consecutive fog hours
treated as independent coin flips — and the app turned >= 0.5 into the word
GO. A back-test over this hindcast (2026-11-24 readiness review) found the
hourly probabilities and the expected-hours sum sound, but the product about
1.7x high (shown 0.5-0.6 -> 29% observed; 0.95-1.0 -> 66%) and, on the 2025
holdout, worse than the site's own per-airport monthly day climatology.

What it does (run on the owner's Mac — forecast/data/hindcast and
forecast/out/truth.parquet are gitignored and exist only there):
  1. Replays forecast/engine.py's hourly scoring over every local NBH+NBS
     hindcast cycle (calibration.json v2 products + per-airport offsets +
     integer rounding). The scoring primitives are imported from engine.py;
     the merge and feature block are a copy of engine.main()'s, and --parity
     proves the copy against a real engine run.
  2. Aggregates each airport-cycle with build_deploy.cal_day_stats — the same
     function the live build calls — into the any-hour product for day 1 and
     for day 2 at each coverage end hour (build_deploy.DAY2_COVER).
  3. Joins truth: did the UTC day contain at least one hourly below-CAT-I
     flag (>= 18 hourly obs required).
  4. Fits one monotone map per statistic on 2023-2024, tests it on 2025, and
     writes forecast/out/daycal.json: the breakpoints build_deploy.py reads
     in CI with the standard library, plus the receipts below.

PRE-REGISTERED on 2026-11-24, before this fitter had scored a 2025 row:
  Rows       every engine station, weekly 12Z cycles. Fit on valid days in
             2023-2024, test on valid days in 2025 (the calibration's own
             holdout year).
  Statistic  the any-hour product exactly as build_deploy.cal_day_stats
             builds it: all of day 1; day 2 up to each coverage end hour.
  Map        isotonic regression (pool-adjacent-violators) of the outcome on
             the statistic. Blocks with fewer than 200 training station-days
             are pooled into the block below — on 2023-24 the top block of a
             bare isotonic fit is 6 station-days that all fogged, which would
             print 100%. One breakpoint per block at its mean statistic,
             linear interpolation between, flat beyond both ends.
  Published  p = max(map(statistic), peak hourly chance). A day cannot be
             less likely to hold a fog hour than its likeliest hour; without
             the floor the two numbers could argue on screen. (Chosen on
             2023-24 only: there the floor bound on 0.2% of day-1 rows.)
  SHIP RULE  a map ships only if on the 2025 holdout the PUBLISHED p
             (a) has Brier skill > 0 against the per-airport monthly day
                 climatology (dayscale.json climo_day; rows that have one), and
             (b) its >= 0.5 group, when it has n >= 50, verifies within
                 10 points of its mean.
             Each day-2 coverage hour is judged alone. A day-2 map that does
             not ship means build_deploy does not publish that day as "cal".
  No refit   the map that was tested (2023-24) is the map that ships.
  Reported, not gating: the map scored on a sample of the live bake's cycle
             hour (--fetch-transfer), and the residual tables for Central
             Valley fields, Nov-Feb, chase airports and fog-duration classes.
  Known when this was written: the readiness review had scored a bare
  isotonic map of the day-1 product and of the day-2 product to 12Z on 2025
  (+11% and +7% skill). The minimum block, the one-point-per-block form, the
  floor and every day-2 coverage below 12Z had not been scored on 2025.

RESULT of that one run (added after it; every number is in daycal.json):
  day 1 ships, +11.0% skill, >= 0.5 group shown 0.64 / observed 0.58 (n 427);
  day 2 ships for coverage to 06Z, 09Z and 12Z (+3.5%, +6.0%, +7.1%) and is
  held for 00Z and 03Z (-4.1%, -0.5%). Not clean everywhere: values published
  at 0.75+ verified at 0.68 (n 115), Central Valley fields at >= 0.5 verified
  at 0.37 over three years (n 65), and day 2 in Nov-Feb 2025 at >= 0.5
  verified at 0.40 (n 67). No covariate was added: on 2023-24 the peak hour
  carried no signal beyond the map, and the Central Valley rows are about 15
  cycle dates.

  (Those are the first run's numbers. They were scored with the leak described
  in amendment 1 below; the numbers in daycal.json now are the leak-free ones.)

AMENDED on 2026-11-24 after an independent review of that run. 2025 had been
seen by then, so none of this is a clean pre-registration. Every amendment
only WITHHOLDS a number or a "calibrated" label; none loosens the ship rule,
and the maps are still the ones fitted on 2023-24.
  1. Leak-free holdout. The engine's climatology (climo.parquet) is built from
     every year of truth, 2025 included, and it is a model feature — so the
     2025 forecasts in the first run had seen 2025 outcomes (worth about 0.7
     skill points on day 1). Every cycle of the test year is now replayed with
     a climatology that stops on 31 December of the year before.
  2. The rule is applied at the cycle hour that is actually used. The fit is
     on 12Z cycles; the live bake runs the 09Z cycle and a manual redeploy can
     run any hour. The same rule (a)+(b) is now also scored on 30 real cycles
     of 2025 at 00Z, 09Z and 21Z. A map is published from cycle hour h only
     when the nearest sampled hours at or below h and at or above h (among the
     hours that use that map) both pass; the 12Z holdout is the sample at
     hour 12. On 00Z cycles the day-1 map ran 15 points high at 50% or more,
     and day 2 from real 09Z cycles missed rule (b); neither is published.
  3. Fog season. Rule (b) is also read off the Nov-Feb >= 0.5 group of every
     sample behind a published hour. Day 2 to 12Z showed 61% and verified 46%
     there, so that map is not published for November-February days.
  4. Areas. Rule (b) is also read off each area's own >= 0.5 group, on the
     2025 holdout and on all three years, wherever the group has 50
     airport-days. Central Valley fields shown at 50% or more fogged 35% of
     the time. Where either sample misses by more than 10 points the day is
     not shown as calibrated for that area's fields (the number itself is
     unchanged); every judged area carries its own record so the screen never
     prints the pooled one beside it.
  5. Receipts carry the >= 0.75 group and the number of cycle dates behind
     each group (420 airport-days were 43 dates), and the build publishes the
     receipts of the sample nearest its own cycle hour.

RESULT after the amendments (every number is in daycal.json):
  day 1 ships: +10.3% skill on the leak-free holdout, >= 0.5 group shown 0.64
  / observed 0.58 (n 420). By cycle hour: 09Z +9.7%, 0.62 / 0.56 (n 252),
  pass; 21Z +14.9%, 0.65 / 0.61 (n 375), pass; 00Z +6.5%, 0.62 / 0.46
  (n 221), FAIL. Published from 09Z-21Z cycles only.
  day 2 is not published by the daily bake: its map to 09Z passes on
  12Z-truncated cycles (+4.9%, 0.59 / 0.54) but real 09Z cycles gave
  0.59 / 0.46 (n 101). To 06Z it now misses rule (b) outright (0.60 / 0.49).
  To 12Z it ships from a 12Z cycle only (21Z cycles: 0.62 / 0.52), and not
  in November-February.
  Areas, day 1: not shown as calibrated for Central Valley (0.65 / 0.35,
  n 63, three years), West Coast outside it (0.65 / 0.54, n 296, three
  years; 0.65 / 0.60 on 2025 alone), Northeast (0.64 / 0.51, n 142, 2025),
  Great Lakes (0.62 / 0.78, low) and Southeast (0.62 / 0.84, low). Plains and
  Canada pass; Mountain West, Alaska and Hawaii have too few fog days to
  judge. The pooled map is right on average and wrong by region: maps fitted
  per region are the real fix and need their own pre-registration.
  Still true: values published at 0.75+ verified at 0.67 (n 108).

Outputs: forecast/out/daycal.json (committed) and
forecast/out/daycal_replay.parquet (gitignored cache of step 1; delete it or
pass --rebuild after calibration.json, climo.parquet, truth.parquet or the
hindcast change).

    .venv/bin/python forecast/fit_daycal.py                   # fit + holdout + checks
    .venv/bin/python forecast/fit_daycal.py --parity          # after `python forecast/engine.py`
    .venv/bin/python forecast/fit_daycal.py --fetch-transfer  # the 00Z/09Z/21Z check samples, 5 GB
"""
import argparse
import hashlib
import inspect
import json
import math
import multiprocessing as mp
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).parent
OUT = HERE / "out"
HIND = HERE / "data" / "hindcast"
# The transfer sample (cycles at the live bake's hour) sits in its own folder:
# fit_calibration.py globs hindcast/nb[sh]_*.txt, and 2025 files dropped beside
# the 12Z set would quietly change that script's holdout.
TRANSFER = HIND / "transfer"
CACHE = OUT / "daycal_replay.parquet"

sys.path.insert(0, str(HERE))
from parse_nbm import parse_collective  # noqa: E402
from engine import clim_logit, load_climo, score  # noqa: E402  (the engine's own scoring primitives)
from build_deploy import (BAND_MAX_GAP, BAND_MIN_N, DAY2_COVER, apply_daymap, area_of,  # noqa: E402
                          cal_day_stats, day2_cover_hour, day_chance, daymap_key, map_cycle_hours)

THRESHOLDS = ["v10", "v05", "v025", "sub"]
# The holdout year. Forecasts for it are replayed with a climatology that
# stops before it (amendment 1 in the docstring).
TEST_YEAR = 2025


# ---------------------------------------------------------------- replay ----
def climo_before(year: int):
    """The engine's climatology from truth before 1 January of `year`.

    Same SQL as build_truth.py's climo table and the same shape load_climo()
    returns. The 2026-11-24 review found the first run had scored 2025
    forecasts whose clim_logit feature was built from 2025 outcomes."""
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT icao, month(ts) AS mon, hour(ts) AS hr, count(*) AS n,
               avg(v10) AS r10, avg(v05) AS r05, avg(v025) AS r025, avg(sub) AS rsub
        FROM '{OUT / "truth.parquet"}' WHERE ts < '{int(year)}-01-01' GROUP BY 1, 2, 3""").fetchall()
    means = [float(np.mean([r[i] for r in rows])) for i in (4, 5, 6, 7)]
    return {(r[0], r[1], r[2]): r for r in rows}, dict(zip(THRESHOLDS, means))


class Ctx:
    """Everything engine.main() loads before it scores a bulletin."""
    def __init__(self, pre_test: bool = False):
        self.stations = set(json.load(open(HERE / "stations.json")))
        calib = json.load(open(OUT / "calibration.json"))
        if calib.get("version") != 2:
            raise RuntimeError("fit_daycal replays the v2 calibration only; calibration.json is not version 2")
        self.products, self.offsets = calib["products"], calib.get("offsets", {})
        self.climo, self.means = load_climo()
        # only the replay workers need it; --parity replays a live cycle
        self.climo_pre, self.means_pre = climo_before(TEST_YEAR) if pre_test else (None, None)


def fingerprint() -> str:
    """Changes whenever what the replay reads or calls changes: the fitted
    calibration, the climatology, the station list, the bulletin parser and
    the three engine functions imported above. (Not all of engine.py: its
    observation-logging half changes without touching a forecast.)"""
    h = hashlib.sha1()
    for p in (OUT / "calibration.json", OUT / "climo.parquet", HERE / "stations.json", HERE / "parse_nbm.py"):
        h.update(p.read_bytes())
    for fn in (score, clim_logit, load_climo):
        h.update(inspect.getsource(fn).encode())
    return h.hexdigest()[:12]


def replay_fingerprint(n_pairs: int) -> str:
    """The cache key: fingerprint() plus what only the hindcast replay reads —
    the truth table behind the pre-test-year climatology (2.6 GB, so size and
    mtime, not its bytes), the test year, and how many cycles are on disk."""
    st = (OUT / "truth.parquet").stat()
    return f"{fingerprint()}:{TEST_YEAR}:{st.st_size}:{int(st.st_mtime)}:{n_pairs}"


def replay_bulletins(nbh: Path, nbs: Path | None, ctx: Ctx, thresholds=("sub",), pre_test: bool = False):
    """One cycle through the engine's merge + scoring.

    Returns (cycle, {icao: (fhrs, rows)}), rows[i] = integer percents in the
    order of `thresholds`. The body between the two markers is a copy of
    engine.main() (the block is inline there, so it cannot be imported);
    `--parity` checks it against the engine's own current.json.
    pre_test=True scores with the climatology that stops before TEST_YEAR."""
    rows = parse_collective(nbh, ctx.stations)
    if not rows:
        return None, {}
    cycle_iso = rows[0]["cycle"]
    if nbs is not None and nbs.exists():
        more = parse_collective(nbs, ctx.stations)
        if more and more[0]["cycle"] != cycle_iso:
            raise RuntimeError(f"{nbs.name} is cycle {more[0]['cycle']}, {nbh.name} is {cycle_iso}")
        rows += more
    cycle = datetime.fromisoformat(cycle_iso).replace(tzinfo=timezone.utc)
    products, offsets = ctx.products, ctx.offsets
    climo, means = (ctx.climo_pre, ctx.means_pre) if pre_test else (ctx.climo, ctx.means)

    # ---- copy of engine.main(): merge, NBH wins lead 1-25, NBS fills 26-48 ----
    per: dict[str, dict[int, dict]] = {}
    for r in rows:
        if r["fhr"] < 1 or r["fhr"] > 48 or r["vis_sm"] is None:
            continue
        keep = per.setdefault(r["icao"], {})
        cur = keep.get(r["fhr"])
        if cur is None or (r["product"] == "NBH") > (cur["product"] == "NBH"):
            keep[r["fhr"]] = r

    airports = {}
    for icao, byf in per.items():
        fhrs = sorted(byf)
        probs = []
        for f in fhrs:
            r = byf[f]
            valid = cycle + timedelta(hours=f)
            feats_base = {
                "log_vis": math.log1p(min(max(r["vis_sm"], 0), 10)),
                "cig_k": (r["ceil_ft"] if r["ceil_ft"] is not None else 25000) / 1000.0,
                "ifv": r["ifv"] if r["ifv"] is not None else 0.0,
                "ifc": r["ifc"] if r["ifc"] is not None else 0.0,
                "spread": min(max((r["tmp"] - r["dpt"]) if r["tmp"] is not None and r["dpt"] is not None else 10, -5), 40),
                "wsp": min(max(r["wsp"] if r["wsp"] is not None else 5, 0), 50),
                "lead": f,
                "liv": r.get("liv") if r.get("liv") is not None else 0.0,
                "lic": r.get("lic") if r.get("lic") is not None else 0.0,
            }
            prod = r["product"] if r["product"] in products else "NBS"
            pcal = products[prod]["thresholds"]
            poff = offsets.get(prod, {})
            row = []
            for thr in thresholds:
                feats = dict(feats_base,
                             clim_logit=clim_logit(climo, means, icao, valid.month, valid.hour, thr))
                z_extra = poff.get(thr, {}).get(icao, 0.0)
                p = score(pcal[thr], feats) if thr in pcal else 0.0
                if z_extra and 0.0 < p < 1.0:
                    z = math.log(p / (1 - p)) + z_extra
                    p = 1.0 / (1.0 + math.exp(-z))
                row.append(round(100 * p))
            probs.append(row)
        airports[icao] = (fhrs, probs)
    # ---- end of the copy ----
    return cycle, airports


_CTX = None


def _replay_one(pair):
    """(icao, cycle, fhr, p_all, p) for one cycle. p_all is the engine as it
    runs live (climatology from every year). p is what the fit and every check
    use: the same for cycles before TEST_YEAR, and for TEST_YEAR cycles the
    replay with the climatology that stops before that year."""
    global _CTX
    if _CTX is None:
        _CTX = Ctx(pre_test=True)
    nbh, nbs = pair
    cycle, airports = replay_bulletins(nbh, nbs, _CTX)
    if cycle is None:
        return []
    cyc = cycle.replace(tzinfo=None)
    pre = replay_bulletins(nbh, nbs, _CTX, pre_test=True)[1] if cyc.year >= TEST_YEAR else airports
    out = []
    for icao, (fhrs, probs) in airports.items():
        fh2, pr2 = pre[icao]
        assert fh2 == fhrs, (nbh.name, icao)     # the climatology never decides which hours exist
        out += [(icao, cyc, f, row[0], row2[0]) for f, row, row2 in zip(fhrs, probs, pr2)]
    return out


def cycle_files() -> list[tuple[Path, Path]]:
    """Every cycle the engine could have run: an NBH bulletin, with its NBS twin when present."""
    pairs = []
    for nbh in sorted(HIND.glob("nbh_*_12z.txt")) + sorted(TRANSFER.glob("nbh_*z.txt")):
        pairs.append((nbh, nbh.with_name("nbs_" + nbh.name[4:])))
    return pairs


def load_replay(rebuild: bool = False) -> pd.DataFrame:
    """icao, cycle, fhr, p_all, p (integer percents below CAT I) for every hindcast cycle; see _replay_one."""
    pairs = cycle_files()
    fp = replay_fingerprint(len(pairs))
    if CACHE.exists() and not rebuild:
        df = pd.read_parquet(CACHE)
        if len(df) and df.fp.iloc[0] == fp and "p_all" in df.columns:
            print(f"replay cache: {len(df):,} hourly rows, {df.cycle.nunique()} cycles (cached)")
            return df.drop(columns="fp")
        print("replay cache is stale (inputs changed) — rebuilding")
    print(f"replaying {len(pairs)} cycles…")
    with mp.Pool(min(10, mp.cpu_count())) as pool:
        chunks = pool.map(_replay_one, pairs, chunksize=4)
    df = pd.DataFrame([r for c in chunks for r in c], columns=["icao", "cycle", "fhr", "p_all", "p"])
    for c in ("fhr", "p_all", "p"):
        df[c] = df[c].astype("int16")
    df = df.sort_values(["cycle", "icao", "fhr"]).reset_index(drop=True)
    df.assign(fp=fp).to_parquet(CACHE)
    print(f"replay: {len(df):,} hourly rows, {df.cycle.nunique()} cycles -> {CACHE.name}")
    return df


# ------------------------------------------------------------- day table ----
def bake_date(cyc: datetime) -> date:
    """The UTC date build_deploy would call `today` when it runs on this cycle:
    the forecast job at h:25 picks up cycle h-1 (run logs, Nov 2026)."""
    return (cyc + timedelta(hours=1, minutes=25)).date()


def day_table(replay: pd.DataFrame, col: str = "p") -> pd.DataFrame:
    """One row per airport x cycle x statistic, built by build_deploy.cal_day_stats.

    12Z cycles get day 1 and day 2 at every coverage end hour (truncation is
    how a 12Z cycle stands in for an earlier one); other cycle hours get what
    the live build would compute from them. col picks the replay column
    ("p_all" only to measure what the test-year climatology leak was worth)."""
    icao, cyc_ns = replay.icao.values, replay.cycle.values
    fhr, p = replay.fhr.values.tolist(), replay[col].values.tolist()
    edge = np.flatnonzero((icao[1:] != icao[:-1]) | (cyc_ns[1:] != cyc_ns[:-1])) + 1
    starts, ends = np.r_[0, edge], np.r_[edge, len(replay)]
    cycles = pd.to_datetime(replay.cycle.values[starts]).to_pydatetime()
    rows = []
    for a, b, cyc in zip(starts.tolist(), ends.tolist(), cycles):
        today = bake_date(cyc)
        covers = DAY2_COVER if cyc.hour == 12 else (day2_cover_hour(cyc.hour),)
        first = True
        for c in covers:
            st = cal_day_stats(fhr[a:b], p[a:b], cyc, today, c)
            for dd, s in st.items():
                if dd == 1 and not first:
                    continue
                if s["pk"] is None:
                    continue
                rows.append((icao[a], cyc, daymap_key(dd, c), today + timedelta(days=dd),
                             1.0 - s["noev"], s["pk"], s["used"], s["eh"]))
            first = False
    df = pd.DataFrame(rows, columns=["icao", "cycle", "key", "vday", "raw", "pk", "used", "eh"])
    df["hour"] = pd.to_datetime(df.cycle).dt.hour
    return df


def load_truth() -> pd.DataFrame:
    """icao, d, any_sub, hrs, nobs per UTC day: the hourly truth of
    fit_calibration.py (obs rounded to the nearest hour), rolled up by day."""
    con = duckdb.connect()
    df = con.execute(f"""
        WITH th AS (
          SELECT icao, date_trunc('hour', ts + INTERVAL 30 MINUTE) AS hr_ts, max(sub) AS sub
          FROM '{OUT / "truth.parquet"}'
          WHERE ts >= '2022-12-31' AND ts < '2026-01-10' GROUP BY 1, 2)
        SELECT icao, hr_ts::DATE AS d, max(sub)::INT AS any_sub, sum(sub)::INT AS hrs, count(*)::INT AS nobs
        FROM th GROUP BY 1, 2
    """).fetchdf()
    df["d"] = pd.to_datetime(df.d).dt.date
    return df


def assemble(days: pd.DataFrame) -> pd.DataFrame:
    """Day table + truth (>= 18 hourly obs) + the climatology baseline."""
    truth = load_truth()
    m = days.merge(truth, left_on=["icao", "vday"], right_on=["icao", "d"], how="inner")
    m = m[m.nobs >= 18].drop(columns="d").copy()
    vd = pd.to_datetime(m.vday.astype(str))
    m["year"], m["mon"] = vd.dt.year, vd.dt.month
    cl = json.load(open(OUT / "dayscale.json"))["climo_day"]
    pc = [cl.get(i, {}).get(str(mo)) for i, mo in zip(m.icao, m.mon)]
    m["p_clim"] = [c[0] if c else np.nan for c in pc]
    # climatological fog hours per fog day: how long fog lasts here once it is in
    m["hpd"] = [c[1] / c[0] if c and c[0] > 0.02 else np.nan for c in pc]
    atlas = {a["icao"]: a for a in json.load(open(HERE.parent / "app" / "public" / "data" / "airports.json"))["airports"]}
    m["area"] = [area_of(i, atlas.get(i), AREAS) for i in m.icao]
    return m


# ------------------------------------------------------------------- fit ----
MIN_BLOCK = 200
BANDS = [0.0, 0.02, 0.05, 0.10, 0.25, 0.50, 0.75, 1.0]


def fit_map(raw: np.ndarray, y: np.ndarray, min_block: int = MIN_BLOCK, ends: bool = False) -> dict:
    """Isotonic regression of y on raw (pool-adjacent-violators) -> breakpoints.

    Ties are pooled at 6 decimals first: the same product reached through a
    different multiplication order differs in the 16th digit, and PAV would
    otherwise order those rows by rounding noise. Blocks under min_block rows
    are then pooled into the block below. One breakpoint per block at its mean
    raw value (ends=True instead keeps both ends of every block, which is
    scikit-learn's IsotonicRegression — used only to reproduce the review)."""
    x = np.round(raw.astype(float), 6)
    o = np.argsort(x, kind="stable")
    ux, idx = np.unique(x[o], return_index=True)
    n = np.diff(np.r_[idx, len(x)]).astype(float)
    sy = np.add.reduceat(y[o].astype(float), idx)
    blocks: list[list[float]] = []            # [x_lo, x_hi, sum_x, sum_y, n]
    for xv, s, k in zip(ux.tolist(), sy.tolist(), n.tolist()):
        blocks.append([xv, xv, xv * k, s, k])
        while len(blocks) > 1 and blocks[-2][3] / blocks[-2][4] >= blocks[-1][3] / blocks[-1][4]:
            b = blocks.pop()
            a = blocks[-1]
            a[1] = b[1]; a[2] += b[2]; a[3] += b[3]; a[4] += b[4]
    i = len(blocks) - 1
    while i > 0:                              # top down: a thin block joins the one below it
        if blocks[i][4] < min_block:
            b = blocks.pop(i)
            a = blocks[i - 1]
            a[1] = b[1]; a[2] += b[2]; a[3] += b[3]; a[4] += b[4]
        i -= 1
    if len(blocks) > 1 and blocks[0][4] < min_block:
        b = blocks.pop(1)
        a = blocks[0]
        a[1] = b[1]; a[2] += b[2]; a[3] += b[3]; a[4] += b[4]
    if ends:
        pts = [(bx, b[3] / b[4]) for b in blocks for bx in ((b[0], b[1]) if b[1] > b[0] else (b[0],))]
    else:
        pts = [(b[2] / b[4], b[3] / b[4]) for b in blocks]
    xs = [round(p[0], 6) for p in pts]
    ys = [round(min(max(p[1], 0.0), 1.0), 4) for p in pts]
    if len(xs) == 1:                          # one block: a constant map still needs two points
        xs, ys = [0.0, 1.0], [ys[0], ys[0]]
    assert all(a < b for a, b in zip(xs, xs[1:])) and all(a <= b for a, b in zip(ys, ys[1:]))
    return {"x": xs, "y": ys, "n_fit": int(len(x)), "blocks": len(blocks),
            "min_block_n": int(min(b[4] for b in blocks))}


def published(m: dict, raw: np.ndarray, pk: np.ndarray) -> np.ndarray:
    """build_deploy.day_chance + cal_tier's rounding, vectorised (asserted equal in main)."""
    return np.round(np.maximum(np.interp(raw, m["x"], m["y"]), pk / 100.0), 3)


def brier(p, y) -> float:
    return float(((p - y) ** 2).mean())


def group(p, y, lo, hi, cyc=None) -> dict:
    """One band of published values: how many, what they showed, what happened.
    With cyc, also how many cycle dates they came from — fog days cluster, so
    427 airport-days from 42 dates is far fewer independent looks than 427."""
    s = (p >= lo) & (p < hi)
    k = int(s.sum())
    out = {"n": k, "shown": round(float(p[s].mean()), 3) if k else None,
           "obs": round(float(y[s].mean()), 3) if k else None}
    if cyc is not None:
        out["dates"] = int(len(np.unique(cyc[s])))
    return out


# ------------------------------------------------------------------ areas ----
# One pooled map over 1,100 stations hides where it is wrong. The 2026-11-24
# review cut the published day-1 chance by region: California fields shown at
# 50% or more fogged 48% of the time over three years and Central Valley
# fields 37%, while Southeast and Great Lakes fields ran 17 points LOW — and
# the screen printed the pooled "58% of the time" beside every one of them.
# A field belongs to the first area below that lists it. The Central Valley
# list is the one the first run's residual table already used; the rest are
# plain state groups, fixed here before any of them was scored.
CENTRAL_VALLEY = ("KVIS KFAT KMCE KMER KMOD KSCK KSMF KSAC KBFL KHJO KNLC KMAE KMYV KOVE KCIC KRBL KRDD "
                  "KMHR KPRB KPTV").split()
AREAS = [
    ("Central Valley", {"icaos": CENTRAL_VALLEY}),
    ("West Coast", {"st": "CA OR WA".split()}),
    ("Mountain West", {"st": "NV ID MT WY UT CO AZ NM".split()}),
    ("Plains", {"st": "ND SD NE KS OK TX MN IA MO".split()}),
    ("Great Lakes", {"st": "WI IL IN MI OH KY".split()}),
    ("Southeast", {"st": "AR LA MS AL GA FL SC NC TN".split()}),
    ("Northeast", {"st": "VA WV MD DE PA NJ NY CT RI MA VT NH ME DC".split()}),
    ("Alaska", {"st": ["AK"]}),
    ("Hawaii", {"st": ["HI"]}),
    ("Canada", {"country": "CA"}),
]
AREA_MIN_N = BAND_MIN_N        # the ship rule's own floor for judging a >= 0.5 group (50)
AREA_MAX_GAP = BAND_MAX_GAP    # and its own tolerance (10 points)


def area_receipts(a: pd.DataFrame, p: np.ndarray) -> list[dict]:
    """Rule (b) read off each area's own rows of one map's 12Z set.

    a = every year's rows. Two samples per area: the test year alone, and all
    years (the fit years are in-sample for the map, but the map is pooled over
    all stations, so an area that misses there misses either way — West Coast
    fields outside the Central Valley were 6 points high on 2025 alone and 11
    points high over three years and 296 airport-days). A sample is judged
    when its >= 0.5 group has AREA_MIN_N airport-days. ok is False when any
    judged sample misses by more than AREA_MAX_GAP, None when neither can be
    judged (the area's fields then keep the pooled receipt). `show` names the
    judged sample with the larger miss: that is the record printed on screen."""
    y, cyc = a.any_sub.values.astype(float), a.cycle.values
    test = (a.year == TEST_YEAR).values
    years = sorted(set(a.year))
    out = []
    for name, d in AREAS:
        s = (a.area == name).values
        samples = {str(TEST_YEAR): s & test, f"{years[0]}-{str(years[-1])[2:]}": s}
        bands = {label: {"ge50": group(p[k], y[k], 0.5, 1.01, cyc[k]), "b25_50": group(p[k], y[k], 0.25, 0.5, cyc[k]),
                         "ge75": group(p[k], y[k], 0.75, 1.01, cyc[k])} for label, k in samples.items()}
        gap = {label: b["ge50"]["shown"] - b["ge50"]["obs"] for label, b in bands.items()
               if b["ge50"]["n"] >= AREA_MIN_N}
        show = max(gap, key=lambda k: abs(gap[k])) if gap else None
        out.append({"name": name, **d, "stations": int(a.icao[s].nunique()), "judged": list(gap),
                    "ok": None if not gap else bool(all(abs(g) <= AREA_MAX_GAP for g in gap.values())),
                    "show": show, "high": None if show is None else bool(gap[show] > 0), "samples": bands})
    return out


def reliability(p, y) -> list[dict]:
    out = []
    for lo, hi in zip(BANDS, BANDS[1:]):
        g = group(p, y, lo, hi if hi < 1.0 else 1.01)
        if g["n"]:
            out.append({"lo": lo, "hi": hi, **g})
    return out


def score_rows(df: pd.DataFrame, p: np.ndarray) -> dict:
    """Holdout numbers for one set of rows and the p published on them."""
    y = df.any_sub.values.astype(float)
    raw = df.raw.values
    cyc = df.cycle.values
    has = df.p_clim.notna().values
    fog = df.mon.isin(FOG_SEASON).values
    b_p, b_c, b_raw = brier(p[has], y[has]), brier(df.p_clim.values[has], y[has]), brier(raw[has], y[has])
    return {"n": int(len(df)), "n_clim": int(has.sum()), "cycles": int(len(np.unique(cyc))),
            "base": round(float(y.mean()), 4),
            "brier": round(b_p, 5), "brier_climo": round(b_c, 5), "skill_pct": round(100 * (1 - b_p / b_c), 1),
            "raw_brier": round(b_raw, 5), "raw_skill_pct": round(100 * (1 - b_raw / b_c), 1),
            "ge50": group(p, y, 0.5, 1.01, cyc), "b25_50": group(p, y, 0.25, 0.5, cyc),
            # the ship rule judges the whole >= 0.5 group, which hid this one:
            # values published at 0.75 and up verified at 0.62-0.68 in every sample
            "ge75": group(p, y, 0.75, 1.01, cyc),
            # ...and this one: day 2 at >= 0.5 verified near 40% in Nov-Feb 2025
            "nov_feb": {"n": int(fog.sum()), "ge50": group(p[fog], y[fog], 0.5, 1.01, cyc[fog])},
            "raw_ge50": group(raw, y, 0.5, 1.01), "raw_b25_50": group(raw, y, 0.25, 0.5)}


FOG_SEASON = [11, 12, 1, 2]     # the months the first run's residual table already cut


def season_held(samples: list[dict]) -> list[int]:
    """Target-day months a map is not published in: FOG_SEASON when the
    Nov-Feb >= 0.5 group of any sample behind the map (50 airport-days or
    more, the ship rule's floor) misses by more than the rule's 10 points."""
    for h in samples:
        g = h["nov_feb"]["ge50"]
        if g["n"] >= BAND_MIN_N and abs(g["shown"] - g["obs"]) > BAND_MAX_GAP:
            return list(FOG_SEASON)
    return []


def verdict(h: dict) -> tuple[bool, bool | None]:
    a = h["skill_pct"] > 0
    g = h["ge50"]
    b = None if g["n"] < BAND_MIN_N else abs(g["shown"] - g["obs"]) <= BAND_MAX_GAP
    return a, b


def passes(h: dict) -> bool:
    """The ship rule on one sample's numbers (h from score_rows)."""
    a, b = verdict(h)
    return bool(a and b is not False)


def publish_hours(key: str, passed: dict[int, bool]) -> list[int]:
    """Cycle hours a map may be published from (amendment 2 in the docstring).

    passed = {sampled cycle hour: did the rule pass there}, only hours that
    use this map. Hour h is published when the nearest sampled hour at or
    below it and the nearest at or above it both exist and both passed: a
    cycle hour between two checked ones sees leads between theirs, a cycle
    hour beyond the last check sees leads nobody scored."""
    out = []
    for h in map_cycle_hours(key):
        lo = max((s for s in passed if s <= h), default=None)
        hi = min((s for s in passed if s >= h), default=None)
        if lo is not None and hi is not None and passed[lo] and passed[hi]:
            out.append(h)
    return out


RULE = ("A day's map ships only if, on the 2025 holdout, the published chance (a) has Brier skill > 0 against the "
        "per-airport monthly day climatology (dayscale.json climo_day, same rows) and (b) the group published "
        ">= 0.5, when it has n >= 50, verifies within 10 points of its mean. Each day-2 coverage hour is judged "
        "alone. The map tested (fitted on 2023-2024) is the map shipped; no refit on all three years.")
RULE_HOURS = ("Added after review, 2026-11-24: the same rule is scored on real cycles of the test year at each sampled "
              "cycle hour. A map is published from cycle hour h only when the nearest sampled hours at or below h "
              "and at or above h, among the hours that use that map, both pass (the 12Z holdout is the sample at "
              "hour 12), and only when the cycle is from the build's own UTC date.")
RULE_SEASON = ("Added after review, 2026-11-24: rule (b) is also read off the Nov-Feb >= 0.5 group of every sample "
               "behind a published hour (50 airport-days or more). Where it misses by more than 10 points the "
               "map is not published for target days in November to February.")
RULE_AREAS = ("Added after review, 2026-11-24: rule (b) is read off each area's own >= 0.5 group, on the test year "
              "and on all years, wherever that group has 50 airport-days. Where either misses by more than 10 "
              "points the day is not shown as calibrated for that area's fields; the number itself is unchanged. "
              "An area with neither sample at 50 is not judged.")


def residuals(te: pd.DataFrame, p: np.ndarray, chase: set) -> dict:
    """Where a pooled map could hide a bias: the fields and months the owner chases."""
    y = te.any_sub.values.astype(float)
    cuts = {"central_valley": te.icao.isin(CENTRAL_VALLEY).values,
            "nov_feb": te.mon.isin([11, 12, 1, 2]).values,
            "chase_airports": te.icao.isin(chase).values,
            "fog_hours_per_fog_day_under_3": (te.hpd < 3).values,
            "fog_hours_per_fog_day_3_to_5": ((te.hpd >= 3) & (te.hpd < 5)).values,
            "fog_hours_per_fog_day_5_plus": (te.hpd >= 5).values}
    out = {}
    for name, s in cuts.items():
        g = group(p[s], y[s], 0.5, 1.01)
        out[name] = {"n": int(s.sum()), "base": round(float(y[s].mean()), 4) if s.any() else None,
                     "ge50": g, "b25_50": group(p[s], y[s], 0.25, 0.5),
                     "off_by_over_15": bool(g["n"] >= 50 and abs(g["shown"] - g["obs"]) > 0.15),
                     "reliability": reliability(p[s], y[s])}
    return out


# --------------------------------------------------------------- parity ----
def parity(ctx: Ctx) -> dict:
    """Replay the bulletin the engine just scored and compare, hour for hour.

    Run `python forecast/engine.py` first: it leaves the bulletins in
    forecast/data/_nb[hs]_run.txt and its answer in forecast/out/current.json."""
    current = json.loads((OUT / "current.json").read_text())
    nbh, nbs = HERE / "data" / "_nbh_run.txt", HERE / "data" / "_nbs_run.txt"
    head = parse_collective(nbh, ctx.stations)[0]["cycle"] + "Z"
    if head != current["meta"]["cycle"]:
        raise RuntimeError(f"_nbh_run.txt is cycle {head} but current.json is {current['meta']['cycle']} — "
                           f"run forecast/engine.py first")
    # the engine only rewrites _nbs_run.txt when the cycle had an NBS bulletin
    nbs_rows = parse_collective(nbs, ctx.stations) if nbs.exists() else []
    nbs = nbs if nbs_rows and nbs_rows[0]["cycle"] + "Z" == head else None
    cycle, mine = replay_bulletins(nbh, nbs, ctx, THRESHOLDS)
    eng = current["airports"]
    hours = bad_hours = bad_fhrs = 0
    for icao in set(mine) | set(eng):
        if icao not in mine or icao not in eng or mine[icao][0] != eng[icao]["fhrs"]:
            bad_fhrs += 1
            continue
        for a, b in zip(mine[icao][1], eng[icao]["p"]):
            hours += 1
            bad_hours += a != b
    # aggregation: cal_day_stats against the pre-daycal build_deploy loop
    # (fb899ce5 lines 242-253, kept verbatim as the reference)
    today = bake_date(cycle)
    cal_day, cal_noev = {}, {}
    for icao, f in eng.items():
        for fhr, p in zip(f["fhrs"], f["p"]):
            dt = cycle + timedelta(hours=fhr)
            dd = (dt.date() - today).days
            if 1 <= dd <= 2:
                step = 1 if fhr <= 25 else 3
                cal_day[(icao, dd)] = cal_day.get((icao, dd), 0.0) + step * p[3] / 100.0
                cal_noev[(icao, dd)] = cal_noev.get((icao, dd), 1.0) * (1.0 - p[3] / 100.0) ** step
    days = bad_days = 0
    for icao, f in eng.items():
        st = cal_day_stats(f["fhrs"], [r[3] for r in f["p"]], cycle, today, 23)   # 23 = every day-2 hour
        for dd in (1, 2):
            if ((icao, dd) in cal_day) != (dd in st):
                bad_days += 1
                continue
            if dd in st:
                days += 1
                bad_days += st[dd]["eh"] != cal_day[(icao, dd)] or st[dd]["noev"] != cal_noev[(icao, dd)]
    return {"checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "cycle": current["meta"]["cycle"],
            "fingerprint": fingerprint(),
            "hourly": {"airports": len(eng), "hours": hours, "thresholds": THRESHOLDS,
                       "mismatched_hours": int(bad_hours), "airports_with_different_hours": bad_fhrs},
            "aggregation": {"airport_days": days, "mismatched": int(bad_days),
                            "compares": "eh and the any-hour product, exact float equality, against the "
                                        "pre-daycal build_deploy loop"},
            "ok": bad_hours == 0 and bad_fhrs == 0 and bad_days == 0 and hours > 0 and days > 0}


# ------------------------------------------------------ transfer samples ----
# The fit is on 12Z cycles; the daily bake runs at 10:25Z on the 09Z cycle
# (h:25 run -> cycle h-1 in every Nov 2026 run log), and a manual redeploy
# runs whatever hour it is dispatched in. These 30 cycle dates of 2025 — the
# hindcast's own Thursdays, every one in Nov-Feb plus 13 across the rest of
# the year — are scored with the 2023-24 maps at each hour below. They are
# never fitted on. 09Z was the first run's "reported, not gating" check; the
# review added 00Z (a redeploy on a US evening: day 1 is leads 24-47 h, almost
# all three-hourly) and 21Z (every lead hourly), and made all three gate.
TRANSFER_CYCLES = ("00", "09", "21")
TRANSFER_DATES = ("20250102 20250109 20250116 20250123 20250130 20250206 20250213 20250220 20250227 "
                  "20250306 20250320 20250403 20250417 20250501 20250605 20250703 20250807 20250904 20250918 "
                  "20251002 20251016 20251030 20251106 20251113 20251120 20251127 20251204 20251211 20251218 "
                  "20251225").split()


def fetch_transfer() -> None:
    import urllib.request
    TRANSFER.mkdir(parents=True, exist_ok=True)
    got = skip = miss = 0
    for hh in TRANSFER_CYCLES:
        for d in TRANSFER_DATES:
            for prod in ("nbh", "nbs"):
                dest = TRANSFER / f"{prod}_{d}_{hh}z.txt"
                if dest.exists() and dest.stat().st_size > 1_000_000:
                    skip += 1
                    continue
                url = f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{d}/{hh}/text/blend_{prod}tx.t{hh}z"
                try:
                    with urllib.request.urlopen(url, timeout=180) as r:
                        body = r.read()
                    if len(body) < 1_000_000:
                        raise RuntimeError(f"only {len(body)} bytes")
                    dest.write_bytes(body)
                    got += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  miss {prod} {d} {hh}Z: {e}")
                    miss += 1
    print(f"transfer samples: fetched {got}, already had {skip}, missing {miss} "
          f"of {2 * len(TRANSFER_DATES) * len(TRANSFER_CYCLES)}")


def hour_check(m: pd.DataFrame, fm: dict, key: str, hour: int) -> dict | None:
    """One map scored on real cycles of TEST_YEAR issued at `hour` (None when
    that sample is not on disk — the hour then counts as unchecked)."""
    a = m[(m.hour == hour) & (m.year == TEST_YEAR) & (m.key == key)]
    if not len(a):
        return None
    p = published(fm, a.raw.values, a.pk.values)
    h = score_rows(a, p)
    ra, rb = verdict(h)
    res = {**h, "rule_a_skill_over_0": bool(ra), "rule_b_ge50_within_10": rb, "pass": passes(h),
           "sample": f"{h['cycles']} real {hour:02d}Z cycles of {TEST_YEAR}, never fitted on",
           "reliability": reliability(p, a.any_sub.values.astype(float))}
    # the same station-days as seen from the 12Z cycle of the same date
    b = m[(m.hour == 12) & (m.key == key)].merge(a[["icao", "vday"]], on=["icao", "vday"])
    if len(b):
        pb = published(fm, b.raw.values, b.pk.values)
        res["same_days_from_12z"] = {k: v for k, v in score_rows(b, pb).items()
                                     if k in ("n", "brier", "brier_climo", "skill_pct", "ge50", "b25_50", "ge75")}
    return res


# ------------------------------------------------------------------ main ----
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rebuild", action="store_true", help="re-run the hindcast replay instead of using the cache")
    ap.add_argument("--parity", action="store_true",
                    help="check the replay against the engine's latest run and record it in daycal.json")
    ap.add_argument("--allow-missing-samples", action="store_true",
                    help="write daycal.json even when a cycle-hour sample is not on disk (those hours go unpublished)")
    ap.add_argument("--fetch-transfer", action="store_true",
                    help=f"download the {'/'.join(TRANSFER_CYCLES)}Z transfer samples (5 GB) and stop")
    args = ap.parse_args()
    dest = OUT / "daycal.json"

    if args.fetch_transfer:
        fetch_transfer()
        return
    if args.parity:
        res = parity(Ctx())
        print(json.dumps(res, indent=1))
        if dest.exists():
            dc = json.loads(dest.read_text())
            dc["parity"] = res
            dest.write_text(json.dumps(dc, separators=(",", ":")))
            print(f"recorded in {dest.name}")
        sys.exit(0 if res["ok"] else 1)

    replay = load_replay(args.rebuild)
    m = assemble(day_table(replay))
    # the leak-free split must hold row by row: every test-year day comes from
    # a cycle replayed with the pre-test climatology, every fit day from one
    # replayed as the engine runs live
    cyc_year = pd.to_datetime(m.cycle).dt.year
    assert (cyc_year[m.year == TEST_YEAR] == TEST_YEAR).all() and (cyc_year[m.year < TEST_YEAR] < TEST_YEAR).all()
    # the same test-year days as the first run scored them (climatology that
    # had seen the test year), only to say what the leak was worth
    leaky = assemble(day_table(replay[replay.cycle.dt.year == TEST_YEAR], col="p_all"))
    leaky = leaky[(leaky.hour == 12) & (leaky.year == TEST_YEAR)]
    chase = set(json.load(open(HERE.parent / "app" / "public" / "data" / "chase.json"))["airports"])
    fit12 = m[m.hour == 12]
    n_cycles = {y: int(fit12[fit12.year == y].cycle.nunique()) for y in (2023, 2024, 2025)}
    print(f"12Z station-days with truth: {len(fit12[fit12.key == 'd1']):,} (day 1); cycles by year {n_cycles}")
    sampled = [int(h) for h in TRANSFER_CYCLES]

    maps, checks = {}, {"residuals": {}, "climatology_leak": {}}
    reference = {}
    for key in ["d1"] + [daymap_key(2, c) for c in DAY2_COVER]:
        a = fit12[fit12.key == key]
        tr, te = a[a.year < TEST_YEAR], a[a.year == TEST_YEAR]
        fm = fit_map(tr.raw.values, tr.any_sub.values)
        p = published(fm, te.raw.values, te.pk.values)
        # the vectorised path must be the function the live build calls
        for raw, pk, pv in list(zip(te.raw.values, te.pk.values, p))[:: max(1, len(te) // 3000)]:
            live = day_chance(fm, {"noev": 1.0 - raw, "pk": int(pk)})
            assert abs(live - pv) < 0.00051, (key, raw, pk, pv, live)   # pv is rounded to 3 decimals
        h = score_rows(te, p)
        ra, rb = verdict(h)
        ship = bool(ra and rb is not False)
        y = te.any_sub.values.astype(float)
        # amendment 2: the same rule at the cycle hours that are really used
        hours = {f"{hh:02d}": hc for hh in sampled if hh in map_cycle_hours(key)
                 for hc in [hour_check(m, fm, key, hh)] if hc is not None}
        passed = {int(hh): hc["pass"] for hh, hc in hours.items()}
        if 12 in map_cycle_hours(key):
            passed[12] = ship                 # the 12Z holdout is the live sample at hour 12
        pub = publish_hours(key, passed) if ship else []
        # the receipts a build at cycle hour h prints: the sample nearest below it
        receipt_for = {str(hh): f"{max(s for s in passed if s <= hh):02d}" for hh in pub}
        # the samples that stand behind a published hour, and the season they must also hold in
        behind = [h if s == 12 else hours[f"{s:02d}"] for s in sorted(passed) if passed[s]] if pub else []
        hold_months = season_held(behind)
        lk = leaky[leaky.key == key]
        hl = score_rows(lk, published(fm, lk.raw.values, lk.pk.values))
        checks["climatology_leak"][key] = {
            "with_test_year_in_climatology": {k: hl[k] for k in ("skill_pct", "ge50", "ge75")}, "ships": passes(hl),
            "without": {k: h[k] for k in ("skill_pct", "ge50", "ge75")}}
        maps[key] = {"day": 1 if key == "d1" else 2, "cover_end_utc": None if key == "d1" else int(key[-2:]),
                     "ship": ship, "publish_hours": pub, "receipt_for_hour": receipt_for, "hold_months": hold_months,
                     "x": fm["x"], "y": fm["y"],
                     "n_fit": fm["n_fit"], "blocks": fm["blocks"], "min_block_n": fm["min_block_n"],
                     "holdout": {**h, "rule_a_skill_over_0": bool(ra), "rule_b_ge50_within_10": rb, "pass": ship,
                                 "sample": f"{h['cycles']} weekly 12Z cycles of {TEST_YEAR}, never fitted on",
                                 "top": fm["y"][-1], "raw_for_50": raw_for(fm, 0.5), "raw_050_maps_to": round(apply_daymap(fm, 0.5), 3)},
                     "hours": hours,
                     "areas": area_receipts(a, published(fm, a.raw.values, a.pk.values)),
                     "reliability": reliability(p, y), "raw_reliability": reliability(te.raw.values, y)}
        print(f"\n== {key}: fit n={fm['n_fit']:,} ({fm['blocks']} blocks, smallest {fm['min_block_n']}); "
              f"2025 n={h['n']:,} base {h['base']}")
        print(f"   Brier published {h['brier']} vs climatology {h['brier_climo']} -> skill {h['skill_pct']:+.1f}%   "
              f"(bare product {h['raw_brier']}, {h['raw_skill_pct']:+.1f}%)")
        print(f"   >=0.5 group: published n={h['ge50']['n']} shown {h['ge50']['shown']} observed {h['ge50']['obs']}   "
              f"(bare product n={h['raw_ge50']['n']} shown {h['raw_ge50']['shown']} observed {h['raw_ge50']['obs']})")
        print(f"   >=0.75 group: {h['ge75']}")
        print(f"   rule (a) skill>0: {ra}   rule (b) >=0.5 within 10 pts: {rb}   -> {'SHIP' if ship else 'HOLD'}")
        print(f"   with {TEST_YEAR} inside the climatology (the first run): skill {hl['skill_pct']:+.1f}%, "
              f">=0.5 {hl['ge50']} -> {'SHIP' if passes(hl) else 'HOLD'}")
        print(f"   Nov-Feb >=0.5: {h['nov_feb']['ge50']}")
        for hh, hc in hours.items():
            print(f"   {hh}Z cycles ({hc['cycles']}): skill {hc['skill_pct']:+.1f}%  >=0.5 {hc['ge50']}  "
                  f">=0.75 {hc['ge75']}  Nov-Feb >=0.5 {hc['nov_feb']['ge50']}  "
                  f"-> {'pass' if hc['pass'] else 'FAIL'}")
        print(f"   published from cycle hours: {pub or 'none'}"
              + (f"   NOT for target days in months {hold_months}" if hold_months else ""))
        for r in maps[key]["reliability"]:
            print(f"     {r['lo']:.2f}-{r['hi']:.2f}  n={r['n']:6d}  shown {r['shown']:.3f}  observed {r['obs']:.3f}")
        for ar in maps[key]["areas"]:
            for label in ar["judged"]:
                g = ar["samples"][label]["ge50"]
                print(f"     area {ar['name']:15s} {label:8s} >=0.5 n={g['n']:4d} ({g['dates']:2d} dates) shown {g['shown']} "
                      f"observed {g['obs']}" + ("" if label != ar["show"] else
                                                f"  -> {'ok' if ar['ok'] else 'NOT CALIBRATED HERE'}"))
        # reproduction of the review's figure only: a bare isotonic map, no
        # minimum block, no floor. Never shipped, never used to choose.
        bare = fit_map(tr.raw.values, tr.any_sub.values, min_block=1, ends=True)
        pb = np.interp(te.raw.values, bare["x"], bare["y"])
        hb = score_rows(te, pb)
        reference[key] = {"brier": hb["brier"], "brier_climo": hb["brier_climo"], "skill_pct": hb["skill_pct"],
                          "ge50": hb["ge50"], "blocks": bare["blocks"], "top": bare["y"][-1]}
        print(f"   (review's bare isotonic, for reproduction: Brier {hb['brier']} skill {hb['skill_pct']:+.1f}% "
              f">=0.5 n={hb['ge50']['n']} shown {hb['ge50']['shown']} observed {hb['ge50']['obs']})")
        if key in ("d1", "d2h09", "d2h12"):
            checks["residuals"][key] = residuals(te, p, chase)

    # Central Valley, day 1, year by year. The 2025 holdout has too few
    # station-days there at >= 0.5 for rule (b) to say anything, so the two
    # fitted years sit beside it: in-sample for the map, but the map is pooled
    # over 1,200 stations and a regional bias shows either way.
    a = fit12[(fit12.key == "d1") & fit12.icao.isin(CENTRAL_VALLEY)]
    pv, yv = published(maps["d1"], a.raw.values, a.pk.values), a.any_sub.values.astype(float)
    checks["central_valley_day1_by_year"] = {
        label: {"n": int(s.sum()), "ge50": group(pv[s], yv[s], 0.5, 1.01), "b25_50": group(pv[s], yv[s], 0.25, 0.5),
                "eh": round(float(a.eh.values[s].mean()), 3), "obs_hours": round(float(a.hrs.values[s].mean()), 3)}
        for label, s in [(str(y), (a.year == y).values) for y in (2023, 2024, 2025)] + [("all", np.ones(len(a), bool))]}
    have = sorted({int(hh) for mp_ in maps.values() for hh in mp_["hours"]})
    missing = [hh for hh in sampled if hh not in have]
    if missing:
        # Without a sample its cycle hours count as unchecked and are not
        # published. Run on a machine without the 5 GB of transfer bulletins,
        # this would rewrite the committed file so that the 09Z bake publishes
        # no day-level chance at all. Refuse unless that is asked for.
        msg = (f"no {', '.join(f'{hh:02d}Z' for hh in missing)} sample on disk (fit_daycal.py --fetch-transfer): "
               f"cycle hours that need it would be left unpublished")
        if not args.allow_missing_samples:
            sys.exit(f"{msg}. daycal.json was NOT rewritten (pass --allow-missing-samples to write it anyway).")
        print(f"\n::warning:: {msg}")

    for key, res in checks["residuals"].items():
        print(f"\nresiduals, {key}, 2025 holdout (>= 0.5 group; flagged when n >= 50 and off by more than 15 points):")
        for name, r in res.items():
            g = r["ge50"]
            print(f"   {name:30s} n={r['n']:6d}  >=0.5: n={g['n']:4d} shown {g['shown']} observed {g['obs']}"
                  f"{'   <-- FLAG' if r['off_by_over_15'] else ''}")
    print("\nCentral Valley, day 1, by year (2023-24 in-sample):")
    for label, r in checks["central_valley_day1_by_year"].items():
        print(f"   {label}: n={r['n']}  >=0.5: {r['ge50']}  expected hours {r['eh']} vs observed {r['obs_hours']}")
    old = json.loads(dest.read_text()) if dest.exists() else {}
    par = old.get("parity") or {"ok": None, "note": "not run — python forecast/engine.py, then fit_daycal.py --parity"}
    if par.get("fingerprint") and par["fingerprint"] != fingerprint():
        par = dict(par, stale=True)
    out = {
        # 2: maps carry publish_hours, receipt_for_hour, hours and areas, and
        # build_deploy.load_daycal requires them
        "version": 2,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "fit": f"2023-2024 weekly 12Z hindcast ({n_cycles[2023] + n_cycles[2024]} cycles)",
        "test": f"{TEST_YEAR} weekly 12Z hindcast ({n_cycles[TEST_YEAR]} cycles), never fitted on",
        "test_climatology": f"test-year forecasts replayed with a climatology from truth before {TEST_YEAR}-01-01",
        "statistic": "1 - prod(1 - p_hour)^step over the day's forecast hours (day 1: all of them; day 2: hours "
                     "up to cover_end_utc), exactly as build_deploy.cal_day_stats builds it",
        "target": "at least one hourly below-CAT-I flag in the UTC day (>= 18 hourly obs)",
        "published": "max(map(statistic), peak hourly chance), 3 decimals",
        "method": f"isotonic regression (pool-adjacent-violators), blocks under {MIN_BLOCK} station-days pooled "
                  f"into the block below, one breakpoint per block, linear interpolation between",
        "rule": RULE,
        "rule_by_cycle_hour": RULE_HOURS,
        "rule_by_season": RULE_SEASON,
        "rule_by_area": RULE_AREAS,
        "sampled_cycle_hours": sorted(set(sampled) | {12}),
        "maps": maps,
        "reference_bare_isotonic": reference,
        "checks": checks,
        "parity": par,
    }
    dest.write_text(json.dumps(out, separators=(",", ":")))
    print(f"\nwrote {dest.name} ({dest.stat().st_size // 1024} KB): "
          + ", ".join(f"{k} {'SHIP' if v['ship'] else 'HOLD'} from {v['publish_hours'] or 'no cycle hour'}"
                      for k, v in maps.items()))


def raw_for(m: dict, target: float) -> float | None:
    """Smallest bare product the map turns into `target` (None if it never gets there)."""
    lo, hi = 0.0, 1.0
    if apply_daymap(m, hi) < target:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if apply_daymap(m, mid) < target:
            lo = mid
        else:
            hi = mid
    return round(hi, 3)


if __name__ == "__main__":
    main()
