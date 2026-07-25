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

export function statsPage(d: StatsData): string {
  // The total-searches metric AND the sparkline both default to the 7d window
  // and share one range selector (7d/30d/all). The JS re-slices the same data
  // on click; here we render the 7d view so the page is correct without JS.
  const sorted = [...d.rows].reverse(); // oldest first
  const sw = 600, sh = 80;
  // Window by TIME, not bucket count: metric buckets are 5-min (hour*300 = unix
  // seconds) and SPARSE (only buckets with a search exist, ~64/day not 288), so
  // "last N buckets" ≠ "last N days". The old count-based slice made 30d == all
  // whenever total buckets < the 30d count, and made 7d span ~31 days.
  const nowSec = Math.floor(Date.now() / 1000);
  const spark7 = sorted.filter((r) => r.hour * 300 >= nowSec - 7 * 86400);
  const counts = spark7.map((r) => r.searches);
  const sparkMax = Math.max(...counts, 1);
  const step = counts.length > 1 ? sw / (counts.length - 1) : 0;
  const sparkPoints = counts
    .map((c, i) => `${(i * step).toFixed(1)},${(sh - (c / sparkMax) * sh).toFixed(1)}`)
    .join(" ");
  // initial total matches the default (7d) window, so the rendered value agrees
  // with the active button on load (previously this showed the all-time sum
  // under a "7d"-active button).
  const initialTotal = spark7.reduce((s, r) => s + r.searches + (r.cache_hits ?? 0), 0);
  // full data (all buckets, oldest first) for the client range selector +
  // sparkline redraw. cache_hits included so "total" matches the SSR formula.
  const sparkJson = JSON.stringify(
    sorted.map((r) => ({
      hour: r.hour,
      time: new Date(r.hour * 300_000).toISOString().slice(0, 16) + "Z",
      searches: r.searches,
    }))
  );
  const metricsJson = JSON.stringify(
    sorted.map((r) => ({
      hour: r.hour,
      searches: r.searches,
      cache_hits: r.cache_hits ?? 0,
      total_ms: r.total_ms ?? 0,
      cache_ms: r.cache_ms ?? 0,
    }))
  );

  const snapshotJson = JSON.stringify(d.snapshots);

  const PIE_COLORS = ['#4a9', '#58a6ff', '#bc8cff', '#e5a04b', '#e06c75', '#56b6c2', '#c678dd', '#98c379', '#d19a66'];
  const SPECIAL_DOMAINS: Record<string, string> = { unknown: '#555', homepage: '#666' };
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

    .chart-wrap, .sparkline-wrap, .pie-wrap {
      background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
      padding: 1rem; margin-bottom: 1.25rem; position: relative; }
    .chart-wrap h2, .sparkline-wrap h2, .pie-wrap h2 {
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
    .chart-tip, .pie-tip, .spark-tip {
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
    .summary-2 { grid-template-columns: repeat(2, 1fr); }
    .summary-3 { grid-template-columns: repeat(3, 1fr); }
    .metric { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 0.8rem 0.9rem; }
    .metric .metric-head { display: flex; justify-content: space-between; align-items: center;
                           flex-wrap: wrap; gap: 0.4rem 0.5rem; margin-bottom: 0.2rem; }
    .metric .metric-head .label { margin-bottom: 0; }
    .metric .label { font-size: 0.7rem; color: var(--muted); margin-bottom: 0.2rem; }
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
    #tseries { cursor: crosshair; touch-action: pan-y; }
    #tseries polyline { transition: stroke 0.15s; vector-effect: non-scaling-stroke; }
    #tseries-dot { fill: var(--bright); stroke: var(--accent); stroke-width: 1.5; }
    #tseries-clear { background: var(--panel-2); border: 1px solid var(--line-2); color: var(--muted);
                     padding: 0.35rem 0.65rem; font-size: 0.72rem; cursor: pointer;
                     border-radius: 6px; font-family: inherit; min-height: 34px; }
    #tseries-clear:hover { color: var(--text); }
    .tseries-axis { display: flex; justify-content: space-between; font-size: 0.65rem;
                    color: var(--dim); margin-top: 0.25rem; font-variant-numeric: tabular-nums; }
    .tseries-hint { font-size: 0.65rem; color: var(--faint); margin-top: 0.5rem; text-align: center; }
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
      #spark, #tseries { height: 72px; }
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

    <div class="sparkline-wrap">
      <div class="ranged-header">
        <h2 id="spark-label">searches / 5 min</h2>
        <span class="range-btns" id="searches-range">
          <button data-range="24h">24h</button>
          <button data-range="7d" class="active">7d</button>
          <button data-range="30d">30d</button>
          <button data-range="all">all</button>
        </span>
      </div>
      ${counts.length > 1
        ? `<svg viewBox="0 0 ${sw} ${sh}" preserveAspectRatio="none" id="spark">
            <polyline points="${sparkPoints}" id="spark-poly" />
          </svg>`
        : `<div style="color:#444;font-size:0.8rem;padding:1rem 0;text-align:center">no data yet</div>`}
    </div>
    <div class="summary summary-2">
      <div class="metric">
        <div class="label" id="searches-label">total searches (7 days)</div>
        <div class="value" id="searches-value">${initialTotal.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="metric-head">
          <div class="label" tabindex="0" data-tip="cached (edge) vs uncached (fly replica)">avg latency</div>
          <span class="range-btns" id="latency-range">
            <button data-range="24h" class="active">24h</button>
            <button data-range="7d">7d</button>
            <button data-range="30d">30d</button>
          </span>
        </div>
        <div class="value" style="font-size:1.3rem" id="latency-value">${d.avgCacheLatency > 0 ? `${d.avgCacheLatency.toFixed(1)} ms <span style="opacity:.4;font-size:.85rem">cached</span> · ` : ''}${d.avgLatency.toFixed(0)} ms${d.avgCacheLatency > 0 ? ' <span style="opacity:.4;font-size:.85rem">cold</span>' : ''}</div>
      </div>
    </div>

    <div class="pie-wrap">
      <h2>traffic sources</h2>
      ${d.trafficSources.length > 0
        ? `<canvas id="pie"></canvas>
           <div class="pie-legend">${pieLegendHtml}</div>
           <div class="traffic-series">
             <div class="ranged-header">
               <h2 id="tseries-label">traffic over time — all sources</h2>
               <span style="display:inline-flex;gap:0.35rem;align-items:center">
                 <button id="tseries-clear" style="display:none">&times; all sources</button>
                 <span class="range-btns" id="tseries-range">
                   <button data-range="24h">24h</button>
                   <button data-range="7d" class="active">7d</button>
                   <button data-range="30d">30d</button>
                 </span>
               </span>
             </div>
             <svg viewBox="0 0 600 90" preserveAspectRatio="none" id="tseries">
               <polyline points="" id="tseries-poly" />
               <circle id="tseries-dot" r="2.5" style="display:none" />
             </svg>
             <div class="tseries-axis"><span id="tseries-t0"></span><span id="tseries-t1"></span></div>
             <div class="tseries-hint">tap a slice, legend entry, or table row to pin an app &middot; trace the line for details</div>
             <div id="tseries-empty" style="display:none;color:#444;font-size:0.75rem;padding:0.8rem 0;text-align:center">collecting data &mdash; hourly per-app history began 2026-07-24</div>
           </div>
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
  <div class="spark-tip" id="spark-tip"></div>
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
    const PIE_COLORS = ['#4a9', '#58a6ff', '#bc8cff', '#e5a04b', '#e06c75', '#56b6c2', '#c678dd', '#98c379', '#d19a66'];
    const traffic = ${trafficJson};
    const trafficGrandTotal = ${trafficGrandTotal};
    const pieCanvas = document.getElementById('pie');
    const pieTip = document.getElementById('pie-tip');

    /* --- per-app traffic history: click pins a domain, hover the line for detail --- */
    const trafficSeries = ${trafficSeriesJson};     // hourly, 30d: domain -> [[hour, hits], ...]
    const trafficSeries5m = ${trafficSeries5mJson}; // 5-min, 48h: domain -> [[bucket, hits], ...]
    const tseriesPoly = document.getElementById('tseries-poly');
    const tseriesDot = document.getElementById('tseries-dot');
    const tseriesLabel = document.getElementById('tseries-label');
    const tseriesEl = document.getElementById('tseries');
    const tseriesEmpty = document.getElementById('tseries-empty');
    const tseriesClear = document.getElementById('tseries-clear');
    const tseriesT0 = document.getElementById('tseries-t0');
    const tseriesT1 = document.getElementById('tseries-t1');
    const TS_W = 600, TS_H = 90, TS_PAD = 5;
    let pinnedDomain = null;   // click-selected; survives pie mouse-out
    let tsView = null;         // {h0, counts, color} — the line hover reads this

    const SPECIAL_SERIES = { unknown: '#555', homepage: '#666' };
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
                        '30d': { secs: 2592000, stepSec: 3600, grain: 'hourly' } };
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
    function showSeries(domain) {
      if (!tseriesPoly) return;
      const cfg = TS_RANGES[tsRange];
      const nowUnit = Math.floor(Date.now() / 1000 / cfg.stepSec);
      const raw = seriesFor(domain, cfg.stepSec).filter(([t]) => t >= nowUnit - cfg.secs / cfg.stepSec);
      if (tseriesLabel) tseriesLabel.textContent = 'traffic over time — ' + (domain || 'all sources') + ' · ' + cfg.grain;
      const empty = raw.length < 2;
      if (tseriesEl) tseriesEl.style.display = empty ? 'none' : 'block';
      if (tseriesEmpty) tseriesEmpty.style.display = empty ? 'block' : 'none';
      if (empty) { tsView = null; if (tseriesT0) tseriesT0.textContent = ''; if (tseriesT1) tseriesT1.textContent = ''; return; }
      // fill gaps with 0 so quiet periods read as quiet, not skipped
      const t0 = raw[0][0], tN = raw[raw.length - 1][0];
      const byUnit = new Map(raw);
      const counts = [];
      for (let t = t0; t <= tN && counts.length <= 800; t++) counts.push(byUnit.get(t) || 0);
      const max = Math.max(...counts, 1);
      const color = domainColor(domain);
      const step = counts.length > 1 ? TS_W / (counts.length - 1) : 0;
      const toY = c => TS_PAD + (1 - c / max) * (TS_H - 2 * TS_PAD);
      tseriesPoly.setAttribute('points', counts.map((c, i) => (i * step).toFixed(1) + ',' + toY(c).toFixed(1)).join(' '));
      tseriesPoly.style.stroke = color;
      if (tseriesDot) { tseriesDot.style.display = 'none'; tseriesDot.style.stroke = color; }
      const showClock = cfg.secs <= 604800;
      if (tseriesT0) tseriesT0.textContent = fmtSec(t0 * cfg.stepSec, showClock);
      if (tseriesT1) tseriesT1.textContent = fmtSec((tN + 1) * cfg.stepSec, showClock);
      tsView = { t0Sec: t0 * cfg.stepSec, stepSec: cfg.stepSec, counts, max, step, toY };
    }
    document.getElementById('tseries-range')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-range]');
      if (!btn) return;
      tsRange = btn.dataset.range;
      document.querySelectorAll('#tseries-range button').forEach(b => b.classList.toggle('active', b.dataset.range === tsRange));
      showSeries(pinnedDomain);
    });
    function applySelection(domain) {
      pinnedDomain = domain;
      if (tseriesClear) tseriesClear.style.display = domain != null ? 'inline-block' : 'none';
      document.querySelectorAll('.pie-legend span[data-domain], .ts-table tbody tr[data-domain]').forEach(el =>
        el.classList.toggle('sel', el.dataset.domain === domain));
      showSeries(domain);
    }
    tseriesClear?.addEventListener('click', () => applySelection(null));
    document.querySelectorAll('.pie-legend span[data-domain], .ts-table tbody tr[data-domain]').forEach(el => {
      el.addEventListener('click', e => {
        if (e.target.closest('a')) return; // domain links still navigate
        applySelection(pinnedDomain === el.dataset.domain ? null : el.dataset.domain);
      });
    });

    // trace the line: crosshair dot + timestamp/value tooltip (mouse or finger)
    function tseriesTrace(clientX, clientY) {
      if (!tsView) return;
      const rect = tseriesEl.getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const idx = Math.round(frac * (tsView.counts.length - 1));
      const c = tsView.counts[idx];
      if (tseriesDot) {
        tseriesDot.setAttribute('cx', (idx * tsView.step).toFixed(1));
        tseriesDot.setAttribute('cy', tsView.toY(c).toFixed(1));
        tseriesDot.style.display = 'block';
      }
      sparkTip.textContent = fmtSec(tsView.t0Sec + idx * tsView.stepSec, true) + ' UTC — ' + c + (c === 1 ? ' hit' : ' hits');
      sparkTip.style.display = 'block';
      // keep the tip above the finger/cursor and inside the viewport
      const tx = Math.min(clientX + 12, window.innerWidth - sparkTip.offsetWidth - 8);
      sparkTip.style.left = Math.max(8, tx) + 'px';
      sparkTip.style.top = (clientY - 40) + 'px';
    }
    function tseriesTraceEnd() {
      sparkTip.style.display = 'none';
      if (tseriesDot) tseriesDot.style.display = 'none';
    }
    if (tseriesEl) {
      tseriesEl.addEventListener('mousemove', e => tseriesTrace(e.clientX, e.clientY));
      tseriesEl.addEventListener('mouseleave', tseriesTraceEnd);
      tseriesEl.addEventListener('touchstart', e => { const t = e.touches[0]; tseriesTrace(t.clientX, t.clientY); }, { passive: true });
      tseriesEl.addEventListener('touchmove', e => { const t = e.touches[0]; tseriesTrace(t.clientX, t.clientY); }, { passive: true });
      tseriesEl.addEventListener('touchend', tseriesTraceEnd);
    }
    showSeries(null);

    if (pieCanvas && traffic.length > 0) {
      const pieTotal = traffic.reduce((s, r) => s + r.hits, 0);

      const SPECIAL = { unknown: '#555', homepage: '#666' };
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
          if (pinnedDomain == null) showSeries(idx >= 0 ? segments[idx].domain : null);
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
      pieCanvas.addEventListener('mouseleave', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; if (pinnedDomain == null) showSeries(null); });
      pieCanvas.addEventListener('touchmove', e => { e.preventDefault(); const t = e.touches[0]; pieHover(t.clientX, t.clientY); });
      pieCanvas.addEventListener('touchend', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; if (pinnedDomain == null) showSeries(null); });
      pieCanvas.addEventListener('click', e => {
        const idx = hitTest(e.clientX, e.clientY);
        if (idx < 0) return;
        const dom = segments[idx].domain;
        applySelection(pinnedDomain === dom ? null : dom);
      });
      window.addEventListener('resize', () => drawPie());
    }

    /* --- searches: one range selector drives BOTH the total and the sparkline --- */
    const sparkData = ${sparkJson};    // {time, searches}, oldest first, ALL buckets
    const allMetrics = ${metricsJson}; // {hour, searches, cache_hits}, same order
    // window by TIME (bucket is 5-min; hour*300 = unix seconds), not bucket
    // count — buckets are sparse so count-based slicing made 30d == all.
    const SEARCH_SECS = { '24h': 86400, '7d': 604800, '30d': 2592000 }; // missing key (all) = everything
    const NOW_SEC = Math.floor(Date.now() / 1000);
    const RANGE_LABEL = { '24h': '24 hours', '7d': '7 days', '30d': '30 days', 'all': 'all time' };
    // long windows aggregate 5-min buckets so the line stays readable
    const AGG_SECS = { '30d': 3600, 'all': 21600 };
    const AGG_LABEL = { 300: '5 min', 3600: 'hour', 21600: '6 h' };
    const SPARK_W = ${sw}, SPARK_H = ${sh};

    const searchesValue = document.getElementById('searches-value');
    const spark = document.getElementById('spark');
    const sparkPoly = document.getElementById('spark-poly');
    const sparkLabel = document.getElementById('spark-label');
    const sparkTip = document.getElementById('spark-tip');
    let sparkView = sparkData; // the currently-shown slice; the tooltip reads this

    function sliceWindow(arr, range) {
      const secs = SEARCH_SECS[range];
      return secs == null ? arr : arr.filter(r => r.hour * 300 >= NOW_SEC - secs);
    }
    function totalForRange(range) {
      return sliceWindow(allMetrics, range).reduce((s, r) => s + r.searches + (r.cache_hits || 0), 0);
    }
    function aggregate(arr, aggSecs) {
      if (aggSecs === 300) return arr;
      const buckets = new Map();
      for (const r of arr) {
        const key = Math.floor((r.hour * 300) / aggSecs);
        buckets.set(key, (buckets.get(key) || 0) + r.searches);
      }
      return [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([key, searches]) => ({
        hour: (key * aggSecs) / 300,
        time: new Date(key * aggSecs * 1000).toISOString().slice(0, 16) + 'Z',
        searches,
      }));
    }
    function drawSpark(range) {
      if (!sparkPoly) return;
      const aggSecs = AGG_SECS[range] || 300;
      sparkView = aggregate(sliceWindow(sparkData, range), aggSecs);
      const counts = sparkView.map(r => r.searches);
      const max = Math.max(...counts, 1);
      const step = counts.length > 1 ? SPARK_W / (counts.length - 1) : 0;
      sparkPoly.setAttribute('points', counts.map((c, i) => (i * step).toFixed(1) + ',' + (SPARK_H - (c / max) * SPARK_H).toFixed(1)).join(' '));
      if (sparkLabel) sparkLabel.textContent = 'searches / ' + AGG_LABEL[aggSecs] + ' (' + (RANGE_LABEL[range] || range) + ')';
    }
    function applyRange(range) {
      document.querySelectorAll('#searches-range button').forEach(b => b.classList.toggle('active', b.dataset.range === range));
      if (searchesValue) searchesValue.textContent = totalForRange(range).toLocaleString();
      const searchesLabel = document.getElementById('searches-label');
      if (searchesLabel) searchesLabel.textContent = 'total searches (' + (RANGE_LABEL[range] || range) + ')';
      drawSpark(range);
    }

    /* --- avg latency: independent range selector --- */
    const latencyValue = document.getElementById('latency-value');
    function applyLatencyRange(range) {
      document.querySelectorAll('#latency-range button').forEach(b => b.classList.toggle('active', b.dataset.range === range));
      if (!latencyValue) return;
      const win = sliceWindow(allMetrics, range);
      let searches = 0, totalMs = 0, cacheHits = 0, cacheMs = 0;
      for (const r of win) {
        searches += r.searches; totalMs += r.total_ms || 0;
        cacheHits += r.cache_hits || 0; cacheMs += r.cache_ms || 0;
      }
      const cold = searches > 0 ? totalMs / searches : 0;
      const cached = cacheHits > 0 ? cacheMs / cacheHits : 0;
      latencyValue.innerHTML = (cached > 0 ? cached.toFixed(1) + ' ms <span style="opacity:.4;font-size:.85rem">cached</span> · ' : '')
        + cold.toFixed(0) + ' ms' + (cached > 0 ? ' <span style="opacity:.4;font-size:.85rem">cold</span>' : '');
    }
    document.getElementById('latency-range')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-range]');
      if (btn) applyLatencyRange(btn.dataset.range);
    });

    document.getElementById('searches-range')?.addEventListener('click', e => {
      const btn = e.target.closest('button[data-range]');
      if (btn) applyRange(btn.dataset.range);
    });

    function sparkTrace(clientX, clientY) {
      const rect = spark.getBoundingClientRect();
      const x = clientX - rect.left;
      const idx = Math.min(Math.round(x / rect.width * (sparkView.length - 1)), sparkView.length - 1);
      if (idx >= 0 && sparkView[idx]) {
        const sc = sparkView[idx].searches;
        sparkTip.textContent = sparkView[idx].time + ' — ' + sc + (sc === 1 ? ' search' : ' searches');
        sparkTip.style.display = 'block';
        const tx = Math.min(clientX + 12, window.innerWidth - sparkTip.offsetWidth - 8);
        sparkTip.style.left = Math.max(8, tx) + 'px';
        sparkTip.style.top = (clientY - 40) + 'px';
      }
    }
    if (spark) {
      spark.addEventListener('mousemove', e => sparkTrace(e.clientX, e.clientY));
      spark.addEventListener('mouseleave', () => { sparkTip.style.display = 'none'; });
      spark.addEventListener('touchstart', e => { const t = e.touches[0]; sparkTrace(t.clientX, t.clientY); }, { passive: true });
      spark.addEventListener('touchmove', e => { const t = e.touches[0]; sparkTrace(t.clientX, t.clientY); }, { passive: true });
      spark.addEventListener('touchend', () => { sparkTip.style.display = 'none'; });
    }

    // default 7d: SSR already renders the 7d view; this makes the JS slice the
    // single source of truth so server + client always agree, and primes
    // sparkView for the tooltip.
    applyRange('7d');
    ${THEME_SCRIPT}
  </script>
</body>
</html>`;
}
