// Same-origin proxy for NOAA AWC current METARs (AWC sends no CORS headers,
// so the browser can't fetch them directly). Cached at the edge for 2 min.
export const onRequestGet: PagesFunction = async ({ request }) => {
  const url = new URL(request.url);
  const ids = url.searchParams.get("ids") ?? "";
  if (!/^[A-Z0-9]{4}$/.test(ids)) return new Response("bad id", { status: 400 });
  // hours of history (for the nowcast model's trend features), capped small
  const hours = Math.min(parseInt(url.searchParams.get("hours") ?? "1", 10) || 1, 6);
  const upstream = await fetch(
    `https://aviationweather.gov/api/data/metar?ids=${ids}&hours=${hours}&format=json`,
    { cf: { cacheTtl: 120, cacheEverything: true } } as RequestInit,
  );
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=120",
    },
  });
};
