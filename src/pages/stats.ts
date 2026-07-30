import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT } from "./theme";

export interface SnapshotPoint {
  ts: number;  // millisecond timestamp
  total: number;
  with_handles: number;
  with_avatars: number;
}

export interface StatsData {
  total: number;
  hiddenCount: number;
  rows: { hour: number; searches: number; total_ms: number; cache_hits: number; cache_ms: number }[];
  totalSearches: number;
  avgLatency: number;
  avgCacheLatency: number;
  handlePct: string;
  avatarPct: string;
  snapshots: SnapshotPoint[];
  trafficSources: { domain: string; hits: number }[];
  trafficSeries: { hour: number; domain: string; hits: number }[];
  trafficSeries5m: { bucket: number; domain: string; hits: number }[];
}

/** cached/cold latency as two atomic pairs.
 *
 *  This was one inline string — "6.2 ms <span>cached</span> · 173 ms
 *  <span>cold</span>" — so at narrow widths the last tag wrapped by itself and
 *  the card rendered a lone orphaned "cold" on its own line. Each number+tag is
 *  now one nowrap unit, so they either sit on one line or wrap as whole pairs.
 */
function latencyHtml(cached: number, cold: number): string {
  const pair = (n: string, tag: string) =>
    `<span class="lat-pair"><span class="lat-n">${n}</span><span class="lat-t">${tag}</span></span>`;
  const parts = [];
  if (cached > 0) parts.push(pair(`${cached.toFixed(1)} ms`, "cached"));
  parts.push(pair(`${cold.toFixed(0)} ms`, "cold"));
  return parts.join('<span class="lat-sep">·</span>');
}

