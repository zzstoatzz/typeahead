import { FAVICON } from "../types";
import { escHtml as esc } from "../utils";
import { THEME_CSS, THEME_HEAD, THEME_SCRIPT } from "./theme";
import { reportProviders, cacheState, fallback, mean, pairs, summarize, type Observation, type Report } from "../eval/model";
import { DIMENSIONS, score, weightsFrom } from "../eval/score";
import type { RunEntry } from "../eval/archive";
import { EVAL_TIMING_CSS, evalTiming } from "./eval-timing";
import { EVAL_METHOD_CSS, evalMethod } from "./eval-method";

const number = (value: number | null, suffix = "") => value === null ? "—" : `${value.toLocaleString("en-US", { maximumFractionDigits: 1 })}${suffix}`;
const names = { typeahead: "typeahead", bluesky: "Bluesky", tern: "Tern" };

function comparisonChart(observations: Observation[], metric: string): string {
  const providers = reportProviders({ observations });
  const rowHeight = providers.length * 22 + 14;
  const queries = [...new Set(observations.map(o => o.q))];
  const data = queries.map(q => ({ q, values: providers.map(provider => {
    const samples = observations.filter(o => o.q === q && o.provider === provider && o.outcome === "ok");
    return { provider, n: samples.length, value: mean(samples.flatMap(o => metric === "results" ? [o.actors.length] : o.ms === null ? [] : [o.ms])) };
  }) }));
  const max = metric === "results" ? 10 : Math.max(1, ...data.flatMap(row => row.values.map(v => v.value ?? 0)));
  const height = Math.max(130, queries.length * rowHeight + 48);
  return `<div class="chart-scroll"><svg class="chart" viewBox="0 0 880 ${height}" role="img" aria-labelledby="chart-title chart-desc">
    <title id="chart-title">${metric === "results" ? "Average result count" : "Average successful response time"} by query</title>
    <desc id="chart-desc">One bar per provider for each query. Missing observations are marked with a dash. Exact values and results follow in the query details.</desc>
    ${[0, .25, .5, .75, 1].map(f => `<line class="grid-line" x1="190" x2="190" y1="26" y2="${height - 10}" transform="translate(${f * 580},0)"/><text class="axis" x="${190 + f * 580}" y="15" text-anchor="middle">${number(max * f)}${metric === "results" ? "" : "ms"}</text>`).join("")}
    ${data.map((row, i) => `<text class="query-label" x="175" y="${i * rowHeight + 52}" text-anchor="end">${esc(row.q)}</text>
      ${row.values.map((v, j) => {
        const width = v.value === null ? 0 : 580 * v.value / max;
        const y = i * rowHeight + 30 + j * 22;
        return `<g><title>${esc(row.q)} · ${names[v.provider]}: ${number(v.value, metric === "results" ? " results" : " ms")} · ${v.n} successful samples</title>
          <rect class="${v.provider}-fill" x="190" y="${y}" width="${width}" height="16" rx="2"/>
          <text class="value-label" x="${198 + width}" y="${y + 13}">${number(v.value)}</text></g>`;
      }).join("")}`).join("")}
    </svg></div>`;
}

function queryDetails(observations: Observation[]): string {
  const providers = reportProviders({ observations });
  return [...new Set(observations.map(o => o.q))].map(q => {
    const rows = observations.filter(o => o.q === q);
    return `<details class="query-detail"><summary><strong>${esc(q)}</strong><span>${esc(rows[0].category)} · ${rows.filter(o => o.outcome !== "pending").length} observations</span></summary>
      <div class="result-columns">${providers.map(provider => `<section><h3 class="${provider}-text">${names[provider]}</h3>
        ${rows.filter(o => o.provider === provider).map(o => `<div class="result-sample"><p>pass ${o.round + 1} · ${number(o.ms, " ms")} · ${esc(o.outcome)}${o.status === null ? "" : ` · HTTP ${o.status}`} · cache ${cacheState(o)}${fallback(o) ? " · Bluesky fallback" : ""}</p>
          <p class="dim">${new Date(o.startedAt).toISOString()}${o.headers["x-typeahead-engine"] ? ` · engine ${esc(o.headers["x-typeahead-engine"])}` : ""}</p>
          ${o.outcome === "ok" ? (o.actors.length ? `<ol>${o.actors.map(actor => `<li><a href="https://aturi.to/${esc(encodeURIComponent(actor.did).replaceAll("%3A", ":"))}" target="_blank" rel="noopener noreferrer">${esc(actor.handle)}</a>${actor.displayName ? `<span>${esc(actor.displayName)}</span>` : ""}</li>`).join("")}</ol>` : "<p>no results</p>") : `<p>${esc(o.error ?? "request reserved; no completed observation yet")}</p>`}
        </div>`).join("") || "<p>no observation</p>"}</section>`).join("")}</div></details>`;
  }).join("");
}

