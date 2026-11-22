import { FAVICON } from "../types";
import { escHtml } from "../utils";
import { type Live, type Placed, type Reign, type Slot, HISTORY_DAYS } from "../first";
import { THEME_HEAD, THEME_CSS, THEME_SCRIPT, ogTags } from "./theme";

/** One letter on the index: its live first place and how long it has held it. */
export interface Tile {
  query: string;
  first: Placed | null;
  days: number;
}

/** One query's page. `slots` is null for a query nobody keeps. */
export interface QueryView {
  query: string;
  live: Live;
  slots: Slot[] | null;
  reigns: Reign[];
}

const STRIP_COLORS = ["var(--accent)", "var(--violet)", "var(--blue)", "var(--amber)", "var(--red)"];

const href = (query: string) => `/first/${encodeURIComponent(query)}`;
const name = (actor: Placed) => escHtml(actor.displayName || actor.handle);
const avatar = (actor: Placed, size: number) => actor.avatar
  ? `<img src="${escHtml(actor.avatar)}" alt="" width="${size}" height="${size}" loading="lazy">`
  : `<span class="blank" style="width:${size}px;height:${size}px"></span>`;
const daysLabel = (days: number) => `${days} ${days === 1 ? "day" : "days"}`;

const STYLE = `${THEME_CSS}
    body { padding: calc(2rem + env(safe-area-inset-top)) 1rem calc(3rem + env(safe-area-inset-bottom)); }
    main { max-width: 980px; margin: 0 auto; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; margin-bottom: 1.75rem; }
    header nav { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    header nav a { color: var(--muted); text-decoration: none; font-size: 0.85rem; }
    h1 { font-size: 1.25rem; color: var(--bright); }
    .lede { color: var(--muted); margin: 0.25rem 0 1.25rem; }
    form { display: flex; gap: 0.5rem; margin-bottom: 1.25rem; }
    form input { flex: 1; min-width: 0; min-height: 44px; padding: 0 0.75rem; font: inherit; color: var(--bright); background: var(--panel); border: 1px solid var(--line-2); border-radius: 8px; }
    form button { min-height: 44px; padding: 0 1rem; font: inherit; color: var(--bg); background: var(--accent); border: 0; border-radius: 8px; cursor: pointer; }
    .most { display: flex; gap: 0.75rem; align-items: center; padding: 0.75rem 0.9rem; margin-bottom: 1.25rem; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .most b { color: var(--bright); }
    .most .letters { margin-left: auto; color: var(--accent); font-weight: 700; letter-spacing: 0.2em; }
    img, .blank { border-radius: 50%; background: var(--panel-2); flex: none; display: block; }
    .who { display: flex; flex-direction: column; min-width: 0; }
    .name { color: var(--bright); font-family: system-ui, -apple-system, sans-serif; font-weight: 600; line-height: 1.3; }
    .handle { color: var(--muted); font-size: 0.75rem; line-height: 1.3; overflow-wrap: anywhere; }
    .tiles { list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 8px; }
    .tile { display: grid; grid-template-columns: 1.75rem 40px minmax(0, 1fr); align-items: center; column-gap: 0.65rem; padding: 0.65rem 0.75rem; min-height: 64px; height: 100%;
      color: inherit; text-decoration: none; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .tile:hover { background: var(--hover); border-color: var(--line-2); }
    .letter { font-size: 1.5rem; font-weight: 700; color: var(--accent); text-align: center; }
    .streak { color: var(--dim); font-size: 0.7rem; }
    .none { color: var(--dim); }
    @media (max-width: 560px) {
      .tiles { grid-template-columns: 1fr; gap: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
      .tiles li + li { border-top: 1px solid var(--line); }
      .tile { grid-template-columns: 1.1rem 32px minmax(0, 1fr) auto; min-height: 52px; padding: 0.5rem 0.75rem; background: none; border: 0; border-radius: 0; }
      .tile img, .tile .blank { width: 32px; height: 32px; }
      .letter { font-size: 1.1rem; }
      .tile .streak { grid-column: 4; grid-row: 1; }
      .tile .who .streak { display: none; }
      .most .letters { letter-spacing: 0.1em; }
    }
    @media (min-width: 561px) { .tile > .streak { display: none; } }
    .query { font-size: 3rem; line-height: 1; color: var(--accent); font-weight: 700; word-break: break-all; margin-bottom: 1rem; }
    .places { list-style: none; display: grid; gap: 0.6rem; padding: 1rem; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .places li { display: grid; grid-template-columns: 1.2rem 36px minmax(0, 1fr); gap: 0.65rem; align-items: center; }
    .places a { color: inherit; text-decoration: none; }
    .rank { color: var(--dim); }
    h2 { font-size: 0.95rem; color: var(--bright); margin: 1.75rem 0 0.6rem; }
    .strip { display: grid; grid-template-columns: repeat(${HISTORY_DAYS}, 1fr); gap: 2px; }
    .strip i { height: 24px; border-radius: 2px; }
    .strip i.unkept { background: none; box-shadow: inset 0 0 0 1px var(--line-2); }
    .axis { display: flex; justify-content: space-between; color: var(--dim); font-size: 0.7rem; margin-top: 0.25rem; }
    .reigns { list-style: none; margin-top: 0.75rem; display: grid; gap: 0.35rem; }
    .reigns li { display: flex; gap: 0.6rem; align-items: center; color: var(--muted); }
    .reigns .swatch { width: 10px; height: 10px; border-radius: 2px; flex: none; }
    .reigns b { color: var(--text); font-weight: 400; }
    .note { color: var(--muted); margin-top: 1rem; font-size: 0.85rem; }
    .back { display: inline-block; margin-top: 1.5rem; color: var(--blue); }`;

