/* =========================================================================
 * HEURISTIC ZONE: slice colours guessed from app icons.
 *
 * Everything here is a best-effort guess with tuned constants, kept out of
 * the stats page logic on purpose. It defines one browser function,
 * `deriveSourceColor(img)`, which returns a '#rrggbb' accent or null. The
 * page falls back to its fixed palette on null, so a bad guess can only
 * produce a less fitting colour, never a broken chart.
 *
 * The image must be same-origin (served by /source-icon), or the canvas is
 * tainted and getImageData throws; that also returns null.
 * ========================================================================= */

export const SOURCE_COLOR_SCRIPT = `
    function deriveSourceColor(img) {
      const SIZE = 32, BINS = 24;
      try {
        const canvas = document.createElement('canvas');
        canvas.width = SIZE; canvas.height = SIZE;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, SIZE, SIZE);
        const px = ctx.getImageData(0, 0, SIZE, SIZE).data;
        const bins = Array.from({ length: BINS }, () => ({ weight: 0, r: 0, g: 0, b: 0 }));
        for (let i = 0; i < px.length; i += 4) {
          if (px[i + 3] < 200) continue;
          const r = px[i] / 255, g = px[i + 1] / 255, b = px[i + 2] / 255;
          const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
          const s = max === min ? 0 : (max - min) / (1 - Math.abs(2 * l - 1));
          // greys, near-black and near-white say nothing about the brand
          if (s < 0.25 || l < 0.12 || l > 0.92) continue;
          let h = max === r ? ((g - b) / (max - min)) % 6 : max === g ? (b - r) / (max - min) + 2 : (r - g) / (max - min) + 4;
          h = (h * 60 + 360) % 360;
          const bin = bins[Math.floor(h / (360 / BINS)) % BINS];
          const w = s * (1 - Math.abs(2 * l - 1));
          bin.weight += w; bin.r += r * w; bin.g += g * w; bin.b += b * w;
        }
        const best = bins.reduce((a, c) => (c.weight > a.weight ? c : a));
        // a few stray coloured pixels in a monochrome logo are not a brand colour
        if (best.weight < SIZE * SIZE * 0.04) return null;
        let r = best.r / best.weight, g = best.g / best.weight, b = best.b / best.weight;
        // pull lightness into a mid band so the slice reads on light and dark panels
        const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
        const target = Math.min(0.62, Math.max(0.45, l));
        const k = l === 0 ? 1 : target / l;
        r = Math.min(1, r * k); g = Math.min(1, g * k); b = Math.min(1, b * k);
        const hex = v => Math.round(v * 255).toString(16).padStart(2, '0');
        return '#' + hex(r) + hex(g) + hex(b);
      } catch (_) {
        return null;
      }
    }
`;
