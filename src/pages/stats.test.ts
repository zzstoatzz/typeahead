import { describe, expect, test } from "bun:test";
import { statsPage, type StatsData } from "./stats";

function page(domains: string[]): string {
  const d: StatsData = {
    total: 100, hiddenCount: 0, rows: [], totalSearches: 0,
    avgLatency: 0, avgCacheLatency: 0, handlePct: "0", avatarPct: "0",
    snapshots: [],
    trafficSources: domains.map((domain, i) => ({ domain, hits: 100 - i })),
    trafficSeries: [], trafficSeries5m: [], trafficDaily: [],
  };
  return statsPage(d);
}

/** The selection targets — one legend entry, one table row — as raw markup. */
function legendEntry(html: string, domain: string): string {
  const start = html.indexOf(`<span data-domain="${domain}">`);
  if (start < 0) return "";
  const next = html.indexOf(`<span data-domain=`, start + 1);
  return html.slice(start, next < 0 ? start + 400 : next);
}
function tableRow(html: string, domain: string): string {
  return html.match(new RegExp(`<tr data-domain="${domain}">.*?</tr>`, "s"))?.[0] ?? "";
}

describe("traffic source selection targets", () => {
  // The split contract (2026-08-15): in the TABLE the domain name is a link to
  // the app's site — the row still selects via the rank/hits/% cells because
  // the click handler skips anchors. In the LEGEND the whole entry stays a
  // selection target (it has no other tappable surface), so the name there is
  // NOT a link.
  const html = page(["plyr.fm", "unknown"]);

  test("the legend entry is not a link", () => {
    const entry = legendEntry(html, "plyr.fm");
    expect(entry).toContain("plyr.fm");
    expect(entry).not.toContain("<a ");
  });

  test("the table row's domain name links to the app's site", () => {
    const cell = tableRow(html, "plyr.fm").match(/<td class="ts-domain">.*?<\/td>/s)?.[0] ?? "";
    expect(cell).toContain('href="https://plyr.fm"');
    expect(cell).toContain('target="_blank"');
  });

  test("the row keeps non-anchor cells to select by", () => {
    const row = tableRow(html, "plyr.fm");
    expect(row).toContain('class="ts-hits"');
    expect(row).toContain('class="ts-pct"');
  });

  test("bare app names (no dot) get no outbound link", () => {
    const html2 = page(["pckt"]);
    expect(tableRow(html2, "pckt")).not.toContain("<a ");
  });

  test("synthetic buckets get no outbound link", () => {
    // "unknown"/"homepage" are attribution buckets, not hostnames
    expect(tableRow(html, "unknown")).not.toContain("<a ");
  });
});

describe("domain escaping", () => {
  // Regression (stored XSS): domains arrive verbatim from the X-Client / Origin
  // request headers. The HTML paths escaped them, but the same values are also
  // inlined into a <script> as JSON — and JSON.stringify does not escape
  // "</script>", so one request with a crafted X-Client closed the element and
  // ran for every visitor to /stats.
  const hostile = 'a"></script><script>alert(1)</script>';
  const html = page([hostile]);

  test("no raw script tag survives into the document", () => {
    expect(html).not.toContain("<script>alert(1)</script>");
  });

  test("the JSON payload cannot close the script element", () => {
    expect(html).not.toContain("</script><script>alert");
    expect(html).toContain("\\u003c/script>");
  });

  test("the quote cannot break out of the data-domain attribute", () => {
    expect(html).toContain("&quot;&gt;&lt;/script&gt;");
  });
});

describe("link previews", () => {
  const series = [
    { hour: Math.floor(Date.now() / 3_600_000), domain: "alpha.app", hits: 900 },
    { hour: Math.floor(Date.now() / 3_600_000), domain: "beta.app", hits: 300 },
  ];
  const withView = (apps: string[]) => statsPage({
    total: 10, hiddenCount: 0, rows: [], totalSearches: 0, avgLatency: 0, avgCacheLatency: 0,
    handlePct: "99.9", avatarPct: "0", snapshots: [], trafficSources: [],
    trafficSeries: series, trafficSeries5m: [], trafficDaily: [],
    view: { apps, range: "7d", origin: "https://typeahead.waow.tech" },
  } as StatsData);

  test("a shared comparison names what is being compared", () => {
    const html = withView(["alpha.app", "beta.app"]);
    expect(html).toContain('<meta property="og:title" content="alpha.app vs beta.app · typeahead traffic">');
    expect(html).toContain("alpha.app 900 (75.0%)");
    // a ratio, not a ranking — request volume isn't proportional to adoption.
    // Scoped to the meta tag: the word appears in a source comment explaining
    // exactly this change, and asserting on the whole document catches that.
    const desc = html.match(/<meta property="og:description" content="([^"]*)"/)?.[1] ?? "";
    expect(desc).toContain("alpha.app 3.00× beta.app by request volume");
    expect(desc).not.toContain("leads");
  });

  test("the card image is handed the same params as the page", () => {
    expect(withView(["alpha.app", "beta.app"]))
      .toContain('content="https://typeahead.waow.tech/og-image?apps=alpha.app%2Cbeta.app"');
  });

  test("apps with no traffic in the window are dropped, not previewed as zero", () => {
    const html = withView(["alpha.app", "ghost.app"]);
    expect(html).not.toContain("ghost.app");
    expect(html).toContain('content="alpha.app · typeahead traffic">');
  });

  test("unfiltered falls back to describing the service", () => {
    const html = withView([]);
    expect(html).toContain('<meta property="og:title" content="typeahead — stats">');
    expect(html).toContain("99.9% with a resolved handle");
  });
});
