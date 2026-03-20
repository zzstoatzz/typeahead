import { FAVICON } from "../types";

export function docsPage(): string {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>typeahead — switching from bluesky</title>
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
    h2 { font-size: 0.85rem; font-weight: 600; color: #ccc; margin: 1.5rem 0 0.6rem; }
    h3 { font-size: 0.8rem; font-weight: 600; color: #aaa; margin: 1.2rem 0 0.5rem; }
    p, li { font-size: 0.8rem; color: #999; line-height: 1.6; }
    p { margin-bottom: 0.7rem; }
    ul { margin: 0 0 0.7rem 1.2rem; }
    li { margin-bottom: 0.3rem; }
    code { font-family: ui-monospace, monospace; font-size: 0.75rem; background: #1a1a1a;
           border: 1px solid #222; border-radius: 3px; padding: 0.1rem 0.35rem; color: #ccc; }
    pre { background: #111; border: 1px solid #222; border-radius: 6px; padding: 0.8rem;
          overflow-x: auto; margin-bottom: 0.7rem; }
    pre code { background: none; border: none; padding: 0; font-size: 0.72rem; color: #bbb; }
    .diff-add { color: #4a9; }
    .diff-del { color: #a55; }
    .kw { color: #c678dd; }
    .str { color: #98c379; }
    .fn { color: #61afef; }
    .cm { color: #5c6370; font-style: italic; }
    .op { color: #abb2bf; }
    .callout { background: #111; border: 1px solid #222; border-left: 3px solid #4a9;
               border-radius: 6px; padding: 0.7rem 0.9rem; margin-bottom: 1rem;
               font-size: 0.78rem; color: #999; line-height: 1.6; }
    .callout strong { color: #ccc; }
    .callout.warn { border-left-color: #b98a3e; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; font-size: 0.75rem; }
    th { text-align: left; color: #666; font-weight: 400; padding: 0.4rem 0.6rem;
         border-bottom: 1px solid #222; }
    td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #1a1a1a; color: #999; }
    td code { font-size: 0.7rem; }
    .yes { color: #4a9; }
    .no { color: #666; }
    footer { padding-top: 1.5rem; border-top: 1px solid #1a1a1a; font-size: 0.7rem;
             color: #444; display: flex; justify-content: center; gap: 0.4rem; }
    footer a { color: #555; text-decoration: none; }
    footer a:hover { color: #888; }
    a { color: #58a6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    @media (max-width: 640px) {
      body { padding: 1.5rem 0.75rem; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><a href="/" style="color:inherit;text-decoration:none"><strong>typeahead</strong></a> docs</h1>
    </div>
    <p class="subtitle">switching from the bluesky typeahead API</p>

    <div class="callout">
      <strong>tl;dr</strong> — replace <code>public.api.bsky.app</code> with <code>typeahead.waow.tech</code>.
    </div>

    <h2>what this is</h2>
    <p>
      typeahead is a community-run actor search for <a href="https://atproto.com">atproto</a>,
      aiming to be a drop-in replacement for bluesky's
      <code>app.bsky.actor.searchActorsTypeahead</code> endpoint. the endpoint path and query
      params are identical; the response shape is compatible but slimmer (see
      <a href="#response-comparison">response comparison</a> below).
    </p>
    <p>
      the index is built from a few <a href="https://docs.bsky.app/blog/jetstream">jetstream</a>
      collections — profiles, posts, likes, and follows — so any account that creates or
      interacts with content gets discovered automatically. for accounts that predate the index
      or haven't been seen yet, search queries trigger a throttled backfill from the bluesky API
      to fill gaps on demand. searches use FTS5 prefix matching against handles and display names,
      with results edge-cached for 60s.
    </p>

    <h2>the change</h2>
    <p>replace the base URL. everything else stays the same:</p>
    <pre><code><span class="diff-del">- https://public.api.bsky.app</span>/xrpc/app.bsky.actor.searchActorsTypeahead?q=...&amp;limit=10
<span class="diff-add">+ https://typeahead.waow.tech</span>/xrpc/app.bsky.actor.searchActorsTypeahead?q=...&amp;limit=10</code></pre>

    <h3>before</h3>
    <pre><code><span class="kw">const</span> response <span class="op">=</span> <span class="kw">await</span> <span class="fn">fetch</span>(
  <span class="str">\`https://public.api.bsky.app/xrpc/app.bsky.actor.searchActorsTypeahead?q=\${<span class="fn">encodeURIComponent</span>(query)}&amp;limit=10\`</span>
);</code></pre>

    <h3>after</h3>
    <pre><code><span class="kw">const</span> TYPEAHEAD_URL <span class="op">=</span> <span class="str">'https://typeahead.waow.tech'</span>;

<span class="kw">const</span> response <span class="op">=</span> <span class="kw">await</span> <span class="fn">fetch</span>(
  <span class="str">\`\${TYPEAHEAD_URL}/xrpc/app.bsky.actor.searchActorsTypeahead?q=\${<span class="fn">encodeURIComponent</span>(query)}&amp;limit=10\`</span>
);</code></pre>

    <p>
      extracting the base URL into a constant (or env var) makes it easy to switch back
      if you ever need to.
    </p>

    <h2>identify your app</h2>
    <p>
      set the <code>X-Client</code> header so your app shows up by name in our
      <a href="/stats">traffic stats</a> instead of as "unknown":
    </p>
    <pre><code><span class="kw">const</span> response <span class="op">=</span> <span class="kw">await</span> <span class="fn">fetch</span>(
  <span class="str">\`\${TYPEAHEAD_URL}/xrpc/app.bsky.actor.searchActorsTypeahead?q=\${<span class="fn">encodeURIComponent</span>(query)}&amp;limit=10\`</span>,
  { headers: { <span class="str">'X-Client'</span>: <span class="str">'my-app.example.com'</span> } }
);</code></pre>
    <p>
      use your domain or app name — whatever you want to be identified as.
      browser-based apps will also be identified automatically via the <code>Origin</code> header,
      but <code>X-Client</code> is preferred since it works everywhere (server-side, CLI, native apps).
    </p>

    <h2 id="response-comparison">response comparison</h2>
    <p>
      both return <code>{ "actors": [...] }</code>. the actor objects differ:
    </p>
    <table>
      <tr><th>field</th><th>bluesky</th><th>typeahead</th></tr>
      <tr><td><code>did</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>handle</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>displayName</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>avatar</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>associated</code></td><td class="yes">✓</td><td class="no">—</td></tr>
      <tr><td><code>labels</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>createdAt</code></td><td class="yes">✓</td><td class="no">—</td></tr>
      <tr><td><code>viewer</code></td><td class="yes">✓</td><td class="no">—</td></tr>
    </table>

    <div class="callout warn">
      <strong>if you depend on <code>associated</code>, <code>viewer</code>,
      or <code>createdAt</code></strong> — this API doesn't return them. we return
      did + handle + displayName + avatar + labels, which covers most typeahead UIs.
      if you need the full <code>profileViewBasic</code> surface, you'll need to stick with
      the bluesky API or fetch those fields separately.
    </div>

    <h2>operational notes</h2>
    <ul>
      <li><strong>rate limited</strong> — 60 req/min per IP</li>
      <li><strong>cached</strong> — results are edge-cached for 60s, so very recent profile changes may lag</li>
      <li><strong>limit range</strong> — <code>1–100</code> (bluesky defaults to 10)</li>
      <li><strong>moderation</strong> — actors with <code>!hide</code> or <code>!takedown</code> labels are excluded</li>
      <li><strong>CORS</strong> — enabled, so browser-based apps can call it directly</li>
    </ul>

    <h2>example: plyr.fm</h2>
    <p>
      <a href="https://tangled.sh/zzstoatzz.io/plyr.fm">plyr.fm</a> uses typeahead for actor
      search. the integration looks roughly like:
    </p>
    <pre><code><span class="cm">// config.ts</span>
<span class="kw">export const</span> TYPEAHEAD_URL <span class="op">=</span> <span class="str">'https://typeahead.waow.tech'</span>;

<span class="cm">// HandleSearch.svelte</span>
<span class="kw">const</span> response <span class="op">=</span> <span class="kw">await</span> <span class="fn">fetch</span>(
  <span class="str">\`\${TYPEAHEAD_URL}/xrpc/app.bsky.actor.searchActorsTypeahead?q=\${<span class="fn">encodeURIComponent</span>(query)}&amp;limit=10\`</span>
);
<span class="kw">const</span> data <span class="op">=</span> <span class="kw">await</span> response.<span class="fn">json</span>();
<span class="kw">const</span> actors <span class="op">=</span> (data.actors ?? []).<span class="fn">map</span>(actor <span class="op">=></span> ({
  did: actor.did,
  handle: actor.handle,
  display_name: actor.displayName ?? actor.handle,
  avatar_url: actor.avatar ?? <span class="kw">null</span>,
}));</code></pre>

    <h2>request indexing</h2>
    <p>
      if someone isn't showing up in results, you (or your users) can request indexing from the
      <a href="/">homepage</a>. newly created accounts are picked up automatically via jetstream,
      but accounts created before the index existed may need a manual nudge.
    </p>

    <footer>
      <a href="/">&larr; home</a>
      · <a href="/stats">stats</a>
    </footer>
  </div>
</body>
</html>`;
}
