# Parallel Tantivy development backend

Owner:tantivy-dev-20261028. User explicitly authorizes a parallel dev service and
strong smoke testing; production cutover is outside scope.

Create one independent Fly app,typeahead-tantivy-dev,with its own volume and
https://typeahead-tantivy-dev.fly.dev endpoint. Start with a tiny synthetic
integration pilot through the real normalizer,export/import,HTTP,admin replay,
restart and hydration path. Only a passing pilot may start the full cloud-local
source import. Reuse the published serving generation via immutable R2 reads.
Never fork or copy a serving/rollback disk; never run corpus work on HeavyPad.

Initial storage budget:40GiB volume,/data scratch,32GiB maximum owned payload and
4GiB free reserve. Stream exporter to importer instead of storing a corpus JSONL
copy. Temporary source artifacts are retired after verified full import. Keep
only the admitted dev index,journal/commit metadata and bounded qualification
receipts. The long-lived dev server is intentionally retained for user testing;
failed build/pilot machines must stop rather than retry forever.

Bootstrap deadline:90minutes hard process-group bound. Resource shape may use
2 performance CPUs/4GiB while importing,then explicitly qualify the final dev
serving shape separately. Full source identity,count,stream hash,normalizer and
planner versions must be verified before readiness. Report partial or stale
state explicitly; no synthetic/sample result can masquerade as full corpus.

Smoke scope:canonical/compatibility alias behavior,rich profile hydration,
exact-first and stable score ties,multiword/reversed names,long tokens,CJK/mixed
scripts,moderation/deletion/rename/PDS migration,hard filtering and optional PDS
preference,scope acknowledgement,limits/invalid input,stale/replay/conflicts,
durable restart and atomic import failure. Then varied first-touch/warm/mixed
search and read-only source replay; include failures,admission and client queue
in results. Load generator resources stay outside serving limits. No production
load test or upstream database write is permitted.

The current source uses second-resolutionupdated_at; base compact records lack
per-actor timestamps. Preserve the snapshot watermark floor and make equal-time
conflicts visible rather than silently choosing an order. Source-version and
delete retention limitations remain cutover blockers if not resolved.

Before calling this complete:dev URL must serve an honestly identified dataset,
replay/freshness must be observed or explicitly disabled/stale,all smoke failures
must be retained,and protected production/rollback/ingester identities/routes
must be rechecked. Update README and the dev operations record with actual state.
