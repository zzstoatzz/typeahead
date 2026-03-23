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
  rows: { hour: number; searches: number; total_ms: number }[];
  totalSearches: number;
  avgLatency: number;
  handlePct: string;
  avatarPct: string;
  snapshots: SnapshotPoint[];
  trafficSources: { domain: string; hits: number }[];
}

export function statsPage(d: StatsData): string {
  // search sparkline data (oldest first)
  const sorted = [...d.rows].reverse();
  const counts = sorted.map((r) => r.searches);
  const sparkMax = Math.max(...counts, 1);
  const sw = 600, sh = 80;
  const step = counts.length > 1 ? sw / (counts.length - 1) : 0;
  const sparkPoints = counts
    .map((c, i) => `${(i * step).toFixed(1)},${(sh - (c / sparkMax) * sh).toFixed(1)}`)
    .join(" ");
  const sparkJson = JSON.stringify(
    sorted.map((r) => ({
      time: new Date(r.hour * 300_000).toISOString().slice(0, 16) + "Z",
      searches: r.searches,
    }))
  );

  const snapshotJson = JSON.stringify(d.snapshots);
  const trafficJson = JSON.stringify(d.trafficSources);

  const PIE_COLORS = ['#4a9', '#58a6ff', '#bc8cff', '#e5a04b', '#e06c75', '#56b6c2', '#c678dd', '#98c379', '#d19a66'];
  const trafficTotal = d.trafficSources.reduce((s, r) => s + r.hits, 0);
  const SPECIAL_DOMAINS: Record<string, string> = { unknown: '#555', homepage: '#666' };
  const pieLegendHtml = d.trafficSources.map((r, i) => {
    const color = SPECIAL_DOMAINS[r.domain] ?? PIE_COLORS[i % PIE_COLORS.length];
    const pct = trafficTotal > 0 ? ((r.hits / trafficTotal) * 100).toFixed(1) : '0';
    const label = r.domain in SPECIAL_DOMAINS
      ? r.domain
      : `<a href="https://${r.domain}" target="_blank" rel="noopener" class="legend-link">${r.domain}</a>`;
    return `<span><span class="ldot" style="background:${color}"></span>${label} (${pct}%)</span>`;
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
    .pie-tip { display: none; position: fixed; background: rgba(17, 17, 17, 0.95);
               border: 1px solid #333; border-radius: 6px; padding: 0.5rem 0.7rem;
               font-size: 0.72rem; color: #ccc; pointer-events: none; z-index: 50;
               white-space: nowrap; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }

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
      <h2>actors indexed</h2>
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
      <h2>searches / 5 min (7 days)</h2>
      ${counts.length > 1
        ? `<svg viewBox="0 0 ${sw} ${sh}" preserveAspectRatio="none" id="spark">
            <polyline points="${sparkPoints}" />
          </svg>`
        : `<div style="color:#444;font-size:0.8rem;padding:1rem 0;text-align:center">no data yet</div>`}
    </div>
    <div class="summary summary-2">
      <div class="metric">
        <div class="label">total searches (7d)</div>
        <div class="value">${d.totalSearches.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="label" data-tip="average response time for uncached searches (last 24h)">avg latency</div>
        <div class="value">${d.avgLatency.toFixed(1)} ms</div>
      </div>
    </div>

    <div class="pie-wrap">
      <h2>traffic sources</h2>
      ${d.trafficSources.length > 0
        ? `<canvas id="pie"></canvas>
           <div class="pie-legend">${pieLegendHtml}</div>`
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
    const snaps = ${snapshotJson};
    const canvas = document.getElementById('trend');
    const chartTip = document.getElementById('chart-tip');

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

      let yMax = 0;
      for (const s of snaps) { if (s.total > yMax) yMax = s.total; }
      yMax = yMax * 1.08 || 1;

      const toX = i => pad.l + (i / (n - 1)) * cw;
      const toY = v => pad.t + ch - (v / yMax) * ch;

      const series = [
        { key: 'with_avatars', color: COLORS.avatars },
        { key: 'with_handles', color: COLORS.handles },
        { key: 'total', color: COLORS.total },
      ];

      for (const s of series) {
        const pts = snaps.map((d, i) => [toX(i), toY(d[s.key])]);
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
        const t0ms = snaps[0].ts;
        const tNms = snaps[n - 1].ts;
        const spanDays = (tNms - t0ms) / 86400000;

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
        const x = toX(hoverIdx);
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
      const frac = (mx - pad.l) / cw;
      const idx = Math.max(0, Math.min(snaps.length - 1, Math.round(frac * (snaps.length - 1))));
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
    const pieCanvas = document.getElementById('pie');
    const pieTip = document.getElementById('pie-tip');

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
        }
        if (idx >= 0) {
          const seg = segments[idx];
          const pct = ((seg.hits / pieTotal) * 100).toFixed(1);
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
      pieCanvas.addEventListener('mouseleave', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; });
      pieCanvas.addEventListener('touchmove', e => { e.preventDefault(); const t = e.touches[0]; pieHover(t.clientX, t.clientY); });
      pieCanvas.addEventListener('touchend', () => { hoverIdx = -1; drawPie(); pieTip.style.display = 'none'; });
      window.addEventListener('resize', () => drawPie());
    }

    /* --- search sparkline --- */
    const sparkData = ${sparkJson};
    const spark = document.getElementById('spark');
    const sparkTip = document.getElementById('spark-tip');
    if (spark) {
      spark.addEventListener('mousemove', e => {
        const rect = spark.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = Math.min(Math.round(x / rect.width * (sparkData.length - 1)), sparkData.length - 1);
        if (idx >= 0 && sparkData[idx]) {
          const sc = sparkData[idx].searches;
          sparkTip.textContent = sparkData[idx].time + ' — ' + sc + (sc === 1 ? ' search' : ' searches');
          sparkTip.style.display = 'block';
          sparkTip.style.left = (e.clientX + 12) + 'px';
          sparkTip.style.top = (e.clientY - 28) + 'px';
        }
      });
      spark.addEventListener('mouseleave', () => { sparkTip.style.display = 'none'; });
    }
  </script>
</body>
</html>`;
}
