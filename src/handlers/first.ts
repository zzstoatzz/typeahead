import { KEPT, SHOWN, TRACKED, dayOf, history, keep, missing, parseQuery, reigns, searchLive, streak, strip, tracked } from "../first";
import { firstIndexPage, firstQueryPage } from "../pages/first";
import { statsDb } from "../stats/stats-store";
import type { Env } from "../types";
import { html } from "../utils";
import { searchCacheGeneration } from "./search-contract";

/** How long a rendered page is served from the edge before it is searched again. */
export const PAGE_TTL_S = 600;
/** How long a browser keeps it: short, so a refresh after a deploy shows the new page. */
export const BROWSER_TTL_S = 60;

/** `/first`, `/first?q=…` and `/first/<query>`: rendered from live searches,
 *  cached at the edge for `PAGE_TTL_S`. A render is also what keeps today's
 *  places for tracked queries, once per day, alongside the hourly cron. */
export async function handleFirst(request: Request, env: Env, ctx: ExecutionContext, now = Date.now()): Promise<Response> {
  const url = new URL(request.url);
  if (url.pathname === "/first" && url.searchParams.has("q")) {
    const query = parseQuery(url.searchParams.get("q") ?? "");
    return new Response(null, { status: 302, headers: { Location: query ? `/first/${encodeURIComponent(query)}` : "/first" } });
  }
  const segment = url.pathname === "/first" ? null : url.pathname.slice("/first/".length);
  const query = segment === null ? null : parseQuery(segment);
  if (segment !== null && query === null) return new Response(null, { status: 302, headers: { Location: "/first" } });

  const cache = caches.default;
  // the deployed version is in the key, so a deploy never serves a page the old code rendered
  const version = env.CF_VERSION_METADATA?.id ?? "local";
  const key = new Request(`${url.origin}/first-cache/${searchCacheGeneration(env.SEARCH_CACHE_GENERATION)}/${version}/${query === null ? "" : encodeURIComponent(query)}`);
  const cached = await cache.match(key);
  if (cached) return cached;

  const db = statsDb(env);
  const today = dayOf(now);
  const queries = query === null ? TRACKED : [query];
  const [lives, past, unkept] = await Promise.all([
    searchLive(env, ctx, queries, query === null ? KEPT : SHOWN),
    history(db, today, queries),
    missing(db, today, queries),
  ]);
  if (unkept.length) {
    const fresh = lives.filter((live) => unkept.includes(live.query));
    ctx.waitUntil(keep(db, today, fresh).catch((error) =>
      console.log(JSON.stringify({ event: "first_keep_failed", error: error instanceof Error ? error.message : String(error) }))));
  }

  let body: string;
  if (query === null) {
    body = firstIndexPage(lives.map((live) => {
      const first = live.ok ? live.actors[0] ?? null : null;
      return { query: live.query, first, days: first ? streak(first, today, past.get(live.query) ?? []) : 0 };
    }));
  } else {
    // SAFETY: `queries` is `[query]` here, and searchLive answers each query once
    const live = lives[0]!;
    const slots = tracked(query) ? strip(live.ok ? live.actors[0] ?? null : null, today, past.get(query) ?? []) : null;
    body = firstQueryPage({ query, live, slots, reigns: slots ? reigns(slots) : [] });
  }
  const response = html(body, { "Cache-Control": `public, max-age=${BROWSER_TTL_S}, s-maxage=${PAGE_TTL_S}` });
  if (lives.every((live) => live.ok)) ctx.waitUntil(cache.put(key, response.clone()));
  return response;
}

/** How long the /first link-preview image is kept at the edge. */
export const IMAGE_TTL_S = 3600;

/** A smaller copy of a Bluesky avatar, enough for a 138px tile, as JPEG:
 *  without a suffix the CDN answers WebP, which the renderer cannot draw. */
const thumbnail = (avatar: string) => avatar
  ? avatar.replace("/img/avatar/plain/", "/img/avatar_thumbnail/plain/").replace(/@[a-z]+$/, "") + "@jpeg"
  : "";

/** `/og-image/first`: the link-preview card for /first, each letter's live
 *  first place drawn by the private image renderer, cached for an hour. */
export async function handleFirstImage(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(request.url);
  const version = env.CF_VERSION_METADATA?.id ?? "local";
  const key = new Request(`${url.origin}/first-cache/${searchCacheGeneration(env.SEARCH_CACHE_GENERATION)}/${version}/og-image`);
  const cached = await caches.default.match(key);
  if (cached) return cached;
  const lives = await searchLive(env, ctx, TRACKED, KEPT);
  const tiles = lives.map((live) => [live.query, live.ok ? thumbnail(live.actors[0]?.avatar ?? "") : ""]);
  const rendered = await env.IMAGE_RENDERER.fetch(new Request(`https://image-renderer.internal/og-image/first?d=${encodeURIComponent(JSON.stringify(tiles))}`));
  if (!rendered.ok) return new Response("image unavailable", { status: 503 });
  const response = new Response(rendered.body, { headers: { "Content-Type": "image/png", "Cache-Control": `public, max-age=${IMAGE_TTL_S}` } });
  if (lives.every((live) => live.ok)) ctx.waitUntil(caches.default.put(key, response.clone()));
  return response;
}
