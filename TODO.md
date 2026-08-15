# Roadmap / TODO

## 0. CHASE Canada tier (design agreed, deferred to a fresh session)
Curated Canadian airports for the fog-chase board — border belt + Maritimes +
every high-fog Canadian field already in our rankings (CYQI 555 h/yr, CYYT 516,
CYHZ 395, CYHM, CYXU, CYQG…). Nav Canada publishes no NASR equivalent, so a
batched agent fleet (cheap model, AIP-curation recipe: per-claim sources,
planted probes, owner audit) researches per-runway ALS type, RVR sensors, and
ILS category from CAP charts / CFS. Output: `pipeline/data/ca_chase_curated.csv`
→ merged into `chase.json` with `curated` confidence badges in the panel.
NASR APT/CIFP re-download (`fetch_nasr.py`) now rides in the monthly
reference refresh CI (item 1, done).

## 0.5 DEPLOY "tomorrow" window (Travis, 2026-08-14) — DONE 2026-08-14
Add a **tomorrow** option beside "next 7 days / next 14 days" in the deploy
planner — a 1-day window that makes the panel a pure night-before board:
verdict + WHEELS UP + per-field peeks for the next ~30 h only. Completes the
funnel's full cycle (14d -> 7d -> tomorrow -> wheels-up).

## 1. Monthly reference-data refresh — DONE 2026-08-14 (GitHub Actions, no Mac)
`.github/workflows/reference.yml` runs monthly (3rd, 07:40Z) + on dispatch:
`fetch_nasr.py` (current-cycle NASR + CIFP; re-extracts when the zip is newer)
→ `refresh_reference.py` (ILS Master scraped from the aeronav reports page,
EGNOS xlsx scraped from the ESSP map page, CIFP LPV, NASR categories — rebuilds
the four reference CSVs only on content change) → `build_chase.py` → when the
minima/category CSVs changed, re-exports atlas aggregates against
`fogatlas-forecast/reference/classified.parquet` in R2 (re-upload that object
after any METAR refresh) → commits + `wrangler pages deploy`. Parsers were
validated by regenerating the committed CSVs to within verified real-world
cycle drift. Gotchas encoded in the scripts: COPTER ILS rows excluded from
CAT I floors; cat_curated universe = airports_full.csv US rows (AK/HI carry
NASR categories despite having no METAR archive); APRA needs Accept: json.

## 2. Quarterly METAR refresh — MACHINERY DONE 2026-08-15; needs a schedule
Incremental-append shipped in `fetch_iem.py` (per-station since-last-day fetch,
overlap-dropping append, full-fetch fallback, same-start batching; blacklist
only on full fetches). Window is dynamic end-to-end: analyze emits
`out/window.json`, export uses real window-hours for coverage, the app binds
the tagline/notes to it. First refresh ran 2026-08-15: data through
2026-08-15, +103 airports (see below). The refresh recipe (Mac, ~4h fetch +
~1h rebuild, all resumable):
  pipeline: fetch_iem --list airports_full.csv --batch 10 --pause 10 (then --sky)
  → analyze_pilot → analyze_persistence → export_aggregates (all --list
  airports_full.csv) → build_chase → copy airports/detail/persistence to
  app/public/data → forecast: build_truth (stations.json + climo) →
  build_pages → vite build + pages deploy → commit → re-upload
  pipeline/out/classified.parquet to R2 reference/ (reference CI contract).
Remaining: put it on a Jan/Apr/Jul/Oct cadence (scheduled-task ritual like the
monthly bar check, or launchd). CI-ification would need the 24 GB raw archive
synced to R2 — possible, not obviously worth it.

## Smaller candidates
- International LTS CAT I research pass: which runways have CHARTED LTS CAT I
  (EASA SA CAT I analog) minima — currently the HUD tier gets no credit abroad;
  agents would read eAIP IAC charts for LTS CAT I minima boxes (start with the
  top-40 already-researched airports)
- Next ~100 international airports' per-runway CAT I floors (agent pass #2;
  Vágar + Nalchik retry with better sources — skipped at low confidence)
- ~~Alaska + Hawaii absent from the METAR archive~~ — DONE 2026-08-15. Root
  cause: IEM serves AK/HI/territory stations under 4-letter ICAO, not the FAA
  local code; the wrong id blacklisted all 112 (incl. San Juan + Guam) on day
  one. Fixed in build_airport_list (local-code only for K-prefixed icaos),
  unblacklisted, full histories fetched. Atlas now 3,497 airports; Shemya
  PASY 375 h/yr (top-30 globally), St Paul 160, Barrow 198; Honolulu/San Juan
  ≈ 0 (clean negative controls). 90 new US chase fields; 102 new stations
  enrolled in shadow forecasts (NBM covers them).
- Route/mission view (origin–destination fog risk) from the original design
- Print-friendly briefing mode
