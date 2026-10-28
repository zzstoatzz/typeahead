import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT, CARD_VERSION } from "./theme";
import { escHtml as esc } from "../utils";

export interface SnapshotPoint {
  ts: number;  // millisecond timestamp
  total: number;
  with_handles: number;
  with_avatars: number;
}

export interface StatsData {
  total: number;
  hiddenCount: number | null;
  exclusionsAsOf?: string;
  rows: { hour: number; searches: number; total_ms: number; cache_hits: number; cache_ms: number }[];
  totalSearches: number;
  avgLatency: number;
  avgCacheLatency: number;
  handlePct: string;
  avatarPct: string;
  snapshots: SnapshotPoint[];
  trafficSources: { domain: string; hits: number }[];
  /** the whole traffic_sources table, so the pie can draw what the ranked rows leave out */
  trafficAll: { domains: number; hits: number };
  trafficSeries: { hour: number; domain: string; hits: number }[];
  trafficSeries5m: { bucket: number; domain: string; hits: number }[];
  trafficDaily: { day: number; domain: string; hits: number }[];
  requestSeries: { hour: number; domain: string; endpoint: string; status: number; hits: number }[];
  requestSeries5m: { bucket: number; domain: string; endpoint: string; status: number; hits: number }[];
  requestDaily: { day: number; domain: string; endpoint: string; status: number; hits: number }[];
  /** view restored from the query string — drives the link preview */
  view?: { apps: string[]; range: string; origin: string };
}

/** Link-preview metadata that describes THIS view, not the page in general.
 *
 *  A shared comparison is only worth sharing if the unfurl says what is being
 *  compared, so the title and description are built from the same numbers the
 *  chart draws, and the card image is handed the same params.
 */
