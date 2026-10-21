# Ranking page refresh — October 20, 2026

Production Worker: `feb97b42-2a8c-4b6e-9a04-22d53fb472e7`.
Previous version retained for rollback: `89901546-0e01-4789-b19a-541f0545460e`.
Refresh deployment state before rollback if another deployment has intervened.

The production bundle diff was confined to the reputation page. Existing
bindings, search transport, backend secret and cron configuration were preserved.
Typecheck, 93 Worker tests, dry-run build, and authenticated post-deploy smoke
passed. Mobile verification covered the empty guide and populated live report.
Preview checks also covered desktop, both themes, and expandable details.

Evidence: `~/.local/share/typeahead-experiments/ranking-page-20261020/` contains
previous Worker modules/metadata and `deploy-smoke.log`.
The isolated `typeahead-ranking-review` Worker remains available for comparing
the approved design using one captured public report; it has no backend bindings.

## Data access remains unchanged

The HTML itself requires no database read. `/debug/reputation` looks up one
actor by DID or case-insensitive handle in Turso, then reads moderation overrides,
blocked-domain rules, and exact-handle eligibility. Scalar counts and stored
quality score are SQL columns; stored labels and associated metadata are JSON
text. The score breakdown is calculated for the response, not separately stored.

Percentiles use 101 precomputed snapshot quantiles, stored as JSON in SQLite
`index_meta` and exposed by `/index/status`. They are approximate snapshot bands,
not exact current corpus ranks. There is no per-report corpus count.

Three live search requests and calls to 12 labelers run alongside percentile
and eligibility reads. Labeler requests have a three-second timeout; search
probes have eight seconds. The response waits for all of them. Labeler answers
are request-scoped and are not persisted. Collapsing a section does not defer
its requests. An actor with `profile_checked_at = 0` may trigger a conditional
claim and enrichment write on view.

A proposed next step is to load the stored report first, fetch external sections
on demand, and give cached external results explicit timestamps. No access or
storage changes were included in this release.

## Copy and icon follow-up

Removed the PDS comparison from the ranking guide and redundant moderation
explanations. Retained operational status and query-result diagnostics. Small
inline SVG icons identify ranking inputs and report sections; labels remain
visible and icons are hidden from assistive technology. No image requests or
dependencies were added. Doodl integration is a future option: ZDS resolves
drawing references into bundled PNG assets served by content hash.

Follow-up deployed as `479e77d2-8334-43d6-9089-95271f7d591a`; immediate rollback is
`feb97b42-2a8c-4b6e-9a04-22d53fb472e7`. Typecheck, all 93 tests and
authenticated smoke passed. Live mobile guide and preview report icons were
visually verified, including light and dark themes. Evidence: `icons-deploy-smoke.log`.
