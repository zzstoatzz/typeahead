# Label stream

**Status: proposal, 2026-11-01. Not implemented.**

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
stream from the head seq recorded at cutover. It keeps labels from the adopted
source whose subject is a DID or that DID's `app.bsky.actor.profile/self`, and
posts batches with the last seq to an authenticated worker endpoint. The worker
writes only rows for DIDs in `actors`; others are dropped. Live traffic is about
2M small frames a month, so it is cheap to download in full.

**Storage.** A new table `account_labels(did, subject, val, neg, cts, exp, seq)`,
one row per `(did, subject, val)`, where `subject` is `account` or `profile`.
A negation is stored as a row, not a delete, so a delayed older assertion cannot
resurrect it. Stream rows carry their seq; reconciliation rows carry seq 0. The
same transaction:

1. upserts the batch where `excluded.seq >= account_labels.seq`, so a stream
   row always wins over a reconciliation row;
2. rewrites `actors.labels` from `account_labels` for the affected DIDs, with
   `updated_at = unixepoch()` so Tantivy reconciles them;
3. writes the cursor.

**Reconciliation.** A one-time cloud job (Prefect, not HeavyPad) walks indexed
DIDs by rowid, 100 per `queryLabels` call, paced, and writes current state at
seq 0. It skips `handle.invalid` and quarantined rows (1.78M, most of the spam
wave). Order: the 3,043 hidden accounts first (31 calls, which also clears
stale exclusions), then the 41k scored-visible accounts, then the rest. The
walk is complete when its rowid cursor passes the maximum; its progress is a
measured fraction, not a sample.

**New accounts.** A newly discovered DID carries no reconciliation row. The
discovery path enqueues a label lookup that batches up to 100 DIDs per call at
about 2,900 discoveries an hour (about 29 calls an hour), using a durable queue
like `identity_queue`, so a restart cannot drop it.

**Ownership.** `actors.labels` gets one writer. The three `getProfiles` writers
(`refreshModeration`, admin enrich, backfill) stop writing labels and `hidden`
once reconciliation finishes. Until then they keep writing, so nothing regresses
mid-walk.

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
