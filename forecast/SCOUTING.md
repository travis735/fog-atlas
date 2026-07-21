# V3 Forecast — Source Scouting (verified 2026-07-21)

Every claim below was verified by fetching the actual product. No guidance format is assumed.

## Primary source: NBM text collectives via AWS Open Data (DECISION: sole guidance source for V3.0)

- **Bucket**: `https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.YYYYMMDD/HH/text/`
  - `blend_nbhtx.tHHz` — NBH: hourly resolution, ~25 h horizon (near-term strip)
  - `blend_nbstx.tHHz` — NBS: 3-hourly, ~72 h horizon (the 48 h page window + margin)
  - `blend_nbetx.tHHz` — NBE: extended 12-hourly to ~day 8 (DEPLOY planner later)
- **Cadence**: every cycle probed (00–21z by 3 h) exists; mirror is near-real-time (current cycle appears within the hour). ~29 MB per NBS collective, 9,578 stations.
- **Archive depth**: verified back to **2020-11** (2020-01 404s; boundary unprobed inside 2020). 5+ years of hindcast material for calibration.
- **NOMADS is bot-walled** (403 even on direct file paths with browser UA) — AWS mirror is the door. LAMP on tgftp also 403'd; deferred (see below).

## Station coverage (intersect with our data, from the 2026-07-20 12z collective)

- **US chase airports: 809/810** (only KSVR missing — no NBM station)
- **Canadian curated: 37/37** — NBM includes Canadian stations → **airport-level forecast tier covers Canada after all**; the US-only constraint now applies only to the CPC/planner layer.
- Whole atlas: 1,113/3,394 covered (810 US + 177 CA + others). Non-covered airports: climatology-only pages, honestly labeled.

## Block format (empirically decoded, KSFO + CYQI 2026-07-20 12z)

Fixed-width rows per station, `FHR` = lead hours. Relevant rows:
- **CIG** — ceiling, hundreds of ft; `-88` = unlimited
- **VIS** — visibility, **tenths of miles, capped 100 (=10 mi)** (CYQI showed 1–3 = 0.1–0.3 mi in real dense fog)
- **IFC / IFV** — **probability (%) of IFR ceiling / IFR visibility** — NBM ships probabilistic rows; our calibration builds on these + deterministic VIS/CIG
- LCB (lowest cloud base), MHT (mixing height), TMP/DPT (temp/dew F), WSP/GST (kt) available as extra features.
- Sanity: CYQI's overnight block showed VIS 0.1–0.3 mi with IFC 60–70% — NBM actively forecasting dense fog at our foggiest curated field.

## Deferred / planner-phase sources

- **LAMP**: tgftp 403; NBH (hourly updates, hourly resolution) covers the 1–25 h need. Revisit only if the scorecard exposes a near-term skill gap (pre-registered as an option, not a commitment).
- **CPC 6-10/8-14 day outlooks**: `https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst/610temp_latest.zip` verified (3.3 MB shapefile). US-only; planner phase.

## Calibration data plan (feeds chunk 1)

Model per threshold T ∈ {vis<1mi, vis<½mi, vis<¼mi, sub-CAT-I(vis|ceil)}: logistic on
NBM features (VIS, CIG, IFV, IFC, spread, wind) + per-airport climatology logit (month×hour base rate) + lead. Truth from our METAR archive (same rules as the atlas bands). Initial fit on a pilot hindcast sample (archived collectives above); **shadow-mode live scoring gates go-live per airport regardless** (pre-registered bar: beat climatology Brier).
