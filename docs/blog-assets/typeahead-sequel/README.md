# Typeahead sequel assets

Prepared September 5, 2026 for `../../drafts/typeahead-sequel.md`.

- `architecture.svg` is the editable vector diagram; `architecture.png` is its
  2880×2160 raster export. Generate SVG with `python3 render-architecture.py`.
- `traffic-evidence.json` contains the aggregate Logfire query and response.
  It excludes credentials and individual user queries.
- `traffic.svg` and `traffic.png` show complete 15-minute windows before
  search-span sampling began. They measure engine duration, not edge latency.

Palette: paper #f7f9fc, ink #152940, muted #52647a; request blue #245cba,
continuous-update teal #08766c, batch purple #7450a2. The diagram puts serving
first, with update/build inputs beneath it; hosting is attached to components
rather than forcing long cross-provider edges. Arrows indicate data flow.

Run `uv run --with matplotlib python render-traffic.py` to reproduce the chart
from its saved evidence.

PNG rendering uses @resvg/resvg-js with system fonts, width 2880. The existing
`../raster.mjs` documents that renderer. Install render dependencies in a scratch
directory; they are not application dependencies. Traffic uses matplotlib.

Pckt needs image uploads or importer-supported public URLs before publishing;
relative paths in the Markdown are for local review.
