import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT } from "./theme";

export function docsPage(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>typeahead — switching from bluesky</title>
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    body { display: flex; justify-content: center;
           padding: calc(1.5rem + env(safe-area-inset-top)) 1rem calc(2rem + env(safe-area-inset-bottom)); }
    .container { max-width: 620px; width: 100%; }
    .header { display: flex; align-items: baseline; gap: 0.4rem; margin-bottom: 0.4rem; }
    h1 { font-size: 1.1rem; font-weight: 400; color: var(--muted); }
    .subtitle { font-size: 0.8rem; color: var(--dim); margin-bottom: 1.5rem; }
    h2 { font-size: 0.85rem; font-weight: 600; color: var(--text); margin: 1.5rem 0 0.6rem; }
    h3 { font-size: 0.8rem; font-weight: 600; color: var(--muted); margin: 1.2rem 0 0.5rem; }
    p, li { font-size: 0.8rem; color: var(--muted); line-height: 1.6; }
    p { margin-bottom: 0.7rem; }
    ul { margin: 0 0 0.7rem 1.2rem; }
    li { margin-bottom: 0.3rem; }
    code { font-size: 0.75rem; background: var(--panel-2);
           border: 1px solid var(--line); border-radius: 4px; padding: 0.1rem 0.35rem; color: var(--text); }
    pre { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 0.8rem;
          overflow-x: auto; -webkit-overflow-scrolling: touch; margin-bottom: 0.7rem; }
    pre code { background: none; border: none; padding: 0; font-size: 0.72rem; color: var(--text); }
    .diff-add { color: var(--accent); }
    .diff-del { color: var(--red); }
    .kw { color: var(--violet); }
    .str { color: var(--accent); }
    .fn { color: var(--blue); }
    .cm { color: var(--dim); font-style: italic; }
    .op { color: var(--muted); }
    .callout { background: var(--panel); border: 1px solid var(--line); border-left: 3px solid var(--accent);
               border-radius: 8px; padding: 0.7rem 0.9rem; margin-bottom: 1rem;
               font-size: 0.78rem; color: var(--muted); line-height: 1.6; }
    .callout strong { color: var(--text); }
    .callout.warn { border-left-color: var(--amber); }
    table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; font-size: 0.75rem; }
    th { text-align: left; color: var(--dim); font-weight: 400; padding: 0.4rem 0.6rem;
         border-bottom: 1px solid var(--line); }
    td { padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line); color: var(--muted); }
    td code { font-size: 0.7rem; }
    .yes { color: var(--accent); }
    .no { color: var(--dim); }
    footer { padding-top: 1.5rem; border-top: 1px solid var(--line); font-size: 0.7rem;
             color: var(--faint); display: flex; align-items: center; justify-content: center;
             flex-wrap: wrap; gap: 0.4rem 0.6rem; }
    footer a { color: var(--dim); text-decoration: none; padding: 0.35rem 0.15rem; }
    footer a:hover { color: var(--muted); }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1><a href="/" class="wordmark">typeahead</a> docs</h1>
    </div>
    <p class="subtitle">switching from the bluesky typeahead API</p>

    <div class="callout">
      <strong>tl;dr</strong> — migrating? replace <code>public.api.bsky.app</code> with
      <code>typeahead.waow.tech</code> and you're done. building something new? use the
      canonical endpoint: <code>/xrpc/tech.waow.typeahead.searchActors</code>.
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
      accounts are discovered straight from the relay firehose
      (<a href="https://atproto.com/specs/event-stream">com.atproto.sync.subscribeRepos</a>) —
      anyone who creates a profile, posts, likes, or follows is picked up automatically,
      wherever their PDS is hosted.
      accounts that predate the index or haven't posted yet can be surfaced on demand
      (see <a href="#request-indexing">request indexing</a> below).
    </p>
    <p>
      every search is served entirely from our own data: a precomputed prefix-index snapshot
      merged with a live overlay for recent renames, deletes, moderation changes, and brand-new
      accounts, then edge-cached for 60s. no request does work proportional to the full corpus
      (millions of actors), and bluesky is never in the request path — it's a rollback-only kill
      switch, not a fallback.
    </p>

    <h2>the endpoint</h2>
    <p>
      the canonical endpoint lives under our own
      <a href="https://atproto.com/specs/lexicon">lexicon</a> namespace
      (<a href="https://tangled.org/zzstoatzz.io/typeahead/blob/main/lexicons/tech.waow.typeahead.searchActors.json">schema</a>):
    </p>
    <pre><code>GET https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=...&amp;limit=10</code></pre>
    <p>
      same params (<code>q</code>, <code>limit</code> 1–100) and the same
      <code>{ "actors": [...] }</code> response as the bluesky-compatible alias below.
      prefer this path for new integrations — <code>app.bsky.*</code> names belong to
      bluesky's lexicons, and this service isn't bluesky.
      there's also a machine-readable summary at <a href="/llms.txt">/llms.txt</a>.
    </p>

    <h2>switching from bluesky</h2>
    <p>
      the alias <code>/xrpc/app.bsky.actor.searchActorsTypeahead</code> behaves identically
      and is kept indefinitely, so migrating is just a base-URL swap:
    </p>
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
      ideally use your domain — the <a href="/stats">stats page</a> links each source
      to <code>https://&lt;your-value&gt;</code>, so a domain gives you a working link
      (a bare app name works too, it just won't link anywhere).
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
      <tr><td><code>associated</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>labels</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>createdAt</code></td><td class="yes">✓</td><td class="yes">✓</td></tr>
      <tr><td><code>viewer</code></td><td class="yes">✓</td><td class="no">—</td></tr>
    </table>

    <div class="callout warn">
      <strong>if you depend on <code>viewer</code></strong> — this API doesn't return it
      (it requires authentication). we return did + handle + displayName + avatar +
      associated + labels + createdAt, which covers the full <code>profileViewBasic</code>
      surface minus <code>viewer</code>.
    </div>

    <h2>operational notes</h2>
    <ul>
      <li><strong>rate limited</strong> — 60 req/min per IP</li>
      <li><strong>cached</strong> — results are edge-cached for 60s, so very recent profile changes may lag</li>
      <li><strong>limit range</strong> — <code>1–100</code> (bluesky defaults to 10)</li>
      <li><strong>moderation</strong> — see <a href="#moderation">moderation</a> below</li>
      <li><strong>CORS</strong> — enabled, so browser-based apps can call it directly</li>
    </ul>

    <h2>inspect ranking</h2>
    <p>
      if a result looks surprising, use the <a href="/reputation">ranking report</a>.
      it shows the stored ranking score, the score if recalculated now, percentile among
      visible scored actors, and a few example query positions.
    </p>
    <p>
      ranking score and visibility are separate. follower/post/age signals affect the score;
      fresh accounts are penalized while very new. moderation labels, domain blocks, and
      overrides decide whether an actor can appear at all.
    </p>

    <h2 id="moderation">moderation</h2>
    <p>
      atproto is a network, not a single app. bluesky PBC runs the largest appview and applies its
      own moderation labels — <code>!hide</code>, <code>!takedown</code>, <code>!suspend</code>,
      <code>spam</code> — which hide actors from its search results. we respect these labels by
      default: actors flagged by bluesky's moderation service
      (<code>did:plc:ar7c4by46qjdydhdevvrndac</code>) are excluded from search.
    </p>
    <p>
      however, not every account banned by bluesky PBC is harmful. some were suspended for policy
      disputes, handle issues, or reasons unrelated to abuse. because atproto is designed for
      <a href="https://atproto.com/articles/intent">credible exit</a>, we think these people should
      still be findable — identity is not content moderation.
    </p>
    <p>
      to support this, the index enriches banned/suspended accounts via their PDS directly
      (using <code>com.atproto.repo.getRecord</code>), so profiles are discoverable even when
      bluesky's API refuses to serve them. these accounts start hidden and can be explicitly
      un-hidden via an admin override on a case-by-case basis.
    </p>
    <p>
      handles containing explicit slurs are always filtered, regardless of override status. these
      regexes are
      <a href="https://github.com/bluesky-social/atproto/blob/main/packages/pds/src/handle/explicit-slurs.ts">ported
      from bluesky's own PDS</a> and catch Unicode diacritics, Cyrillic substitutions, and leetspeak.
    </p>

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

    <h2 id="request-indexing">request indexing</h2>
    <p>
      if someone isn't showing up in results, you (or your users) can request indexing from the
      <a href="/">homepage</a>. newly created accounts are picked up automatically from the relay
      firehose, but accounts created before the index existed may need a manual nudge.
    </p>

    <footer>
      <a href="/">&larr; home</a>
      · <a href="/stats">stats</a>
      <button class="theme-btn" id="theme-toggle" type="button">theme: auto</button>
    </footer>
  </div>
  <script>${THEME_SCRIPT}</script>
</body>
</html>`;
}
