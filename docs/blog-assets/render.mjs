import { mathjax } from 'mathjax-full/js/mathjax.js';
import { TeX } from 'mathjax-full/js/input/tex.js';
import { SVG } from 'mathjax-full/js/output/svg.js';
import { liteAdaptor } from 'mathjax-full/js/adaptors/liteAdaptor.js';
import { RegisterHTMLHandler } from 'mathjax-full/js/handlers/html.js';
import { AllPackages } from 'mathjax-full/js/input/tex/AllPackages.js';
import { Resvg } from '@resvg/resvg-js';
import { writeFileSync } from 'node:fs';

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const tex = new TeX({ packages: AllPackages });
const svgjax = new SVG({ fontCache: 'none' });
const doc = mathjax.document('', { InputJax: tex, OutputJax: svgjax });

const INK = '#1a1a1a';
const CARD = '#ffffff';
const PX_PER_EX = 9;     // base size
const SCALE = 3;         // retina crispness
const PAD = 22;          // padding inside the card (css px, pre-scale)
const RADIUS = 14;

function render(latex, outPng) {
  const node = doc.convert(latex, { display: true, em: 16, ex: 8 });
  let svg = adaptor.innerHTML(node);
  // MathJax draws glyphs with currentColor; pin to ink so it rasterizes dark.
  svg = svg.replaceAll('currentColor', INK).replaceAll('fill="currentColor"', `fill="${INK}"`);
  // pull width/height (in ex) from the root <svg ...>
  const wEx = parseFloat(svg.match(/width="([\d.]+)ex"/)[1]);
  const hEx = parseFloat(svg.match(/height="([\d.]+)ex"/)[1]);
  const mathW = wEx * PX_PER_EX;
  const mathH = hEx * PX_PER_EX;
  const W = Math.ceil(mathW + 2 * PAD);
  const H = Math.ceil(mathH + 2 * PAD);
  // strip the root svg's own width/height/style so we can size the nested instance
  const inner = svg
    .replace(/width="[\d.]+ex"/, `width="${mathW}"`)
    .replace(/height="[\d.]+ex"/, `height="${mathH}"`)
    .replace(/style="[^"]*"/, '');
  const wrapped =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">` +
    `<rect x="0" y="0" width="${W}" height="${H}" rx="${RADIUS}" ry="${RADIUS}" fill="${CARD}"/>` +
    `<g transform="translate(${PAD},${PAD})">${inner.replace(/^<svg /, `<svg x="0" y="0" `)}</g>` +
    `</svg>`;
  const resvg = new Resvg(wrapped, { fitTo: { mode: 'width', value: Math.round(W * SCALE) }, background: 'rgba(0,0,0,0)' });
  const png = resvg.render().asPng();
  writeFileSync(outPng, png);
  console.log(`${outPng}  ${W}x${H}css -> ${Math.round(W*SCALE)}x${Math.round(H*SCALE)}px  ${png.length}B`);
}

const eqs = {
  'eq-keys': String.raw`\mathrm{keys}_d(h) = \bigcup_{j=1}^{k-1} \{\, \texttt{d:}\sigma \mid \sigma \sqsubseteq s_j,\ 1 \le |\sigma| \le P \,\}`,
  'eq-emissions': String.raw`E = \sum_{h \in \text{Actors}} |\mathrm{keys}(h)|`,
  'eq-artifact': String.raw`A = \sum_{k \in \mathcal{K}} \min(N, \deg(k))`,
};
for (const [name, latex] of Object.entries(eqs)) render(latex, `/tmp/mathrender/${name}.png`);
