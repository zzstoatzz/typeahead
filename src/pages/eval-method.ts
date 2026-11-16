import { reportProviders, type Report } from "../eval/model";
import { DIMENSIONS, type Weights } from "../eval/score";

export const EVAL_METHOD_CSS = `
  .section-head{display:flex;align-items:center;justify-content:space-between;gap:16px;position:relative}
  .eval-method summary{list-style:none;color:var(--muted);font-size:.75rem;min-height:44px;display:flex;align-items:center}
  .eval-method summary::-webkit-details-marker{display:none}
  .eval-method summary::before{content:'+';width:1em;color:var(--accent)}
  .eval-method[open] summary::before{content:'−'}
  .eval-method summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .method-panel{position:absolute;z-index:10;right:0;top:100%;width:min(480px,calc(100vw - 56px));padding:14px 16px;background:var(--panel);border:1px solid var(--line-2);border-radius:6px;box-shadow:0 10px 28px #0003;color:var(--muted);font-size:13px}
  .method-panel dl{display:grid;grid-template-columns:70px 1fr;gap:10px 12px}
  .method-panel dt{color:var(--text)}.method-panel dd{margin:0}
  .method-panel p{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
  @media(max-width:600px){.section-head:has(.eval-method[open]){flex-wrap:wrap}.eval-method[open]{flex-basis:100%}.method-panel{position:static;width:100%;margin-bottom:12px;box-shadow:none}.method-panel dl{grid-template-columns:1fr;gap:4px}.method-panel dd+dt{margin-top:8px}}
`;

export function evalMethod(report: Report, weights: Weights): string {
  const queries = report.queries?.length ?? new Set(report.observations.map(o => o.q)).size;
  const providers = reportProviders(report).length;
  const passes = queries ? report.expected / (queries * providers) : null;
  const total = DIMENSIONS.reduce((sum, d) => sum + weights[d.id], 0);
  const composition = total ? DIMENSIONS.map(d => `${Math.round(100 * weights[d.id] / total)}% ${d.label}`).join(" · ") : "all weights are zero; no score";
  return `<details class="eval-method" id="method"><summary>method</summary><div class="method-panel"><dl>
    <dt>sample</dt><dd>${queries} ${queries === 1 ? "query" : "queries"}${passes && Number.isInteger(passes) ? ` × ${passes} passes` : ""} × ${providers} services. Up to 10 results per answer, from the same runner.</dd>
    <dt>time</dt><dd>Full response including network time. Median: the middle response. p90: 90% finished within this time. Cache warmth is not controlled.</dd>
    <dt>score</dt><dd>${composition}. Queries count equally; failures score zero. Unfinished and fallback cases are excluded. Relevance and field coverage are not scored.</dd>
    <dt>fields</dt><dd>Present fields / returned accounts, counting repeats. Empty labels count as present. Missing verification means unknown, not unverified.</dd>
    <dt>collection</dt><dd>${report.expected / providers} requests per service daily, one at a time, ≥5 seconds apart. Stops on rate limits. Page views only read saved data.</dd>
    </dl><p><a href="/eval?run=${encodeURIComponent(report.run)}&view=test-set#test-set">inspect queries</a> · <a href="https://github.com/zzstoatzz/typeahead/blob/main/docs/evaluation.md">full method</a></p></div></details>`;
}
