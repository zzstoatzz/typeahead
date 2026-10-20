# PDS filtering rollout plan

Updated 2026-10-20; not shipped. Compact snapshots are live, PDS filtering is not.

Nate approved the preview and authorized production rollout on October 20. See
[verification evidence](../evals/pds-homepage-20261020.md). The show-before-shipping checkpoint is complete. Full-corpus qualification
remains required before routing production to this candidate.

## Homepage interaction

Keep the existing search field and page geometry. Put a 44px filter button inside
the field's trailing edge, with reserved input padding. The closed control adds
no row or page height. Use the site's existing color, border, typography and
focus styles. Inspect the implementation at mobile widths before shipping.

Open an anchored popover containing a labeled “Hosting server (PDS)” hostname
input, short help text, Apply and Clear. Default is All servers. Applying a scope
closes the panel, marks the button active with a small badge, and announces the
selected hostname through its accessible label and tooltip. Keep the complete
hostname visible in the panel; do not squeeze it into a new permanent chip row.
Enter applies; Escape and outside click dismiss without applying edits; return
focus to the trigger. Support keyboard traversal and a viewport-contained panel
at 320px, 375px and desktop widths, light and dark themes, and 200% zoom.

References inspected: plyr.fm frontend/src/lib/components/HiddenTagsFilter.svelte
(collapsed/expanded and active states) and LinksMenu.svelte (anchored panel,
outside dismissal and Escape), plus the live Typeahead homepage. Reuse their
interaction pattern, not their framework or accessibility suppressions.

Persist applied q/pds in the URL so reload, sharing and back/forward preserve
scope. Abort superseded searches and reject stale responses when q or pds changes.
Clear immediately reruns the current query globally. Validate and normalize with
the same server contract. Render unavailable scope separately from empty results;
never silently show global results for a scoped request. Explain that hosting
server is not a handle suffix. Do not invent a host directory or suggestions API.

## Shipping sequence and acceptance

1. Compact telemetry and publisher/rollback qualification completed during the
   October 20 rollout. Preserve that recovery baseline and verify fresh health
   and spans before any PDS transition. See compact-production-cutover.md.
2. Integrate the local PDS experiment onto the shipped compact implementation in
   isolation, preserving other sessions' edits. Complete the existing compact
   publisher/rollback gates before replacing its production source pin. Verify
   scoped keys work through compact conversion, reader, overlay and pruning.
3. Build a full-corpus PDS-capable compact snapshot on an isolated R2 channel.
   Measure size, scratch disk, RSS, conversion time and replay duration. Keep the
   initial 10,000-actor hosting policy unless measured evidence supports changing
   it. Require pds_scope_version=1 and replay-policy readiness before scoped use.
4. Compare unscoped bodies against the current compact implementation on identical
   data. Compare scoped bodies against a reference built only from each host's
   members. Exercise custom handles, PDS migrations, moderation, deletion, excluded
   and unknown hosts, failed replay/restart and policy changes. Benchmark common,
   rare, multi-token and cold queries on the isolated full corpus. Require no
   statistically supported regression in unscoped p50/p95/p99 or correctness.
5. Admit a fully warmed compatible backend with current overlay, verified disaster
   artifacts and a ready old route. Then deploy Worker scope support with the UI
   still hidden. Require X-Typeahead-Pds acknowledgment, normalized scoped cache
   keys, both public aliases byte-identical, and fail-closed behavior on unsupported
   snapshots, backend failure and fallback modes. Verify live scoped smoke queries.
6. Ship the compact homepage control after API acceptance. Test real browser
   requests for apply/clear, URL history, stale-response races, keyboard use,
   empty/unavailable states and mobile layout. Publish the lexicon and update
   README, /docs, /llms.txt and the homepage API example together with shipped status.

