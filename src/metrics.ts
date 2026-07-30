import type { TursoDB } from "./db";

/** fire-and-forget: increment 5-min search count + accumulate response time */
export async function recordMetric(db: TursoDB, ms: number): Promise<void> {
  const bucket = Math.floor(Date.now() / 300_000);
  await db.prepare(
    `INSERT INTO metrics (hour, searches, total_ms)
     VALUES (?1, 1, ?2)
     ON CONFLICT(hour) DO UPDATE SET
       searches = searches + 1,
       total_ms = total_ms + ?2`
  )
    .bind(bucket, ms)
    .run();
}

export async function recordCacheHit(db: TursoDB, ms: number): Promise<void> {
  const bucket = Math.floor(Date.now() / 300_000);
  await db.prepare(
    `INSERT INTO metrics (hour, cache_hits, cache_ms)
     VALUES (?1, 1, ?2)
     ON CONFLICT(hour) DO UPDATE SET
       cache_hits = cache_hits + 1,
       cache_ms = cache_ms + ?2`
  )
    .bind(bucket, ms)
    .run();
}

/** fire-and-forget: record actor count deltas at 5-min granularity.
 *  also maintains the running `actors_total` counter so the hourly snapshot
 *  never has to COUNT(*) the full ~6M-row table (a ~300s scan over libSQL HTTP). */
export async function recordActorDelta(
  db: TursoDB,
  deltas: { actors?: number; handles?: number; avatars?: number },
): Promise<void> {
  const bucket = Math.floor(Date.now() / 300_000);
  const actorsDelta = deltas.actors ?? 0;
  const stmts = [
    db.prepare(
      `INSERT INTO actor_deltas (bucket, actors_delta, handles_delta, avatars_delta)
       VALUES (?1, ?2, ?3, ?4)
       ON CONFLICT(bucket) DO UPDATE SET
         actors_delta = actors_delta + ?2,
         handles_delta = handles_delta + ?3,
         avatars_delta = avatars_delta + ?4`
    ).bind(bucket, actorsDelta, deltas.handles ?? 0, deltas.avatars ?? 0),
  ];
  if (actorsDelta !== 0) {
    stmts.push(
      db.prepare(
        `UPDATE counters SET value = value + ?1 WHERE name = 'actors_total'`
      ).bind(actorsDelta)
    );
  }
  await db.batch(stmts);
}

/** fire-and-forget: increment cumulative hit counter per client identity */
export async function recordTrafficSource(db: TursoDB, request: Request): Promise<void> {
  const client = request.headers.get("X-Client");
  const origin = request.headers.get("Origin");
  const referer = request.headers.get("Referer");
  const selfHost = new URL(request.url).hostname;

  let domain: string;
  if (client) {
    domain = client;
  } else if (origin) {
    domain = new URL(origin).hostname;
  } else if (referer) {
    try {
      const refHost = new URL(referer).hostname;
      domain = refHost === selfHost ? "homepage" : refHost;
    } catch { domain = "unknown"; }
  } else {
    domain = "unknown";
  }

  // normalize local/dev traffic
  if (domain === "localhost" || domain.startsWith("127.") || domain === "[::1]") {
    domain = "unknown";
  }
  // X-Client is a raw request header — arbitrary bytes, any length, chosen by
  // the caller. It gets stored, then rendered on /stats. The renderer escapes
  // it, but this is the layer that decides what may enter the corpus at all:
  // anything that is not hostname-shaped is not a traffic source.
  if (!/^[a-zA-Z0-9._:\-\[\]]{1,64}$/.test(domain)) {
    domain = "unknown";
  }
  const bucket = Math.floor(Date.now() / 300_000);
  await db.batch([
    db.prepare(
      `INSERT INTO traffic_sources (domain, hits)
       VALUES (?1, 1)
       ON CONFLICT(domain) DO UPDATE SET hits = hits + 1`
    ).bind(domain),
    db.prepare(
      `INSERT INTO traffic_buckets (bucket, domain, hits)
       VALUES (?1, ?2, 1)
       ON CONFLICT(bucket, domain) DO UPDATE SET hits = hits + 1`
    ).bind(bucket, domain),
  ]);
}
