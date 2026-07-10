# Fog Atlas

> A worldwide map of where the sky closes — ten years of METAR observations distilled into when, where, and how often airports drop below approach minima, and how much of that an EFVS-equipped aircraft gets back.

Enhanced Flight Vision Systems (EFVS) let suitably equipped aircraft fly approaches in visibility that would otherwise force a diversion. The value of that equipment is concentrated at a specific kind of airport: one that is frequently fogbound **and** lacks CAT II/III ILS infrastructure. Fog Atlas maps that intersection from public data.

## What it shows

Every METAR observation is classified into one of three visibility bands:

| Band | Prevailing visibility | Meaning |
|---|---|---|
| Normal ops | ≥ 800 m (½ SM) | Conventional CAT I approach workable |
| **EFVS-recoverable** | 300–800 m | Below typical CAT I minima, but within EFVS-usable range |
| Below all | < 300 m | Too low for EFVS too; CAT III autoland territory |

Aggregated by airport × month × hour-of-day over a 10-year archive, tagged by cause (fog, mist, haze, smoke, snow), and cross-referenced with CAT II/III capability.

## Data sources

- **METAR archive:** [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/request/download.phtml) (IEM) ASOS/METAR archive, backfilled from NOAA ISD for stations IEM covers thinly.
- **Airports:** OurAirports open data.
- **CAT II/III capability:** FAA CIFP for US airports; hand-curated list for international (see methodology for confidence levels).
- **Cancellations (US validation layer):** US DOT Bureau of Transportation Statistics on-time performance data.

## Honest limitations

METAR prevailing visibility is a proxy for RVR, not a substitute. Band thresholds are global approximations, not per-runway minima. International station data quality varies. CAT II/III flags outside the US are curated, not authoritative. Full methodology and assumptions: [`METHODOLOGY.md`](METHODOLOGY.md).

## Layout

- `pipeline/` — Python + DuckDB ingestion and aggregation (offline; produces static JSON)
- `app/` — static web app (Vite + MapLibre GL + Observable Plot)

## Live

**https://fog-atlas.pages.dev** — 3,394 airports × 10 years (2016–2025), visibility + ceiling bands, a live fog-chase board for EFVS testing (per-runway ALS/RVR/minima filters from FAA NASR, still-air ETE from a chosen base, 3-minute live strata + nowcast), validated against documented climatology, runway-measured RVR (SFO cross-check), and 13.9M US flight records (flights scheduled during EFVS-recoverable hours were weather-cancelled at 5.2× baseline).
