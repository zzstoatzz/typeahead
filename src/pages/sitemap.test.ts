import { expect, test } from "bun:test";
import { SITE_PATHS, robotsTxt, sitemapXml } from "./sitemap";

test("the sitemap lists the public pages and every letter, and robots.txt points to it", () => {
  const xml = sitemapXml();
  expect(xml.match(/<loc>/g)).toHaveLength(SITE_PATHS.length);
  for (const path of ["/", "/docs", "/stats", "/first", "/first/a", "/first/z"]) expect(xml).toContain(`<loc>https://typeahead.waow.tech${path}</loc>`);
  expect(xml).not.toContain("/eval");
  expect(robotsTxt()).toContain("Sitemap: https://typeahead.waow.tech/sitemap.xml");
});
