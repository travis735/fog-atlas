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
  2. Aggregates each airport-cycle with build_deploy.day_stats — the same
     function the live build calls — into the any-hour product of each date
     the cycle forecasts (its own date under way, the next, the one after).
  3. Joins truth: did the UTC date contain at least one hourly below-CAT-I
     flag (>= 18 hourly obs required; for a date under way, after the cycle).
  4. Fits one monotone map per statistic on 2023-2024, tests it on real 2025
     cycles at every bucket of cycle hours, fits and tests each area's own
     map, and writes forecast/out/daycal.json: the breakpoints
     build_deploy.py reads in CI with the standard library, plus the receipts.

What follows is the record in the order it happened: version 1's
pre-registration and result, version 2's amendments (day counted from the UTC
date, maps named by planner day: d1, d2hNN), then VERSION 3, which is what
the code below does. build_deploy.cal_day_stats and DAY2_COVER in the older
text are now day_stats and the slot table.

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

=============================================================================
VERSION 3 — every build hour, day 2, regions.
PRE-REGISTERED on 2026-11-25 (03:30Z), after the 03Z/06Z/15Z/18Z bulletins
below were downloaded and BEFORE any of them, any own-date target, any
natural-coverage day 2 or any regional map had been scored.

Why: the planner is about to be rebuilt on every hourly forecast tick (a tick
at h:25Z runs cycle h-1), and the pilot reads it on a US evening (22Z-08Z).
Version 2 had a day-level chance only from 09Z-21Z cycles, day 2 on
climatology, and every GO in the West flagged "ran about 30 points high" with
the number left alone.

THE SERVICE DAY (build_deploy.service_day). Days are counted from the UTC
  date of (build time - 10 h); day k = service day + k. Day 1 flips at 10:00Z,
  as the daily 10:25Z bake always did. So what a cycle forecasts for a day
  depends on two things only: the cycle hour h, and j = target date - cycle
  date in days. Every statistic, map and check below is keyed by (h, j); the
  day number only picks which date is asked for.
      cycles 09Z-23Z of D   day 1 = D+1 (j=1)   day 2 = D+2 (j=2)
      cycles 00Z-08Z of D+1 day 1 = D+1 (j=0)   day 2 = D+2 (j=1)

STATISTIC, every slot: 1 - prod(1 - p_hour)^step over the forecast hours of
  the target date that the cycle covers (leads 1-25 hourly, step 1; NBS
  three-hourly valid times beyond, step 3), exactly as build_deploy.day_stats
  builds it. Hours before the cycle are not forecast and are not in it.
TRUTH  j >= 1: at least one hourly below-CAT-I flag in the UTC date (>= 18
         hourly obs), as before — also when the cycle covers only part of the
         date (the claim on screen is about the whole day).
       j = 0 (the day under way): at least one hourly below-CAT-I flag at an
         hour AFTER the cycle hour, through 23Z of that date, with at least
         three quarters of those hours observed. Never an hour already past.
BASELINE for rule (a): j >= 1, dayscale.json climo_day as before. j = 0: the
  per-airport monthly frequency of the same event (an hour below CAT I
  between that cycle hour and 23Z) over every truth year except the row's own.

SLOTS — which map a (cycle hour, j) uses and where it was fitted. Maps are
  fitted on 2023-24 only, same method as version 2 (isotonic, blocks under
  200 station-days pooled downward, one breakpoint per block, published as
  max(map, peak hourly chance)).
    ahead       j=1, h 09-23  all of the date            map d1   (the committed
                              map, 12Z cycles of 2023-24; not refitted)
    under way   j=0, h 00-08  hours after the cycle      map d1u  (new: 104
                              weekly 03Z cycles of 2023-24, their own date)
    evening     j=1, h 00-08  all of the date, 16-47 h   map d2f  (new: the same
                              ahead                      03Z cycles, the next date)
    after next  j=2, h 15-23  the date through 15Z, 18Z  map d2f  (no fit sample
                              or 21Z, 3-hourly only      of its own)
    partial     j=2, h 09-11  the date through 09Z       map d2h09 (committed)
                j=2, h 12-14  the date through 12Z       map d2h12 (committed)
    anything else (a cycle from an earlier date than the table expects, a day
    the 48 h do not reach): no map, no day-level chance.
  One fallback, named now: the h00 under-way bucket tries d1u first and, only
  if d1u fails there, d1 (a 00Z cycle's own date is one hour short of the full
  day a 23Z cycle sees). Both results are recorded; two candidates is two
  looks, said so in the receipt. No other slot has a second candidate.

BUCKETS — eight, three cycle hours each, named by their first hour, which is
  the representative hour: h00 (00-02), h03, h06, h09, h12, h15, h18, h21
  (21-23). No cycle hour is more than 2 h from a checked one, and all three
  hours of a bucket cover day 2 through the same UTC hour.
  Check sample of a bucket: real cycles of 2025 at its representative hour on
  the 30 check dates (TRANSFER_DATES) — except h12, which keeps the 52 weekly
  12Z cycles of 2025. On disk before this: 00Z, 09Z, 12Z, 21Z. Fetched for
  this: 03Z, 06Z, 15Z, 18Z (2025) and 03Z on the 104 weekly dates of 2023-24
  (12.2 GB, 344 s). Every 2025 cycle is replayed with the pre-2025 climatology.

MINIMUM HOURS: a day's statistic is published only when at most 3
  hour-equivalents are missing from what a complete NBH+NBS issuance of that
  cycle covers (build_deploy.expected_used; for the ahead slot at 09Z-21Z
  that is the old "20 of 23"). Fits and checks use every row that has the
  statistic, as version 2 did; rows short of hours are counted in the receipt.

SHIP RULE, per (slot, bucket) — the rule already in force, unchanged:
  on the bucket's 2025 sample the published chance (a) has Brier skill > 0
  against the baseline on the same rows, and (b) its >= 0.5 group, when it
  has n >= 50, verifies within 10 points. A bucket whose map passes publishes
  from its three cycle hours; one that fails publishes no day-level chance
  (p is null and the receipt says why). The season rule stands too: where a
  passing sample's Nov-Feb >= 0.5 group (n >= 50) misses by more than 10
  points, the slot is not published for November-February target days.
  This replaces version 2's "nearest sampled hour either side" rule: every
  bucket now has its own sample.

