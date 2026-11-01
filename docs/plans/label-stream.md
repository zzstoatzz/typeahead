# Label stream

**Status, 2026-11-01:** step 1 (worker storage and write path) is
committed and unit-tested, not deployed; nothing calls it yet. Steps 2–5 are
proposals. Build order is at the end.

Moderation labels reach Typeahead only when a `getProfiles` call happens to
revisit an account. Hidden accounts are never revisited, so a withdrawn label
keeps an account hidden indefinitely, and a new label on a visible account waits
for the next sample. This plan follows Bluesky's label stream from now on and
reconciles current label state for the accounts we index, without replaying
the stream's history.

## Why not another refresh pass

Every freshness job handed to a fixed-size cron sample has fallen silently
behind: a 245-day sweep, a cursor that never advanced, counts frozen by a skip
predicate, enrichment sized below arrivals, a wall kill in 120 of 140 runs.
A sample has no coverage measure, so "behind" looks like "done". Labels have
a change feed; use it, with a cursor committed after its effects and a measured
lag, as the [identity queue](../discovery-and-identity.md) does. The one-time
reconciliation below is a bounded walk with a known end, not a recurring sample.

## Agreed policy

- One public policy.
- Adopt only Bluesky's moderation service (`did:plc:ar7c4by46qjdydhdevvrndac`)
  with the current values: `!hide`, `!takedown`, `!suspend`, `spam`. The other
  labelers on the reputation page stay informational.
- Only **account-level** labels (subject `uri` is the DID) exclude an account.
  On 2026-11-01, 2,623 of 2,647 label-hidden accounts had an account-level label;
  24 were hidden only by labels on their `app.bsky.actor.profile` record
  (13 `!takedown`, 10 `!hide`, 2 `spam`). Bluesky's typeahead returns those
  accounts.
- A `!takedown` or `!hide` on the profile record follows Bluesky: the account
  stays searchable by handle, and its display name and avatar are neither shown
  nor matched. Bluesky omits both from the view; whether it still matches the
  display name could not be isolated, so not matching is our choice.
- A manual `show` override waives local rules and Bluesky `!hide`/`spam`, but
  not Bluesky `!takedown`/`!suspend`.
- The emergency Bluesky fallback applies the same rule to its results, using
  their labels plus local rules from Turso. If Turso is also unavailable, it
  returns the results unfiltered.

## Source measurements (2026-11-01)

Both endpoints are public on `mod.bsky.app`.

`com.atproto.label.subscribeLabels` replays from any seq; `cursor=0` starts at
the first label (2023-04-13). Seq by date:

| seq | label created |
| --- | --- |
| 1 | 2023-04-13 |
| 10M | 2025-02-20 |
| 30M | 2026-02-26 |
| 40M | 2026-08-09 |
| 44M | 2026-10-12 |
| 45,261,615 (head) | 2026-11-01 |

The recent rate is about 2M labels a month. Mean frame size was 326 bytes over
the first 2,000 and 240 bytes over 2,000 near seq 45M, so a full replay is
roughly 11–15 GB, and even a replay from March 2026 is about 15M frames. In a
60 s live sample, 1 of 24 labels was account-level; the rest were on records.
No sampled label carried `exp`.

`com.atproto.label.queryLabels` returns current state for listed subjects,
including negations. One call took 100 DIDs in 0.9 s; 200 DIDs exceeded the
250-label page and returned a pagination cursor.

Filtering the stream by our index saves writes, not download: the stream has no
server-side filter. Per-DID queries are the way to touch only our accounts.

## Design

**Live consumer.** A Zig task in the ingester, beside the PLC tail, reads the
stream from the stored cursor (`GET /admin/labels/cursor`), or from the head
when there is none. It keeps labels from the adopted source whose subject is a
DID or that DID's `app.bsky.actor.profile/self`, every value included, and
posts batches with the last seq to `POST /admin/labels`. The worker writes only
rows for DIDs in `actors`; others are dropped. A malformed label is counted as
ignored rather than failing the batch, so one bad frame cannot stall the
stream. Live traffic is about 2M small frames a month, so it is cheap to
download in full.

**What the stream owns.** `actors.labels` is also the label list search and
profile responses serve. In a 20,000-row sample on 2026-11-01 most of it was
self-labels from the profile record (`!no-unauthenticated` 18,015, bridgy
markers, `bot`), with Bluesky's informational labels (`porn`, `nudity`) next.
The stream owns only the entries whose `src` is Bluesky's moderation service;
every other source's labels pass through untouched.

