# Roadmap / TODO

## 0. CHASE Canada tier — DONE 2026-07-20 (shipped with CHASE V2)
Shipped same day as the rest of CHASE V2: Sonnet fleet researched the CAP/CFS
charts (147 claims confirmed, 8 refuted-and-corrected, 15 low-confidence ends
dropped), all five C060 probes passed with per-end precision. `chase.json`
carries 810 US + 37 CA airports (111 curated Canadian runway ends, `cur:1`
badges in the panel). Original design note kept below for the record.

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
SCHEDULED 2026-08-15: task `fogatlas-quarterly-metar-refresh` runs the recipe
on the 15th of Feb/May/Aug/Nov at 9am (quarter-anchored to the 2026-08-15
refresh; runs on next app launch if the Mac was closed), with plausibility
gates that hold the deploy on anomalies. CI-ification would need the 24 GB
raw archive synced to R2 — possible, not obviously worth it.

## 3. SEO / distribution — audit 2026-09-11, phase 3 shipped 2026-09-12
History: baked static answers + city pages + season sections (2026-08-15/16),
crawler-visible root identity (08-17), pages.dev JS hop (08-18), self-healing
bake gate + IndexNow (08-29 — IndexNow has returned HTTP 200 daily since).

**Audit verdict (six-lens, adversarially verified, 2026-09-11):** the site was
technically sound but invisible and unmeasured — a 52-day-old domain with
6,777 templated pages, one confirmed indexed page, zero inbound links, no
analytics on any baked page, a site-wide soft-404 (every unknown path served
the map shell with HTTP 200), a receipts page 28 days stale and contradicting
the cohort count, 65% of airport pages answering "will it be foggy tomorrow?"
with "No fog forecast feed exists", and GitHub's 60-day idle rule set to
disable every cron ~2026-10-28. Refuted along the way: "run the bar-check so
Central Valley airports carry percentages by tule season" — under the
pre-registered live bar (>=10 fog event-hours since 2026-07-21) they cannot
graduate before the Dec 1 ritual, whatever we do.

**Shipped 2026-09-12 (all in build_pages.py + workflows, riding the daily bake):**
- analytics beacon on every baked page; real `404.html` + `_redirects` (IATA
  → ICAO for covered airports, city aliases delhi/sydney/melbourne/bangalore…,
  lower-casing + full IATA map inline in the 404 page); `www` → apex 301 via a
  zone Redirect Rule (pages.dev stays canonical + JS hop — Pages can't host-match)
- every non-public page now leads with a climatology-first answer plus the
  airport's OWN terminal forecast (AWC TAF, attributed, fog/mist parsed, ~30 h
  horizon) and an age-stamped latest observation; public pages get a
  verification receipt line; "through Friday" now means the 36-h horizon end