Rollout controls: keep the source configuration at `PDS_UI_ENABLED=0` until
live API acceptance, so unrelated Worker deployments cannot expose the UI early.
Use `--var PDS_UI_ENABLED:0` for the initial API-only deployment as an explicit
guard; change the source default to `1` only after acceptance. `PDS_SEARCH_DISABLED=1` hides the control and rejects
scoped requests before cache lookup, including already cached answers.

Rollback: set `PDS_SEARCH_DISABLED=1` to hide the UI and disable scoped requests with an explicit unavailable
response before routing to an incompatible backend. Invalidate/version scoped
cache entries; account for the existing 60-second TTL. Preserve a compatible
snapshot/overlay pair and do not reuse a pruned overlay against an older snapshot.
Global search must remain available throughout. Observe traffic and replay before
deleting any serving or recovery resources.

## Performance priorities

The 2026-10-17 live paired check favors compact, but edge cache misses remain
slower than Bluesky in the quick benchmark. Restore end-to-end spans first, then
attribute miss time among Worker work, backend transport, engine and hydration.
Optimize the measured dominant phase and repeat the same query set; do not trade
scope correctness or global ranking for a faster number.

## Production transition prepared October 20, 2026

Source is merged on main (`8c24887`) with immutable builder tag
`pds-compact-31ec142-20261020`. This is source preparation, not a live PDS release.
The qualified native image is
`registry.fly.io/typeahead-search-compact:pds-31ec142-20261020@sha256:7df51c4b6ba2b26dcfc1aeb15b7821c7ec0caf6594cb511fc93ddbe946101938`.

Private candidate `8d4916be311008` and volume `vol_4qlwyx1kl6o19x6r` follow
`staging/pds-candidate-31ec142-20261020/latest.json`, using
`/data/pds-current-20261020` and `/data/pds-overlay/overlay.db`. No public services
are assigned. Full pair upload/readback and native admission are still pending.
Expected generation: `b1792484789-3d65-px05`, watermark `1792484192`.

The existing root manifest was copied immutably and read back at
`releases/pds-rollback-20261020/b1792400507-4df8-px05/latest.json`.
Existing compact machine `781e732c264678` has only 8.4 GB free; it must be pinned
to that protected pointer before publishing the new 23.3 GB recovery pair at
root. Its old image, volume, snapshot roots and live overlay remain recovery
resources. Retain the earlier recovery pair and archives through the existing
October 24/26 retention gates in the compact cutover runbook.

After candidate admission, fresh lag <=300 seconds, native benchmarks and Logfire:

1. Refresh current Worker versions/source and both backend health checks. Clone
   the then-current Worker with only its backend changed to healthy legacy.
   Confirm public smoke and allow in-flight requests to drain.
2. Save fresh old compact machine config; pin it to the protected old pointer and
   remove public services. Verify it remains healthy privately. Keep legacy serving.
3. Register the merged publisher pin and explicit 10,000-actor cutoff, preserving
   its cron `0 9 */3 * *` and all unrelated deployments. Publish root only from the
   fully read-back qualified manifest. Do not call a future scheduled run complete.
4. Refresh the candidate config to follow root `latest.json`, retaining its
   existing snapshot and overlay paths. Apply with legacy coverage, then require
   exact generation, admission, scope readiness and current catch-up again.
5. Assign the saved public service configuration to the new machine; verify it
   is the sole public compact machine. Clone the latest Worker back to compact.
   Verify global health and authenticated smoke before exposing PDS API/UI.
6. Deploy reconciled Worker source with `--var PDS_UI_ENABLED:0`; verify real
   scoped queries, normalized cache separation, aliases and explicit unavailable
   states. Enable the homepage only after that acceptance, then check live browser
   behavior, public schema, telemetry and smoke. Preserve newer unrelated source.

Before any rollback to an incompatible backend, deploy `PDS_SEARCH_DISABLED=1`
and verify even cached scopes return unavailable. Route through healthy legacy
while restoring the previous compact configuration. Do not reuse a newer pruned
snapshot/overlay pair against the old snapshot.
