# blog assets — "the pendulum backswing"

Source + rendered images embedded in the pckt blog post
(`waow-tech.pckt.blog/the-pendulum-backswing-tzgmqge`).

## rendering pipeline
- `render.mjs` — MathJax (TeX → SVG, glyphs as vector paths) → @resvg/resvg-js
  → PNG @3x on a white card. Produces `eq-*.png`.
- `raster.mjs` — rasterizes any SVG → PNG @2x with system fonts (used for the
  hand-authored diagrams + `../architecture-diagram.svg`).
- deps: `bun add mathjax-full @resvg/resvg-js` (run from a scratch dir).

## sources
- `../architecture-diagram.svg` — the system diagram (lives in docs/).
- `cost-bars.svg` — d: index cost decoupling (+69% build vs +0.9% serve).
- `dns-tree.svg` — DNS subtree-harvest intuition for the d: index.
- equation TeX is inline in `render.mjs`.

## rendered (embedded)
- `eq-keys.png`, `eq-emissions.png`, `eq-artifact.png` — display equations.
- `cost-bars.png`, `dns-tree.png` — conceptual visuals.
- diagram PNG is rasterized on demand from the SVG.

## note on embedding
pckt renders images from its own S3 media library; a bare PDS blob won't
render. The live post sets each image block's `attrs.src` to the public
`com.atproto.sync.getBlob` URL, which pckt's importer preserves.