- airport pages: ICAO/IATA in title/H1/JSON-LD, approach-capability + cause +
  EFVS + reliability facts as prose and FAQ, monthly hours table, tule-fog
  block (Central Valley), marine-layer honesty block + FAQ (SFO/OAK/SJC + SF
  city), breadcrumbs + hub/home links + nearby airports, og:image/twitter
  cards, snippet fixes (word-boundary truncation, capitalisation, "most of
  the year" seasons); city pages same chrome, Bangalore/Bengaluru merged,
  "Will it be foggy in Delhi tomorrow?" title alias
- new pages: /about/, /methodology/ (from README/METHODOLOGY.md), regions
  /fog/region/central-valley/ (tule fog) and /fog/region/north-india/,
  /fog/foggiest-airports/, /fog/foggiest-us-airports/ (quality-gated),
  /fog/cat-iii-airports/, /fog/cat-ii-airports/, /fog/efvs/; scorecard page
  rendered server-side from /api/scorecard (status line, pass table, stale
  warning) — no more hard-coded "shadow mode"
- machine layer: /fog/index.json + /fog/city/index.json, llms.txt to spec
  (absolute links, Optional section, "unverified p must not be quoted"),
  data.json schemaVersion/refreshCadence/validThrough/thresholdDefinition/
  currentObservation/capability/nearby, current robots tokens
- crawl: sitemap index (priority child = public cohort + their cities +
  demand cities + lists/docs; then airports; then cities) with honest lastmod;
  IndexNow submits only today's changed URLs and reports its real status
  (step fails the run AFTER the KV upload)
- pipeline health: watchdog.yml (twice daily: bake fresh, issuance <12 h,
  scorecard <40 d and refreshed monthly, every public airport has a receipt,
  key file, real 404, priority sitemap, about page, beacon → opens/updates a
  `watchdog` issue); barcheck.yml (1st of month: sync R2 logs → score →
  KV `scorecard` verified → commit shadow_report.json — the monthly ritual
  now only READS that file, no wrangler, no sandbox prompt); reference.yml
  timeout 45 → 120 min (09-03 run was killed at 45m17s); bake prints a
  page-uniqueness metric to the step summary

**Search Console read (2026-09-16) + response:** 6,584 indexed / 903 not on a
57-day-old domain — indexation is solved (the Oct 15 sitemap checkpoint is
retired). 3-month performance: 66 clicks, all long-tail place queries on
city/airport pages (CYTZ, Prince Rupert, Auckland, Prague…); Google tested
the site on the bare head terms "fog forecast" (3,633 imp, pos 6.0) and "fog
forecast tomorrow" (2,604 imp, pos 7.1) Sep 4–9 at 0.06% CTR and pulled
back. Shipped: /fog/forecast/ "Where will it be foggy tomorrow?" — the
location-less answer (verified ≥50% list, TAF dense-fog list by country,
fog-at-latest-observation list; daily; linked from every page). Decisions
taken: option C wording (keep guidance + TAF), CC BY 4.0 (in every data.json,
Dataset node, About, llms.txt), contact hello@fogatlas.org (published once
Email Routing is live — the zone token cannot create routing rules,
destination addresses or Email Sending: dashboard or token scope).
Cloudflare AI-bot policy confirmed Allow/Allow/Allow.

**Search Console read (2026-09-26):** 106 clicks / 11.9K imp over 3 months
(66 / 9.6K on 09-16 → ~4 clicks/day since, ~2% CTR on the long-tail place
queries that make up 5/6 of the new impressions); 6,680 indexed / 866 not.
Corrections + findings: the head-term audition never hit the homepage (8 imp
in the 09-16 export vs 7,244 on city pages, 2,682 on airport pages) — Google
LOCALISES "fog forecast" to the searcher's nearest city/airport page, which
is why the zero-click impressions came from IN/MX/EC/CO. The 467 "Alternate
page with proper canonical" are pre-09-12 URL shapes (unknown paths served
the map shell whose canonical is the homepage; www served duplicates) —
benign, shrinking on recrawl; 197+197 "currently not indexed" ≈ 5%, normal.
Named place queries sit at pos 7–10 (SF 7.6, Auckland 7.8, Casablanca 7.7,
Hamilton 8.7, Delhi 10.0) — only links move that. Bug found + fixed: the
SFO/OAK/SJC airport and SF/Oakland/San Jose city pages opened their meta
description, FAQ answer and OG text with the marine-layer caveat, so the
snippet for "fog forecast san francisco" (393 imp, 0 clicks) was a truncated
disclaimer — now forecast first, caveat second. Bing Webmaster import done
2026-09-26. Still wanted from GSC: Performance → filter Query = "fog
forecast" → Pages + Countries tabs (which local pages Google auditioned, and
whether /fog/forecast/ has a row yet); the "Crawled – currently not indexed"
list; the new "generative AI features" report.

**Travis-only, time-critical:**
- BEFORE 2026-09-15: Cloudflare dashboard → fogatlas.org → Security →
  Settings → AI bot policy: allow Search + Agent (+ Training if you want
  CCBot). Cloudflare blocks AI crawlers by default on zones onboarded after
  July 2025 and changes defaults again on Sep 15; edge probes cannot see
  verified-bot blocks. Common Crawl has zero captures of fogatlas.org.
- DONE 2026-09-26: Bing Webmaster Tools import. Next Bing read (mid-Oct):
  IndexNow Insights + AI Performance — the only view of what Bing did with
  the daily submissions.
- Search Console, next pull: Performance → filter Query "fog forecast" →
  Pages + Countries (export both); Page indexing → "Crawled – currently not
  indexed" (export); the "generative AI features" report. Paste to Claude.
  Optional: service account → GSC_SA_KEY secret.
- Keep one real commit landing every <8 weeks (GitHub disables schedules in
  public repos after 60 idle days; barcheck.yml's monthly bot commit may or
  may not count). Optional: healthchecks.io ping after the deploy step.
- Decide: notes/decision-shadow-page-wording.md (A raw-NBM attributed / B
  hindcast admission / C keep guidance), a data license (CC BY 4.0
  suggested), a contact email for About / Organization JSON-LD.
- Send: notes/distribution-drafts-2026-09.md — two SF emails now, OPSGROUP
  mid-Oct, one Show HN late Oct, press after; never PPRuNe; Reddit only
  after reading each sub's rules yourself. Wayback "Save Page Now" on the
  four hub pages (zero captures exist).

**Later (gated on the GSC/Bing reads):** single-station city↔airport
canonical decision (96% of city pages restate one airport page); country /
US-state sub-hubs; per-page og:image cards (needs a rasterizer in CI); c8
phase 2 regions (Punjab, UAE, UK, Auckland, Melbourne, Atlantic Canada) +
/fog/season-2026/ story page; Hindi variant for ~40 IN/PK stations (only if
the English India pages index); weekly GSC/Bing API pull + urlInspection
census; Oct 15 checkpoint — if priority-sitemap indexation <30%, drop the
2,282 uncovered long-tail airports from the sitemap rather than add pages.

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
