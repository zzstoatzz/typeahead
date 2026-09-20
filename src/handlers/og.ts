import { ImageResponse } from "workers-og";
import type { Env, StatsKvData } from "../types";
import { FAVICON_SVG } from "../types";
import { escHtml as esc } from "../utils";

/** Link-preview card for /stats.
 *
 *  Reads the same materialized KV blob /stats does, so a preview costs no Turso
 *  query and can never disagree with the page it previews.
 *
 *  Rendered with satori, which supports a flexbox subset of CSS and no canvas —
 *  hence bars rather than the line chart the page draws. Bars survive being
 *  looked at as a 300px-wide thumbnail in a feed, which a 5-minute-resolution
 *  line does not.
 */

const W = 1200, H = 630;
// The card cannot follow the viewer's theme, so it commits to the dark one.
// Values match theme.ts.
const BG = "#0a0b0a", PANEL = "#101211", LINE = "#212523";
const TEXT = "#d9dedb", BRIGHT = "#f2f5f3", MUTED = "#8a938e", DIM = "#59615c";
const ACCENT = "#44aa99";
const PIE_COLORS = ['#14b8a6', '#3b82f6', '#a855f7', '#f59e0b', '#f43f5e', '#06b6d4', '#65a30d', '#f97316', '#ec4899'];
const SPECIAL = { unknown: '#94a3b8', homepage: '#64748b' } satisfies Record<string, string>;
const isSpecial = (d: string): d is keyof typeof SPECIAL => Object.hasOwn(SPECIAL, d);

