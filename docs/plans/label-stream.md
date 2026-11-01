# Label stream

**Status: proposal, 2026-11-01. Not implemented.**

Moderation labels reach Typeahead only when a `getProfiles` call happens to
revisit an account. Hidden accounts are never revisited, so a withdrawn label
keeps an account hidden indefinitely, and a new label on a visible account waits
for the next sample. This plan replaces sampling with Bluesky's label stream.

## Why not another refresh pass

Every freshness job handed to a fixed-size cron sample has fallen silently
behind: a 245-day sweep, a cursor that never advanced, counts frozen by a skip
predicate, enrichment sized below arrivals, a wall kill in 120 of 140 runs.
A sample has no coverage measure, so "behind" looks like "done". Labels have
a change feed; use it, with a cursor committed after its effects and a measured
lag, as the [identity queue](../discovery-and-identity.md) does.

## Agreed policy

- One public policy.
- Adopt only Bluesky's moderation service (`did:plc:ar7c4by46qjdydhdevvrndac`)
  with the current values: `!hide`, `!takedown`, `!suspend`, `spam`. The other
  labelers on the reputation page stay informational.
- Only **account-level** labels (subject `uri` is the DID) exclude an account.
  On 2026-11-01, 2,623 of 2,647 label-hidden accounts had an account-level label;
  24 were hidden only by labels on their `app.bsky.actor.profile` record
  (13 `!takedown`, 10 `!hide`, 2 `spam`). Bluesky's typeahead returns those
  accounts, omitting display name and avatar where the profile record is taken
  down.
- A manual `show` override waives local rules and Bluesky `!hide`/`spam`, but
  not Bluesky `!takedown`/`!suspend`.
- The emergency Bluesky fallback applies the same rule to its results, using
  their labels plus local rules from Turso. If Turso is also unavailable, it
  returns the results unfiltered.

## Source measurements (2026-11-01)

`wss://mod.bsky.app/xrpc/com.atproto.label.subscribeLabels` is public.

| | value |
| --- | --- |
| replay from `cursor=0` | full history: seq 1 is a label from 2023-04-13 |
| head seq | 45,261,615 |
| live rate | 24 labels in 60 s (about 35k/day) |
| account-level share | 1 of 24 live; 22 of the first 2,000 historical |
| replay delivery | 2,000 frames in 1 s |
| frame size | mean 326 bytes (first 2,000), 240 bytes (2,000 from seq 45M) |
| labels with `exp` | 0 in both samples |

A full replay is about 45M frames, roughly 11–15 GB, and hours of
delivery. It must not run on a laptop or HeavyPad.

## Design

**Consumer.** A Zig task in the ingester, beside the PLC tail, reads the stream,
keeps account-level labels from the adopted source, and posts batches with the
last seq to an authenticated worker endpoint. The ingester does not filter by
indexed DID: a label can arrive before its account is discovered.

**Storage.** A new table `account_labels(did, val, neg, cts, exp, seq)`, one row
per `(did, val)`, keeps the latest assertion by `seq`. A negation is stored as a
row, not a delete, so a replayed older assertion cannot resurrect it. The same
transaction:

1. upserts the batch's rows where `excluded.seq > account_labels.seq`;
2. rewrites `actors.labels` from `account_labels` for the affected DIDs that exist,
   with `updated_at = unixepoch()` so Tantivy reconciles them;
3. writes the cursor.

**Ownership.** `actors.labels` gets one writer. The three `getProfiles` writers
(`refreshModeration`, admin enrich, backfill) stop writing labels and `hidden`
once the replay has caught up. New actors take labels from `account_labels`
in the ingest insert, an indexed lookup per new DID.

**Expiry.** No scheduler is needed for search: Tantivy evaluates `exp` at
query time. The stored `hidden` flag is no longer treated as truth; the daily
exclusion count already re-derives it.

**Bootstrap.** Replay from `cursor=0` once, on a temporary cloud machine running
the same consumer with paced, batched writes; then the ingester continues from
the replay's last seq. Until the replay catches up, the existing writers keep
labels, so nothing regresses mid-replay.

**Observability.** Ingester `/health` reports cursor seq, the newest label's
`cts`, lag, and frames, labels and write failures. `/health/freshness` treats
label lag beyond a bound as stale.

## Open questions

- Profile-record `!takedown`: hide the display name and avatar the way Bluesky
  does, or leave presentation unchanged? This needs a Tantivy change if the
  display name should also stop matching queries.
- Exact account-level share over the full history, which sets the replay's
  write volume; measure it during the replay rather than estimating it now.
- Whether the stream's history includes labels later removed by Bluesky
  without a negation. If so, a replay could restore labels the appview no
  longer reports; compare a sample against `getProfiles` before the cutover.

## Gates

- Replay applies negations and out-of-order seq correctly (unit tests over
  recorded frames).
- A crash between write and cursor save replays without double effects.
- Add, negate and expire a label on an account that emits no other events; search
  reflects each change.
- A `getProfiles` write cannot overwrite a newer stream decision.
- A sample of accounts matches `getProfiles` labels after the replay.
- Override precedence is identical in `resolveHidden` and Tantivy's `moderation.rs`.
- Measured replay write rate stays inside Turso's single-writer budget.
