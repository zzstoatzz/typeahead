import type { TursoDB } from "../db";
import type { Env, IngestEvent } from "../types";
import { clientIP, json } from "../utils";
import { recordActorDelta } from "../metrics";

export async function handleIngest(
  request: Request,
  db: TursoDB,
  env: Env,
  ctx: ExecutionContext,
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

  // batch upsert — bare-DID events use INSERT OR IGNORE (0 Turso writes for known actors),
  // profile/identity events use full UPSERT with COALESCE to preserve existing fields
  const stmts = events.map((e) => {
    const isBareDID = !e.handle && !e.display_name && !e.avatar_cid;
    if (isBareDID) {
      return db.prepare(
        "INSERT OR IGNORE INTO actors (did) VALUES (?1)"
      ).bind(e.did);
    }
    const avatarCid = e.avatar_cid || null;
    return db.prepare(
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
      avatarCid || ''
    );
  });

  let batchResults: { results: unknown[]; meta: { changes: number } }[];
  try {
    batchResults = await db.batch(stmts);
  } catch (e: any) {
    console.log(JSON.stringify({ event: "ingest_error", error: e?.message, count: events.length }));
    return json({ error: e?.message || "db batch failed" }, 500);
  }

  // count newly inserted bare DIDs (changes === 1 means INSERT succeeded, not IGNORE'd)
  let newActors = 0;
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    const isBareDID = !e.handle && !e.display_name && !e.avatar_cid;
    if (isBareDID && batchResults[i]?.meta.changes === 1) newActors++;
  }
  if (newActors > 0) {
    ctx.waitUntil(recordActorDelta(db, { actors: newActors }));
  }

  if (cursor !== undefined) {
    try {
      await env.KV.put("jetstream_cursor", String(cursor));
    } catch {
      // best-effort — don't fail the ingest if KV write fails (e.g. quota)
    }
  }

  // Best-effort activity bump: tag actors that produced any jetstream event in
  // the last hour. Guarded by a 1h threshold so a hot actor causes at most one
  // row write per hour even if they generate hundreds of events. Ranking only
  // needs hour granularity ("active recently" vs "dormant for months").
  // Fire-and-forget — must never delay or fail the ingest response.
  const uniqueDids = Array.from(new Set(events.map((e) => e.did))).filter((d) => d);
  if (uniqueDids.length > 0) {
    const placeholders = uniqueDids.map(() => "?").join(",");
    ctx.waitUntil(
      db.prepare(
        `UPDATE actors SET last_activity_at = unixepoch()
         WHERE did IN (${placeholders}) AND last_activity_at < unixepoch() - 3600`
      ).bind(...uniqueDids).run().catch(() => {})
    );
  }

  return json({ ok: true, ingested: events.length });
}