function page(title: string, description: string, path: string, body: string): string {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  ${ogTags({ title, description, path })}
  ${FAVICON}
  ${THEME_HEAD}
  <style>${STYLE}</style>
</head>
<body>
<main>
  <header>
    <a class="wordmark" href="/">typeahead</a>
    <nav><a href="/first">first</a><a href="/stats">stats</a><a href="/docs">docs</a><button class="theme-btn" id="theme-toggle" data-compact type="button" aria-label="Theme: system. Switch to dark.">◐</button></nav>
  </header>
  ${body}
</main>
<script>${THEME_SCRIPT}</script>
</body>
</html>`;
}

const searchForm = (value = "") => `<form action="/first" method="get" role="search">
    <input name="q" value="${escHtml(value)}" maxlength="64" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Who is first for" placeholder="who is first for…">
    <button type="submit">go</button>
  </form>`;

function mostLetters(tiles: readonly Tile[]): string {
  const held = new Map<string, { first: Placed; letters: string[] }>();
  for (const tile of tiles) {
    if (!tile.first) continue;
    const entry = held.get(tile.first.did) ?? { first: tile.first, letters: [] };
    entry.letters.push(tile.query);
    held.set(tile.first.did, entry);
  }
  const top = [...held.values()].sort((a, b) => b.letters.length - a.letters.length)[0];
  if (!top || top.letters.length < 2) return "";
  return `<p class="most">${avatar(top.first, 36)}<span><b>${name(top.first)}</b> is first for ${top.letters.length} letters</span><span class="letters">${escHtml(top.letters.join(""))}</span></p>`;
}

function tile(entry: Tile): string {
  const streak = entry.first ? `<span class="streak">${daysLabel(entry.days)}</span>` : "";
  const who = entry.first
    ? `<span class="who"><span class="name">${name(entry.first)}</span><span class="handle">@${escHtml(entry.first.handle)}</span>${streak}</span>`
    : `<span class="who"><span class="none">unavailable</span></span>`;
  return `<li><a class="tile" href="${href(entry.query)}"><span class="letter">${escHtml(entry.query)}</span>${entry.first ? avatar(entry.first, 40) : `<span class="blank" style="width:40px;height:40px"></span>`}${who}${streak}</a></li>`;
}

export function firstIndexPage(tiles: readonly Tile[]): string {
  return page("typeahead — first by letter", "Who comes up first when you type one letter, live, with each letter's history.", "/first", `
  <h1>first by letter</h1>
  <p class="lede">who comes up first when you type one letter, live. each day's first place is kept.</p>
  ${searchForm()}
  ${mostLetters(tiles)}
  <ul class="tiles">${tiles.map(tile).join("")}</ul>`);
}

function colors(slots: readonly Slot[]): Map<string, string> {
  const days = new Map<string, number>();
  for (const slot of slots) if (slot.first) days.set(slot.first.did, (days.get(slot.first.did) ?? 0) + 1);
  const ranked = [...days.entries()].sort((a, b) => b[1] - a[1]).map(([did]) => did);
  return new Map(ranked.map((did, i) => [did, STRIP_COLORS[i] ?? "var(--dim)"]));
}

function historyBlock(slots: readonly Slot[], reigns: readonly Reign[]): string {
  const palette = colors(slots);
  const cells = slots.map((slot) => slot.first
    ? `<i style="background:${palette.get(slot.first.did)}" title="${escHtml(slot.first.handle)}"></i>`
    : `<i class="unkept" title="not kept"></i>`).join("");
  const runs = reigns.map((reign) => reign.first
    ? `<li><span class="swatch" style="background:${palette.get(reign.first.did)}"></span><span><b>${name(reign.first)}</b> ${daysLabel(reign.days)}</span></li>`
    : `<li><span class="swatch unkept" style="box-shadow:inset 0 0 0 1px var(--line-2)"></span><span>not kept, ${daysLabel(reign.days)}</span></li>`).join("");
  return `<h2>last ${HISTORY_DAYS} days</h2>
  <div class="strip" role="img" aria-label="first place each day for the last ${HISTORY_DAYS} days">${cells}</div>
  <div class="axis"><span>${HISTORY_DAYS} days ago</span><span>today</span></div>
  <ul class="reigns">${runs}</ul>`;
}

export function firstQueryPage(view: QueryView): string {
  const { query, live } = view;
  const places = !live.ok
    ? `<p class="note">search is unavailable right now.</p>`
    : live.actors.length === 0
      ? `<p class="note">nobody comes up for this.</p>`
      : `<ol class="places">${live.actors.map((actor, i) => `<li><span class="rank">${i + 1}</span>${avatar(actor, 36)}<a class="who" href="https://bsky.app/profile/${escHtml(actor.did)}"><span class="name">${name(actor)}</span><span class="handle">@${escHtml(actor.handle)}</span></a></li>`).join("")}</ol>`;
  const kept = view.slots
    ? historyBlock(view.slots, view.reigns)
    : `<p class="note">only single letters keep a history; this is the live answer.</p>`;
  return page(`typeahead — first for ${query}`, `Who comes up first when you type "${query}".`, href(query), `
  ${searchForm(query)}
  <p class="query">${escHtml(query)}</p>
  ${places}
  ${kept}
  <a class="back" href="/first">all letters</a>`);
}
