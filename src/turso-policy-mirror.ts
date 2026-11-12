import type { SqlDb } from "./db";
import { getBlockedDomains, getOverrides, isDomainBlocked, resolveHidden } from "./moderation";

/** The Turso copy of a moderation policy change. The control database is the
 *  source of truth (src/control-store.ts); these writes keep `mod_overrides`,
 *  `blocked_domains` and `actors.hidden` in Turso as they were before it
 *  existed, because the Turso-fed slots are the rollback target and read
 *  them. Each admin handler makes one call here after its control write.
 *  When Turso is retired this file and those calls go; the one thing a
 *  handler takes from here is the `hidden` that `mirrorOverrideClear`
 *  returns for the `DELETE /admin/mod-override` response. */

export async function mirrorOverrideSet(db: SqlDb, did: string, action: "show" | "hide", reason: string): Promise<void> {
  await db.prepare(
    `INSERT INTO mod_overrides (did, action, reason) VALUES (?1, ?2, ?3)
     ON CONFLICT(did) DO UPDATE SET action = ?2, reason = ?3, created_at = unixepoch()`
  ).bind(did, action, reason).run();

  // immediately apply to actors table (touch updated_at so fly replica syncs)
  await db.prepare(
    "UPDATE actors SET hidden = ?1, updated_at = unixepoch() WHERE did = ?2"
  ).bind(action === 'show' ? 0 : 1, did).run();
}

/** Returns the actor's effective `hidden` once the override is gone; 0 for
 *  an actor Turso does not hold. */
export async function mirrorOverrideClear(db: SqlDb, did: string): Promise<0 | 1> {
  await db.prepare("DELETE FROM mod_overrides WHERE did = ?1").bind(did).run();

  // recompute effective hidden from ALL sources now that the override is gone
  // (labels + slur + domain block) — not labels alone, or a domain-blocked
  // account would resurface the moment its show/hide override is removed.
  const row = await db.prepare(
    "SELECT handle, labels FROM actors WHERE did = ?1"
  ).bind(did).first<{ handle: string; labels: string }>();
  if (!row) return 0;

  let labels: any[] = [];
  try { labels = JSON.parse(row.labels); } catch { labels = []; }
  const blockedDomains = await getBlockedDomains(db);
  const hidden = resolveHidden({
    labels,
    handle: row.handle,
    override: null,
    domainBlocked: isDomainBlocked(row.handle || "", blockedDomains),
  });
  await db.prepare("UPDATE actors SET hidden = ?1, updated_at = unixepoch() WHERE did = ?2").bind(hidden, did).run();
  return hidden;
}

// rowid-window size for domain backfills. The `handle LIKE '%.domain'` match
// is a full table scan (leading wildcard can't use the handle index), so a
// single UPDATE holds the SQLite single-writer lock for ~8s over 5.9M rows,
// blocking ALL reads and timing out the client. Windowing bounds each
// transaction to ~WINDOW rows → short locks reads can interleave with, run in
// the background so the client returns immediately.
const BACKFILL_WINDOW = 500_000;

async function maxRowid(db: SqlDb): Promise<number> {
  const row = await db.prepare("SELECT max(rowid) AS m FROM actors").first<{ m: number | null }>();
  return row?.m ?? 0;
}

/** Hide apex + every subdomain of `domain`, sparing 'show'-override DIDs, in
 *  bounded rowid windows (short write locks). Only flips 0→1 so already-hidden
 *  rows don't churn updated_at / the search service sync. */
async function backfillDomainBlock(db: SqlDb, domain: string): Promise<void> {
  const suffix = "%." + domain;
  const max = await maxRowid(db);
  let cursor = 0;
  let hidden = 0;
  while (cursor < max) {
    const res = await db.prepare(
      `UPDATE actors SET hidden = 1, updated_at = unixepoch()
       WHERE rowid > ?1 AND rowid <= ?2
         AND (handle = ?3 OR handle LIKE ?4) COLLATE NOCASE
         AND hidden = 0
         AND did NOT IN (SELECT did FROM mod_overrides WHERE action = 'show')`
    ).bind(cursor, cursor + BACKFILL_WINDOW, domain, suffix).run();
    hidden += res.meta?.changes ?? 0;
    cursor += BACKFILL_WINDOW;
  }
  console.log(JSON.stringify({ event: "domain_block_backfill", domain, hidden }));
}

export async function mirrorDomainBlock(db: SqlDb, ctx: Pick<ExecutionContext, "waitUntil">, domain: string, reason: string): Promise<void> {
  // record the block synchronously (fast, indexed PK) — new accounts are then
  // caught by the enrich/cron paths via resolveHidden regardless of backfill.
  await db.prepare(
    `INSERT INTO blocked_domains (domain, reason) VALUES (?1, ?2)
     ON CONFLICT(domain) DO UPDATE SET reason = ?2, created_at = unixepoch()`
  ).bind(domain, reason).run();

  // backfill existing matches in the background so the client never blocks on
  // the scan. waitUntil keeps the worker alive past the response; the cron's
  // resolveHidden pass is the backstop if it doesn't finish.
  ctx.waitUntil(backfillDomainBlock(db, domain));
}

/** After unblocking `domain`, recompute effective hidden for every actor that
 *  matched it, from all REMAINING sources (labels + slur + other domain blocks
 *  + override) — so a label-hidden or still-domain-blocked account stays hidden.
 *  Bounded rowid windows (short locks); runs in the background. */
async function recomputeDomainUnblock(db: SqlDb, domain: string): Promise<void> {
  const remaining = await getBlockedDomains(db);
  const suffix = "%." + domain;
  const max = await maxRowid(db);
  let cursor = 0;
  let restored = 0;
  while (cursor < max) {
    const lo = cursor;
    cursor += BACKFILL_WINDOW;
    const { results } = await db.prepare(
      `SELECT did, handle, labels FROM actors
       WHERE rowid > ?1 AND rowid <= ?2 AND (handle = ?3 OR handle LIKE ?4) COLLATE NOCASE`
    ).bind(lo, cursor, domain, suffix).all<{ did: string; handle: string; labels: string }>();
    if (!results || results.length === 0) continue;

    const overrides = await getOverrides(db, results.map((r) => r.did));
    const stmts = results.map((r) => {
      let labels: any[] = [];
      try { labels = JSON.parse(r.labels); } catch { labels = []; }
      const hidden = resolveHidden({
        labels,
        handle: r.handle,
        override: overrides.get(r.did) ?? null,
        domainBlocked: isDomainBlocked(r.handle || "", remaining),
      });
      return db.prepare(
        "UPDATE actors SET hidden = ?1, updated_at = unixepoch() WHERE did = ?2 AND hidden != ?1"
      ).bind(hidden, r.did);
    });
    const batchResults = await db.batch(stmts);
    restored += batchResults.filter((x) => x.meta.changes > 0).length;
  }
  console.log(JSON.stringify({ event: "domain_unblock_recompute", domain, restored }));
}

export async function mirrorDomainUnblock(db: SqlDb, ctx: Pick<ExecutionContext, "waitUntil">, domain: string): Promise<void> {
  await db.prepare("DELETE FROM blocked_domains WHERE domain = ?1").bind(domain).run();
  ctx.waitUntil(recomputeDomainUnblock(db, domain));
}
