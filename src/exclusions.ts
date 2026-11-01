import { z } from "zod";
import { rowsAs, type TursoDB } from "./db";
import { isSearchExcludedHandle } from "./identity-policy";
import quarantine from "./identity-quarantine.json";
import { getBlockedDomains, isDomainBlocked, resolveHidden } from "./moderation";

export const EXCLUSIONS_KEY = "search_exclusions";
export const EXCLUSIONS_REFRESH_MS = 23 * 3_600_000;

const exclusionsSchema = z.object({
  version: z.literal(2),
  as_of: z.string(),
  policy_hidden: z.number().int().nonnegative(),
  stale_hidden_flags: z.number().int().nonnegative(),
  handle_invalid: z.number().int().nonnegative(),
  quarantined: z.number().int().nonnegative(),
});
export type SearchExclusions = z.infer<typeof exclusionsSchema>;

export function parseExclusions(raw: string | null): SearchExclusions | null {
  if (raw === null) return null;
  try {
    const parsed = exclusionsSchema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

type ExclusionsKv = {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
};

const sqlString = (value: string) => `'${value.replaceAll("'", "''")}'`;
const QUARANTINED = quarantine.map((h) => sqlString(h.toLowerCase())).join(", ");
/** Two scalar counts, each an index SEARCH. Grouping one IN() count by handle
 *  instead sorts every matching entry in a temp b-tree: 21s against 4s. */
export const IDENTITY_COUNT_SQL = `SELECT
  (SELECT COUNT(*) FROM actors WHERE handle = 'handle.invalid' COLLATE NOCASE) AS handle_invalid,
  (SELECT COUNT(*) FROM actors WHERE handle COLLATE NOCASE IN (${QUARANTINED})) AS quarantined`;

export function parseLabels(raw: string): unknown[] {
  try {
    const labels: unknown = JSON.parse(raw || "[]");
    return Array.isArray(labels) ? labels : [];
  } catch {
    return [];
  }
}

/** Counts what search excludes, by the rule the serving engine applies at
 *  query time. The stored `hidden` flag only nominates candidates: the mod
 *  refresh never revisits a hidden row, so flags set under older rules
 *  outlive them. Daily, because the quarantined-handle count walks every
 *  index entry of one PLC spam wave (1.76M on 2026-11-01). */
export async function recordExclusions(db: TursoDB, kv: ExclusionsKv, now = new Date()): Promise<SearchExclusions | null> {
  const previous = parseExclusions(await kv.get(EXCLUSIONS_KEY));
  if (previous && now.getTime() - Date.parse(previous.as_of) < EXCLUSIONS_REFRESH_MS) return null;

  const [flaggedRes, overridesRes, identityRes] = await db.batch([
    db.prepare("SELECT did, handle, labels FROM actors WHERE hidden != 0"),
    db.prepare("SELECT did, action FROM mod_overrides"),
    db.prepare(IDENTITY_COUNT_SQL),
  ], "read");
  type Candidate = { did: string; handle: string; labels: string };
  const flagged = rowsAs<Candidate>(flaggedRes.results);
  const overrides = new Map(rowsAs<{ did: string; action: "show" | "hide" }>(overridesRes.results).map((r) => [r.did, r.action]));

  const flaggedDids = new Set(flagged.map((r) => r.did));
  const hideOnly = [...overrides].filter(([did, action]) => action === "hide" && !flaggedDids.has(did)).map(([did]) => did);
  const unflagged = hideOnly.length === 0 ? [] : (await db.prepare(
    `SELECT did, handle, labels FROM actors WHERE did IN (${hideOnly.map(() => "?").join(",")})`
  ).bind(...hideOnly).all<Candidate>()).results;

  const domains = await getBlockedDomains(db);
  let policyHidden = 0;
  let staleFlags = 0;
  for (const row of [...flagged, ...unflagged]) {
    if (!row.handle || isSearchExcludedHandle(row.handle)) continue;
    const hidden = resolveHidden({
      labels: parseLabels(row.labels),
      handle: row.handle,
      override: overrides.get(row.did) ?? null,
      domainBlocked: isDomainBlocked(row.handle, domains),
    });
    if (hidden) policyHidden++;
    else if (flaggedDids.has(row.did)) staleFlags++;
  }

  const [identity] = rowsAs<{ handle_invalid: number; quarantined: number }>(identityRes.results);

  const exclusions = {
    version: 2,
    as_of: now.toISOString(),
    policy_hidden: policyHidden,
    stale_hidden_flags: staleFlags,
    handle_invalid: Number(identity?.handle_invalid ?? 0),
    quarantined: Number(identity?.quarantined ?? 0),
  } satisfies SearchExclusions;
  await kv.put(EXCLUSIONS_KEY, JSON.stringify(exclusions));
  return exclusions;
}
