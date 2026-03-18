import { createClient, type Client } from "@libsql/client/web";

interface Env {
  KV: KVNamespace;
  ADMIN_SECRET: string;
  RATE_LIMITER: RateLimit;
  RATE_LIMITER_STRICT: RateLimit;
  TURSO_URL: string;
  TURSO_AUTH_TOKEN: string;
}

interface Stmt {
  bind(...args: unknown[]): Stmt;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  run(): Promise<{ meta: { changes: number } }>;
}

interface TursoDB {
  prepare(sql: string): Stmt;
  batch(stmts: Stmt[]): Promise<{ results: unknown[]; meta: { changes: number } }[]>;
}

function tursoDb(client: Client): TursoDB {
  return {
    prepare(sql) {
      let args: unknown[] = [];
      const s: Stmt & { _sql: string; _args: () => unknown[] } = {
        _sql: sql,
        _args: () => args,
        bind(...a) { args = a; return s; },
        async all<T>() {
          const r = await client.execute({ sql, args: args as any });
          return { results: r.rows as unknown as T[] };
        },
        async first<T>() {
          const r = await client.execute({ sql, args: args as any });
          return (r.rows[0] as unknown as T) ?? null;
        },
        async run() {
          const r = await client.execute({ sql, args: args as any });
          return { meta: { changes: r.rowsAffected } };
        },
      };
      return s;
    },
    async batch(stmts) {
      const results = await client.batch(
        stmts.map((s) => ({ sql: (s as any)._sql as string, args: (s as any)._args() as any[] })),
        "write",
      );
      return results.map((r) => ({
        results: r.rows as unknown[],
        meta: { changes: r.rowsAffected },
      }));
    },
  };
}

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

const SLINGSHOT_URL =
  "https://slingshot.microcosm.blue/xrpc/blue.microcosm.identity.resolveMiniDoc";

const FAVICON = `<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='13' cy='13' r='9' fill='none' stroke='%2344aa99' stroke-width='2.5' opacity='0.8'/%3E%3Cline x1='20' y1='20' x2='28' y2='28' stroke='%2344aa99' stroke-width='2.5' stroke-linecap='round' opacity='0.8'/%3E%3C/svg%3E">`;

function clientIP(request: Request): string {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: CORS_HEADERS });
}

function avatarUrl(did: string, cidOrUrl: string): string {
  if (cidOrUrl.startsWith("https://")) return cidOrUrl;
  return `https://cdn.bsky.app/img/avatar/plain/${did}/${cidOrUrl}@jpeg`;
}

function extractAvatarCid(url: string): string {
  const match = url.match(/\/([^/]+)@jpeg$/);
  return match?.[1] ?? '';
}

/** strip anything that could break FTS5 syntax, preserving unicode letters/digits */
function sanitize(q: string): string {
  return q.replace(/[^\p{L}\p{N}\s.-]/gu, "").trim();
}

interface ActorRow {
  did: string;
  handle: string;
  display_name: string;
  avatar_url: string;
}

interface IngestEvent {
  did: string;
  handle?: string;
  display_name?: string;
  avatar_cid?: string;
  hidden?: boolean;
}

interface SlingshotResponse {
  did: string;
  handle: string;
  pds: string;
}

const BSKY_TYPEAHEAD_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead";

const BSKY_MOD_DID = "did:plc:ar7c4by46qjdydhdevvrndac";
/** labels from bluesky's moderation service that hide an actor from search */
const MOD_HIDE_VALS = new Set(["!hide", "!takedown", "spam"]);

/**
 * returns whether an actor should be hidden from search.
 *
 * only hides actors flagged by bluesky's moderation service (!hide, !takedown, spam).
 * !no-unauthenticated is intentionally NOT filtered — it applies to content, not identity.
 * bluesky's own public typeahead API returns !no-unauthenticated accounts, and so do we.
 */
function shouldHide(labels?: any[]): boolean {
  if (!labels) return false;
  const now = Date.now();
  return labels.some((l: any) => {
    if (l.neg) return false;
    if (l.exp && new Date(l.exp).getTime() <= now) return false;
    return l.src === BSKY_MOD_DID && MOD_HIDE_VALS.has(l.val);
  });
}

// --- backfill: remove this block once at parity with Bluesky ---

async function backfillFromBsky(
  term: string,
  limit: number,
  db: TursoDB,
): Promise<void> {
  try {
    const res = await fetch(
      `${BSKY_TYPEAHEAD_URL}?q=${encodeURIComponent(term)}&limit=${limit}`
    );
    if (!res.ok) return; // 429 or other error — just bail

    const data: any = await res.json();
    const actors: any[] = (data.actors || []).filter((a: any) => a.did);
    if (actors.length === 0) return;

    // upsert all — fills in missing actors AND enriches existing ones
    // (e.g. actors ingested via Jetstream that lack avatar/displayName)
    const stmts = actors.map((a) =>
      db.prepare(
        `INSERT INTO actors (did, handle, display_name, avatar_url, hidden, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, unixepoch())
         ON CONFLICT(did) DO UPDATE SET
           handle = COALESCE(NULLIF(?2, ''), actors.handle),
           display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
           avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
           hidden = ?5,
           updated_at = unixepoch()`
      ).bind(
        a.did,
        a.handle || '',
        a.displayName || '',
        extractAvatarCid(a.avatar || ''),
        shouldHide(a.labels) ? 1 : 0
      )
    );

    await db.batch(stmts);
    console.log(JSON.stringify({ event: "backfill", term, upserted: actors.length }));
  } catch {
    // best-effort — don't let backfill errors affect anything
  }
}

