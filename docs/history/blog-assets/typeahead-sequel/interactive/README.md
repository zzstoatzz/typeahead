# Native Leaflet figures

Current draft: https://leaflet.pub/01a07046-5ea6-766a-84ad-2e0cf4772772

The two figures are raw HTML embeds inside Leaflet; article prose stays in
native text blocks. No separate hosting is needed. The local HTML article is
an assembly preview, not the editing destination. Its prose has intentionally
not been revised to match the latest figures yet.

Current design:

- Traffic: baseline and voting-window comparison, selected by two explicit
  buttons. No hover selection, slider, autoplay, or incident controls. Animated
  bar lengths use real 15-minute aggregates from `../traffic-evidence.json`.
  Exact counts and medians remain visible; definitions and the complete data
  table are in an optional disclosure.
- Index: illustrated Alex-to-Ari display-name change. A separate correction
  updates search results before the next build. The correction then moves into
  the prepared index; results stay the same. Rename, build, and reset controls
  work with keyboard input. Reduced-motion users receive immediate state changes.

Rebuild self-contained embed pages from the repository root:

```sh
uv run --with markdown python docs/blog-assets/typeahead-sequel/interactive/build.py
bunx tsc -p docs/blog-assets/typeahead-sequel/interactive/tsconfig.json
bunx oxlint --deny-warnings docs/blog-assets/typeahead-sequel/interactive/*.ts
```

Replace the corresponding Leaflet block through Edit embed, paste the complete
HTML, and apply. Current heights: traffic 860 px; name-change example 840 px.
Source templates are `traffic.html` and `architecture.html`; the latter filename
is retained for compatibility but no longer contains the infrastructure tour.
`revision-3.md` records the design decisions. Historical measurements, static
architecture assets, and earlier five-minute aggregates remain available one
level up. These are historical data and schematic examples, not live controls.
