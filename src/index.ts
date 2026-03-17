interface Env {
  DB: D1Database;
  KV: KVNamespace;
  ADMIN_SECRET: string;
  RATE_LIMITER: RateLimit;
  RATE_LIMITER_STRICT: RateLimit;
}

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
};

const SLINGSHOT_URL =
  "https://slingshot.microcosm.blue/xrpc/blue.microcosm.identity.resolveMiniDoc";

function clientIP(request: Request): string {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: CORS_HEADERS });
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
}

interface SlingshotResponse {
  did: string;
  handle: string;
  pds: string;
}

const BSKY_TYPEAHEAD_URL =
  "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead";

// --- backfill: remove this block once at parity with Bluesky ---

async function backfillFromBsky(
  term: string,
  limit: number,
  env: Env
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
      env.DB.prepare(
        `INSERT INTO actors (did, handle, display_name, avatar_url, updated_at)
         VALUES (?1, ?2, ?3, ?4, unixepoch())
         ON CONFLICT(did) DO UPDATE SET
           handle = COALESCE(NULLIF(?2, ''), actors.handle),
           display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
           avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
           updated_at = unixepoch()`
      ).bind(
        a.did,
        a.handle || '',
        a.displayName || '',
        a.avatar || ''
      )
    );

    await env.DB.batch(stmts);
    console.log(JSON.stringify({ event: "backfill", term, upserted: actors.length }));
  } catch {
    // best-effort — don't let backfill errors affect anything
  }
}

async function throttledBackfill(term: string, limit: number, env: Env): Promise<void> {
  // kill switch — set KV key "backfill" to "off" to disable without redeploying
  const flag = await env.KV.get("backfill");
  if (flag === "off") return;

  // global budget — cap total backfill triggers across all users
  const { success } = await env.RATE_LIMITER_STRICT.limit({ key: "backfill" });
  if (!success) {
    console.log(JSON.stringify({ event: "backfill_throttled", term }));
    return;
  }

  return backfillFromBsky(term, limit, env);
}

// --- end backfill ---

