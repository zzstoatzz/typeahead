import { Resvg } from '@resvg/resvg-js';
import { readFileSync, writeFileSync } from 'node:fs';
const [,, inSvg, outPng, scaleStr] = process.argv;
const scale = parseFloat(scaleStr || '2');
const svg = readFileSync(inSvg, 'utf8');
const m = svg.match(/viewBox="0 0 ([\d.]+) ([\d.]+)"/) || svg.match(/width="([\d.]+)"[^>]*height="([\d.]+)"/);
const w = Math.round(parseFloat(m[1]) * scale);
const resvg = new Resvg(svg, {
  fitTo: { mode: 'width', value: w },
  font: { loadSystemFonts: true, defaultFontFamily: 'Helvetica' },
  background: 'white',
});
const png = resvg.render().asPng();
writeFileSync(outPng, png);
console.log(`${outPng}  ${w}px wide  ${png.length}B`);
