import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT } from "./theme";

export function reputationPage(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ranking report · typeahead</title>
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    /* This page had its own palette — a second, dark-only design system with
       different token names (--text-bright vs --bright, --border vs --line), so
       it ignored the theme toggle and drifted from every other page. The names
       are kept as aliases onto the shared tokens; the values live in theme.ts. */
    :root {
      --bg-subtle: var(--panel);
      --bg-hover: var(--hover);
      --border: var(--line);
      --border-focus: var(--line-2);
      --text-bright: var(--bright);
      --text-secondary: var(--muted);
      --text-dim: var(--dim);
      --text-muted: var(--faint);
      --gold: var(--amber);
    }
    body {
      background:
        radial-gradient(circle at 18% 8%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 23rem),
        radial-gradient(circle at 92% 18%, color-mix(in srgb, var(--amber) 6%, transparent), transparent 20rem),
        var(--bg);
      padding: 1.25rem 1rem 4rem;
    }
    body::before {
      content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
      background-image:
        linear-gradient(color-mix(in srgb, var(--text) 4%, transparent) 1px, transparent 1px),
        linear-gradient(90deg, color-mix(in srgb, var(--text) 3%, transparent) 1px, transparent 1px);
      background-size: 44px 44px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,0.65), transparent 78%);
    }
    .container { max-width: 720px; margin: 0 auto; position: relative; z-index: 1; }
    a { color: var(--accent-dim); text-decoration: none; }
    a:hover { color: var(--accent); }
    .topbar {
      display: flex; align-items: center; justify-content: space-between; gap: 1rem;
      margin-bottom: 1.8rem;
    }
    .brand { display: flex; align-items: baseline; gap: 0.55rem; min-width: 0; }
    .brand a { color: var(--text-bright); font-size: 15px; font-weight: 800; letter-spacing: -0.02em; }
    .brand a:hover { color: var(--accent); }
    .brand .tag {
      color: var(--text-dim); border: 1px solid var(--border-focus); border-radius: 999px;
      padding: 0.08rem 0.45rem; font-size: 11px; white-space: nowrap;
    }
    .toplinks { display: flex; align-items: center; gap: 0.55rem; color: var(--text-muted); font-size: 12px; }
    .toplinks a { color: var(--text-dim); }
    .toplinks a:hover { color: var(--accent); }

    .entry { margin-bottom: 1rem; }
    .lead { font-size: 24px; color: var(--text-bright); line-height: 1.18; margin-bottom: 0.5rem; letter-spacing: -0.03em; max-width: 14ch; }
    .sub { color: var(--text-secondary); margin-bottom: 1rem; max-width: 58ch; }
    form { display: flex; gap: 0.5rem; }
    .ta-wrap { position: relative; flex: 1; min-width: 0; }
    input {
      width: 100%; min-width: 0; font-family: inherit; font-size: 16px;
      padding: 0.72rem 0.85rem; background: color-mix(in srgb, var(--bg-subtle) 86%, transparent);
      border: 1px solid var(--border); border-radius: 8px; color: var(--text-bright);
    }
    input:focus { outline: none; border-color: var(--accent-dim); box-shadow: 0 0 0 3px rgba(42, 157, 92, 0.12); }
    input::placeholder { color: var(--text-muted); }
    button {
      font: inherit; padding: 0.72rem 1rem; cursor: pointer;
      background: var(--accent-dim); color: #fff; border: none; border-radius: 8px;
    }
    button:hover { background: var(--accent); }
    button:disabled { opacity: 0.55; cursor: default; }

    .ta-typeahead-dropdown {
      position: absolute; top: calc(100% + 6px); left: 0; right: 0; display: none; z-index: 20;
      border: 1px solid var(--border-focus); border-radius: 12px;
      background: color-mix(in srgb, var(--bg-subtle) 86%, transparent);
      backdrop-filter: blur(16px) saturate(150%);
      -webkit-backdrop-filter: blur(16px) saturate(150%);
      box-shadow: 0 18px 42px -22px rgba(0, 0, 0, 0.9);
      overflow: hidden;
    }
    .ta-typeahead-dropdown.open { display: block; }
    .ta-typeahead-item {
      display: flex; align-items: center; gap: 0.65rem; padding: 0.62rem 0.72rem;
      cursor: pointer; border-bottom: 1px solid var(--border);
    }
    .ta-typeahead-item:last-child { border-bottom: none; }
    .ta-typeahead-item:hover, .ta-typeahead-item.active { background: rgba(42, 157, 92, 0.1); }
    .ta-typeahead-avatar {
      width: 34px; height: 34px; border-radius: 50%; object-fit: cover; flex: 0 0 auto;
      background: var(--bg-hover); border: 1px solid var(--border-focus);
    }
    .ta-typeahead-avatar-ph { display: inline-block; }
    .ta-typeahead-text { min-width: 0; display: flex; flex-direction: column; line-height: 1.25; }
    .ta-typeahead-handle { color: var(--text-bright); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ta-typeahead-name { color: var(--text-dim); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .status {
      color: var(--text-secondary); margin-top: 1rem; border: 1px solid var(--border);
      border-radius: 12px; background: rgba(13, 13, 13, 0.72); padding: 1rem;
      backdrop-filter: blur(14px) saturate(140%);
    }
    .status.err { color: #e0a293; border-color: rgba(192, 86, 63, 0.35); background: rgba(40, 14, 10, 0.5); }

    .who {
      display: flex; align-items: center; gap: 0.9rem; margin: 1.35rem 0 0.9rem;
      min-height: 64px;
    }
    .who-avatar {
      width: 60px; height: 60px; border-radius: 50%; flex: 0 0 auto; overflow: hidden;
      background: var(--bg-hover); border: 1px solid var(--border-focus);
      display: inline-flex; align-items: center; justify-content: center;
    }
    .who-avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .ini { font-size: 22px; color: var(--text-secondary); text-transform: uppercase; font-weight: 700; }
    .who-text { min-width: 0; }
    .who-name { color: var(--text-bright); font-size: 18px; font-weight: 700; line-height: 1.2; overflow-wrap: anywhere; }
    .who-handle { color: var(--text-secondary); overflow-wrap: anywhere; }
    .who-tag { color: var(--text-dim); font-size: 12px; margin-top: 0.15rem; }

    .cards { display: flex; flex-direction: column; gap: 0.85rem; }
    .card {
      border: 1px solid var(--border-focus); border-radius: 14px;
      background: color-mix(in srgb, var(--bg-subtle) 62%, transparent);
      backdrop-filter: blur(16px) saturate(150%);
      -webkit-backdrop-filter: blur(16px) saturate(150%);
      box-shadow: inset 0 1px 0 0 rgba(255,255,255,0.06), 0 12px 34px -14px rgba(0,0,0,0.75);
      padding: 1.1rem 1.15rem; opacity: 0; transform: translateY(8px);
      animation: rise 0.45s cubic-bezier(0.22, 1, 0.36, 1) forwards;
    }
    .card:nth-child(2) { animation-delay: 0.05s; }
    .card:nth-child(3) { animation-delay: 0.1s; }
    .card:nth-child(4) { animation-delay: 0.15s; }
    @keyframes rise { to { opacity: 1; transform: none; } }
    .card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.6rem; margin-bottom: 0.75rem; }
    .card-label { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-dim); }
    .badge {
      font-size: 12px; color: var(--text-secondary); border: 1px solid var(--border-focus);
      border-radius: 999px; padding: 0.12rem 0.52rem; white-space: nowrap;
    }
    .badge.good { color: var(--accent); border-color: rgba(42, 157, 92, 0.55); }
    .badge.warn { color: var(--gold); border-color: rgba(217, 164, 65, 0.55); }
    .badge.bad { color: #e0a293; border-color: rgba(192, 86, 63, 0.6); }
    .metric { display: flex; align-items: baseline; gap: 0.55rem; flex-wrap: wrap; }
    .metric .num {
      font-size: 42px; line-height: 1; color: var(--accent); font-weight: 800;
      letter-spacing: -0.03em; font-variant-numeric: tabular-nums;
    }
    .metric .unit { color: var(--text-secondary); }
    .card-sub { color: var(--text-secondary); margin-top: 0.65rem; font-size: 13px; }
    .card-sub .hi { color: var(--text-bright); }
    .score-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-top: 0.85rem; }
    .mini {
      border-top: 1px solid var(--border); padding-top: 0.7rem;
    }
    .mini-label { color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em; }
    .mini-value { color: var(--text-bright); font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }

    .bars { display: grid; gap: 0.55rem; }
    .bar-row { display: grid; grid-template-columns: 9.5rem 1fr 4.6rem; gap: 0.7rem; align-items: center; }
    .bar-label { color: var(--text-secondary); font-size: 13px; }
    .bar { height: 8px; background: var(--bg); border-radius: 999px; overflow: hidden; border: 1px solid var(--border); }
    .bar span { display: block; height: 100%; min-width: 1px; background: linear-gradient(90deg, var(--accent-dim), var(--accent)); }
    .bar.penalty span { background: linear-gradient(90deg, #7f2f24, var(--red)); }
    .bar-val { text-align: right; color: var(--text-bright); font-variant-numeric: tabular-nums; font-size: 13px; }

    .checks { display: grid; gap: 0.55rem; }
    .check {
      display: grid; grid-template-columns: 1.2rem 1fr; gap: 0.55rem; align-items: start;
      padding-top: 0.58rem; border-top: 1px solid var(--border);
    }
    .check:first-child { padding-top: 0; border-top: none; }
    .mark { color: var(--accent); font-weight: 800; }
    .mark.bad { color: var(--red); }
    .mark.warn { color: var(--gold); }
    .check-title { color: var(--text-bright); }
    .check-note { color: var(--text-dim); font-size: 12px; }

    .queries { display: grid; gap: 0.55rem; }
    .query {
      display: grid; grid-template-columns: 1fr auto; gap: 0.75rem; align-items: center;
      padding-top: 0.58rem; border-top: 1px solid var(--border);
    }
    .query:first-child { padding-top: 0; border-top: none; }
    .q-name { color: var(--text-bright); overflow-wrap: anywhere; }
    .q-kind { color: var(--text-dim); font-size: 12px; }
    .rank { color: var(--gold); font-weight: 800; }
    .rank.missing { color: var(--red); }
    details { margin-top: 0.85rem; color: var(--text-dim); }
    summary { cursor: pointer; color: var(--text-secondary); }
    pre {
      margin-top: 0.6rem; background: rgba(0,0,0,0.32); border: 1px solid var(--border);
      border-radius: 10px; padding: 0.85rem; overflow: auto; color: #aaa; font-size: 12px; line-height: 1.45;
    }
    footer {
      padding-top: 1.5rem; margin-top: 1.5rem; border-top: 1px solid var(--border);
      font-size: 12px; color: var(--text-muted); display: flex; justify-content: center; gap: 0.4rem;
    }
    footer a { color: var(--text-dim); }
    footer a:hover { color: var(--accent); }
    @media (max-width: 680px) {
      body { padding: 1rem 0.75rem 3rem; }
      .topbar { align-items: flex-start; }
      .toplinks { flex-wrap: wrap; justify-content: flex-end; }
      .lead { font-size: 22px; max-width: none; }
      form { flex-direction: column; }
      .score-pair { grid-template-columns: 1fr; }
      .bar-row { grid-template-columns: 1fr 4.4rem; }
      .bar { grid-column: 1 / -1; grid-row: 2; }
      .query { grid-template-columns: 1fr; gap: 0.2rem; }
    }
    @media (prefers-reduced-motion: reduce) {
      .card { animation: none; opacity: 1; transform: none; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="topbar">
      <div class="brand">
        <a href="/">typeahead</a>
        <span class="tag">ranking report</span>
      </div>
      <nav class="toplinks" aria-label="site links">
        <a href="/stats">stats</a>
        <span>·</span>
        <a href="/docs">docs</a>
      </nav>
    </div>
    <div class="entry">
      <div class="lead">inspect an actor's search ranking</div>
      <div class="sub">A plain-language readout of ranking inputs, visibility gates, and example search positions.</div>
      <form id="form">
        <div class="ta-wrap">
          <input id="actor" name="actor" placeholder="enter a handle or DID" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" autofocus>
        </div>
        <button id="go" type="submit">inspect</button>
      </form>
    </div>
    <div id="out"></div>
    <footer>
      <a href="/">home</a>
      · <a href="/stats">stats</a>
      · <a href="/docs">docs</a>
      <button class="theme-btn" id="theme-toggle" type="button">theme: auto</button>
    </footer>
  </div>
  <script>
    ${THEME_SCRIPT}
    const form = document.getElementById('form');
    const actor = document.getElementById('actor');
    const out = document.getElementById('out');
    const go = document.getElementById('go');
    const params = new URLSearchParams(location.search);

    setupHandleTypeahead(actor, (picked) => {
      actor.value = picked.handle;
      inspect(picked.did || picked.handle);
    });

    const initialActor = params.get('actor') || params.get('handle') || params.get('did');
    if (initialActor) {
      actor.value = initialActor;
      load(initialActor);
    }

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

    async function load(val) {
      go.disabled = true;
      go.textContent = '...';
      out.innerHTML = '<div class="status">reading the index...</div>';
      try {
        const res = await fetch('/debug/reputation?actor=' + encodeURIComponent(val));
        const data = await res.json();
        if (!res.ok) {
          out.innerHTML = '<div class="status err">' + esc(data.error || 'request failed') + '</div>';
          return;
        }
        actor.value = data.actor.handle || val;
        render(data);
      } catch {
        out.innerHTML = '<div class="status err">network error.</div>';
      } finally {
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
            ? '<img class="ta-typeahead-avatar" src="' + esc(a.avatar) + '" loading="lazy" alt="">'
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
      const hidden = d.moderation.hiddenComputed || d.moderation.hiddenStored;
      const stale = Math.abs(d.score.staleDelta) >= 0.01;
      const exact = d.sampleQueryPositions.exactHandle;
      const display = d.sampleQueryPositions.displayName;
      const prefix = d.sampleQueryPositions.handlePrefix;
      const maxPart = Math.max(b.followersInfluence, b.postsInfluence, b.followerFollowRatioBonus, b.matureAccountBonus, 1);
      const pct = d.score.percentile || {};
      out.innerHTML =
        '<div class="who">' +
          '<div class="who-avatar">' + avatar(d.actor) + '</div>' +
          '<div class="who-text">' +
            '<div class="who-name">' + esc(d.actor.displayName || d.actor.handle) + '</div>' +
            '<div class="who-handle">@' + esc(d.actor.handle) + '</div>' +
            '<div class="who-tag">' + esc(shortDid(d.actor.did)) + (d.actor.createdAt ? ' · created ' + esc(dateShort(d.actor.createdAt)) : '') + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="cards">' +
          card('ranking score', statusBadge(stale ? 'warn' : 'good', stale ? 'pending write' : 'current'),
            '<div class="metric"><span class="num">' + num(d.score.computedNow) + '</span><span class="unit">score if recalculated now</span></div>' +
            '<div class="card-sub">' + esc(scoreSummary(d, pct, stale)) + '</div>' +
            '<div class="score-pair">' +
              mini('stored score', num(d.score.stored)) +
              mini('scored actors', pct.rank == null ? 'n/a' : '#' + numInt(pct.rank) + ' of ' + numInt(pct.total)) +
              mini('top band', pct.topPercent == null ? 'not available' : 'top ' + num(pct.topPercent) + '%') +
              mini('recalc delta', signed(d.score.staleDelta)) +
            '</div>') +
          card('score ingredients', statusBadge('good', 'ranking only'),
            '<div class="bars">' +
              bar('followers', b.followersInfluence, maxPart) +
              bar('posts', b.postsInfluence, maxPart) +
              bar('follow ratio', b.followerFollowRatioBonus, maxPart) +
              bar('account age bonus', b.matureAccountBonus, maxPart) +
              bar('freshness penalty', Math.abs(b.freshness.penalty), maxPart, b.freshness.penalty < 0 ? 'penalty' : '') +
            '</div>' +
            '<div class="card-sub"><span class="hi">' + num(b.inputs.followersCount) + '</span> followers, ' +
              '<span class="hi">' + num(b.inputs.postsCount) + '</span> posts, and an account age of ' +
              '<span class="hi">' + (b.freshness.ageDays == null ? 'unknown' : num(b.freshness.ageDays) + ' days') + '</span>. ' +
              'New accounts are penalized before they have history: <24h is -100, <72h is -50. Older accounts can receive a maturity bonus.</div>') +
          card('visibility gates', statusBadge(hidden ? 'bad' : 'good', hidden ? 'hidden' : 'can appear'),
            '<div class="checks">' +
              info('Separate from ranking', 'These checks decide whether the actor is allowed to appear. They do not add to or subtract from the ranking score.') +
              check(!d.moderation.hiddenStored, 'Stored visibility', d.moderation.hiddenStored ? 'The actors row is currently marked hidden.' : 'The actors row is currently visible.') +
              check(!d.moderation.hiddenComputed, 'Moderation result', gateCopy(d)) +
              check(d.indexing.shouldBeInPrefixIndex, 'Prefix lookup eligibility', d.indexing.shouldBeInPrefixIndex ? 'Handle is present and the actor is allowed into prefix lookup.' : 'Hidden actors or actors without handles are excluded from prefix lookup.') +
              check(d.indexing.exactHandleLookupFindsIt, 'Exact handle lookup', d.indexing.exactHandleLookupFindsIt ? 'A direct handle lookup resolves to this DID.' : 'The exact handle path does not currently return this actor.') +
            '</div>') +
          card('example searches', statusBadge(exact.found ? 'good' : 'warn', exact.found ? 'exact works' : 'not found'),
            '<div class="queries">' +
              queryRow('exact handle', exact) +
              queryRow('display name', display) +
              queryRow('handle prefix', prefix) +
            '</div>') +
          card('stored data freshness', statusBadge('good', 'timestamps'),
            '<div class="checks">' +
              info('Row updated', d.indexing.updatedAt ? dateTime(d.indexing.updatedAt) : 'unknown') +
              info('Profile checked', d.indexing.profileCheckedAt ? dateTime(d.indexing.profileCheckedAt) : 'never') +
              info('PDS', d.actor.pds || 'unknown') +
              info('Labels', d.moderation.labels.length ? d.moderation.labels.map(l => l.val).join(', ') : 'none') +
            '</div>' +
            '<details><summary>raw report</summary><pre>' + esc(JSON.stringify(d, null, 2)) + '</pre></details>') +
        '</div>';
    }

    function card(label, badge, body) {
      return '<section class="card"><div class="card-head"><span class="card-label">' + esc(label) + '</span>' + badge + '</div>' + body + '</section>';
    }
    function statusBadge(kind, text) { return '<span class="badge ' + kind + '">' + esc(text) + '</span>'; }
    function mini(label, value) { return '<div class="mini"><div class="mini-label">' + esc(label) + '</div><div class="mini-value">' + esc(value) + '</div></div>'; }
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
    function gateCopy(d) {
      if (d.moderation.override) return 'Override is ' + d.moderation.override.action + (d.moderation.override.reason ? ': ' + d.moderation.override.reason : '.');
      if (d.moderation.domainBlocked) return 'Blocked by domain rule for ' + d.moderation.domainBlocked.domain + '.';
      const active = d.moderation.labels.filter(l => l.activeSearchHide).map(l => l.val);
      if (active.length) return 'Active search-hide label: ' + active.join(', ') + '.';
      return 'No active hide labels, domain block, or override is hiding it.';
    }
    function avatar(a) {
      if (a.avatar) return '<img src="' + esc(a.avatar) + '" alt="">';
      return '<span class="ini">' + esc((a.handle || '?').charAt(0)) + '</span>';
    }

    function scoreSummary(d, p, stale) {
      const stored = 'Search uses the stored score: ' + num(d.score.stored) + '.';
      const freshness = stale
        ? ' Recalculation differs by ' + signed(d.score.staleDelta) + ', so enrichment has not written the newest value yet.'
        : ' It matches the current formula.';
      if (!p || p.percentile == null) return stored + freshness + ' No percentile yet because this actor has no positive stored score.';
      return stored + freshness + ' That ranks #' + numInt(p.rank) + ' of ' + numInt(p.total) +
        ' visible scored actors, above ' + num(p.percentile) + '% of them.';
    }
    function shortDid(did) { return did ? did.slice(0, 12) + '...' + did.slice(-5) : ''; }
    function dateShort(s) { const d = new Date(s); return Number.isNaN(d.getTime()) ? s : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }); }
    function dateTime(s) { const d = new Date(s); return Number.isNaN(d.getTime()) ? s : d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
    function num(n) { return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 }); }
    function numInt(n) { return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 }); }
    function signed(n) { return (n > 0 ? '+' : '') + num(n); }
    function esc(s) { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; }
  </script>
</body>
</html>`;
}
