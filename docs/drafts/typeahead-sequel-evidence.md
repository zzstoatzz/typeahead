# Sequel evidence and operational handoff

Checked 2026-09-05, approximately 05:30–05:45 UTC. No production configuration,
credentials, deployments, or public posts changed during this investigation.

## Story and source map

| Claim | Evidence | Qualification |
| --- | --- | --- |
| Sequel to the May rebuild | [Original Pckt post](https://pckt.blog/b/waow-tech/typeahead-more-like-typebehind-amirite-tzgmqge), read in browser | May 26, 2026; the final sync-layer breadcrumb provides the narrative connection |
| Claude's architecture | [Artifact](https://claude.ai/code/artifact/241a0273-3021-438d-a785-e8a24e97c611), accessible in browser | Diagram, not additional conversation history |
| Original adoption burst | September 4 incident report; [bsky38](https://bsky38.com); race38 README links [announcement](https://bsky.app/profile/bsky38.com/post/3muphbj35cs2h) | Distinguish organizer's voting site from Nate's separate race38 tracker |
| Tracker uses typeahead | Sibling `race38/web/ballot.ts`, canonical endpoint and `X-Client: race38` | Tracker frontend, not the organizer's implementation |
| 33× engine volume | [Aggregate Logfire response](../blog-assets/typeahead-sequel/traffic-evidence.json) | 5,423 / 163 = 33.27; complete 15-min windows; roughly 6.03 searches/s averaged over the busiest shown window |
| Latency improved | Same query: p50 4.02 → 2.96 ms, p95 17.99 → 8.91 ms | Engine span duration, not end-to-end; cache-warming explanation is inference |
| Earlier numbers differ | `docs/cost-scalability.md` originally captured 3,459 at 18:00; final window query yields 4,076 | Use complete windows; retain original as dated research, do not silently treat its intermediate count as final |
| Replica removal | Commit `c9f5196`; `docs/replica-removal-plan.md` | Cutover September 2 06:24 UTC; fresh-volume attach ~20 min plus 47 min replay of 19h |
| Fallback incidents | `docs/retros/2026-09-03-ingester-panic-hang-and-turso-saturation.md`; commits `5272752`, `22a652e` | Worker error totals are incident-report evidence, not re-queried Cloudflare historical logs |
| Current fallback policy | `src/handlers/search.ts`; Worker deployments at 19:25 and 19:46 UTC on September 4 | Defaults verified in code; secret values deliberately not disclosed |
| Home builds | Prefect API deployment and completed run; R2 `latest.json` | September 4 09:00:14–10:07:40 UTC; cron `0 9 */3 * *`, not an exact rolling 72h interval |
| Current corpus | R2 manifest agrees with live `/index/status` | Build b1788512492-0ed0, 11,631,204 actors, 127,296,942 prefix entries, 14,027,964,416 bytes |
| Current health | Public freshness probe plus advancing Fly ingest logs | Freshness ok, overlay lag 140s at first probe, snapshot age ~20.5h; ingest pending=0 and cursor advancing |

## Access established by a real read

- **Fly:** machine and volume inventory; live ingester logs. Search is 4 shared
  CPUs / 2 GB / 60 GB in ewr; ingest 1 shared CPU / 256 MB / no volume.
- **Cloudflare:** Wrangler OAuth account verified; deployments and R2 bucket
  listing succeeded. Active Worker version observed:
  `6e09a605-6fa0-4006-9691-974f735bc7fd` (September 5 04:33 UTC).
- **R2:** authenticated download of `typeahead-index/latest.json` succeeded.
  The key is at bucket root, not `channels/production/latest.json`.
- **Turso SQL:** local `.env` database token executed `SELECT 1` successfully
  through the Hrana HTTP pipeline. The Turso account-management CLI is signed
  out. SQL access and organization/billing administration are distinct.
- **Logfire:** local `LOGFIRE_READ_TOKEN` queried
  `https://logfire-us.pydantic.dev/v2/query` successfully. Project:
  `https://logfire-us.pydantic.dev/waow/typeahead`. Never print either read or
  write tokens. Later search spans are sampled 1 in 10; do not compare their raw
  count with these pre-sampling windows.
- **Prefect:** the self-hosted API authenticated with `my-prefect-server/.env`,
  using `DOMAIN` and `AUTH_STRING`. `home-pool` was READY. Identity runs completed
  at 04:22 and 05:24 UTC September 5; enrichment completed September 4 23:30;
  PLC completed August 31 05:28; index completed September 4 10:07.
  The hosted Prefect Cloud connector is not this server.
- **Tangled:** `tg auth status` reports an active zzstoatzz.io session. No write
  permissions tested by publishing anything.

## Heavypad result and remaining access

- **Heavypad:** access completed. `ssh -o BatchMode=yes stoat@heavypad`
  succeeded after the normal Tailscale browser check. Host `heavypad`, user
  `stoat`, `prefect-home-worker` active, local port 8080 health OK. Build root
  exists; Zig and rclone are under `/home/stoat/.local/bin`. Host disk has 932 GB
  available (47% used). Noninteractive SSH needs that bin directory on PATH.
  Default `ssh heavypad` still selects the laptop username `nate`; use explicit
  `stoat@heavypad`. No ACL, SSH configuration, or credentials changed.
- **Pckt:** original article accessible; browser is signed out. User sign-in to
  waow.tech requested. Manuscript and images are local, not yet saved in Pckt.
  Publishing is a separate final action after reviewing the draft.
- **Turso management:** `turso auth login` is needed for org-level usage/billing
  commands. Existing database-token access is working and sufficient for SQL
  investigation; no credentials were created or rotated.

## Tooling caveat

`just prefect deployment ls` authenticated but crashed while joining a returned
deployment to its flow (`KeyError` for a flow UUID). Direct filtered API reads
worked. Query deployments with a sufficiently large limit (the first 30 omitted
the index deployment), and exclude SCHEDULED when asking for recent executed
runs. This is a local CLI/API compatibility observation, not a worker failure.

## Files prepared

- `typeahead-sequel.md`: proposed Pckt text, with image references.
- `../blog-assets/typeahead-sequel/architecture.{svg,png}`: new diagram.
- `../blog-assets/typeahead-sequel/traffic.{svg,png}`: measured traffic figure.
- `../architecture.md`: updated system reference; README points to new image.

For Pckt, use uploaded image URLs in the final article. Relative local paths are
preview references. The prior post's image import details are in
`../blog-assets/README.md`.

## Editorial and interactive revision

Voice references: [Semble](https://nate.leaflet.pub/3mnxnia4lvk2n),
[within my ken](https://nate.leaflet.pub/3mj5mknhufc23), and
[home hosting costs](https://nate.leaflet.pub/3mrxzyhlsc22r). The revision favors
concrete examples, linked demonstrations, and short qualifications over suspense
headings and general conclusions. The source text is editable Markdown.

`traffic-5m.json` contains 43 complete five-minute windows, September 4
16:30–20:05 UTC, with 41,244 completed search spans and 7,504 empty results.
The window at 18:30 contains 1,946 searches; the 19:40 window has p95 36.4 ms.
The response preserves SQL, time bounds, and retrieval timestamp. All buckets
precede the 20:08 deployment enabling one-in-ten sampling. Empty engine results
are not equivalent to errors. Engine span durations exclude browser transit
and engine checkout; a five-minute percentile cannot isolate the restart.

`query-evidence.json` preserves public engine debug responses for three queries.
Their keys, candidate counts, and account results drive the search inspector.
Cache states, animation durations, the Alex→Ari rename, and build progression are
illustrations. The snapshot numbers are observed manifest values.

Browser checks: desktop (1280 px) and phone (360 px), no horizontal page overflow;
query selection, step-slider keyboard navigation, rename suppression, build
playback, cache-hit replay, incident jumps, traffic replay, and 43 data rows.
No browser errors or warnings observed. Standalone TypeScript and Oxlint checks
pass. Publication and authenticated Pckt embed integration remain for after review.

## Native editor check

Created and reload-verified an unpublished native Pckt draft under waow.tech:
https://pckt.blog/dash/waow-tech/edit/untitled-40v6a1t

The prose is stored as editable headings, paragraphs, links, emphasis, and inline
code; two explicit figure placeholders remain. Pckt's block menu offers no raw
HTML block. Pasting the existing race38 website URL and choosing Embed created
a link-preview card, not an iframe. Rich HTML paste stripped a normal iframe.
Test content was removed and the clean draft reloaded successfully. These tests
rule out those workflows, not every potential provider-specific integration.

Leaflet's official post https://lab.leaflet.pub/3m3q3meshhc2g documents iframe/embed
blocks. Opened Leaflet login for the user's next handoff; no Leaflet draft or
publication has been created yet. No post was published on either platform.

## Leaflet native draft — verified

https://leaflet.pub/01a07046-5ea6-766a-84ad-2e0cf4772772

Signed in as waow.tech. Leaflet offers both Embed Website and Embed HTML.
Embed HTML preserves JavaScript in a sandboxed srcdoc iframe: a test button
changed its label, then the actual traffic widget responded to the restart
marker (19:40–19:45). The architecture widget's profile-update sequence showed
n:alex suppression and n:ari addition. Both embeds and 21 native text blocks
survived a reload. No figure placeholders remain. No publication/share action
was invoked. Raw HTML embeds mean no separate widget hosting is required for
this draft. Publication rendering remains untested until publication review.

Embed heights are 1020 px for traffic and 1000 px for architecture. The diagram
is shown at Leaflet's ~588 px column width using an additional 421–620 px CSS
rule; narrower phones retain the compact flow sequence. Rich text is native,
not part of either HTML embed. Pckt's earlier draft is retained as a fallback.

## Reader-facing figure revision

Replaced the two Leaflet embeds in place, leaving all 21 native prose blocks
unchanged (compared their HTML before and after). Traffic now compares the
16:45 baseline with either 17:30 or 18:30, using actual 15-minute aggregates;
removed the hover/scrubber conflict, playback, and incident controls. The second
figure is a worked Alex→Ari correction example followed by consolidation into
the next index. Removed the infrastructure tour and traveling packet animation.

Checked local figures at 588 px and 360 px, keyboard activation, reset during
consolidation, real selected values (1,705 and median 3.6 ms), and stable results
after consolidation. TypeScript and lint pass. Replaced embeds through Leaflet's
HTML editor and verified persistence and interactions after reload. Native
heights now 860/840 px. Article references to old controls deliberately remain
for the later prose pass requested by Nate. No publication action taken.


## Approved launch replay embedded

Replaced the first Leaflet HTML embed with `docs/drafts/launch-replay/embed.html`. Removed the portrait selection prompt; details appear on hover/focus. Native frame height is 650 px. Verified running counter advances in the sandboxed iframe, end count is 24,455, and source plus height persist after reload. Native article copy and second architecture embed remain unchanged for the next editorial pass. No publication action.


## Native measurement footnote

Inspected hyperlink-academy/leaflet source: EmbedBlock.tsx creates an ordinary text block for captions; TextBlock/inputRules.ts supports native footnote insertion with `[^`; foldBlocks.ts supports nested block folding. Removed the replay About the data disclosure. Replaced the immediately following measurement paragraph with a one-line caption and native footnote, verified note content after reload. Footnote opens in Leaflet, outside the iframe. Frame resized to 638.5 px to contain the static graphic. No publication.


## Editorial reset: September 5

Removed the rejected name-change embed from the native Leaflet draft, along with obsolete demo instructions, the Alex/Ari example, and detailed plumbing prose. Condensed the body to factual paragraphs on corpus growth, snapshot/overlay and home builds, Turso fallback saturation with dropped ingest events, and volume reduction with the correction to the May cost estimate. Preserved Nate’s opening edits and personal notes verbatim. Approved launch replay, native caption, and native data footnote retained. Local markdown mirrors the native copy; older standalone HTML remains a historical prototype, not the current article. Draft remains unpublished.

## Twelve-hour replay: September 5

Exported September 4 17:00 through September 5 05:00 UTC: 51,003 recorded search spans, 128,943 sample-weighted searches. All 144 five-minute bucket counts and zero-match counts verified independently. First sampled span at 20:08:57 UTC; later counts are estimates, explicitly marked ≈. Five-minute p95 has spikes (419 ms at 22:35, 353 ms at 04:10), then returns to about 10–13 ms in the closing buckets; do not claim uniformly flat latency. Added selectable 12h/4h/1h windows, window navigation, aligned latency and volume plots, and vote snapshots rebuilt from public history with ties ordered by first vote. Removed title. Native Leaflet embed, data footnote, intro sentence, and Prefect server repository link updated. Embed height 723.5px. Browser checks covered narrower window, previous window, start/end scrub, replay, Unicode and mobile layout. Typecheck and Oxlint passed. Public trace candidates prepared; browser sign-in needed to create public links. Nothing published.

## Adaptive window controls

Replaced detached previous/next arrows and centered duration buttons with a compact View select beside the date. A time-range select appears only for 4h/1h zoom and names the actual intervals; all hourly starts remain accessible. Controls wrap below the service label on narrow embeds. Updated native Leaflet embed; checked full and four-hour views, switching to the first interval, and desktop/narrow screenshots. TypeScript and Oxlint pass.

## Labels, legend, and source links

Replaced “95% within” with P95 in readout, chart, and tooltips. Readout and chart label link to Cloudprober’s Percentiles, Histograms, and Distributions explanation. Moved returned/zero-match legend immediately beneath the chart, above portraits. Native footnote now links Logfire project (sign-in clearly noted), public race38 history endpoint, and committed sampling implementation. Kept all interpretation caveats. Leaflet footnote only accepts one paragraph of HTML paste; verified complete single-paragraph text and all three links after saving. Typecheck/Oxlint pass; browser checked embed fit and legend position.

## Public Logfire traces created

After Nate signed in, created non-expiring public links for the reviewed race-search, late-search, and slow-search candidates. Verified each with a credential-free HTTP request: status 200 and response contains the expected trace and span IDs. Stored URLs in public-trace-links.json. Replaced the private Logfire project link in the native footnote with three clearly labeled public examples; verified saved hrefs and absence of the private link. Article remains unpublished.
