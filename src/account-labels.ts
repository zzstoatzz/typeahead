import { z } from "zod";
import { authorized, parseBody, rejectUnauthorized } from "./admin-auth";
import type { Stmt, TursoDB } from "./db";
import { parseLabels } from "./exclusions";
import { BSKY_MOD_DID, getBlockedDomains, getOverrides, isDomainBlocked, resolveHidden } from "./moderation";
import type { Env } from "./types";
import { json } from "./utils";

export const LABEL_CURSOR = "label_stream_seq";
export const MAX_LABELS = 1000;
export const MAX_RECONCILE_DIDS = 100;
const STATEMENTS_PER_TRANSACTION = 240;

const did = z.string().regex(/^did:(plc:[a-z2-7]{24}|web:[a-zA-Z0-9.:%-]{1,253})$/);
const label = z.object({
  src: z.string(),
  uri: z.string().max(512),
  val: z.string().min(1).max(128),
  cid: z.string().max(128).optional(),
  neg: z.boolean().optional(),
  cts: z.string().datetime({ offset: true }),
  exp: z.string().datetime({ offset: true }).optional(),
});
type Label = z.infer<typeof label>;

const streamLabel = label.extend({ seq: z.number().int().min(1) });

/** Labels are validated one at a time: a malformed frame is counted and
 *  skipped rather than failing the batch, which would stall the stream at it. */
const streamSchema = z.object({
  labels: z.array(z.unknown()).max(MAX_LABELS),
  cursor: z.number().int().min(0),
});
const reconcileSchema = z.object({
  dids: z.array(did).min(1).max(MAX_RECONCILE_DIDS),
  labels: z.array(z.unknown()).max(MAX_LABELS * 5),
});

const PROFILE_URI = /^at:\/\/(did:[^/]+)\/app\.bsky\.actor\.profile\/self$/;

/** The account a label is about, or null for a label we do not act on: only
 *  Bluesky's moderation service, and only labels on the account itself or on
 *  its profile record. */
export function labelSubject(l: Pick<Label, "src" | "uri">): string | null {
  if (l.src !== BSKY_MOD_DID) return null;
  const subject = PROFILE_URI.exec(l.uri)?.[1] ?? l.uri;
  return did.safeParse(subject).success ? subject : null;
}

/** A label is stored only for an account we index. Stream rows carry their
 *  seq and reconciliation rows seq 0, so a reconciliation answer never
 *  overwrites a label the stream delivered, and an older stream frame never
 *  overwrites a newer one. A negation is a row, not a delete, so a delayed
 *  assertion cannot resurrect a withdrawn label. */
function upsertLabel(db: TursoDB, subject: string, l: Label, seq: number): Stmt {
  return db.prepare(
    `INSERT INTO account_labels (did, uri, val, cid, neg, cts, exp, seq)
     SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8 WHERE EXISTS (SELECT 1 FROM actors WHERE did = ?1)
     ON CONFLICT(did, uri, val) DO UPDATE SET
       cid = excluded.cid, neg = excluded.neg, cts = excluded.cts, exp = excluded.exp, seq = excluded.seq
     WHERE excluded.seq >= account_labels.seq`,
  ).bind(subject, l.uri, l.val, l.cid ?? null, l.neg ? 1 : 0, l.cts, l.exp ?? null, seq);
}

/** `actors.labels` is also the label list search and profile responses serve,
 *  and most of it is self-labels from the profile record. This rewrites only
 *  Bluesky moderation's share from `account_labels`, keeps every other
 *  source's labels, and leaves the row (and `updated_at`, which drives
 *  Tantivy reconciliation) untouched when nothing changed. */
const PROJECTED_LABELS = `(SELECT json_group_array(json(v)) FROM (
    SELECT j.value AS v FROM json_each(CASE WHEN json_valid(actors.labels) THEN actors.labels ELSE '[]' END) j
     WHERE j.value->>'src' IS NOT ?2
    UNION ALL
    SELECT json_patch(json_object('src', ?2, 'uri', uri, 'val', val, 'cts', cts), json_object('cid', cid, 'exp', exp))
      FROM account_labels WHERE did = ?1 AND neg = 0
  ))`;

function projectLabels(db: TursoDB, subject: string): Stmt {
  return db.prepare(
    `UPDATE actors SET labels = ${PROJECTED_LABELS}, updated_at = unixepoch()
     WHERE did = ?1 AND labels IS NOT ${PROJECTED_LABELS}`,
  ).bind(subject, BSKY_MOD_DID);
}

function advanceCursor(db: TursoDB, cursor: number): Stmt {
  return db.prepare(
    `INSERT INTO counters (name, value) VALUES (?1, ?2)
     ON CONFLICT(name) DO UPDATE SET value = MAX(value, excluded.value)`,
  ).bind(LABEL_CURSOR, cursor);
}

/** Each account's label writes and its projection share a transaction. */
function transactions(groups: Stmt[][]): Stmt[][] {
  const out: Stmt[][] = [[]];
  for (const group of groups) {
    let current = out[out.length - 1]!;
    if (current.length > 0 && current.length + group.length > STATEMENTS_PER_TRANSACTION) {
      current = [];
      out.push(current);
    }
    current.push(...group);
  }
  return out;
}

/** The stored `hidden` flag nominates candidates for the exclusion count; the
 *  engine decides from labels at query time. Recomputed after the labels
 *  commit, through the same rule as every other write site. */
