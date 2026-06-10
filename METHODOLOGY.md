# Methodology & Assumptions

Fog Atlas trades per-runway precision for honest, worldwide comparability. Every approximation is listed here. If you need certified minima analysis for a specific operation, use your AFM, OpSpecs, and Jeppesen — not this site.

## Observation basis

- **Source:** routine hourly METARs (IEM ASOS archive; `report_type=3`), 2016–2025. Special reports (SPECI) are deliberately excluded so that frequencies are an unbiased sample of hours; including SPECIs would overweight deteriorating weather.
- **Hourly dedup:** the last routine report in each UTC hour (US stations file at ~:53–:56; many international stations file at :00/:30).
- **Statistic:** "% of hours" = hours in band ÷ hours with a valid visibility report. Station outages reduce the denominator, not the frequency. Coverage is reported per airport.

## Visibility bands

| Band | Threshold | Rationale |
|---|---|---|
| Normal | ≥ ½ SM (~800 m) | At or above typical CAT I visibility minima |
| EFVS-recoverable | 300–800 m | Below CAT I, but within the range where EFVS operations (e.g. FAA 91.176) commonly remain workable |
| Below all | < 300 m | Approaching CAT III / RVR-1000-and-below territory; EFVS dispatch value assumed nil |

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

- **US:** derived from FAA CIFP approach data (machine-readable, authoritative).
- **International:** hand-curated from AIPs and industry references for ~150–200 airports; everything else is **assumed CAT I** and labeled accordingly. Confidence level is shown per airport.
- The EFVS-value framing: at a CAT III airport, suitably equipped airlines already land in fog — EFVS value concentrates where low visibility is frequent *and* CAT II/III is absent.

## Cancellation validation (US only, phase 1.5)

US DOT BTS on-time data provides cancellations with a generic "weather" cause — not fog-specific. We use it only as a correlation check (cancellation rate on sub-CAT-I mornings vs. baseline), never as a global layer.

## Sources

- Iowa Environmental Mesonet ASOS/METAR archive (primary)
- NOAA ISD (international backfill, planned)
- OurAirports (airport metadata)
- FAA CIFP (US approach capability)
- US DOT BTS (US cancellations, planned)
