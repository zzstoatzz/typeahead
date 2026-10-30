# discovery and identity

How an account enters the corpus, and how it gets a handle.

## the principle

A PDS is a PDS. No code path branches on where an account is hosted. Hosts differ
by measurable properties — account count, whether they answer `describeServer` —
never by identity.

## discovery: the relay's firehose

The ingester reads `com.atproto.sync.subscribeRepos` from the relay
(`services/src/main.zig`), not jetstream.

**Jetstream is a lossy projection of the relay.** It drops records the relay
carries, and it does so partially — most hosts flow through it fine — so the loss
cannot be found by spot-checking. See the memory note `project-did-web-discovery-gap`
for the measurement.

Consequences:

- No `wantedCollections` on the firehose. The allowlist lives in
  `ingest.wantedCollection`, and every frame is decoded before it can be discarded
  (~250 frames/s).
- One commit carries many ops; `handleCommit` loops `c.ops`.
- DAG-CBOR gives the avatar blob ref as a real CID (tag 42), not jetstream's
  pre-rendered `ref.$link`. `ingest.avatarCidOf` renders it.
- **Pin to one relay host.** Relay `seq` spaces are per-relay; a cursor is only
  valid for its issuer. KV key `relay_cursor`; `plausibleRelayCursor` rejects a
  stale jetstream µs value.

`listRepos` is a fallback if the firehose ever proves incomplete. Its cursor is
opaque and format varies by implementation — persist and replay what the host
returned, never construct one.

### hosts with identities but no sync stream

Measured 2026-09-18: habitat's pear mints `did:web` identities, serves their
DID documents and `/.well-known/atproto-did`, and answers `resolveHandle`, but
exposes no `subscribeRepos`. The relay never carries these accounts, so the
firehose never discovers them; `getProfile` finds one only after
`/request-indexing` adds it by hand. This is a measurable host property, not
a special case: "feeds a relay" is now something a host may or may not do, and
spaces hosts (point-to-point sync, no relay) will make it common. Membership
on such a host is often private on purpose, so bulk-indexing it into the
public corpus is not the answer either. Decision 2026-09-18: typeahead stays
the public-context service and private completion belongs to the org host —
see [private directories](private-directories.md).

## identity: batch, always

Resolving one DID per HTTP request does not work at corpus scale.

1. **`getProfiles`, 25 per call** — enrichment phase 1. ~154 DIDs/s, ~96% hit
   rate, and it returns display name and avatar so it feeds phase 2 too. Sized
   against arrivals (~2,900 new accounts/hour), not against the backlog.
2. **The ingester's identity pool** — every `#identity` event, new DID or
   existing (see below). Posts `pds` along with the handle.
3. **Cloud PDS repair (planned)** — the retired bulk PLC job selected missing
   handles, leaving actors with handles but no PDS untouched. Its home flow,
   deployment definitions and generic writer have been removed. The retained
   offline reconstruction and conditional repair tools are described in
   [repair contracts](pds-identity-repair.md). No recurring replacement is deployed.

## identity: handle changes are the live consumer's job

`#identity` is a prod to invalidate cached identity, not data: per the sync
spec it "does not … reliably indicate what the current state of the identity
is", and the bsky relay omits the optional `handle` (21/21 events sampled
2026-09-25). So for every `#identity` DID the ingester (`services/src/identity.zig`):

1. fetches the DID document from its authority — `plc.directory/{did}` or the
   `did:web` host — via `zat.DidResolver`, never from an identity cache;
2. verifies the claimed handle resolves back to the DID (`.well-known/atproto-did`,
   then DNS TXT) via `zat.HandleResolver`;
3. posts `{did, handle, pds}` to `/admin/ingest`, which overwrites the row's
   handle. An unverified handle is retried after 60 s and again after 15 min
   (a fresh domain can lag its DNS or well-known), writing only DID and pds
   meanwhile; if it still fails it is recorded as `handle.invalid`, the atproto
   convention the appview also serves, rather than keeping a previous handle
   that no longer resolves to the DID either. A failed POST is retried on the
   same schedule. The pending ingester change also retries an unreachable DID
   document on that schedule while recording the bare DID; it is not deployed yet.

`handle.invalid` rows recover when the handle verifies again: the next
`#identity` event re-resolves them, and appview enrichment writes whatever
handle the appview has verified.