/** fire-and-forget: increment hourly search count + accumulate response time */
async function recordMetric(env: Env, ms: number): Promise<void> {
  const hour = Math.floor(Date.now() / 3_600_000);
  await env.DB.prepare(
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
  env: Env,
  ctx: ExecutionContext
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
  const { results } = await env.DB.prepare(
    `SELECT a.did, a.handle, a.display_name, a.avatar_url
     FROM actors_fts
     JOIN actors a ON a.rowid = actors_fts.rowid
     WHERE actors_fts MATCH ?1 AND a.handle != ''
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
      ...(r.avatar_url ? { avatar: r.avatar_url } : {}),
    }));

  // --- backfill: remove this block once at parity with Bluesky ---
  const hasGaps = actors.length < limit || actors.some((a) => !a.avatar);
  if (hasGaps) {
    ctx.waitUntil(throttledBackfill(term, limit, env));
  }
  // --- end backfill ---

  ctx.waitUntil(recordMetric(env, Date.now() - t0));

  const response = json({ actors });

  // cache for 60 seconds
  const cacheable = new Response(response.body, response);
  cacheable.headers.set("Cache-Control", "public, max-age=60");
  await cache.put(cacheKey, cacheable.clone());

  return cacheable;
}

async function handleIngest(
  request: Request,
  env: Env
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
    const avatarUrl = e.avatar_cid
      ? `https://cdn.bsky.app/img/avatar/plain/${e.did}/${e.avatar_cid}@jpeg`
      : null;
    return env.DB.prepare(
      `INSERT INTO actors (did, handle, display_name, avatar_url, updated_at)
       VALUES (?1, ?2, ?3, ?4, unixepoch())
       ON CONFLICT(did) DO UPDATE SET
         handle = COALESCE(NULLIF(?2, ''), actors.handle),
         display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
         avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
         updated_at = unixepoch()`
    ).bind(
      e.did,
      e.handle || '',
      e.display_name || '',
      avatarUrl || ''
    );
  });

  try {
    await env.DB.batch(stmts);
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
  env: Env
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
    env.DB.prepare("DELETE FROM actors WHERE did = ?1").bind(did)
  );
  await env.DB.batch(stmts);

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

/** resolve a handle or DID via slingshot, then upsert into D1 */
async function handleRequestIndexing(
  request: Request,
  env: Env
): Promise<Response> {
  const url = new URL(request.url);
  const identifier =
    url.searchParams.get("handle") ||
    url.searchParams.get("did") ||
    "";

  if (!identifier) {
    return html(indexPage("enter a handle or DID to request indexing."));
  }

  // resolve via slingshot
  const res = await fetch(
    `${SLINGSHOT_URL}?identifier=${encodeURIComponent(identifier)}`
  );
  if (!res.ok) {
    return html(indexPage(`could not resolve "${escHtml(identifier)}". check that it's a valid handle or DID.`));
  }

  const identity: SlingshotResponse = await res.json();

  // fetch profile from public API for display name + avatar
  let displayName = "";
  let avatarUrl = "";
  try {
    const profileRes = await fetch(
      `https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor=${encodeURIComponent(identity.did)}`
    );
    if (profileRes.ok) {
      const profile: any = await profileRes.json();
      displayName = profile.displayName || "";
      avatarUrl = profile.avatar || "";
    }
  } catch {
    // profile enrichment is best-effort
  }

  await env.DB.prepare(
    `INSERT INTO actors (did, handle, display_name, avatar_url, updated_at)
     VALUES (?1, ?2, ?3, ?4, unixepoch())
     ON CONFLICT(did) DO UPDATE SET
       handle = ?2,
       display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
       avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
       updated_at = unixepoch()`
  )
    .bind(identity.did, identity.handle, displayName, avatarUrl)
    .run();

  return html(
    indexPage(`indexed <strong>@${escHtml(identity.handle)}</strong> (${escHtml(identity.did)})`)
  );
}

async function handleStats(env: Env): Promise<Response> {
  const [totalRes, handlesRes, avatarsRes, updatedRes, metricsRes] =
    await env.DB.batch([
      env.DB.prepare("SELECT COUNT(*) AS cnt FROM actors"),
      env.DB.prepare("SELECT COUNT(*) AS cnt FROM actors WHERE handle != ''"),
      env.DB.prepare("SELECT COUNT(*) AS cnt FROM actors WHERE avatar_url != ''"),
      env.DB.prepare("SELECT MAX(updated_at) AS ts FROM actors"),
      env.DB.prepare(
        "SELECT hour, searches, total_ms FROM metrics ORDER BY hour DESC LIMIT 168"
      ),
    ]);

  const total = (totalRes.results[0] as any)?.cnt ?? 0;
  const withHandles = (handlesRes.results[0] as any)?.cnt ?? 0;
  const withAvatars = (avatarsRes.results[0] as any)?.cnt ?? 0;
  const lastUpdated = (updatedRes.results[0] as any)?.ts ?? null;
  const rows = (metricsRes.results ?? []) as {
    hour: number;
    searches: number;
    total_ms: number;
  }[];

  const totalSearches = rows.reduce((s, r) => s + r.searches, 0);
  const totalMs = rows.reduce((s, r) => s + r.total_ms, 0);
  const avgLatency = totalSearches > 0 ? totalMs / totalSearches : 0;
  const handlePct = total > 0 ? ((withHandles / total) * 100).toFixed(1) : "0";
  const avatarPct = total > 0 ? ((withAvatars / total) * 100).toFixed(1) : "0";

  const lastUpdatedStr = lastUpdated
    ? new Date(lastUpdated * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC"
    : "never";

  return html(statsPage({ total, withHandles, withAvatars, lastUpdatedStr, rows, totalSearches, avgLatency, handlePct, avatarPct }));
}

interface StatsData {
  total: number;
  withHandles: number;
  withAvatars: number;
  lastUpdatedStr: string;
  rows: { hour: number; searches: number; total_ms: number }[];
  totalSearches: number;
  avgLatency: number;
  handlePct: string;
  avatarPct: string;
}

function statsPage(d: StatsData): string {
  // build sparkline data (oldest first)
  const sorted = [...d.rows].reverse();
  const counts = sorted.map((r) => r.searches);
  const max = Math.max(...counts, 1);
  const w = 600;
  const h = 80;
  const step = counts.length > 1 ? w / (counts.length - 1) : 0;
  const points = counts
    .map((c, i) => `${(i * step).toFixed(1)},${(h - (c / max) * h).toFixed(1)}`)
    .join(" ");
  const jsonData = JSON.stringify(
    sorted.map((r) => ({
      hour: new Date(r.hour * 3_600_000).toISOString().slice(0, 13) + ":00Z",
      searches: r.searches,
    }))
  );

  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead — stats</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0;
           display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .container { max-width: 620px; width: 100%; padding: 2rem; }
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: #888; }
    h1 strong { color: #e0e0e0; }
    .subtitle { font-size: 0.8rem; color: #555; margin-bottom: 1.5rem; }
    .metrics { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.8rem; margin-bottom: 1.5rem; }
    .metric { background: #111; border: 1px solid #222; border-radius: 6px; padding: 0.7rem 0.9rem; }
    .metric .label { font-size: 0.7rem; color: #555; margin-bottom: 0.2rem; }
    .metric .label[data-tip] { cursor: default; position: relative; border-bottom: 1px dotted #444; display: inline-block; }
    .metric .label[data-tip]:hover::after {
      content: attr(data-tip); position: absolute; left: 0; top: 1.6em; width: 200px;
      padding: 0.4rem 0.5rem; background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
      font-size: 0.95em; color: #999; line-height: 1.4; z-index: 20; white-space: normal;
    }
    .metric .value { font-size: 1.1rem; color: #ccc; }
    .sparkline-wrap { background: #111; border: 1px solid #222; border-radius: 6px;
                      padding: 0.9rem; margin-bottom: 1.5rem; }
    .sparkline-wrap h2 { font-size: 0.75rem; font-weight: 400; color: #555; margin-bottom: 0.6rem; }
    svg { width: 100%; height: auto; }
    polyline { fill: none; stroke: #4a9; stroke-width: 1.5; }
    .summary { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.8rem; margin-bottom: 1.5rem; }
    .tooltip { display: none; position: fixed; background: #1a1a1a; border: 1px solid #333;
               border-radius: 4px; padding: 0.4rem 0.6rem; font-size: 0.75rem; color: #ccc;
               pointer-events: none; z-index: 50; }
    footer { padding-top: 1.5rem; border-top: 1px solid #1a1a1a; font-size: 0.7rem;
             color: #444; display: flex; justify-content: center; gap: 0.4rem; }
    footer a { color: #555; text-decoration: none; }
    footer a:hover { color: #888; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><strong>typeahead</strong> stats</h1>
    </div>
    <p class="subtitle">index health and search activity</p>

    <div class="metrics">
      <div class="metric">
        <div class="label">actors indexed</div>
        <div class="value">${d.total.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="label">with handles</div>
        <div class="value">${d.withHandles.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="label">with avatars</div>
        <div class="value">${d.withAvatars.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="label">last indexed</div>
        <div class="value" style="font-size:0.8rem">${escHtml(d.lastUpdatedStr)}</div>
      </div>
    </div>

    <div class="sparkline-wrap">
      <h2>searches / hour (7 days)</h2>
      ${counts.length > 1
        ? `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" id="spark">
            <polyline points="${points}" />
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
    </div>

    <footer>
      <a href="/">&larr; home</a>
    </footer>
  </div>
  <div class="tooltip" id="tip"></div>
  <script>
    const data = ${jsonData};
    const spark = document.getElementById('spark');
    const tip = document.getElementById('tip');
    if (spark) {
      spark.addEventListener('mousemove', e => {
        const rect = spark.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = Math.min(Math.round(x / rect.width * (data.length - 1)), data.length - 1);
        if (idx >= 0 && data[idx]) {
          tip.textContent = data[idx].hour + ' — ' + data[idx].searches + ' searches';
          tip.style.display = 'block';
          tip.style.left = (e.clientX + 12) + 'px';
          tip.style.top = (e.clientY - 28) + 'px';
        }
      });
      spark.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
    }
  </script>
</body>
</html>`;
}

function indexPage(message?: string): string {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead</title>
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
            border-radius: 6px; color: #e0e0e0; font-size: 0.9rem; outline: none; }
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
    .msg { margin-top: 1rem; padding: 0.8rem; background: #1a1a1a; border-radius: 6px;
           font-size: 0.85rem; line-height: 1.4; }
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
      <h1><strong>typeahead</strong><a href="https://tangled.sh/zzstoatzz.io/typeahead" class="src" target="_blank" rel="noopener">[src]</a><sup class="experimental">*experimental</sup></h1>
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
      <form class="index-form" method="get" action="/request-indexing">
        <input name="handle" placeholder="handle or DID" autocomplete="off">
        <button type="submit">index</button>
      </form>
      ${message ? `<div class="msg">${message}</div>` : ""}
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
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    const { pathname } = new URL(request.url);

    if (pathname === "/" && request.method === "GET") {
      return html(indexPage());
    }

    if (pathname === "/stats" && request.method === "GET") {
      return handleStats(env);
    }

    if (pathname === "/request-indexing" && request.method === "GET") {
      const ip = clientIP(request);
      // stricter limit — this endpoint makes outbound fetches
      const { success } = await env.RATE_LIMITER_STRICT.limit({ key: ip });
      if (!success) {
        console.log(JSON.stringify({ event: "rate_limited", endpoint: "/request-indexing", ip }));
        return json({ error: "rate limited" }, 429);
      }
      return handleRequestIndexing(request, env);
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
      return handleSearch(request, env, ctx);
    }

    if (pathname === "/admin/ingest" && request.method === "POST") {
      return handleIngest(request, env);
    }

    if (pathname === "/admin/delete" && request.method === "POST") {
      return handleDelete(request, env);
    }

    if (pathname === "/admin/cursor" && request.method === "GET") {
      return handleCursor(request, env);
    }

    return json({ error: "not found" }, 404);
  },
};
