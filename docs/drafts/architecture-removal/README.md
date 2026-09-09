# Replica-removal preview

Scope: the September 2, 2026 removal, not the September 4 Worker fallback changes.

Evidence: commit `c9f5196f1ca4985db69dab931deb4882abc9b63a`, `docs/replica-removal-plan.md` phase 3, and `docs/architecture.md`.

The old search box already had a prepared snapshot and overlay. The commit removed the full actor replica, bootstrap and full-resync paths, and local FTS implementation. FTS was already disabled; the label therefore says “Local FTS code.” The cutover moved from a 100 GB to a 60 GB volume. CPU and RAM stayed at four shared CPUs and 2 GB. Turso remains the durable actor store.

Native Leaflet draft was read before implementation and left untouched, including the user's new title, intro, replay heading, and architecture paragraph. This is a separate review preview, not embedded or published.

Two radio controls drive CSS transitions. Keyboard navigation and reduced motion are supported. No fabricated request traces, timers, or telemetry.
