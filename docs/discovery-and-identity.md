# discovery and identity

How an account gets into the corpus, and how it gets a handle. Written
2026-07-28 after both paths turned out to be broken in different ways.

## the principle

**A PDS is a PDS.** No code path may branch on where an account is hosted.
Hosts differ by measurable properties — account count, whether they answer
`describeServer` — never by identity. Any heuristic that reads a hostname to
decide what an account deserves is a bug. This is the whole premise of the
protocol, and two of our subsystems had quietly violated it.

## discovery: consume the relay, never jetstream

The ingester reads `com.atproto.sync.subscribeRepos` from the relay directly
(`services/src/main.zig`). It used to read jetstream.

**Jetstream is a lossy projection of the relay, and it is silent about it.**
Measured 2026-07-28 for the six repos on `pds.zat.dev`:

| layer | result |
| --- | --- |
| the PDS's own firehose | emits, 1,140 frames from `cursor=0` |
| relay ingest (`listHosts` per-host `seq`) | advancing, 1869 → 1911 |
| relay store (`getRepoStatus`) | rev-for-rev current with the PDS, 6/6 repos |
| **relay `subscribeRepos`** | **re-emits — two live likes caught 9s apart** |
| **jetstream, 7-day replay** | **0 events, 6/6 repos** |

Everything upstream is healthy; the loss is entirely in the projection. It is
also *partial* — `blacksky.app` flows through jetstream fine — so it cannot be
found by spot-checking a host. That is what made it survive so long.

Consequences for the ingester:

- The firehose has no `wantedCollections`, so the allowlist moved client-side
  into `ingest.wantedCollection` and every frame is decoded before it can be
  discarded (~248 frames/s).
- One commit carries many ops; `handleCommit` loops `c.ops`.
- DAG-CBOR gives the avatar blob ref as a real CID (tag 42), not jetstream's
  pre-rendered `ref.$link` string — `ingest.avatarCidOf` renders it.
- **The cursor is a relay `seq`, not jetstream's µs timestamp.** Separate KV key
  (`relay_cursor`), and `plausibleRelayCursor` rejects anything ≥1e12. That
  guard is not theoretical: during the cutover the still-running jetstream
  ingester wrote `1785217869995374` into the new key, and the guard caught it in
  production. Subscribing at a seq that far ahead yields an empty stream, which
  the watchdog would have reported as a dead upstream.

`listRepos` is a fallback discovery path if the firehose ever proves incomplete.
Its cursor is **opaque and implementation-specific** — `pds.zat.dev` returns
`1780420119:did:…` (seconds, one colon), a bsky host returns
`1706750763394::did:…` (millis, two colons). Never construct one; persist and
replay what the host returned. A synthesized cursor returned 0 repos, which is
indistinguishable from "no new accounts".

## identity: batch, or it never converges

Resolving `DID → (handle, pds)` one HTTP request at a time does not work at
corpus scale. On 2026-07-28 the enrichment cron had **1,311,817 rows with no
handle and 9,315,512 with no pds**, both draining at 500/run — 109 years and
212 years. Every row sat at `identity_checked_at=0`, so the `ORDER BY … ASC`
tiebreak was arbitrary and a given DID might never surface at all.

Three paths now, in cost order:

1. **`getProfiles`, 25 DIDs per call** — enrichment phase 1. ~154 DIDs/s, 96%
   hit rate measured against the real backlog, and it returns display name and
   avatar too, so it feeds phase 2 in the same response.
2. **The ingester's slingshot pool** — resolves new DIDs from `#identity`
   events. It already fetched `pds` and was throwing it away; it now posts it,
   which is what let phase 1b be deleted rather than merely shrunk.
3. **`scripts/plc-identity-sync.py`** — bulk, authoritative, for the residual.
   `getProfiles` only knows accounts the appview indexed; the ~4% it misses are
   exactly the self-hosted-PDS accounts, and it never returns `pds` at all.

Deleted, not disabled: phase 1b's per-DID slingshot pds backfill, and
`scripts/backfill-pds.py`. Both were structurally incapable of draining their
backlogs, and their presence disguised that.

## the PLC log is the bulk source

Handles and PDS endpoints live in the PLC log, and **100% of ops carry both**
(verified across a full week's bundle). fig's
[Allegedly](https://tangled.org/@microcosm.blue/Allegedly) — the tool behind
`plc.wtf` — makes it bulk-readable as weekly gzipped bundles:

```bash
allegedly bundle --dest ./plc-weekly          # scrape the unpublished tail
allegedly backfill --dir ./plc-weekly | scripts/plc-identity-sync.py --apply
```

Measured 2026-07-28 against `https://plc.t3.storage.dev/plc.directory/`:

- 149 of 193 weeks published, **27.96 GB gzipped**, complete from 2022-11-17
- **published bundles end 2025-09-18** — the missing weeks are the recent tail,
  not gaps. `allegedly bundle` fills them and skips weeks already on disk.
- one week = 57 MB gz → 220 MB raw, 310,060 ops, parsed at **343k ops/sec/core**
- `bundle` deliberately stops ~73h short of now, since PLC ops can be
  invalidated inside that window. The firehose `#identity` events cover the rest.

`allegedly backfill` output is **unordered** — current state must come from the
op with the greatest `createdAt` per DID, never the last one seen. Getting that
wrong silently resurrects stale handles.

Other microcosm services do **not** help here: slingshot is structurally
one-DID-per-request (`resolveMiniDocs` 404s, repeated `identifier` params return
only the first), and `plc.wtf` runs in wrap mode with no `/export`, though
`GET /{did}` works as a second per-DID resolver to fan out across.
