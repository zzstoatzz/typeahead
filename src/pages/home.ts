import { normalizePds } from "../pds";
import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT, ogTags, TAGLINE } from "./theme";

export function indexPage(pdsEnabled = false): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  ${ogTags({ title: "typeahead — community actor search for atproto", description: TAGLINE + " Discovered from the relay firehose, ranked with moderation applied, served under an open lexicon with no auth required.", path: "/" })}
  <link rel="help" href="/llms.txt" type="text/plain" title="API and integration guide for agents">
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    /* top-aligned, not centered: a centered column jumps when the mobile
       keyboard opens under the search field */
    body { display: flex; justify-content: center; }
    .container {
      max-width: 460px; width: 100%;
      padding: max(env(safe-area-inset-top), 12vh) 1.25rem calc(2rem + env(safe-area-inset-bottom));
    }
    .header { display: flex; align-items: center; flex-wrap: wrap; gap: 0.35rem 0.75rem; margin-bottom: 0.4rem; }
    .resource-links { display: flex; align-items: center; gap: 0.55rem; }
    .resource-links a { font-size: 0.75rem; color: var(--muted); text-decoration: none; padding: 0.65rem 0; }
    .resource-links a:hover { color: var(--bright); }
    .header .theme-btn { margin-left: auto; min-width: 44px; min-height: 44px; padding: 0; border: 0; background: none; font-size: 1.15rem; color: var(--muted); }
    h1 { font-size: 1.15rem; font-weight: 400; color: var(--muted); }
    .subtitle { font-size: 0.8rem; color: var(--dim); margin-bottom: 2rem; }
    .subtitle a { color: var(--dim); text-decoration: none; border-bottom: 1px dotted var(--faint); }
    .subtitle a:hover { color: var(--muted); }
    section { margin-bottom: 2rem; }
    label { display: block; font-size: 0.75rem; color: var(--muted); margin-bottom: 0.5rem; }
    .search-wrap { position: relative; }
    input {
      width: 100%; padding: 0.7rem 0.9rem; background: var(--panel); border: 1px solid var(--line-2);
      border-radius: 8px; color: var(--text); font-size: 16px; font-family: inherit; outline: none;
      min-height: 48px;
    }
    input:focus { border-color: var(--accent-dim); }
    input { caret-color: var(--accent); }
    input::placeholder { color: var(--dim); }
    #q { padding-right: 3.4rem; }
    .filter-toggle { position: absolute; right: 2px; top: 2px; width: 44px; height: 44px;
      border: 0; border-radius: 6px; background: transparent; color: var(--muted); cursor: pointer; }
    .filter-toggle:hover { background: var(--hover); }
    .filter-toggle[data-active="true"] { color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
    .filter-toggle img { width: 18px; height: 18px; object-fit: contain; border-radius: 4px; vertical-align: middle; }
    .filter-toggle[data-active="true"]::after { content: ''; position: absolute; top: 7px; right: 7px;
      width: 5px; height: 5px; background: var(--accent); border-radius: 50%; }
    button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .filter-panel { position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 20;
      padding: 1rem; background: var(--panel); border: 1px solid var(--line-2); border-radius: 8px;
      box-shadow: 0 12px 32px rgba(0,0,0,0.25); }
    .filter-panel p { color: var(--muted); font-size: 0.75rem; line-height: 1.5; margin: 0.6rem 0; }
    .filter-actions { display: flex; gap: 0.5rem; }
    .filter-actions button { font: inherit; font-size: 0.8rem; min-height: 44px; padding: 0.5rem 1rem;
      background: var(--panel-2); color: var(--text); border: 1px solid var(--line-2); border-radius: 8px; cursor: pointer; }
    #pds-error { color: var(--red); }
    .results { position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px;
               background: var(--panel); border: 1px solid var(--line-2); border-radius: 8px;
               max-height: min(300px, 45vh); overflow-y: auto; z-index: 10; display: none;
               box-shadow: 0 12px 32px rgba(0,0,0,0.25); }
    .results.show { display: block; }
    .result { display: flex; align-items: center; gap: 0.7rem; padding: 0.6rem 0.8rem;
              min-height: 48px; cursor: default; font-size: 0.85rem; }
    .result:hover { background: var(--hover); }
    .result img { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
    .result .placeholder { width: 32px; height: 32px; border-radius: 50%; background: var(--panel-2); flex-shrink: 0; }
    .result .info { min-width: 0; }
    .result .name { color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .result .handle { color: var(--dim); font-size: 0.75rem; }
    .empty { padding: 0.7rem 0.8rem; color: var(--dim); font-size: 0.8rem; }
    .index-form { display: flex; gap: 0.5rem; }
    .index-form input { flex: 1; min-width: 0; }
    button[type="submit"] {
      padding: 0.6rem 1.1rem; background: var(--panel-2); border: 1px solid var(--line-2);
      border-radius: 8px; color: var(--text); font: inherit; font-size: 0.85rem; cursor: pointer;
      min-height: 48px;
    }
    button[type="submit"]:hover { background: var(--hover); border-color: var(--accent-dim); }
    button:disabled { opacity: 0.5; cursor: default; }
    .msg { margin-top: 0.75rem; padding: 0.6rem 0.8rem; background: var(--panel); border-radius: 8px;
           border: 1px solid var(--line);
           font-size: 0.85rem; line-height: 1.4; display: flex; justify-content: space-between;
           align-items: start; gap: 0.5rem; }
    .msg.error { border-left: 2px solid var(--red); }
    .msg .dismiss { color: var(--dim); cursor: pointer; font-size: 0.85rem; flex-shrink: 0;
                    background: none; border: none; padding: 0.2rem 0.4rem; font-family: inherit; }
    .msg .dismiss:hover { color: var(--muted); }
    .api { margin-bottom: 1.5rem; background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
           padding: 0.55rem 0.75rem; display: flex; align-items: center; gap: 0.5rem;
           overflow-x: auto; white-space: nowrap; -webkit-overflow-scrolling: touch; }
    .api .method { font-size: 0.65rem; font-weight: 600; color: var(--accent);
                   background: color-mix(in srgb, var(--accent) 14%, transparent);
                   padding: 0.15rem 0.4rem; border-radius: 4px; flex-shrink: 0; letter-spacing: 0.03em; }
    .api .path { font-size: 0.7rem; color: var(--muted); }
    footer { padding-top: 1.5rem; border-top: 1px solid var(--line); font-size: 0.7rem;
             color: var(--faint); display: flex; align-items: center; justify-content: center;
             flex-wrap: wrap; gap: 0.4rem 0.6rem; }
    footer a { color: var(--muted); text-decoration: none; padding: 0.35rem 0.15rem; }
    footer a:hover { color: var(--muted); }
    @media (max-width: 360px) {
      .header .resource-links { order: 2; flex-basis: 100%; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <h1><a href="/" class="wordmark">typeahead</a></h1>
      <nav class="resource-links" aria-label="Project resources">
        <a href="https://tangled.org/zzstoatzz.io/typeahead" aria-label="Source code" target="_blank" rel="noopener">[src]</a>
        <a href="/llms.txt" title="API and integration guide for agents" aria-label="llms.txt — API and integration guide for agents">[llms.txt]</a>
      </nav>
      <button class="theme-btn" id="theme-toggle" data-compact type="button" aria-label="Theme: system. Switch to dark.">◐</button>
    </header>
    <p class="subtitle">community actor search for <a href="https://atproto.com" target="_blank" rel="noopener">atproto</a></p>

    <section>
      <label for="q">try it</label>
      <div class="search-wrap">
        <input id="q" placeholder="search handles..." autocomplete="off" autofocus>
        <button ${pdsEnabled ? "" : "hidden"} class="filter-toggle" id="filter-toggle" type="button" aria-label="Filter by hosting server: all servers" aria-expanded="false" aria-controls="filter-panel" title="Filter by hosting server">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="var(--panel)"/><circle cx="15" cy="17" r="3" fill="var(--panel)"/></svg>
        </button>
        <form class="filter-panel" id="filter-panel" hidden aria-label="Hosting server filter">
          <label for="pds">Hosting server (PDS)</label>
          <input id="pds" placeholder="e.g. acme.com" autocomplete="off" autocapitalize="none" spellcheck="false" aria-describedby="pds-help pds-error">
          <p id="pds-help">Search people hosted on this server, including custom handles. This is not a handle suffix.</p>
          <p id="pds-error" role="alert" hidden></p>
          <div class="filter-actions"><button type="submit">Apply</button><button type="button" id="pds-clear">All servers</button></div>
        </form>
        <div class="results" id="results" role="status" aria-live="polite"></div>
      </div>
    </section>

    <section>
      <label for="handle-input">request indexing</label>
      <form class="index-form" id="index-form">
        <input id="handle-input" placeholder="handle or DID" autocomplete="off">
        <button type="submit" id="index-btn">index</button>
      </form>
      <div id="index-msg"></div>
    </section>

    <div class="api">
      <span class="method">GET</span>
      <span class="path" title="limit defaults to 10">/xrpc/tech.waow.typeahead.searchActors?q=...${pdsEnabled ? "&amp;pds=acme.com" : ""}</span>
    </div>

    <footer>
      by <a href="https://waow.tech/">waow.tech</a>
      · <a href="/stats">stats</a>
      · <a href="/docs">docs</a>
    </footer>
  </div>
  <script>
    const q = document.getElementById('q');
    const results = document.getElementById('results');
    const normalizePds = ${normalizePds.toString()};
    const filterToggle = document.getElementById('filter-toggle');
    const filterPanel = document.getElementById('filter-panel');
    const pdsInput = document.getElementById('pds');
    const pdsError = document.getElementById('pds-error');
    let appliedPds = '';
    let timer = null;
    let request = null;
    let generation = 0;

    function closeFilter(focus = false) {
      filterPanel.hidden = true;
      filterToggle.setAttribute('aria-expanded', 'false');
      if (focus) filterToggle.focus();
    }
    function updateFilter() {
      filterToggle.dataset.active = String(Boolean(appliedPds));
      filterToggle.setAttribute('aria-label', 'Filter by hosting server: ' + (appliedPds || 'all servers'));
      filterToggle.title = appliedPds || 'Filter by hosting server';
      const fallback = filterToggle.querySelector('svg');
      fallback.style.display = '';
      filterToggle.querySelector('img')?.remove();
      const host = normalizePds(appliedPds);
      if (!host) return;
      const icon = new Image(18, 18);
      icon.alt = '';
      icon.referrerPolicy = 'no-referrer';
      icon.hidden = true;
      icon.onload = () => {
        if (!icon.isConnected || !icon.naturalWidth) return;
        fallback.style.display = 'none';
        icon.hidden = false;
      };
      let triedIco = false;
      icon.onerror = () => {
        if (!icon.isConnected) return;
        if (triedIco) { icon.remove(); return; }
        triedIco = true;
        icon.src = 'https://' + host + '/favicon.ico';
      };
      filterToggle.append(icon);
      icon.src = 'https://' + host + '/favicon.svg';
    }
    function saveUrl(push) {
      const url = new URL(location.href);
      if (q.value.trim()) url.searchParams.set('q', q.value.trim()); else url.searchParams.delete('q');
      if (appliedPds) url.searchParams.set('pds', appliedPds); else url.searchParams.delete('pds');
      if (url.href !== location.href) history[push ? 'pushState' : 'replaceState'](null, '', url);
    }
    function message(text) {
      results.replaceChildren();
      const el = document.createElement('div'); el.className = 'empty'; el.textContent = text;
      results.append(el); results.classList.add('show');
    }
    function search(delay = 0) {
      clearTimeout(timer);
      if (request) request.abort();
      const current = ++generation;
      const value = q.value.trim();
      results.classList.remove('show');
      results.replaceChildren();
      if (appliedPds && !normalizePds(appliedPds)) { message('Invalid hosting server. Open the filter to correct it.'); return; }
      if (value.length < 2) return;
      timer = setTimeout(async () => {
        request = new AbortController();
        message('Searching…');
        try {
          const params = new URLSearchParams({q: value, limit: '8'});
          if (appliedPds) params.set('pds', appliedPds);
          const response = await fetch('/xrpc/tech.waow.typeahead.searchActors?' + params, {signal: request.signal});
          const data = await response.json();
          if (current !== generation) return;
          if (!response.ok) {
            message(data.error === 'PdsScopeUnavailable' ? 'Filtering is unavailable for this hosting server. Choose another server or All servers.' : 'Search is unavailable. Please try again.');
            return;
          }
          if (!Array.isArray(data.actors)) throw new Error('Invalid search response');
          if (!data.actors.length) { message(appliedPds ? 'No results on this hosting server.' : 'No results.'); return; }
          results.replaceChildren();
          for (const actor of data.actors) {
            const row = document.createElement('div'); row.className = 'result';
            const avatar = document.createElement('div'); avatar.className = 'placeholder';
            if (actor.avatar) {
              const img = document.createElement('img'); img.alt = ''; img.src = actor.avatar;
              img.onerror = () => { img.onerror = null; if (actor.avatarFallback) img.src = actor.avatarFallback; };
              row.append(img);
            } else row.append(avatar);
            const info = document.createElement('div'); info.className = 'info';
            const name = document.createElement('div'); name.className = 'name'; name.textContent = actor.displayName || actor.handle;
            const handle = document.createElement('div'); handle.className = 'handle'; handle.textContent = '@' + actor.handle;
            info.append(name, handle); row.append(info); results.append(row);
          }
          results.classList.add('show');
        } catch (error) {
          if (current === generation && error.name !== 'AbortError') message('Search is unavailable. Please try again.');
        }
      }, delay);
    }
    function restoreUrl() {
      const params = new URLSearchParams(location.search);
      q.value = params.get('q') || '';
      const raw = params.get('pds');
      appliedPds = raw === null ? '' : (normalizePds(raw) || raw || ' ');
      updateFilter(); closeFilter(); search();
    }
    q.addEventListener('input', () => { saveUrl(false); search(200); });
    filterToggle.addEventListener('click', () => {
      if (!filterPanel.hidden) { closeFilter(true); return; }
      pdsInput.value = appliedPds; pdsError.hidden = true;
      filterPanel.hidden = false; filterToggle.setAttribute('aria-expanded', 'true');
      results.classList.remove('show'); pdsInput.focus();
    });
    function applyPds(value) {
      appliedPds = value; updateFilter(); closeFilter(true); saveUrl(true); search();
    }
    filterPanel.addEventListener('submit', event => {
      event.preventDefault();
      const raw = pdsInput.value.trim();
      const normalized = raw ? normalizePds(raw) : '';
      if (normalized === null) {
        pdsError.textContent = 'Enter a hostname without a port or path.'; pdsError.hidden = false; pdsInput.focus(); return;
      }
      applyPds(normalized);
    });
    document.getElementById('pds-clear').addEventListener('click', () => applyPds(''));
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && !filterPanel.hidden) { event.preventDefault(); closeFilter(true); } });
    document.addEventListener('click', event => {
      if (!event.target.closest('.search-wrap')) { closeFilter(); results.classList.remove('show'); }
    });
    q.addEventListener('focus', () => { closeFilter(); if (results.innerHTML) results.classList.add('show'); });
    window.addEventListener('popstate', restoreUrl);
    restoreUrl();

    // caret handoff: while any text field holds the real caret, the wordmark's rests
    const wordmark = document.querySelector('.wordmark');
    document.querySelectorAll('input').forEach(el => {
      el.addEventListener('focus', () => wordmark.classList.add('quiet'));
      el.addEventListener('blur', () => wordmark.classList.remove('quiet'));
    });
    if (document.activeElement && document.activeElement.tagName === 'INPUT') wordmark.classList.add('quiet');
    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

    // request indexing form
    const indexForm = document.getElementById('index-form');
    const handleInput = document.getElementById('handle-input');
    const indexBtn = document.getElementById('index-btn');
    const indexMsg = document.getElementById('index-msg');

    function showMsg(text, isError) {
      indexMsg.innerHTML = '<div class="msg' + (isError ? ' error' : '') + '">' +
        '<span>' + text + '</span>' +
        '<button class="dismiss" onclick="dismissMsg()">\\u00d7</button></div>';
    }

    function dismissMsg() {
      indexMsg.innerHTML = '';
    }

    indexForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const val = handleInput.value.trim();
      if (!val) { showMsg('enter a handle or DID.', true); return; }

      indexBtn.disabled = true;
      indexBtn.textContent = '...';
      dismissMsg();

      try {
        const r = await fetch('/request-indexing?handle=' + encodeURIComponent(val), { method: 'POST' });
        const data = await r.json();
        if (data.error) {
          showMsg(esc(data.error), true);
        } else {
          const hidden = data.hidden ? ' <em style="color:var(--amber)">(' + esc(data.reason || 'hidden') + ')</em>' : '';
          showMsg('indexed <strong>@' + esc(data.handle) + '</strong>' + hidden, false);
          handleInput.value = '';
        }
      } catch {
        showMsg('something went wrong — try again.', true);
      } finally {
        indexBtn.disabled = false;
        indexBtn.textContent = 'index';
      }
    });
    ${THEME_SCRIPT}
  </script>
</body>
</html>`;
}
