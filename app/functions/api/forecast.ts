// Serve the current V3 forecast JSON from KV, edge-cached 5 minutes so KV
// reads stay bounded regardless of traffic (one KV read per pop per 5 min).
interface Env { FOGCAST: KVNamespace }

export const onRequest: PagesFunction<Env> = async (ctx) => {
  const cacheKey = new Request(new URL("/api/forecast", ctx.request.url).toString());
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  const body = await ctx.env.FOGCAST.get("current", "stream");
  if (!body) {
    return new Response(JSON.stringify({ error: "no forecast yet" }), {
      status: 503,
      headers: { "content-type": "application/json", "cache-control": "public, max-age=60" },
    });
  }
  const res = new Response(body, {
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=300",
      "access-control-allow-origin": "*",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, res.clone()));
  return res;
};
