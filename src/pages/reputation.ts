import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT } from "./theme";
import { LABELERS } from "../labelers";

const rankingIcons = {
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  score: '<path d="M5 19V12M12 19V5M19 19V9"/>',
  exact: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
  visibility: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  moderation: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/>',
  data: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
};

function rankingIcon(name: keyof typeof rankingIcons): string {
  return `<svg class="ranking-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${rankingIcons[name]}</svg>`;
}

export function reputationPage(): string {
  const LABELER_COUNT = LABELERS.length;
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>how ranking works · typeahead</title>
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    body { display: flex; justify-content: center;
           padding: calc(1.5rem + env(safe-area-inset-top)) 1rem calc(2rem + env(safe-area-inset-bottom)); }
    .container { max-width: 720px; width: 100%; }
    .header { display: flex; align-items: baseline; justify-content: space-between; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: var(--muted); }
    .toplinks { font-size: 0.75rem; color: var(--faint); display: flex; gap: 0.45rem; }
    .toplinks a { color: var(--dim); text-decoration: none; }
    .toplinks a:hover { color: var(--muted); }
    .subtitle { font-size: 0.8rem; color: var(--dim); margin-bottom: 1.5rem; }
    a { color: var(--accent-dim); text-decoration: none; }
    a:hover { color: var(--accent); }

    form { display: flex; gap: 0.5rem; margin-bottom: 1.25rem; }
    .ta-wrap { position: relative; flex: 1; min-width: 0; }
    input {
      width: 100%; min-width: 0; padding: 0.7rem 0.9rem; background: var(--panel);
      border: 1px solid var(--line-2); border-radius: 8px; color: var(--text);
      font-size: 16px; font-family: inherit; outline: none; min-height: 48px;
      caret-color: var(--accent);
    }
    input:focus { border-color: var(--accent-dim); }
    input::placeholder { color: var(--dim); }
    button[type="submit"] {
      padding: 0.6rem 1.1rem; background: var(--panel-2); border: 1px solid var(--line-2);
      border-radius: 8px; color: var(--text); font: inherit; font-size: 0.85rem; cursor: pointer;
      min-height: 48px;
    }
    button[type="submit"]:hover { background: var(--hover); border-color: var(--accent-dim); }
    button:disabled { opacity: 0.5; cursor: default; }

    .ta-typeahead-dropdown {
      position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px; display: none; z-index: 20;
      background: var(--panel); border: 1px solid var(--line-2); border-radius: 8px;
      max-height: min(300px, 45vh); overflow-y: auto;
      box-shadow: 0 12px 32px rgba(0,0,0,0.25);
    }
    .ta-typeahead-dropdown.open { display: block; }
    .ta-typeahead-item {
      display: flex; align-items: center; gap: 0.7rem; padding: 0.6rem 0.8rem;
      min-height: 48px; cursor: pointer; font-size: 0.85rem;
    }
    .ta-typeahead-item:hover, .ta-typeahead-item.active { background: var(--hover); }
    .ta-typeahead-avatar {
      width: 32px; height: 32px; border-radius: 50%; object-fit: cover; flex: 0 0 auto;
      background: var(--panel-2);
    }
    .ta-typeahead-avatar-ph { display: inline-block; }
    .ta-typeahead-text { min-width: 0; display: flex; flex-direction: column; line-height: 1.3; }
    .ta-typeahead-handle { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ta-typeahead-name { color: var(--dim); font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .status {
      color: var(--muted); border: 1px solid var(--line); border-radius: 10px;
      background: var(--panel); padding: 0.9rem 1rem; font-size: 0.85rem;
    }
    .status .stage { display: flex; align-items: baseline; justify-content: space-between; gap: 0.6rem; }
    .status .elapsed { color: var(--faint); font-size: 0.72rem; font-variant-numeric: tabular-nums; flex-shrink: 0; }
    .status .stage-note { color: var(--dim); font-size: 0.75rem; margin-top: 0.3rem; }
    /* progress carets: same blinking block as the wordmark — waiting looks
       like typeahead thinking, not a generic ellipsis */
    .scan-caret, .btn-caret {
      display: inline-block; width: 0.5em; height: 1em; margin-left: 0.25em;
      background: var(--accent); vertical-align: text-bottom;
      animation: caret-blink 1.1s steps(1) infinite;
    }
    .btn-caret { margin-left: 0; }
    @media (prefers-reduced-motion: reduce) {
      .scan-caret, .btn-caret { animation: none; opacity: 0.5; }
    }
    .status.err { color: var(--red); border-left: 2px solid var(--red); }

    .who { display: flex; align-items: center; gap: 0.9rem; margin: 1.35rem 0 0.9rem; min-height: 56px; }
    .who-avatar {
      width: 52px; height: 52px; border-radius: 50%; flex: 0 0 auto; overflow: hidden;
      background: var(--panel-2); border: 1px solid var(--line);
      display: inline-flex; align-items: center; justify-content: center;
    }
    .who-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .ini { font-size: 20px; color: var(--muted); text-transform: uppercase; font-weight: 700; }
    .who-text { min-width: 0; }
    .who-name { color: var(--bright); font-size: 1rem; font-weight: 700; line-height: 1.25; overflow-wrap: anywhere; }
    .who-handle { color: var(--muted); font-size: 0.85rem; overflow-wrap: anywhere; }
    .who-tag { color: var(--dim); font-size: 0.72rem; margin-top: 0.15rem; overflow-wrap: anywhere; }

    /* state is a 6px dot plus the word, never a stripe, a pill or a tinted edge */
    .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--dim); vertical-align: middle; margin-right: 0.45rem;
           box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 18%, transparent); color: var(--dim); }
    .good { color: var(--accent); } .good .dot, .dot.good { background: var(--accent); color: var(--accent); }
    .warn { color: var(--amber); } .warn .dot, .dot.warn { background: var(--amber); color: var(--amber); }
    .bad { color: var(--red); } .bad .dot, .dot.bad { background: var(--red); color: var(--red); }
    .na { color: var(--muted); } .na .dot, .dot.na { background: var(--faint); color: var(--faint); }
    .kicker { color: var(--dim); font-size: 0.66rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.11em; }
    .state { font-size: 0.78rem; white-space: nowrap; }

    /* verdicts: one quiet row of three answers, divided by hairlines, no boxes */
    .verdicts { display: grid; grid-template-columns: repeat(3, 1fr); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin-bottom: 1.1rem; }
    .verdict { padding: 0.8rem 0.9rem 0.85rem; min-width: 0; }
    .verdict + .verdict { border-left: 1px solid var(--line); }
    .v-value { color: var(--bright); font-size: 1rem; font-weight: 700; line-height: 1.25; margin-top: 0.3rem; overflow-wrap: anywhere; }
    .v-sub { color: var(--muted); font-size: 0.74rem; margin-top: 0.2rem; }

    /* sections: hairline cards, closed by default, one line each */
    .secs { display: grid; gap: 0.5rem; }
    .sec { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); margin-top: 0; }
    .sec > summary { list-style: none; display: grid; grid-template-columns: 7.5rem 1fr auto; gap: 0.8rem; align-items: center; padding: 0.8rem 0.95rem; cursor: pointer; color: var(--text); font-size: 0.85rem; min-height: 48px; }
    .sec > summary::-webkit-details-marker { display: none; }
    .sec > summary:hover .sec-line { color: var(--text); }
    .sec[open] > summary { border-bottom: 1px solid var(--line); }
    .sec-line { color: var(--muted); font-size: 0.8rem; overflow-wrap: anywhere; }
    .sec-line .hi { color: var(--text); }
    .sec-end { display: flex; align-items: center; gap: 0.7rem; }
    .sec-chev { color: var(--faint); font-size: 0.7rem; transition: transform 0.15s; }
    .sec[open] .sec-chev { transform: rotate(90deg); }
    @media (prefers-reduced-motion: reduce) { .sec-chev { transition: none; } }
    .sec-body { padding: 0.95rem; }
    .card-sub { color: var(--muted); margin-top: 0.65rem; font-size: 0.8rem; }
    .card-sub .hi { color: var(--text); }
    .metric { display: flex; align-items: baseline; gap: 0.55rem; flex-wrap: wrap; }
    .metric .num {
      font-size: 2rem; line-height: 1; color: var(--accent); font-weight: 700;
      letter-spacing: -0.02em; font-variant-numeric: tabular-nums;
    }
    .metric .unit { color: var(--muted); font-size: 0.8rem; }
    .score-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-top: 0.85rem; }
    .mini { border-top: 1px solid var(--line); padding-top: 0.7rem; }
    .mini-label { color: var(--dim); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.07em; }
    .mini-value { color: var(--text); font-size: 1.05rem; font-weight: 700; font-variant-numeric: tabular-nums; }

    .bars { display: grid; gap: 0.55rem; }
    .bar-row { display: grid; grid-template-columns: 9.5rem 1fr 4.6rem; gap: 0.7rem; align-items: center; }
    .bar-label { color: var(--muted); font-size: 0.8rem; }
    .labelers { display: flex; flex-direction: column; margin-top: 0.75rem; border-top: 1px solid var(--line); }
    .labeler { display: flex; justify-content: space-between; align-items: baseline; gap: 0.75rem; padding: 0.5rem 0; border-bottom: 1px solid var(--line); }
    .lab-vals { text-align: right; font-size: 0.8rem; color: var(--amber); overflow-wrap: anywhere; }
    .lab-none { font-size: 0.75rem; color: var(--dim); }
    .bar { height: 8px; background: var(--bg); border-radius: 999px; overflow: hidden; border: 1px solid var(--line); }
    .bar span { display: block; height: 100%; min-width: 1px; background: linear-gradient(90deg, var(--accent-dim), var(--accent)); }
    .bar.penalty span { background: var(--red); opacity: 0.75; }
    .bar-val { text-align: right; color: var(--text); font-variant-numeric: tabular-nums; font-size: 0.8rem; }

    .checks { display: grid; gap: 0.55rem; }
    .check {
      display: grid; grid-template-columns: 1.2rem 1fr; gap: 0.55rem; align-items: start;
      padding-top: 0.58rem; border-top: 1px solid var(--line);
    }
    .check:first-child { padding-top: 0; border-top: none; }
    .mark { color: var(--accent); font-weight: 700; }
    .mark.bad { color: var(--red); }
    .mark.warn { color: var(--amber); }
    .check-title { color: var(--text); font-size: 0.85rem; }
    .check-note { color: var(--dim); font-size: 0.75rem; overflow-wrap: anywhere; }

    .queries { display: grid; gap: 0.55rem; }
    .query {
      display: grid; grid-template-columns: 1fr auto; gap: 0.75rem; align-items: center;
      padding-top: 0.58rem; border-top: 1px solid var(--line);
    }
    .query:first-child { padding-top: 0; border-top: none; }
    .q-name { color: var(--text); font-size: 0.85rem; overflow-wrap: anywhere; }
    .q-kind { color: var(--dim); font-size: 0.72rem; }
    .rank { color: var(--amber); font-weight: 700; font-variant-numeric: tabular-nums; }
    .rank.missing { color: var(--red); }
    details.raw { margin-top: 0.85rem; color: var(--dim); font-size: 0.8rem; }
    details.raw summary { cursor: pointer; color: var(--muted); }
    pre {
      margin-top: 0.6rem; background: var(--bg); border: 1px solid var(--line);
      border-radius: 8px; padding: 0.85rem; overflow: auto; color: var(--muted);
      font-size: 0.72rem; line-height: 1.45;
    }

    .explanation { margin: 1.6rem 0; border-bottom: 1px solid var(--line); padding-bottom: 1rem; }
    .explanation > summary { font-size: 1.1rem; color: var(--bright); cursor: pointer; padding: 0.4rem 0; }
    .ranking-facts { margin: 0.7rem 0; }
    .ranking-facts div { display: grid; grid-template-columns: 8.3rem 1fr; gap: 0.75rem; padding: 0.45rem 0; font-size: 0.85rem; line-height: 1.5; }
    .ranking-facts dt { color: var(--muted); display: flex; gap: 0.45rem; align-items: baseline; }
    .ranking-icon { width: 16px; height: 16px; flex-shrink: 0; vertical-align: -3px; color: var(--muted); }
    .sec .kicker .ranking-icon { margin-right: 0.45rem; }
    .ranking-facts dd { color: var(--text); }
    .weights summary { cursor: pointer; color: var(--muted); font-size: 0.8rem; padding: 0.6rem 0; }
    .factors div { display: grid; grid-template-columns: 7rem 1fr; gap: 0.75rem; padding: 0.65rem 0; border-bottom: 1px solid var(--line); font-size: 0.8rem; line-height: 1.5; }
    .factors dt { color: var(--bright); }
    .limits { padding-left: 1rem; margin-top: 0.8rem; font-size: 0.8rem; color: var(--muted); line-height: 1.6; }
    .limits li { margin: 0.4rem 0; }
    .inspect-heading { font-size: 0.9rem; color: var(--bright); margin-bottom: 0.7rem; }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
    @media (max-width: 560px) { .factors div { grid-template-columns: 1fr; gap: 0.2rem; } .header { flex-wrap: wrap; } .sec > summary { grid-template-columns: 1fr auto; } .sec-line { grid-column: 1; } .sec-end { grid-column: 2; grid-row: 1 / span 2; } }

    footer { padding-top: 1.5rem; margin-top: 1.5rem; border-top: 1px solid var(--line);
             font-size: 0.7rem; color: var(--faint); display: flex; align-items: center;
             justify-content: center; flex-wrap: wrap; gap: 0.4rem 0.6rem; }
    footer a { color: var(--dim); }
    footer a:hover { color: var(--muted); }
    footer .theme-btn { margin-left: 0.4rem; }
    @media (max-width: 560px) {
      .verdicts { grid-template-columns: 1fr; }
      .verdict + .verdict { border-left: 0; border-top: 1px solid var(--line); }
      .sec > summary { grid-template-columns: 1fr auto; }
      .sec > summary .kicker { grid-column: 1; }
      .score-pair { grid-template-columns: 1fr; }
      .bar-row { grid-template-columns: 1fr 4.4rem; }
      .bar { grid-column: 1 / -1; grid-row: 2; }
      .verdict { display: grid; grid-template-columns: 6rem 1fr; align-items: baseline; padding: 0.7rem 0; column-gap: 0.5rem; }
      .v-value { margin-top: 0; }
      .v-sub { grid-column: 2; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><a href="/" class="wordmark">typeahead</a> ranking</h1>
      <nav class="toplinks" aria-label="site links">
        <a href="/stats">stats</a>
        <span>·</span>
        <a href="/docs">docs</a>
      </nav>
    </div>
    <details class="explanation" id="ranking-guide" open>
      <summary>How ranking works</summary>
      <dl class="ranking-facts">
        <div><dt>${rankingIcon("search")}Matches</dt><dd>Handle and display-name prefixes.</dd></div>
        <div><dt>${rankingIcon("score")}Score</dt><dd>Followers, posts, follow ratio, account age.</dd></div>
        <div><dt>${rankingIcon("exact")}Exact handle</dt><dd>Promoted ahead of other matches.</dd></div>
      </dl>
      <details class="weights">
        <summary>Weights and limits</summary>
        <dl class="factors">
          <div><dt>Followers</dt><dd>100 × log₁₀(count + 1)</dd></div>
          <div><dt>Posts</dt><dd>20 × log₁₀(count + 1)</dd></div>
          <div><dt>Follow ratio</dt><dd>+50 above 10; +150 above 100. Requires &gt;100 followers and &gt;0 following.</dd></div>
          <div><dt>Account age</dt><dd>−100 under 1 day; −50 under 3 days. +30 after 1 year; +60 after 3 years. Unknown: 0.</dd></div>
        </dl>
        <ul class="limits">
          <li>Scores use stored counts. Minimum score: 0.</li>
          <li>Multi-word queries require every word. Tantivy retains complete matches before final ranking and result limits.</li>
          <li>Hidden accounts are excluded regardless of score.</li>
        </ul>
      </details>
    </details>
    <section aria-labelledby="inspect-title">
    <h2 class="inspect-heading" id="inspect-title">Inspect an account</h2>

    <div class="entry">
      <form id="form">
        <div class="ta-wrap">
          <input id="actor" name="actor" placeholder="enter a handle or DID" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" aria-label="Handle or DID">
        </div>
        <button id="go" type="submit">inspect</button>
      </form>
    </div>
    <div id="out"></div>
    </section>
    <footer>
      <a href="/">home</a>
      · <a href="/stats">stats</a>
      · <a href="/docs">docs</a>
      <button class="theme-btn" id="theme-toggle" type="button">theme: auto</button>
    </footer>
  </div>
  <script>
    ${THEME_SCRIPT}
    const LABELER_COUNT = ${LABELER_COUNT};
    const form = document.getElementById('form');
    const actor = document.getElementById('actor');
    const out = document.getElementById('out');
    const go = document.getElementById('go');
    const params = new URLSearchParams(location.search);

    setupHandleTypeahead(actor, (picked) => {
      actor.value = picked.handle;
      inspect(picked.did || picked.handle);
    });

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const val = actor.value.trim().replace(/^@/, '');
      if (!val) return;
      inspect(val);
    });

    function inspect(val) {
      const v = String(val || '').trim().replace(/^@/, '');
      const key = v.indexOf('did:') === 0 ? 'did' : 'actor';
      history.replaceState(null, '', '/reputation?' + key + '=' + encQueryValue(v));
      load(v);
    }

    function encQueryValue(v) {
      return encodeURIComponent(v).replace(/%3A/gi, ':');
    }

    // one honest waiting state: the report is a single request whose time is
    // the labeler fan-out (a few seconds worst case), not a pipeline of stages
    let stageTimer = null;
    function startStages() {
      const t0 = Date.now();
      const tick = () => {
        const ms = Date.now() - t0;
        out.innerHTML = '<div class="status"><div class="stage"><span>asking the index and ' + LABELER_COUNT + ' moderation services' +
          '<span class="scan-caret"></span></span><span class="elapsed">' + (ms / 1000).toFixed(1) + 's</span></div></div>';
      };
      tick();
      stageTimer = setInterval(tick, 250);
    }
    function stopStages() {
      if (stageTimer) { clearInterval(stageTimer); stageTimer = null; }
    }

    async function load(val) {
      document.getElementById('ranking-guide').open = false;
      go.disabled = true;
      go.innerHTML = '<span class="btn-caret"></span>';
      startStages();
      try {
        const res = await fetch('/debug/reputation?actor=' + encodeURIComponent(val));
        const data = await res.json();
        stopStages();
        if (!res.ok) {
          out.innerHTML = '<div class="status err">' + esc(data.error || 'request failed') + '</div>';
          return;
        }
        actor.value = data.actor.handle || val;
        render(data);
      } catch {
        out.innerHTML = '<div class="status err">network error.</div>';
      } finally {
        stopStages();
        go.disabled = false;
        go.textContent = 'inspect';
      }
    }

    function setupHandleTypeahead(input, onPick) {
      const dd = document.createElement('div');
      dd.className = 'ta-typeahead-dropdown';
      dd.setAttribute('role', 'listbox');
      input.parentNode.appendChild(dd);
      let timer = null, items = [], sel = -1, last = '';
      function curVal() { return input.value.trim().replace(/^@/, ''); }
      function hide() { dd.classList.remove('open'); dd.innerHTML = ''; items = []; sel = -1; }
      function renderItems() {
        dd.innerHTML = items.map((a, i) => {
          const dn = a.displayName ? '<span class="ta-typeahead-name">' + esc(a.displayName) + '</span>' : '';
          const av = a.avatar
            ? '<img class="ta-typeahead-avatar" src="' + esc(a.avatar) + '"' + (a.avatarFallback ? ' data-fb="' + esc(a.avatarFallback) + '" onerror="this.onerror=null;if(this.dataset.fb)this.src=this.dataset.fb"' : '') + ' loading="lazy" alt="">'
            : '<span class="ta-typeahead-avatar ta-typeahead-avatar-ph"></span>';
          return '<div class="ta-typeahead-item' + (i === sel ? ' active' : '') + '" data-i="' + i + '" role="option">' +
            av + '<div class="ta-typeahead-text"><span class="ta-typeahead-handle">@' + esc(a.handle) + '</span>' + dn + '</div></div>';
        }).join('');
        dd.classList.toggle('open', items.length > 0);
      }
      async function fetchNow() {
        const q = curVal();
        if (q.length < 1 || q.indexOf('did:') === 0) { hide(); return; }
        if (q === last) return;
        last = q;
        try {
          const r = await fetch('/xrpc/tech.waow.typeahead.searchActors?q=' + encodeURIComponent(q) + '&limit=8', {
            headers: { 'X-Client': 'typeahead/reputation' },
          });
          const d = r.ok ? await r.json() : null;
          if (!d || curVal() !== q) return;
          items = (d.actors || []).slice(0, 8);
          sel = items.length ? 0 : -1;
          renderItems();
        } catch {}
      }
      function choose(i) {
        const a = items[i];
        if (!a) return;
        hide();
        onPick(a);
      }
      input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(fetchNow, 150);
      });
      input.addEventListener('keydown', (e) => {
        if (!items.length) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); sel = (sel + 1) % items.length; renderItems(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); sel = (sel - 1 + items.length) % items.length; renderItems(); }
        else if (e.key === 'Enter' && sel >= 0 && dd.classList.contains('open')) { e.preventDefault(); choose(sel); }
        else if (e.key === 'Escape') hide();
      });
      dd.addEventListener('mousedown', (e) => {
        const item = e.target.closest('.ta-typeahead-item');
        if (!item) return;
        e.preventDefault();
        choose(Number(item.getAttribute('data-i')));
      });
      document.addEventListener('click', (e) => { if (!e.target.closest('.ta-wrap')) hide(); });
    }

    function render(d) {
      const b = d.score.breakdown;
      const m = d.moderation;
      const hidden = m.hiddenComputed || m.hiddenStored;
      const stale = Math.abs(d.score.staleDelta) >= 0.01;
      const exact = d.sampleQueryPositions.exactHandle;
      const display = d.sampleQueryPositions.displayName;
      const prefix = d.sampleQueryPositions.handlePrefix;
      const maxPart = Math.max(b.followersInfluence, b.postsInfluence, b.followerFollowRatioBonus, b.matureAccountBonus, 1);
      const pct = d.score.percentile || {};
      const applied = m.labelers.filter(l => l.labels.length);
      const appliedCount = applied.reduce((n, l) => n + l.labels.length, 0);
      const down = m.labelers.filter(l => l.status !== 'ok');
      const ownLabels = m.labels.map(l => l.val);

      const verdictSearch = hidden
        ? verdict('bad', 'search', 'hidden', gateShort(d))
        : verdict('good', 'search', 'visible', exact.found ? 'exact handle ranks #' + exact.position : 'exact handle not in top 50');
      const verdictMod = appliedCount
        ? verdict('warn', 'moderation', appliedCount + ' label' + (appliedCount === 1 ? '' : 's'), 'from ' + applied.map(l => l.name).join(', '))
        : verdict('good', 'moderation', 'no labels', (m.labelers.length - down.length) + ' of ' + m.labelers.length + ' services answered');
      const verdictRank = verdict('na', 'score', num(d.score.stored), pct.topPercent == null ? 'percentile unavailable' : '≈ top ' + num(pct.topPercent) + '% by score');

      out.innerHTML =
        '<div class="who">' +
          '<div class="who-avatar">' + avatar(d.actor) + '</div>' +
          '<div class="who-text">' +
            '<div class="who-name">' + esc(d.actor.displayName || d.actor.handle) + '</div>' +
            '<div class="who-handle">@' + esc(d.actor.handle) + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="verdicts">' + verdictRank + verdictSearch + verdictMod + '</div>' +
        '<div class="secs">' +
          sec('sec-rank', 'score breakdown', stale ? 'Recalculation: ' + signed(d.score.staleDelta) : 'Followers, posts, ratio, age',
            statusBadge(stale ? 'warn' : 'good', stale ? 'pending write' : 'current'), true,
            '<div class="card-sub">Stored score: ' + num(d.score.stored) + '. Recalculated: ' + num(d.score.computedNow) + '. ' + (stale ? 'Search uses the stored value.' : '') + '</div>' +
            '<div class="bars" style="margin-top:0.9rem">' +
              bar('followers', b.followersInfluence, maxPart) +
              bar('posts', b.postsInfluence, maxPart) +
              bar('follow ratio', b.followerFollowRatioBonus, maxPart) +
              bar('account age bonus', b.matureAccountBonus, maxPart) +
              bar('new-account penalty', Math.abs(b.freshness.penalty), maxPart, b.freshness.penalty < 0 ? 'penalty' : '') +
            '</div>' +
            '<div class="card-sub"><span class="hi">' + num(b.inputs.followersCount) + '</span> followers, ' +
              '<span class="hi">' + num(b.inputs.postsCount) + '</span> posts, and an account age of ' +
              '<span class="hi">' + (b.freshness.ageDays == null ? 'unknown' : num(b.freshness.ageDays) + ' days') + '</span>. ' +
              '</div>') +
          sec('sec-vis', 'visibility', hidden ? gateShort(d) : (d.indexing.shouldBeInPrefixIndex ? 'In prefix lookup' : 'Not in prefix lookup'),
            statusBadge(hidden ? 'bad' : 'good', hidden ? 'hidden' : 'can appear'), hidden,
            '<div class="checks">' +
              check(!m.hiddenStored, 'stored visibility', m.hiddenStored ? 'the actors row is currently marked hidden.' : 'the actors row is currently visible.') +
              check(!m.hiddenComputed, 'moderation result', gateCopy(d)) +
              check(d.indexing.shouldBeInPrefixIndex, 'prefix lookup eligibility', d.indexing.shouldBeInPrefixIndex ? 'handle is present and the actor is allowed into prefix lookup.' : 'hidden actors or actors without handles are excluded from prefix lookup.') +
              check(d.indexing.exactHandleLookupFindsIt, 'exact handle lookup', d.indexing.exactHandleLookupFindsIt ? 'a direct handle lookup resolves to this DID.' : 'the handle is not currently resolvable to this DID via exact lookup.') +
            '</div>') +
          sec('sec-mod', 'moderation', appliedCount
              ? applied.map(l => '<span class="hi">' + esc(l.name) + '</span>: ' + l.labels.map(x => x.val).join(', ')).join(' · ')
              : (m.labelers.length - down.length) + ' of ' + m.labelers.length + ' services answered',
            moderationBadge(m), appliedCount > 0,
            '<div class="checks">' +
              info('labels typeahead holds', (ownLabels.length ? ownLabels.join(', ') : 'none') + ' — ' + m.storedLabelsSource + '.') +
            '</div>' +
            '<div class="labelers">' + m.labelers.map(labelerRow).join('') + '</div>' +
            '<div class="card-sub">Labels checked when this report loaded.</div>') +
          sec('sec-search', 'search positions', [exact, display, prefix].filter(p => p && p.query).map(p => '<span class="hi">' + esc(p.query) + '</span> ' + (p.found ? '#' + p.position : 'not top 50')).join(' · '),
            statusBadge(exact.found ? 'good' : 'warn', exact.found ? 'exact works' : 'not found'), !exact.found,
            '<div class="queries">' +
              queryRow('exact handle', exact) +
              queryRow('display name', display) +
              queryRow('handle prefix', prefix) +
            '</div>') +
          sec('sec-data', 'stored data', 'profile checked ' + (d.indexing.profileCheckedAt ? dateShort(d.indexing.profileCheckedAt) : 'never') + ' · row updated ' + (d.indexing.updatedAt ? dateShort(d.indexing.updatedAt) : 'unknown'),
            '', false,
            '<div class="checks">' +
              (d.indexing.enrichedOnView ? info('enriched on view', 'this actor had never been profile-enriched — the report fetched its profile just now.') : '') +
              info('row updated', d.indexing.updatedAt ? dateTime(d.indexing.updatedAt) : 'unknown') +
              info('profile checked', d.indexing.profileCheckedAt ? dateTime(d.indexing.profileCheckedAt) : 'never') +
              info('PDS', d.actor.pds || 'unknown') +
              info('profile source', sourceCopy(d.indexing.profileSource)) +
            '</div>' +
            '<details class="raw"><summary>raw report</summary><pre>' + esc(JSON.stringify(d, null, 2)) + '</pre></details>') +
        '</div>';
    }
    function verdict(kind, label, value, sub) {
      return '<div class="verdict"><div class="kicker">' + esc(label) + '</div><div class="v-value"><span class="dot ' + kind + '"></span>' + esc(value) + '</div><div class="v-sub">' + esc(sub) + '</div></div>';
    }
    const sectionIcons = ${JSON.stringify({"sec-rank": rankingIcon("score"), "sec-vis": rankingIcon("visibility"), "sec-mod": rankingIcon("moderation"), "sec-search": rankingIcon("search"), "sec-data": rankingIcon("data")})};
    function sec(kind, label, line, state, open, body) {
      return '<details class="sec ' + kind + '"' + (open ? ' open' : '') + '><summary><span class="kicker">' + (sectionIcons[kind] || '') + esc(label) + '</span><span class="sec-line">' + line + '</span>' +
        '<span class="sec-end">' + state + '<span class="sec-chev">▶</span></span></summary><div class="sec-body">' + body + '</div></details>';
    }
    function sourceCopy(src) {
      if (src === 'blacksky') return "Blacksky's appview (api.blacksky.community): Bluesky refuses this profile, so counts and rank come from there and run lower than Bluesky's for the same audience.";
      if (src === 'pds') return 'the PDS record only: Bluesky refuses this profile and Blacksky has none, so there are no follower or post counts.';
      if (src === 'bsky') return "Bluesky's public appview.";
      return 'not recorded yet (written before sources were tracked).';
    }
    function gateShort(d) {
      if (d.moderation.override) return 'admin override: ' + d.moderation.override.action;
      if (d.moderation.domainBlocked) return 'domain rule for ' + d.moderation.domainBlocked.domain;
      const active = d.moderation.labels.filter(l => l.activeSearchHide).map(l => l.val);
      if (active.length) return 'label ' + active.join(', ');
      return 'stored row marked hidden';
    }
    function statusBadge(kind, text) { return '<span class="state ' + kind + '"><span class="dot"></span>' + esc(text) + '</span>'; }
    function bar(label, val, max, cls) {
      return '<div class="bar-row"><div class="bar-label">' + esc(label) + '</div><div class="bar ' + (cls || '') + '"><span style="width:' +
        Math.max(1, Math.round((val / max) * 100)) + '%"></span></div><div class="bar-val">' + (cls === 'penalty' ? '-' : '') + num(val) + '</div></div>';
    }
    function check(ok, title, note) {
      return '<div class="check"><div class="mark ' + (ok ? '' : 'bad') + '">' + (ok ? '✓' : '×') + '</div><div><div class="check-title">' +
        esc(title) + '</div><div class="check-note">' + esc(note) + '</div></div></div>';
    }
    function info(title, note) {
      return '<div class="check"><div class="mark warn">•</div><div><div class="check-title">' + esc(title) +
        '</div><div class="check-note">' + esc(note) + '</div></div></div>';
    }
    function queryRow(label, p) {
      const found = p && p.found;
      return '<div class="query"><div><div class="q-name">' + esc(p && p.query ? p.query : label) + '</div><div class="q-kind">' +
        esc(label + (p && p.matchedBy ? ' · ' + p.matchedBy : '')) + '</div></div><div class="rank ' + (found ? '' : 'missing') + '">' +
        (found ? '#' + p.position : 'not top 50') + '</div></div>';
    }
    function moderationBadge(m) {
      const applied = m.labelers.reduce((n, l) => n + l.labels.length, 0);
      const down = m.labelers.filter(l => l.status !== 'ok').length;
      if (applied) return statusBadge('warn', applied + ' label' + (applied === 1 ? '' : 's') + ' applied');
      return statusBadge(down ? 'warn' : 'good', down ? down + ' unavailable' : 'no labels');
    }
    function labelerRow(l) {
      const what = l.status !== 'ok' ? '<span class="lab-none">did not answer</span>'
        : l.labels.length ? l.labels.map(x => esc(x.val)).join(' · ')
        : '<span class="lab-none">none</span>';
      return '<div class="labeler"><div><div class="q-name">' + esc(l.name) + '</div><div class="q-kind">@' + esc(l.handle) + '</div></div><div class="lab-vals">' + what + '</div></div>';
    }
    function gateCopy(d) {
      if (d.moderation.override) return 'override is ' + d.moderation.override.action + (d.moderation.override.reason ? ': ' + d.moderation.override.reason : '.');
      if (d.moderation.domainBlocked) return 'blocked by domain rule for ' + d.moderation.domainBlocked.domain + '.';
      const active = d.moderation.labels.filter(l => l.activeSearchHide).map(l => l.val);
      if (active.length) return 'active search-hide label: ' + active.join(', ') + '.';
      return 'no active hide labels, domain block, or override is hiding it.';
    }
    function avatar(a) {
      if (a.avatar) return '<img src="' + esc(a.avatar) + '"' + (a.avatarFallback ? ' data-fb="' + esc(a.avatarFallback) + '" onerror="this.onerror=null;if(this.dataset.fb)this.src=this.dataset.fb"' : '') + ' alt="">';
      return '<span class="ini">' + esc((a.handle || '?').charAt(0)) + '</span>';
    }

    function dateShort(s) { const d = new Date(s); return Number.isNaN(d.getTime()) ? s : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); }
    function dateTime(s) { const d = new Date(s); return Number.isNaN(d.getTime()) ? s : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
    function num(n) { return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 }); }
    function numInt(n) { return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 }); }
    function signed(n) { return (n > 0 ? '+' : '') + num(n); }
    function esc(s) { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; }
    // deep links load LAST: every top-level const/let above must exist before
    // load() runs, or the page throws in the temporal dead zone and shows
    // nothing (2026-09-03, twice). reputation.test.ts pins this order.
    const initialActor = params.get('actor') || params.get('handle') || params.get('did');
    if (initialActor) {
      actor.value = initialActor;
      load(initialActor);
    }
  </script>
</body>
</html>`;
}
