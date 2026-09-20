import type { Env } from "./types";
import { CORS_HEADERS, FAVICON_SVG } from "./types";
import { getDb } from "./db";
import { clientIP, json, html } from "./utils";
import { recordSnapshot, materializeStats } from "./enrichment";
import { endpointLabel, recordRequest, trafficDomain } from "./metrics";
import { refreshModeration } from "./cron";
import { handleSearch } from "./handlers/search";
import { handleIngest } from "./handlers/ingest";
import { handleDelete, handleCursor, handleRequestIndexing, handleModOverrideSet, handleModOverrideDelete, handleModOverrideList, handleDomainBlockSet, handleDomainBlockDelete, handleDomainBlockList, handleEnrichProfiles } from "./handlers/admin";
import { handleGetProfiles, handleGetProfile } from "./handlers/profiles";
import { handleStats } from "./handlers/stats";
import { handleOgImage, handleAppleTouchIcon, handleWebManifest } from "./handlers/og";
import { handleFreshness, measureFreshness, notifyFreshnessChange, postWebhook } from "./handlers/freshness";
import { handleReputation } from "./handlers/reputation";
import { indexPage } from "./pages/home";
import { docsPage } from "./pages/docs";
import { llmsTxt } from "./pages/llms";
import { reputationPage } from "./pages/reputation";

/** Cloudflare kills a scheduled invocation at 15 minutes of wall time, with
 *  no exception and no log. A step is only started while this much of the
 *  run is left, so every run ends with a `cron_done` line — or a
 *  `cron_budget_exhausted` one naming what was skipped. */
export const CRON_BUDGET_MS = 12 * 60 * 1000;

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
    const steps: [string, () => Promise<void>][] = [
      // FIRST, deliberately.
      //
      // This is three HTTP calls, and it is the check that exists to catch the
      // system failing silently. Last in the chain it took ~7 minutes to run
      // (measured 2026-07-31: cron at 18:00:51, freshness at 18:07:51) and any
      // hang in the enrichment step that used to follow (up to 5000 DIDs
      // through getProfiles) meant it never ran at all. A watchdog positioned
      // behind the thing most likely to wedge is not a watchdog.
      //
      // It also measures a more honest number here: the steady state left by
      // the previous hour, rather than the replica moments after this cron
      // wrote a batch into it.
      //
      // It never fixes anything — the repair (restart the search service) is
      // not the worker's to make. It reports, and something else acts.
      ["freshness", async () => {
        const f = await measureFreshness(env);
        console.log(JSON.stringify({ event: f.stale ? "freshness_stale" : "freshness_ok", ...f }));
        // notify BEFORE storing: the notifier compares against the previous run
        await notifyFreshnessChange(env, f);
        await env.KV.put("freshness", JSON.stringify({ ...f, at: Date.now() })).catch(() => {});
      }],
      ["recordSnapshot", () => recordSnapshot(db)],
      ["materializeStats", () => materializeStats(db, env)],
      ["refreshModeration", () => refreshModeration(db, env)],
      // no enrichment here. The identity + profile passes that used to run
      // last were killed by the 15-minute wall in 120 of 140 runs
      // (2026-08-27..09-03), silently: a wall kill is not an exception, so
      // cron_step_failed never fired. They live on heavypad now
      // (typeahead-identity-hourly, typeahead-enrich-backfill), where an
      // hour is an hour.
    ];
    const t_run = Date.now();
    const phases: Record<string, number> = {};
    for (const [name, fn] of steps) {
      const elapsed = Date.now() - t_run;
      if (elapsed > CRON_BUDGET_MS) {
        // say so, loudly: skipping is the honest alternative to being killed
        console.log(JSON.stringify({ event: "cron_budget_exhausted", skipped: name, elapsed_ms: elapsed, phases }));
        break;
      }
      const t0 = Date.now();
      try {
        await fn();
      } catch (e: any) {
        console.log(JSON.stringify({ event: "cron_step_failed", step: name, error: e?.message }));
      }
      phases[name] = Date.now() - t0;
    }
    console.log(JSON.stringify({ event: "cron_done", elapsed_ms: Date.now() - t_run, phases }));
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    // Count every answer, including the ones that are not a Response at all:
    // an uncaught throw is what Cloudflare turns into a 1101/500 at the edge,
    // and it was invisible to anything we own for at least a day (2026-08-23).
    // Rethrown unchanged — this hook observes, it does not handle.
    let status = 500;
    try {
      const response = await route(request, env, ctx);
      status = response.status;
      return response;
    } finally {
      const endpoint = endpointLabel(new URL(request.url).pathname);
      const domain = trafficDomain(request);
      ctx.waitUntil(
        getDb(env).then((db) => recordRequest(db, domain, endpoint, status)).catch(() => {}),
      );
    }
  },
};

async function route(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
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
  // Exercise the alert path on demand and report exactly what the webhook
  // said. Without this, a failed notification is invisible until the next
  // real incident — which is the thing the alerting exists to prevent.
  if (pathname === "/admin/test-alert" && request.method === "POST") {
    if (request.headers.get("Authorization") !== `Bearer ${env.ADMIN_SECRET}`) {
      return json({ error: "unauthorized" }, 401);
    }
    const r = await postWebhook(env, "**typeahead: alert path test** — ignore me");
    return json(r, r.ok ? 200 : 500);
  }
  if (pathname === "/health/freshness" && request.method === "GET") {
    return handleFreshness(env);
  }
  if (pathname === "/og-image" && request.method === "GET") {
    return handleOgImage(request, env);
  }
  if (pathname === "/favicon.svg" && request.method === "GET") {
    return new Response(FAVICON_SVG, {
      headers: { "Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=86400" },
    });
  }
  if (pathname === "/apple-touch-icon.png" && request.method === "GET") {
    return handleAppleTouchIcon();
  }
  if (pathname === "/site.webmanifest" && request.method === "GET") {
    return handleWebManifest();
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
    return handleReputation(request, db, env);
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
    return handleRequestIndexing(request, db, env, ctx);
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

  if (
    pathname === "/xrpc/app.bsky.actor.getProfile" &&
    request.method === "GET"
  ) {
    const ip = clientIP(request);
    const { success } = await env.RATE_LIMITER.limit({ key: ip });
    if (!success) {
      return json({ error: "rate limited" }, 429);
    }
    return handleGetProfile(request, db, env, ctx);
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
}
