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
- Alaska + Hawaii are absent from the METAR archive entirely (zero PA*/PH* in
  airports.json — discovered 2026-08-14; the fetch never covered them). The
  Aleutians (PACD Cold Bay, PADK Adak, PASN St Paul) are world-class fog with
  ILS + NASR categories already curated. Fix rides on the fetch_iem
  incremental work in item 2: fetch AK/HI ASOS networks, reanalyze, re-export.
- Route/mission view (origin–destination fog risk) from the original design
- Print-friendly briefing mode
