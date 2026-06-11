// Worldwide current METARs in one request: NOAA AWC's rolling cache file
// (~265 KB gz, all ~5k reporting stations, refreshed every minute upstream).
// Edge-cached 3 min so AWC sees ~20 req/h from us total, regardless of
// visitor count. Decompressed here so the browser gets plain CSV.
export const onRequestGet: PagesFunction = async () => {
  const upstream = await fetch(
    "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
    { cf: { cacheTtl: 180, cacheEverything: true } } as RequestInit,
  );
  if (!upstream.ok || !upstream.body) {
    return new Response("upstream unavailable", { status: 502 });
  }
  let body: ReadableStream = upstream.body;
  // the .gz is gzip CONTENT (not transfer encoding) — decompress unless
  // the runtime already did
  const head = upstream.headers.get("content-encoding") ?? "";
  if (!head.includes("gzip")) {
    try {
      body = upstream.body.pipeThrough(new DecompressionStream("gzip"));
    } catch {
      body = upstream.body;
    }
  }
  return new Response(body, {
    headers: {
      "content-type": "text/csv; charset=utf-8",
      "cache-control": "public, max-age=180",
    },
  });
};
