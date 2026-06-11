import { defineConfig } from "vite";

// dev-only parity with the Pages Function at /api/metar
export default defineConfig({
  server: {
    proxy: {
      "/api/metar": {
        target: "https://aviationweather.gov",
        changeOrigin: true,
        rewrite: (path) =>
          path.replace(/^\/api\/metar/, "/api/data/metar") + "&format=json",
      },
    },
  },
});
