// Worldwide current METARs in one request: NOAA AWC's rolling cache file
// (~265 KB gz, all ~5k reporting stations, refreshed every minute upstream).
// Edge-cached 3 min so AWC sees ~20 req/h from us total, regardless of
// visitor count. Decompressed here so the browser gets plain CSV.
//
// Every failure answers 502 with cache-control: no-store. The board treats
// anything but a 2xx as "feed down" and retries every 60 s; a cacheable error
// would keep it down after the upstream had recovered. A fetch that THROWS
// (DNS, connection reset) used to escape this handler as an unhandled
// exception; it is the same outage and gets the same 502.
const unavailable = (why: string) =>
  new Response(`upstream unavailable: ${why}`, {
    status: 502,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });

export const onRequestGet: PagesFunction = async () => {
  let upstream: Response;
  try {
    upstream = await fetch(
      "https://aviationweather.gov/data/cache/metars.cache.csv.gz",
      { cf: { cacheTtl: 180, cacheEverything: true } } as RequestInit,
    );
  } catch {
    return unavailable("the request to aviationweather.gov failed");
  }
  if (!upstream.ok || !upstream.body) {
    return unavailable(`aviationweather.gov answered HTTP ${upstream.status}`);
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
      // text/plain, not text/csv: Cloudflare compresses responses to visitors
      // only for a fixed list of content types, and text/csv is not on it
      // (developers.cloudflare.com/speed/optimization/content/compression,
      // read 2026-11-25). As text/csv this went out at 985,018 bytes a read,
      // about 20 MB an hour on a healthy board and 60 MB an hour on a failing
      // one over a phone tether, while /api/deploy (application/json) left
      // the same edge brotli-compressed at an eighth of its size. The body is
      // the same CSV; no reader looks at the type.
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=180",
    },
  });
};