async function refreshHidden(db: TursoDB, dids: string[]): Promise<number> {
  if (dids.length === 0) return 0;
  type Row = { did: string; handle: string; labels: string; hidden: number };
  const { results } = await db.prepare(
    `SELECT did, handle, labels, hidden FROM actors WHERE did IN (${dids.map(() => "?").join(",")})`,
  ).bind(...dids).all<Row>();
  const [overrides, domains] = await Promise.all([getOverrides(db, dids), getBlockedDomains(db)]);
  const changed: Stmt[] = [];
  for (const row of results) {
    const hidden = resolveHidden({
      labels: parseLabels(row.labels),
      handle: row.handle,
      override: overrides.get(row.did) ?? null,
      domainBlocked: isDomainBlocked(row.handle, domains),
    });
    if (hidden !== Number(row.hidden)) {
      changed.push(db.prepare("UPDATE actors SET hidden = ?1 WHERE did = ?2").bind(hidden, row.did));
    }
  }
  if (changed.length > 0) await db.batch(changed);
  return changed.length;
}

export type LabelOutcome = { accepted: number; ignored: number; accounts: number; hidden_changed: number };

/** Applies one batch of label stream frames. The cursor commits with the last
 *  transaction, so a failure replays the batch, and replaying applied frames
 *  rewrites the same rows. */
export async function applyStream(db: TursoDB, body: z.infer<typeof streamSchema>): Promise<LabelOutcome> {
  const bySubject = new Map<string, Stmt[]>();
  let ignored = 0;
  for (const raw of body.labels) {
    const parsed = streamLabel.safeParse(raw);
    const subject = parsed.success ? labelSubject(parsed.data) : null;
    if (!parsed.success || !subject) {
      ignored++;
      continue;
    }
    const l = parsed.data;
    const group = bySubject.get(subject) ?? [];
    group.push(upsertLabel(db, subject, l, l.seq));
    bySubject.set(subject, group);
  }
  const groups = [...bySubject].map(([subject, stmts]) => [...stmts, projectLabels(db, subject)]);
  const txs = transactions(groups);
  txs[txs.length - 1]!.push(advanceCursor(db, body.cursor));
  for (const stmts of txs) await db.batch(stmts);
  const accounts = [...bySubject.keys()];
  return { accepted: body.labels.length - ignored, ignored, accounts: accounts.length, hidden_changed: await refreshHidden(db, accounts) };
}

/** queryLabels answers with each label's history, negations included; the
 *  latest record per subject and value is the current state. */
export function currentLabels(labels: Label[]): Label[] {
  const latest = new Map<string, Label>();
  for (const l of labels) {
    const key = `${l.uri}\n${l.val}`;
    const prior = latest.get(key);
    if (!prior || Date.parse(l.cts) >= Date.parse(prior.cts)) latest.set(key, l);
  }
  return [...latest.values()];
}

/** Replaces the reconciled state of each listed account with the labeler's
 *  current answer. Rows the stream delivered are kept: they are newer than
 *  any answer obtained after the stream started. */
export async function applyReconcile(db: TursoDB, body: z.infer<typeof reconcileSchema>): Promise<LabelOutcome> {
  const listed = new Set(body.dids);
  const bySubject = new Map<string, Label[]>(body.dids.map((d) => [d, []]));
  let ignored = 0;
  for (const raw of body.labels) {
    const parsed = label.safeParse(raw);
    const subject = parsed.success ? labelSubject(parsed.data) : null;
    if (!parsed.success || !subject || !listed.has(subject)) {
      ignored++;
      continue;
    }
    bySubject.get(subject)!.push(parsed.data);
  }
  let accepted = 0;
  const groups = [...bySubject].map(([subject, labels]) => {
    const current = currentLabels(labels);
    accepted += current.length;
    return [
      db.prepare("DELETE FROM account_labels WHERE did = ?1 AND seq = 0").bind(subject),
      ...current.map((l) => upsertLabel(db, subject, l, 0)),
      projectLabels(db, subject),
    ];
  });
  for (const stmts of transactions(groups)) await db.batch(stmts);
  return { accepted, ignored, accounts: body.dids.length, hidden_changed: await refreshHidden(db, body.dids) };
}

async function respond(event: string, apply: () => Promise<LabelOutcome & { cursor?: number }>): Promise<Response> {
  try {
    const outcome = await apply();
    if (outcome.accepted > 0 || outcome.hidden_changed > 0) console.log(JSON.stringify({ event, ...outcome }));
    return json({ ok: true, ...outcome });
  } catch (e) {
    const error = e instanceof Error ? e.message : String(e);
    console.log(JSON.stringify({ event: `${event}_error`, error }));
    return json({ error }, 500);
  }
}

export async function handleLabelStream(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/labels");
  const body = await parseBody(request, streamSchema);
  if (!body) return json({ error: "invalid label batch" }, 400);
  return respond("labels_applied", async () => ({ ...(await applyStream(db, body)), cursor: body.cursor }));
}

export async function handleLabelReconcile(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/labels/reconcile");
  const body = await parseBody(request, reconcileSchema);
  if (!body) return json({ error: "invalid reconciliation batch" }, 400);
  return respond("labels_reconciled", () => applyReconcile(db, body));
}

/** null until the consumer's first batch: it then starts at the stream head. */
export async function handleLabelCursor(request: Request, db: TursoDB, env: Env): Promise<Response> {
  if (!authorized(request, env)) return rejectUnauthorized(request, env, "/admin/labels/cursor");
  const row = await db.prepare("SELECT value FROM counters WHERE name = ?1").bind(LABEL_CURSOR).first<{ value: number }>();
  return json({ cursor: row ? Number(row.value) : null });
}
