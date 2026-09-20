import { test, expect } from "bun:test";
import { endpointLabel, recordRequest, trafficDomain } from "./metrics";

// request_buckets is keyed by (bucket, domain, endpoint, status). Its row count
// is bounded only because endpoint is a FIXED label set: every path that is not
// a known route must collapse to "other", or a scanner probing /wp-admin,
// /.env, /phpinfo.php mints a row per guess.
test("endpointLabel: known routes get stable labels, everything else is 'other'", () => {
  expect(endpointLabel("/xrpc/tech.waow.typeahead.searchActors")).toBe("search");
  expect(endpointLabel("/xrpc/app.bsky.actor.searchActorsTypeahead")).toBe("search");
  expect(endpointLabel("/xrpc/app.bsky.actor.getProfiles")).toBe("getProfiles");
  expect(endpointLabel("/request-indexing")).toBe("request-indexing");
  expect(endpointLabel("/admin/ingest")).toBe("admin/ingest");
  expect(endpointLabel("/admin/delete")).toBe("admin/delete");
  expect(endpointLabel("/admin/mod-override")).toBe("admin");
  expect(endpointLabel("/health/freshness")).toBe("health");
  expect(endpointLabel("/")).toBe("page");
  expect(endpointLabel("/stats")).toBe("page");
  expect(endpointLabel("/favicon.svg")).toBe("page");
  expect(endpointLabel("/apple-touch-icon.png")).toBe("page");
  expect(endpointLabel("/site.webmanifest")).toBe("page");
  expect(endpointLabel("/favicon.ico")).toBe("other");
  for (const probe of ["/wp-admin", "/.env", "/xrpc/com.atproto.nope", "/admin", "/search"]) {
    expect(endpointLabel(probe)).toBe("other");
  }
});

test("trafficDomain: X-Client > Origin > Referer(self→homepage) > unknown; non-hostnames rejected", () => {
  const at = (h: Record<string, string>) => trafficDomain(new Request("https://typeahead.waow.tech/x", { headers: h }));
  expect(at({ "X-Client": "northsky.app", Origin: "https://other.example" })).toBe("northsky.app");
  expect(at({ Origin: "https://northsky.app" })).toBe("northsky.app");
  expect(at({ Referer: "https://typeahead.waow.tech/" })).toBe("homepage");
  expect(at({ Referer: "https://plyr.fm/track/1" })).toBe("plyr.fm");
  expect(at({})).toBe("unknown");
  expect(at({ Origin: "http://localhost:3000" })).toBe("unknown");
  expect(at({ "X-Client": "<script>alert(1)</script>" })).toBe("unknown");
  expect(at({ "X-Client": "a".repeat(65) })).toBe("unknown");
  expect(at({ Origin: "not a url" })).toBe("unknown");
});

test("recordRequest: one UPSERT keyed by the full (bucket, domain, endpoint, status)", async () => {
  const calls: { sql: string; binds: any[] }[] = [];
  const db: any = {
    prepare(sql: string) {
      const s: any = { bind(...a: any[]) { s.binds = a; return s; }, async run() { calls.push({ sql, binds: s.binds }); return { meta: { changes: 1 } }; } };
      return s;
    },
  };
  await recordRequest(db, "northsky.app", "search", 429);
  expect(calls).toHaveLength(1);
  expect(calls[0].sql).toMatch(/INSERT INTO request_buckets/);
  expect(calls[0].sql).toMatch(/ON CONFLICT\(bucket, domain, endpoint, status\)/);
  expect(calls[0].binds.slice(1)).toEqual(["northsky.app", "search", 429]);
  expect(calls[0].binds[0]).toBe(Math.floor(Date.now() / 300_000));
});
