import type { Env } from "./types";
import { CORS_HEADERS } from "./types";
import { createDb } from "./db";
import { clientIP, json, html } from "./utils";
import { recordSnapshot } from "./enrichment";
import { enrichActors } from "./enrichment";
import { refreshModeration } from "./cron";
import { handleSearch } from "./handlers/search";
import { handleIngest } from "./handlers/ingest";
import { handleDelete, handleCursor, handleRequestIndexing } from "./handlers/admin";
import { handleStats } from "./handlers/stats";
import { indexPage } from "./pages/home";
import { docsPage } from "./pages/docs";

export default {
  async scheduled(_event: ScheduledEvent, env: Env, _ctx: ExecutionContext): Promise<void> {
    const db = createDb(env);
    await recordSnapshot(db);
    await refreshModeration(db, env);
    await enrichActors(db, env);
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const { pathname } = new URL(request.url);

    if (pathname === "/" && request.method === "GET") {
      return html(indexPage());
    }
    if (pathname === "/docs" && request.method === "GET") {
      return html(docsPage());
    }
    if (pathname === "/admin/cursor" && request.method === "GET") {
      return handleCursor(request, env);
    }
    if (pathname === "/request-indexing" && request.method === "GET") {
      return new Response(null, { status: 302, headers: { Location: "/" } });
    }

    const db = createDb(env);

    if (pathname === "/stats" && request.method === "GET") {
      return handleStats(db);
    }

    if (pathname === "/request-indexing" && request.method === "POST") {
      const ip = clientIP(request);
      const { success } = await env.RATE_LIMITER.limit({ key: `index:${ip}` });
      if (!success) {
        console.log(JSON.stringify({ event: "rate_limited", endpoint: "/request-indexing", ip }));
        return json({ error: "slow down — try again in a minute." }, 429);
      }
      return handleRequestIndexing(request, db, env);
    }

    if (
      pathname === "/xrpc/app.bsky.actor.searchActorsTypeahead" &&
      request.method === "GET"
    ) {
      const ip = clientIP(request);
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) {
        console.log(JSON.stringify({ event: "rate_limited", endpoint: "/search", ip }));
        return json({ error: "rate limited" }, 429);
      }
      return handleSearch(request, db, env, ctx);
    }

    if (pathname === "/admin/ingest" && request.method === "POST") {
      return handleIngest(request, db, env, ctx);
    }

    if (pathname === "/admin/delete" && request.method === "POST") {
      return handleDelete(request, db, env, ctx);
    }

    return json({ error: "not found" }, 404);
  },
};
