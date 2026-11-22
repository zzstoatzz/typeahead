import { LETTERS } from "../first";

const ORIGIN = "https://typeahead.waow.tech";

/** The public pages a crawler should know about. `/eval` is unlisted. */
export const SITE_PATHS = ["/", "/docs", "/stats", "/reputation", "/first", ...LETTERS.map((letter) => `/first/${letter}`)];

export function sitemapXml(): string {
  const urls = SITE_PATHS.map((path) => `  <url><loc>${ORIGIN}${path}</loc></url>`).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls}
</urlset>
`;
}

/** Cloudflare puts its managed content signals above this. */
export function robotsTxt(): string {
  return `User-agent: *
Allow: /

Sitemap: ${ORIGIN}/sitemap.xml
`;
}
