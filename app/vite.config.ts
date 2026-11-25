import { defineConfig } from "vite";
import type { IncomingMessage, ServerResponse } from "node:http";
import { onRequestGet as nowFn } from "./functions/api/now.ts";
import { onRequestGet as metarFn } from "./functions/api/metar.ts";

// Dev-only: /api/now and /api/metar are answered by the SAME Pages Function
// code that runs in production. They used to be re-implemented here, and the
// copies drifted: the dev proxy for /api/metar forwarded any query, so the
// CHASE board's 150-airport request worked on a laptop and got 400 "bad id"
// from the deployed function for four months (2026-07-10 to 2026-11-24).
// (`cf` fetch options are ignored by Node, so nothing is edge-cached in dev.)
type PagesGet = (ctx: { request: Request }) => Response | Promise<Response>;
const pagesFunction = (fn: PagesGet) => async (req: IncomingMessage & { originalUrl?: string }, res: ServerResponse) => {
  try {
    const r = await fn({ request: new Request(new URL(req.originalUrl ?? req.url ?? "/", "http://localhost")) });
    res.statusCode = r.status;
    r.headers.forEach((v, k) => res.setHeader(k, v));
    res.end(Buffer.from(await r.arrayBuffer()));
  } catch (e) {
    res.statusCode = 500;
    res.end(String(e));
  }
};

export default defineConfig({
  plugins: [{
    name: "dev-pages-functions",
    configureServer(server) {
      server.middlewares.use("/api/now", pagesFunction(nowFn as unknown as PagesGet));
      server.middlewares.use("/api/metar", pagesFunction(metarFn as unknown as PagesGet));
    },
  }],
});
