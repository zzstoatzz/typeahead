import { FAVICON } from "../types";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT, ogTags } from "./theme";

export function docsPage(): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  ${ogTags({ title: "typeahead — docs", description: "Search AT Protocol accounts, filter by hosting PDS, and fetch profile cards. A public API with no authentication required.", path: "/docs" })}
  ${FAVICON}
  ${THEME_HEAD}
  <style>${THEME_CSS}
    body { padding: calc(2rem + env(safe-area-inset-top)) 1.25rem calc(2rem + env(safe-area-inset-bottom)); }
    .container { max-width: 760px; margin: 0 auto; }
    header, footer { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
    h1 { font-size: 1.1rem; font-weight: 400; }
    .intro { margin: 1rem 0; color: var(--muted); }
    nav { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; padding: 0.5rem 0 1.5rem; }
    a { color: var(--blue); text-underline-offset: 3px; }
    nav a, footer a { font-size: 0.85rem; }
    section { padding: 1.75rem 0; border-top: 1px solid var(--line); scroll-margin-top: 1rem; }
    h2 { color: var(--bright); font-size: 1rem; margin-bottom: 1rem; }
    p, li { line-height: 1.75; }
    p + p, p + pre, pre + p, dl + p, details + p { margin-top: 0.8rem; }
    ul { margin: 0.8rem 0 0 1.25rem; }
    li + li { margin-top: 0.4rem; }
    code { overflow-wrap: anywhere; font-size: 0.85em; color: var(--text); background: var(--panel-2); padding: 0.15em 0.3em; border-radius: 3px; }
    pre { padding: 1rem; overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; line-height: 1.65; }
    pre code { padding: 0; background: none; overflow-wrap: normal; }
    .parameters { display: grid; grid-template-columns: 6rem minmax(0, 1fr); gap: 0; margin-top: 1rem; }
    dt, dd { padding: 0.75rem 0; border-bottom: 1px solid var(--line); }
    dd { padding-left: 0.75rem; }
    .note { border-left: 3px solid var(--accent); padding-left: 1rem; margin-top: 1rem; color: var(--muted); }
    details { margin-top: 1rem; padding: 1rem; border: 1px solid var(--line); border-radius: 8px; }
    summary { cursor: pointer; color: var(--text); }
    details p { margin-top: 0.8rem; }
    footer { border-top: 1px solid var(--line); padding-top: 1.5rem; justify-content: flex-start; }
    .theme-btn { color: var(--muted); min-height: 40px; }
    @media (max-width: 480px) {
      body { padding-left: 1rem; padding-right: 1rem; }
      .parameters { grid-template-columns: 4.5rem minmax(0, 1fr); }
      pre { padding: 0.75rem; }
    }
  </style>
</head>
<body>
  <main class="container">
    <header>
      <h1><a href="/" class="wordmark">typeahead</a> / docs</h1>
      <button class="theme-btn" id="theme-toggle" type="button">theme: auto</button>
    </header>
    <p class="intro">Search AT Protocol accounts from your app. Public API, no authentication required.</p>
    <nav aria-label="On this page">
      <a href="#search">search</a><a href="#pds">PDS filters</a><a href="#profiles">profiles</a><a href="#behavior">behavior</a>
    </nav>

    <section id="search">
      <h2>Search accounts</h2>
      <pre><code>curl 'https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=nate&amp;limit=10' \\
  -H 'X-Client: my-app.example.com'</code></pre>
      <dl class="parameters">
        <dt><code>q</code></dt><dd>Required. Search handles and display names.</dd>
        <dt><code>limit</code></dt><dd>1–100 results; defaults to 10.</dd>
        <dt><code>pds</code></dt><dd>Optional hosting PDS filter. <a href="#pds">Details below.</a></dd>
      </dl>
      <p>Returns <code>{ "actors": [...] }</code>. Actors include <code>did</code> and <code>handle</code>, plus available <code>displayName</code>, <code>avatar</code>, <code>associated</code>, <code>labels</code> and <code>createdAt</code>. Authenticated <code>viewer</code> state is not included.</p>
      <p><a href="https://tangled.org/zzstoatzz.io/typeahead/blob/main/lexicons/tech.waow.typeahead.searchActors.json">Response schema</a> · <a href="/llms.txt">Agent reference</a></p>
      <details id="switching-from-bluesky">
        <summary>Switching from Bluesky’s typeahead</summary>
        <p>Replace <code>https://public.api.bsky.app</code> with <code>https://typeahead.waow.tech</code>. Keep <code>/xrpc/app.bsky.actor.searchActorsTypeahead</code>: it is a permanent alias with the same behavior as our canonical endpoint.</p>
        <p id="response-comparison">The response is a basic profile list without <code>viewer</code> state. If you need authenticated relationship information, obtain it separately through the user’s appview.</p>
      </details>
      <p>Use your domain in <code>X-Client</code> for attribution on <a href="/stats">/stats</a>. Browser requests can also be attributed through <code>Origin</code>.</p>
      <p class="note">For tests, send <code>X-Client: smoke-testing</code>. Requests and errors stay in operational totals, but do not count toward public app adoption.</p>
    </section>

    <section id="pds">
      <h2>Filter by hosting PDS</h2>
      <pre><code>GET /xrpc/tech.waow.typeahead.searchActors?q=bob&amp;pds=acme.com</code></pre>
      <p>The filter uses recorded hosting identity, so it includes custom-domain handles. It applies before ranking and result selection; there is no per-PDS account-count cutoff.</p>
      <ul>
        <li>Hostnames match exactly, ignoring case. Subdomains are separate.</li>
        <li>HTTP(S) origins and a trailing slash or dot are accepted. Ports, paths and empty values return <code>400</code>.</li>
        <li>An unknown PDS returns an empty list. Accounts with missing hosting metadata cannot match the filter.</li>
        <li>Unavailable scope support returns <code>503 PdsScopeUnavailable</code>, never unfiltered results.</li>
      </ul>
      <p class="note">PDS metadata coverage is incomplete. A missing scoped result does not establish that an account is hosted elsewhere.</p>
      <details id="pds-preference">
        <summary>Advanced: prefer a PDS without filtering</summary>
        <p><code>prefer_pds=acme.com</code> keeps global matches and defaults to <code>preference=pds_first</code>. Alternatively, <code>preference=boost&amp;boost=10</code> adds a finite, nonnegative score boost; exact eligible handles retain priority.</p>
        <p>The independent <code>pds</code> filter still applies. Preference and boost options require <code>prefer_pds</code>. Missing hosting metadata also limits preferences. These extensions work on both endpoint names here; they are not Bluesky API parameters.</p>
      </details>
    </section>

    <section id="profiles">
      <h2>Fetch profile cards</h2>
      <pre><code>GET /xrpc/app.bsky.actor.getProfile?actor=zzstoatzz.io
GET /xrpc/app.bsky.actor.getProfiles?actors=zzstoatzz.io&amp;actors=...</code></pre>
      <p>Accepts handles or DIDs; the batch endpoint accepts up to 25 actors. Responses include available profile text, images, account counts, labels and timestamps. They omit viewer state, verification and live status.</p>
      <p>Counts come from enrichment and are omitted until known. <code>indexedAt</code> is the source row’s last write time. Profile responses are cached for five minutes.</p>
    </section>

    <section id="behavior">
      <h2>Limits and freshness</h2>
      <ul>
        <li>60 requests per minute per IP. Browser CORS is enabled.</li>
        <li>Search responses are edge-cached for 60 seconds; changes may take longer to arrive through ingestion.</li>
        <li>Invalid query parameters return <code>400</code>; rate limits return <code>429</code>; an unavailable search backend returns <code>503</code>.</li>
        <li>Tantivy receives continuous source updates. Live updates do not wait for full snapshot rebuilds.</li>
      </ul>
      <p>For surprising ordering, see the <a href="/reputation">ranking report</a>. Ranking and visibility are separate decisions.</p>
      <details id="moderation">
        <summary>Moderation and visibility</summary>
        <p>Search respects Bluesky moderation labels <code>!hide</code>, <code>!takedown</code>, <code>!suspend</code> and <code>spam</code>, as well as domain blocks and explicit-slur filtering. Per-DID overrides can change visibility; an explicit show override is the final decision.</p>
        <p>An appview refusing to serve a profile does not by itself hide that account. Profiles can be enriched through another appview or directly from a PDS. PDS account deactivation or deletion removes the account from search.</p>
      </details>
      <details id="request-indexing">
        <summary>An account is missing</summary>
        <p>Request indexing from the <a href="/">homepage</a>. Accounts outside the relay’s coverage or quiet since before indexing began may need this. The handle and authoritative DID document must resolve to each other.</p>
      </details>
    </section>
    <footer><a href="/">home</a><a href="/stats">stats</a><a href="/llms.txt">llms.txt</a><a href="https://tangled.org/zzstoatzz.io/typeahead">source</a></footer>
  </main>
  <script>${THEME_SCRIPT}</script>
</body>
</html>`;
}
