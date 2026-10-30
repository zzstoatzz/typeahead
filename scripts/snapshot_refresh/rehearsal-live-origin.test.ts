import { expect, test } from "bun:test";
import { createServer } from "node:http";
import { liveConfiguration, liveOriginAdapter } from "./rehearsal-live-origin";

const input = {
  schema_version: 1,
  native: { origin: "https://typeahead-startup-20261024.fly.dev", build_id: "b1792908655-native" },
  candidate: { engine: "tantivy", origin: "https://typeahead-tantivy-a.fly.dev", build_id: "b1792908655-candidate", format: "tantivy-0.27-f32-did-expiry-v4", admission_sha256: "a".repeat(64), binary_sha256: "b".repeat(64), retrieval_strategy: "global-merge" },
};

test("live configuration rejects arbitrary origins and unsupported candidates before network", () => {
  expect(() => liveConfiguration({ ...input, native: { ...input.native, origin: "https://example.com" } })).toThrow();
  expect(() => liveConfiguration({ ...input, candidate: { ...input.candidate, origin: "https://typeahead-tantivy-dev.fly.dev" } })).toThrow();
  expect(() => liveConfiguration({ ...input, queries: ["arbitrary"] })).toThrow();
});

test("approved live adapter uses bounded credential-free GETs and rejects redirects and oversized bodies", async () => {
  let mode = "normal";
  const calls: { method: string | undefined; authorization: string | undefined; cookie: string | undefined }[] = [];
  const server = createServer((request, response) => {
    calls.push({ method: request.method, authorization: request.headers.authorization, cookie: request.headers.cookie });
    if (mode === "redirect") { response.writeHead(302, { Location: "/redirect-target" }); response.end(); }
    else if (mode === "oversized") response.end(Buffer.alloc(1048577));
    else if (mode === "slow") {
      response.writeHead(200); response.write("{");
      const timer = setTimeout(() => response.end("}"), 3000);
      response.on("close", () => clearTimeout(timer));
    }
    else response.end('{"actors":[]}');
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Loopback server unavailable");
  const adapter = liveOriginAdapter(liveConfiguration(input), (_url, options) => fetch(`http://127.0.0.1:${address.port}/search`, options));
  const url = input.candidate.origin + "/search?q=zzstoatzz.io&limit=10";
  try {
    for (const request of [new Request(url, { method: "POST" }), new Request(url.replace("/search", "/admin/source-reconcile")), new Request(url.replace("zzstoatzz.io", "arbitrary")), new Request(url + "&limit=10")]) {
      await expect(adapter.request(request)).rejects.toThrow("egress rejected");
    }
    expect(calls).toHaveLength(0);
    const response = await adapter.request(new Request(url, { headers: { Authorization: "fixture-only", Cookie: "fixture-only" } }));
    expect(await response.text()).toBe('{"actors":[]}');
    mode = "redirect";
    await expect(adapter.request(new Request(url))).rejects.toThrow("redirect rejected");
    expect(calls).toHaveLength(2);
    mode = "oversized";
    await expect(adapter.request(new Request(url))).rejects.toThrow("response budget");
    mode = "slow";
    await expect(adapter.request(new Request(url))).rejects.toThrow();
    expect(calls.every(call => call.method === "GET" && !call.authorization && !call.cookie)).toBe(true);
    expect(adapter.evidence().request_count).toBe(4);
  } finally { await new Promise<void>(resolve => server.close(() => resolve())); }
}, 10000);
