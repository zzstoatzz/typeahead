import { FAVICON } from "../types";

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
  // per-domain hourly series, only for domains shown in the pie (compact form:
  // domain -> [[hour, hits], ...])
  const pieDomains = new Set(pieSlice.map((r) => r.domain));
  const seriesByDomain: Record<string, [number, number][]> = {};
  for (const r of d.trafficSeries) {
    if (!pieDomains.has(r.domain)) continue;
    (seriesByDomain[r.domain] ??= []).push([r.hour, r.hits]);
  }
  const trafficSeriesJson = JSON.stringify(seriesByDomain);

  const pieLegendHtml = pieSlice.map((r, i) => {
    const color = SPECIAL_DOMAINS[r.domain] ?? PIE_COLORS[i % PIE_COLORS.length];
    const pct = trafficGrandTotal > 0 ? ((r.hits / trafficGrandTotal) * 100).toFixed(1) : '0';
    const label = r.domain in SPECIAL_DOMAINS
      ? r.domain
      : `<a href="https://${r.domain}" target="_blank" rel="noopener" class="legend-link">${r.domain}</a>`;
    return `<span><span class="ldot" style="background:${color}"></span>${label} (${pct}%)</span>`;
  }).join('');

  const trafficTableRows = d.trafficSources.map((r, i) => {
    const pct = trafficGrandTotal > 0 ? ((r.hits / trafficGrandTotal) * 100).toFixed(1) : '0';
    const label = r.domain in SPECIAL_DOMAINS
      ? r.domain
      : `<a href="https://${r.domain}" target="_blank" rel="noopener" class="legend-link">${r.domain}</a>`;
    return `<tr><td class="ts-rank">${i + 1}</td><td class="ts-domain">${label}</td><td class="ts-hits">${r.hits.toLocaleString()}</td><td class="ts-pct">${pct}%</td></tr>`;
  }).join('');

  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead — stats</title>
  ${FAVICON}
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0;
           display: flex; justify-content: center; padding: 2rem 1rem; min-height: 100vh; }
    .container { max-width: 620px; width: 100%; }
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: #888; }
    h1 strong { color: #e0e0e0; }
    .subtitle { font-size: 0.8rem; color: #555; margin-bottom: 1.5rem; }

    .chart-wrap { background: #111; border: 1px solid #222; border-radius: 6px;
                  padding: 0.9rem; margin-bottom: 1.5rem; position: relative; }
    .chart-wrap h2 { font-size: 0.75rem; font-weight: 400; color: #555; margin-bottom: 0.6rem; }
    .ranged-header { display: flex; justify-content: space-between; align-items: center;
                     margin-bottom: 0.6rem; }
    .ranged-header h2 { margin-bottom: 0; }
    .range-btns { display: inline-flex; gap: 0.15rem; }
    .range-btns button { background: transparent; border: 1px solid #222; color: #666;
                         padding: 0.15rem 0.45rem; font-size: 0.65rem; cursor: pointer;
                         border-radius: 3px; font-family: inherit; }
    .range-btns button:hover { color: #aaa; border-color: #333; }
    .range-btns button.active { background: #1a1a1a; color: #ccc; border-color: #333; }
    #trend { width: 100%; height: 200px; display: block; touch-action: none; }
    .legend { display: flex; gap: 1.2rem; margin-top: 0.6rem; font-size: 0.7rem; color: #888; }
    .legend span { display: flex; align-items: center; gap: 0.35rem; }
    .ldot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .chart-tip { display: none; position: fixed; background: rgba(17, 17, 17, 0.95);
                 border: 1px solid #333; border-radius: 6px; padding: 0.5rem 0.7rem;
                 font-size: 0.72rem; color: #ccc; pointer-events: none; z-index: 50;
                 line-height: 1.6; white-space: nowrap;
                 box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
    .ct-time { color: #666; font-size: 0.65rem; }
    .ct-row { display: flex; align-items: center; gap: 0.35rem; }
    .ct-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }

    .sparkline-wrap { background: #111; border: 1px solid #222; border-radius: 6px;
                      padding: 0.9rem; margin-bottom: 1.5rem; }
    .sparkline-wrap h2 { font-size: 0.75rem; font-weight: 400; color: #555; margin-bottom: 0.6rem; }
    svg { width: 100%; height: auto; }
    polyline { fill: none; stroke: #4a9; stroke-width: 1.5; }

    .summary { display: grid; gap: 0.8rem; margin-bottom: 1.5rem; }
    .summary-2 { grid-template-columns: repeat(2, 1fr); }
    .summary-3 { grid-template-columns: repeat(3, 1fr); }
    .metric { background: #111; border: 1px solid #222; border-radius: 6px; padding: 0.7rem 0.9rem; }
    .metric .metric-head { display: flex; justify-content: space-between; align-items: center;
                           gap: 0.5rem; margin-bottom: 0.2rem; }
    .metric .metric-head .label { margin-bottom: 0; }
    .metric .label { font-size: 0.7rem; color: #555; margin-bottom: 0.2rem; }
    .metric .label[data-tip] { cursor: default; position: relative; border-bottom: 1px dotted #444; display: inline-block; }
    .metric .label[data-tip]:hover::after {
      content: attr(data-tip); position: absolute; left: 0; top: 1.6em; width: 200px;
      padding: 0.4rem 0.5rem; background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
      font-size: 0.95em; color: #999; line-height: 1.4; z-index: 20; white-space: normal;
    }
    .metric .value { font-size: 1.1rem; color: #ccc; }
    .spark-tip { display: none; position: fixed; background: #1a1a1a; border: 1px solid #333;
                 border-radius: 4px; padding: 0.4rem 0.6rem; font-size: 0.75rem; color: #ccc;
                 pointer-events: none; z-index: 50; }

    .pie-wrap { background: #111; border: 1px solid #222; border-radius: 6px;
                padding: 0.9rem; margin-bottom: 1.5rem; }
    .pie-wrap h2 { font-size: 0.75rem; font-weight: 400; color: #555; margin-bottom: 0.6rem; }
    #pie { display: block; margin: 0 auto; width: 280px; height: 280px; touch-action: none; }
    .pie-legend { display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; margin-top: 0.7rem;
                  font-size: 0.7rem; color: #888; justify-content: center; }
    .pie-legend span { display: flex; align-items: center; gap: 0.3rem; line-height: 1.4; }
    .legend-link { color: #888; text-decoration: none; border-bottom: 1px dotted #444; }
    .legend-link:hover { color: #ccc; border-bottom-color: #888; }
    .traffic-series { margin-top: 1rem; padding-top: 0.8rem; border-top: 1px solid #1a1a1a; }
    .traffic-series h2 { font-size: 0.72rem; font-weight: 400; color: #555; margin-bottom: 0.5rem; }
    #tseries polyline { fill: none; stroke: #4a9; stroke-width: 1.5; transition: stroke 0.15s; }
    .pie-tip { display: none; position: fixed; background: rgba(17, 17, 17, 0.95);
               border: 1px solid #333; border-radius: 6px; padding: 0.5rem 0.7rem;
               font-size: 0.72rem; color: #ccc; pointer-events: none; z-index: 50;
               white-space: nowrap; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }

    .ts-table-wrap { margin-top: 1rem; max-height: 240px; overflow-y: auto;
                     -webkit-overflow-scrolling: touch;
                     border: 1px solid #1f1f1f; border-radius: 4px; }
    .ts-table-wrap::-webkit-scrollbar { width: 8px; }
    .ts-table-wrap::-webkit-scrollbar-track { background: #0a0a0a; }
    .ts-table-wrap::-webkit-scrollbar-thumb { background: #2a2a2a; border-radius: 4px; }
    .ts-table { width: 100%; border-collapse: collapse; font-size: 0.72rem;
                color: #aaa; table-layout: fixed; }
    .ts-table thead th { position: sticky; top: 0; background: #161616; color: #666;
                         font-weight: 400; text-align: left; padding: 0.45rem 0.7rem;
                         border-bottom: 1px solid #222; font-size: 0.68rem;
                         text-transform: uppercase; letter-spacing: 0.04em; }
    .ts-table td { padding: 0.35rem 0.7rem; border-bottom: 1px solid #181818; }
    .ts-table tr:last-child td { border-bottom: none; }
    .ts-rank { color: #555; width: 2.2rem; }
    .ts-domain { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ts-domain a { display: inline-block; max-width: 100%;
                   overflow: hidden; text-overflow: ellipsis;
                   white-space: nowrap; vertical-align: bottom; }
    .ts-hits { text-align: right; font-variant-numeric: tabular-nums; color: #ccc;
               width: 4.5rem; }
    .ts-pct { text-align: right; font-variant-numeric: tabular-nums; color: #888;
              width: 3.2rem; }
    .ts-summary { font-size: 0.7rem; color: #555; margin-top: 0.6rem; text-align: center; }

    footer { padding-top: 1.5rem; border-top: 1px solid #1a1a1a; font-size: 0.7rem;
             color: #444; display: flex; justify-content: center; gap: 0.4rem; }
    footer a { color: #555; text-decoration: none; }
    footer a:hover { color: #888; }

    @media (max-width: 640px) {
      body { padding: 1.5rem 0.75rem; }
      #trend { height: 160px; }
      .legend { gap: 0.8rem; font-size: 0.65rem; flex-wrap: wrap; }
      .summary-3 { grid-template-columns: 1fr 1fr; }
      #pie { width: 220px; height: 220px; }
      .pie-legend { gap: 0.4rem 0.7rem; font-size: 0.65rem; }
      .ts-table-wrap { max-height: 280px; }
      .ts-table { font-size: 0.68rem; }
      .ts-table thead th { padding: 0.4rem 0.5rem; font-size: 0.62rem; }
      .ts-table td { padding: 0.32rem 0.5rem; }
      .ts-rank { width: 1.8rem; }
      .ts-hits { width: 3.6rem; }
      .ts-pct { width: 2.8rem; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><a href="/" style="color:inherit;text-decoration:none"><strong>typeahead</strong></a> stats</h1>
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
        <div class="label" data-tip="% of indexed actors with a resolved handle (vs bare DID only)">handle coverage</div>
        <div class="value">${d.handlePct}%</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="% of indexed actors with a profile image">avatar coverage</div>
        <div class="value">${d.avatarPct}%</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="actors hidden by bluesky moderation (!hide, !takedown, spam)">hidden by moderation</div>
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
          <div class="label" data-tip="cached (edge) vs uncached (fly replica)">avg latency</div>
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
             <h2 id="tseries-label">traffic over time — all sources</h2>
             <svg viewBox="0 0 600 60" preserveAspectRatio="none" id="tseries">
               <polyline points="" id="tseries-poly" />
             </svg>
             <div id="tseries-empty" style="display:none;color:#444;font-size:0.75rem;padding:0.8rem 0;text-align:center">collecting data — hourly per-app history starts July 2026</div>
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

    /* --- per-app traffic history: pie hover drives this line --- */
    const trafficSeries = ${trafficSeriesJson}; // domain -> [[hour, hits], ...]
    const tseriesPoly = document.getElementById('tseries-poly');
    const tseriesLabel = document.getElementById('tseries-label');
    const tseriesEl = document.getElementById('tseries');
    const tseriesEmpty = document.getElementById('tseries-empty');

    function seriesFor(domain) {
      if (domain != null) return trafficSeries[domain] || [];
      // all sources: sum per hour across domains
      const sums = new Map();
      for (const dom in trafficSeries) {
        for (const [h, hits] of trafficSeries[dom]) sums.set(h, (sums.get(h) || 0) + hits);
      }
      return [...sums.entries()].sort((a, b) => a[0] - b[0]);
    }
    function showSeries(domain, color) {
      if (!tseriesPoly) return;
      const raw = seriesFor(domain);
      if (tseriesLabel) tseriesLabel.textContent = 'traffic over time — ' + (domain || 'all sources');
      if (raw.length < 2) {
        if (tseriesEl) tseriesEl.style.display = 'none';
        if (tseriesEmpty) tseriesEmpty.style.display = 'block';
        return;
      }
      if (tseriesEl) tseriesEl.style.display = 'block';
      if (tseriesEmpty) tseriesEmpty.style.display = 'none';
      // fill hour gaps with 0 so quiet periods read as quiet, not skipped
      const h0 = raw[0][0], hN = raw[raw.length - 1][0];
      const byHour = new Map(raw);
      const counts = [];
      for (let h = h0; h <= hN && counts.length <= 24 * 31; h++) counts.push(byHour.get(h) || 0);
      const max = Math.max(...counts, 1);
      const step = counts.length > 1 ? 600 / (counts.length - 1) : 0;
      tseriesPoly.setAttribute('points', counts.map((c, i) => (i * step).toFixed(1) + ',' + (60 - (c / max) * 60).toFixed(1)).join(' '));
      tseriesPoly.setAttribute('stroke', color || '#4a9');
      tseriesPoly.style.stroke = color || '#4a9';
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
          if (idx >= 0) showSeries(segments[idx].domain, segments[idx].color);
          else showSeries(null);
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
      pieCanvas.addEventListener('mouseleave', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; showSeries(null); });
      pieCanvas.addEventListener('touchmove', e => { e.preventDefault(); const t = e.touches[0]; pieHover(t.clientX, t.clientY); });
      pieCanvas.addEventListener('touchend', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; showSeries(null); });
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

    if (spark) {
      spark.addEventListener('mousemove', e => {
        const rect = spark.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = Math.min(Math.round(x / rect.width * (sparkView.length - 1)), sparkView.length - 1);
        if (idx >= 0 && sparkView[idx]) {
          const sc = sparkView[idx].searches;
          sparkTip.textContent = sparkView[idx].time + ' — ' + sc + (sc === 1 ? ' search' : ' searches');
          sparkTip.style.display = 'block';
          sparkTip.style.left = (e.clientX + 12) + 'px';
          sparkTip.style.top = (e.clientY - 28) + 'px';
        }
      });
      spark.addEventListener('mouseleave', () => { sparkTip.style.display = 'none'; });
    }

    // default 7d: SSR already renders the 7d view; this makes the JS slice the
    // single source of truth so server + client always agree, and primes
    // sparkView for the tooltip.
    applyRange('7d');
  </script>
</body>
</html>`;
}
