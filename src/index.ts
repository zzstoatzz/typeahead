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

/** strip anything that could break FTS5 syntax */
function sanitize(q: string): string {
  return q.replace(/[^\w\s.-]/g, "").trim();
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
           handle = COALESCE(?2, actors.handle),
           display_name = COALESCE(NULLIF(?3, ''), actors.display_name),
           avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
           updated_at = unixepoch()`
      ).bind(
        a.did,
        a.handle || null,
        a.displayName || null,
        a.avatar || null
      )
    );

    await env.DB.batch(stmts);
    console.log(JSON.stringify({ event: "backfill", term, upserted: actors.length }));
  } catch {
    // best-effort — don't let backfill errors affect anything
  }
}

// --- end backfill ---

async function handleSearch(
  request: Request,
  env: Env,
  ctx: ExecutionContext
): Promise<Response> {
  const url = new URL(request.url);
  const q = url.searchParams.get("q") || url.searchParams.get("term") || "";
  const limitParam = parseInt(url.searchParams.get("limit") || "10", 10);
  const limit = Math.max(1, Math.min(limitParam || 10, 100));

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

  const ftsQuery = `"${term}"*`;
  const { results } = await env.DB.prepare(
    `SELECT a.did, a.handle, a.display_name, a.avatar_url
     FROM actors_fts
     JOIN actors a ON a.rowid = actors_fts.rowid
     WHERE actors_fts MATCH ?1
     ORDER BY rank
     LIMIT ?2`
  )
    .bind(ftsQuery, limit)
    .all<ActorRow>();

  const actors = (results || []).map((r) => ({
    did: r.did,
    handle: r.handle,
    ...(r.display_name ? { displayName: r.display_name } : {}),
    ...(r.avatar_url ? { avatar: r.avatar_url } : {}),
  }));

  // --- backfill: remove this block once at parity with Bluesky ---
  const hasGaps = actors.length < limit || actors.some((a) => !a.avatar);
  if (hasGaps) {
    ctx.waitUntil(backfillFromBsky(term, limit, env));
  }
  // --- end backfill ---

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
         handle = COALESCE(?2, actors.handle),
         display_name = COALESCE(?3, actors.display_name),
         avatar_url = COALESCE(NULLIF(?4, ''), actors.avatar_url),
         updated_at = unixepoch()`
    ).bind(
      e.did,
      e.handle || null,
      e.display_name || null,
      avatarUrl
    );
  });

  await env.DB.batch(stmts);

  if (cursor !== undefined) {
    await env.KV.put("jetstream_cursor", String(cursor));
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
