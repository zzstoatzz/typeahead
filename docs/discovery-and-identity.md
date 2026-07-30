# discovery and identity

How an account enters the corpus, and how it gets a handle.

## the principle

A PDS is a PDS. No code path branches on where an account is hosted. Hosts differ
by measurable properties — account count, whether they answer `describeServer` —
never by identity.

## discovery: the relay firehose

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

## identity: batch, always

Resolving one DID per HTTP request does not work at corpus scale.

1. **`getProfiles`, 25 per call** — enrichment phase 1. ~154 DIDs/s, ~96% hit
   rate, and it returns display name and avatar so it feeds phase 2 too. Sized
   against arrivals (~2,900 new accounts/hour), not against the backlog.
2. **The ingester's slingshot pool** — resolves new DIDs from `#identity` events
   and posts `pds` along with the handle.
3. **`scripts/plc-identity-sync.py`** — bulk, authoritative, weekly. Covers the
   ~4% `getProfiles` cannot see (self-hosted PDSes) and is the only source of
   `pds` in bulk.

Every equality query on `actors.handle` needs `COLLATE NOCASE` or it scans the
table. Keyset on `rowid`, not `did`.

## the PLC bulk path

The PLC log is the authoritative source for handle and pds, and both appear on
100% of ops. Bundles are weekly gzipped files; details and gotchas are in the
memory note `reference-plc-bulk-identity`.

Writes use one statement per batch via `json_each`, not one statement per row.
Each row change fires the `actors_au` trigger and rewrites FTS5, which is the
irreducible cost; everything around it is not.

Pace writes adaptively. Turso is single-writer and the live ingester shares it.
