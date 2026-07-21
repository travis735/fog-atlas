
(async function () {
  const S = window.__FOG;
  const $ = (id) => document.getElementById(id);
  const mon = new Date().getMonth();
  const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];

  // climatology strip for the current month (always available, crawler-stable data)
  const rates = S.clim[mon];
  const peak = Math.max(...rates, 1);
  const bars = rates.map((r, h) => {
    const y = 46 - Math.round(44 * r / peak);
    return `<rect x="${h * 28 + 1}" y="${y}" width="26" height="${46 - y}" rx="2" fill="#3f76a3" opacity="${r > 0 ? 0.9 : 0.25}"><title>${String(h).padStart(2, "0")}:00 — ${r}% of ${MONTHS[mon]} hours</title></rect>`;
  }).join("");
  $("strip").innerHTML = `<svg viewBox="0 0 672 48" preserveAspectRatio="none">${bars}</svg>
    <div class="striplab"><span>midnight</span><span>${MONTHS[mon]} · typical hours with ${S.climLabel} (local time)</span><span>11 pm</span></div>`;
  const mh = rates.reduce((a, b) => a + b, 0) / 100 * 30;
  $("ctx").textContent = `${MONTHS[mon]} typically brings ${mh < 1 ? "under an hour" : Math.round(mh) + " hours"} of ${S.climLabel} conditions here.`;

  // live conditions
  try {
    const arr = await (await fetch(`/api/metar?ids=${S.icao}&hours=3`)).json();
    const ob = arr && arr[0];
    if (ob) {
      const vis = typeof ob.visib === "string" ? parseFloat(ob.visib) || 10 : ob.visib;
      const foggy = vis != null && vis < 1.0;
      const when = (ob.rawOb || "").match(/\d{6}Z/) || [""];
      $("verdict").innerHTML = foggy
        ? `<b>Fog now</b> — visibility ${vis} mile${vis === 1 ? "" : "s"} at ${S.icao} (${when[0]}).`
        : `<span class="ok">No fog right now</span> — visibility ${vis == null ? "unknown" : vis + " mi"} at ${S.icao} (${when[0]}).`;
    }
  } catch (e) { $("verdict").textContent = "Live conditions unavailable."; }

  // forecast layer — probabilities only when the engine says public
  if (!S.covered) return;
  try {
    const fc = await (await fetch("/api/forecast")).json();
    const a = fc.airports && fc.airports[S.icao];
    if (!a) return;
    if (fc.meta && fc.meta.public === true) {
      const peakP = Math.max(...a.p.map((r) => r[0]));
      const idx = a.p.findIndex((r) => r[0] === peakP);
      const at = new Date(new Date(fc.meta.cycle).getTime() + a.fhrs[idx] * 3600e3);
      const t = at.toLocaleString("en-US", { weekday: "short", hour: "numeric", timeZone: S.tz });
      $("verdict").innerHTML += peakP >= 20
        ? ` <b>Fog likely ${t}</b> — ${peakP}% chance of visibility under a mile.`
        : ` Next 48 h: fog unlikely (peak ${peakP}%).`;
    } else {
      // shadow mode: NWS guidance wording, no model percentages
      const minVis = Math.min(...a.vis.filter((v) => v != null));
      const maxLiv = Math.max(...a.liv.filter((v) => v != null), 0);
      if (minVis < 1 || maxLiv >= 40)
        $("verdict").innerHTML += " NWS guidance suggests <b>fog is possible in the next 48 hours</b> — calibrated probabilities publish here after verification.";
    }
  } catch (e) { /* forecast layer optional */ }
})();
