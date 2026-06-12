# Roadmap / TODO

## 1. Monthly reference-data refresh (launchd, automatable now)
Consolidate the FAA/ESSP reference parsing — currently session one-offs — into
`pipeline/refresh_reference.py`, then schedule it monthly from the Mac via launchd
(`StartCalendarInterval`; missed runs fire at next wake, surviving power cycles).

- Re-download per AIRAC cycle: FAA ILS Master (per-runway minima; scrape current
  link from aeronav procedures/reports page), NASR ILS_CSV (categories), CIFP
  (LPV path points), ESSP EGNOS xlsx
  (`egnos.gsc-europa.eu/sites/default/files/lpv_procedures_map/egnos_procedures-<AIRAC>.xlsx`;
  current cycle readable from drupal-settings JSON on the map page)
- Rebuild `us_ils_levels.csv`, `cat_curated.csv` (NASR portion), `us_lpv.csv`,
  `egnos_lpv.csv` → rerun `build_airport_list.py` + `export_aggregates.py`
  against the existing parquet (no METAR refetch) → `wrangler pages deploy` → git push
- Deliverables: `pipeline/refresh_reference.py`, `scripts/com.fogatlas.reference.plist`,
  one-line `launchctl bootstrap` install instructions

## 2. Quarterly METAR refresh (blocked on incremental fetcher)
Extend the climatology window in place.

- Prerequisite: incremental-append mode in `fetch_iem.py` — currently treats
  existing station files as complete; must fetch since-last-timestamp per
  station, append, dedupe (vis + sky passes)
- Then: full re-analysis (analyze → persistence → training set if model is
  retrained) → export → deploy; launchd guard script runs Jan/Apr/Jul/Oct
- Update the "data through <date>" stamp in the app shell

## Smaller candidates
- International LTS CAT I research pass: which runways have CHARTED LTS CAT I
  (EASA SA CAT I analog) minima — currently the HUD tier gets no credit abroad;
  agents would read eAIP IAC charts for LTS CAT I minima boxes (start with the
  top-40 already-researched airports)
- Next ~100 international airports' per-runway CAT I floors (agent pass #2;
  Vágar + Nalchik retry with better sources — skipped at low confidence)
- Custom domain → unlocks Bot Fight Mode / WAF + zone analytics
- Route/mission view (origin–destination fog risk) from the original design
- Print-friendly briefing mode