export function evalPage(report: Report | null, params = new URLSearchParams(), runs: RunEntry[] = []): string {
  const locationLabel = (location: string) => location === "Cloudflare Workers (ENAM placement hint)" ? "Cloudflare Workers" : location;
  if (report) report = { ...report, location: locationLabel(report.location) };
  runs = runs.map(run => ({ ...run, location: locationLabel(run.location) }));
  const state = new URLSearchParams(params);
  if (report) state.set("run", report.run);
  const weights = weightsFrom(params);
  const observations = report?.observations ?? [];
  const providers = reportProviders(report);
  const result = score(observations, weights, providers);
  const summaries = providers.map(p => summarize(observations.filter(o => o.provider === p)));
  const link = (view: string) => { const next = new URLSearchParams(state); next.set("view", view); return `/eval?${esc(next.toString())}#${view}`; };
  const hidden = (except: string[]) => [...state].filter(([key]) => !except.includes(key)).map(([key, v]) => `<input type="hidden" name="${esc(key)}" value="${esc(v)}">`).join("");
  const open = (view: string) => params.get("view") === view ? " open" : "";
  const selected = (x: string, y: string) => x === y ? " selected" : "";
  const category = params.get("category") ?? "all";
  const pass = params.get("pass") ?? "all";
  const metric = params.get("metric") === "results" ? "results" : "latency";
  const inspected = observations.filter(o => (category === "all" || o.category === category) && (pass === "all" || String(o.round + 1) === pass));
  const queries = report?.queries ?? [...new Map(observations.map(o => [o.q, { q: o.q, category: o.category }])).values()];
  const categories = [...new Set(queries.map(q => q.category))];
  const matched = pairs(observations);
  const overlap = mean(matched.flatMap(p => p.overlap === null ? [] : [p.overlap]));
  const completed = observations.filter(o => o.outcome !== "pending").length;
  const medianRow = ["median response", ...summaries.map(s => number(s.medianMs, " ms"))];
  const labelsRow = ["labels coverage", ...summaries.map(s => report?.actorFieldsVersion ? number(s.labelsPct, "%") : "not captured")];
  const verificationRow = ["verification coverage", ...summaries.map(s => report?.actorFieldsVersion ? number(s.verificationPct, "%") : "not captured")];
  const primaryRows = [
    medianRow,
    ["successful requests", ...summaries.map(s => `${s.ok} / ${s.completed}`)],
    ["exact handle first", ...summaries.map(s => number(s.exactPct, "%"))],
    labelsRow,
    verificationRow,
  ];
  const detailRows = [
    medianRow,
    ["mean response", ...summaries.map(s => number(s.averageMs, " ms"))],
    ["p90 response", ...summaries.map(s => number(s.p90Ms, " ms"))],
    ["successful / completed", ...summaries.map(s => `${s.ok} / ${s.completed}`)],
    ["failed / pending", ...summaries.map(s => `${s.failures} / ${s.pending}`)],
    ["exact handles ranked first", ...summaries.map(s => `${number(s.exactPct, "%")} · ${s.exactCount} samples`)],
    ["empty answers", ...summaries.map(s => number(s.emptyPct, "%"))],
    ["mean results", ...summaries.map(s => number(s.results))],
    ["display names / actor appearances", ...summaries.map(s => number(s.displayPct, "%"))],
    ["avatar URLs / actor appearances", ...summaries.map(s => number(s.avatarPct, "%"))],
    labelsRow,
    ["nonempty labels", ...summaries.map(s => report?.actorFieldsVersion ? number(s.labeledPct, "%") : "not captured")],
    verificationRow,
    ["verified accounts", ...summaries.map(s => report?.actorFieldsVersion ? number(s.verifiedPct, "%") : "not captured")],
    ["account creation date", ...summaries.map(s => report?.actorFieldsVersion ? number(s.createdAtPct, "%") : "not captured")],
    ["cache hit / miss / unknown", ...summaries.map(s => `${s.hits} / ${s.misses} / ${s.unknownCache}`)],
  ];
  const dataUrl = `/eval/data.json${report ? `?run=${encodeURIComponent(report.run)}` : ""}`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>eval · actor search</title><meta name="robots" content="noindex">${FAVICON}${THEME_HEAD}<style>${THEME_CSS}
  .container{max-width:1040px;margin:auto;padding:16px 24px 40px}header,.toolbar,.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
  header{border-bottom:1px solid var(--line);padding-bottom:8px}header nav{display:flex;gap:16px;align-items:center}header a{text-decoration:none;color:var(--muted)}a{color:var(--accent);text-underline-offset:3px}a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  h1{font-size:1.1rem;font-weight:500;margin:0}h2{font-size:.85rem;font-weight:500;margin:0}.toolbar{margin:16px 0 12px}.toolbar form{display:flex;gap:8px;align-items:center}label,.muted,.caption{color:var(--muted);font-size:.75rem}
  select,input,button{font:inherit;font-size:16px;color:var(--text);background:var(--panel);border:1px solid var(--line-2);border-radius:5px;min-height:44px;padding:8px 10px}button,summary{cursor:pointer}select{max-width:100%}.theme-btn{font-size:.7rem}button:hover{border-color:var(--muted)}
  .score-panel{border:1px solid var(--line);border-radius:8px;padding:18px 20px}.section-head{margin-bottom:8px}.section-head p{margin:0;font-size:.7rem;color:var(--muted)}.verdict{color:var(--bright);font-size:.8rem}.score-row{display:grid;grid-template-columns:100px 1fr 66px;gap:16px;align-items:center;min-height:48px;text-decoration:none}.score-row strong{font-size:1.55rem;font-weight:500;text-align:right;font-variant-numeric:tabular-nums}.score-row>span:first-child{font-size:.85rem}
  .track{height:28px;background:var(--line);border-radius:3px;display:flex;overflow:hidden}.segment{height:100%;border-right:1px solid var(--bg);background:currentColor}.segment:nth-child(2){opacity:.72}.segment:nth-child(3){opacity:.42}.typeahead-text{color:var(--accent)}.bluesky-text{color:var(--blue)}.typeahead-fill{fill:var(--accent)}.bluesky-fill{fill:var(--blue)}.tern-text{color:var(--tern-color)}.tern-fill{fill:var(--tern-color)}.scale{display:flex;justify-content:space-between;margin:2px 82px 12px 116px;font-size:.65rem;color:var(--muted)}.score-caption{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:.7rem;color:var(--muted);border-top:1px solid var(--line);padding-top:12px;margin-top:10px}.score-caption a{color:var(--muted)}
  .glance-table{margin:12px 0 16px;overflow-x:auto}.glance-table td{white-space:nowrap;padding:8px 4px}.glance-table th{padding:8px 4px}.glance{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:16px 0 20px}.glance a{text-decoration:none;display:block;padding:4px 0;min-height:44px}.glance .label{display:block;font-size:.7rem;color:var(--muted);margin-bottom:5px}.glance .values{display:flex;gap:16px;font-size:1rem}.notice{color:var(--amber);font-size:.75rem;padding:8px 0}
  .drill{border-top:1px solid var(--line)}.drill>summary{padding:15px 0;min-height:50px;font-size:.85rem;list-style-position:inside}.drill>summary .hint{color:var(--muted);font-size:.7rem;margin-left:10px}.drill-content{padding:0 0 20px}table{width:100%;border-collapse:collapse;font-size:.78rem}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:right;font-weight:400}th:first-child,td:first-child{text-align:left}thead th{color:var(--muted);font-size:.7rem}td input{width:76px;text-align:right}.table-scroll{overflow-x:auto}.weight-actions{display:flex;gap:16px;align-items:center;margin-top:12px}.rules{display:grid;gap:8px;margin:16px 0;font:14px/1.5 system-ui,sans-serif;color:var(--muted)}.rules p{margin:0}.rules b{font-weight:500;color:var(--text)}
  .agreement{display:flex;gap:24px;flex-wrap:wrap;font-size:.75rem;color:var(--muted);margin:16px 0}.agreement strong{color:var(--bright)}.filters{display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:16px}.filters label{display:flex;flex-direction:column;gap:4px}.chart-scroll{overflow-x:auto;margin:16px 0}.chart{display:block;width:100%;min-width:640px}.grid-line{stroke:var(--line)}.axis,.value-label{fill:var(--muted);font-size:11px}.query-label{fill:var(--text);font-size:13px}.legend{display:flex;gap:20px;font-size:.75rem}.query-detail{border-bottom:1px solid var(--line)}.query-detail summary{padding:12px 0;min-height:44px;font-size:.8rem}.query-detail summary span{color:var(--muted);margin-left:12px;font-size:.7rem}.query-detail summary strong{font-weight:500}.result-columns{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:24px;padding:8px 0 20px}.result-columns section{min-width:0}.result-columns h3{font-size:.85rem;font-weight:500}.result-sample{font-size:.75rem;overflow-wrap:anywhere;margin:12px 0 20px}.result-sample ol{padding-left:24px}.result-sample li{padding:4px 0}.result-sample li span{display:block;color:var(--muted)}.dim{font-size:.65rem;color:var(--muted)}.method{font:14px/1.6 system-ui,sans-serif;color:var(--muted)}.method p{margin:8px 0}.empty{padding:24px 0;color:var(--muted)}footer{border-top:1px solid var(--line);padding-top:12px;margin-top:8px;display:flex;gap:16px;justify-content:space-between;font-size:.7rem;color:var(--muted)}
  @media(max-width:600px){.container{padding:12px 16px 28px}header nav{gap:10px}.toolbar{gap:6px}.toolbar form{width:100%}.toolbar select{flex:1;min-width:0;font-size:16px}.score-panel{padding:14px 12px}.score-row{grid-template-columns:78px 1fr 48px;gap:8px;min-height:56px}.score-row strong{font-size:1.3rem}.score-row>span:first-child{font-size:.75rem}.scale{margin-left:86px;margin-right:56px}.section-head{margin-bottom:6px}.glance{gap:8px;margin:12px 0}.glance .label{font-size:.65rem}.glance .values{flex-direction:column;gap:2px;font-size:.9rem}.drill>summary .hint{display:block;margin:5px 0 0 16px}.result-columns{grid-template-columns:1fr}.query-detail summary span{display:block;margin:4px 0 0 16px}.filters label{flex:1 1 100%}.filters button{width:100%}.table-scroll table{min-width:460px}#dimensions table{min-width:0}#dimensions td,#dimensions th{padding:8px 4px}#dimensions input{width:64px}.weight-actions{flex-wrap:wrap}.score-caption{font-size:.65rem}}
  .container{--tern-color:color-mix(in srgb,var(--amber) 85%,var(--text))}${EVAL_TIMING_CSS}${EVAL_METHOD_CSS}</style></head><body><main class="container">
  <header><a class="wordmark" href="/">typeahead</a><nav><a href="/stats">stats</a><a href="/docs">docs</a><button class="theme-btn" id="theme-toggle">theme: auto</button></nav></header>
  <div class="toolbar"><h1>eval</h1>${report ? `<form action="/eval" method="get">${hidden(["run", "view"])}<label for="run">run</label><select id="run" name="run">${(runs.length ? runs : [{ run: report.run, startedAt: report.startedAt, location: report.location }]).map(r => `<option value="${esc(r.run)}"${selected(r.run, report.run)}>${new Date(r.startedAt).toISOString().slice(0, 16).replace("T", " ")} UTC · ${esc(r.location)}</option>`).join("")}</select><button>view</button></form>` : ""}</div>
  ${report ? `<section class="score-panel" aria-label="aggregate benchmark comparison"><div class="section-head"><h2>score <span class="muted">/ 100</span></h2>${evalMethod(report, weights)}</div>
  ${result.providers.map(p => `<a class="score-row ${p.provider}-text" href="${link("dimensions")}" aria-label="${names[p.provider]} benchmark score ${number(p.total)}. Inspect dimensions and weights."><span>${names[p.provider]}</span><span class="track">${p.total === null ? "" : p.dimensions.map(d => `<span class="segment" style="width:${d.contribution ?? 0}%" title="${d.label}: ${number(d.contribution)} points"></span>`).join("")}</span><strong>${number(p.total)}</strong></a>`).join("")}
  <div class="scale" aria-hidden="true"><span>0</span><span>50</span><span>100</span></div><div class="score-caption"><span>${result.queries} queries · ${result.matched} matched cases · ${esc(report.location)}</span><a href="${link("dimensions")}">weights ↗</a></div></section>
  <div class="glance-table"><table><thead><tr><th></th>${providers.map(p => `<th class="${p}-text">${names[p]}</th>`).join("")}</tr></thead><tbody>${primaryRows.map(row => `<tr><th>${row[0]}</th>${row.slice(1).map(value => `<td>${value}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
  ${result.excluded ? `<p class="notice">${result.excluded} incomplete or fallback cases excluded.</p>` : ""}${summaries[0].fallbacks ? `<p class="notice">${summaries[0].fallbacks} typeahead answers came from Bluesky fallback.</p>` : ""}
  ${evalTiming(report, state)}
  <details class="drill" id="dimensions"${open("dimensions")}><summary>score &amp; weights</summary><div class="drill-content"><form action="/eval#dimensions" method="get">${hidden(["speed", "availability", "exact", "view"])}<input type="hidden" name="view" value="dimensions"><div class="table-scroll"><table><thead><tr><th>dimension</th>${providers.map(p => `<th class="${p}-text">${names[p]}</th>`).join("")}<th>weight</th></tr></thead><tbody>${DIMENSIONS.map((d, i) => `<tr><th scope="row"><label for="weight-${d.id}">${d.label}</label></th>${result.providers.map(p => `<td>${number(p.dimensions[i].points)}</td>`).join("")}<td><input id="weight-${d.id}" name="${d.id}" type="number" min="0" max="100" step="1" value="${weights[d.id]}"></td></tr>`).join("")}</tbody></table></div><div class="weight-actions"><button>apply weights</button><a href="/eval?${esc(new URLSearchParams({ run: report.run, view: "dimensions" }).toString())}#dimensions">reset weights</a><span class="muted">weights normalize to 100%</span></div></form><div class="rules">${DIMENSIONS.map(d => `<p><b>${d.label}.</b> ${d.rule}</p>`).join("")}<p>Each query has equal weight within a dimension; repeated passes are averaged first. Only completed pairs are scored. A missing weighted dimension leaves the total unscored. This measures these service checks, not overall search relevance.</p></div></div></details>
  <details class="drill" id="metrics"${open("metrics")}><summary>metrics</summary><div class="drill-content"><div class="table-scroll"><table><thead><tr><th>metric</th>${providers.map(p => `<th class="${p}-text">${names[p]}</th>`).join("")}</tr></thead><tbody>${detailRows.map(row => `<tr><th scope="row">${row[0]}</th>${row.slice(1).map(value => `<td>${value}</td>`).join("")}</tr>`).join("")}</tbody></table></div><div class="agreement"><span><strong>${number(overlap, "%")}</strong> DID overlap vs Bluesky</span><span><strong>${matched.filter(p => p.sameFirst).length} / ${matched.filter(p => p.a.actors.length && p.b.actors.length).length}</strong> same first vs Bluesky</span><span><strong>${matched.filter(p => p.a.ms !== null && p.b.ms !== null && p.a.ms < p.b.ms).length} / ${matched.length}</strong> faster than Bluesky</span></div></div></details>
  <details class="drill" id="test-set"${open("test-set")}><summary>queries <span class="hint">${completed} / ${report.expected} requests</span></summary><div class="drill-content"><form class="filters" method="get" action="/eval#test-set">${hidden(["category", "pass", "metric", "view"])}<input type="hidden" name="view" value="test-set"><label>queries<select name="category"><option value="all">all categories</option>${categories.map(c => `<option${selected(c, category)}>${esc(c)}</option>`).join("")}</select></label><label>pass<select name="pass"><option value="all">both passes</option><option value="1"${selected("1", pass)}>first pass</option><option value="2"${selected("2", pass)}>second pass</option></select></label><label>chart<select name="metric"><option value="latency"${selected("latency", metric)}>response time</option><option value="results"${selected("results", metric)}>result count</option></select></label><button>inspect queries</button></form><div class="legend">${providers.map(p => `<span class="${p}-text">${names[p]}</span>`).join("")}</div><h2>${metric === "results" ? "results per query" : "response time by query"}</h2>${inspected.length ? comparisonChart(inspected, metric) + queryDetails(inspected) : '<p class="empty">no observations match these filters</p>'}${queries.filter(q => !observations.some(o => o.q === q.q)).map(q => `<p class="caption">${esc(q.q)} · ${esc(q.category)} · not collected</p>`).join("")}</div></details>` : '<p class="empty">no sample collected yet</p>'}
  <footer><span>${runs.length || (report ? 1 : 0)} saved runs${report ? ` · ${new Date(report.startedAt).toISOString().slice(0, 10)}` : ""}</span><a href="${esc(dataUrl)}">download run ↗</a></footer>
  </main><script>${THEME_SCRIPT}</script></body></html>`;
}
