// Serve the DEPLOY planner data (14-day expected chaseable hours) from KV,
// edge-cached 30 min — it rebuilds daily, so reads stay tiny.
interface Env { FOGCAST: KVNamespace }

export const onRequest: PagesFunction<Env> = async (ctx) => {
  const cacheKey = new Request(new URL("/api/deploy", ctx.request.url).toString());
  const cache = caches.default;
  const hit = await cache.match(cacheKey);
  if (hit) return hit;

  const body = await ctx.env.FOGCAST.get("deploy", "stream");
  if (!body) {
    return new Response(JSON.stringify({ error: "no deploy data yet" }), {
      status: 503,
      headers: { "content-type": "application/json", "cache-control": "public, max-age=60" },
    });
  }
  const res = new Response(body, {
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=1800",
      "access-control-allow-origin": "*",
    },
  });
  ctx.waitUntil(cache.put(cacheKey, res.clone()));
  return res;
};