REGIONS. The ten areas are version 2's (AREAS), unchanged. 2025 has been
  looked at by region, so it is not a clean holdout for a regional map:
  LEAVE ONE YEAR OUT. For each family (d1 at 12Z, d1u and d2f at 03Z) and
    each area: fit the area's own map on the area's rows of two of the years
    2023, 2024, 2025 (same method, same 200 station-day minimum block, so a
    thin area cannot print 100%), publish it on the third, rotate, pool the
    three out-of-fold sets. The comparison is the pooled map put through the
    same three folds, on the same rows.
  FORM. Tried in this order; the first that passes is used:
    floored    max(area map, peak hourly chance), the pooled map's own form;
    unfloored  the area map alone. The floor assumes the peak hourly chance
               is itself reliable there; item HOURLY below measures that. An
               unfloored area can show a day chance below the peak hourly
               chance, so the payload marks it and the screen must not print
               the two as if they agreed.
  REGIONAL RULE on the pooled out-of-fold rows — the ship rule plus three
    conditions that only withhold:
    (a) Brier skill > 0 against the baseline;
    (b) the >= 0.5 group within 10 points when it has n >= 50;
    (c) Brier lower than the pooled map's on the same rows (else no reason
        to replace it);
    (d) the 0.25-0.5 group within 10 points when it has n >= 50 (a map that
        tops out under 0.5 publishes nothing else above SCRAP);
    (e) the >= 0.5 group is empty or has n >= 50: an area map never
        publishes a GO-level value that nothing could judge.
    The season rule is read off the same rows.
  Asymmetry, stated: calibration.json was fitted on 2023-24, so 2023-24
    products are in-sample for the hourly model and 2025 products are not;
    2025 rows also use the pre-2025 climatology. Results are reported per
    held-out year so that shows. 2025 at 03Z is 30 cycle dates, not 52.
  WHAT SHIPS: the area map fitted on 2023-24 (the 2025 fold's map), so the
    2025 cycle-hour samples below are out of sample for it. The other two
    folds are evidence about the procedure, not about that map.
  BY BUCKET: an area map is used in a (slot, bucket) only when the slot's
    pooled map is published there AND the area map, in the form chosen above,
    passes (a), (b) and (d) on the area's own rows of that bucket's 2025
    sample. Where it does not, the area keeps the pooled number there with
    version 2's "not calibrated for these fields" flag. Thin areas will be
    patchy by bucket; that is the rule's answer, not a defect to smooth.

HOURLY (reported, gates nothing; the hourly calibration is not refitted):
  by area on the 2025 rows of each family's fit hour — summed expected hours
  against summed observed hours over the same days, and the peak hour's own
  reliability (did the hour shown as the day's peak fall below CAT I) at
  25-49%, 50% and over, 75% and over. An area's expected hours "run high"
  ("run low") when they exceed (fall short of) the observed hours by more
  than 25%, judged only with at least 100 observed fog hours in the sample.

Known when this was written (version 2's results, all at 12Z-based or
  "date after the cycle" targets): d1 passes at 09Z, 12Z and 21Z; d1 scored on
  00Z cycles against the NEXT date — the evening slot here — failed (0.62
  shown / 0.46 observed), which is why that slot gets its own map; d2h09 on
  real 09Z cycles failed (0.59 / 0.46); d2h12 passed at 12Z and missed in
  Nov-Feb (0.61 / 0.46); the pooled d1 by area (Central Valley 0.65 / 0.35,
  West Coast 0.65 / 0.54, Northeast 0.64 / 0.51, Great Lakes 0.62 / 0.78,
  Southeast 0.62 / 0.84). Never scored before this: any own-date target, any
  03Z/06Z/15Z/18Z cycle, d1u, d2f, day 2 at its natural coverage past 12Z,
  any area map.

RESULT of that one run, 2026-11-25 (every number is in daycal.json; nothing
  above was changed after it, and version 2's maps and receipts for d1,
  d2h09 and d2h12 came out identical to the committed ones):
  DAY 1 has a day-level chance from every cycle hour.
    ahead (d1): 09Z +9.7% skill, >= 0.5 shown 0.62 / observed 0.56 (n 252);
      12Z +10.3%, 0.64 / 0.58 (420); 15Z +11.9%, 0.63 / 0.59 (284); 18Z
      +12.9%, 0.64 / 0.58 (353); 21Z +14.9%, 0.65 / 0.61 (375). All pass.
    under way: at 00Z d1u FAILED (+17.1%, 0.69 / 0.57, n 559: 11.8 points)
      and the named fallback d1 passed (+17.3%, 0.66 / 0.57, n 535), so
      00Z-02Z cycles use d1. d1u passes at 03Z (+19.2%, 0.70 / 0.62, n 520)
      and 06Z (+21.5%, 0.72 / 0.65, n 578).
    Still true, and not gated: values at 0.75 and over ran high in every
    sample — 0.78 / 0.61 at 00Z (n 177), 0.86 / 0.68 at 03Z (176), 0.87 /
    0.77 at 06Z (228), 0.78 / 0.65-0.73 from daytime cycles.
  DAY 2 has no day-level chance from any cycle hour in November-February.
    evening (d2f, what a US evening build sees): skill +7.3%, +7.9%, +9.0% at
      00Z, 03Z, 06Z, but >= 0.5 shown 0.60 / observed 0.45, 0.46, 0.49
      (n 176, 184, 213). FAIL at all three, by 15.5, 13.7 and 10.5 points.
      The 0.25-0.5 group was within 4 points each time; the rule judges the
      map, not a part of it.
    after next (d2f): 15Z FAIL (0.59 / 0.48, n 99), 21Z FAIL (0.59 / 0.48,
      n 126), 18Z passes (0.59 / 0.50, n 123) and misses in Nov-Feb
      (0.60 / 0.47, n 96): published from 18Z-20Z cycles, March-October only.
    partial: d2h09 FAIL on real 09Z cycles (0.59 / 0.46, as known); d2h12
      passes at 12Z, March-October only (as known).
    So day 2 carries the hourly forecast's expected hours, peak hour and
    window over the hours it covers, and no chance.
  AREAS (leave one year out, pooled out-of-fold rows; area map vs the pooled
    map through the same folds):
    Central Valley  floored fails (d1: its >= 0.5 group is 11 rows nothing
      can judge; d1u: 0.62 shown / 0.39 observed, n 56). Unfloored PASSES:
      skill +8.5% vs +1.2% (d1), +10.7% vs -1.0% (d1u). Per held-out year,
      d1: +10.5 / +2.5 / +12.6% (2023 / 2024 / 2025; pooled +5.8 / +7.4 /
      -8.4%). The map fitted on 2023-24 is two or three blocks and tops out
      at 0.233 (d1) and 0.224 (d1u): a pooled 0.55 becomes 0.23, and no
      Central Valley field is ever shown above 23%. Out of fold its values
      at 0.10-0.25 verified at 0.22 (shown 0.19, n 386; d1) and 0.23 (shown
      0.17, n 284; d1u). What it cannot do: days whose peak hourly chance
      was 50% or more fogged 36% (n 11, 12Z cycles) and 39% (n 56, 03Z) of
      the time, and with a 200 station-day block the map cannot tell those
      from the rest of its top. Used in every day-1 bucket (it passed all
      eight on 2025 rows).
    West Coast  d1: the area map fails rule (c) only — skill +10.1% against
      the pooled map's +10.5% — though its >= 0.5 group ran 6 points high
      (0.55 / 0.49, n 258) where the pooled map ran 11. By held-out year the
      pooled map's >= 0.5 group verified 0.65, 0.43, 0.60: the miss is one
      year. d1u: the floored area map passes leave-one-year-out (+15.0% vs
      +13.3%; 0.60 / 0.56, n 359) and then FAILS on 2025 at both of its
      buckets (03Z 0.61 / 0.44, n 117; 06Z 0.62 / 0.48, n 128). West Coast
      fields keep the pooled map and its flag in every bucket: 11 points
      high from daytime cycles (2023-25), 27 points high from evening ones
      (0.72 / 0.45, n 111, 2025).
    Mountain West  d1 unfloored passes (top 0.386; floored fails only on a
      >= 0.5 group of 4 rows); d1u fails (c). The same blind spot as the
      Central Valley's, the other way: days whose peak hour showed 25-49%
      fogged 57% of the time there (n 42).
    Canada  d1 floored passes (top 0.588); d1u fails (c).
    Plains, Great Lakes, Southeast, Northeast  every area map fails (c): it
      does not beat the pooled map. Alaska, Hawaii  no skill to speak of.
    The pooled map's own area flags are version 2's for d1, and for d1u:
    Central Valley, West Coast high; Great Lakes, Southeast low.
  HOURLY (2025): Central Valley expected hours 159 against 151 observed from
    12Z cycles (x1.06) but 294 against 145 from 03Z cycles (x2.02, "runs
    high"); there the hour shown as the peak at 50% or more fell below CAT I
    22% of the time (n 32). Mountain West, Plains x0.75 and Alaska x0.44 run
    low from 12Z cycles.
  Clarification, not a change: "the old 20 of 23" in MINIMUM HOURS is exact at
    the buckets' own hours (09Z, 12Z, 15Z, 18Z, 21Z); a whole issuance holds
    22 to 24 hour-equivalents of tomorrow depending on the cycle hour. No row
    in any sample was short of hours.

AMENDED on 2026-11-25 after an independent review of that run (the reviewer
replayed every bulletin with their own code and got the same numbers; the
objections are to what the rule produced). Every result above had been seen,
so none of this is a clean pre-registration. As with version 2's amendments,
each one only WITHHOLDS an area map or adds a record to print; no pooled map
is refitted, no slot's ship verdict changes, and nothing is loosened.
  1. Regional rule (f): an unfloored area map — one that can never show more
     than its own top — replaces the pooled map only where the pooled map's
     >= 0.5 group in that area was judged (n >= 50) and ran high by more than
     10 points. Why: Mountain West got an unfloored map topping out at 0.39 on
     a Brier margin of 0.00005 (date bootstrap: better in 65% of resamples;
     it lost the 2024 fold) while the pooled map's >= 0.5 group there had
     verified at 0.62 shown / 0.61 observed (n 44, 24 dates). From 03Z-08Z
     cycles the same fields kept the pooled map, so the number stepped by up
     to 51 points when the build crossed 03Z. Result: Mountain West keeps the
     pooled map in every bucket, as it did before version 3. The Central
     Valley is unchanged (its pooled >= 0.5 group ran 30 points high, n 63),
     and so is Canada (its map is floored).
  2. A day under way is also scored from the build on. The check counts fog at
     any hour after the cycle hour; the build of cycle h is on screen at
     h+1:25, when the hour filed as h+1 is already over. The same published
     chance against fog after that hour: 00Z (map d1) 0.66 shown / 0.57
     observed, 03Z 0.70 / 0.60 (n 519), 06Z 0.72 / 0.62 (n 578) — 9, 10 and
     11 points high where the check said 9, 9 and 7. The slot verdicts stand
     (the rule was written on the first definition; changing it now would be
     choosing after the result, and it is the owner's call whether an evening
     build should then publish). The record goes into each under-way receipt
     as `after_build`, and the screen prints the larger of the two misses.
  3. Every area's record is also taken on each bucket's OWN 2025 sample
     (slots[..].areas[..].own): the published chance by band, the expected
     against the observed hours, and what days did by their peak hourly
     chance. Version 3 printed the record of the map's fit hour from every
     bucket, so the 00Z-02Z builds, which use map d1 by the named fallback,
     showed West Coast fields "about 11 points high" (12Z cycles, the next
     date) while on their own sample those fields ran 24 points high at 50%
     and over (0.68 / 0.44, n 128) and 30 at 75% and over (0.79 / 0.49,
     n 57), and Central Valley expected hours read x1.06 where 00Z cycles
     ran x1.87. build_deploy.day_receipt prints the larger miss of the fit
     hour's record and the bucket's own.
  Not changed, and the owner's to decide: values at 75% and over still run 18
  points high from 00Z-05Z cycles; the Central Valley map tops out at 22-23%,
  under the MARGINAL bar, while the days it prints that on fogged about one
  time in three (values at 0.20 and over: 0.22 shown / 0.33 observed, n 172);
  the h18 day-2 slot publishes March-October on 27 airport-days at 50%+.

Outputs: forecast/out/daycal.json (committed) and
forecast/out/daycal_replay.parquet (gitignored cache of step 1; delete it or
pass --rebuild after calibration.json, climo.parquet, truth.parquet or the
hindcast change).

    .venv/bin/python forecast/fit_daycal.py                   # fit + holdout + checks
    .venv/bin/python forecast/fit_daycal.py --parity          # after `python forecast/engine.py`
    .venv/bin/python forecast/fit_daycal.py --fetch-transfer  # every bulletin beyond the weekly 12Z set, 18 GB
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
# The 03Z cycles of 2023-24 the evening maps (d1u, d2f) are fitted on. Their
# own folder for the same reason, and so a check sample can never be mistaken
# for a fit sample by where it sits.
FIT03 = HIND / "fit03z"
CACHE = OUT / "daycal_replay.parquet"

sys.path.insert(0, str(HERE))
from parse_nbm import parse_collective  # noqa: E402
from engine import clim_logit, load_climo, score  # noqa: E402  (the engine's own scoring primitives)
from build_deploy import (BAND_MAX_GAP, BAND_MIN_N, BUCKETS, MAX_MISSING_HOURS, SERVICE_DAY_OFFSET_H,  # noqa: E402
                          SLOT_KEYS, SLOT_MAPS, apply_daymap, area_of, covered_hours, day_chance, day_stats,
                          expected_used, service_day, slot_kind)

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
    for nbh in sorted(HIND.glob("nbh_*_12z.txt")) + sorted(TRANSFER.glob("nbh_*z.txt")) + sorted(FIT03.glob("nbh_*z.txt")):
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
def build_time(cyc: datetime) -> datetime:
    """When build_deploy runs on this cycle: the forecast job at h:25 picks up
    cycle h-1 (run logs, Nov 2026)."""
    return cyc + timedelta(hours=1, minutes=25)


# The cycle hours behind a check or a fit, and the reaches (j = target date -
# cycle date) the planner asks of each: an evening cycle's own date and the
# next, a daytime cycle's next date and the one after.
EVENING_HOURS = (0, 3, 6)
DAYTIME_HOURS = (9, 12, 15, 18, 21)


def day_table(replay: pd.DataFrame, col: str = "p") -> pd.DataFrame:
    """One row per airport x cycle x reach, built by build_deploy.day_stats.

    key "j0"/"j1"/"j2" is the statistic exactly as the live build computes it
    from that cycle for the date j days after the cycle's own. 12Z cycles
    also get "j2t09": the date after next cut off at 09Z, which is how a 12Z
    cycle stands in for a 09Z one when d2h09 is fitted. col picks the replay
    column ("p_all" only to measure what the test-year climatology leak was
    worth). short = the row is missing more hours than the build publishes."""
    icao, cyc_ns = replay.icao.values, replay.cycle.values
    fhr, p = replay.fhr.values.tolist(), replay[col].values.tolist()
    edge = np.flatnonzero((icao[1:] != icao[:-1]) | (cyc_ns[1:] != cyc_ns[:-1])) + 1
    starts, ends = np.r_[0, edge], np.r_[edge, len(replay)]
    cycles = pd.to_datetime(replay.cycle.values[starts]).to_pydatetime()
    rows = []
    need: dict[tuple, int] = {}
    for a, b, cyc in zip(starts.tolist(), ends.tolist(), cycles):
        js = (0, 1) if cyc.hour in EVENING_HOURS else (1, 2)
        targets = {cyc.date() + timedelta(days=j): j for j in js}
        for d, s in day_stats(fhr[a:b], p[a:b], cyc, targets).items():
            if s["pk"] is None:
                continue
            if (cyc, d) not in need:
                need[(cyc, d)] = expected_used(cyc, d) - MAX_MISSING_HOURS
            rows.append((icao[a], cyc, f"j{targets[d]}", d, 1.0 - s["noev"], s["pk"], s["used"], s["eh"],
                         s["used"] < need[(cyc, d)]))
        if cyc.hour == 12:
            d = cyc.date() + timedelta(days=2)
            s = day_stats(fhr[a:b], p[a:b], cyc, {d: 2}, through={d: 9}).get(d)
            if s and s["pk"] is not None:
                rows.append((icao[a], cyc, "j2t09", d, 1.0 - s["noev"], s["pk"], s["used"], s["eh"], s["used"] < 12))
    df = pd.DataFrame(rows, columns=["icao", "cycle", "key", "vday", "raw", "pk", "used", "eh", "short"])
    df["hour"] = pd.to_datetime(df.cycle).dt.hour
    return df


def load_truth() -> pd.DataFrame:
    """icao, d, any_sub, hrs, nobs per UTC day: the hourly truth of
    fit_calibration.py (obs rounded to the nearest hour), rolled up by day.
    any_h/hrs_h/nobs_h are the same three over the hours AFTER hour h only,
    for a day seen under way from an h-Z cycle."""
    con = duckdb.connect()
    win = ", ".join(f"(max(sub) FILTER (WHERE hour(hr_ts) > {h}))::INT AS any_{h}, "
                    f"coalesce(sum(sub) FILTER (WHERE hour(hr_ts) > {h}), 0)::INT AS hrs_{h}, "
                    f"(count(*) FILTER (WHERE hour(hr_ts) > {h}))::INT AS nobs_{h}, "
                    # the same day counted from the BUILD on (version 3, amendment 2): the hour filed
                    # as h+1 is the METARs of h:30 to h+1:29, and the build of cycle h is on screen at h+1:25
                    f"(max(sub) FILTER (WHERE hour(hr_ts) > {h + 1}))::INT AS anyb_{h}, "
                    f"(count(*) FILTER (WHERE hour(hr_ts) > {h + 1}))::INT AS nobsb_{h}" for h in EVENING_HOURS)
    df = con.execute(f"""
        WITH th AS (
          SELECT icao, date_trunc('hour', ts + INTERVAL 30 MINUTE) AS hr_ts, max(sub) AS sub
          FROM '{OUT / "truth.parquet"}'
          WHERE ts >= '2022-12-31' AND ts < '2026-01-10' GROUP BY 1, 2)
        SELECT icao, hr_ts::DATE AS d, max(sub)::INT AS any_sub, sum(sub)::INT AS hrs, count(*)::INT AS nobs, {win}
        FROM th GROUP BY 1, 2
    """).fetchdf()
    df["d"] = pd.to_datetime(df.d).dt.date
    return df


def window_min_obs(h: int) -> int:
    """Hourly obs a day under way needs after an h-Z cycle: three quarters of the hours left (18 of 24 for a whole day)."""
    return math.ceil(0.75 * (23 - h))


def window_climo() -> dict:
    """{(icao, month, h): {year: (days, fog days)}} for the event a day under
    way is judged on: an hour below CAT I after hour h, through 23Z. Every
    truth year, so a row can be given the frequency over all years but its
    own (the day climatology in dayscale.json is whole-day only, and held
    against a part-day event it would be an easy baseline to beat)."""
    con = duckdb.connect()
    cols = ", ".join(f"count(*) FILTER (WHERE n_{h} >= {window_min_obs(h)}) AS days_{h}, "
                     f"coalesce(sum(a_{h}) FILTER (WHERE n_{h} >= {window_min_obs(h)}), 0) AS fog_{h}"
                     for h in EVENING_HOURS)
    per = ", ".join(f"max(sub) FILTER (WHERE hour(hr_ts) > {h}) AS a_{h}, "
                    f"count(*) FILTER (WHERE hour(hr_ts) > {h}) AS n_{h}" for h in EVENING_HOURS)
    rows = con.execute(f"""
        WITH th AS (
          SELECT icao, date_trunc('hour', ts + INTERVAL 30 MINUTE) AS hr_ts, max(sub) AS sub
          FROM '{OUT / "truth.parquet"}' GROUP BY 1, 2),
        dy AS (SELECT icao, hr_ts::DATE AS d, {per} FROM th GROUP BY 1, 2)
        SELECT icao, year(d) AS y, month(d) AS mon, {cols} FROM dy GROUP BY 1, 2, 3
    """).fetchall()
    out: dict[tuple, dict] = {}
    for r in rows:
        for k, h in enumerate(EVENING_HOURS):
            out.setdefault((r[0], int(r[2]), h), {})[int(r[1])] = (int(r[3 + 2 * k]), int(r[4 + 2 * k]))
    return out


def assemble(days: pd.DataFrame, truth: pd.DataFrame, wclim: dict) -> pd.DataFrame:
    """Day table + truth + the climatology baseline, one row per airport x cycle x reach.

    j >= 1: the whole UTC date (>= 18 hourly obs) against dayscale.json's day
    climatology, as version 2. j = 0: the hours after the cycle against the
    frequency of that same event in every year but the row's own."""
    m = days.merge(truth, left_on=["icao", "vday"], right_on=["icao", "d"], how="inner").drop(columns="d")
    vd = pd.to_datetime(m.vday.astype(str))
    m["year"], m["mon"] = vd.dt.year, vd.dt.month
    cl = json.load(open(OUT / "dayscale.json"))["climo_day"]
    pc = [cl.get(i, {}).get(str(mo)) for i, mo in zip(m.icao, m.mon)]
    m["p_clim"] = [c[0] if c else np.nan for c in pc]
    # climatological fog hours per fog day: how long fog lasts here once it is in
    m["hpd"] = [c[1] / c[0] if c and c[0] > 0.02 else np.nan for c in pc]
    under = (m.key == "j0").values
    keep = (m.nobs >= 18).values & ~under
    m["any_sub"], m["hrs"] = m.any_sub.astype(float), m.hrs.astype(float)
    # ab_any: for a day under way, was there an hour below CAT I after the
    # BUILD (hours after cycle hour + 1); NaN for every other row and where
    # under three quarters of those hours were observed. Reported, never fitted on.
    m["ab_any"] = np.nan
    for h in EVENING_HOURS:
        s = under & (m.hour == h).values
        if not s.any():
            continue
        keep |= s & (m[f"nobs_{h}"].astype(float) >= window_min_obs(h)).values
        m.loc[s, "any_sub"] = m.loc[s, f"any_{h}"].astype(float).values
        m.loc[s, "hrs"] = m.loc[s, f"hrs_{h}"].astype(float).values
        sb = s & (m[f"nobsb_{h}"].astype(float) >= window_min_obs(h + 1)).values
        m.loc[sb, "ab_any"] = m.loc[sb, f"anyb_{h}"].astype(float).values
        base = []
        for i, mo, y in zip(m.icao.values[s], m.mon.values[s], m.year.values[s]):
            yrs = wclim.get((i, int(mo), h), {})
            nd = sum(v[0] for k, v in yrs.items() if k != y)
            base.append(sum(v[1] for k, v in yrs.items() if k != y) / nd if nd >= 30 else np.nan)
        m.loc[s, "p_clim"] = base
    m = m[keep].drop(columns=[c for c in m.columns if c[:4] in ("any_", "hrs_") and c != "any_sub" or c[:5] == "nobs_"
                              or c[:5] == "anyb_" or c[:6] == "nobsb_"]).copy()
    m["any_sub"], m["hrs"] = m.any_sub.astype(int), m.hrs.astype(int)
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


def published(m: dict, raw: np.ndarray, pk: np.ndarray, floor: bool = True) -> np.ndarray:
    """build_deploy.day_chance + cal_tier's rounding, vectorised (asserted equal in main)."""
    v = np.interp(raw, m["x"], m["y"])
    return np.round(np.maximum(v, pk / 100.0) if floor else v, 3)


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
    if not has.any():
        b_p = b_c = b_raw = float("nan")
    else:
        b_p, b_c, b_raw = brier(p[has], y[has]), brier(df.p_clim.values[has], y[has]), brier(raw[has], y[has])

    def skill(b: float) -> float | None:
        # an area with no fog day and a climatology of zero has no skill to speak of (Hawaii)
        return round(100 * (1 - b / b_c), 1) if b_c > 0 else None
    return {"n": int(len(df)), "n_clim": int(has.sum()), "cycles": int(len(np.unique(cyc))),
            "short": int(df.short.sum()),
            "base": round(float(y.mean()), 4) if len(df) else None,
            "brier": round(b_p, 5) if has.any() else None, "brier_climo": round(b_c, 5) if has.any() else None,
            "skill_pct": skill(b_p),
            "raw_brier": round(b_raw, 5) if has.any() else None, "raw_skill_pct": skill(b_raw),
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


def within(g: dict) -> bool | None:
    """Did a group of published values verify within the rule's 10 points? None under 50 airport-days."""
    return None if g["n"] < BAND_MIN_N else bool(abs(g["shown"] - g["obs"]) <= BAND_MAX_GAP)


def verdict(h: dict) -> tuple[bool, bool | None]:
    """The ship rule's two results on one sample's numbers (h from score_rows)."""
    return bool(h["skill_pct"] is not None and h["skill_pct"] > 0), within(h["ge50"])


def passes(h: dict) -> bool:
    a, b = verdict(h)
    return bool(a and b is not False)


def with_verdict(h: dict, sample: str, key: str, fm: dict) -> dict:
    """A receipt: the numbers, the two rule results, and which breakpoints it is a receipt for."""
    a, b = verdict(h)
    return {**h, "rule_a_skill_over_0": a, "rule_b_ge50_within_10": b, "pass": bool(a and b is not False),
            "sample": sample, "map": key, "map_top": fm["y"][-1], "map_050": round(apply_daymap(fm, 0.5), 3)}


RULE = ("A (slot, bucket) publishes a day-level chance only if, on real 2025 cycles issued at the bucket's "
        "representative hour, the published chance (a) has Brier skill > 0 against climatology on the same rows "
        "and (b) the group published >= 0.5, when it has n >= 50, verifies within 10 points of its mean. Maps are "
        "fitted on 2023-2024 only; the map tested is the map shipped.")
RULE_SEASON = ("Rule (b) is also read off the Nov-Feb >= 0.5 group of the passing sample (50 airport-days or more). "
               "Where it misses by more than 10 points the slot is not published for target days in November to "
               "February.")
RULE_AREAS = ("Version 2's flag, kept for every field that stays on the pooled map: rule (b) is read off each "
              "area's own >= 0.5 group at the map's fit hour, on the test year and on all years, wherever that "
              "group has 50 airport-days. Where either misses by more than 10 points the day is not shown as "
              "calibrated for that area's fields; the number itself is unchanged.")
RULE_REGIONAL = ("An area gets its own map (same method, fitted on the area's rows) when, leaving each of 2023, "
                 "2024 and 2025 out in turn and pooling the three out-of-fold sets, the published chance (a) has "
                 "Brier skill > 0, (b) is within 10 points at >= 0.5 when n >= 50, (c) has a lower Brier than the "
                 "pooled map put through the same folds, (d) is within 10 points at 0.25-0.5 when n >= 50 and "
                 "(e) has a >= 0.5 group that is empty or n >= 50. Tried floored at the peak hourly chance first, "
                 "then unfloored. The map shipped is the one fitted on 2023-24; it is used in a bucket only where "
                 "it also passes (a), (b) and (d) on the area's rows of that bucket's 2025 sample. 2025 had been "
                 "looked at by region before this rule was written. Added after the result (it only withholds): "
                 "(f) an unfloored area map replaces the pooled map only where the pooled map's >= 0.5 group in "
                 "that area was judged (n >= 50) and ran high by more than 10 points.")
RULE_AFTER_BUILD = ("Reported, gates nothing: a day under way is also scored against fog after the build is on screen "
                    "(the hours after cycle hour + 1), not only after the cycle hour. Each under-way receipt carries "
                    "it as after_build.")
RULE_AREA_OWN = ("Reported, gates nothing: each area's record is also taken on every bucket's own 2025 sample "
                 "(slots[..].areas[..].own). The screen prints the larger miss of that and the fit hour's record.")
RULE_HOURLY = ("Reported, gates nothing: by area on the 2025 rows of each map's fit hour, summed expected hours "
               "against summed observed hours, and whether the hour shown as the day's peak fell below CAT I. "
               "Expected hours 'run high' ('low') when they exceed (fall short of) observed by more than 25%, "
               "judged only with at least 100 observed fog hours.")


# ---------------------------------------------------------------- regions ----
FAMILY = {  # the maps an area can have its own version of: fit hour and reach
    "d1": (12, "j1"), "d1u": (3, "j0"), "d2f": (3, "j1")}
YEARS = (2023, 2024, 2025)
FORMS = ("floored", "unfloored")


def compact(h: dict) -> dict:
    return {k: h[k] for k in ("n", "n_clim", "cycles", "base", "brier", "brier_climo", "skill_pct",
                              "ge50", "b25_50", "ge75", "nov_feb") if k in h}


def brief(h: dict) -> dict:
    """One held-out year of one area: enough to see the year-to-year swing, no more."""
    return {k: h[k] for k in ("n", "cycles", "base", "skill_pct", "ge50", "b25_50", "ge75")}


def regional(rows: pd.DataFrame, pooled_fm: dict) -> dict:
    """Leave-one-year-out verdict for every area, for one family (rows = all
    three years at its fit hour). Returns {area: record}; the record of an
    area that passed carries the map fitted on 2023-24 under "map"."""
    rows = rows[rows.year.isin(YEARS)]
    flags = {a["name"]: a for a in area_receipts(rows, published(pooled_fm, rows.raw.values, rows.pk.values))}
    # the comparison: the pooled map through the same folds
    pooled_oof = np.full(len(rows), np.nan)
    yr = rows.year.values
    for y in YEARS:
        tr = yr != y
        fm = fit_map(rows.raw.values[tr], rows.any_sub.values[tr])
        pooled_oof[~tr] = published(fm, rows.raw.values[~tr], rows.pk.values[~tr])
    out = {}
    for name, _ in AREAS:
        s = (rows.area == name).values
        a = rows[s]
        ay = a.year.values
        day = a.any_sub.values.astype(float)
        rec = {"stations": int(a.icao.nunique()), "rows": int(len(a)), "form": None, "pass": False,
               "years": f"{YEARS[0]}-{str(YEARS[-1])[2:]}", "flag": flags[name], "forms": {},
               "pooled_same_rows": compact(score_rows(a, pooled_oof[s])) if len(a) else None,
               # Days grouped by their peak hourly chance, and how often they held a fog hour: what the
               # screen can say where an unfloored area map puts the day chance below the peak hour
               # (Central Valley days whose peak hour showed 50% or more fogged about 4 times in 10).
               "by_peak_hour": {b: group(a.pk.values / 100.0, day, lo, hi, a.cycle.values)
                                for b, lo, hi in (("b25_50", 0.25, 0.5), ("ge50", 0.5, 1.01), ("ge75", 0.75, 1.01))}}
        if len(a) and all((ay == y).any() for y in YEARS):
            oof = {f: np.full(len(a), np.nan) for f in FORMS}
            for y in YEARS:
                tr = ay != y
                fm = fit_map(a.raw.values[tr], a.any_sub.values[tr])
                for f in FORMS:
                    oof[f][~tr] = published(fm, a.raw.values[~tr], a.pk.values[~tr], floor=f == "floored")
            for f in FORMS:
                h = score_rows(a, oof[f])
                ra, rb = verdict(h)
                rules = {"a_skill_over_0": ra, "b_ge50_within_10": rb,
                         "c_beats_pooled": bool(h["brier"] is not None and rec["pooled_same_rows"]["brier"] is not None
                                                and h["brier"] < rec["pooled_same_rows"]["brier"]),
                         "d_b25_50_within_10": within(h["b25_50"]),
                         "e_ge50_empty_or_50": bool(h["ge50"]["n"] == 0 or h["ge50"]["n"] >= BAND_MIN_N),
                         # (f), added after the result (amendment 1 of version 3): an unfloored map can
                         # never show more than its own top, so it takes the pooled map's 50%-and-over
                         # values away. That is only earned where those values were judged and ran high.
                         # Mountain West got one on a Brier margin of 0.00005 while the pooled map's
                         # >= 0.5 group there had verified at 0.62 shown / 0.61 observed (n 44).
                         "f_unfloored_only_where_pooled_ran_high": None if f == "floored" else bool(
                             flags[name]["ok"] is False and flags[name]["high"] is True)}
                rec["forms"][f] = {
                    "pass": all(v is not False for v in rules.values()), "rules": rules, "oof": compact(h),
                    # a map that tops out under 0.25 has no group the rules judge:
                    # this is where its own top values can be read off
                    "reliability": reliability(oof[f], day),
                    "hold_months": season_held([h]),
                    "by_year": {str(y): {"area_map": brief(score_rows(a[ay == y], oof[f][ay == y])),
                                         "pooled_map": brief(score_rows(a[ay == y], pooled_oof[s][ay == y]))}
                                for y in YEARS}}
            rec["form"] = next((f for f in FORMS if rec["forms"][f]["pass"]), None)
            rec["pass"] = rec["form"] is not None
            # the map that would ship: fitted on the two years before the test year
            tr = ay < TEST_YEAR
            fm = fit_map(a.raw.values[tr], a.any_sub.values[tr])
            r55 = raw_for(pooled_fm, 0.55)
            rec["map_2023_24"] = {"top": fm["y"][-1], "blocks": fm["blocks"], "n_fit": fm["n_fit"],
                                  "min_block_n": fm["min_block_n"],
                                  "pooled_055_becomes": None if r55 is None else round(apply_daymap(fm, r55), 3),
                                  "pooled_top_becomes": round(apply_daymap(fm, 1.0), 3)}
            if rec["pass"]:
                rec["map"] = fm
        out[name] = rec
    return out


PEAK_BANDS = (("b25_50", 0.25, 0.5), ("ge50", 0.5, 1.01), ("ge75", 0.75, 1.01))


def after_build(rows: pd.DataFrame, p: np.ndarray) -> dict:
    """A day under way, counted only from when its number can be read (version 3, amendment 2).

    The check counts an hour below CAT I at any hour after the cycle hour. The
    build of cycle h is on screen at h+1:25, by which time the hour filed as
    h+1 is over. This is the same published chance against fog after that
    hour. Reported beside the check, never gating: it is the record the
    screen prints for a day under way when it is the larger miss."""
    s = rows.ab_any.notna().values
    y, cyc = rows.ab_any.values[s].astype(float), rows.cycle.values[s]
    g = {b: group(p[s], y, lo, hi, cyc) for b, lo, hi in PEAK_BANDS}
    return {"counts": "an hour below CAT I after the build is on screen (cycle hour + 1), through 23Z",
            "n": int(s.sum()), **g, "ge50_within_10": within(g["ge50"])}


def area_own(rows: pd.DataFrame, p: np.ndarray, kind: str, sample: str) -> dict | None:
    """One area's record on one bucket's OWN check sample (version 3, amendment 3).

    Until this was added every area record on screen came from the map's fit
    hour. Bucket h00 (builds 01:25Z-03:25Z, the owner's evening) uses map d1,
    so a West Coast GO printed "about 11 points high" from 12Z cycles of the
    next date while this bucket's own West Coast rows ran 24 points high at
    50% and over (0.68 shown / 0.44 observed, n 128) and 30 at 75% and over.
    rows = the area's rows of the bucket's 2025 sample, p = what was published
    on them (the area's own map where it has one). eh is left out for a day 2
    seen in part: its expected hours stop where the forecast does and the
    observed hours do not."""
    if not len(rows):
        return None
    y, cyc = rows.any_sub.values.astype(float), rows.cycle.values
    out = {"sample": sample, "n": int(len(rows)), **{b: group(p, y, lo, hi, cyc) for b, lo, hi in PEAK_BANDS},
           # days by their peak hourly chance, and how often they held a fog hour
           "by_peak_hour": {b: group(rows.pk.values / 100.0, y, lo, hi, cyc) for b, lo, hi in PEAK_BANDS},
           "eh": None}
    if kind in ("under_way", "ahead", "evening"):
        eh, oh = float(rows.eh.sum()), int(rows.hrs.sum())
        judged = oh >= 100
        out["eh"] = {"days": int(len(rows)), "dates": int(rows.cycle.nunique()), "expected_hours": round(eh, 1),
                     "observed_hours": oh, "ratio": round(eh / oh, 2) if oh > 0 else None, "judged": bool(judged),
                     "runs": None if not judged else "high" if eh > 1.25 * oh else "low" if eh < 0.75 * oh else None}
    return out


def area_bucket_check(rows: pd.DataFrame, fm: dict, floor: bool, key: str, sample: str) -> dict:
    """One area map on the area's own rows of one bucket's 2025 sample: rules (a), (b) and (d)."""
    h = score_rows(rows, published(fm, rows.raw.values, rows.pk.values, floor=floor))
    out = with_verdict(compact(h), sample, key, fm)
    out["rule_d_b25_50_within_10"] = within(h["b25_50"])
    out["pass"] = bool(out["pass"] and out["rule_d_b25_50_within_10"] is not False)
    return out


# ----------------------------------------------------------------- hourly ----
def peak_rows(replay: pd.DataFrame, hour: int, j: int) -> pd.DataFrame:
    """icao, cycle, vday, pk, peak_ts: the hour each test-year day's peak hourly chance sits at (the first, on ties)."""
    r = replay[(replay.cycle.dt.hour == hour) & (replay.cycle.dt.year == TEST_YEAR)]
    valid = r.cycle + pd.to_timedelta(r.fhr.astype(int), unit="h")
    r = r.assign(valid=valid, vday=valid.dt.normalize())
    r = r[r.vday == r.cycle.dt.normalize() + pd.Timedelta(days=j)]
    top = r.loc[r.groupby(["icao", "cycle"], sort=False).p.idxmax(), ["icao", "cycle", "vday", "p", "valid"]]
    return top.rename(columns={"p": "pk_at", "valid": "peak_ts"})


def hourly_by_area(m: pd.DataFrame, replay: pd.DataFrame, fam: str) -> dict:
    """How the hourly forecast itself verified by area (RULE_HOURLY), on the
    test-year rows of one family's fit hour. The readiness review found KVIS
    expected hours at 0.51 a day against 0.24 observed over three years; the
    day map cannot fix that, but the screen can say where it holds."""
    hour, key = FAMILY[fam]
    a = m[(m.hour == hour) & (m.key == key) & (m.year == TEST_YEAR)]
    pr = peak_rows(replay, hour, int(key[1]))
    con = duckdb.connect()
    con.register("pr", pr[["icao", "peak_ts"]].drop_duplicates())
    obs = con.execute(f"""
        WITH th AS (
          SELECT icao, date_trunc('hour', ts + INTERVAL 30 MINUTE) AS hr_ts, max(sub) AS sub
          FROM '{OUT / "truth.parquet"}'
          WHERE ts >= '{TEST_YEAR - 1}-12-31' AND ts < '{TEST_YEAR + 1}-01-10' GROUP BY 1, 2)
        SELECT pr.icao, pr.peak_ts, th.sub FROM pr JOIN th ON th.icao = pr.icao AND th.hr_ts = pr.peak_ts
    """).fetchdf()
    pr = pr.merge(obs, on=["icao", "peak_ts"], how="left")
    a = a.merge(pr[["icao", "cycle", "sub"]], on=["icao", "cycle"], how="left")
    out = []
    for name, _ in AREAS:
        s = a[a.area == name]
        if not len(s):
            continue
        eh, oh = float(s.eh.sum()), int(s.hrs.sum())
        ratio = round(eh / oh, 2) if oh > 0 else None
        judged = oh >= 100
        runs = None if not judged else "high" if eh > 1.25 * oh else "low" if eh < 0.75 * oh else None
        k = s[s["sub"].notna()]
        pk, hit, day = k.pk.values / 100.0, k["sub"].values.astype(float), k.any_sub.values.astype(float)
        bands = {}
        for label, lo, hi in (("b25_50", 0.25, 0.5), ("ge50", 0.5, 1.01), ("ge75", 0.75, 1.01)):
            g = group(pk, hit, lo, hi, k.cycle.values)
            sel = (pk >= lo) & (pk < hi)
            # obs = the peak hour itself fell below CAT I; day = some hour of the day did
            bands[label] = dict(g, day=round(float(day[sel].mean()), 3) if sel.any() else None)
        out.append({"name": name, "runs": runs, "judged": bool(judged),
                    "eh": {"days": int(len(s)), "dates": int(s.cycle.nunique()), "expected_hours": round(eh, 1),
                           "observed_hours": oh, "ratio": ratio,
                           "per_day": [round(eh / len(s), 3), round(oh / len(s), 3)]},
                    "peak": bands})
    return {"sample": f"{a.cycle.nunique()} {'weekly ' if hour == 12 else 'real '}{hour:02d}Z cycles of {TEST_YEAR}",
            "areas": out}


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
    # aggregation: day_stats against the pre-daycal build_deploy loop
    # (fb899ce5 lines 242-253, kept verbatim as the reference; `today` there
    # was the UTC date of the build, so dd is counted from that here too)
    today = build_time(cycle).date()
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
    targets = {today + timedelta(days=dd): dd for dd in (1, 2)}
    for icao, f in eng.items():
        st = {targets[d]: s for d, s in day_stats(f["fhrs"], [r[3] for r in f["p"]], cycle, targets).items()}
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
#
# Version 3 (2026-11-25) checks every bucket of three cycle hours at its first
# hour, so 03Z, 06Z, 15Z and 18Z joined them (12Z keeps the weekly hindcast),
# and the evening maps needed a fit sample of their own: 03Z on the 104 weekly
# dates of 2023-24, in FIT03.
TRANSFER_CYCLES = ("00", "03", "06", "09", "15", "18", "21")
TRANSFER_DATES = ("20250102 20250109 20250116 20250123 20250130 20250206 20250213 20250220 20250227 "
                  "20250306 20250320 20250403 20250417 20250501 20250605 20250703 20250807 20250904 20250918 "
                  "20251002 20251016 20251030 20251106 20251113 20251120 20251127 20251204 20251211 20251218 "
                  "20251225").split()
FIT03_CYCLE = "03"


def fit03_dates() -> list[str]:
    """The weekly 12Z hindcast's own dates in the two fit years."""
    return sorted(p.name[4:12] for p in HIND.glob("nbh_*_12z.txt") if int(p.name[4:8]) < TEST_YEAR)


# What a run must find on disk: {(cycle hour, year): cycles}. A sample that is
# short would silently turn its bucket's numbers off, or fit a map on half a
# sample, so main() refuses to write daycal.json without all of them.
NEEDED = {(12, 2023): 52, (12, 2024): 52, (12, TEST_YEAR): 52, (3, 2023): 52, (3, 2024): 52,
          **{(int(h), TEST_YEAR): len(TRANSFER_DATES) for h in TRANSFER_CYCLES}}


def fetch_transfer() -> None:
    """Everything beyond the weekly 12Z set (fetch_hindcast.py): 420 bulletins
    of the 2025 check dates and 208 of the 2023-24 fit dates, 18 GB in all.
    Eight at a time: one at a time took 40 minutes for what the mirror serves
    in six."""
    import concurrent.futures as cf
    import urllib.request
    TRANSFER.mkdir(parents=True, exist_ok=True)
    FIT03.mkdir(parents=True, exist_ok=True)
    jobs = [(TRANSFER, d, hh) for hh in TRANSFER_CYCLES for d in TRANSFER_DATES]
    jobs += [(FIT03, d, FIT03_CYCLE) for d in fit03_dates()]
    todo, skip = [], 0
    for folder, d, hh in jobs:
        for prod in ("nbh", "nbs"):
            dest = folder / f"{prod}_{d}_{hh}z.txt"
            if dest.exists() and dest.stat().st_size > 1_000_000:
                skip += 1
            else:
                todo.append((dest, f"https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.{d}/{hh}/text/blend_{prod}tx.t{hh}z"))

    def one(job) -> str | None:
        dest, url = job
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                body = r.read()
            if len(body) < 1_000_000:
                raise RuntimeError(f"only {len(body)} bytes")
            part = dest.with_suffix(".part")      # never leave half a bulletin under the real name
            part.write_bytes(body)
            part.rename(dest)
            return None
        except Exception as e:  # noqa: BLE001
            return f"  miss {dest.name}: {e}"

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        misses = [r for r in ex.map(one, todo) if r]
    print("\n".join(misses))
    print(f"check and fit samples: fetched {len(todo) - len(misses)}, already had {skip}, missing {len(misses)} "
          f"of {2 * len(jobs)}")


# --------------------------------------------------------- slots and checks ----
def slot_plan() -> list[dict]:
    """Every (bucket, j) the planner can ask for, with the maps it may use in
    the order they are tried (the docstring's SLOTS table)."""
    plan = []
    for sk in SLOT_KEYS:
        rep, j = int(sk[1:3]), int(sk[-1])
        kind = slot_kind(rep, j)
        if kind == "partial":
            cands = (f"d2h{rep:02d}",)
        elif kind == "under_way":
            cands = ("d1u", "d1") if rep == 0 else ("d1u",)     # the one named fallback
        else:
            cands = SLOT_MAPS[kind]
        plan.append({"slot": sk, "rep": rep, "j": j, "kind": kind, "candidates": cands})
    return plan


def sample_words(rows: pd.DataFrame, rep: int) -> str:
    n = rows.cycle.nunique()
    return (f"{n} weekly 12Z cycles of {TEST_YEAR}, never fitted on" if rep == 12
            else f"{n} real {rep:02d}Z cycles of {TEST_YEAR}, never fitted on")


# The rows each pooled map is fitted on: (cycle hour, statistic key), target days before the test year.
MAP_FIT = {"d1": (12, "j1"), "d1u": (3, "j0"), "d2f": (3, "j1"), "d2h09": (12, "j2t09"), "d2h12": (12, "j2")}
# ...and the slot whose check is on the map's own fit hour (the largest look at its top band)
MAP_HOME = {"d1": "h12/j1", "d1u": "h03/j0", "d2f": "h03/j1", "d2h09": "h09/j2", "d2h12": "h12/j2"}
MAP_WORDS = {
    "d1": "the whole date, seen 1-39 h ahead",
    "d1u": "the date under way: the hours after the cycle, through 23Z",
    "d2f": "the whole date, seen 16-48 h ahead (after next: through 15Z, 18Z or 21Z)",
    "d2h09": "the date after next, through 09Z only",
    "d2h12": "the date after next, through 12Z only"}


# ------------------------------------------------------------------ main ----
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rebuild", action="store_true", help="re-run the hindcast replay instead of using the cache")
    ap.add_argument("--parity", action="store_true",
                    help="check the replay against the engine's latest run and record it in daycal.json")
    ap.add_argument("--allow-missing-samples", action="store_true",
                    help="write daycal.json even when a sample is short or absent (inspection only: do not commit it)")
    ap.add_argument("--fetch-transfer", action="store_true",
                    help=f"download the {'/'.join(TRANSFER_CYCLES)}Z check samples of {TEST_YEAR} and the "
                         f"{FIT03_CYCLE}Z fit sample of 2023-24 (18 GB) and stop")
    ap.add_argument("--out", default=str(OUT / "daycal.json"), help="where to write (default: the committed file)")
    args = ap.parse_args()
    dest = Path(args.out)

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
    # every sample the pre-registration names must be whole, before anything is fitted
    on_disk = replay.groupby([replay.cycle.dt.hour, replay.cycle.dt.year]).cycle.nunique().to_dict()
    lacking = [f"{h:02d}Z {y}: {on_disk.get((h, y), 0)} of {n} cycles" for (h, y), n in NEEDED.items()
               if on_disk.get((h, y), 0) < n]
    if lacking:
        # Run on a machine without the 18 GB of bulletins this would rewrite
        # the committed file with whole buckets switched off, and the planner
        # would go quiet at those hours with nothing in CI to say why.
        msg = f"samples short or absent ({'; '.join(lacking)}) — fetch them with fit_daycal.py --fetch-transfer"
        if not args.allow_missing_samples:
            sys.exit(f"{msg}. daycal.json was NOT rewritten (--allow-missing-samples writes one for inspection).")
        print(f"::warning:: {msg}")
    # build_deploy.covered_hours is what the build believes a cycle forecasts;
    # the bulletins must agree, for every sampled cycle hour and reach
    for hour in sorted({h for h, _ in NEEDED}):
        one = replay[replay.cycle.dt.hour == hour]
        if not len(one):
            continue
        cyc = one.cycle.iloc[0].to_pydatetime()
        seen = one[one.cycle == one.cycle.iloc[0]].groupby("icao").fhr.apply(tuple).mode().iloc[0]
        for j in (0, 1, 2):
            d = cyc.date() + timedelta(days=j)
            got = sorted({(cyc + timedelta(hours=int(f))).hour for f in seen if (cyc + timedelta(hours=int(f))).date() == d})
            assert got == covered_hours(cyc, d), (hour, j, got, covered_hours(cyc, d))

    truth, wclim = load_truth(), window_climo()
    m = assemble(day_table(replay), truth, wclim)
    # the leak-free split must hold row by row: every test-year day comes from
    # a cycle replayed with the pre-test climatology, every fit day from one
    # replayed as the engine runs live
    cyc_year = pd.to_datetime(m.cycle).dt.year
    assert (cyc_year[m.year == TEST_YEAR] == TEST_YEAR).all() and (cyc_year[m.year < TEST_YEAR] < TEST_YEAR).all()
    # the same test-year days as the first run scored them (climatology that
    # had seen the test year), only to say what the leak was worth
    leaky = assemble(day_table(replay[(replay.cycle.dt.year == TEST_YEAR) & (replay.cycle.dt.hour == 12)], col="p_all"),
                     truth, wclim)
    leaky = leaky[leaky.year == TEST_YEAR]
    chase = set(json.load(open(HERE.parent / "app" / "public" / "data" / "chase.json"))["airports"])
    fit12 = m[m.hour == 12]
    n_cycles = {y: int(fit12[fit12.year == y].cycle.nunique()) for y in (2023, 2024, 2025)}
    print(f"12Z station-days with truth: {len(fit12[fit12.key == 'j1']):,} (next date); cycles by year {n_cycles}")
    by_sample = m.groupby(["hour", "key"]).agg(rows=("raw", "size"), short=("short", "sum"))
    print("rows by cycle hour and reach (short = missing more hours than the build publishes):")
    print(by_sample.to_string())

    # ---- the pooled maps: fitted on 2023-24, as pre-registered ----
    fitted, maps, reference = {}, {}, {}
    for key, (hour, stat) in MAP_FIT.items():
        a = m[(m.hour == hour) & (m.key == stat)]
        tr = a[a.year < TEST_YEAR]
        fm = fit_map(tr.raw.values, tr.any_sub.values)
        fitted[key] = fm
        maps[key] = {"floor": True, "x": fm["x"], "y": fm["y"], "n_fit": fm["n_fit"], "blocks": fm["blocks"],
                     "min_block_n": fm["min_block_n"], "top": fm["y"][-1], "raw_for_50": raw_for(fm, 0.5),
                     "raw_050_maps_to": round(apply_daymap(fm, 0.5), 3), "home": MAP_HOME[key],
                     "statistic": MAP_WORDS[key],
                     "fit": f"{tr.cycle.nunique()} weekly {hour:02d}Z cycles of 2023-24"}
        print(f"\nmap {key}: fit n={fm['n_fit']:,} from {tr.cycle.nunique()} {hour:02d}Z cycles ({fm['blocks']} blocks, "
              f"smallest {fm['min_block_n']}); top {fm['y'][-1]}; a bare 0.50 -> {maps[key]['raw_050_maps_to']}; "
              f"0.50 needs a bare {maps[key]['raw_for_50']}")

    # ---- every (bucket, j): the ship rule on real cycles at the bucket's own hour ----
    slots, checks = {}, {"residuals": {}, "climatology_leak": {}}
    for sp in slot_plan():
        rows = m[(m.hour == sp["rep"]) & (m.key == f"j{sp['j']}") & (m.year == TEST_YEAR)]
        cyc0 = datetime(TEST_YEAR, 6, 1, sp["rep"])
        hrs = covered_hours(cyc0, cyc0.date() + timedelta(days=sp["j"]))
        slot = {"kind": sp["kind"], "rep": sp["rep"], "j": sp["j"], "hours": [sp["rep"] + i for i in range(3)],
                "cover": {"from": hrs[0], "through": hrs[-1]}, "map": None, "check": None, "tried": [],
                "why": None, "hold_months": [], "areas": {}}
        y = rows.any_sub.values.astype(float)
        for key in sp["candidates"]:
            if not len(rows):
                slot["tried"].append({"map": key, "pass": False, "rule_a_skill_over_0": False,
                                      "rule_b_ge50_within_10": None, "sample": "no sample on disk"})
                continue
            p = published(fitted[key], rows.raw.values, rows.pk.values)
            # the vectorised path must be the function the live build calls
            for raw, pk, pv in list(zip(rows.raw.values, rows.pk.values, p))[:: max(1, len(rows) // 2000)]:
                live = day_chance(fitted[key], {"noev": 1.0 - raw, "pk": int(pk)})
                assert abs(live - pv) < 0.00051, (sp["slot"], key, raw, pk, pv, live)   # pv is rounded to 3 decimals
            c = with_verdict(score_rows(rows, p), sample_words(rows, sp["rep"]), key, fitted[key])
            c["reliability"] = reliability(p, y)
            if sp["kind"] == "under_way":
                c["after_build"] = after_build(rows, p)     # reported, gates nothing
            slot["tried"].append(c)
            if c["pass"]:
                slot["map"], slot["check"] = key, c
                slot["hold_months"] = season_held([c])
                break
        if slot["map"] is None:
            slot["why"] = "no sample" if not len(rows) else "failed"
        slots[sp["slot"]] = slot
        for c in slot["tried"]:
            if "n" not in c:
                continue
            print(f"\n== {sp['slot']} ({sp['kind']}, forecast {hrs[0]:02d}Z-{hrs[-1]:02d}Z) map {c['map']}: "
                  f"{c['sample']}; n={c['n']:,} ({c['short']} short) base {c['base']}")
            print(f"   Brier {c['brier']} vs climatology {c['brier_climo']} -> skill {c['skill_pct']:+.1f}%   "
                  f"(bare product {c['raw_skill_pct']:+.1f}%)")
            print(f"   >=0.5 {c['ge50']}   0.25-0.5 {c['b25_50']}   >=0.75 {c['ge75']}")
            if "after_build" in c:
                ab = c["after_build"]
                print(f"   counted from the build on (reported): >=0.5 {ab['ge50']}   >=0.75 {ab['ge75']}")
            print(f"   Nov-Feb >=0.5 {c['nov_feb']['ge50']}")
            print(f"   rule (a) {c['rule_a_skill_over_0']}  rule (b) {c['rule_b_ge50_within_10']}  -> "
                  f"{'PASS' if c['pass'] else 'FAIL'}")
        print(f"   {sp['slot']}: " + (f"publishes map {slot['map']}" + (f", not for months {slot['hold_months']}"
                                                                       if slot["hold_months"] else "")
                                     if slot["map"] else "publishes NO day-level chance"))

    # ---- areas: leave one year out, then each bucket ----
    regional_out, hourly = {}, {}
    for fam, (hour, stat) in FAMILY.items():
        rows = m[(m.hour == hour) & (m.key == stat)]
        rec = regional(rows, fitted[fam])
        hourly[fam] = hourly_by_area(m, replay, fam)
        print(f"\n#### areas, family {fam} ({hour:02d}Z cycles, {stat}); leave one year out, pooled out-of-fold rows")
        for name, r in rec.items():
            fm = r.pop("map", None)
            for f in FORMS:
                o = (r["forms"].get(f) or {})
                if not o:
                    continue
                h = o["oof"]
                print(f"  {name:15s} {f:9s} n={h['n']:6d} skill {h['skill_pct']}  (pooled map {r['pooled_same_rows']['skill_pct']})  "
                      f">=0.5 {h['ge50']['n']}/{h['ge50']['shown']}/{h['ge50']['obs']}  "
                      f"0.25-0.5 {h['b25_50']['n']}/{h['b25_50']['shown']}/{h['b25_50']['obs']}  "
                      f">=0.75 {h['ge75']['n']}/{h['ge75']['shown']}/{h['ge75']['obs']}  rules {o['rules']} -> "
                      f"{'PASS' if o['pass'] else 'fail'}")
            if "map_2023_24" in r:
                print(f"  {name:15s} map fitted on 2023-24: top {r['map_2023_24']['top']}, a pooled 0.55 becomes "
                      f"{r['map_2023_24']['pooled_055_becomes']}, {r['map_2023_24']['blocks']} blocks -> "
                      f"{('area map, ' + r['form']) if r['pass'] else 'keeps the pooled map'}")
            if fm is not None:
                maps[f"{fam}@{name}"] = {
                    "family": fam, "area": name, "floor": r["form"] == "floored", "x": fm["x"], "y": fm["y"],
                    "n_fit": fm["n_fit"], "blocks": fm["blocks"], "min_block_n": fm["min_block_n"],
                    "top": fm["y"][-1], "raw_for_50": raw_for(fm, 0.5),
                    "raw_050_maps_to": round(apply_daymap(fm, 0.5), 3),
                    "pooled_055_becomes": r["map_2023_24"]["pooled_055_becomes"],
                    "fit": f"{name} fields, weekly {hour:02d}Z cycles of 2023-24"}
        regional_out[fam] = rec
    for sp in slot_plan():
        slot = slots[sp["slot"]]
        fam = slot["map"]
        rows = m[(m.hour == sp["rep"]) & (m.key == f"j{sp['j']}") & (m.year == TEST_YEAR)]
        for name, _ in AREAS:
            r = (regional_out.get(fam) or {}).get(name)
            if slot["map"] is None:
                slot["areas"][name] = {"use": "pooled", "map": None, "why": "slot not published", "hold_months": [],
                                       "check": None}
                continue
            ra = rows[rows.area == name]
            if r is None or not r["pass"]:
                slot["areas"][name] = {"use": "pooled", "map": None, "hold_months": [], "check": None,
                                       "why": "no area map for this reach" if r is None
                                       else "leave-one-year-out failed"}
            else:
                rk = f"{fam}@{name}"
                c = area_bucket_check(ra, maps[rk], maps[rk]["floor"], rk, sample_words(rows, sp["rep"]))
                slot["areas"][name] = {"use": "regional" if c["pass"] else "pooled", "map": rk if c["pass"] else None,
                                       "why": None if c["pass"] else "failed at this bucket",
                                       "hold_months": r["forms"][r["form"]]["hold_months"] if c["pass"] else [],
                                       "check": c}
            # the area's record on THIS bucket's sample, with the map its fields get here
            used_map = maps[slot["areas"][name]["map"]] if slot["areas"][name]["use"] == "regional" else fitted[fam]
            slot["areas"][name]["own"] = area_own(
                ra, published(used_map, ra.raw.values, ra.pk.values,
                              floor=used_map.get("floor", True) if slot["areas"][name]["use"] == "regional" else True),
                sp["kind"], sample_words(rows, sp["rep"]))
        used = [n for n, a in slot["areas"].items() if a["use"] == "regional"]
        failed = [n for n, a in slot["areas"].items() if a["why"] == "failed at this bucket"]
        if slot["map"]:
            print(f"  {sp['slot']} ({fam}): area maps used for {used or 'none'}"
                  + (f"; failed at this bucket: {failed}" if failed else ""))
    # an area map nothing uses does not ship
    in_use = {a["map"] for s in slots.values() for a in s["areas"].values() if a["use"] == "regional"}
    maps = {k: v for k, v in maps.items() if "@" not in k or k in in_use}

    print("\n#### the hourly forecast by area (reported, gates nothing)")
    for fam, hrec in hourly.items():
        for a in hrec["areas"]:
            print(f"  {fam:4s} {a['name']:15s} expected {a['eh']['expected_hours']:8.1f} h vs observed "
                  f"{a['eh']['observed_hours']:5d} h (x{a['eh']['ratio']})  runs {a['runs'] or ('-' if a['judged'] else 'not judged')}"
                  f"   peak hour >=50%: {a['peak']['ge50']}")

    # ---- version 2's side checks on the committed day-1 map, kept so its numbers can still be traced ----
    a = fit12[fit12.key == "j1"]
    tr, te = a[a.year < TEST_YEAR], a[a.year == TEST_YEAR]
    p = published(fitted["d1"], te.raw.values, te.pk.values)
    lk = leaky[leaky.key == "j1"]
    hl = score_rows(lk, published(fitted["d1"], lk.raw.values, lk.pk.values))
    h12 = slots["h12/j1"]["tried"][0]
    checks["climatology_leak"]["d1"] = {
        "with_test_year_in_climatology": {k: hl[k] for k in ("skill_pct", "ge50", "ge75")}, "ships": passes(hl),
        "without": {k: h12[k] for k in ("skill_pct", "ge50", "ge75")}}
    checks["residuals"]["d1"] = residuals(te, p, chase)
    # reproduction of the first review's figure only: a bare isotonic map, no
    # minimum block, no floor. Never shipped, never used to choose.
    bare = fit_map(tr.raw.values, tr.any_sub.values, min_block=1, ends=True)
    hb = score_rows(te, np.interp(te.raw.values, bare["x"], bare["y"]))
    reference["d1"] = {"brier": hb["brier"], "brier_climo": hb["brier_climo"], "skill_pct": hb["skill_pct"],
                       "ge50": hb["ge50"], "blocks": bare["blocks"], "top": bare["y"][-1]}
    # Central Valley, day 1 from 12Z cycles, year by year, with the pooled map
    # (2023-24 in-sample for it, but it is pooled over 1,200 stations and a
    # regional bias shows either way).
    a = a[a.icao.isin(CENTRAL_VALLEY)]
    pv, yv = published(fitted["d1"], a.raw.values, a.pk.values), a.any_sub.values.astype(float)
    checks["central_valley_day1_by_year"] = {
        label: {"n": int(s.sum()), "ge50": group(pv[s], yv[s], 0.5, 1.01), "b25_50": group(pv[s], yv[s], 0.25, 0.5),
                "eh": round(float(a.eh.values[s].mean()), 3), "obs_hours": round(float(a.hrs.values[s].mean()), 3)}
        for label, s in [(str(y), (a.year == y).values) for y in (2023, 2024, 2025)] + [("all", np.ones(len(a), bool))]}

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
        # 3: keyed by (bucket of cycle hours, j) instead of by planner day;
        # area maps; build_deploy.load_daycal requires every slot and checks
        # each published one against its own receipt
        "version": 3,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "service_day_offset_h": SERVICE_DAY_OFFSET_H,
        "fit": f"2023-2024 weekly hindcast: {n_cycles[2023] + n_cycles[2024]} cycles at 12Z, "
               f"{on_disk.get((3, 2023), 0) + on_disk.get((3, 2024), 0)} at 03Z",
        "test": f"{TEST_YEAR}, never fitted on: {n_cycles[TEST_YEAR]} weekly 12Z cycles and {len(TRANSFER_DATES)} "
                f"cycles at each of {', '.join(h + 'Z' for h in TRANSFER_CYCLES)}",
        "test_climatology": f"test-year forecasts replayed with a climatology from truth before {TEST_YEAR}-01-01",
        "statistic": "1 - prod(1 - p_hour)^step over the forecast hours of the target date that the cycle covers "
                     "(hours after the cycle when the date is under way), exactly as build_deploy.day_stats "
                     "builds it",
        "target": "at least one hourly below-CAT-I flag in the UTC date (>= 18 hourly obs); for a date under way, "
                  "at an hour after the cycle (three quarters of those hours observed)",
        "baseline": "dayscale.json climo_day; for a date under way, the per-airport monthly frequency of the same "
                    "part-day event over every truth year but the row's own",
        "published": "max(map(statistic), peak hourly chance), 3 decimals; an area map marked floor=false is "
                     "published without the max",
        "method": f"isotonic regression (pool-adjacent-violators), blocks under {MIN_BLOCK} station-days pooled "
                  f"into the block below, one breakpoint per block, linear interpolation between",
        "min_hours": f"published only when at most {MAX_MISSING_HOURS} hour-equivalents of what the cycle covers "
                     f"are missing",
        "rule": RULE,
        "rule_by_season": RULE_SEASON,
        "rule_by_area": RULE_AREAS,
        "rule_regional": RULE_REGIONAL,
        "rule_hourly": RULE_HOURLY,
        "rule_after_build": RULE_AFTER_BUILD,
        "rule_area_own": RULE_AREA_OWN,
        "buckets": {b: {"hours": [int(b[1:]) + i for i in range(3)], "rep": int(b[1:])} for b in BUCKETS},
        "areas": [{"name": name, **d} for name, d in AREAS],
        "maps": maps,
        "slots": slots,
        "regional": regional_out,
        "hourly": hourly,
        "reference_bare_isotonic": reference,
        "checks": checks,
        "parity": par,
    }
    text = json.dumps(out, separators=(",", ":"))
    # what is written must be what the build will accept: load it back through
    # the build's own validator before it replaces the committed file
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(text)
    try:
        from build_deploy import load_daycal
        load_daycal(tmp)
    except Exception:
        tmp.unlink()
        raise
    tmp.replace(dest)
    print(f"\nwrote {dest.name} ({dest.stat().st_size // 1024} KB)")
    for sk in SLOT_KEYS:
        s = slots[sk]
        used = [n for n, a in s["areas"].items() if a["use"] == "regional"]
        print(f"  {sk} {s['kind']:10s} " + (f"map {s['map']}" + (f" (not months {s['hold_months']})" if s["hold_months"] else "")
                                           + (f"; area maps: {', '.join(used)}" if used else "")
                                           if s["map"] else "NO day-level chance"))


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
