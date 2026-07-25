import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT } from "./theme";

export function indexPage(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>typeahead</title>
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
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.15rem; font-weight: 400; color: var(--muted); }
    .src { font-size: 0.75em; color: var(--dim); text-decoration: none; margin-left: 0.3em; }
    .src:hover { color: var(--muted); }
    .experimental { font-size: 0.55em; color: var(--amber); opacity: 0.75; cursor: default;
                    position: relative; vertical-align: super; line-height: 1; margin-left: 0.3em; }
    .experimental:hover::after, .experimental:focus::after {
      content: "this is experimental and may break or disappear — don't depend on it for anything critical";
      position: absolute; left: 0; top: 1.4em; width: 220px; padding: 0.5rem;
      background: var(--panel-2); border: 1px solid var(--line-2); border-radius: 6px;
      font-size: 1.4em; color: var(--muted); line-height: 1.4; z-index: 20; white-space: normal;
    }
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
    footer a { color: var(--dim); text-decoration: none; padding: 0.35rem 0.15rem; }
    footer a:hover { color: var(--muted); }
    footer .theme-btn { margin-left: 0.4rem; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><a href="/" class="wordmark">typeahead</a><sup class="experimental" tabindex="0">*experimental</sup> <a href="https://tangled.org/zzstoatzz.io/typeahead" class="src" target="_blank" rel="noopener">[src]</a></h1>
    </div>
    <p class="subtitle">community actor search for <a href="https://atproto.com" target="_blank" rel="noopener">atproto</a></p>

    <section>
      <label for="q">try it</label>
      <div class="search-wrap">
        <input id="q" placeholder="search handles..." autocomplete="off" autofocus>
        <div class="results" id="results"></div>
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
      <span class="path">/xrpc/app.bsky.actor.searchActorsTypeahead?q=...&amp;limit=10</span>
    </div>

    <footer>
      by <a href="https://bsky.app/profile/zzstoatzz.io" target="_blank" rel="noopener">@zzstoatzz.io</a>
      · <a href="/stats">stats</a>
      · <a href="/docs">docs</a>
      <button class="theme-btn" id="theme-toggle" type="button">theme: auto</button>
    </footer>
  </div>
  <script>
    const q = document.getElementById('q');
    const results = document.getElementById('results');
    let timer = null;
    q.addEventListener('input', () => {
      clearTimeout(timer);
      const v = q.value.trim();
      if (v.length < 2) { results.classList.remove('show'); return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch('/xrpc/app.bsky.actor.searchActorsTypeahead?q=' + encodeURIComponent(v) + '&limit=8');
          const data = await r.json();
          const actors = data.actors || [];
          if (actors.length === 0) {
            results.innerHTML = '<div class="empty">no results</div>';
          } else {
            results.innerHTML = actors.map(a =>
              '<div class="result">' +
              (a.avatar ? '<img src="' + a.avatar + '" alt="">' : '<div class="placeholder"></div>') +
              '<div class="info"><div class="name">' + esc(a.displayName || a.handle) + '</div>' +
              '<div class="handle">@' + esc(a.handle) + '</div></div></div>'
            ).join('');
          }
          results.classList.add('show');
        } catch(e) {}
      }, 200);
    });
    document.addEventListener('click', e => { if (!e.target.closest('.search-wrap')) results.classList.remove('show'); });
    q.addEventListener('focus', () => { if (results.innerHTML) results.classList.add('show'); });

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
