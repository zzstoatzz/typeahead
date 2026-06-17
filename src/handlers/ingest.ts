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

  // Every event starts with an `INSERT OR IGNORE INTO actors(did)` PROBE: it's
  // a no-op write for a known actor, and changes===1 iff the actor is genuinely
  // new — regardless of whether it's bare or profile-bearing. We count new
  // actors from the probes so `actors_total` / the deltas chart stay accurate
  // for ALL new actors (the old code only counted bare-DID inserts, so every
  // actor first seen via a profile/identity event — and the whole PLC backfill —
  // silently undercounted; fixed 2026-06-17). Profile/identity events then get
  // a follow-up UPDATE to set fields (COALESCE preserves existing values).
  // The UPDATE also resets profile_checked_at to 0 so the cron's cold-floor pool
  // picks the actor up next tick to refresh appview aggregates (followers/qs)
  // the firehose can't carry — the "firehose-triggered enqueue".
  const stmts: ReturnType<typeof db.prepare>[] = [];
  const probeIdx: number[] = []; // result index of each event's INSERT OR IGNORE probe
  for (const e of events) {
    probeIdx.push(stmts.length);
    stmts.push(db.prepare("INSERT OR IGNORE INTO actors (did) VALUES (?1)").bind(e.did));
    const isBareDID = !e.handle && !e.display_name && !e.avatar_cid;
    if (!isBareDID) {
      stmts.push(db.prepare(
        `UPDATE actors SET
           handle = COALESCE(NULLIF(?2, ''), handle),
           display_name = COALESCE(NULLIF(?3, ''), display_name),
           avatar_url = COALESCE(NULLIF(?4, ''), avatar_url),
           updated_at = unixepoch(),
           profile_checked_at = 0
         WHERE did = ?1`
      ).bind(e.did, e.handle || '', e.display_name || '', e.avatar_cid || ''));
    }
  }

  let batchResults: { results: unknown[]; meta: { changes: number } }[];
  try {
    batchResults = await db.batch(stmts);
  } catch (e: any) {
    console.log(JSON.stringify({ event: "ingest_error", error: e?.message, count: events.length }));
    return json({ error: e?.message || "db batch failed" }, 500);
  }

  // count ALL genuinely-new actors (probe changes===1 = inserted, not IGNORE'd)
  let newActors = 0;
  for (const i of probeIdx) {
    if (batchResults[i]?.meta.changes === 1) newActors++;
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
