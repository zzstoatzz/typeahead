# Ingester event-loss mitigation and recovery

Completed September 5, 2026, approximately 23:23 UTC. The overflow loss mechanism
is fixed in production, the retained event window has been audited, and all
377 restored active accounts have been found through the serving search engine.
This does not establish that the historical period was lossless.

## Completion audit

| Requirement | Evidence and result |
| --- | --- |
| Preserve failed batches and replay cursor | `services/src/ingest.zig` retains ingest/delete queues, retries with backpressure, and does not acknowledge past failed or partially flushed commits. Regression tests exercise full backlog failures, failed deletes, split commits, and identity discovery failures. |
| Resume safely after restart | `services/src/main.zig` fetches the persisted cursor, initializes both handler and relay from it, refuses to start without a valid cursor, and pins the relay sequence space to bsky.network. Deployment logs recorded resume from cursor 33366401405. |
| Test the implementation | Current local Zig suite: 135/135 tests passed, 12/12 build steps succeeded. Two of these tests cover the separate, undeployed search optimization. The deployed identity/retry build previously passed 133 tests and ReleaseSafe compilation. |
| Deploy and observe real failure handling | Fly currently reports ingester image `deployment-01M1SP6X02DA2XYR0HE0ZR3YZJ`, digest `sha256:b2b717af9529e2a3653b3137152aa08620010e756fe9df92acaecfa239743169`. The deployed retry path retained 4,000 entries and one delete after a real upstream timeout at 20:22 UTC and subsequently drained them. The later identity-discovery deployment preserved that path. |
| Mitigate upstream contention | Cancelling bulk enrichment reduced write latency and cleared ingestion lag. Its daily schedule is now inactive, with no running, pending, or scheduled runs at the final check. The queued enrichment backlog is retained. |
| Investigate available historical evidence | Captured 4,184,560 relay frames over sequences 33310380734–33314565292 and audited presence for all 409,824 distinct DIDs. Capture/checkpoint and audit databases are backed up locally with integrity checks and hashes. |
| Recover eligible missing accounts safely | Of 1,774 DIDs absent at their individual audit times: 377 restored/enriched, nine present at recheck, three became inactive, and 1,385 were observed inactive. No eligible missing active candidates remain in this captured set. Every restoration used current status and canonical policy-aware writers, without supplying a relay cursor. All 377 were independently found by current-handle search after normal sync. |
| Reconcile supported inactive/profile cases | Removed 78 confirmed deleted/deactivated primary-source records with pre-write backups and verified database absence; 71 usable handles also verified absent from search and seven without usable handles have tombstone evidence. Two additional cases were already absent. Refreshed and verified 137 existing profiles, including four independently confirmed empty-field clears. Takedowns were preserved under existing fallback policy. |
| Protect serving during recovery | Small batches and readiness/freshness/P95/P99/error gates. A P99 spike paused further writes; work resumed only after the gate cleared. All recovery writers and search verifiers have now completed. No search deployment or CPU resize occurred. |

At the final live check: ingester source age approximately one second, queue
135 ingest/one delete, no backpressure, identity queue empty, and successful
write within five seconds. Search was ready with 38-second sync age and healthy
freshness. Fly reported P95 approximately 26 ms, 912 HTTP 200 responses in the
five-minute window, and no CPU throttling. This is a point-in-time health check,
not an assertion that slow-tail behavior is resolved.

## What remains uncertain

The original 90 overflow logs counted 360,000 queued entries and 114 delete
entries discarded. These are not unique accounts, nor a count of permanently
missing database rows: operations repeat, some upstream writes may partially
commit, and subsequent live updates can repair records.

The recovered relay sequence window maps to September 3, approximately
20:16–23:13 UTC; failures occurred while processing it on September 4. It is
incorrect to equate relay source time with wall-clock failure time. The saved
candidate capture identifies affected DIDs and event classes, not every
historical field value. It cannot reconstruct every overwritten value or prove
all historical event effects were recovered.

The 2,306 remaining current-profile differences are a separate data-quality
backlog, not established historical losses. The comparison covers selected
profile fields and does not establish equality of PDS data, counters, labels,
or every event-derived property. Recovery marks restored actors recently
active; that is recovery activity, not proof of a new user action.

Identity discovery is now on the protected queue, but failed best-effort
resolution can still delay handle/PDS freshness for existing accounts. The
appview can also return `handle.invalid` for a separately resolvable identity;
two recovery cases were corrected from current identity evidence. This is a
separate ongoing enrichment behavior, not an uncompleted restoration.

Long upstream outages still depend on the relay retaining replayable history.
The fix provides conservative retry/replay behavior, not unlimited retention or
an exactly-once historical accounting guarantee.

## Follow-ups and access boundaries

- The local Prefect enrichment guard is tested but not published or deployed.
  Publication in `my-prefect-server` requires Nate's explicit approval under
  his external-project rule. Keeping the bulk schedule paused is the current
  verified mitigation. Re-enable it only after the guarded workflow is live.
- Search slow-tail and sync contention, the local bounded-overlay optimization,
  request-accounting database writes, capacity, and vendor costs are tracked in
  `2026-09-05-system-performance-review.md`. They do not require expanding this
  incident into general profile cleanup.
- No commits, pushes, public issues, or PRs were created. Existing user edits
  remain in place. The production health monitor remains enabled.

Evidence is retained in `.codex/recovery/2026-09-05/` (gitignored, private file
permissions). Its manifest hashes the SQLite backups and specific correction
and tombstone evidence. The detailed chronology is in
`2026-09-05-ingester-cpu-quota.md`.