Outcome counters (`verified`, `unverified`, `no_handle`, `did_failed`,
`retried`, `post_failed`, plus the pool's `dropped`) are on the ingester's
`/health`.

Until 2026-09-25 step 1 went through slingshot, an identity cache that nothing
had prodded. It answered seconds after the PLC op with the old handle for
~20–26% of handle changes (sampled 2026-09-22..24), and no later job rewrote a
non-empty handle, so those rows stayed stale indefinitely.

The backlog it left was repaired on 2026-09-25 by the one-off
`typeahead-handle-repair` Prefect deployment (my-prefect-server 83da98a): a drift
scan against the PLC bundles (cutoff 09-16) plus the PLC export up to the fix
found 21,569 rows whose handle differed from the DID document; 20,305 verified
handles were written (compare-and-set), 1,139 unverified handles were left as
they were. Afterwards 262/265 live sampled accounts matched the appview; the
3 misses were handles that do not currently verify at all.

A second run on 2026-09-26 (my-prefect-server 8abf9cc) applied the
`handle.invalid` rule to the backlog: of 8,770 rows that differed from their
DID document or stored `handle.invalid`, 2,029 got a verified handle back
(mostly `handle.invalid` rows that verify again) and 1,133 whose claimed handle
still failed after a 15-minute re-check were recorded as `handle.invalid`.
Afterwards 0/200 sampled `handle.invalid` rows had a handle that verifies.
A drift scan against the weekly PLC bundles alone over-reports staleness:
accounts that changed handle after the bundle cutoff look wrong while being
current; compare against the live DID document.

Every equality query on `actors.handle` needs `COLLATE NOCASE` or it scans the
table. Keyset on `rowid`, not `did`.

## the PLC bulk path

The PLC log supplies DID-authorized handle claims and PDS declarations. A handle
claim alone does not prove ownership: the handle must also resolve back to the
DID through DNS or HTTPS. The retired bulk writer and weekly deployment definitions have been removed.
Retained offline tools read weekly gzipped bundles; they do not authorize writes.

Since October 28, raw relay handle hints only enqueue the DID for verification.
Authenticated ingest accepts handle updates only with the verifier marker, or
the explicit `handle.invalid` sentinel. Manual indexing checks the authority DID
document and reverse handle resolution before writing. Existing invalid claims
are contained at the response boundary while offline repair is pending. See the
[repair record](ops/identity-repair-20261028/README.md).

Writes use one statement per batch via `json_each`, not one statement per row.
Each row change fires the `actors_au` trigger and rewrites FTS5, which is the
irreducible cost; everything around it is not.

Pace writes adaptively. Turso is single-writer and the live ingester shares it.

## reading the numbers

`SELECT COUNT(*) FROM actors WHERE handle = '' COLLATE NOCASE` is the health
metric. It is **not** a backlog. Split it before reacting:

```sql
-- never attempted: fresh firehose arrivals waiting on the hourly cron
SELECT COUNT(*) FROM actors
 WHERE handle = '' COLLATE NOCASE AND identity_checked_at = 0;

-- attempted and still unresolved: the genuinely hard ones
SELECT COUNT(*) FROM actors
 WHERE handle = '' COLLATE NOCASE AND identity_checked_at > 0;
```

The first number is a **working set**, not a problem. The firehose discovers
~2,900 accounts/hour and stamps them as bare DIDs; enrichment phase 1 resolves up
to `IDENTITY_LIMIT` per hourly tick. A steady value of a few thousand is the
system working. It only matters if it grows across many hours.

The second number is the real floor: deactivated accounts, `handle.invalid`,
and `did:web` accounts that are not in PLC at all. Expect low thousands.

## what fixes what

| symptom | cause | what to do |
| --- | --- | --- |
| never-attempted count growing over many hours | phase 1 not keeping pace with arrivals | raise `IDENTITY_LIMIT`, or check for 429s from `getProfiles` |
| attempted count growing | accounts the appview cannot see | bulk PLC repair is required; the home schedule is currently paused |
| hour missing from `snapshots` | cron did not fire, or a step failed | compare `cron_last_attempt` in KV — it is written before any step |
| ingester `pending` climbing | worker writes are slow or failing | check Turso write latency; see the single-writer note below |

## the constraint behind most incidents here

Turso is **single-writer**, and the live ingester shares it with every batch job.
Any bulk write must batch (one statement per batch, not per row) and pace against
observed latency. A job that saturates the writer starves ingestion, and the
ingester's retries can keep it saturated after the job stops.

If Turso becomes unresponsive during maintenance work, the workload is the cause.
Stop the job first, then confirm.
