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

Rollout controls: keep `PDS_UI_ENABLED` unset until live API acceptance; set it
to `1` to show the filter. `PDS_SEARCH_DISABLED=1` hides the control and rejects
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
