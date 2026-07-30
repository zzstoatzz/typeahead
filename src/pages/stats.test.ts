import { describe, expect, test } from "bun:test";
import { statsPage, type StatsData } from "./stats";

function page(domains: string[]): string {
  const d: StatsData = {
    total: 100, hiddenCount: 0, rows: [], totalSearches: 0,
    avgLatency: 0, avgCacheLatency: 0, handlePct: "0", avatarPct: "0",
    snapshots: [],
    trafficSources: domains.map((domain, i) => ({ domain, hits: 100 - i })),
    trafficSeries: [], trafficSeries5m: [],
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
  // Regression: the domain name was an <a target="_blank">, and the click
  // handler skips anchors so links could still navigate. That made the largest
  // part of every legend entry and row unselectable — on touch, where the thumb
  // lands on the name, multi-select was effectively unreachable and taps opened
  // the app's website instead.
  const html = page(["plyr.fm", "unknown"]);

  test("the legend entry is not a link", () => {
    const entry = legendEntry(html, "plyr.fm");
    expect(entry).toContain("plyr.fm");
    expect(entry).not.toContain("<a ");
  });

  test("the table row's domain cell is not a link", () => {
    const cell = tableRow(html, "plyr.fm").match(/<td class="ts-domain">.*?<\/td>/s)?.[0] ?? "";
    expect(cell).toContain("plyr.fm");
    expect(cell).not.toContain("<a ");
  });

  test("navigation stays available as its own target", () => {
    const row = tableRow(html, "plyr.fm");
    expect(row).toContain('class="visit"');
    expect(row).toContain('href="https://plyr.fm"');
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
