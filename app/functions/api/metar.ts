// Same-origin proxy for NOAA AWC current METARs (AWC sends no CORS headers,
// so the browser can't fetch them directly). Cached at the edge for 2 min.
//
// ids is one ICAO id or a comma list. Until 2026-11-24 this accepted exactly
// one id, while the CHASE board's history warm-start (main.ts warmChaseHist)
// had been sending up to 150 since 2026-07-10: every one of those calls got
// 400 "bad id", the caller swallowed it, and the nowcast ran without its
// trend inputs. The dev proxy forwarded anything, so it worked on a laptop.
//
// MAX_IDS: AWC answers at most 400 METARs per request, newest first, and drops
// the rest silently (measured 2026-11-24: 150 ids x 3 h came back as exactly
// 400 entries, 130 stations short of history; 50 ids x 3 h came back as 176
// to 188, complete). Callers that need history chunk their requests
// (METAR_CHUNK in app/src/feed.ts, which must not exceed this).
const MAX_IDS = 50;
const ICAO = /^[A-Z0-9]{4}$/;

const refuse = (status: number, why: string) =>
  new Response(why, { status, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });

export const onRequestGet: PagesFunction = async ({ request }) => {
  const url = new URL(request.url);
  const ids = (url.searchParams.get("ids") ?? "").split(",");
  if (!ids.every((id) => ICAO.test(id))) return refuse(400, "bad id");
  if (ids.length > MAX_IDS) return refuse(400, `too many ids (max ${MAX_IDS})`);
  // hours of history (for the nowcast model's trend features), capped small
  const hours = Math.min(parseInt(url.searchParams.get("hours") ?? "1", 10) || 1, 6);
  let upstream: Response;
  try {
    upstream = await fetch(
      `https://aviationweather.gov/api/data/metar?ids=${ids.join(",")}&hours=${hours}&format=json`,
      { cf: { cacheTtl: 120, cacheEverything: true } } as RequestInit,
    );
  } catch {
    return refuse(502, "upstream unavailable: the request to aviationweather.gov failed");
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": "application/json",
      // an upstream error must not sit in the browser cache for two minutes
      "cache-control": upstream.ok ? "public, max-age=120" : "no-store",
    },
  });
};