async function throttledBackfill(term: string, limit: number, db: TursoDB, env: Env): Promise<void> {
  // kill switch — set KV key "backfill" to "off" to disable without redeploying
  const flag = await env.KV.get("backfill");
  if (flag === "off") return;

  // global budget — cap total backfill triggers across all users
  const { success } = await env.RATE_LIMITER_STRICT.limit({ key: "backfill" });
  if (!success) {
    console.log(JSON.stringify({ event: "backfill_throttled", term }));
    return;
  }

  return backfillFromBsky(term, limit, db);
}

// --- end backfill ---

/** record an actor-count snapshot for the current hour (idempotent) */
async function recordSnapshot(db: TursoDB): Promise<void> {
  const hour = Math.floor(Date.now() / 3_600_000);
  const row = await db.prepare(
    `SELECT COUNT(*) AS total,
            SUM(CASE WHEN handle != '' THEN 1 ELSE 0 END) AS with_handles,
            SUM(CASE WHEN avatar_url != '' THEN 1 ELSE 0 END) AS with_avatars
     FROM actors WHERE hidden = 0`
  ).first<{ total: number; with_handles: number; with_avatars: number }>();
  if (row) {
    await db.prepare(
      `INSERT OR REPLACE INTO snapshots (hour, total, with_handles, with_avatars)
       VALUES (?1, ?2, ?3, ?4)`
    )
      .bind(hour, row.total, row.with_handles, row.with_avatars)
      .run();
  }
}

/** resolve handles for actors missing them via slingshot */
async function resolveHandles(db: TursoDB): Promise<void> {
  const { results } = await db.prepare(
    "SELECT did FROM actors WHERE handle = '' ORDER BY updated_at DESC LIMIT 1000"
  ).all<{ did: string }>();
  if (!results || results.length === 0) return;

  let resolved = 0;
  for (const { did } of results) {
    try {
      const res = await fetch(
        `${SLINGSHOT_URL}?identifier=${encodeURIComponent(did)}`
      );
      if (!res.ok) continue;
      const identity: SlingshotResponse = await res.json();
      if (identity.handle) {
        await db.prepare(
          "UPDATE actors SET handle = ?1 WHERE did = ?2 AND handle = ''"
        ).bind(identity.handle, did).run();
        resolved++;
      }
    } catch {
      // best-effort — skip failures
    }
  }
  if (resolved > 0) {
    console.log(JSON.stringify({ event: "handle_resolve", resolved, checked: results.length }));
  }
}

const BSKY_GET_PROFILES_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles";

/** refresh moderation labels, walking the full index over multiple cron runs */
async function refreshModeration(db: TursoDB, env: Env): Promise<void> {
  // resume where we left off (rowid cursor persisted in KV)
  const cursorStr = await env.KV.get("mod_cursor");
  const cursor = cursorStr ? Number(cursorStr) : 0;

  const { results } = await db.prepare(
    "SELECT rowid, did FROM actors WHERE rowid > ?1 ORDER BY rowid ASC LIMIT 1000"
  ).bind(cursor).all<{ rowid: number; did: string }>();

  if (!results || results.length === 0) {
    // wrapped around — reset cursor for next run
    await env.KV.put("mod_cursor", "0");
    console.log(JSON.stringify({ event: "moderation_refresh", status: "wrapped", cursor }));
    return;
  }

  let checked = 0;
  let changed = 0;

  // batch into groups of 25 (getProfiles limit), ~200ms pause between calls
  for (let i = 0; i < results.length; i += 25) {
    const batch = results.slice(i, i + 25);
    const params = batch.map((r) => `actors=${encodeURIComponent(r.did)}`).join("&");
    try {
      if (i > 0) await new Promise((r) => setTimeout(r, 200));

      const res = await fetch(`${BSKY_GET_PROFILES_URL}?${params}`);
      if (res.status === 429) {
        // save progress and bail — pick up here next run
        const lastRowid = results[Math.max(0, i - 1)].rowid;
        await env.KV.put("mod_cursor", String(lastRowid));
        console.log(JSON.stringify({ event: "moderation_refresh", status: "rate_limited", checked, changed, cursor: lastRowid }));
        return;
      }
      if (!res.ok) continue;

      const data: any = await res.json();
      const profiles: any[] = data.profiles || [];
      checked += profiles.length;

      const stmts: Stmt[] = [];
      for (const p of profiles) {
        const hide = shouldHide(p.labels) ? 1 : 0;
        stmts.push(
          db.prepare(
            "UPDATE actors SET hidden = ?1 WHERE did = ?2 AND hidden != ?1"
          ).bind(hide, p.did)
        );
      }
      if (stmts.length > 0) {
        const batchResults = await db.batch(stmts);
        changed += batchResults.filter((r) => r.meta.changes > 0).length;
      }
    } catch {
      // best-effort — skip failures
    }
  }

  // save cursor at end of this page
  const lastRowid = results[results.length - 1].rowid;
  await env.KV.put("mod_cursor", String(lastRowid));
  console.log(JSON.stringify({ event: "moderation_refresh", checked, changed, cursor: lastRowid }));
}

/** fire-and-forget: increment hourly search count + accumulate response time */
async function recordMetric(db: TursoDB, ms: number): Promise<void> {
  const hour = Math.floor(Date.now() / 3_600_000);
  await db.prepare(
    `INSERT INTO metrics (hour, searches, total_ms)
     VALUES (?1, 1, ?2)
     ON CONFLICT(hour) DO UPDATE SET
       searches = searches + 1,
       total_ms = total_ms + ?2`
  )
    .bind(hour, ms)
    .run();
}