function ogTags(d: StatsData): string {
  const view = d.view ?? { apps: [], range: '7d', origin: 'https://typeahead.waow.tech' };
  const RANGE_SECS = { '24h': 86400, '7d': 604800, '30d': 2592000, 'all': 2592000 } as const;
  const RANGE_WORDS = { '24h': 'last 24 hours', '7d': 'last 7 days', '30d': 'last 30 days', 'all': 'last 30 days' } as const;
  type Range = keyof typeof RANGE_SECS;
  const isRange = (r: string): r is Range => Object.hasOwn(RANGE_SECS, r);
  const range: Range = isRange(view.range) ? view.range : '7d';
  const secs = RANGE_SECS[range];
  const cutoff = Math.floor(Date.now() / 1000 / 3600) - secs / 3600;

  const by = new Map<string, number>();
  let all = 0;
  for (const r of d.trafficSeries) {
    if (r.hour < cutoff) continue;
    by.set(r.domain, (by.get(r.domain) ?? 0) + r.hits);
    all += r.hits;
  }
  const apps = view.apps.filter(a => by.has(a));

  const q = new URLSearchParams();
  if (apps.length) q.set('apps', apps.join(','));
  if (view.range !== '7d') q.set('range', view.range);
  const qs = q.toString();
  const pageUrl = view.origin + '/stats' + (qs ? '?' + qs : '');
  q.set('v', String(CARD_VERSION));
  const imgUrl = view.origin + '/og-image?' + q.toString();

  let title: string, desc: string;
  if (apps.length) {
    const rows = apps.map(a => ({ a, hits: by.get(a) ?? 0 })).sort((x, y) => y.hits - x.hits);
    title = rows.map(r => r.a).join(' vs ') + ' · typeahead traffic';
    const parts = rows.map(r =>
      `${r.a} ${r.hits.toLocaleString('en-US')}` + (all > 0 ? ` (${((r.hits / all) * 100).toFixed(1)}%)` : ''));
    const lead = rows.length >= 2 && rows[1]!.hits > 0
      ? ` — ${rows[0]!.a} ${(rows[0]!.hits / rows[1]!.hits < 10
          ? (rows[0]!.hits / rows[1]!.hits).toFixed(2) : Math.round(rows[0]!.hits / rows[1]!.hits))}× ${rows[1]!.a} by request volume`
      : '';
    desc = `search traffic over the ${RANGE_WORDS[range]}: ${parts.join(' · ')}${lead}`;
  } else {
    title = 'typeahead — stats';
    desc = `${d.total.toLocaleString('en-US')} actors indexed, ${d.handlePct}% with a resolved handle. Live traffic, latency and coverage for community actor search on atproto.`;
  }

  const m = (p: string, c: string) => `<meta property="${p}" content="${esc(c)}">`;
  const nm = (p: string, c: string) => `<meta name="${p}" content="${esc(c)}">`;
  return [
    nm('description', desc),
    m('og:type', 'website'),
    m('og:site_name', 'typeahead'),
    m('og:title', title),
    m('og:description', desc),
    m('og:url', pageUrl),
    m('og:image', imgUrl),
    m('og:image:width', '1200'),
    m('og:image:height', '630'),
    m('og:image:alt', title),
    nm('twitter:card', 'summary_large_image'),
    nm('twitter:title', title),
    nm('twitter:description', desc),
    nm('twitter:image', imgUrl),
    `<link rel="canonical" href="${esc(pageUrl)}">`,
    `<title>${esc(title)}</title>`,
  ].join('\n  ');
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
  const ACTIVITY_SECS = { '24h': 86400, '7d': 604800, '30d': 2592000, 'all': null } as const;
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
  // Inlining JSON into a <script> is not the same as serialising it. JSON.stringify
  // does NOT escape "</script>", so a single "<" in attacker-controlled data closes
  // the element and everything after it is parsed as HTML. Domains reach this page
  // straight from the X-Client / Origin request headers, which made this a stored
  // XSS: one request with X-Client: </script><script>... and the payload ran for
  // every visitor to /stats. Escaping "<" is enough and keeps the value valid JSON.
  const jsonScript = <T extends object>(v: T) => JSON.stringify(v).replace(/</g, "\\u003c");

  const activityJson = jsonScript(activity);
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
  const snapshotJson = jsonScript(thinned);

  const PIE_COLORS = [
    // Ordered so adjacent slices never sit next to a neighbour on the wheel —
    // the old set had two violets and two ambers that read as the same colour on
    // a 90px line. Mid-tone rather than pastel or deep, since the page renders
    // on both a light and a dark background.
    '#14b8a6', '#3b82f6', '#a855f7', '#f59e0b', '#f43f5e', '#06b6d4', '#65a30d', '#f97316', '#ec4899',
  ];
  const SPECIAL_DOMAINS = { unknown: '#94a3b8', homepage: '#64748b' } satisfies Record<string, string>;
  const PIE_TOP_N = 12;
  // Every percentage on the page divides by the WHOLE table, and the ring
  // draws the whole table too: the top N as named slices plus one neutral
  // "everything else" slice for the rest. Before 2026-09-04 the ring held
  // only the top 12 and the legend divided by the 50 rows fetched, so the
  // largest slice read as 50.7% of the ring, was labelled 44.5%, and was
  // 43.6% of all traffic. The rest slice is selectable like any source: its
  // history is the sum of every source outside the named twelve, computed
  // on the client from the same per-domain series (see seriesFor).
  const trafficListed = d.trafficSources.reduce((s, r) => s + r.hits, 0);
  const trafficGrandTotal = Math.max(d.trafficAll.hits, trafficListed);
  const pieSlice = d.trafficSources.slice(0, PIE_TOP_N);
  const pieShown = pieSlice.reduce((s, r) => s + r.hits, 0);
  const restHits = trafficGrandTotal - pieShown;
  const restDomains = Math.max(d.trafficAll.domains, d.trafficSources.length) - pieSlice.length;
  const rest = restHits > 0 && restDomains > 0 ? { hits: restHits, domains: restDomains } : null;
  const REST_LABEL = 'everything else';
  const trafficJson = jsonScript(rest
    ? [...pieSlice, { domain: REST_LABEL, hits: rest.hits, rest: true }]
    : pieSlice);
  // per-domain hourly series for every recorded domain (bounded: ~50 domains ×
  // ≤720 hourly rows), compact form: domain -> [[hour, hits], ...]
  const seriesByDomain: Record<string, [number, number][]> = {};
  for (const r of d.trafficSeries) {
    (seriesByDomain[r.domain] ??= []).push([r.hour, r.hits]);
  }
  const trafficSeriesJson = jsonScript(seriesByDomain);
  // raw 5-min buckets (last 48h) for the 24h view
  const seriesByDomain5m: Record<string, [number, number][]> = {};
  for (const r of d.trafficSeries5m) {
    (seriesByDomain5m[r.domain] ??= []).push([r.bucket, r.hits]);
  }
  const trafficSeries5mJson = jsonScript(seriesByDomain5m);
  // daily grain, full history — this is why "all" has no cap: resolution
  // decays with age (5-min → hourly → daily) but no app's history is dropped
  const seriesByDomainDaily: Record<string, [number, number][]> = {};
  for (const r of d.trafficDaily) {
    (seriesByDomainDaily[r.domain] ??= []).push([r.day, r.hits]);
  }
  const trafficSeriesDailyJson = jsonScript(seriesByDomainDaily);

  // request_buckets, summed down to the two other ways of slicing the same
  // volume: by endpoint (all apps, all statuses) and by status class (all
  // apps, all endpoints). Same compact shape as the traffic maps, one per
  // grain, so the chart reads them through the same code path.
  const statusClass = (code: number) => `${Math.floor(code / 100)}xx`;
  type Grain = Record<string, [number, number][]>;
  type RequestGrains = { endpoint: Grain; status: Grain };
  const sliceRequests = <R extends { domain: string; endpoint: string; status: number; hits: number }>(
    rows: R[], t: (r: R) => number,
  ): RequestGrains => {
    const out: RequestGrains = { endpoint: {}, status: {} };
    const acc = { endpoint: new Map<string, Map<number, number>>(), status: new Map<string, Map<number, number>>() };
    for (const r of rows) {
      for (const [dim, key] of [["endpoint", r.endpoint], ["status", statusClass(r.status)]] as const) {
        const m = acc[dim].get(key) ?? new Map<number, number>();
        m.set(t(r), (m.get(t(r)) ?? 0) + r.hits);
        acc[dim].set(key, m);
      }
    }
    for (const dim of ["endpoint", "status"] as const) {
      for (const [key, m] of acc[dim]) out[dim][key] = [...m.entries()].sort((a, b) => a[0] - b[0]);
    }
    return out;
  };
  const requestSeriesJson = jsonScript({
    hourly: sliceRequests(d.requestSeries, (r) => r.hour),
    m5: sliceRequests(d.requestSeries5m, (r) => r.bucket),
    daily: sliceRequests(d.requestDaily, (r) => r.day),
  });

  const sourcePills = d.trafficSources.map((row, i) => {
    const pct = trafficGrandTotal ? (row.hits / trafficGrandTotal * 100).toFixed(1) : '0';
    return `<button type="button" data-domain="${esc(row.domain)}" data-hits="${row.hits}" data-share="${pct}" title="${esc(row.domain)} · ${row.hits.toLocaleString()} requests · ${pct}% of traffic" aria-label="${esc(row.domain)}, ${row.hits.toLocaleString()} requests, ${pct}% of traffic" aria-pressed="false"${i >= 12 ? ' hidden' : ''}><span class="source-name">${esc(row.domain)}</span><span class="source-value">${pct}%</span></button>`;
  }).join('');

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  ${ogTags(d)}
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    :root, :root[data-theme] { --dim: var(--muted); }
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

    /* cursor:pointer is not decoration here — iOS Safari only synthesises a
       click on a non-interactive element that looks clickable. The legend and
       the table rows both carry it, which is exactly why they worked on a phone
       and the donut did not. */
    #pie { display: block; margin: 0 auto; width: 280px; height: 280px;
           touch-action: pan-y; cursor: pointer; }
    #pie-breadcrumb { display: flex; align-items: center; gap: 0.3rem; height: 36px; overflow-x: auto; white-space: nowrap; scrollbar-width: none; }
    #pie-breadcrumb button { border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 0.65rem; padding: 0.4rem 0.3rem; cursor: pointer; flex-shrink: 0; }
    #pie-breadcrumb button[aria-current] { color: var(--text); }
    .pie-help { display: flex; justify-content: space-between; gap: 0.5rem; color: var(--muted); font-size: 0.6rem; margin-top: 0.6rem; }
    .pie-stage { position: relative; }
    .source-detail { position: absolute; inset: 26%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 5px; text-align: center; pointer-events: none; }
    .source-detail .detail-name { width: 100%; color: var(--text); font-size: clamp(12px, 3.3vw, 18px); line-height: 1.3; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
    .source-detail .detail-total { font-size: clamp(24px, 6vw, 36px); }
    .source-detail .detail-count { font-size: clamp(10px, 2.5vw, 12px); line-height: 1.5; color: var(--muted); font-variant-numeric: tabular-nums; }
    .source-detail a { pointer-events: auto; display: inline-flex; align-items: center; min-height: 32px; color: var(--accent); font-size: 11px; }
    .source-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
    .source-heading a { font-size: 0.65rem; color: var(--muted); white-space: nowrap; }
    .source-context { font-size: 0.7rem; color: var(--muted); margin-bottom: 0.6rem; }
    #pie-breadcrumb[hidden] { display: none; }
    #pie .pie-segment { cursor: pointer; outline: none; }
    #pie .pie-face { stroke: var(--panel); stroke-width: 0.6; transition: opacity 100ms ease; }
    #pie .pie-segment:hover .pie-face, #pie .pie-segment:focus-visible .pie-face { filter: brightness(1.12); }
    #pie .pie-segment:focus-visible .pie-face { stroke: var(--text); stroke-width: 1.5; }
    #pie-explore, #pie-back { min-height: 44px; border: 1px solid var(--line-2); border-radius: 8px; padding: 0.4rem 0.6rem; background: var(--panel-2); color: var(--text); font: inherit; cursor: pointer; }
    #pie-explore[hidden], #pie-back[hidden], #pie-hint[hidden] { display: none; }
    .pie-help { align-items: center; justify-content: center; }
    .pie-help:has(#pie-back:not([hidden])) { justify-content: space-between; }
    #pie .pie-segment[aria-pressed="true"] .pie-face { stroke: var(--text); stroke-width: 1.5; }
    #source-list button[aria-pressed="true"]::after { content: "✓"; color: var(--text); }
    #pie text, #pie image, #pie .pie-logo { pointer-events: none; }
    @media (prefers-reduced-motion: reduce) { #pie .pie-face { transition: none; } }
    .traffic-series { margin-top: 0.85rem; padding-top: 0.75rem; border-top: 1px solid var(--line); }
    .traffic-series h2 { font-size: 0.72rem; font-weight: 400; color: var(--muted); margin-bottom: 0; }
    .traffic-series .ranged-header { margin-bottom: 0.5rem; }
    /* The plot RESERVES its height so swapping the chart for the empty state
       cannot move the page. Previously the empty state was content-sized while
       the svg was responsive (viewBox 600x90, height:auto), so hovering a domain
       with no traffic in the window made everything below lurch up. */
    .tseries-plot { position: relative; height: 112px; margin-top: 0.6rem; }
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
    .activity-metrics { display: grid; grid-template-columns: 1.25fr 1fr 1fr; gap: 0.75rem; margin: 1rem 0; }
    .activity-metrics .value { white-space: nowrap; font-variant-numeric: tabular-nums; font-size: 1.15rem; }
    .activity-metrics small { font-size: 0.65rem; color: var(--muted); }
    #activity-period { color: var(--dim); font-size: 0.65rem; margin-left: 0.35rem; }
    .activity-wrap > .ranged-header { display: flex; flex-wrap: nowrap; gap: 0.5rem; }
    #activity-range { display: grid; grid-template-columns: repeat(4, 1fr); }
    #activity-range button, #tseries-dim button { min-height: 34px; padding: 0.25rem 0.45rem; font-size: 0.65rem; border-color: transparent; }
    #activity-range, #tseries-dim { gap: 0; border: 1px solid var(--line); border-radius: 7px; padding: 2px; }
    #activity-range button.active, #tseries-dim button.active { border-color: var(--line-2); }
    .traffic-series > .ranged-header { display: flex; flex-wrap: nowrap; gap: 0.5rem; }
    #tseries-dim { display: inline-flex; }
    #tseries-chips { display: flex; gap: 0.35rem; height: 40px; align-items: center; overflow-x: auto; scrollbar-width: none; }
    .ts-chip { display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; height: 30px; padding: 0 0.55rem; border: 1px solid var(--line); border-radius: 999px; background: transparent; color: var(--muted); font: inherit; font-size: 0.65rem; cursor: pointer; }
    .ts-chip.selected { background: var(--panel-2); border-color: var(--line-2); color: var(--text); }
    .ts-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .series-name { display: flex; align-items: center; gap: 0.35rem; white-space: nowrap; }
    .series-check { color: var(--muted); }
    .source-logo { display: inline-block; position: relative; width: 14px; height: 14px; flex-shrink: 0; vertical-align: middle; }
    .source-logo img { position: absolute; inset: 0; width: 14px; height: 14px; object-fit: contain; border-radius: 3px; visibility: hidden; }
    .source-logo::before { content: ""; position: absolute; width: 6px; height: 6px; inset: 4px; border-radius: 50%; background: var(--logo-color, var(--muted)); }
    .source-logo.loaded::before { visibility: hidden; }
    .pie-selection { display: flex; align-items: center; gap: 0.5rem; height: 36px; font-size: 0.65rem; color: var(--muted); }
    #pie-selection-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pie-selection a { margin-left: auto; white-space: nowrap; color: var(--accent); }
    #traffic-chart { scroll-margin-top: 16px; }
    #clear-series { background: transparent; border: 0; color: var(--muted); font: inherit; font-size: 0.7rem; cursor: pointer; min-height: 36px; padding: 0 0.5rem; }
    #clear-series:disabled { opacity: 0.4; cursor: default; }
    .dim-btns button { padding: 0.25rem 0.5rem; font-size: 0.66rem; min-height: 28px; }
    #tseries-dim { margin-left: auto; }
    .empty-card { color: var(--dim); font-size: 0.8rem; padding: 2rem 0; text-align: center; }
    .pie-cta { margin-top: 0.6rem; font-size: 0.7rem; color: var(--dim); text-align: center; }
    .pie-cta a { display: inline-flex; align-items: center; min-height: 44px; white-space: nowrap; color: var(--blue); text-decoration: none; }
    .tseries-axis { display: flex; justify-content: space-between; font-size: 0.65rem;
                    color: var(--dim); margin-top: 0.25rem; font-variant-numeric: tabular-nums; }
    .tseries-foot { display: flex; align-items: center; justify-content: space-between;
                    gap: 0.5rem; margin-top: 0.5rem; }
    .tseries-hint { font-size: 0.65rem; color: var(--muted); white-space: nowrap; min-width: 0; }
    .ts-foot-btns { display: flex; align-items: center; gap: 0.3rem; flex-shrink: 0; }
    .ts-menu-wrap { position: relative; flex-shrink: 0; }
    #tseries-menu-btn { background: transparent; border: 1px solid var(--line); color: var(--dim);
                        padding: 0.2rem 0.5rem 0.35rem; font-size: 0.8rem; line-height: 1; cursor: pointer;
                        border-radius: 6px; font-family: inherit; min-height: 34px; min-width: 34px; }
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
    .tseries-totals { height: 20px; white-space: nowrap; overflow-x: auto; scrollbar-width: thin; margin-top: 0.6rem; color: var(--muted); font-size: 0.65rem; line-height: 1.5; overflow-wrap: anywhere; }
    .tseries-totals { display: flex; align-items: center; gap: 1rem; scrollbar-width: none; padding-right: 1rem; mask-image: linear-gradient(to right, black calc(100% - 20px), transparent); }
    .series-total { display: inline-flex; align-items: center; gap: 0.3rem; flex-shrink: 0; font-variant-numeric: tabular-nums; }

    footer { padding-top: 1.5rem; border-top: 1px solid var(--line); font-size: 0.7rem;
             color: var(--faint); display: flex; align-items: center; justify-content: center;
             flex-wrap: wrap; gap: 0.4rem 0.6rem; }
    footer a { color: var(--dim); text-decoration: none; padding: 0.35rem 0.15rem; }
    footer a:hover { color: var(--muted); }

    @media (max-width: 640px) {
      /* narrow: the heading alone nearly fills the line, so the chips get their
         own row rather than fighting it for space */
      body { padding-left: 0.75rem; padding-right: 0.75rem; }
      #trend { height: 170px; }
      .legend { gap: 0.8rem; font-size: 0.65rem; flex-wrap: wrap; }
      .summary-2, .summary-3 { grid-template-columns: 1fr 1fr; }
      .summary-3 > :last-child { grid-column: 1 / -1; display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }
      #pie { width: 200px; height: 200px; }
      .tseries-plot { aspect-ratio: auto; height: 72px; }
    }
    @media (max-width: 360px) {
      #activity-period { display: block; margin: 0.2rem 0 0; }
      #tseries-dim button { padding-inline: 0.3rem; font-size: 0.6rem; }
      .traffic-series h2 { font-size: 0.65rem; }
    }
    #pie { width: 100%; height: auto; aspect-ratio: 1; margin: 0; overflow: visible; touch-action: pan-y; }
    @media (max-width: 360px) { .pie-help { font-size: 0.55rem; } }
    .source-tools { display: flex; gap: 0.6rem; align-items: center; margin-top: 0.6rem; }
    #source-search { width: 100%; min-width: 0; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: var(--text); padding: 0.5rem 0; font: inherit; font-size: 16px; border-radius: 0; }
    #source-toggle { flex-shrink: 0; min-height: 44px; padding: 0.4rem 0.6rem; border: 1px solid var(--line-2); border-radius: 8px; background: transparent; color: var(--text); font: inherit; font-size: 0.65rem; cursor: pointer; }
    #source-toggle[hidden] { display: none; }
    #source-list { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.65rem; }
    #source-list button { display: inline-flex; align-items: center; gap: 0.35rem; max-width: 100%; padding: 0.3rem 0.55rem; min-height: 32px; border: 1px solid var(--line); border-radius: 16px; color: var(--muted); background: transparent; font: inherit; font-size: 0.65rem; cursor: pointer; }
    #source-list button[aria-pressed="true"] { background: var(--panel-2); border-color: var(--line-2); color: var(--text); }
    #source-list button[hidden], .source-actions button[hidden] { display: none; }
    .source-name { min-width: 0; overflow-wrap: anywhere; text-align: left; }
    .source-value { flex-shrink: 0; font-variant-numeric: tabular-nums; color: var(--muted); }
    .source-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; font-size: 0.6rem; color: var(--muted); }
    #source-count { margin-right: auto; }
    .source-actions button { padding: 0.25rem; min-height: 32px; background: transparent; border: 0; color: var(--text); font: inherit; cursor: pointer; }
    #source-empty { font-size: 0.7rem; color: var(--muted); }
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
        ? `<canvas id="trend" role="img" aria-label="actors indexed over time"></canvas>`
        : `<div class="empty-card">collecting data — check back soon</div>`}
      <div class="legend">
        ${[
          { color: 'var(--accent)', label: `total (${d.total.toLocaleString()})`, value: d.total },
          { color: 'var(--violet)', label: 'with avatars', value: d.snapshots[d.snapshots.length - 1]?.with_avatars ?? 0 },
          { color: 'var(--blue)', label: 'with handles', value: d.snapshots[d.snapshots.length - 1]?.with_handles ?? 0 },
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
        <div class="label" tabindex="0" data-tip="Actors excluded by moderation or invalid identity, including quarantined handle claims. ${d.exclusionsAsOf ? `Audited ${esc(d.exclusionsAsOf)}.` : 'The offline audit is pending.'}">excluded from search</div>
        <div class="value">${d.hiddenCount === null ? "Audit pending" : d.hiddenCount.toLocaleString()}</div>
      </div>
    </div>

    <!-- ONE window control for the whole activity section. There used to be
         three independent ones (searches sparkline, latency, traffic series)
         plus a duplicate line chart: "searches / 5 min" and "traffic over time"
         both plotted volume over time, and the donut's total was computed over
         a different window than the searches count sitting next to it. -->
    <div class="activity-wrap">
      <div class="ranged-header">
        <h2>activity <span id="activity-period">7 days</span></h2>
        <span class="range-btns" id="activity-range">
          <button data-range="24h">24h</button>
          <button data-range="7d" class="active">7d</button>
          <button data-range="30d">30d</button>
          <button data-range="all">all</button>
        </span>
      </div>
      <div class="activity-metrics">
        <div class="metric"><div class="label" id="searches-label">searches</div><div class="value" id="searches-value">${initialTotal.toLocaleString()}</div></div>
        <div class="metric"><div class="label" title="Mean response time for cached searches at the edge">avg cached</div><div class="value" id="cached-latency">${d.avgCacheLatency.toFixed(1)} <small>ms</small></div></div>
        <div class="metric"><div class="label" title="Mean response time for searches that miss the edge cache">avg cold</div><div class="value" id="cold-latency">${d.avgLatency.toFixed(0)} <small>ms</small></div></div>
      </div>
      <!-- the series lives INSIDE this card, directly under the control that
           drives it. It used to sit below the donut, several hundred pixels from
           a selector that silently governed it. -->
      <div class="traffic-series" id="traffic-chart">
        <div class="ranged-header">
          <h2 id="tseries-label">traffic over time — all sources</h2>
          <span class="range-btns dim-btns" id="tseries-dim" aria-label="slice by">
            <button data-dim="source" class="active">source</button>
            <button data-dim="endpoint">endpoint</button>
            <button data-dim="status">status</button>
          </span>

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
        <div id="tseries-chips" role="group" aria-label="Chart series"></div>
        <div class="tseries-totals" id="tseries-totals" aria-live="polite"></div>
        <div class="tseries-foot">
          <span class="tseries-hint" id="tseries-hint">Tap to compare</span>
          <div class="ts-foot-btns"><button id="clear-series" type="button">clear</button>
          <div class="ts-menu-wrap">
            <button id="tseries-menu-btn" aria-haspopup="menu" aria-expanded="false" title="chart actions">export</button>
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
      <div class="source-heading"><h2>traffic sources</h2><a href="#traffic-chart">View activity ↑</a></div>
      <div class="source-context">${trafficGrandTotal.toLocaleString()} requests · ${d.trafficSources.length} apps</div>
      ${d.trafficSources.length > 0
        ? `<nav id="pie-breadcrumb" aria-label="Traffic source depth"></nav>
           <div class="pie-stage"><svg id="pie" viewBox="0 0 400 400" role="group" aria-label="Traffic sources. Select a source to compare, or zoom into smaller sources."></svg><div class="source-detail" id="source-detail" aria-live="polite"></div></div>
           <div class="pie-help"><button type="button" id="pie-back" hidden>← Back to top apps</button><button type="button" id="pie-explore" hidden></button></div>
           <div class="source-tools"><input type="search" id="source-search" placeholder="Find an app" aria-label="Find an app" autocomplete="off"><button type="button" id="source-toggle" aria-controls="source-list" aria-expanded="false"${d.trafficSources.length <= 12 ? ' hidden' : ''}>Show all</button></div>
           <div id="source-list" aria-label="All apps">${sourcePills}</div>
           <p id="source-empty" hidden>No matching apps</p>
           <div class="source-actions"><span id="source-count">${Math.min(12, d.trafficSources.length)} of ${d.trafficSources.length} apps</span><button type="button" id="source-more"${d.trafficSources.length <= 12 ? ' hidden' : ''}>show 50 more</button><button type="button" id="source-all"${d.trafficSources.length <= 12 ? ' hidden' : ''}>show all</button><button type="button" id="source-less" hidden>less</button></div>`
        : `<div class="empty-card">collecting data — check back soon</div>`}
      <div class="pie-cta">
        <a href="/docs">Add your app &rarr;</a>
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
    /* Canvas cannot take a CSS variable, so theme tokens are resolved here and
       re-read on every draw. Everything below used to be literal hex — which is
       why the axis labels and the donut's centre text were near-invisible in
       light mode, and why the trend series drifted out of step with the palette. */
    function cssColor(v) {
      const m = /^var\\((--[\\w-]+)\\)$/.exec(v);
      return m ? tok(m[1], v) : v;
    }
    function tok(name, fallback) {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    }
    const COLORS = { total: 'var(--accent)', handles: 'var(--blue)', avatars: 'var(--violet)' };

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
        ctx.shadowColor = cssColor(s.color); ctx.shadowBlur = 18;
        ctx.strokeStyle = cssColor(s.color); ctx.lineWidth = 2; ctx.globalAlpha = 0.07;
        ctx.stroke(); ctx.restore();

        // medium glow
        ctx.save(); trace();
        ctx.shadowColor = cssColor(s.color); ctx.shadowBlur = 6;
        ctx.strokeStyle = cssColor(s.color); ctx.lineWidth = 1; ctx.globalAlpha = 0.15;
        ctx.stroke(); ctx.restore();

        // core line
        trace();
        ctx.strokeStyle = cssColor(s.color);
        ctx.lineWidth = 1.2; ctx.globalAlpha = 0.55;
        ctx.stroke(); ctx.globalAlpha = 1;
      }

      // x-axis
      {
        const axisY = H - pad.b;
        ctx.beginPath(); ctx.moveTo(pad.l, axisY); ctx.lineTo(W - pad.r, axisY);
        ctx.strokeStyle = tok('--line'); ctx.lineWidth = 1;
        ctx.stroke();

        ctx.font = '10px system-ui, sans-serif';
        ctx.fillStyle = tok('--dim'); ctx.textAlign = 'center';
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
            ctx.strokeStyle = tok('--line-2'); ctx.lineWidth = 1; ctx.stroke();
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
        ctx.strokeStyle = tok('--line-2');
        ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);

        const snap = snaps[hoverIdx];
        for (const s of series) {
          const y = toY(snap[s.key]);
          ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.fillStyle = tok('--panel'); ctx.globalAlpha = 0.9; ctx.fill();
          ctx.beginPath(); ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.strokeStyle = cssColor(s.color); ctx.lineWidth = 1.5; ctx.globalAlpha = 0.8;
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
    const PIE_COLORS = ${jsonScript(PIE_COLORS)};
    const traffic = ${trafficJson};
    const trafficGrandTotal = ${trafficGrandTotal};
    const pieCanvas = document.getElementById('pie');
    const pieTip = document.getElementById('pie-tip');
    let refreshPie = () => {};

    /* --- per-app traffic history: click pins a domain, hover the line for detail --- */
    const trafficSeries = ${trafficSeriesJson};     // hourly, 30d: domain -> [[hour, hits], ...]
    const trafficSeries5m = ${trafficSeries5mJson}; // 5-min, 48h: domain -> [[bucket, hits], ...]
    const trafficSeriesDaily = ${trafficSeriesDailyJson}; // daily, full history: domain -> [[day, hits], ...]
    // the same volume sliced two other ways (every request the worker answered,
    // not just search cache misses): {hourly,m5,daily} -> {endpoint,status} -> key -> [[t, hits], ...]
    const requestSeries = ${requestSeriesJson};
    const tseriesDim = document.getElementById('tseries-dim');
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
    const tseriesHint = document.getElementById('tseries-hint');
    const tsMenu = document.getElementById('tseries-menu');
    const tseriesT0 = document.getElementById('tseries-t0');
    const tseriesT1 = document.getElementById('tseries-t1');
    const TS_W = 600, TS_H = 135, TS_PAD = 6;
    const SVGNS = 'http://www.w3.org/2000/svg';
    let inspectedSource = null;
    let pinned = [];           // click-selected domains, in selection order
    let hovered = null;        // pie-hover preview; only active while nothing is pinned
    let showBaseline = true;   // the dotted all-sources line, for relative scale
    let tsView = null;         // {t0Sec, stepSec, step, toY, series[]} — the trace reads this
    // which way the volume is sliced. 'source' is the original per-app chart
    // (search cache misses, from traffic_buckets); the other two read
    // request_buckets, which counts every answer the worker gave — so their
    // total is larger and includes the ingester's own admin traffic.
    let tsDim = 'source';
    const DIMS = {
      source:   { all: 'all sources',  title: 'traffic over time',  hint: 'Tap to compare' },
      endpoint: { all: 'all requests', title: 'requests by endpoint', hint: 'Tap to toggle' },
      status:   { all: 'all requests', title: 'requests by status',   hint: 'Tap to toggle' },
    };
    const ENDPOINT_ORDER = ['search', 'getProfiles', 'request-indexing', 'page', 'admin/ingest', 'admin/delete', 'admin', 'health', 'debug', 'other'];
    // Okabe–Ito: distinguishable under deuteranopia/protanopia/tritanopia and
    // on both backgrounds. Hue is still not the only cue — status lines also
    // carry a dash pattern (below), and every tooltip row and total is named.
    const ENDPOINT_COLORS = { search: '#009E73', getProfiles: '#0072B2', 'request-indexing': '#CC79A7', page: '#56B4E9',
                              'admin/ingest': '#E69F00', 'admin/delete': '#D55E00', admin: '#8c8c8c', health: '#F0E442',
                              debug: '#b0b0b0', other: '#666666' };
    const STATUS_ORDER = ['2xx', '3xx', '4xx', '5xx'];
    const STATUS_COLORS = { '2xx': '#009E73', '3xx': '#56B4E9', '4xx': '#E69F00', '5xx': '#D55E00' };
    // solid / dotted / dashed / dash-dot: tells 4xx from 5xx without colour
    const STATUS_DASH = { '2xx': '', '3xx': '1.5 3', '4xx': '6 3', '5xx': '8 3 2 3' };
    function dimMap(stepSec) {
      const g = stepSec === 300 ? requestSeries.m5 : stepSec === 86400 ? requestSeries.daily : requestSeries.hourly;
      return (g && g[tsDim]) || {};
    }
    // every key this dim can plot, in a fixed order — the chip row is built from this
    function dimKeys() {
      const present = new Set();
      for (const g of ['hourly', 'm5', 'daily']) for (const k in ((requestSeries[g] || {})[tsDim] || {})) present.add(k);
      const order = tsDim === 'status' ? STATUS_ORDER : ENDPOINT_ORDER;
      return [...order.filter(k => present.has(k)), ...[...present].filter(k => !order.includes(k)).sort()];
    }
    function keyColor(key) {
      if (tsDim === 'source') return domainColor(key);
      if (key == null) return tok('--accent');
      const table = tsDim === 'status' ? STATUS_COLORS : ENDPOINT_COLORS;
      return table[key] || '#94a3b8';
    }

    const SPECIAL_SERIES = ${jsonScript(SPECIAL_DOMAINS)};
    const REST_KEY = ${JSON.stringify(REST_LABEL)};
    // the named sources are the pie's real slices; everything outside them is REST_KEY
    const NAMED = new Set(traffic.filter(r => !r.rest).map(r => r.domain));
    const allTraffic = ${jsonScript(d.trafficSources)};
    const sourceDomains = allTraffic.map(r => r.domain);
    function domainColor(domain) {
      if (domain == null) return tok('--accent');
      if (domain === REST_KEY) return tok('--dim', '#8a938c');
      if (SPECIAL_SERIES[domain]) return SPECIAL_SERIES[domain];
      const idx = sourceDomains.indexOf(domain);
      return idx >= 0 ? PIE_COLORS[idx % PIE_COLORS.length] : tok('--accent');
    }
    let tsRange = '7d';
    // range picks both the window and the finest data that stays readable
    const TS_RANGES = { '24h': { secs: 86400, stepSec: 300, grain: '5 min' },
                        '7d':  { secs: 604800, stepSec: 3600, grain: 'hourly' },
                        '30d': { secs: 2592000, stepSec: 3600, grain: 'hourly' },
                        // no window and no cap: daily rollups are kept forever
                        // (resolution decays with age, history never drops)
                        'all': { secs: null, stepSec: 86400, grain: 'daily' } };
    // KV materializes hourly; until the first run ships traffic_daily, "all"
    // falls back to the hourly series so the chart is never empty
    if (Object.keys(trafficSeriesDaily).length === 0) {
      TS_RANGES['all'] = { secs: 2592000, stepSec: 3600, grain: 'hourly' };
    }
    function seriesFor(domain, stepSec) {
      const source = tsDim !== 'source' ? dimMap(stepSec)
                   : stepSec === 300 ? trafficSeries5m
                   : stepSec === 86400 ? trafficSeriesDaily : trafficSeries;
      if (domain != null && domain !== REST_KEY) return source[domain] || [];
      // null sums every source; REST_KEY sums every source the pie does not name
      const sums = new Map();
      for (const dom in source) {
        if (domain === REST_KEY && NAMED.has(dom)) continue;
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
      const all = DIMS[tsDim].all;
      if (pinned.length === 0) {
        return hovered != null && tsDim === 'source'
          ? [{ domain: hovered, name: hovered, color: domainColor(hovered) }]
          : [{ domain: null, name: all, color: keyColor(null) }];
      }
      const specs = pinned.map(d => ({ domain: d, name: d, color: keyColor(d) }));
      if (showBaseline) specs.unshift({ domain: null, name: all, color: 'var(--dim)', baseline: true });
      return specs;
    }
    function showSeries() {
      refreshPie();
      if (!tseriesLines) return;
      const cfg = TS_RANGES[tsRange];
      const nowUnit = Math.floor(Date.now() / 1000 / cfg.stepSec);
      const minUnit = cfg.secs == null ? -Infinity : nowUnit - cfg.secs / cfg.stepSec;
      const specs = activeSpecs();
      const raws = specs.map(s => seriesFor(s.domain, cfg.stepSec).filter(([t]) => t >= minUnit));
      // Deliberately short: what is plotted is named by the chips, and the true
      // span is printed on the axis below. This used to append the domain, "all
      // sources", and "· last 30d" — enough text to overflow the header on a
      // phone to say what two other elements already said.
      if (tseriesLabel) tseriesLabel.textContent = cfg.grain + ' traffic';
      if (tseriesHint) tseriesHint.textContent = pinned.length ? '× removes a line' : DIMS[tsDim].hint;
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
        else if (tsDim === 'status' && STATUS_DASH[s.domain]) line.setAttribute('stroke-dasharray', STATUS_DASH[s.domain]);
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
    const brandNamespaces = { 'plyr.fm': 'fm.plyr', 'bsky38.com': 'com.bsky38', 'marque.at': 'at.marque', 'standard-reader.app': 'app.standard-reader', 'userinput.app': 'app.userinput', 'youandme.at': 'at.youandme' };
    const failedLogos = new Set();
    function sourceIconUrls(domain) {
      if (!domain || !/^[a-z0-9-]+(?:\\.[a-z0-9-]+)+$/i.test(domain)) return null;
      if (/\\.(?:localhost|local|internal|test|invalid|ts\\.net)$/i.test(domain) || /^[0-9.]+$/.test(domain)) return null;
      const favicon = domain === 'northsky.app' ? 'https://northsky.app/static/apple-touch-icon.png' : 'https://' + domain + '/favicon.ico';
      const namespace = brandNamespaces[domain];
      return [namespace ? 'https://brand.waow.tech/xrpc/tech.waow.brand.getIcon?namespace=' + encodeURIComponent(namespace) : favicon, favicon];
    }
    function sourceLogo(domain) {
      const urls = sourceIconUrls(domain);
      if (!urls) return null;
      const slot = document.createElement('span');
      slot.className = 'source-logo';
      slot.style.setProperty('--logo-color', domainColor(domain));
      const img = document.createElement('img');
      img.alt = ''; img.width = 14; img.height = 14;
      slot.append(img);
      if (failedLogos.has(domain)) return slot;
      img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
      const [primary, favicon] = urls;
      img.src = primary;
      img.addEventListener('load', () => { img.style.visibility = 'visible'; slot.classList.add('loaded'); }, { once: true });
      img.addEventListener('error', () => { if (img.src !== favicon) img.src = favicon; else failedLogos.add(domain); });
      return slot;
    }
    const sourceButtons = [...document.querySelectorAll('#source-list button[data-domain]')];
    let sourceLimit = 12;
    for (const button of sourceButtons) {
      const logo = sourceLogo(button.dataset.domain);
      if (logo) button.prepend(logo);
      else { const dot = document.createElement('span'); dot.className = 'ldot'; dot.style.background = domainColor(button.dataset.domain); button.prepend(dot); }
      button.addEventListener('click', () => { if (tsDim !== 'source') setDim('source'); applySelection(button.dataset.domain); });
    }
    function renderSources() {
      if (!sourceButtons.length) return;
      const query = document.getElementById('source-search').value.trim().toLowerCase();
      let shown = 0;
      for (const [i, button] of sourceButtons.entries()) {
        button.hidden = query ? !button.dataset.domain.toLowerCase().includes(query) : i >= sourceLimit;
        if (!button.hidden) shown++;
        button.setAttribute('aria-pressed', String(tsDim === 'source' && pinned.includes(button.dataset.domain)));
      }
      document.getElementById('source-count').textContent = query ? shown + ' matches' : shown + ' of ' + sourceButtons.length + ' apps';
      const toggle = document.getElementById('source-toggle');
      toggle.hidden = !!query || sourceButtons.length <= 12;
      toggle.textContent = sourceLimit > 12 ? 'Show fewer' : 'Show all';
      toggle.setAttribute('aria-expanded', String(sourceLimit > 12));
      document.getElementById('source-empty').hidden = shown > 0;
      document.getElementById('source-more').hidden = !!query || shown === sourceButtons.length;
      document.getElementById('source-more').textContent = 'show ' + Math.min(50, sourceButtons.length - shown) + ' more';
      document.getElementById('source-all').hidden = !!query || shown === sourceButtons.length;
      document.getElementById('source-less').hidden = !!query || sourceLimit <= 12;
    }
    document.getElementById('source-search')?.addEventListener('input', renderSources);
    document.getElementById('source-toggle')?.addEventListener('click', () => { sourceLimit = sourceLimit > 12 ? 12 : sourceButtons.length; renderSources(); });
    document.getElementById('source-more')?.addEventListener('click', () => { sourceLimit += 50; renderSources(); });
    document.getElementById('source-all')?.addEventListener('click', () => { sourceLimit = sourceButtons.length; renderSources(); });
    document.getElementById('source-less')?.addEventListener('click', () => { sourceLimit = 12; renderSources(); });
    function renderTotals() {
      renderSources();
      if (!tseriesChips || !tseriesTotals) return;
      const scrollLeft = tseriesChips.scrollLeft;
      const existing = new Map([...tseriesChips.children].map(el => [el.dataset.key, el]));
      const cfg = TS_RANGES[tsRange];
      const since = cfg.secs == null ? -Infinity : Math.floor(Date.now() / 1000 / cfg.stepSec) - cfg.secs / cfg.stepSec;
      const sumFor = domain => seriesFor(domain, cfg.stepSec)
        .filter(([t]) => tsView ? t >= tsView.t0Sec / tsView.stepSec && t < tsView.t0Sec / tsView.stepSec + tsView.series[0].counts.length : t >= since)
        .reduce((sum, [, hits]) => sum + hits, 0);
      const all = sumFor(null);
      const keys = tsDim === 'source' ? [...new Set([...pinned, ...(hovered ? [hovered] : [])])] : dimKeys();
      const rows = [{ domain: null, name: DIMS[tsDim].all }, ...keys.map(domain => ({ domain, name: domain }))];
      for (const row of rows) {
        const active = row.domain == null ? (pinned.length === 0 ? !hovered : showBaseline) : pinned.includes(row.domain) || hovered === row.domain;
        const key = tsDim + ':' + (row.domain == null ? '__total' : row.domain);
        let button = existing.get(key);
        if (!button) {
          button = document.createElement('button');
          button.type = 'button'; button.dataset.key = key;
          const name = document.createElement('span'); name.className = 'series-name';
          const dot = document.createElement('span'); dot.className = 'ldot';
          dot.style.background = keyColor(row.domain);
          name.append(dot);
          if (tsDim === 'source') { const logo = sourceLogo(row.domain); if (logo) { name.append(logo); dot.style.display = 'none'; } }
          const label = document.createElement('span'); label.textContent = row.domain == null ? 'total' : row.name; name.append(label);
          const check = document.createElement('span'); check.className = 'series-check';
          button.append(name, check);
          button.addEventListener('click', () => {
            hovered = null;
            if (row.domain == null) {
              if (!pinned.length) return;
              showBaseline = !showBaseline; showSeries(); syncUrl();
            } else applySelection(row.domain);
          });
          tseriesChips.appendChild(button);
        }
        existing.delete(key);
        button.className = 'ts-chip' + (active ? ' selected' : '');
        button.setAttribute('aria-pressed', String(active));
        button.setAttribute('aria-label', (row.domain == null && !pinned.length ? '' : active ? 'Hide ' : 'Show ') + row.name);
        const sum = sumFor(row.domain);
        const share = all > 0 ? (sum / all * 100).toFixed(1) + '%' : '—';
        button.title = row.name + ': ' + sum.toLocaleString() + ' requests (' + share + ')';
        button.querySelector('.series-check').textContent = active && (row.domain != null || pinned.length) ? '×' : active ? '' : '+';
        const dot = button.querySelector('.ldot');
        dot.style.background = row.domain == null && pinned.length ? 'transparent' : keyColor(row.domain);
        dot.style.border = row.domain == null && pinned.length ? '1px dashed var(--muted)' : '';
      }
      for (const button of existing.values()) button.remove();
      tseriesChips.scrollLeft = scrollLeft;
      const ranked = keys.filter(k => pinned.includes(k)).map(name => ({ name, sum: sumFor(name) })).sort((a, b) => b.sum - a.sum);
      tseriesTotals.replaceChildren();
      for (const row of [...activeSpecs()].sort((a, b) => Number(a.domain == null) - Number(b.domain == null))) {
        const sum = sumFor(row.domain);
        const share = all > 0 ? (sum / all * 100).toFixed(1) : '0';
        const item = document.createElement('span'); item.className = 'series-total'; item.tabIndex = 0;
        item.title = row.name + ': ' + sum.toLocaleString() + ' requests (' + share + '%)';
        item.setAttribute('aria-label', item.title);
        const dot = document.createElement('span'); dot.className = 'ldot'; dot.style.background = keyColor(row.domain);
        if (row.domain == null && pinned.length) { dot.style.background = 'transparent'; dot.style.border = '1px dashed var(--muted)'; }
        item.append(dot, sum.toLocaleString() + ' (' + share + '%)');
        tseriesTotals.append(item);
      }
      if (ranked.length >= 2 && ranked[1].sum > 0) {
        const ratio = ranked[0].sum / ranked[1].sum;
        const comparison = document.createElement('span'); comparison.className = 'series-total';
        comparison.textContent = ranked[0].name + ' ' + (ratio < 10 ? ratio.toFixed(2) : Math.round(ratio)) + '× ' + ranked[1].name;
        tseriesTotals.append(comparison);
      }
      tseriesTotals.title = (tsDim === 'source' ? 'Search cache misses' : 'All answered requests') + ' in the plotted window. Requests, not people. Apps call at different rates.';
      const clear = document.getElementById('clear-series');
      clear.disabled = tsDim === 'source' && pinned.length === 0;
      clear.textContent = tsDim !== 'source' && pinned.length === 0 ? 'select all' : 'clear';
    }
    document.getElementById('clear-series').addEventListener('click', () => {
      hovered = null;
      if (tsDim !== 'source' && pinned.length === 0) { pinned = dimKeys(); showBaseline = true; showSeries(); syncUrl(); }
      else applySelection(null);
    });

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
        ? rank[0].name + ' ' + (r => r < 10 ? r.toFixed(2) : Math.round(r))(rank[0].sum / rank[1].sum)
          + '× ' + rank[1].name + ' by request volume'
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
      g.fillText('typeahead · ' + DIMS[tsDim].title, PAD, 36);
      g.font = '11px ' + font;
      g.fillStyle = v('--dim');
      g.fillText(tseriesLabel.textContent.replace(DIMS[tsDim].title + ' · ', '')
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
      if (tsDim === 'source') p.delete('view'); else p.set('view', tsDim);
      // pinned keys live under 'apps' for sources (shared links predate the other
      // views) and 'sel' otherwise, so a link never restores an endpoint as an app
      p.delete('apps'); p.delete('sel');
      if (tsDim === 'source') {
        if (pinned.length) p.set('apps', pinned.join(','));
      } else {
        // default (everything on) is the bare URL; '-' is the one state that
        // needs a marker: nothing pinned, just the total
        const keys = dimKeys();
        const allOn = pinned.length === keys.length && keys.every(k => pinned.includes(k));
        if (pinned.length === 0) p.set('sel', '-');
        else if (!allOn) p.set('sel', pinned.join(','));
      }
      if (tsRange === '7d') p.delete('range'); else p.set('range', tsRange);
      if (pinned.length && !showBaseline) p.set('total', '0'); else p.delete('total');
      const q = p.toString();
      history.replaceState(null, '', location.pathname + (q ? '?' + q : '') + location.hash);
    }
    // Only domains we actually hold a series for. A link naming a domain that
    // has since aged out of traffic_buckets would otherwise render a chip
    // pointing at an empty line.
    const KNOWN = new Set([...Object.keys(trafficSeries), ...Object.keys(trafficSeries5m), ...Object.keys(trafficSeriesDaily), REST_KEY]);
    window.__restoreView = () => {
      const p = BOOT;
      const view = p.get('view');
      if (DIMS[view] && view !== 'source') {
        tsDim = view;
        setActiveDimBtn();
        const keys = dimKeys();
        const sel = p.get('sel');
        pinned = sel == null ? keys.slice()
               : sel === '-' ? []
               : [...new Set(sel.split(',').map(s => s.trim()).filter(s => keys.includes(s)))];
        showBaseline = p.get('total') !== '0';
        showSeries();
        syncUrl();
        return;
      }
      const apps = (p.get('apps') || '').split(',').map(s => s.trim()).filter(s => KNOWN.has(s));
      if (!apps.length) { syncUrl(); return; }
      pinned = [...new Set(apps)];
      for (const app of pinned) sourceLimit = Math.max(sourceLimit, sourceButtons.findIndex(b => b.dataset.domain === app) + 1);
      showBaseline = p.get('total') !== '0';

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
    /* Every tap TOGGLES. No modes, no modifier keys.
       This was "plain click replaces, shift-click adds", with a "compare" button
       standing in for shift on touch. It failed for the reason modes usually
       fail: the indicator sat in the chart header while the thing you tap is a
       screen-length below it, so you could not see which mode you were in — and
       re-tapping a selected source silently removed it. Toggling always is what
       a chart legend does everywhere else, it needs nothing held or discovered,
       and every accumulation is visible in the chips and one tap to undo. */
    const tseriesWrap = document.querySelector('.traffic-series');
    function setActiveDimBtn() {
      if (tseriesWrap) tseriesWrap.dataset.dim = tsDim;
      if (!tseriesDim) return;
      tseriesDim.querySelectorAll('button[data-dim]').forEach(b => b.classList.toggle('active', b.dataset.dim === tsDim));
    }
    function setDim(dim) {
      if (!DIMS[dim] || dim === tsDim) return;
      tsDim = dim;
      // the picker views start with every line ON — the first thing you see is
      // the whole breakdown, and 'clear' is one tap if you want to pick instead
      inspectedSource = null;
      pinned = dim === 'source' ? [] : dimKeys(); hovered = null; showBaseline = true;
      setActiveDimBtn();
      showSeries();
      syncUrl();
    }
    if (tseriesDim) tseriesDim.addEventListener('click', e => {
      const b = e.target.closest('button[data-dim]');
      if (b) setDim(b.dataset.dim);
    });
    function applySelection(domain) {
      if (domain == null) { pinned = []; inspectedSource = null; }
      else if (pinned.includes(domain)) {
        pinned = pinned.filter(d => d !== domain);
        if (inspectedSource === domain) inspectedSource = null;
      } else { pinned = [...pinned, domain]; inspectedSource = domain; }
      if (pinned.length === 0) showBaseline = true;  // fresh start next time
      showSeries();
      syncUrl();
    }
    function showSourceDetail(row, total, count, scoped) {
      const detail = document.getElementById('source-detail'); detail.replaceChildren();
      const name = document.createElement('span');
      name.className = row ? 'detail-name' : 'detail-total';
      name.textContent = row ? row.domain : fmtNum(total);
      if (row) name.title = row.domain;
      detail.append(name);
      const counts = document.createElement('span'); counts.className = 'detail-count';
      counts.textContent = row ? row.hits.toLocaleString() + ' requests' : count + ' apps'; detail.append(counts);
      const share = document.createElement('span'); share.className = 'detail-count';
      const percent = row ? row.hits / trafficGrandTotal * 100 : 0;
      share.textContent = row ? (percent > 0 && percent < 0.1 ? '<0.1' : percent.toFixed(1)) + '% of all traffic' : scoped ? (total / trafficGrandTotal * 100).toFixed(2) + '% of all traffic' : 'all-time requests'; detail.append(share);
      if (row && /^[a-z0-9-]+(?:\\.[a-z0-9-]+)+$/i.test(row.domain)) {
        const link = document.createElement('a'); link.href = 'https://' + row.domain;
        link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Visit website ↗'; detail.append(link);
      }
    }

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
        // always named: a lone dot beside a number says nothing about WHICH line
        // it is, and the user reading this may not see the dot's colour at all
        txt.textContent = r.name + ' — ' + r.c + (r.c === 1 ? ' hit' : ' hits');
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

    if (pieCanvas && allTraffic.length > 0) {
      const rootRows = allTraffic.filter(row => row.hits > 0);
      const rootSum = rootRows.reduce((sum, row) => sum + row.hits, 0);
      if (trafficGrandTotal > rootSum) rootRows.push({domain: 'unattributed remainder', hits: trafficGrandTotal - rootSum});
      const stack = [{ rows: rootRows, title: 'All sources' }];
      let view = [];
      function svgNode(name, attrs = {}, text) {
        const el = document.createElementNS(SVGNS, name);
        for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
        if (text != null) el.textContent = text;
        return el;
      }
      function activateOnTap(el, action) {
        let down = null;
        el.addEventListener('pointerdown', e => { if (e.isPrimary && e.button === 0) down = {id: e.pointerId, x: e.clientX, y: e.clientY}; });
        el.addEventListener('pointercancel', () => { down = null; });
        el.addEventListener('pointerup', e => {
          const start = down; down = null;
          if (start && start.id === e.pointerId && Math.hypot(e.clientX - start.x, e.clientY - start.y) <= 10) action();
        });
        el.addEventListener('click', e => { if (e.detail === 0) action(); });
        el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); action(); } });
      }
      const point = (r, a) => [200 + Math.cos(a) * r, 200 + Math.sin(a) * r];
      function arcPath(r, a, b, reverse = false) {
        const first = point(r, reverse ? b : a), last = point(r, reverse ? a : b);
        return 'M ' + first.join(' ') + ' A ' + r + ' ' + r + ' 0 ' + (b - a > Math.PI ? 1 : 0) + ' ' + (reverse ? 0 : 1) + ' ' + last.join(' ');
      }
      let renderedWidth = 0;
      function renderPie() {
        renderedWidth = pieCanvas.clientWidth;
        const rows = stack[stack.length - 1].rows;
        const total = rows.reduce((sum, row) => sum + row.hits, 0);
        const tail = rows.slice(12);
        view = rows.slice(0, 12).map(row => ({...row, rest: false}));
        if (tail.length) view.push({domain: 'Smaller sources', hits: tail.reduce((sum, row) => sum + row.hits, 0), rest: true, rows: tail});
        pieCanvas.dataset.total = total;
        pieCanvas.dataset.depth = stack.length - 1;
        pieCanvas.replaceChildren();
        let angle = -Math.PI / 2;
        view.forEach(row => {
          const start = angle;
          const end = angle + row.hits / total * Math.PI * 2 - 0.00001;
          angle = end + 0.00001;
          const mid = (start + end) / 2;
          const outer = 190, inner = 133;
          const group = svgNode('g', {class: 'pie-segment' + (row.rest ? ' tail' : ''), tabindex: 0, role: 'button', 'data-domain': row.domain, 'data-hits': row.hits, 'data-rest': row.rest, 'data-angle': mid,
            'aria-label': row.rest ? 'Zoom into ' + row.rows.length + ' smaller sources, ' + row.hits.toLocaleString() + ' hits' : row.domain + ', ' + row.hits.toLocaleString() + ' hits, ' + (row.hits / total * 100).toFixed(1) + '% of this view',
            ...(row.rest ? {} : {'aria-pressed': String(tsDim === 'source' && pinned.includes(row.domain))})});
          const path = arcPath(outer, start, end) + ' L ' + point(inner, end).join(' ') + ' A ' + inner + ' ' + inner + ' 0 ' + (end - start > Math.PI ? 1 : 0) + ' 0 ' + point(inner, start).join(' ') + ' Z';
          group.append(svgNode('path', {d: path, class: 'pie-face', fill: row.rest ? 'var(--line-2)' : domainColor(row.domain), opacity: tsDim === 'source' && pinned.length && !pinned.includes(row.domain) && !row.rest ? 0.72 : 1}));
          group.append(svgNode('title', {}, group.getAttribute('aria-label')));
          const scale = renderedWidth / 400;
          const iconPixels = Math.min(24, (outer - inner) * scale * 0.6);
          const size = iconPixels / scale;
          const radius = (outer + inner) / 2;
          const clearance = Math.min((outer - inner) / 2, radius * Math.sin(Math.min((end - start) / 2, Math.PI / 2)));
          if (!row.rest && iconPixels >= 18 && size / Math.SQRT2 + 2 / scale <= clearance) {
            const urls = sourceIconUrls(row.domain);
            if (urls) {
              const [x, y] = point(radius, mid);
              const icon = svgNode('g', {class: 'pie-logo', 'aria-hidden': 'true', opacity: 0});
              icon.append(svgNode('rect', {x: x - size / 2, y: y - size / 2, width: size, height: size, rx: 4 / scale, fill: 'var(--panel)'}));
              const img = svgNode('image', {href: urls[0], x: x - size / 2 + 1 / scale, y: y - size / 2 + 1 / scale, width: size - 2 / scale, height: size - 2 / scale, preserveAspectRatio: 'xMidYMid meet'});
              img.addEventListener('load', () => icon.setAttribute('opacity', '1'));
              img.addEventListener('error', () => { if (img.getAttribute('href') !== urls[1]) img.setAttribute('href', urls[1]); else icon.remove(); });
              icon.append(img); group.append(icon);
            }
          }

          const activate = () => {
            if (row.rest) { stack.push({rows: row.rows, title: row.rows.length + ' sources'}); renderPie(); }
            else { if (tsDim !== 'source') setDim('source'); applySelection(row.domain); }
          };
          activateOnTap(group, activate);
          pieCanvas.append(group);
        });
        const back = document.getElementById('pie-back');
        back.hidden = stack.length === 1;
        back.onclick = () => { stack.splice(1); renderPie(); document.getElementById('pie-explore').focus({preventScroll: true}); };
        const explore = document.getElementById('pie-explore');
        explore.hidden = !tail.length;
        explore.textContent = 'Explore ' + tail.length + ' more →';
        explore.setAttribute('aria-label', 'Zoom into the remaining ' + tail.length + ' apps');
        explore.onclick = () => { stack.push({rows: tail, title: tail.length + ' sources'}); renderPie(); };
        const breadcrumb = document.getElementById('pie-breadcrumb'); breadcrumb.replaceChildren();
        breadcrumb.hidden = stack.length === 1;
        stack.forEach((level, i) => {
          const button = document.createElement('button'); button.textContent = (i ? '› ' : '') + level.title;
          if (i === stack.length - 1) button.setAttribute('aria-current', 'location');
          button.addEventListener('click', () => { stack.splice(i + 1); renderPie(); }); breadcrumb.append(button);
        });
        refreshPie();
      }
      refreshPie = () => {
        const rows = stack[stack.length - 1].rows;
        const row = tsDim === 'source' ? rows.find(r => r.domain === inspectedSource) : null;
        showSourceDetail(row, rows.reduce((sum, r) => sum + r.hits, 0), rows.length, stack.length > 1);
        pieCanvas.querySelectorAll('.pie-segment[data-rest="false"]').forEach(group => {
          const selected = tsDim === 'source' && pinned.includes(group.dataset.domain);
          group.setAttribute('aria-pressed', String(selected));
          group.querySelector('.pie-face').setAttribute('opacity', tsDim === 'source' && pinned.length && !selected ? '0.72' : '1');
        });
      };
      renderPie();
      new ResizeObserver(() => { if (Math.abs(pieCanvas.clientWidth - renderedWidth) > 1) renderPie(); }).observe(pieCanvas);
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
    const cachedLatency = document.getElementById('cached-latency');
    const coldLatency = document.getElementById('cold-latency');

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
      document.getElementById('activity-period').textContent = label;
      const cold = searches > 0 ? totalMs / searches : 0;
      const cached = cacheHits > 0 ? cacheMs / cacheHits : 0;
      cachedLatency.innerHTML = (cacheHits > 0 ? cached.toFixed(1) : '—') + ' <small>ms</small>';
      coldLatency.innerHTML = (searches > 0 ? cold.toFixed(0) : '—') + ' <small>ms</small>';

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