**Storage.** Migration 009 adds `account_labels(did, uri, val, cid, neg, cts,
exp, seq)`, one row per `(did, uri, val)`, where `uri` is the DID or its
profile record. A negation is stored as a row, not a delete, so a delayed older
assertion cannot resurrect it. Stream rows carry their seq; reconciliation rows
carry seq 0. The cursor is `counters.label_stream_seq` and only moves forward.
For each account, one transaction:

1. upserts its labels where `excluded.seq >= account_labels.seq`, so a stream
   row always wins over a reconciliation row and an older frame never
   overwrites a newer one;
2. rewrites Bluesky moderation's share of `actors.labels` from its non-negated
   rows, with `updated_at = unixepoch()` so Tantivy reconciles it, only when
   the result differs.

The cursor commits with the batch's last transaction, so a failure replays the
batch and replaying rewrites the same rows. The stored `hidden` flag is then
recomputed through `resolveHidden` for the affected accounts.

Until the serving rule changes (step 5), a profile-record label still hides an
account exactly as it does today; the stream changes where labels come from,
not what they mean.

**Reconciliation.** A one-time cloud job (Prefect, not HeavyPad) walks indexed
DIDs by rowid, 100 per `queryLabels` call, paced, and posts each answer with
the DIDs it asked about to `POST /admin/labels/reconcile`. The worker folds the
history to the latest record per value, replaces that account's seq-0 rows, and
clears moderation labels a profile fetch left behind. It skips `handle.invalid` and quarantined rows (1.78M, most of the spam
wave). Order: the 3,043 hidden accounts first (31 calls, which also clears
stale exclusions), then the 41k scored-visible accounts, then the rest. The
walk is complete when its rowid cursor passes the maximum; its progress is a
measured fraction, not a sample.

**New accounts.** A newly discovered DID carries no reconciliation row. The
discovery path enqueues a label lookup that batches up to 100 DIDs per call at
about 2,900 discoveries an hour (about 29 calls an hour), using a durable queue
like `identity_queue`, so a restart cannot drop it.

**Ownership.** Bluesky moderation's share of `actors.labels` gets one writer.
Once reconciliation finishes, the three `getProfiles` writers
(`refreshModeration`, admin enrich, backfill) keep writing other sources'
labels but preserve the Bluesky moderation entries. Until then they keep
writing everything, so nothing regresses mid-walk.

**Expiry.** No scheduler is needed for search: Tantivy evaluates `exp` at
query time. The stored `hidden` flag is no longer treated as truth; the daily
exclusion count already re-derives it.

**Observability.** Ingester `/health` reports cursor seq, the newest label's
`cts`, lag, and frames, labels and write failures. `/health/freshness` treats
label lag beyond a bound as stale. The reconciliation job reports its rowid
progress and per-call failures.

## Open questions

- `queryLabels` rate limits are unknown. Twelve million indexed DIDs is about
  110k calls; pace from measured responses and stop on 429s.
- Whether a label created between the reconciliation call for a DID and the
  stream cutover can be missed. Start the stream before reconciliation begins,
  so every change after the start is covered by seq.

## Gates

- Negations and out-of-order seq apply correctly (unit tests over recorded
  frames).
- A reconciliation row never overwrites a stream row.
- A crash between write and cursor save replays without double effects.
- Add, negate and expire a label on an account that emits no other events;
  search reflects each change.
- A sample of accounts matches `queryLabels` after reconciliation.
- Profile-record takedown: the account is returned by handle, without display
  name or avatar, on search, profile endpoints and fallback.
- Override precedence is identical in `resolveHidden` and Tantivy's `moderation.rs`.
- The reconciliation write rate stays inside Turso's single-writer budget.

## Build order

Each step ships and is verified before the next.

1. Worker storage and write path: migration 009, `src/account-labels.ts`,
   unit tests over hand-built labels in the decoded frame shape. Tests over
   recorded CBOR frames come with the consumer. **Committed, not deployed.**
2. Live consumer in the ingester, starting at head, with lag in `/health`.
   Deploy order: worker, then ingester (which applies migration 009), then smoke.
3. One-time reconciliation as a cloud job: hidden accounts, then scored, then
   the rest.
4. Label lookups for newly discovered accounts through a durable queue.
5. Hand over ownership; apply the profile-record policy (searchable by handle,
   display name and avatar withheld) in `resolveHidden`, Tantivy and fallback.