const RANGE_SECS = { '24h': 86400, '7d': 604800, '30d': 2592000, 'all': 2592000 } as const;
const RANGE_LABEL = { '24h': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days', 'all': 'last 30 days' } as const;
type Range = keyof typeof RANGE_SECS;
const isRange = (r: string): r is Range => Object.hasOwn(RANGE_SECS, r);

type Series = { hour: number; domain: string; hits: number }[];

/** totals per domain over the window, plus the overall total */
function totals(series: Series, secs: number) {
  const cutoff = Math.floor(Date.now() / 1000 / 3600) - secs / 3600;
  const by = new Map<string, number>();
  let all = 0;
  for (const r of series) {
    if (r.hour < cutoff) continue;
    by.set(r.domain, (by.get(r.domain) ?? 0) + r.hits);
    all += r.hits;
  }
  return { by, all };
}

/** the page's colour for a domain — its rank position, wrapped into the palette */
function colorFor(domain: string, order: string[]): string {
  if (isSpecial(domain)) return SPECIAL[domain];
  const i = order.indexOf(domain);
  return PIE_COLORS[(i < 0 ? 0 : i) % PIE_COLORS.length]!;
}

/** Ranks nine apart share a palette entry, which on the page is fine — they are
 *  never adjacent — but on a card of two bars it reads as one series drawn
 *  twice. Keep the page's colour where it is unambiguous, break ties otherwise. */
function distinctColors(domains: string[], order: string[]): string[] {
  const out: string[] = [];
  for (const d of domains) {
    let c = colorFor(d, order);
    if (out.includes(c)) c = PIE_COLORS.find(p => !out.includes(p)) ?? c;
    out.push(c);
  }
  return out;
}

const n = (v: number) => v.toLocaleString("en-US");

/* Every child carries flex-shrink:0 and an explicit height.
 *
 * satori resolves a column whose content exceeds the fixed 630px by SHRINKING
 * its children rather than clipping, and a shrunk-to-zero row draws on top of
 * the next one. An earlier version overlapped its own title with its subtitle
 * for exactly that reason. Heights below are budgeted to fit. */
function row(h: number, style: string, inner: string): string {
  return `<div style="display:flex;height:${h}px;flex-shrink:0;${style}">${inner}</div>`;
}
const rule = `<div style="display:flex;height:1px;flex-shrink:0;background:${LINE}"></div>`;

/** The page is panels on a dark field, hairline rules, one mono face, and a
 *  caret after the wordmark. The card is the same, at poster size. */
function shell(inner: string, right: string): string {
  return `<div style="display:flex;width:${W}px;height:${H}px;background:${BG};padding:44px;font-family:'JetBrains Mono'">
    <div style="display:flex;flex-direction:column;width:100%;height:100%;background:${PANEL};border:1px solid ${LINE};border-radius:18px;padding:44px 52px">
      ${row(46, "align-items:center", `
        <div style="display:flex;font-size:32px;color:${BRIGHT};font-weight:700">typeahead</div>
        <div style="display:flex;width:15px;height:30px;margin-left:12px;background:${ACCENT}"></div>
        <div style="display:flex;margin-left:auto;font-size:24px;color:${DIM}">${right}</div>`)}
      ${rule}
      ${inner}
      ${rule}
      ${row(46, "align-items:center", `
        <div style="display:flex;font-size:22px;color:${DIM}">typeahead.waow.tech</div>`)}
    </div>
  </div>`;
}

/** one bar per app, on a shared scale */
function comparison(apps: string[], series: Series, range: Range, order: string[]): string {
  const secs = RANGE_SECS[range];
  const { by, all } = totals(series, secs);
  const ranked = [...apps].sort((a, b) => (by.get(b) ?? 0) - (by.get(a) ?? 0));
  const colors = distinctColors(ranked, order);
  const rows = ranked.map((d, i) => ({ domain: d, hits: by.get(d) ?? 0, color: colors[i]! }));
  const max = Math.max(1, ...rows.map(r => r.hits));

  // long domains shrink rather than wrap — a wrap has nowhere to go
  const longest = Math.max(...rows.map(r => r.domain.length));
  const nameSize = longest > 22 ? 28 : longest > 16 ? 32 : 36;

  const bars = rows.map(r => `<div style="display:flex;flex-direction:column;height:${rows.length > 2 ? 92 : 116}px;flex-shrink:0;justify-content:center">
      ${row(44, "align-items:center", `
        <div style="display:flex;width:16px;height:16px;border-radius:8px;background:${r.color};flex-shrink:0"></div>
        <div style="display:flex;margin-left:16px;font-size:${nameSize}px;color:${TEXT}">${esc(r.domain)}</div>
        <div style="display:flex;margin-left:auto;font-size:${nameSize}px;color:${BRIGHT}">${n(r.hits)}</div>
        <div style="display:flex;width:130px;justify-content:flex-end;font-size:24px;color:${DIM}">${all > 0 ? ((r.hits / all) * 100).toFixed(1) : '0.0'}%</div>`)}
      <div style="display:flex;width:100%;height:12px;flex-shrink:0;background:${BG};border-radius:6px">
        <div style="display:flex;width:${Math.max(2, Math.round((r.hits / max) * 100))}%;height:12px;background:${r.color};border-radius:6px"></div>
      </div>
    </div>`).join("");

  // a ratio of request volume, which is not a ranking of the apps producing it
  let ratio = "";
  if (rows.length >= 2 && rows[0]!.hits > 0 && rows[1]!.hits > 0) {
    const r = rows[0]!.hits / rows[1]!.hits;
    ratio = `${rows[0]!.domain} ${r < 10 ? r.toFixed(2) : Math.round(r)}\u00d7 ${rows[1]!.domain}`;
  }

  return `<div style="display:flex;flex-direction:column;flex:1;justify-content:center;padding:8px 0">
    ${bars}
    ${ratio ? row(46, `align-items:center;font-size:24px;color:${MUTED}`,
      `<div style="display:flex;color:${DIM};margin-right:12px">\u222b</div>${esc(ratio)} by request volume`) : ""}
  </div>`;
}

/** "11.9M" reads at thumbnail size where "11,901,310" is a grey smear.
 *  Below ten thousand the exact figure is short enough to keep. */
export function compact(v: number): string {
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (v >= 10_000) return Math.round(v / 1_000) + "K";
  return n(v);
}

const pct = (part: number, whole: number) => whole > 0 ? ((part / whole) * 100).toFixed(1) + "%" : "0%";

export interface OverviewFigures {
  total: number;
  withHandles: number;
  withAvatars: number;
  hidden: number;
  /** searches answered over the last seven days, cache hits included */
  searches7d: number;
}

/** the figures the default card shows, from the same KV blob /stats reads */
export function overviewFigures(kv: StatsKvData | null): OverviewFigures {
  const last = kv?.snapshots?.[kv.snapshots.length - 1];
  // metrics rows are 5-minute buckets, newest first (see materializeStats)
  const since = Math.floor(Date.now() / 300_000) - 7 * 288;
  const searches7d = (kv?.metrics ?? [])
    .filter(r => r.hour >= since)
    .reduce((s, r) => s + r.searches + (r.cache_hits ?? 0), 0);
  return {
    total: last?.total ?? 0,
    withHandles: last?.with_handles ?? 0,
    withAvatars: last?.with_avatars ?? 0,
    hidden: last?.hidden ?? 0,
    searches7d,
  };
}

/** the default card: what this is, what makes it different, and how big it is.
 *  Four tiles share ~1096px, so every caption is kept under 18 characters and
 *  told not to wrap: a wrapped caption draws over the next row (see row()). */
export function overview(f: OverviewFigures): string {
  const tile = (v: string, label: string, sub: string) => `<div style="display:flex;flex-direction:column;flex:1;flex-shrink:0">
      ${row(64, `font-size:52px;color:${ACCENT};align-items:center`, v)}
      ${row(32, `font-size:22px;color:${TEXT};align-items:center;white-space:nowrap`, label)}
      ${row(28, `font-size:18px;color:${DIM};align-items:center;white-space:nowrap`, sub)}
    </div>`;
  const handlePct = f.total > 0 ? Math.round((f.withHandles / f.total) * 100) : 0;
  return `<div style="display:flex;flex-direction:column;flex:1;justify-content:center">
    ${row(60, `font-size:46px;color:${BRIGHT};align-items:center`, "community actor search for atproto")}
    ${row(40, `font-size:23px;color:${MUTED};align-items:center`, "every actor on the network \u00b7 relay-firehose fresh \u00b7 moderation applied")}
    <div style="display:flex;gap:28px;padding-top:40px;flex-shrink:0">
      ${tile(compact(f.total), "actors indexed", handlePct + "% with handles")}
      ${tile(pct(f.withAvatars, f.total), "with avatars", compact(f.withAvatars) + " profile pics")}
      ${tile(compact(f.hidden), "hidden", "moderation labels")}
      ${tile(compact(f.searches7d), "searches", "last 7 days")}
    </div>
  </div>`;
}

// satori needs real font bytes; there is no system font in the runtime
async function jetbrainsMono(): Promise<ArrayBuffer> {
  const css = await (await fetch("https://fonts.googleapis.com/css2?family=JetBrains+Mono", {
    // the CSS served depends on the UA — an old desktop Safari string gets
    // truetype rather than woff2, which is what satori can parse
    headers: { "User-Agent": "Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_6_8; de-at) AppleWebKit/533.21.1 (KHTML, like Gecko) Version/5.0.5 Safari/533.21.1" },
  })).text();
  const m = css.match(/src: url\((.+?)\) format\('(opentype|truetype)'\)/);
  if (!m) throw new Error("could not resolve JetBrains Mono");
  return await (await fetch(m[1]!)).arrayBuffer();
}

export async function handleOgImage(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const range = url.searchParams.get("range") ?? "7d";
  const apps = (url.searchParams.get("apps") ?? "")
    .split(",").map(s => s.trim()).filter(Boolean).slice(0, 3); // 3 bars is the height budget

  const kv = await env.KV.get<StatsKvData>("stats_data", "json");

  const series = kv?.traffic_series ?? [];
  // slice order fixes the palette, exactly as the page assigns it
  const order = [...totals(series, RANGE_SECS['30d']).by.entries()]
    .sort((a, b) => b[1] - a[1]).map(([d]) => d);

  const known = new Set(series.map(r => r.domain));
  const wanted = apps.filter(a => known.has(a));

  let body: string, right: string;
  if (wanted.length > 0) {
    const r: Range = isRange(range) ? range : "7d";
    body = comparison(wanted, series, r, order);
    right = "requests \u00b7 " + RANGE_LABEL[r];
  } else {
    body = overview(overviewFigures(kv));
    right = "";   // the headline below already says it
  }

  return new ImageResponse(shell(body, right), {
    width: W,
    height: H,
    fonts: [{ name: "JetBrains Mono", data: await jetbrainsMono(), style: "normal" }],
    // crawlers refetch these constantly; the underlying data only moves hourly
    headers: { "Cache-Control": "public, max-age=3600" },
  });
}

/** iOS and Android ignore SVG icons, so the mark is rasterized once here and
 *  cached for a day. satori drops an inline <svg> element on the floor (the
 *  first version rendered a blank square) but embeds an <img> data URI. */
export function handleAppleTouchIcon(): Response {
  const src = "data:image/svg+xml;base64," + btoa(FAVICON_SVG);
  return new ImageResponse(`<div style="display:flex;width:180px;height:180px"><img src="${src}" width="180" height="180" /></div>`, {
    width: 180,
    height: 180,
    headers: { "Cache-Control": "public, max-age=86400" },
  });
}

export function handleWebManifest(): Response {
  const manifest = {
    name: "typeahead",
    short_name: "typeahead",
    description: "community actor search for atproto",
    start_url: "/",
    display: "standalone",
    background_color: BG,
    theme_color: BG,
    icons: [
      { src: "/favicon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" },
    ],
  };
  return new Response(JSON.stringify(manifest), {
    headers: { "Content-Type": "application/manifest+json", "Cache-Control": "public, max-age=86400" },
  });
}
