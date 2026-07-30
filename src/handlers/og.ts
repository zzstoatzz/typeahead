import { ImageResponse } from "workers-og";
import type { Env } from "../types";
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
const SPECIAL: Record<string, string> = { unknown: '#94a3b8', homepage: '#64748b' };

const RANGE_SECS: Record<string, number> = { '24h': 86400, '7d': 604800, '30d': 2592000, 'all': 2592000 };
const RANGE_LABEL: Record<string, string> = { '24h': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days', 'all': 'last 30 days' };

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
  if (SPECIAL[domain]) return SPECIAL[domain]!;
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
 * the next one. The first version of this card overlapped its own title with
 * its subtitle for exactly that reason. Heights below are budgeted to fit. */
function row(h: number, style: string, inner: string): string {
  return `<div style="display:flex;height:${h}px;flex-shrink:0;${style}">${inner}</div>`;
}

function shell(inner: string): string {
  return `<div style="display:flex;flex-direction:column;width:${W}px;height:${H}px;background:${BG};padding:56px 72px;font-family:'JetBrains Mono'">
    ${row(44, "align-items:center;gap:14px", `
      <div style="display:flex;font-size:34px;color:${BRIGHT};font-weight:700">typeahead</div>
      <div style="display:flex;width:16px;height:32px;background:${ACCENT}"></div>`)}
    ${inner}
    ${row(30, `margin-top:auto;font-size:22px;color:${DIM};align-items:flex-end`, "typeahead.waow.tech")}
  </div>`;
}

/** the comparison card: one bar per selected app, shared scale */
function comparison(apps: string[], series: Series, range: string, order: string[]): string {
  const secs = RANGE_SECS[range] ?? RANGE_SECS['7d']!;
  const { by, all } = totals(series, secs);
  const ranked = [...apps].sort((a, b) => (by.get(b) ?? 0) - (by.get(a) ?? 0));
  const colors = distinctColors(ranked, order);
  const rows = ranked.map((d, i) => ({ domain: d, hits: by.get(d) ?? 0, color: colors[i]! }));
  const max = Math.max(1, ...rows.map(r => r.hits));

  const bars = rows.map(r => {
    const pct = all > 0 ? ((r.hits / all) * 100).toFixed(1) : '0.0';
    return `<div style="display:flex;flex-direction:column;height:70px;flex-shrink:0">
      ${row(36, "align-items:center;gap:14px", `
        <div style="display:flex;width:18px;height:18px;border-radius:9px;background:${r.color};flex-shrink:0"></div>
        <div style="display:flex;font-size:30px;color:${TEXT}">${esc(r.domain)}</div>
        <div style="display:flex;margin-left:auto;font-size:30px;color:${BRIGHT}">${n(r.hits)}</div>
        <div style="display:flex;font-size:26px;color:${DIM};width:110px;justify-content:flex-end">${pct}%</div>`)}
      <div style="display:flex;width:100%;height:16px;flex-shrink:0;background:${PANEL};border-radius:8px">
        <div style="display:flex;width:${Math.max(2, Math.round((r.hits / max) * 100))}%;height:16px;background:${r.color};border-radius:8px"></div>
      </div>
    </div>`;
  }).join("");

  // the same statement the page makes under its chart
  let lead = "";
  if (rows.length >= 2 && rows[0]!.hits > 0 && rows[1]!.hits > 0) {
    const r = rows[0]!.hits / rows[1]!.hits;
    lead = `${rows[0]!.domain} leads ${r < 10 ? r.toFixed(2) : Math.round(r)}×`;
  }

  // the title is the one variable-width thing here; cap it so three long
  // domains cannot force a wrap the fixed height has no room for
  const heading = rows.map(r => r.domain).join("  vs  ");
  const headSize = heading.length > 34 ? 32 : heading.length > 24 ? 38 : 44;

  return `<div style="display:flex;flex-direction:column;flex-shrink:0;padding-top:26px">
    ${row(headSize + 12, `font-size:${headSize}px;color:${BRIGHT};align-items:center`, esc(heading))}
    ${row(34, `font-size:26px;color:${MUTED};align-items:center`, `search traffic · ${RANGE_LABEL[range] ?? RANGE_LABEL['7d']}`)}
    <div style="display:flex;flex-direction:column;gap:14px;padding-top:22px;flex-shrink:0">${bars}</div>
    ${lead ? row(52, `margin-top:18px;border-top:1px solid ${LINE};font-size:28px;color:${TEXT};align-items:flex-end`, `∫ ${esc(lead)}`) : ""}
  </div>`;
}

/** the default card: what the service is, and how big the index is */
function overview(total: number, handlePct: string): string {
  const stat = (v: string, label: string) => `<div style="display:flex;flex-direction:column;flex-shrink:0">
      ${row(70, `font-size:56px;color:${ACCENT};align-items:center`, v)}
      ${row(34, `font-size:26px;color:${MUTED};align-items:center`, label)}
    </div>`;
  return `<div style="display:flex;flex-direction:column;flex-shrink:0;padding-top:44px">
    ${row(66, `font-size:52px;color:${BRIGHT};align-items:center`, "community actor search")}
    ${row(66, `font-size:52px;color:${BRIGHT};align-items:center`, "for atproto")}
    <div style="display:flex;gap:70px;padding-top:48px;flex-shrink:0">
      ${stat(n(total), "actors indexed")}
      ${stat(handlePct + "%", "with a resolved handle")}
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

  const kv = await env.KV.get("stats_data", "json") as {
    snapshots?: { total: number; with_handles: number }[];
    traffic_series?: Series;
  } | null;

  const series = kv?.traffic_series ?? [];
  // slice order fixes the palette, exactly as the page assigns it
  const order = [...totals(series, RANGE_SECS['30d']!).by.entries()]
    .sort((a, b) => b[1] - a[1]).map(([d]) => d);

  const known = new Set(series.map(r => r.domain));
  const wanted = apps.filter(a => known.has(a));

  let body: string;
  if (wanted.length > 0) {
    body = comparison(wanted, series, RANGE_SECS[range] ? range : "7d", order);
  } else {
    const last = kv?.snapshots?.[kv.snapshots.length - 1];
    const total = last?.total ?? 0;
    const pct = last && last.total > 0 ? ((last.with_handles / last.total) * 100).toFixed(1) : "0";
    body = overview(total, pct);
  }

  return new ImageResponse(shell(body), {
    width: W,
    height: H,
    fonts: [{ name: "JetBrains Mono", data: await jetbrainsMono(), style: "normal" }],
    // crawlers refetch these constantly; the underlying data only moves hourly
    headers: { "Cache-Control": "public, max-age=3600" },
  });
}
