import type { Env } from "./types";
import { CORS_HEADERS } from "./types";
import { getDb } from "./db";
import { clientIP, json, html } from "./utils";
import { recordSnapshot, materializeStats, enrichActors } from "./enrichment";
import { refreshModeration } from "./cron";
import { handleSearch } from "./handlers/search";
import { handleIngest } from "./handlers/ingest";
import { handleDelete, handleCursor, handleRequestIndexing, handleModOverrideSet, handleModOverrideDelete, handleModOverrideList, handleDomainBlockSet, handleDomainBlockDelete, handleDomainBlockList, handleEnrichProfiles } from "./handlers/admin";
import { handleGetProfiles } from "./handlers/profiles";
import { handleStats } from "./handlers/stats";
import { handleOgImage } from "./handlers/og";
import { handleReputation } from "./handlers/reputation";
import { indexPage } from "./pages/home";
import { docsPage } from "./pages/docs";
import { llmsTxt } from "./pages/llms";
import { reputationPage } from "./pages/reputation";

export default {
  async scheduled(_event: ScheduledEvent, env: Env, _ctx: ExecutionContext): Promise<void> {
    const db = await getDb(env);
    // Record the ATTEMPT before any work. Without this a missing snapshot row is
    // ambiguous: it could mean the cron never fired (Cloudflare schedules are
    // best-effort) or that recordSnapshot itself failed. Those need different
    // fixes, and there was no way to tell them apart after the fact.
    await env.KV.put("cron_last_attempt", String(Date.now())).catch(() => {});
    // isolate each step: a slow/failing one (e.g. recordSnapshot when the table
    // grew past a cheap COUNT) must never take down the rest — that cascade is
    // what froze the /stats page from 2026-05-18.
    const steps: [string, () => Promise<unknown>][] = [
      ["recordSnapshot", () => recordSnapshot(db)],
      ["materializeStats", () => materializeStats(db, env)],
      ["refreshModeration", () => refreshModeration(db, env)],
      ["enrichActors", () => enrichActors(db, env)],
    ];
    for (const [name, fn] of steps) {
      try {
        await fn();
      } catch (e: any) {
        console.log(JSON.stringify({ event: "cron_step_failed", step: name, error: e?.message }));
      }
    }
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const { pathname } = new URL(request.url);

    if (pathname === "/" && request.method === "GET") {
      return html(indexPage());
    }
    // atproto handle verification for the service's own account
    // (SERVICE_DID is a wrangler var; set once the account exists)
    if (pathname === "/.well-known/atproto-did" && request.method === "GET") {
      if (!env.SERVICE_DID) return new Response("not found", { status: 404 });
      return new Response(env.SERVICE_DID, {
        headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300" },
      });
    }
    if (pathname === "/llms.txt" && request.method === "GET") {
      return new Response(llmsTxt(), {
        headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=3600", ...CORS_HEADERS },
      });
    }
    if (pathname === "/og-image" && request.method === "GET") {
      return handleOgImage(request, env);
    }
    if (pathname === "/docs" && request.method === "GET") {
      return html(docsPage());
    }
    if (pathname === "/reputation" && request.method === "GET") {
      return html(reputationPage());
    }
    if (pathname === "/admin/cursor" && request.method === "GET") {
      return handleCursor(request, env);
    }
    if (pathname === "/request-indexing" && request.method === "GET") {
      return new Response(null, { status: 302, headers: { Location: "/" } });
    }

    const db = await getDb(env);

    if (pathname === "/stats" && request.method === "GET") {
      return handleStats(request, db, env, ctx);
    }

    if (pathname === "/debug/reputation" && request.method === "GET") {
      return handleReputation(request, db);
    }

    if (pathname === "/request-indexing" && request.method === "POST") {
      const isAdmin = request.headers.get("Authorization") === `Bearer ${env.ADMIN_SECRET}`;
      if (!isAdmin) {
        const ip = clientIP(request);
        const { success } = await env.RATE_LIMITER.limit({ key: `index:${ip}` });
        if (!success) {
          console.log(JSON.stringify({ event: "rate_limited", endpoint: "/request-indexing", ip }));
          return json({ error: "slow down — try again in a minute." }, 429);
        }
      }
      return handleRequestIndexing(request, db, env);
    }

    if (
      (pathname === "/xrpc/tech.waow.typeahead.searchActors" ||
        pathname === "/xrpc/app.bsky.actor.searchActorsTypeahead") &&
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

    if (
      pathname === "/xrpc/app.bsky.actor.getProfiles" &&
      request.method === "GET"
    ) {
      const ip = clientIP(request);
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) {
        return json({ error: "rate limited" }, 429);
      }
      return handleGetProfiles(request, db, env, ctx);
    }

    if (pathname === "/admin/ingest" && request.method === "POST") {
      return handleIngest(request, db, env, ctx);
    }

    if (pathname === "/admin/delete" && request.method === "POST") {
      return handleDelete(request, db, env, ctx);
    }

    if (pathname === "/admin/mod-override" && request.method === "POST") {
      return handleModOverrideSet(request, db, env);
    }
    if (pathname === "/admin/mod-override" && request.method === "DELETE") {
      return handleModOverrideDelete(request, db, env);
    }
    if (pathname === "/admin/mod-overrides" && request.method === "GET") {
      return handleModOverrideList(request, db, env);
    }

    if (pathname === "/admin/domain-block" && request.method === "POST") {
      return handleDomainBlockSet(request, db, env, ctx);
    }
    if (pathname === "/admin/domain-block" && request.method === "DELETE") {
      return handleDomainBlockDelete(request, db, env, ctx);
    }
    if (pathname === "/admin/domain-blocks" && request.method === "GET") {
      return handleDomainBlockList(request, db, env);
    }

    if (pathname === "/admin/enrich-profiles" && request.method === "POST") {
      return handleEnrichProfiles(request, db, env);
    }

    return json({ error: "not found" }, 404);
  },
};
