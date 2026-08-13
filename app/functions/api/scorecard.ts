// Serve the shadow-verification scorecard (written by score_shadow.py runs)
// from KV, edge-cached 1 h — it changes at most daily.
interface Env { FOGCAST: KVNamespace }

export const onRequest: PagesFunction<Env> = async (ctx) => {
  const cacheKey = new Request(new URL("/api/scorecard", ctx.request.url).toString());
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  const body = await ctx.env.FOGCAST.get("scorecard", "stream");
  if (!body) {
    return new Response(JSON.stringify({ error: "no scorecard yet" }), {
      status: 503,
      headers: { "content-type": "application/json", "cache-control": "public, max-age=300" },
    });
  }
  const res = new Response(body, {
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=3600",
      "access-control-allow-origin": "*",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, res.clone()));
  return res;
};
