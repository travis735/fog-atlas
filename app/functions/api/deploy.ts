// Serve the DEPLOY planner data (14-day expected chaseable hours) from KV.
//
// The payload is rebuilt on every hourly forecast tick (since 2026-11-25; it
// was built once a day and cached here for 30 minutes, so an hourly rebuild
// could sit unseen for half its life). The edge keeps a copy for 5 minutes
// and a browser for 1: with KV's own propagation (about a minute) a new build
// is on screen within about 10 minutes, and KV reads stay bounded at one per
// edge location per 5 minutes however many people have the panel open.
//
// Errors are never cached anywhere (no-store). The 503 used to go out as
// "public, max-age=60", so a browser could keep answering "no deploy data"
// from its own cache for a minute after the key was back; and a KV read that
// threw escaped as an unhandled exception instead of an answer.
interface Env { FOGCAST: KVNamespace }

const HEADERS = { "content-type": "application/json", "access-control-allow-origin": "*" };

function unavailable(status: number, error: string): Response {
  return new Response(JSON.stringify({ error }), {
    status,
    headers: { ...HEADERS, "cache-control": "no-store" },
  });
}

export const onRequest: PagesFunction<Env> = async (ctx) => {
  const cacheKey = new Request(new URL("/api/deploy", ctx.request.url).toString());
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  let body: ReadableStream | null;
  try {
    // streamed, never parsed here: the payload is ~450 KB and the build
    // (forecast.yml) already read it back from KV before calling it live
    body = await ctx.env.FOGCAST.get("deploy", "stream");
  } catch (e) {
    return unavailable(502, `planner data unreadable: ${(e as Error)?.message ?? e}`);
  }
  if (!body) return unavailable(503, "no deploy data yet");

  const res = new Response(body, {
    headers: {
      ...HEADERS,
      // s-maxage is the edge copy (cache.put below honours it), max-age the browser's
      "cache-control": "public, max-age=60, s-maxage=300",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, res.clone()));
  return res;
};
