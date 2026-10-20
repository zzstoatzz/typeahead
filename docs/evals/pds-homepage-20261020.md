# PDS homepage review — October 20, 2026

Local preview only. No production deployments, publisher changes, or new hosted
resources. Ported the October 8 PDS experiment onto current compact main
`369cf6a`, preserving the primary checkout's unrelated edits.

The filter uses the existing homepage colors, typography, borders and button
sizes. A 44px control sits inside the existing 48px search field. Its anchored
panel takes no layout space while closed (or open). Apply normalizes the hostname,
adds an active dot and accessible host label, saves scope in the URL and searches.
All servers clears scope. Escape cancels edits; reload and history restore scope.
The active button tries the selected host’s HTTPS `/favicon.svg`, then
`/favicon.ico`, with no referrer, and retains the sliders if both fail. The icon
is decorative, 18px, with a subtle active tint; the control remains 44px. No
backend proxy, third-party icon service or search-path work is added. Removed
images cannot replace the icon for a newly selected scope.
The browser and Worker share the hostname normalizer. Superseded searches are
aborted and guarded by a generation counter. Unavailable scopes and empty results
are separate states; invalid scope URLs cannot trigger global searches.

## Verification

- TypeScript check, targeted Oxlint, 98 Bun tests and Zig test targets passed.
  ReleaseFast service and loopback evaluation-server builds passed.
- Real FLATPX05 conversion retained scope metadata and registry. The candidate,
  PDS-only reference and current-main engine ran against compact files built from
  the same 100,005-actor synthetic source. 1,712 response comparisons passed.
- 18 real workerd/Miniflare checks passed: normalized scope caches, global/scope
  isolation, both aliases, excluded/invalid/unknown scopes, unsupported and dead
  engines, and emergency proxy mode. Scoped requests failed closed.
- Real admin refresh exercised membership migration, hiding, renaming and exclusion;
  removing the policy-ready marker disabled scoped search while global stayed up.
  Zig tests cover deletion and policy hashes. This does not test full remote replay.
- Local unscoped p50/p95: current main 0.211/0.537 ms, candidate 0.211/0.522 ms.
  Scoped p50/p95: 0.231/0.575 ms. Single warm fixture run, not a production or
  statistical no-regression claim. Full compact artifacts: 37,863,424 bytes versus
  35,098,624 bytes with scopes disabled (+7.9%).
- Browser checked desktop 1280px, mobile 390px and narrow 320px; light/dark, apply,
  clear, Enter, Escape, invalid input, URL reload/history, populated, empty and
  unavailable states. At 390px the field stayed y=231.336, h=48; the following
  form stayed y=338.531, h=48 across panel open/close. No horizontal overflow;
  browser console read returned no warnings/errors. 200% zoom and deterministic
  delayed-response race coverage remain for final acceptance.
- Read-only production health: compact ready, expected build b1792400507-4df8-px05,
  lag 263 seconds at this check. The public freshness route returned 403 without
  authentication. No claim of live PDS verification: it is not deployed.

[Machine-readable fixture evidence](pds-compact-results-20261020.json).
Local artifacts and runnable preview are under
`~/.local/share/typeahead-experiments/pds-homepage-20261020`.

## Remaining production gates

Build a full-corpus PDS compact pair on an isolated channel; measure build disk/RSS,
size, conversion and replay. Exercise policy-change replay/restart and promotion,
measure unscoped tail latency against current compact on identical data, then
validate rollback/recovery. Keep the initial 10,000-row host cutoff. Do not change
production pointer, publisher pin or sole serving machine for this review.

After approval and passing qualification, admit a warmed compatible backend,
verify Worker scoped responses and aliases, then expose this UI. Preserve the
then-current Worker source. Hide/disable scoped requests explicitly before an
incompatible-backend rollback; retain compatible snapshot/overlay recovery pairs.

## Authorized rollout preparation — October 20

Nate approved the UI and authorized rollout. Candidate `b867870` passed Tangled
pipeline `3myc77ioj6522`. The scope-only disable switch passed 22 Worker runtime
checks, including disabling an already cached scoped answer while global search
remained available, then re-enabling it.

Live Worker version `6a814dee-4ffe-4b26-aa38-dc39e92f3c6c` was downloaded for source
comparison. Its `statsPage` function exactly matches the primary checkout's
uncommitted stats implementation, not repository main. That source and its tests
are carried into this release to preserve deployed behavior; the primary checkout
is untouched. This is deployment reconciliation, not a new stats redesign.

Full-corpus export/build runs in the isolated HeavyPad directory
`/home/stoat/typeahead-pds-20261020`, user units
`typeahead-pds-compile-20261020` and `typeahead-pds-full-build-20261020`.
The build uses `INDEX_CHANNEL=local` and cannot publish a production pointer.
Qualification and deployment are still pending.
