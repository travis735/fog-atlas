import { defineConfig } from "vite";
import { gunzipSync } from "node:zlib";

// dev-only parity with the Pages Functions at /api/metar and /api/now
export default defineConfig({
  plugins: [{
    name: "dev-api-now",
    configureServer(server) {
      server.middlewares.use("/api/now", async (_req, res) => {
        try {
          const r = await fetch("https://aviationweather.gov/data/cache/metars.cache.csv.gz");
          const gz = new Uint8Array(await r.arrayBuffer());
          res.setHeader("content-type", "text/plain");
          res.end(gunzipSync(gz));
        } catch (e) {
          res.statusCode = 502;
          res.end(String(e));
        }
      });
    },
  }],
  server: {
    proxy: {
      "/api/metar": {
        target: "https://aviationweather.gov",
        changeOrigin: true,
        rewrite: (path) =>
          path.replace(/^\/api\/metar/, "/api/data/metar") + "&format=json" +
          (path.includes("hours=") ? "" : "&hours=3"),
      },
    },
  },
});