async function handleSearch(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const url = new URL(request.url);
  const q = url.searchParams.get("q") || url.searchParams.get("term") || "";
  const limitRaw = url.searchParams.get("limit");
  const limitParam = limitRaw !== null ? parseInt(limitRaw, 10) : 10;

  if (!q.trim()) {
    return json({ error: "InvalidRequest", message: "Error: Params must have the property \"q\"" }, 400);
  }
  if (isNaN(limitParam) || limitParam < 1 || limitParam > 100) {
    return json({ error: "InvalidRequest", message: "Error: limit must be between 1 and 100" }, 400);
  }

  const limit = limitParam;
  const term = sanitize(q);
  if (!term) {
    return json({ actors: [] });
  }

  // cache API — edge cache for hot queries
  const cacheKey = new Request(
    `https://typeahead-cache/${encodeURIComponent(term)}:${limit}`,
    request
  );
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached;
  }

  const t0 = Date.now();

  const ftsQuery = `"${term}"*`;
  const { results } = await db.prepare(
    `SELECT a.did, a.handle, a.display_name, a.avatar_url
     FROM actors_fts
     JOIN actors a ON a.rowid = actors_fts.rowid
     WHERE actors_fts MATCH ?1 AND a.handle != '' AND a.hidden = 0
     ORDER BY rank
     LIMIT ?2`
  )
    .bind(ftsQuery, limit)
    .all<ActorRow>();

  const actors = (results || [])
    .map((r) => ({
      did: r.did,
      handle: r.handle,
      ...(r.display_name ? { displayName: r.display_name } : {}),
      ...(r.avatar_url ? { avatar: avatarUrl(r.did, r.avatar_url) } : {}),
    }));

  // --- backfill: remove this block once at parity with Bluesky ---
  const hasGaps = actors.length < limit || actors.some((a) => !a.avatar);
  if (hasGaps) {
    ctx.waitUntil(throttledBackfill(term, limit, db, env));
  }
  // --- end backfill ---

  ctx.waitUntil(recordMetric(db, Date.now() - t0));

  const response = json({ actors });

  // cache for 60 seconds
  const cacheable = new Response(response.body, response);
  cacheable.headers.set("Cache-Control", "public, max-age=60");
  await cache.put(cacheKey, cacheable.clone());

  return cacheable;
}

async function handleIngest(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const auth = request.headers.get("Authorization");
  if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
    const ip = clientIP(request);
    await env.RATE_LIMITER_STRICT.limit({ key: `auth:${ip}` });
    console.log(JSON.stringify({ event: "auth_failure", endpoint: "/admin/ingest", ip }));
    return json({ error: "unauthorized" }, 401);
  }

  let body: { events: IngestEvent[]; cursor?: number };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const { events, cursor } = body;
  if (!Array.isArray(events) || events.length === 0) {
    return json({ error: "events must be a non-empty array" }, 400);
  }
  if (events.length > 10_000) {
    return json({ error: "batch too large (max 10000)" }, 400);
  }

  // batch upsert — use COALESCE to preserve existing fields on partial updates
  const stmts = events.map((e) => {
    const avatarCid = e.avatar_cid || null;
    const hidden = e.hidden !== undefined ? (e.hidden ? 1 : 0) : null;
    return db.prepare(
      `INSERT INTO actors (did, handle, display_name, avatar_url, hidden, updated_at)
       VALUES (?1, ?2, ?3, ?4, COALESCE(?5, 0), unixepoch())
       ON CONFLICT(did) DO UPDATE SET
         handle = COALESCE(NULLIF(?2, ''), actors.handle),
         display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
         avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
         hidden = COALESCE(?5, actors.hidden),
         updated_at = unixepoch()`
    ).bind(
      e.did,
      e.handle || '',
      e.display_name || '',
      avatarCid || '',
      hidden
    );
  });

  try {
    await db.batch(stmts);
  } catch (e: any) {
    console.log(JSON.stringify({ event: "ingest_error", error: e?.message, count: events.length }));
    return json({ error: e?.message || "db batch failed" }, 500);
  }

  if (cursor !== undefined) {
    try {
      await env.KV.put("jetstream_cursor", String(cursor));
    } catch {
      // best-effort — don't fail the ingest if KV write fails (e.g. quota)
    }
  }

  return json({ ok: true, ingested: events.length });
}

async function handleDelete(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const auth = request.headers.get("Authorization");
  if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
    const ip = clientIP(request);
    await env.RATE_LIMITER_STRICT.limit({ key: `auth:${ip}` });
    console.log(JSON.stringify({ event: "auth_failure", endpoint: "/admin/delete", ip }));
    return json({ error: "unauthorized" }, 401);
  }

  let body: { dids: string[] };
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const { dids } = body;
  if (!Array.isArray(dids) || dids.length === 0) {
    return json({ error: "dids must be a non-empty array" }, 400);
  }
  if (dids.length > 10_000) {
    return json({ error: "batch too large (max 10000)" }, 400);
  }

  const stmts = dids.map((did) =>
    db.prepare("DELETE FROM actors WHERE did = ?1").bind(did)
  );
  await db.batch(stmts);

  return json({ ok: true, deleted: dids.length });
}

