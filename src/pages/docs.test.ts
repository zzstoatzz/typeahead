import { expect, test } from "bun:test";
import { docsPage } from "./docs";

test("every docs section and expandable topic has a link to itself", () => {
  const html = docsPage();
  const targets = [...html.matchAll(/<(?:section|details) id="([^"]+)"/g)].map((m) => m[1]);
  const anchors = [...html.matchAll(/<a class="anchor" href="#([^"]+)"/g)].map((m) => m[1]);
  expect(targets.length).toBeGreaterThan(0);
  expect(anchors).toEqual(targets);
});

test("a link into a closed topic opens it", () => {
  const html = docsPage();
  expect(html).toContain("target.closest('details')");
  expect(html).toContain("window.addEventListener('hashchange', reveal);");
});
