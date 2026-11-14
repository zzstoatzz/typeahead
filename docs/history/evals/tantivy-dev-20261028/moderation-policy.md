# Operator moderation policy

October 29, 2026. Local parallel-engine source adapter; deployment, native
operator inventory and performance admission remain separate root-owned gates.

## Authority and precedence

The Worker stores DID actions in `mod_overrides` (DID primary key) and blocked
handle domains in `blocked_domains` (domain primary key). Canonical
`src/moderation.ts::resolveHidden` applies label restrictions, explicit handle
slur detection, apex/subdomain blocks, then per-DID show/hide. A show override is
the final word and can rescue label or domain false positives. Negated and
expired labels do not hide; only the canonical Bluesky moderator's configured
hide labels apply. `!no-unauthenticated` does not hide identity search results.

`actors.hidden` is a materialization, not the complete authority. Policy writes
and actor updates are separate statements. Domain backfills run asynchronously;
policy deletion has no tombstone. A source adapter cannot establish current
policy merely by polling actor timestamps or scanning policy `created_at`.

## Atomic source observation

With `--policy-mode snapshot`, every source-owned batch first obtains backend
state, then reads selected current actors and the complete bounded policy in
one fixed read-only SQLite statement (`CURRENT_POLICY_SQL`). Its JSON envelope
includes actor rows once, overrides once, domains once and a source clock. Actor
lookup uses DID primary-key searches; each policy table is read in indexed key
order with a 10,001-row sentinel. More than 10,000 rows in either policy table
fails the whole observation. Unknown actions also fail closed. Rules are never
silently truncated. Empty actor batches still obtain a policy snapshot, so policy
changes do not depend on actor activity or incremental cursor position.

`policy_snapshot` is committed with the actor batch and observation cursor:

```json
{"schema_version":1,"source_observed_at":1793280000,"overrides":[{"did":"did:example:actor","action":"hide"}],"blocked_domains":["blocked.test"],"complete":true}
```

A rule deletion changes the canonical policy hash. A newer clock with unchanged
rules refreshes observation freshness without requiring profile reindexing. The
adapter does not approximate canonical moderation in Python. The backend owns
canonical evaluation and must apply it before accepting top-K candidates,
refilling past rejected candidates. A global policy change must not wait for a
corpus sweep, and a show override must be able to rescue a stored profile whose
old materialized `hidden` flag is true. Physical actor deletion remains separate.

The flag defaults off until backend policy admission, pre-K correctness and
query cost are qualified. The previous source-only image and its receipts do
not qualify policy snapshots.

## Native operator rows

Native `actor_overlay` and `prefix_overlay` permit `source=1` operator rows and
preserve them across snapshot compaction. An actor NULL blob can be a global DID
tombstone. Repository inspection found no production writer of
`overlay_source.operator`; the explicit `source=1` write is in an overlay unit
test. This is not evidence that real native operator state is absent. These rows
are local native state, not reconstructible from Turso policy tables. A bounded
root-owned inventory/attestation must establish absence or define and test an
explicit migration with preserved precedence. Nonzero unowned rows block any
cutover discussion.

## Verification and freshness gates

Actual HTTP/SQLite tests verify one-statement profile/policy reads, deletion
hashes without actor timestamp changes, incomplete-policy cap rejection,
unknown-action rejection and primary-key actor query plans. The optional actual
Rust integration (`test_rust.py --policy-checks`) exercises more than twenty
high-ranked blocked candidates before eligible results, global/PDS/preference
queries, show-over-label/domain precedence, expired labels, policy deletion
without reindexing, and backend rejection of incomplete or oversized snapshots.
Its receipt must pass against the exact backend source and binary before those
behaviors are called qualified.

Policy freshness must be independent of full-corpus sweep age. A reasonable
readiness contract exposes the last successful complete policy observation and
requires it within a declared short bound; source failures must not extend that
bound. Source polling progress and the last completed identity round remain
separate metrics. A policy error cannot be converted into an empty rule set or
waived by a fresh actor watermark. Exact limits and production pacing require
root-owned workload measurement; this document makes no deployed guarantee.

## Local Rust generation integration

The local backend now commits a validated cached policy with source documents,
revision, cursor and durable retry receipt. First policy enablement changes the
format to `tantivy-0.27-f32-did-policy-v3`; source-only v2 readers reject it.
Canonical moderation and CJK verification share one hydrated profile before
candidate acceptance, and accepted hydration is reused. Query metadata carries
only build identity, avoiding per-query cloning of the full policy. Debug search
separately reports hydration count/time and moderation time.

Policy freshness is independently limited to 300 seconds, including fixture
allow-stale mode. Missing policy in a later batch does not refresh the clock.
Public status redacts full snapshots and retry receipts. Its separate candidate
cutover source gate uses polling age 120 seconds, policy age 300 seconds and
completed reconciliation round age/duration at most 86,400 seconds. These are
initial unqualified SLOs, not measured corpus-wide guarantees. Completion records
`last_completed_started_at` and `last_completed_at`; old round receipts without
both clocks do not establish sweep duration.

The policy-on synthetic integration receipt records the exact binary and test
source. Passing it establishes local semantics, expiry/readiness behavior,
public redaction and policy/source commit recovery, not a live deployment or
native-speed parity on the full corpus.
