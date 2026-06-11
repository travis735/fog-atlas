# Methodology & Assumptions

Fog Atlas trades per-runway precision for honest, worldwide comparability. Every approximation is listed here. If you need certified minima analysis for a specific operation, use your AFM, OpSpecs, and Jeppesen — not this site.

## Observation basis

- **Source:** routine hourly METARs (IEM ASOS archive; `report_type=3`), 2016–2025. Special reports (SPECI) are deliberately excluded so that frequencies are an unbiased sample of hours; including SPECIs would overweight deteriorating weather.
- **Hourly dedup:** the last routine report in each UTC hour (US stations file at ~:53–:56; many international stations file at :00/:30).
- **Statistic:** "% of hours" = hours in band ÷ hours with a valid visibility report. Station outages reduce the denominator, not the frequency. Coverage is reported per airport.

## Bands (visibility OR ceiling)

A CAT I approach needs both visibility above minima **and** ceiling at or above the ~200 ft decision height, so both terms enter the classification (ceiling = lowest BKN/OVC/VV layer from the sky-condition fields, joined per hour):

| Band | Threshold | Rationale |
|---|---|---|
| Normal | vis ≥ ½ SM (~800 m) and ceiling ≥ 200 ft | CAT I workable |
| EFVS-recoverable | vis 300–800 m, **or** ceiling < 200 ft with workable visibility | Below CAT I minima but within the range where EFVS operations (e.g. FAA 91.176) commonly remain workable; a thin low deck is exactly what EFVS sees through |
| Below all | vis < 300 m | Approaching CAT III / RVR-1000-and-below territory; EFVS dispatch value assumed nil. Ceiling-only events never land here |

Empirical note: ceiling-only hours (vis fine, ceiling < 200 ft) are rare everywhere — single digits to a few dozen hours per year even at stratus-prone airports — because in fog, visibility and ceiling collapse together and the visibility term already catches the hour. The famous counterexample cuts the other way: San Francisco's marine stratus sits at 500–1,500 ft, well above the decision height, so SFO shows almost no sub-CAT-I hours **even with ceilings included**. Its fog problem is arrival *capacity* (no paired visual approaches under the deck), not approach minima — a distinction this map now makes verifiably rather than by omission.

Known approximations:

1. **Prevailing visibility ≠ RVR.** METAR visibility is a human/sensor prevailing value for the aerodrome; RVR is runway-specific and often better than prevailing visibility in fog (high-intensity lights). Our bands therefore *understate* what's flyable on a lit CAT I runway and the split should be read as a climatological index, not an ops decision.
2. **Thresholds are global constants.** Real minima vary per runway, per approach, per operator. ½ SM / 300 m are defensible central values, not authoritative ones.
3. **Reporting granularity.** US ASOS reports fractions (¼ SM = ~400 m falls in the EFVS band; ⅛ SM = ~200 m falls below). International METARs report meters with their own steps. Band edges sit between common reporting steps where possible.

## Cause attribution

Present-weather codes, first match in priority order: `FG` (incl. FZFG) → `SN` → `HZ`/`FU` → `BR` → other/none. An observation with multiple phenomena is attributed to the highest-priority one.

The app's cause chart folds `BR` (mist) into the fog family: by definition `BR` is reported when visibility is ≥ 800 m, so a `BR` code attached to a sub-CAT-I observation is fog that the observer/algorithm coded conservatively. The pipeline output keeps the raw distinction.

## Reporting reliability

"% of hours" assumes observations sample hours impartially. Two failure modes are detected per station and flagged:

1. **Low coverage** (< 40% of possible hours in the archive): too thin to support frequency claims.
2. **Suspect reporting** — the signature of an encoding artifact rather than weather: a majority of sub-CAT-I observations at literal-zero visibility combined with no diurnal structure (real fog is strongly morning-skewed; haze and marine advection fog are flatter, so the threshold is deliberately loose).

Flagged stations are excluded from the fog field and rankings, demoted on the map, and carry a warning banner in their deep-dive. Their raw numbers remain visible for completeness.

## CAT II/III capability

- **US:** derived from the FAA NASR ILS database (`ILS_BASE.csv` CATEGORY field, best category per airport) — 97 airports with CAT II/III.
- **International:** the FAA's published OpSpec C060 list of foreign facilities approved for CAT II/III operations — 158 airports. Everything else is **assumed CAT I** and labeled accordingly; the C060 list reflects FAA approval for US carriers, so a foreign airport with CAT II/III capability not used by US carriers may be missing.
- The EFVS-value framing: at a CAT III airport, suitably equipped airlines already land in fog — EFVS value concentrates where low visibility is frequent *and* CAT II/III is absent.

## Cancellation validation (US only)

US DOT/BTS on-time data (2023–2024, 13.9M scheduled departures) joined to our hourly bands at each flight's origin airport:

| Band at scheduled departure hour | Flights | Weather-cancel rate |
|---|---|---|
| Normal | 13,401,786 | 0.73% |
| EFVS-recoverable | 40,759 | 3.81% — **5.2× baseline** |
| Below 300 m | 11,637 | 2.20% — 3.0× baseline |

Two honest readings of the structure. First, the below-300m multiplier being *lower* than the EFVS band's looks backwards until you notice where that exposure lives: mostly at CAT III-equipped hubs (ORD, DTW, SEA) where autoland keeps the operation running — which is the CAT II/III-absence thesis showing up in cancellation data. Second, BTS "weather" cancellations are generic: Denver's high multiplier is blizzard-driven, not fog-driven; treat the aggregate as validation that the bands mark operationally hostile hours, not as a fog-specific cost model. JFK's *below-baseline* rate during sub-CAT-I hours (0.05%) likely reflects proactive schedule thinning being coded as carrier/NAS rather than weather.

## Sources

- Iowa Environmental Mesonet ASOS/METAR archive (primary)
- NOAA ISD (international backfill, planned)
- OurAirports (airport metadata)
- FAA CIFP (US approach capability)
- US DOT BTS (US cancellations, planned)
