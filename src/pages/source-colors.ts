/* =========================================================================
 * HEURISTIC ZONE: guesses made from app icons we do not control.
 *
 * Everything here is a best-effort guess with tuned constants, kept out of
 * the stats page logic on purpose. It defines browser functions:
 *   deriveSourceColor(img) -> '#rrggbb' accent, or null
 *   knockoutIcon(img)      -> data URL of the mark with a flat square
 *                             background removed and cropped, or null
 *   iconLuminance(img)     -> mean luminance 0..1 of the visible pixels
 * Every caller falls back to the plain icon or the fixed palette, so a bad
 * guess can only look less polished, never break the chart.
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
    function knockoutIcon(img) {
      const N = 96, TOLERANCE = 60;
      try {
        const canvas = document.createElement('canvas');
        canvas.width = N; canvas.height = N;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, N, N);
        const data = ctx.getImageData(0, 0, N, N), px = data.data;
        const at = (i, j) => (j * N + i) * 4;
        const corners = [at(1, 1), at(N - 2, 1), at(1, N - 2), at(N - 2, N - 2)];
        if (!corners.every(o => px[o + 3] > 200)) return null;
        const r0 = px[corners[0]], g0 = px[corners[0] + 1], b0 = px[corners[0] + 2];
        const flat = o => Math.abs(px[o] - r0) + Math.abs(px[o + 1] - g0) + Math.abs(px[o + 2] - b0) < TOLERANCE;
        // only a uniform plate counts; a photo or full-bleed artwork is left whole
        if (!corners.every(flat)) return null;
        const seen = new Uint8Array(N * N), stack = [];
        for (let i = 0; i < N; i++) stack.push(i, 0, i, N - 1, 0, i, N - 1, i);
        while (stack.length) {
          const j = stack.pop(), i = stack.pop();
          if (i < 0 || j < 0 || i >= N || j >= N) continue;
          const k = j * N + i;
          if (seen[k]) continue;
          seen[k] = 1;
          const o = k * 4;
          if (px[o + 3] > 0 && !flat(o)) continue;
          px[o + 3] = 0;
          stack.push(i + 1, j, i - 1, j, i, j + 1, i, j - 1);
        }
        let x0 = N, y0 = N, x1 = -1, y1 = -1;
        for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) if (px[at(i, j) + 3] > 40) {
          x0 = Math.min(x0, i); y0 = Math.min(y0, j); x1 = Math.max(x1, i); y1 = Math.max(y1, j);
        }
        // nothing, or almost nothing, left: the plate was the logo
        if (x1 < 0 || (x1 - x0) * (y1 - y0) < N * N * 0.04) return null;
        ctx.putImageData(data, 0, 0);
        const w = x1 - x0 + 1, h = y1 - y0 + 1, side = Math.max(w, h);
        const out = document.createElement('canvas');
        out.width = side; out.height = side;
        out.getContext('2d').drawImage(canvas, x0, y0, w, h, (side - w) / 2, (side - h) / 2, w, h);
        return out.toDataURL();
      } catch (_) {
        return null;
      }
    }
    function iconLuminance(img) {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = 32; canvas.height = 32;
        const ctx = canvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, 32, 32);
        const px = ctx.getImageData(0, 0, 32, 32).data;
        let sum = 0, n = 0;
        for (let i = 0; i < px.length; i += 4) {
          if (px[i + 3] < 128) continue;
          sum += (0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]) / 255; n++;
        }
        return n ? sum / n : 0.5;
      } catch (_) {
        return 0.5;
      }
    }
`;