async function handleCursor(
  request: Request,
  env: Env
): Promise<Response> {
  const auth = request.headers.get("Authorization");
  if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
    const ip = clientIP(request);
    await env.RATE_LIMITER_STRICT.limit({ key: `auth:${ip}` });
    console.log(JSON.stringify({ event: "auth_failure", endpoint: "/admin/cursor", ip }));
    return json({ error: "unauthorized" }, 401);
  }

  const cursor = await env.KV.get("jetstream_cursor");
  return json({ cursor: cursor ? Number(cursor) : null });
}

/** resolve a handle or DID via slingshot, then upsert into the active DB */
async function handleRequestIndexing(
  request: Request,
  db: TursoDB,
  env: Env,
): Promise<Response> {
  const url = new URL(request.url);
  const identifier =
    url.searchParams.get("handle") ||
    url.searchParams.get("did") ||
    "";

  if (!identifier) {
    return json({ error: "enter a handle or DID to request indexing." }, 400);
  }

  // resolve via slingshot
  const res = await fetch(
    `${SLINGSHOT_URL}?identifier=${encodeURIComponent(identifier)}`
  );
  if (!res.ok) {
    return json({ error: `could not resolve "${identifier}". check that it's a valid handle or DID.` }, 404);
  }

  const identity: SlingshotResponse = await res.json();

  // fetch profile from public API for display name + avatar + labels
  let displayName = "";
  let avatarCid = "";
  let hidden = false;
  try {
    const profileRes = await fetch(
      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(identity.did)}`
    );
    if (profileRes.ok) {
      const profile: any = await profileRes.json();
      displayName = profile.displayName || "";
      avatarCid = extractAvatarCid(profile.avatar || "");
      hidden = shouldHide(profile.labels);
    }
  } catch {
    // profile enrichment is best-effort
  }

  await db.prepare(
    `INSERT INTO actors (did, handle, display_name, avatar_url, hidden, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, unixepoch())
     ON CONFLICT(did) DO UPDATE SET
       handle = ?2,
       display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
       avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
       hidden = ?5,
       updated_at = unixepoch()`
  )
    .bind(identity.did, identity.handle, displayName, avatarCid, hidden ? 1 : 0)
    .run();

  return json({
    handle: identity.handle,
    did: identity.did,
    ...(hidden ? { hidden: true, reason: "hidden by moderation" } : { hidden: false }),
  });
}

async function handleStats(db: TursoDB): Promise<Response> {
  const [totalRes, handlesRes, avatarsRes, hiddenRes, metricsRes, snapshotRes] =
    await db.batch([
      db.prepare("SELECT COUNT(*) AS cnt FROM actors WHERE hidden = 0"),
      db.prepare("SELECT COUNT(*) AS cnt FROM actors WHERE handle != '' AND hidden = 0"),
      db.prepare("SELECT COUNT(*) AS cnt FROM actors WHERE avatar_url != '' AND hidden = 0"),
      db.prepare("SELECT COUNT(*) AS cnt FROM actors WHERE hidden != 0"),
      db.prepare(
        "SELECT hour, searches, total_ms FROM metrics ORDER BY hour DESC LIMIT 168"
      ),
      db.prepare(
        "SELECT hour, total, with_handles, with_avatars FROM snapshots ORDER BY hour ASC LIMIT 2000"
      ),
    ]);

  const total = (totalRes.results[0] as any)?.cnt ?? 0;
  const withHandles = (handlesRes.results[0] as any)?.cnt ?? 0;
  const withAvatars = (avatarsRes.results[0] as any)?.cnt ?? 0;
  const hiddenCount = (hiddenRes.results[0] as any)?.cnt ?? 0;
  const rows = (metricsRes.results ?? []) as {
    hour: number;
    searches: number;
    total_ms: number;
  }[];
  const snapshots = (snapshotRes.results ?? []) as SnapshotPoint[];

  // append live counts as the latest point
  const liveHour = Math.floor(Date.now() / 3_600_000);
  if (snapshots.length === 0 || snapshots[snapshots.length - 1].hour < liveHour) {
    snapshots.push({ hour: liveHour, total, with_handles: withHandles, with_avatars: withAvatars });
  }

  const totalSearches = rows.reduce((s, r) => s + r.searches, 0);
  const totalMs = rows.reduce((s, r) => s + r.total_ms, 0);
  const avgLatency = totalSearches > 0 ? totalMs / totalSearches : 0;
  const handlePct = total > 0 ? ((withHandles / total) * 100).toFixed(1) : "0";
  const avatarPct = total > 0 ? ((withAvatars / total) * 100).toFixed(1) : "0";

  return html(statsPage({ total, hiddenCount, rows, totalSearches, avgLatency, handlePct, avatarPct, snapshots }));
}

interface SnapshotPoint {
  hour: number;
  total: number;
  with_handles: number;
  with_avatars: number;
}

interface StatsData {
  total: number;
  hiddenCount: number;
  rows: { hour: number; searches: number; total_ms: number }[];
  totalSearches: number;
  avgLatency: number;
  handlePct: string;
  avatarPct: string;
  snapshots: SnapshotPoint[];
}

function statsPage(d: StatsData): string {
  // search sparkline data (oldest first)
  const sorted = [...d.rows].reverse();
  const counts = sorted.map((r) => r.searches);
  const sparkMax = Math.max(...counts, 1);
  const sw = 600, sh = 80;
  const step = counts.length > 1 ? sw / (counts.length - 1) : 0;
  const sparkPoints = counts
    .map((c, i) => `${(i * step).toFixed(1)},${(sh - (c / sparkMax) * sh).toFixed(1)}`)
    .join(" ");
  const sparkJson = JSON.stringify(
    sorted.map((r) => ({
      hour: new Date(r.hour * 3_600_000).toISOString().slice(0, 13) + ":00Z",
      searches: r.searches,
    }))
  );

  const snapshotJson = JSON.stringify(d.snapshots);

  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead — stats</title>
  ${FAVICON}
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0;
           display: flex; justify-content: center; padding: 2rem 1rem; min-height: 100vh; }
    .container { max-width: 620px; width: 100%; }
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: #888; }
    h1 strong { color: #e0e0e0; }
    .subtitle { font-size: 0.8rem; color: #555; margin-bottom: 1.5rem; }

    .chart-wrap { background: #111; border: 1px solid #222; border-radius: 6px;
                  padding: 0.9rem; margin-bottom: 1.5rem; position: relative; }
    .chart-wrap h2 { font-size: 0.75rem; font-weight: 400; color: #555; margin-bottom: 0.6rem; }
    #trend { width: 100%; height: 200px; display: block; touch-action: none; }
    .legend { display: flex; gap: 1.2rem; margin-top: 0.6rem; font-size: 0.7rem; color: #888; }
    .legend span { display: flex; align-items: center; gap: 0.35rem; }
    .ldot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .chart-tip { display: none; position: fixed; background: rgba(17, 17, 17, 0.95);
                 border: 1px solid #333; border-radius: 6px; padding: 0.5rem 0.7rem;
                 font-size: 0.72rem; color: #ccc; pointer-events: none; z-index: 50;
                 line-height: 1.6; white-space: nowrap;
                 box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
    .ct-time { color: #666; font-size: 0.65rem; }
    .ct-row { display: flex; align-items: center; gap: 0.35rem; }
    .ct-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }

    .sparkline-wrap { background: #111; border: 1px solid #222; border-radius: 6px;
                      padding: 0.9rem; margin-bottom: 1.5rem; }
    .sparkline-wrap h2 { font-size: 0.75rem; font-weight: 400; color: #555; margin-bottom: 0.6rem; }
    svg { width: 100%; height: auto; }
    polyline { fill: none; stroke: #4a9; stroke-width: 1.5; }

    .summary { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.8rem; margin-bottom: 1.5rem; }
    .metric { background: #111; border: 1px solid #222; border-radius: 6px; padding: 0.7rem 0.9rem; }
    .metric .label { font-size: 0.7rem; color: #555; margin-bottom: 0.2rem; }
    .metric .label[data-tip] { cursor: default; position: relative; border-bottom: 1px dotted #444; display: inline-block; }
    .metric .label[data-tip]:hover::after {
      content: attr(data-tip); position: absolute; left: 0; top: 1.6em; width: 200px;
      padding: 0.4rem 0.5rem; background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
      font-size: 0.95em; color: #999; line-height: 1.4; z-index: 20; white-space: normal;
    }
    .metric .value { font-size: 1.1rem; color: #ccc; }
    .spark-tip { display: none; position: fixed; background: #1a1a1a; border: 1px solid #333;
                 border-radius: 4px; padding: 0.4rem 0.6rem; font-size: 0.75rem; color: #ccc;
                 pointer-events: none; z-index: 50; }

    footer { padding-top: 1.5rem; border-top: 1px solid #1a1a1a; font-size: 0.7rem;
             color: #444; display: flex; justify-content: center; gap: 0.4rem; }
    footer a { color: #555; text-decoration: none; }
    footer a:hover { color: #888; }

    @media (max-width: 640px) {
      body { padding: 1.5rem 0.75rem; }
      #trend { height: 160px; }
      .legend { gap: 0.8rem; font-size: 0.65rem; flex-wrap: wrap; }
      .summary { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><strong>typeahead</strong> stats</h1>
    </div>
    <p class="subtitle">index health and search activity</p>

    <div class="chart-wrap">
      <h2>actors indexed</h2>
      ${d.snapshots.length > 1
        ? `<canvas id="trend"></canvas>`
        : `<div style="color:#444;font-size:0.8rem;padding:2rem 0;text-align:center">collecting data — check back soon</div>`}
      <div class="legend">
        <span><span class="ldot" style="background:#4a9"></span> total (${d.total.toLocaleString()})</span>
        <span><span class="ldot" style="background:#58a6ff"></span> with handles</span>
        <span><span class="ldot" style="background:#bc8cff"></span> with avatars</span>
      </div>
    </div>
    <div class="chart-tip" id="chart-tip"></div>

    <div class="sparkline-wrap">
      <h2>searches / hour (7 days)</h2>
      ${counts.length > 1
        ? `<svg viewBox="0 0 ${sw} ${sh}" preserveAspectRatio="none" id="spark">
            <polyline points="${sparkPoints}" />
          </svg>`
        : `<div style="color:#444;font-size:0.8rem;padding:1rem 0;text-align:center">no data yet</div>`}
    </div>

    <div class="summary">
      <div class="metric">
        <div class="label">total searches (7d)</div>
        <div class="value">${d.totalSearches.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="average response time for uncached searches">avg latency</div>
        <div class="value">${d.avgLatency.toFixed(1)} ms</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="% of indexed actors with a resolved handle (vs bare DID only)">handle coverage</div>
        <div class="value">${d.handlePct}%</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="% of indexed actors with a profile image">avatar coverage</div>
        <div class="value">${d.avatarPct}%</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="actors hidden by bluesky moderation (!hide, !takedown, spam)">hidden by moderation</div>
        <div class="value">${d.hiddenCount.toLocaleString()}</div>
      </div>
    </div>

    <footer>
      <a href="/">&larr; home</a>
    </footer>
  </div>
  <div class="spark-tip" id="spark-tip"></div>
  <script>
    const COLORS = { total: '#4a9', handles: '#58a6ff', avatars: '#bc8cff' };

    function fmtNum(n) {
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
      return n.toLocaleString();
    }

    /* --- actors indexed trend chart --- */
    const snaps = ${snapshotJson};
    const canvas = document.getElementById('trend');
    const chartTip = document.getElementById('chart-tip');

    function drawChart(hoverIdx) {
      if (!canvas || snaps.length < 2) return;
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const W = canvas.clientWidth, H = canvas.clientHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, W, H);

      const pad = { t: 16, r: 12, b: 24, l: 12 };
      const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
      const n = snaps.length;

      let yMax = 0;
      for (const s of snaps) { if (s.total > yMax) yMax = s.total; }
      yMax = yMax * 1.08 || 1;

      const toX = i => pad.l + (i / (n - 1)) * cw;
      const toY = v => pad.t + ch - (v / yMax) * ch;

      const series = [
        { key: 'with_avatars', color: COLORS.avatars },
        { key: 'with_handles', color: COLORS.handles },
        { key: 'total', color: COLORS.total },
      ];

      for (const s of series) {
        const pts = snaps.map((d, i) => [toX(i), toY(d[s.key])]);
        const trace = () => {
          ctx.beginPath();
          ctx.moveTo(pts[0][0], pts[0][1]);
          for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
        };

        // glow
        ctx.save(); trace();
        ctx.shadowColor = s.color; ctx.shadowBlur = 18;
        ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.globalAlpha = 0.07;
        ctx.stroke(); ctx.restore();

        // medium glow
        ctx.save(); trace();
        ctx.shadowColor = s.color; ctx.shadowBlur = 6;
        ctx.strokeStyle = s.color; ctx.lineWidth = 1; ctx.globalAlpha = 0.15;
        ctx.stroke(); ctx.restore();

        // core line
        trace();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 1.2; ctx.globalAlpha = 0.55;
        ctx.stroke(); ctx.globalAlpha = 1;
      }

      // x-axis
      {
        const axisY = H - pad.b;
        ctx.beginPath(); ctx.moveTo(pad.l, axisY); ctx.lineTo(W - pad.r, axisY);
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 1;
        ctx.stroke();

        ctx.font = '10px system-ui, sans-serif';
        ctx.fillStyle = '#555'; ctx.textAlign = 'center';
        const t0ms = snaps[0].hour * 3600000;
        const tNms = snaps[n - 1].hour * 3600000;
        const spanDays = (tNms - t0ms) / 86400000;

        // walk midnights
        const d = new Date(t0ms);
        d.setUTCMinutes(0, 0, 0);
        d.setUTCHours(0);
        d.setUTCDate(d.getUTCDate() + 1);
        const minGap = W < 500 ? 65 : 50;
        let prevX = -Infinity;

        while (d.getTime() <= tNms) {
          const frac = (d.getTime() - t0ms) / (tNms - t0ms);
          const x = pad.l + frac * cw;
          if (x >= pad.l && x <= W - pad.r && x - prevX >= minGap) {
            ctx.beginPath(); ctx.moveTo(x, axisY); ctx.lineTo(x, axisY + 4);
            ctx.strokeStyle = 'rgba(255,255,255,0.1)'; ctx.lineWidth = 1; ctx.stroke();
            const label = spanDays > 14
              ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
              : d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
            ctx.globalAlpha = 0.4;
            ctx.fillText(label, x, axisY + 15);
            ctx.globalAlpha = 1;
            prevX = x;
          }
          d.setUTCDate(d.getUTCDate() + 1);
        }
      }

      // hover crosshair + dots
      if (hoverIdx != null && hoverIdx >= 0 && hoverIdx < n) {
        const x = toX(hoverIdx);
        ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, H - pad.b);
        ctx.strokeStyle = 'rgba(255,255,255,0.1)';
        ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);

        const snap = snaps[hoverIdx];
        for (const s of series) {
          const y = toY(snap[s.key]);
          ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.fillStyle = '#fff'; ctx.globalAlpha = 0.9; ctx.fill();
          ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.8;
          ctx.stroke(); ctx.globalAlpha = 1;
        }
      }
    }

    function chartHover(clientX, clientY) {
      if (!canvas || snaps.length < 2) return;
      const rect = canvas.getBoundingClientRect();
      const mx = clientX - rect.left;
      const pad = { l: 12, r: 12 };
      const cw = rect.width - pad.l - pad.r;
      const frac = (mx - pad.l) / cw;
      const idx = Math.max(0, Math.min(snaps.length - 1, Math.round(frac * (snaps.length - 1))));
      drawChart(idx);

      const snap = snaps[idx];
      const t = new Date(snap.hour * 3600000);
      const time = t.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
      chartTip.innerHTML =
        '<div class="ct-time">' + time + '</div>'
        + '<div class="ct-row"><span class="ct-dot" style="background:' + COLORS.total + '"></span> ' + fmtNum(snap.total) + ' actors</div>'
        + '<div class="ct-row"><span class="ct-dot" style="background:' + COLORS.handles + '"></span> ' + fmtNum(snap.with_handles) + ' with handles</div>'
        + '<div class="ct-row"><span class="ct-dot" style="background:' + COLORS.avatars + '"></span> ' + fmtNum(snap.with_avatars) + ' with avatars</div>';
      chartTip.style.display = 'block';
      const tx = clientX + 14, ty = clientY - 10;
      chartTip.style.left = (tx + chartTip.offsetWidth > window.innerWidth ? clientX - chartTip.offsetWidth - 10 : tx) + 'px';
      chartTip.style.top = ty + 'px';
    }

    if (canvas) {
      drawChart(null);
      window.addEventListener('resize', () => drawChart(null));

      canvas.addEventListener('mousemove', e => chartHover(e.clientX, e.clientY));
      canvas.addEventListener('mouseleave', () => {
        chartTip.style.display = 'none';
        drawChart(null);
      });
      canvas.addEventListener('touchmove', e => {
        e.preventDefault();
        const t = e.touches[0];
        chartHover(t.clientX, t.clientY);
      });
      canvas.addEventListener('touchend', () => {
        chartTip.style.display = 'none';
        drawChart(null);
      });
    }

    /* --- search sparkline --- */
    const sparkData = ${sparkJson};
    const spark = document.getElementById('spark');
    const sparkTip = document.getElementById('spark-tip');
    if (spark) {
      spark.addEventListener('mousemove', e => {
        const rect = spark.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = Math.min(Math.round(x / rect.width * (sparkData.length - 1)), sparkData.length - 1);
        if (idx >= 0 && sparkData[idx]) {
          sparkTip.textContent = sparkData[idx].hour + ' — ' + sparkData[idx].searches + ' searches';
          sparkTip.style.display = 'block';
          sparkTip.style.left = (e.clientX + 12) + 'px';
          sparkTip.style.top = (e.clientY - 28) + 'px';
        }
      });
      spark.addEventListener('mouseleave', () => { sparkTip.style.display = 'none'; });
    }
  </script>
</body>
</html>`;
}

function indexPage(): string {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead</title>
  ${FAVICON}
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0;
           display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .container { max-width: 460px; width: 100%; padding: 2rem; }
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: #888; }
    h1 strong { color: #e0e0e0; }
    .src { font-size: 0.75em; color: #555; text-decoration: none; margin-left: 0.3em; }
    .src:hover { color: #888; }
    .experimental { font-size: 0.55em; color: #664; cursor: default; position: relative;
                    vertical-align: super; line-height: 1; margin-left: 0.3em; }
    .experimental:hover::after {
      content: "this is experimental and may break or disappear — don't depend on it for anything critical";
      position: absolute; left: 0; top: 1.4em; width: 220px; padding: 0.5rem;
      background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
      font-size: 1.4em; color: #999; line-height: 1.4; z-index: 20; white-space: normal;
    }
    .subtitle { font-size: 0.8rem; color: #555; margin-bottom: 1.5rem; }
    .subtitle a { color: #555; text-decoration: none; }
    .subtitle a:hover { color: #888; }
    section { margin-bottom: 2rem; }
    label { display: block; font-size: 0.8rem; color: #666; margin-bottom: 0.5rem; }
    .search-wrap { position: relative; }
    input { width: 100%; padding: 0.6rem 0.8rem; background: #1a1a1a; border: 1px solid #333;
            border-radius: 6px; color: #e0e0e0; font-size: 16px; outline: none; }
    input:focus { border-color: #555; }
    input::placeholder { color: #555; }
    .results { position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px;
               background: #141414; border: 1px solid #2a2a2a; border-radius: 6px;
               max-height: 260px; overflow-y: auto; z-index: 10; display: none; }
    .results.show { display: block; }
    .result { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 0.7rem;
              cursor: default; font-size: 0.85rem; }
    .result:hover { background: #1a1a1a; }
    .result img { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
    .result .placeholder { width: 28px; height: 28px; border-radius: 50%; background: #222; flex-shrink: 0; }
    .result .info { min-width: 0; }
    .result .name { color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .result .handle { color: #555; font-size: 0.75rem; }
    .empty { padding: 0.6rem 0.8rem; color: #444; font-size: 0.8rem; }
    .index-form { display: flex; gap: 0.5rem; }
    .index-form input { flex: 1; }
    button { padding: 0.6rem 1rem; background: #2a2a2a; border: 1px solid #333;
             border-radius: 6px; color: #e0e0e0; font-size: 0.9rem; cursor: pointer; }
    button:hover { background: #333; }
    button:disabled { opacity: 0.5; cursor: default; }
    .msg { margin-top: 0.75rem; padding: 0.6rem 0.8rem; background: #1a1a1a; border-radius: 6px;
           font-size: 0.85rem; line-height: 1.4; display: flex; justify-content: space-between;
           align-items: start; gap: 0.5rem; }
    .msg.error { border-left: 2px solid #a55; }
    .msg .dismiss { color: #555; cursor: pointer; font-size: 0.75rem; flex-shrink: 0;
                    background: none; border: none; padding: 0; }
    .msg .dismiss:hover { color: #888; }
    .api { margin-bottom: 1.5rem; background: #111; border: 1px solid #222; border-radius: 6px;
           padding: 0.5rem 0.7rem; display: flex; align-items: center; gap: 0.5rem;
           overflow-x: auto; white-space: nowrap; }
    .api .method { font-size: 0.65rem; font-weight: 600; color: #4a9; background: #4a92;
                   padding: 0.15rem 0.4rem; border-radius: 3px; flex-shrink: 0;
                   font-family: ui-monospace, monospace; letter-spacing: 0.03em; }
    .api .path { font-size: 0.7rem; color: #666; font-family: ui-monospace, monospace; }
    footer { padding-top: 1.5rem; border-top: 1px solid #1a1a1a; font-size: 0.7rem;
             color: #444; display: flex; justify-content: center; gap: 0.4rem; }
    footer a { color: #555; text-decoration: none; }
    footer a:hover { color: #888; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><strong>typeahead</strong><sup class="experimental">*experimental</sup> <a href="https://tangled.sh/zzstoatzz.io/typeahead" class="src" target="_blank" rel="noopener">[src]</a></h1>
    </div>
    <p class="subtitle">community actor search for <a href="https://atproto.com" target="_blank" rel="noopener">atproto</a></p>

    <section>
      <label>try it</label>
      <div class="search-wrap">
        <input id="q" placeholder="search handles..." autocomplete="off" autofocus>
        <div class="results" id="results"></div>
      </div>
    </section>

    <section>
      <label>request indexing</label>
      <form class="index-form" id="index-form">
        <input id="handle-input" placeholder="handle or DID" autocomplete="off">
        <button type="submit" id="index-btn">index</button>
      </form>
      <div id="index-msg"></div>
    </section>

    <div class="api">
      <span class="method">GET</span>
      <span class="path">/xrpc/app.bsky.actor.searchActorsTypeahead?q=...&amp;limit=10</span>
    </div>

    <footer>
      by <a href="https://bsky.app/profile/zzstoatzz.io" target="_blank" rel="noopener">@zzstoatzz.io</a>
      · <a href="/stats">stats</a>
    </footer>
  </div>
  <script>
    const q = document.getElementById('q');
    const results = document.getElementById('results');
    let timer = null;
    q.addEventListener('input', () => {
      clearTimeout(timer);
      const v = q.value.trim();
      if (v.length < 2) { results.classList.remove('show'); return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch('/xrpc/app.bsky.actor.searchActorsTypeahead?q=' + encodeURIComponent(v) + '&limit=8');
          const data = await r.json();
          const actors = data.actors || [];
          if (actors.length === 0) {
            results.innerHTML = '<div class="empty">no results</div>';
          } else {
            results.innerHTML = actors.map(a =>
              '<div class="result">' +
              (a.avatar ? '<img src="' + a.avatar + '" alt="">' : '<div class="placeholder"></div>') +
              '<div class="info"><div class="name">' + esc(a.displayName || a.handle) + '</div>' +
              '<div class="handle">@' + esc(a.handle) + '</div></div></div>'
            ).join('');
          }
          results.classList.add('show');
        } catch(e) {}
      }, 200);
    });
    document.addEventListener('click', e => { if (!e.target.closest('.search-wrap')) results.classList.remove('show'); });
    q.addEventListener('focus', () => { if (results.innerHTML) results.classList.add('show'); });
    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

    // request indexing form
    const indexForm = document.getElementById('index-form');
    const handleInput = document.getElementById('handle-input');
    const indexBtn = document.getElementById('index-btn');
    const indexMsg = document.getElementById('index-msg');

    function showMsg(text, isError) {
      indexMsg.innerHTML = '<div class="msg' + (isError ? ' error' : '') + '">' +
        '<span>' + text + '</span>' +
        '<button class="dismiss" onclick="dismissMsg()">\\u00d7</button></div>';
    }

    function dismissMsg() {
      indexMsg.innerHTML = '';
    }

    indexForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const val = handleInput.value.trim();
      if (!val) { showMsg('enter a handle or DID.', true); return; }

      indexBtn.disabled = true;
      indexBtn.textContent = '...';
      dismissMsg();

      try {
        const r = await fetch('/request-indexing?handle=' + encodeURIComponent(val), { method: 'POST' });
        const data = await r.json();
        if (data.error) {
          showMsg(esc(data.error), true);
        } else {
          const hidden = data.hidden ? ' <em style="color:#886">(' + esc(data.reason || 'hidden') + ')</em>' : '';
          showMsg('indexed <strong>@' + esc(data.handle) + '</strong>' + hidden, false);
          handleInput.value = '';
        }
      } catch {
        showMsg('something went wrong — try again.', true);
      } finally {
        indexBtn.disabled = false;
        indexBtn.textContent = 'index';
      }
    });
  </script>
</body>
</html>`;
}

function html(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", ...CORS_HEADERS },
  });
}

export default {
  async scheduled(_event: ScheduledEvent, env: Env, _ctx: ExecutionContext): Promise<void> {
    const db = tursoDb(createClient({ url: env.TURSO_URL, authToken: env.TURSO_AUTH_TOKEN }));
    await recordSnapshot(db);
    await refreshModeration(db, env);
    await resolveHandles(db);
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const { pathname } = new URL(request.url);

    if (pathname === "/" && request.method === "GET") {
      return html(indexPage());
    }
    if (pathname === "/admin/cursor" && request.method === "GET") {
      return handleCursor(request, env);
    }
    if (pathname === "/request-indexing" && request.method === "GET") {
      return new Response(null, { status: 302, headers: { Location: "/" } });
    }

    const db = tursoDb(createClient({ url: env.TURSO_URL, authToken: env.TURSO_AUTH_TOKEN }));

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
      return handleIngest(request, db, env);
    }

    if (pathname === "/admin/delete" && request.method === "POST") {
      return handleDelete(request, db, env);
    }

    return json({ error: "not found" }, 404);
  },
};
