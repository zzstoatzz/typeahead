// shared visual identity for all typeahead pages.
// tokens + theme boot live here so home/stats/docs render one coherent system.

export const THEME_HEAD = `<meta name="color-scheme" content="dark light">
  <meta name="theme-color" content="#0a0b0a" media="(prefers-color-scheme: dark)">
  <meta name="theme-color" content="#f2f3ef" media="(prefers-color-scheme: light)">
  <script>(function(){try{var t=localStorage.getItem('ta.theme');if(t==='dark'||t==='light')document.documentElement.dataset.theme=t}catch(_){}})()</script>`;

// The light palette is needed in two places CSS cannot share a block across —
// the prefers-color-scheme query and the explicit [data-theme="light"] override.
// Written once here so the two can never drift apart.
const LIGHT_TOKENS = `
        --bg: #f2f3ef; --panel: #fafbf8; --panel-2: #eceee8; --hover: #e6e8e2;
        --line: #d8dbd3; --line-2: #c8ccc3;
        --text: #2c322e; --bright: #131614; --muted: #5d665f; --dim: #8a938c; --faint: #b4bab2;
        --accent: #14795f; --accent-dim: #4c9c88; --blue: #2465c2; --violet: #7a4bc9;
        --amber: #a86a1c; --red: #b13540;
        --tip-bg: rgba(250, 251, 248, 0.97);
        color-scheme: light;`;

export const THEME_CSS = `
    :root {
      --bg: #0a0b0a; --panel: #101211; --panel-2: #161917; --hover: #1a1d1b;
      --line: #212523; --line-2: #2b302d;
      --text: #d9dedb; --bright: #f2f5f3; --muted: #8a938e; --dim: #59615c; --faint: #3d423f;
      --accent: #44aa99; --accent-dim: #2c6e63; --blue: #58a6ff; --violet: #bc8cff;
      --amber: #e5a04b; --red: #e06c75;
      --tip-bg: rgba(16, 18, 17, 0.96);
      color-scheme: dark;
    }
    @media (prefers-color-scheme: light) {
      :root:not([data-theme="dark"]) {${LIGHT_TOKENS}
      }
    }
    :root[data-theme="light"] {${LIGHT_TOKENS}
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html { -webkit-text-size-adjust: 100%; }
    body {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.6;
      min-height: 100vh; -webkit-tap-highlight-color: transparent;
    }
    button, a { touch-action: manipulation; }
    a:focus-visible, button:focus-visible, [tabindex]:focus-visible {
      outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px;
    }
    ::selection { background: var(--accent); color: var(--bg); }
    /* signature: the wordmark carries a live caret — search happens at the caret */
    .wordmark { color: var(--bright); font-weight: 700; text-decoration: none; }
    .wordmark::after {
      content: ""; display: inline-block; width: 0.5em; height: 1em; margin-left: 0.12em;
      background: var(--accent); vertical-align: text-bottom;
      animation: caret-blink 1.1s steps(1) infinite;
    }
    @keyframes caret-blink { 50% { opacity: 0; } }
    /* one caret of attention: while the user holds a real text caret, the
       wordmark's goes quiet and steady — it hands off rather than compete */
    .wordmark.quiet::after { animation: none; opacity: 0.3; }
    @media (prefers-reduced-motion: reduce) {
      .wordmark::after { animation: none; }
      * { transition: none !important; }
    }
    .theme-btn {
      background: none; border: 1px solid var(--line); border-radius: 6px; color: var(--dim);
      font: inherit; font-size: 0.7rem; padding: 0.3rem 0.6rem; cursor: pointer;
      min-height: 32px;
    }
    .theme-btn:hover { color: var(--muted); border-color: var(--line-2); }
`;

// cycles system → dark → light; expects a button with id="theme-toggle"
export const THEME_SCRIPT = `
    (function() {
      var btn = document.getElementById('theme-toggle');
      if (!btn) return;
      function current() { try { return localStorage.getItem('ta.theme') || 'system'; } catch (_) { return 'system'; } }
      function label(mode) { return { system: 'theme: auto', dark: 'theme: dark', light: 'theme: light' }[mode]; }
      function apply(mode) {
        if (mode === 'dark' || mode === 'light') document.documentElement.dataset.theme = mode;
        else delete document.documentElement.dataset.theme;
        btn.textContent = btn.hasAttribute('data-compact') ? { system: '◐', dark: '☾', light: '☀' }[mode] : label(mode);
        var next = { system: 'dark', dark: 'light', light: 'system' }[mode];
        btn.setAttribute('aria-label', 'Theme: ' + mode + '. Switch to ' + next + '.');
        btn.title = 'Theme: ' + mode + ' — switch to ' + next;
      }
      btn.addEventListener('click', function() {
        var next = { system: 'dark', dark: 'light', light: 'system' }[current()];
        try { next === 'system' ? localStorage.removeItem('ta.theme') : localStorage.setItem('ta.theme', next); } catch (_) {}
        apply(next);
      });
      apply(current());
    })();
`;

/** Link-preview cards are cached by URL at the embedding side (Bluesky, Slack,
 *  Discord all keep their own copy). Bump this whenever the card's layout
 *  changes, or the old card keeps being shown against the new copy. */
export const CARD_VERSION = 3;

/** What the card says, for readers who cannot see it. */
export const CARD_ALT = "typeahead — community actor search for atproto, with live index figures";

/** The one-sentence pitch every page's preview leads with. It names what the
 *  service does and where it looks, and it does not define typeahead by what
 *  it replaces. */
export const TAGLINE = "Fast handle and display-name search across the whole atproto network, not just one appview.";

/** Static link-preview tags for the pages whose content doesn't vary.
 *  /stats builds its own, because its preview describes the selected view. */
export function ogTags(o: { title: string; description: string; path: string }): string {
  const esc = (v: string) => v.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const base = "https://typeahead.waow.tech";
  const img = base + "/og-image?v=" + CARD_VERSION;
  return [
    `<title>${esc(o.title)}</title>`,
    `<meta name="description" content="${esc(o.description)}">`,
    `<meta property="og:type" content="website">`,
    `<meta property="og:site_name" content="typeahead">`,
    `<meta property="og:title" content="${esc(o.title)}">`,
    `<meta property="og:description" content="${esc(o.description)}">`,
    `<meta property="og:url" content="${esc(base + o.path)}">`,
    `<meta property="og:image" content="${img}">`,
    `<meta property="og:image:width" content="1200">`,
    `<meta property="og:image:height" content="630">`,
    `<meta property="og:image:alt" content="${esc(CARD_ALT)}">`,
    `<meta name="twitter:card" content="summary_large_image">`,
    `<meta name="twitter:title" content="${esc(o.title)}">`,
    `<meta name="twitter:description" content="${esc(o.description)}">`,
    `<meta name="twitter:image" content="${img}">`,
    `<meta name="twitter:image:alt" content="${esc(CARD_ALT)}">`,
  ].join("\n  ");
}
