import { FAVICON } from "../types";

export function indexPage(): string {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead</title>
  ${FAVICON}
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0;
           display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .container { max-width: 460px; width: 100%; padding: 2rem; }
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: #888; }
    h1 strong { color: #e0e0e0; }
    .src { font-size: 0.75em; color: #555; text-decoration: none; margin-left: 0.3em; }
    .src:hover { color: #888; }
    .experimental { font-size: 0.55em; color: #664; cursor: default; position: relative;
                    vertical-align: super; line-height: 1; margin-left: 0.3em; }
    .experimental:hover::after {
      content: "this is experimental and may break or disappear — don't depend on it for anything critical";
      position: absolute; left: 0; top: 1.4em; width: 220px; padding: 0.5rem;
      background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
      font-size: 1.4em; color: #999; line-height: 1.4; z-index: 20; white-space: normal;
    }
    .subtitle { font-size: 0.8rem; color: #555; margin-bottom: 1.5rem; }
    .subtitle a { color: #555; text-decoration: none; }
    .subtitle a:hover { color: #888; }
    section { margin-bottom: 2rem; }
    label { display: block; font-size: 0.8rem; color: #666; margin-bottom: 0.5rem; }
    .search-wrap { position: relative; }
    input { width: 100%; padding: 0.6rem 0.8rem; background: #1a1a1a; border: 1px solid #333;
            border-radius: 6px; color: #e0e0e0; font-size: 16px; outline: none; }
    input:focus { border-color: #555; }
    input::placeholder { color: #555; }
    .results { position: absolute; top: 100%; left: 0; right: 0; margin-top: 4px;
               background: #141414; border: 1px solid #2a2a2a; border-radius: 6px;
               max-height: 260px; overflow-y: auto; z-index: 10; display: none; }
    .results.show { display: block; }
    .result { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 0.7rem;
              cursor: default; font-size: 0.85rem; }
    .result:hover { background: #1a1a1a; }
    .result img { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
    .result .placeholder { width: 28px; height: 28px; border-radius: 50%; background: #222; flex-shrink: 0; }
    .result .info { min-width: 0; }
    .result .name { color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .result .handle { color: #555; font-size: 0.75rem; }
    .empty { padding: 0.6rem 0.8rem; color: #444; font-size: 0.8rem; }
    .index-form { display: flex; gap: 0.5rem; }
    .index-form input { flex: 1; }
    button { padding: 0.6rem 1rem; background: #2a2a2a; border: 1px solid #333;
             border-radius: 6px; color: #e0e0e0; font-size: 0.9rem; cursor: pointer; }
    button:hover { background: #333; }
    button:disabled { opacity: 0.5; cursor: default; }
    .msg { margin-top: 0.75rem; padding: 0.6rem 0.8rem; background: #1a1a1a; border-radius: 6px;
           font-size: 0.85rem; line-height: 1.4; display: flex; justify-content: space-between;
           align-items: start; gap: 0.5rem; }
    .msg.error { border-left: 2px solid #a55; }
    .msg .dismiss { color: #555; cursor: pointer; font-size: 0.75rem; flex-shrink: 0;
                    background: none; border: none; padding: 0; }
    .msg .dismiss:hover { color: #888; }
    .api { margin-bottom: 1.5rem; background: #111; border: 1px solid #222; border-radius: 6px;
           padding: 0.5rem 0.7rem; display: flex; align-items: center; gap: 0.5rem;
           overflow-x: auto; white-space: nowrap; }
    .api .method { font-size: 0.65rem; font-weight: 600; color: #4a9; background: #4a92;
                   padding: 0.15rem 0.4rem; border-radius: 3px; flex-shrink: 0;
                   font-family: ui-monospace, monospace; letter-spacing: 0.03em; }
    .api .path { font-size: 0.7rem; color: #666; font-family: ui-monospace, monospace; }
    footer { padding-top: 1.5rem; border-top: 1px solid #1a1a1a; font-size: 0.7rem;
             color: #444; display: flex; justify-content: center; gap: 0.4rem; }
    footer a { color: #555; text-decoration: none; }
    footer a:hover { color: #888; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><strong>typeahead</strong><sup class="experimental">*experimental</sup> <a href="https://tangled.sh/zzstoatzz.io/typeahead" class="src" target="_blank" rel="noopener">[src]</a></h1>
    </div>
    <p class="subtitle">community actor search for <a href="https://atproto.com" target="_blank" rel="noopener">atproto</a></p>

    <section>
      <label>try it</label>
      <div class="search-wrap">
        <input id="q" placeholder="search handles..." autocomplete="off" autofocus>
        <div class="results" id="results"></div>
      </div>
    </section>

    <section>
      <label>request indexing</label>
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
          const hidden = data.hidden ? ' <em style="color:#886">(' + esc(data.reason || 'hidden') + ')</em>' : '';
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
  </script>
</body>
</html>`;
}