export function statsPage(d: StatsData): string {
  const sorted = [...d.rows].reverse(); // oldest first
  // Window by TIME, not bucket count: metric buckets are 5-min (hour*300 = unix
  // seconds) and SPARSE (only buckets with a search exist, ~64/day not 288), so
  // "last N buckets" ≠ "last N days".
  const nowSec = Math.floor(Date.now() / 1000);
  // SSR renders the 7d view so the page is correct without JS and the value
  // agrees with the active button on load.
  // Aggregate the metric buckets HERE, not in the browser.
  //
  // The client only ever reduced this array to four numbers per range — it is
  // never plotted. Shipping every 5-minute bucket to do that made the buckets
  // 72% of a 1 MB page (762 KB, ~19k rows) and put the page's weight on a curve
  // that grows with the site's age: the query is LIMIT 100000, so left alone it
  // heads for several MB. Sixteen numbers instead.
  const ACTIVITY_SECS: Record<string, number | null> =
    { '24h': 86400, '7d': 604800, '30d': 2592000, 'all': null };
  const activity: Record<string, { searches: number; cacheHits: number; totalMs: number; cacheMs: number }> = {};
  for (const [range, secs] of Object.entries(ACTIVITY_SECS)) {
    const a = { searches: 0, cacheHits: 0, totalMs: 0, cacheMs: 0 };
    for (const r of sorted) {
      // buckets are 5-min (hour * 300 = unix seconds) and SPARSE — only buckets
      // with a search exist — so windows are by TIME, never by bucket count
      if (secs !== null && r.hour * 300 < nowSec - secs) continue;
      a.searches += r.searches;
      a.cacheHits += r.cache_hits ?? 0;
      a.totalMs += r.total_ms ?? 0;
      a.cacheMs += r.cache_ms ?? 0;
    }
    activity[range] = a;
  }
  const activityJson = JSON.stringify(activity);
  // SSR renders the 7d view so the page is correct without JS and the value
  // agrees with the active button on load.
  const initialTotal = activity['7d']!.searches + activity['7d']!.cacheHits;

  // The trend chart plots these, so unlike the metrics they cannot collapse to
  // a scalar — but 5000 hourly points render into ~620px, so most were being
  // thrown away at draw time anyway. Keep every hour for the last 30 days (the
  // two windowed views), then every 24th sample beyond it for "all". Real
  // samples, never averages, so a hover still reports a value that existed.
  const HOURLY_KEEP_MS = 30 * 86400_000;
  const cutoff = Date.now() - HOURLY_KEEP_MS;
  const lastIdx = d.snapshots.length - 1;
  const thinned = d.snapshots.filter((s, i) =>
    s.ts >= cutoff || i % 24 === 0 || i === lastIdx);
  const snapshotJson = JSON.stringify(thinned);

  const PIE_COLORS = [
    // Ordered so adjacent slices never sit next to a neighbour on the wheel —
    // the old set had two violets and two ambers that read as the same colour on
    // a 90px line. Mid-tone rather than pastel or deep, since the page renders
    // on both a light and a dark background.
    '#14b8a6', '#3b82f6', '#a855f7', '#f59e0b', '#f43f5e', '#06b6d4', '#65a30d', '#f97316', '#ec4899',
  ];
  const SPECIAL_DOMAINS: Record<string, string> = { unknown: '#94a3b8', homepage: '#64748b' };
  const PIE_TOP_N = 12;
  const trafficTotal = d.trafficSources.reduce((s, r) => s + r.hits, 0);
  const pieSlice = d.trafficSources.slice(0, PIE_TOP_N);
  const trafficJson = JSON.stringify(pieSlice);
  const trafficGrandTotal = trafficTotal;
  // per-domain hourly series for every recorded domain (bounded: ~50 domains ×
  // ≤720 hourly rows), compact form: domain -> [[hour, hits], ...]
  const seriesByDomain: Record<string, [number, number][]> = {};
  for (const r of d.trafficSeries) {
    (seriesByDomain[r.domain] ??= []).push([r.hour, r.hits]);
  }
  const trafficSeriesJson = JSON.stringify(seriesByDomain);
  // raw 5-min buckets (last 48h) for the 24h view
  const seriesByDomain5m: Record<string, [number, number][]> = {};
  for (const r of d.trafficSeries5m) {
    (seriesByDomain5m[r.domain] ??= []).push([r.bucket, r.hits]);
  }
  const trafficSeries5mJson = JSON.stringify(seriesByDomain5m);

  // domains come from caller-controlled headers (X-Client/Origin) — escape them
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const pieLegendHtml = pieSlice.map((r, i) => {
    const color = SPECIAL_DOMAINS[r.domain] ?? PIE_COLORS[i % PIE_COLORS.length];
    const pct = trafficGrandTotal > 0 ? ((r.hits / trafficGrandTotal) * 100).toFixed(1) : '0';
    const label = r.domain in SPECIAL_DOMAINS
      ? esc(r.domain)
      : `<a href="https://${esc(r.domain)}" target="_blank" rel="noopener" class="legend-link">${esc(r.domain)}</a>`;
    return `<span data-domain="${esc(r.domain)}"><span class="ldot" style="background:${color}"></span>${label} (${pct}%)</span>`;
  }).join('');

  const trafficTableRows = d.trafficSources.map((r, i) => {
    const pct = trafficGrandTotal > 0 ? ((r.hits / trafficGrandTotal) * 100).toFixed(1) : '0';
    const label = r.domain in SPECIAL_DOMAINS
      ? esc(r.domain)
      : `<a href="https://${esc(r.domain)}" target="_blank" rel="noopener" class="legend-link">${esc(r.domain)}</a>`;
    return `<tr data-domain="${esc(r.domain)}"><td class="ts-rank">${i + 1}</td><td class="ts-domain">${label}</td><td class="ts-hits">${r.hits.toLocaleString()}</td><td class="ts-pct">${pct}%</td></tr>`;
  }).join('');

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>typeahead — stats</title>
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    body { display: flex; justify-content: center;
           padding: calc(1.5rem + env(safe-area-inset-top)) 1rem calc(2rem + env(safe-area-inset-bottom)); }
    .container { max-width: 620px; width: 100%; }
    .header { display: flex; align-items: baseline; justify-content: space-between; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: var(--muted); }
    .subtitle { font-size: 0.8rem; color: var(--dim); margin-bottom: 1.5rem; }

    .chart-wrap, .activity-wrap, .pie-wrap {
      background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
      padding: 1rem; margin-bottom: 1.25rem; position: relative; }
    .chart-wrap h2, .activity-wrap h2, .pie-wrap h2 {
      font-size: 0.75rem; font-weight: 400; color: var(--muted); margin-bottom: 0.6rem; }
    .ranged-header { display: flex; justify-content: space-between; align-items: center;
                     flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.6rem; }
    .ranged-header h2 { margin-bottom: 0; }
    .range-btns { display: inline-flex; gap: 0.2rem; }
    .range-btns button { background: transparent; border: 1px solid var(--line); color: var(--dim);
                         padding: 0.35rem 0.65rem; font-size: 0.72rem; cursor: pointer;
                         border-radius: 6px; font-family: inherit; min-height: 34px; }
    .range-btns button:hover { color: var(--muted); border-color: var(--line-2); }
    .range-btns button.active { background: var(--panel-2); color: var(--text); border-color: var(--line-2); }
    #trend { width: 100%; height: 200px; display: block; touch-action: none; }
    .legend { display: flex; gap: 1.2rem; margin-top: 0.6rem; font-size: 0.7rem; color: var(--muted); }
    .legend span { display: flex; align-items: center; gap: 0.35rem; }
    .ldot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .chart-tip, .pie-tip, .series-tip {
      display: none; position: fixed; background: var(--tip-bg);
      border: 1px solid var(--line-2); border-radius: 8px; padding: 0.5rem 0.7rem;
      font-size: 0.72rem; color: var(--text); pointer-events: none; z-index: 50;
      line-height: 1.6; white-space: nowrap; box-shadow: 0 4px 12px rgba(0,0,0,0.35); }
    .ct-time { color: var(--dim); font-size: 0.65rem; }
    .ct-row { display: flex; align-items: center; gap: 0.35rem; }
    .ct-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }

    svg { width: 100%; height: auto; }
    polyline { fill: none; stroke: var(--accent); stroke-width: 1.5; }

    .summary { display: grid; gap: 0.8rem; margin-bottom: 1.25rem; }
    /* metrics nested in the activity card: the card already provides the
       border and padding, so don't draw a second frame inside it. */
    .activity-wrap .summary { margin-bottom: 0; }
    .activity-wrap .metric { border: none; padding: 0; background: none; }
    .summary-2 { grid-template-columns: repeat(2, 1fr); }
    .summary-3 { grid-template-columns: repeat(3, 1fr); }
    .metric { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 0.8rem 0.9rem; }
    .metric .metric-head { display: flex; justify-content: space-between; align-items: center;
                           flex-wrap: wrap; gap: 0.4rem 0.5rem; margin-bottom: 0.2rem; }
    .metric .metric-head .label { margin-bottom: 0; }
    .metric .label { font-size: 0.7rem; color: var(--muted); margin-bottom: 0.2rem; }
    /* latency renders as atomic number+tag pairs so a tag can never orphan onto
       its own line the way "cold" did. */
    .value.latency { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.4rem; font-size: 1.3rem; }
    .lat-pair { display: inline-flex; align-items: baseline; gap: 0.28rem; white-space: nowrap; }
    .lat-t { opacity: 0.4; font-size: 0.85rem; }
    .lat-sep { opacity: 0.3; }
    .metric .label[data-tip] { cursor: default; position: relative; border-bottom: 1px dotted var(--faint); display: inline-block; }
    .metric .label[data-tip]:hover::after, .metric .label[data-tip]:focus::after {
      content: attr(data-tip); position: absolute; left: 0; top: 1.6em; width: 200px;
      padding: 0.4rem 0.5rem; background: var(--panel-2); border: 1px solid var(--line-2); border-radius: 6px;
      font-size: 0.95em; color: var(--muted); line-height: 1.4; z-index: 20; white-space: normal;
    }
    .metric .value { font-size: 1.15rem; color: var(--text); }

    #pie { display: block; margin: 0 auto; width: 280px; height: 280px; touch-action: none; }
    .pie-legend { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; margin-top: 0.7rem;
                  font-size: 0.7rem; color: var(--muted); justify-content: center; }
    .pie-legend span { display: flex; align-items: center; gap: 0.3rem; line-height: 1.4;
                       padding: 0.25rem 0.1rem; }
    .legend-link { color: var(--muted); text-decoration: none; border-bottom: 1px dotted var(--faint); }
    .legend-link:hover { color: var(--text); border-bottom-color: var(--muted); }
    .traffic-series { margin-top: 1rem; padding-top: 0.8rem; border-top: 1px solid var(--line); }
    .traffic-series h2 { font-size: 0.72rem; font-weight: 400; color: var(--muted); margin-bottom: 0; }
    .traffic-series .ranged-header { margin-bottom: 0.5rem; }
    /* The plot RESERVES its height so swapping the chart for the empty state
       cannot move the page. Previously the empty state was content-sized while
       the svg was responsive (viewBox 600x90, height:auto), so hovering a domain
       with no traffic in the window made everything below lurch up. */
    .tseries-plot { position: relative; aspect-ratio: 600 / 135; }
    .tseries-plot > svg, .tseries-plot > #tseries-empty {
      position: absolute; inset: 0; width: 100%; height: 100%;
    }
    #tseries-empty { display: flex; align-items: center; justify-content: center;
                     color: var(--faint); font-size: 0.75rem; text-align: center; }
    #tseries { cursor: crosshair; touch-action: pan-y; }
    #tseries polyline { vector-effect: non-scaling-stroke; fill: none; stroke-width: 1.5; }
    /* the fat transparent twin under each line is the click/tap target — a
       1.5px stroke is unhittable, especially with a finger */
    #tseries polyline.hit { stroke-width: 12; stroke: transparent; cursor: pointer; }
    #tseries polyline.baseline { stroke-dasharray: 3 3; stroke-width: 1; opacity: 0.75; }
    #tseries-guide { stroke: var(--line-2); stroke-width: 1; vector-effect: non-scaling-stroke; }
    #tseries-dots circle { fill: var(--bg); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
    #tseries-chips { display: flex; flex-wrap: wrap; gap: 0.35rem; justify-content: flex-end; }
    .ts-chip { display: inline-flex; align-items: center; gap: 0.4rem;
               background: var(--panel-2); border: 1px solid var(--line-2); color: var(--text);
               padding: 0.35rem 0.5rem 0.35rem 0.6rem; font-size: 0.72rem; cursor: pointer;
               border-radius: 999px; font-family: inherit; min-height: 34px; }
    .ts-chip .x { color: var(--faint); font-size: 0.95rem; line-height: 1; }
    .ts-chip:hover { border-color: var(--line); }
    .ts-chip:hover .x { color: var(--text); }
    .ts-chip .ldot.dash { background: none !important; border: 1.5px dashed var(--dim); }
    .ts-chip.preview { background: transparent; border-style: dashed; color: var(--muted);
                       cursor: default; pointer-events: none; }
    .tseries-axis { display: flex; justify-content: space-between; font-size: 0.65rem;
                    color: var(--dim); margin-top: 0.25rem; font-variant-numeric: tabular-nums; }
    .tseries-foot { display: flex; align-items: center; justify-content: space-between;
                    gap: 0.5rem; margin-top: 0.5rem; }
    .tseries-hint { font-size: 0.65rem; color: var(--faint); }
    .ts-foot-btns { display: flex; align-items: center; gap: 0.3rem; flex-shrink: 0; }
    /* sticky shift: touch has no modifier key, so multi-select needs a mode.
       A held toggle rather than an accumulating tap, so nobody collects a
       selection they didn't ask for. */
    #tseries-compare { background: transparent; border: 1px solid var(--line); color: var(--dim);
                       padding: 0.3rem 0.55rem; font-size: 0.65rem; cursor: pointer;
                       border-radius: 6px; font-family: inherit; min-height: 28px; white-space: nowrap; }
    #tseries-compare:hover { color: var(--text); border-color: var(--line-2); }
    #tseries-compare[aria-pressed="true"] { background: var(--panel-2); color: var(--text);
                                            border-color: var(--accent); }
    .ts-menu-wrap { position: relative; flex-shrink: 0; }
    #tseries-menu-btn { background: transparent; border: 1px solid var(--line); color: var(--dim);
                        padding: 0.2rem 0.5rem 0.35rem; font-size: 0.8rem; line-height: 1; cursor: pointer;
                        border-radius: 6px; font-family: inherit; min-height: 28px; }
    #tseries-menu-btn:hover, #tseries-menu-btn[aria-expanded="true"] { color: var(--text); border-color: var(--line-2); }
    .ts-menu { position: absolute; right: 0; bottom: calc(100% + 0.3rem); z-index: 40;
               background: var(--tip-bg); border: 1px solid var(--line-2); border-radius: 8px;
               padding: 0.25rem; display: flex; flex-direction: column; min-width: 140px;
               box-shadow: 0 4px 12px rgba(0,0,0,0.35); }
    .ts-menu[hidden] { display: none; }
    .ts-menu button { background: transparent; border: 0; color: var(--muted); text-align: left;
                      padding: 0.5rem 0.6rem; font-size: 0.7rem; font-family: inherit;
                      cursor: pointer; border-radius: 5px; white-space: nowrap; }
    .ts-menu button:hover { background: var(--hover); color: var(--text); }
    /* the integral row — area under each curve over the plotted window */
    .tseries-totals { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.15rem 0.6rem;
                      font-size: 0.68rem; color: var(--muted); margin-top: 0.5rem; }
    .tseries-totals:empty { display: none; }
    .tseries-totals .sig { color: var(--dim); font-size: 0.85rem; cursor: default;
                           position: relative; border-bottom: 1px dotted var(--faint); }
    .tseries-totals .sig:hover::after, .tseries-totals .sig:focus::after {
      content: attr(data-tip); position: absolute; left: 0; top: 1.8em; width: 190px;
      background: var(--tip-bg); border: 1px solid var(--line-2); border-radius: 6px;
      padding: 0.45rem 0.6rem; font-size: 0.65rem; color: var(--muted); z-index: 30; line-height: 1.5; }
    .tseries-totals .tot { display: inline-flex; align-items: baseline; gap: 0.3rem; }
    .tseries-totals .tot b { font-weight: 400; color: var(--text); font-variant-numeric: tabular-nums; }
    .tseries-totals .tot .pct { color: var(--faint); }
    .tseries-totals .lead { color: var(--text); }
    .pie-legend span[data-domain] { cursor: pointer; }
    .pie-legend span.sel { color: var(--bright); }
    .pie-legend span.sel .ldot { outline: 1.5px solid var(--bright); outline-offset: 1px; }
    .ts-table tbody tr[data-domain] { cursor: pointer; }
    .ts-table tbody tr[data-domain]:hover td { background: var(--hover); }
    .ts-table tbody tr.sel td { background: color-mix(in srgb, var(--accent) 12%, var(--panel)); }

    .ts-table-wrap { margin-top: 1rem; max-height: 280px; overflow-y: auto;
                     -webkit-overflow-scrolling: touch;
                     border: 1px solid var(--line); border-radius: 8px; }
    .ts-table-wrap::-webkit-scrollbar { width: 8px; }
    .ts-table-wrap::-webkit-scrollbar-track { background: var(--bg); }
    .ts-table-wrap::-webkit-scrollbar-thumb { background: var(--line-2); border-radius: 4px; }
    .ts-table { width: 100%; border-collapse: collapse; font-size: 0.72rem;
                color: var(--text); table-layout: fixed; }
    .ts-table thead th { position: sticky; top: 0; background: var(--panel-2); color: var(--dim);
                         font-weight: 400; text-align: left; padding: 0.45rem 0.7rem;
                         border-bottom: 1px solid var(--line); font-size: 0.68rem;
                         text-transform: uppercase; letter-spacing: 0.04em; }
    .ts-table td { padding: 0.5rem 0.7rem; border-bottom: 1px solid var(--line); }
    .ts-table tr:last-child td { border-bottom: none; }
    .ts-rank { color: var(--dim); width: 2.2rem; }
    .ts-domain { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ts-domain a { display: inline-block; max-width: 100%;
                   overflow: hidden; text-overflow: ellipsis;
                   white-space: nowrap; vertical-align: bottom; }
    .ts-hits { text-align: right; font-variant-numeric: tabular-nums; color: var(--text);
               width: 4.5rem; }
    .ts-pct { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted);
              width: 3.2rem; }
    .ts-summary { font-size: 0.7rem; color: var(--dim); margin-top: 0.6rem; text-align: center; }

    footer { padding-top: 1.5rem; border-top: 1px solid var(--line); font-size: 0.7rem;
             color: var(--faint); display: flex; align-items: center; justify-content: center;
             flex-wrap: wrap; gap: 0.4rem 0.6rem; }
    footer a { color: var(--dim); text-decoration: none; padding: 0.35rem 0.15rem; }
    footer a:hover { color: var(--muted); }

    @media (max-width: 640px) {
      body { padding-left: 0.75rem; padding-right: 0.75rem; }
      #trend { height: 170px; }
      .legend { gap: 0.8rem; font-size: 0.65rem; flex-wrap: wrap; }
      .summary-2, .summary-3 { grid-template-columns: 1fr 1fr; }
      #pie { width: 230px; height: 230px; }
      .pie-legend { gap: 0.3rem 0.6rem; font-size: 0.68rem; }
      .tseries-plot { aspect-ratio: auto; height: 72px; }
      .ts-table-wrap { max-height: 320px; }
      .ts-table { font-size: 0.7rem; }
      .ts-table thead th { padding: 0.4rem 0.5rem; font-size: 0.62rem; }
      .ts-table td { padding: 0.5rem 0.5rem; }
      .ts-rank { width: 1.8rem; }
      .ts-hits { width: 3.6rem; }
      .ts-pct { width: 2.8rem; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><a href="/" class="wordmark">typeahead</a> stats</h1>
    </div>
    <p class="subtitle">index health and search activity</p>

    <div class="chart-wrap">
      <div class="ranged-header">
        <h2>actors indexed</h2>
        <span class="range-btns" id="trend-range">
          <button data-range="7d">7d</button>
          <button data-range="30d">30d</button>
          <button data-range="all" class="active">all</button>
        </span>
      </div>
      ${d.snapshots.length > 1
        ? `<canvas id="trend"></canvas>`
        : `<div style="color:#444;font-size:0.8rem;padding:2rem 0;text-align:center">collecting data — check back soon</div>`}
      <div class="legend">
        ${[
          { color: '#4a9', label: `total (${d.total.toLocaleString()})`, value: d.total },
          { color: '#bc8cff', label: 'with avatars', value: d.snapshots[d.snapshots.length - 1]?.with_avatars ?? 0 },
          { color: '#58a6ff', label: 'with handles', value: d.snapshots[d.snapshots.length - 1]?.with_handles ?? 0 },
        ].sort((a, b) => b.value - a.value)
         .map(r => `<span><span class="ldot" style="background:${r.color}"></span> ${r.label}</span>`)
         .join('\n        ')}
      </div>
    </div>
    <div class="chart-tip" id="chart-tip"></div>

    <div class="summary summary-3">
      <div class="metric">
        <div class="label" tabindex="0" data-tip="% of indexed actors with a resolved handle (vs bare DID only)">handle coverage</div>
        <div class="value">${d.handlePct}%</div>
      </div>
      <div class="metric">
        <div class="label" tabindex="0" data-tip="% of indexed actors with a profile image">avatar coverage</div>
        <div class="value">${d.avatarPct}%</div>
      </div>
      <div class="metric">
        <div class="label" tabindex="0" data-tip="actors hidden by bluesky moderation (!hide, !takedown, spam)">hidden by moderation</div>
        <div class="value">${d.hiddenCount.toLocaleString()}</div>
      </div>
    </div>

    <!-- ONE window control for the whole activity section. There used to be
         three independent ones (searches sparkline, latency, traffic series)
         plus a duplicate line chart: "searches / 5 min" and "traffic over time"
         both plotted volume over time, and the donut's total was computed over
         a different window than the searches count sitting next to it. -->
    <div class="activity-wrap">
      <div class="ranged-header">
        <h2>activity</h2>
        <span class="range-btns" id="activity-range">
          <button data-range="24h">24h</button>
          <button data-range="7d" class="active">7d</button>
          <button data-range="30d">30d</button>
          <button data-range="all">all</button>
        </span>
      </div>
      <div class="summary summary-2">
        <div class="metric">
          <div class="label" id="searches-label">total searches (7 days)</div>
          <div class="value" id="searches-value">${initialTotal.toLocaleString()}</div>
        </div>
        <div class="metric">
          <div class="label" tabindex="0" data-tip="cached (edge) vs uncached (fly replica)">avg latency</div>
          <div class="value latency" id="latency-value">${latencyHtml(d.avgCacheLatency, d.avgLatency)}</div>
        </div>
      </div>
      <!-- the series lives INSIDE this card, directly under the control that
           drives it. It used to sit below the donut, several hundred pixels from
           a selector that silently governed it. -->
      <div class="traffic-series">
        <div class="ranged-header">
          <h2 id="tseries-label">traffic over time — all sources</h2>
          <div id="tseries-chips"></div>
        </div>
        <div class="tseries-plot">
          <svg viewBox="0 0 600 135" preserveAspectRatio="none" id="tseries">
            <g id="tseries-lines"></g>
            <line id="tseries-guide" y1="0" y2="135" style="display:none" />
            <g id="tseries-dots"></g>
          </svg>
          <div id="tseries-empty" style="display:none">no traffic in this window</div>
        </div>
        <div class="tseries-axis"><span id="tseries-t0"></span><span id="tseries-t1"></span></div>
        <div class="tseries-totals" id="tseries-totals"></div>
        <div class="tseries-foot">
          <span class="tseries-hint" id="tseries-hint">tap to pin</span>
          <div class="ts-foot-btns">
          <button id="tseries-compare" aria-pressed="false" title="add each tap to the chart instead of replacing">compare</button>
          <div class="ts-menu-wrap">
            <button id="tseries-menu-btn" aria-haspopup="menu" aria-expanded="false" title="chart actions">&hellip;</button>
            <div class="ts-menu" id="tseries-menu" role="menu" hidden>
              <button role="menuitem" data-act="link">copy link to this view</button>
              <button role="menuitem" data-act="copy">copy image</button>
              <button role="menuitem" data-act="png">download png</button>
              <button role="menuitem" data-act="csv">download csv</button>
            </div>
          </div>
          </div>
        </div>
      </div>
    </div>

    <div class="pie-wrap">
      <h2>traffic sources</h2>
      ${d.trafficSources.length > 0
        ? `<canvas id="pie"></canvas>
           <div class="pie-legend">${pieLegendHtml}</div>
           ${d.trafficSources.length > PIE_TOP_N
              ? `<div class="ts-summary">top ${PIE_TOP_N} above · all ${d.trafficSources.length} sources below</div>`
              : ''}
           <div class="ts-table-wrap">
             <table class="ts-table">
               <thead><tr><th>#</th><th>domain</th><th style="text-align:right">hits</th><th style="text-align:right">%</th></tr></thead>
               <tbody>${trafficTableRows}</tbody>
             </table>
           </div>`
        : `<div style="color:#444;font-size:0.8rem;padding:2rem 0;text-align:center">collecting data — check back soon</div>`}
      <div style="margin-top:0.6rem;font-size:0.7rem;color:#555;text-align:center">
        want to show up here? <a href="/docs" style="color:#58a6ff;text-decoration:none">switch from bluesky&rsquo;s typeahead &rarr;</a>
      </div>
    </div>
    <div class="pie-tip" id="pie-tip"></div>

    <footer>
      <a href="/">&larr; home</a>
      · <a href="/docs">docs</a>
      <button class="theme-btn" id="theme-toggle" type="button">theme: auto</button>
    </footer>
  </div>
  <div class="series-tip" id="series-tip"></div>
  <script>
    const COLORS = { total: '#4a9', handles: '#58a6ff', avatars: '#bc8cff' };

    function fmtNum(n) {
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
      return n.toLocaleString();
    }

    /* --- actors indexed trend chart --- */
    const allSnaps = ${snapshotJson};
    let snaps = allSnaps; // the currently-shown window; chart + hover read this
    let trendFitY = false;
    const canvas = document.getElementById('trend');
    const chartTip = document.getElementById('chart-tip');
    const TREND_MS = { '7d': 604800000, '30d': 2592000000 }; // missing key (all) = everything

    function applyTrendRange(range) {
      document.querySelectorAll('#trend-range button').forEach(b => b.classList.toggle('active', b.dataset.range === range));
      const ms = TREND_MS[range];
      const sliced = ms == null ? allSnaps : allSnaps.filter(s => s.ts >= Date.now() - ms);
      snaps = sliced.length >= 2 ? sliced : allSnaps;
      trendFitY = ms != null; // windowed views fit the y-axis so growth is visible
      drawChart(null);
    }

    document.getElementById('trend-range')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-range]');
      if (btn) applyTrendRange(btn.dataset.range);
    });

    function drawChart(hoverIdx) {
      if (!canvas || snaps.length < 2) return;
      const ctx = canvas.getContext('2d');
      const dpr = window.devicePixelRatio || 1;
      const W = canvas.clientWidth, H = canvas.clientHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, W, H);

      const pad = { t: 16, r: 12, b: 24, l: 12 };
      const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
      const n = snaps.length;

      let yMax = 0, yMin = Infinity;
      for (const s of snaps) {
        if (s.total > yMax) yMax = s.total;
        if (s.with_avatars < yMin) yMin = s.with_avatars;
      }
      if (!trendFitY || !isFinite(yMin)) yMin = 0;
      const ySpan = (yMax - yMin) || 1;
      yMax = yMax + ySpan * 0.08;
      yMin = trendFitY ? Math.max(0, yMin - ySpan * 0.08) : 0;

      // x is mapped by ACTUAL TIME, not array index — snapshots are not evenly
      // spaced (irregular hourly gaps + outage gaps), and the date axis below is
      // time-based, so index-based points would desync from the labels/hover.
      const t0ms = snaps[0].ts;
      const tNms = snaps[n - 1].ts;
      const span = (tNms - t0ms) || 1;
      const toX = ts => pad.l + ((ts - t0ms) / span) * cw;
      const toY = v => pad.t + ch - ((v - yMin) / (yMax - yMin)) * ch;

      const series = [
        { key: 'with_avatars', color: COLORS.avatars },
        { key: 'with_handles', color: COLORS.handles },
        { key: 'total', color: COLORS.total },
      ];

      for (const s of series) {
        const pts = snaps.map((d) => [toX(d.ts), toY(d[s.key])]);
        const trace = () => {
          ctx.beginPath();
          ctx.moveTo(pts[0][0], pts[0][1]);
          for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
        };

        // glow
        ctx.save(); trace();
        ctx.shadowColor = s.color; ctx.shadowBlur = 18;
        ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.globalAlpha = 0.07;
        ctx.stroke(); ctx.restore();

        // medium glow
        ctx.save(); trace();
        ctx.shadowColor = s.color; ctx.shadowBlur = 6;
        ctx.strokeStyle = s.color; ctx.lineWidth = 1; ctx.globalAlpha = 0.15;
        ctx.stroke(); ctx.restore();

        // core line
        trace();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 1.2; ctx.globalAlpha = 0.55;
        ctx.stroke(); ctx.globalAlpha = 1;
      }

      // x-axis
      {
        const axisY = H - pad.b;
        ctx.beginPath(); ctx.moveTo(pad.l, axisY); ctx.lineTo(W - pad.r, axisY);
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 1;
        ctx.stroke();

        ctx.font = '10px system-ui, sans-serif';
        ctx.fillStyle = '#555'; ctx.textAlign = 'center';
        const spanDays = span / 86400000;

        // walk midnights
        const d = new Date(t0ms);
        d.setUTCMinutes(0, 0, 0);
        d.setUTCHours(0);
        d.setUTCDate(d.getUTCDate() + 1);
        const minGap = W < 500 ? 65 : 50;
        let prevX = -Infinity;

        while (d.getTime() <= tNms) {
          const frac = (d.getTime() - t0ms) / (tNms - t0ms);
          const x = pad.l + frac * cw;
          if (x >= pad.l && x <= W - pad.r && x - prevX >= minGap) {
            ctx.beginPath(); ctx.moveTo(x, axisY); ctx.lineTo(x, axisY + 4);
            ctx.strokeStyle = 'rgba(255,255,255,0.1)'; ctx.lineWidth = 1; ctx.stroke();
            const label = spanDays > 14
              ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
              : d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
            ctx.globalAlpha = 0.4;
            ctx.fillText(label, x, axisY + 15);
            ctx.globalAlpha = 1;
            prevX = x;
          }
          d.setUTCDate(d.getUTCDate() + 1);
        }
      }

      // hover crosshair + dots
      if (hoverIdx != null && hoverIdx >= 0 && hoverIdx < n) {
        const x = toX(snaps[hoverIdx].ts);
        ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, H - pad.b);
        ctx.strokeStyle = 'rgba(255,255,255,0.1)';
        ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);

        const snap = snaps[hoverIdx];
        for (const s of series) {
          const y = toY(snap[s.key]);
          ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.fillStyle = '#fff'; ctx.globalAlpha = 0.9; ctx.fill();
          ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.globalAlpha = 0.8;
          ctx.stroke(); ctx.globalAlpha = 1;
        }
      }
    }

    function chartHover(clientX, clientY) {
      if (!canvas || snaps.length < 2) return;
      const rect = canvas.getBoundingClientRect();
      const mx = clientX - rect.left;
      const pad = { l: 12, r: 12 };
      const cw = rect.width - pad.l - pad.r;
      const frac = Math.max(0, Math.min(1, (mx - pad.l) / cw));
      // map cursor → time, then pick the snapshot nearest in time (x is
      // time-based, so nearest-by-index would not match the cursor position)
      const t0ms = snaps[0].ts;
      const span = (snaps[snaps.length - 1].ts - t0ms) || 1;
      const targetTs = t0ms + frac * span;
      let idx = 0, best = Infinity;
      for (let i = 0; i < snaps.length; i++) {
        const dd = Math.abs(snaps[i].ts - targetTs);
        if (dd < best) { best = dd; idx = i; }
      }
      drawChart(idx);

      const snap = snaps[idx];
      const t = new Date(snap.ts);
      const time = t.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
      chartTip.innerHTML =
        '<div class="ct-time">' + time + '</div>'
      const tipRows = [
        { color: COLORS.total, value: snap.total, label: 'actors' },
        { color: COLORS.avatars, value: snap.with_avatars, label: 'with avatars' },
        { color: COLORS.handles, value: snap.with_handles, label: 'with handles' },
      ].sort((a, b) => b.value - a.value);
      chartTip.innerHTML = '<div class="ct-time">' + time + '</div>'
        + tipRows.map(r => '<div class="ct-row"><span class="ct-dot" style="background:' + r.color + '"></span> ' + fmtNum(r.value) + ' ' + r.label + '</div>').join('');
      chartTip.style.display = 'block';
      const tx = clientX + 14, ty = clientY - 10;
      chartTip.style.left = (tx + chartTip.offsetWidth > window.innerWidth ? clientX - chartTip.offsetWidth - 10 : tx) + 'px';
      chartTip.style.top = ty + 'px';
    }

    if (canvas) {
      drawChart(null);
      window.addEventListener('resize', () => drawChart(null));

      canvas.addEventListener('mousemove', e => chartHover(e.clientX, e.clientY));
      canvas.addEventListener('mouseleave', () => {
        chartTip.style.display = 'none';
        drawChart(null);
      });
      canvas.addEventListener('touchmove', e => {
        e.preventDefault();
        const t = e.touches[0];
        chartHover(t.clientX, t.clientY);
      });
      canvas.addEventListener('touchend', () => {
        chartTip.style.display = 'none';
        drawChart(null);
      });
    }

    /* --- traffic sources pie chart --- */
    const PIE_COLORS = ${JSON.stringify(PIE_COLORS)};
    const traffic = ${trafficJson};
    const trafficGrandTotal = ${trafficGrandTotal};
    const pieCanvas = document.getElementById('pie');
    const pieTip = document.getElementById('pie-tip');

    /* --- per-app traffic history: click pins a domain, hover the line for detail --- */
    const trafficSeries = ${trafficSeriesJson};     // hourly, 30d: domain -> [[hour, hits], ...]
    const trafficSeries5m = ${trafficSeries5mJson}; // 5-min, 48h: domain -> [[bucket, hits], ...]
    const tseriesLines = document.getElementById('tseries-lines');
    const tseriesDots = document.getElementById('tseries-dots');
    const tseriesGuide = document.getElementById('tseries-guide');
    const tseriesChips = document.getElementById('tseries-chips');
    const tseriesLabel = document.getElementById('tseries-label');
    const tseriesEl = document.getElementById('tseries');
    const tseriesEmpty = document.getElementById('tseries-empty');
    // never declared before, so every trace threw ReferenceError and the tooltip
    // never appeared — an id with a hyphen gets no implicit global
    const seriesTip = document.getElementById('series-tip');
    const tseriesTotals = document.getElementById('tseries-totals');
    const menuBtn = document.getElementById('tseries-menu-btn');
    const compareBtn = document.getElementById('tseries-compare');
    const tseriesHint = document.getElementById('tseries-hint');
    const tsMenu = document.getElementById('tseries-menu');
    const tseriesT0 = document.getElementById('tseries-t0');
    const tseriesT1 = document.getElementById('tseries-t1');
    const TS_W = 600, TS_H = 135, TS_PAD = 6;
    const SVGNS = 'http://www.w3.org/2000/svg';
    let pinned = [];           // click-selected domains, in selection order
    let hovered = null;        // pie-hover preview; only active while nothing is pinned
    let showBaseline = true;   // the dotted all-sources line, for relative scale
    let tsView = null;         // {t0Sec, stepSec, step, toY, series[]} — the trace reads this

    const SPECIAL_SERIES = ${JSON.stringify(SPECIAL_DOMAINS)};
    function domainColor(domain) {
      if (domain == null) return '#4a9';
      if (SPECIAL_SERIES[domain]) return SPECIAL_SERIES[domain];
      const idx = traffic.findIndex(r => r.domain === domain);
      return idx >= 0 ? PIE_COLORS[idx % PIE_COLORS.length] : '#4a9';
    }
    let tsRange = '7d';
    // range picks both the window and the finest data that stays readable
    const TS_RANGES = { '24h': { secs: 86400, stepSec: 300, grain: '5 min' },
                        '7d':  { secs: 604800, stepSec: 3600, grain: 'hourly' },
                        '30d': { secs: 2592000, stepSec: 3600, grain: 'hourly' },
                        // per-domain history is only retained for 30d, so "all"
                        // is the widest series that exists. The metrics beside
                        // it DO go back further, so the label says so instead of
                        // quietly implying they match.
                        'all': { secs: 2592000, stepSec: 3600, grain: 'hourly', capped: true } };
    function seriesFor(domain, stepSec) {
      const source = stepSec === 300 ? trafficSeries5m : trafficSeries;
      if (domain != null) return source[domain] || [];
      const sums = new Map();
      for (const dom in source) {
        for (const [t, hits] of source[dom]) sums.set(t, (sums.get(t) || 0) + hits);
      }
      return [...sums.entries()].sort((a, b) => a[0] - b[0]);
    }
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    function fmtSec(sec, withTime) {
      const t = new Date(sec * 1000);
      const day = MON[t.getUTCMonth()] + ' ' + t.getUTCDate();
      if (!withTime) return day;
      const hh = String(t.getUTCHours()).padStart(2, '0');
      const mm = String(t.getUTCMinutes()).padStart(2, '0');
      return day + ' ' + hh + ':' + mm;
    }
    // What gets drawn, in draw order. With nothing pinned that's just the total.
    // With apps pinned the total becomes a dashed reference line, because an app
    // series read alone has no sense of scale — 40 hits/hr looks identical
    // whether it's most of the traffic or a rounding error. It is dismissable
    // (click the line or its chip) since it also compresses everything under it.
    function activeSpecs() {
      if (pinned.length === 0) {
        return hovered != null
          ? [{ domain: hovered, name: hovered, color: domainColor(hovered) }]
          : [{ domain: null, name: 'all sources', color: domainColor(null) }];
      }
      const specs = pinned.map(d => ({ domain: d, name: d, color: domainColor(d) }));
      if (showBaseline) specs.unshift({ domain: null, name: 'all sources', color: 'var(--dim)', baseline: true });
      return specs;
    }
    function showSeries() {
      if (!tseriesLines) return;
      const cfg = TS_RANGES[tsRange];
      const nowUnit = Math.floor(Date.now() / 1000 / cfg.stepSec);
      const minUnit = nowUnit - cfg.secs / cfg.stepSec;
      const specs = activeSpecs();
      const raws = specs.map(s => seriesFor(s.domain, cfg.stepSec).filter(([t]) => t >= minUnit));
      // Deliberately short: what is plotted is named by the chips, and the true
      // span is printed on the axis below. This used to append the domain, "all
      // sources", and "· last 30d" — enough text to overflow the header on a
      // phone to say what two other elements already said.
      if (tseriesLabel) tseriesLabel.textContent = 'traffic over time · ' + cfg.grain;
      renderChips();
      const nonEmpty = raws.filter(r => r.length > 0);
      const empty = nonEmpty.length === 0 || Math.max(...raws.map(r => r.length)) < 2;
      if (tseriesEl) tseriesEl.style.display = empty ? 'none' : 'block';
      if (tseriesEmpty) tseriesEmpty.style.display = empty ? 'flex' : 'none';
      tseriesLines.replaceChildren();
      if (empty) { tsView = null; renderTotals(); if (tseriesT0) tseriesT0.textContent = ''; if (tseriesT1) tseriesT1.textContent = ''; return; }
      // ONE grid for every line — a shared t0/tN and a shared max. Per-series
      // extents would put two lines on silently different axes, which is worse
      // than not drawing the comparison at all.
      const t0 = Math.min(...nonEmpty.map(r => r[0][0]));
      const tN = Math.max(...nonEmpty.map(r => r[r.length - 1][0]));
      const nPts = Math.min(tN - t0 + 1, 800);
      const series = specs.map((s, i) => {
        const byUnit = new Map(raws[i]);
        const counts = [];
        // fill gaps with 0 so quiet periods read as quiet, not skipped
        for (let k = 0; k < nPts; k++) counts.push(byUnit.get(t0 + k) || 0);
        return { ...s, counts };
      });
      const max = Math.max(1, ...series.map(s => Math.max(...s.counts)));
      const step = nPts > 1 ? TS_W / (nPts - 1) : 0;
      const toY = c => TS_PAD + (1 - c / max) * (TS_H - 2 * TS_PAD);
      for (const s of series) {
        const pts = s.counts.map((c, i) => (i * step).toFixed(1) + ',' + toY(c).toFixed(1)).join(' ');
        if (s.baseline) {
          const hit = document.createElementNS(SVGNS, 'polyline');
          hit.setAttribute('points', pts);
          hit.setAttribute('class', 'hit');
          hit.addEventListener('click', e => { e.stopPropagation(); showBaseline = false; showSeries(); syncUrl(); });
          tseriesLines.appendChild(hit);
        }
        const line = document.createElementNS(SVGNS, 'polyline');
        line.setAttribute('points', pts);
        if (s.baseline) line.setAttribute('class', 'baseline');
        line.style.stroke = s.color;
        line.style.pointerEvents = 'none';
        tseriesLines.appendChild(line);
      }
      const showClock = cfg.secs <= 604800;
      if (tseriesT0) tseriesT0.textContent = fmtSec(t0 * cfg.stepSec, showClock);
      if (tseriesT1) tseriesT1.textContent = fmtSec((tN + 1) * cfg.stepSec, showClock);
      tsView = { t0Sec: t0 * cfg.stepSec, stepSec: cfg.stepSec, step, toY, series };
      renderTotals();
    }
    function chip(name, color, dashed, onClear) {
      const b = document.createElement('button');
      b.className = 'ts-chip';
      b.title = 'remove from chart';
      const dot = document.createElement('span');
      dot.className = 'ldot' + (dashed ? ' dash' : '');
      if (!dashed) dot.style.background = color;
      const label = document.createElement('span');
      label.textContent = name;              // domains are caller-controlled — never innerHTML
      b.append(dot, label);
      if (onClear) {
        const x = document.createElement('span');
        x.className = 'x';
        x.textContent = '×';
        b.appendChild(x);
        b.addEventListener('click', onClear);
      }
      return b;
    }
    function renderChips() {
      if (!tseriesChips) return;
      tseriesChips.replaceChildren();
      if (pinned.length === 0) {
        // hover preview: name the line without pretending it's a filter
        if (hovered != null) {
          const c = chip(hovered, domainColor(hovered), false, null);
          c.classList.add('preview');
          tseriesChips.appendChild(c);
        }
        return;
      }
      if (showBaseline) {
        tseriesChips.appendChild(chip('all sources', null, true, () => { showBaseline = false; showSeries(); syncUrl(); }));
      }
      for (const d of pinned) {
        tseriesChips.appendChild(chip(d, domainColor(d), false, () => applySelection(d, true)));
      }
    }
    // The area under each curve. Buckets are counts per fixed-width interval, so
    // the integral over the window is just their sum — no trapezoid needed, and
    // pretending otherwise would invent precision the data doesn't have.
    function renderTotals() {
      if (!tseriesTotals) return;
      tseriesTotals.replaceChildren();
      if (!tsView || pinned.length === 0) return;
      const t0u = tsView.t0Sec / tsView.stepSec, n = tsView.series[0].counts.length;
      // measured over the same window even when the baseline line is hidden,
      // so dismissing it doesn't silently change what the percentages mean
      const all = seriesFor(null, tsView.stepSec)
        .filter(([t]) => t >= t0u && t < t0u + n).reduce((s, r) => s + r[1], 0);
      const apps = tsView.series.filter(s => !s.baseline)
        .map(s => ({ name: s.name, color: s.color, sum: s.counts.reduce((a, b) => a + b, 0) }))
        .sort((a, b) => b.sum - a.sum);
      const sig = document.createElement('span');
      sig.className = 'sig';
      sig.tabIndex = 0;
      sig.textContent = '∫';
      sig.dataset.tip = 'area under each curve — total hits over the window shown';
      tseriesTotals.appendChild(sig);
      for (const a of apps) {
        const el = document.createElement('span');
        el.className = 'tot';
        const dot = document.createElement('span');
        dot.className = 'ldot';
        dot.style.background = a.color;
        const name = document.createElement('span');
        name.textContent = a.name;
        const num = document.createElement('b');
        num.textContent = a.sum.toLocaleString();
        el.append(dot, name, num);
        if (all > 0) {
          const pct = document.createElement('span');
          pct.className = 'pct';
          pct.textContent = '(' + (a.sum / all * 100).toFixed(1) + '%)';
          el.appendChild(pct);
        }
        tseriesTotals.appendChild(el);
      }
      if (apps.length >= 2 && apps[0].sum > 0) {
        const lead = document.createElement('span');
        lead.className = 'lead';
        const r = apps[1].sum > 0 ? apps[0].sum / apps[1].sum : Infinity;
        lead.textContent = apps[1].sum === 0
          ? apps[0].name + ' only'
          : apps[0].name + ' leads ' + (r < 10 ? r.toFixed(2) : Math.round(r)) + '×';
        tseriesTotals.appendChild(lead);
      }
    }

    function cssColor(v) {
      const m = /^var\\((--[\\w-]+)\\)$/.exec(v);
      return m ? getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() : v;
    }
    // Drawn from tsView rather than rasterising the live SVG: the SVG takes its
    // dash pattern, widths and colours from page CSS, none of which survives
    // serialisation, and a shared image wants a title and legend anyway.
    function renderCard() {
      if (!tsView) return null;
      const css = getComputedStyle(document.documentElement);
      const v = n => css.getPropertyValue(n).trim();
      const font = getComputedStyle(document.body).fontFamily;
      const apps = tsView.series;
      const W = 680, PAD = 24, PLOT_T = 82, PLOT_H = 150;
      const rank = apps.filter(a => !a.baseline)
        .map(a => ({ name: a.name, sum: a.counts.reduce((x, c) => x + c, 0) }))
        .sort((a, b) => b.sum - a.sum);
      const leadLine = rank.length >= 2 && rank[0].sum > 0 && rank[1].sum > 0
        ? rank[0].name + ' leads ' + (r => r < 10 ? r.toFixed(2) : Math.round(r))(rank[0].sum / rank[1].sum) + '×'
        : null;
      const H = PLOT_T + PLOT_H + 34 + apps.length * 20 + (leadLine ? 22 : 0) + 16;
      const s = 2;
      const cv = document.createElement('canvas');
      cv.width = W * s; cv.height = H * s;
      const g = cv.getContext('2d');
      g.scale(s, s);
      g.fillStyle = v('--panel') || v('--bg');
      g.fillRect(0, 0, W, H);
      g.font = '15px ' + font;
      g.fillStyle = v('--text');
      g.fillText('typeahead · traffic over time', PAD, 36);
      g.font = '11px ' + font;
      g.fillStyle = v('--dim');
      g.fillText(tseriesLabel.textContent.replace('traffic over time · ', '')
        + ' · ' + tseriesT0.textContent + ' → ' + tseriesT1.textContent + ' UTC', PAD, 56);
      const n = apps[0].counts.length;
      const max = Math.max(1, ...apps.map(a => Math.max(...a.counts)));
      const plotW = W - PAD * 2, step = n > 1 ? plotW / (n - 1) : 0;
      const y = c => PLOT_T + (1 - c / max) * PLOT_H;
      g.strokeStyle = v('--line');
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(PAD, y(0)); g.lineTo(W - PAD, y(0)); g.stroke();
      for (const a of apps) {
        g.beginPath();
        a.counts.forEach((c, i) => i ? g.lineTo(PAD + i * step, y(c)) : g.moveTo(PAD, y(c)));
        g.strokeStyle = cssColor(a.color);
        g.lineWidth = a.baseline ? 1 : 1.5;
        g.setLineDash(a.baseline ? [3, 3] : []);
        g.stroke();
      }
      g.setLineDash([]);
      let ly = PLOT_T + PLOT_H + 30;
      g.font = '11px ' + font;
      for (const a of apps) {
        const sum = a.counts.reduce((x, c) => x + c, 0);
        g.beginPath(); g.arc(PAD + 4, ly - 4, 4, 0, Math.PI * 2);
        if (a.baseline) {           // hollow + dashed, same as the line it labels
          g.strokeStyle = cssColor(a.color); g.lineWidth = 1.5; g.setLineDash([2, 2]); g.stroke(); g.setLineDash([]);
        } else { g.fillStyle = cssColor(a.color); g.fill(); }
        g.fillStyle = v('--muted');
        g.fillText(a.name, PAD + 16, ly);
        g.fillStyle = v('--text');
        g.fillText(sum.toLocaleString() + ' hits', PAD + 220, ly);
        ly += 20;
      }
      if (leadLine) {
        g.fillStyle = v('--dim');
        g.fillText('∫ ' + leadLine, PAD, ly + 6);
      }
      return cv;
    }
    function cardBlob() {
      const cv = renderCard();
      return cv && new Promise(res => cv.toBlob(res, 'image/png'));
    }
    function download(blob, name) {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click();
      // revoking in the same tick has historically raced the download start
      setTimeout(() => URL.revokeObjectURL(url), 10_000);
    }
    function fileName(ext) { return 'typeahead-traffic-' + tsRange + '.' + ext; }
    async function copyImage() {
      const blob = await cardBlob();
      if (!blob) return;
      // clipboard image write needs a secure context and isn't in every browser;
      // fall back to a download rather than failing silently
      try {
        await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
        flashMenu('copied');
      } catch { download(blob, fileName('png')); }
    }
    function flashMenu(msg) {
      if (!menuBtn) return;
      const prev = menuBtn.textContent;
      menuBtn.textContent = msg;
      setTimeout(() => { menuBtn.textContent = prev; }, 1200);
    }
    // Exports exactly what is plotted — same window, same grain, same series,
    // including the dashed total when it's shown. A CSV that quietly differed
    // from the chart above it would be worse than no export at all.
    function exportCsv() {
      if (!tsView) return;
      const q = v => /[",\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
      const lines = [['time_utc', ...tsView.series.map(s => q(s.name))].join(',')];
      for (let i = 0; i < tsView.series[0].counts.length; i++) {
        const t = new Date((tsView.t0Sec + i * tsView.stepSec) * 1000).toISOString().replace('.000Z', 'Z');
        lines.push([t, ...tsView.series.map(s => s.counts[i])].join(','));
      }
      download(new Blob([lines.join('\\n') + '\\n'], { type: 'text/csv' }), fileName('csv'));
    }
    const ACTIONS = {
      link: async () => {
        try { await navigator.clipboard.writeText(location.href); flashMenu('copied'); }
        catch { flashMenu('copy failed'); }
      },
      copy: copyImage,
      png: async () => { const b = await cardBlob(); if (b) download(b, fileName('png')); },
      csv: exportCsv,
    };
    function setMenu(open) {
      if (!tsMenu) return;
      tsMenu.hidden = !open;
      menuBtn.setAttribute('aria-expanded', String(open));
    }
    menuBtn?.addEventListener('click', e => { e.stopPropagation(); setMenu(tsMenu.hidden); });
    tsMenu?.addEventListener('click', e => {
      const act = e.target.dataset?.act;
      if (!act) return;
      setMenu(false);
      ACTIONS[act]();
    });
    document.addEventListener('click', () => setMenu(false));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') setMenu(false); });
    /* --- the view is addressable: ?apps=a,b&range=7d&total=0 ---
       Query params, not path segments: this is a filter over one page, not a
       different resource, and /stats/:app would have to invent an encoding for
       "two apps, 30 days, no baseline". replaceState rather than pushState so a
       shared link is always the current view without burying the previous page
       under one history entry per click. */
    // Snapshot the incoming query BEFORE anything writes to it: initialising the
    // range calls syncUrl(), which with nothing pinned yet would delete ?apps=
    // out of the URL a moment before restore tried to read it.
    const BOOT = new URLSearchParams(location.search);
    window.__boot = BOOT;
    function syncUrl() {
      const p = new URLSearchParams(location.search);
      if (pinned.length) p.set('apps', pinned.join(',')); else p.delete('apps');
      if (tsRange === '7d') p.delete('range'); else p.set('range', tsRange);
      if (pinned.length && !showBaseline) p.set('total', '0'); else p.delete('total');
      const q = p.toString();
      history.replaceState(null, '', location.pathname + (q ? '?' + q : '') + location.hash);
    }
    // Only domains we actually hold a series for. A link naming a domain that
    // has since aged out of traffic_buckets would otherwise render a chip
    // pointing at an empty line.
    const KNOWN = new Set([...Object.keys(trafficSeries), ...Object.keys(trafficSeries5m)]);
    window.__restoreView = () => {
      const p = BOOT;
      const apps = (p.get('apps') || '').split(',').map(s => s.trim()).filter(s => KNOWN.has(s));
      if (!apps.length) { syncUrl(); return; }
      pinned = [...new Set(apps)];
      showBaseline = p.get('total') !== '0';
      document.querySelectorAll('.pie-legend span[data-domain], .ts-table tbody tr[data-domain]').forEach(el =>
        el.classList.toggle('sel', pinned.includes(el.dataset.domain)));
      showSeries();
      syncUrl();
    };
    // driven by the shared #activity-range, not a selector of its own
    window.__setTsRange = (range) => {
      if (!TS_RANGES[range]) return;
      tsRange = range;
      showSeries();
      syncUrl();
    };
    /* Plain click REPLACES the selection; shift/meta/ctrl-click toggles one in
       or out. Making plain click additive is how these controls end up
       accumulating selections nobody asked for.

       Touch has no modifier key, so "compare" is a sticky shift: while it is on,
       every tap toggles. It stays on until turned off — a one-shot would make
       building a three-app comparison three trips to the button. */
    let compareMode = false;
    function additive(e) {
      return compareMode || e.shiftKey || e.metaKey || e.ctrlKey;
    }
    function setCompare(on) {
      compareMode = on;
      compareBtn?.setAttribute('aria-pressed', String(on));
      if (tseriesHint) tseriesHint.textContent = on ? 'tap to add or remove' : 'tap to pin';
    }
    compareBtn?.addEventListener('click', () => setCompare(!compareMode));
    function applySelection(domain, additive) {
      if (domain == null) pinned = [];
      else if (!additive) pinned = pinned.length === 1 && pinned[0] === domain ? [] : [domain];
      else if (pinned.includes(domain)) pinned = pinned.filter(d => d !== domain);
      else pinned = [...pinned, domain];
      if (pinned.length === 0) showBaseline = true;  // fresh start next time
      document.querySelectorAll('.pie-legend span[data-domain], .ts-table tbody tr[data-domain]').forEach(el =>
        el.classList.toggle('sel', pinned.includes(el.dataset.domain)));
      showSeries();
      syncUrl();
    }
    document.querySelectorAll('.pie-legend span[data-domain], .ts-table tbody tr[data-domain]').forEach(el => {
      el.addEventListener('click', e => {
        if (e.target.closest('a')) return; // domain links still navigate
        applySelection(el.dataset.domain, additive(e));
      });
    });

    // trace the line: crosshair dot + timestamp/value tooltip (mouse or finger)
    function tseriesTrace(clientX, clientY) {
      if (!tsView) return;
      const rect = tseriesEl.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const nPts = tsView.series[0].counts.length;
      const idx = Math.round(frac * (nPts - 1));
      // one dot per line, one row per line — reading a value off the wrong
      // series is the obvious way a multi-line chart misleads
      tseriesDots.replaceChildren();
      const rows = [];
      for (const s of tsView.series) {
        const c = s.counts[idx];
        const dot = document.createElementNS(SVGNS, 'circle');
        dot.setAttribute('r', '2.5');
        dot.setAttribute('cx', (idx * tsView.step).toFixed(1));
        dot.setAttribute('cy', tsView.toY(c).toFixed(1));
        dot.style.stroke = s.color;
        tseriesDots.appendChild(dot);
        rows.push({ name: s.name, color: s.color, c, baseline: s.baseline });
      }
      if (tseriesGuide) {
        tseriesGuide.setAttribute('x1', (idx * tsView.step).toFixed(1));
        tseriesGuide.setAttribute('x2', (idx * tsView.step).toFixed(1));
        tseriesGuide.style.display = 'block';
      }
      seriesTip.replaceChildren();
      const when = document.createElement('div');
      when.className = 'ct-time';
      when.textContent = fmtSec(tsView.t0Sec + idx * tsView.stepSec, true) + ' UTC';
      seriesTip.appendChild(when);
      for (const r of rows) {
        const row = document.createElement('div');
        row.className = 'ct-row';
        const dot = document.createElement('span');
        dot.className = 'ct-dot';
        dot.style.background = r.color;
        if (r.baseline) { dot.style.background = 'none'; dot.style.border = '1.5px dashed var(--dim)'; }
        const txt = document.createElement('span');
        txt.textContent = (rows.length > 1 ? r.name + ' — ' : '') + r.c + (r.c === 1 ? ' hit' : ' hits');
        row.append(dot, txt);
        seriesTip.appendChild(row);
      }
      seriesTip.style.display = 'block';
      // keep the tip above the finger/cursor and inside the viewport
      const tx = Math.min(clientX + 12, window.innerWidth - seriesTip.offsetWidth - 8);
      seriesTip.style.left = Math.max(8, tx) + 'px';
      seriesTip.style.top = (clientY - 40) + 'px';
    }
    function tseriesTraceEnd() {
      seriesTip.style.display = 'none';
      if (tseriesDots) tseriesDots.replaceChildren();
      if (tseriesGuide) tseriesGuide.style.display = 'none';
    }
    if (tseriesEl) {
      tseriesEl.addEventListener('mousemove', e => tseriesTrace(e.clientX, e.clientY));
      tseriesEl.addEventListener('mouseleave', tseriesTraceEnd);
      tseriesEl.addEventListener('touchstart', e => { const t = e.touches[0]; tseriesTrace(t.clientX, t.clientY); }, { passive: true });
      tseriesEl.addEventListener('touchmove', e => { const t = e.touches[0]; tseriesTrace(t.clientX, t.clientY); }, { passive: true });
      tseriesEl.addEventListener('touchend', tseriesTraceEnd);
    }
    showSeries();

    if (pieCanvas && traffic.length > 0) {
      const pieTotal = traffic.reduce((s, r) => s + r.hits, 0);

      const SPECIAL = ${JSON.stringify(SPECIAL_DOMAINS)};
      function getColor(i) {
        return SPECIAL[traffic[i].domain] || PIE_COLORS[i % PIE_COLORS.length];
      }

      // build segments: { start, end, color, domain, hits }
      const segments = [];
      let angle = -Math.PI / 2;
      for (let i = 0; i < traffic.length; i++) {
        const sweep = (traffic[i].hits / pieTotal) * Math.PI * 2;
        segments.push({ start: angle, end: angle + sweep, color: getColor(i), domain: traffic[i].domain, hits: traffic[i].hits });
        angle += sweep;
      }

      let animProgress = 0;
      let hoverIdx = -1;
      const animStart = performance.now();
      const animDur = 800;

      function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

      function drawPie() {
        const pctx = pieCanvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const W = pieCanvas.clientWidth, H = pieCanvas.clientHeight;
        pieCanvas.width = W * dpr; pieCanvas.height = H * dpr;
        pctx.scale(dpr, dpr);
        pctx.clearRect(0, 0, W, H);

        const cx = W / 2, cy = H / 2;
        const outerR = Math.min(W, H) / 2 - 8;
        const innerR = outerR * 0.6;
        const maxAngle = -Math.PI / 2 + animProgress * Math.PI * 2;

        for (let i = 0; i < segments.length; i++) {
          const seg = segments[i];
          const drawStart = seg.start;
          const drawEnd = Math.min(seg.end, maxAngle);
          if (drawEnd <= drawStart) continue;

          const grow = hoverIdx === i ? 4 : 0;
          const alpha = hoverIdx >= 0 && hoverIdx !== i ? 0.4 : 1;

          pctx.globalAlpha = alpha;
          pctx.beginPath();
          pctx.arc(cx, cy, outerR + grow, drawStart, drawEnd);
          pctx.arc(cx, cy, innerR - (grow ? 2 : 0), drawEnd, drawStart, true);
          pctx.closePath();
          pctx.fillStyle = seg.color;
          pctx.fill();
        }

        // center text
        pctx.globalAlpha = 1;
        pctx.fillStyle = '#888';
        pctx.font = '600 ' + (outerR * 0.22) + 'px system-ui, sans-serif';
        pctx.textAlign = 'center';
        pctx.textBaseline = 'middle';
        pctx.fillText(fmtNum(pieTotal), cx, cy - outerR * 0.06);
        pctx.font = (outerR * 0.11) + 'px system-ui, sans-serif';
        pctx.fillStyle = '#555';
        pctx.fillText('hits', cx, cy + outerR * 0.14);
      }

      function animLoop(now) {
        const t = Math.min((now - animStart) / animDur, 1);
        animProgress = easeOutCubic(t);
        drawPie();
        if (t < 1) requestAnimationFrame(animLoop);
      }
      requestAnimationFrame(animLoop);

      function hitTest(clientX, clientY) {
        const rect = pieCanvas.getBoundingClientRect();
        const mx = clientX - rect.left - rect.width / 2;
        const my = clientY - rect.top - rect.height / 2;
        const dist = Math.sqrt(mx * mx + my * my);
        const outerR = Math.min(rect.width, rect.height) / 2 - 8;
        const innerR = outerR * 0.6;
        if (dist < innerR || dist > outerR + 8) return -1;
        let a = Math.atan2(my, mx);
        if (a < -Math.PI / 2) a += Math.PI * 2;
        for (let i = 0; i < segments.length; i++) {
          if (a >= segments[i].start && a < segments[i].end) return i;
        }
        return -1;
      }

      function pieHover(clientX, clientY) {
        const idx = hitTest(clientX, clientY);
        if (idx !== hoverIdx) {
          hoverIdx = idx;
          drawPie();
          // hover previews the history only while nothing is pinned
          if (pinned.length === 0) { hovered = idx >= 0 ? segments[idx].domain : null; showSeries(); }
        }
        if (idx >= 0) {
          const seg = segments[idx];
          const denom = trafficGrandTotal > 0 ? trafficGrandTotal : pieTotal;
          const pct = ((seg.hits / denom) * 100).toFixed(1);
          pieTip.innerHTML = '<strong>' + seg.domain + '</strong><br>' + seg.hits.toLocaleString() + (seg.hits === 1 ? ' hit' : ' hits') + ' (' + pct + '%)';
          pieTip.style.display = 'block';
          const tx = clientX + 14, ty = clientY - 10;
          pieTip.style.left = (tx + pieTip.offsetWidth > window.innerWidth ? clientX - pieTip.offsetWidth - 10 : tx) + 'px';
          pieTip.style.top = ty + 'px';
        } else {
          pieTip.style.display = 'none';
        }
      }

      pieCanvas.addEventListener('mousemove', e => pieHover(e.clientX, e.clientY));
      pieCanvas.addEventListener('mouseleave', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; if (pinned.length === 0) { hovered = null; showSeries(); } });
      pieCanvas.addEventListener('touchmove', e => { e.preventDefault(); const t = e.touches[0]; pieHover(t.clientX, t.clientY); });
      pieCanvas.addEventListener('touchend', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; if (pinned.length === 0) { hovered = null; showSeries(); } });
      pieCanvas.addEventListener('click', e => {
        const idx = hitTest(e.clientX, e.clientY);
        if (idx < 0) return;
        const dom = segments[idx].domain;
        hovered = null;
        applySelection(dom, additive(e));
      });
      window.addEventListener('resize', () => drawPie());
    }

    /* --- activity: one range selector drives searches, latency, and the series --- */
    const ACTIVITY = ${activityJson}; // per-range totals, aggregated server-side
    // window by TIME (bucket is 5-min; hour*300 = unix seconds), not bucket
    // count — buckets are sparse so count-based slicing made 30d == all.
    const RANGE_LABEL = { '24h': '24 hours', '7d': '7 days', '30d': '30 days', 'all': 'all time' };
    // long windows aggregate 5-min buckets so the line stays readable
    const AGG_SECS = { '30d': 3600, 'all': 21600 };
    const AGG_LABEL = { 300: '5 min', 3600: 'hour', 21600: '6 h' };

    const searchesValue = document.getElementById('searches-value');
    const searchesLabel = document.getElementById('searches-label');
    const latencyValue = document.getElementById('latency-value');

    /* ONE handler for the whole activity section. Previously total searches,
       avg latency, and the traffic chart each had their own selector, so the
       page could show 24h latency next to a 7d search count next to a 30d
       chart — three windows, no indication they differed. */
    function applyActivityRange(range) {
      document.querySelectorAll('#activity-range button')
        .forEach(b => b.classList.toggle('active', b.dataset.range === range));
      const label = RANGE_LABEL[range] || range;
      const { searches, cacheHits, totalMs, cacheMs } = ACTIVITY[range] || ACTIVITY['all'];
      if (searchesValue) searchesValue.textContent = (searches + cacheHits).toLocaleString();
      if (searchesLabel) searchesLabel.textContent = 'total searches (' + label + ')';

      if (latencyValue) {
        const cold = searches > 0 ? totalMs / searches : 0;
        const cached = cacheHits > 0 ? cacheMs / cacheHits : 0;
        const pair = (n, t) => '<span class="lat-pair"><span class="lat-n">' + n + '</span><span class="lat-t">' + t + '</span></span>';
        const parts = [];
        if (cached > 0) parts.push(pair(cached.toFixed(1) + ' ms', 'cached'));
        parts.push(pair(cold.toFixed(0) + ' ms', 'cold'));
        latencyValue.innerHTML = parts.join('<span class="lat-sep">·</span>');
      }

      window.__setTsRange?.(range);
    }

    document.getElementById('activity-range')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-range]');
      if (btn) applyActivityRange(btn.dataset.range);
    });

    // default 7d: SSR renders the 7d view; this makes the JS slice the single
    // source of truth so server and client always agree. ?range= overrides it —
    // validated against the buttons that exist, so a hand-edited link falls back
    // to the default instead of rendering an undefined window.
    const wantRange = window.__boot?.get('range');
    const hasRange = !!document.querySelector('#activity-range button[data-range="' + CSS.escape(wantRange || '') + '"]');
    applyActivityRange(hasRange ? wantRange : '7d');
    // after the range, so restoring apps plots against the right window
    window.__restoreView?.();
    ${THEME_SCRIPT}
  </script>
</body>
</html>`;
}
