import { afterAll, expect, test } from "bun:test";
import { GENERATION, controlStore } from "../control-d1-fixture";
import { NEW_GENERATION_SQL, blockDomain, clearOverride, recordIndexRequest, setOverride } from "../control-store";
import { handleSlotIndexRequests, handleSlotPolicy } from "./slot-control";

const { d1, control, dispose } = await controlStore();
afterAll(dispose);

const SECRET = "slot-control-secret-of-enough-length";
const ADMIN = "worker-admin-secret-of-enough-length";
const NOW = 1_794_400_000;

const limiter = (success: boolean) => {
  const keys: string[] = [];
  return { keys, limit: async ({ key }: { key: string }) => { keys.push(key); return { success }; } };
};
const env = (fields: { secret?: string; limited?: boolean } = {}) => ({
  CONTROL_DB: d1,
  ADMIN_SECRET: ADMIN,
  SLOT_CONTROL_SECRET: "secret" in fields ? fields.secret : SECRET,
  RATE_LIMITER_STRICT: limiter(!fields.limited),
});
const body = async (res: Response) => JSON.parse(await res.text());
const get = (path: string, headers: Record<string, string> = { Authorization: `Bearer ${SECRET}` }) =>
  new Request(`https://typeahead.waow.tech${path}`, { headers });

test("the policy endpoint serves the snapshot with its revision as the ETag", async () => {
  await setOverride(control, "did:plc:hidden", "hide", "operator note", NOW);
  await blockDomain(control, "spam.example", "another note", NOW);
  const res = await handleSlotPolicy(get("/slot/control/policy"), env());
  expect(res.status).toBe(200);
  expect(res.headers.get("ETag")).toBe('"policy-4242-3"');
  expect(res.headers.get("Cache-Control")).toBe("no-store");
  expect(res.headers.get("Access-Control-Allow-Origin")).toBeNull();
  const text = await res.text();
  expect(JSON.parse(text)).toEqual({
    generation: GENERATION,
    revision: 3,
    overrides: [{ did: "did:plc:hidden", action: "hide" }],
    blocked_domains: ["spam.example"],
  });
  expect(text).not.toContain("note");
});

test("a slot that holds the current revision of this generation is answered 304, by position or by ETag", async () => {
  const byAfter = await handleSlotPolicy(get("/slot/control/policy?generation=4242&after=3"), env());
  expect(byAfter.status).toBe(304);
  expect(byAfter.headers.get("ETag")).toBe('"policy-4242-3"');
  expect(await byAfter.text()).toBe("");

  const byTag = await handleSlotPolicy(
    get("/slot/control/policy", { Authorization: `Bearer ${SECRET}`, "If-None-Match": '"policy-4242-3"' }), env());
  expect(byTag.status).toBe(304);

  // an older or later revision, the revision without its generation, and
  // the same revision of another generation all get the whole policy
  for (const path of [
    "/slot/control/policy?generation=4242&after=2", "/slot/control/policy?generation=4242&after=0",
    "/slot/control/policy?generation=4242&after=9", "/slot/control/policy?after=3",
    "/slot/control/policy?generation=4241&after=3", "/slot/control/policy?generation=4242",
  ]) {
    const res = await handleSlotPolicy(get(path), env());
    expect(res.status).toBe(200);
    expect((await body(res)).revision).toBe(3);
  }
  const stale = await handleSlotPolicy(
    get("/slot/control/policy", { Authorization: `Bearer ${SECRET}`, "If-None-Match": '"policy-4242-2"' }), env());
  expect(stale.status).toBe(200);
});

test("a removal reaches the next snapshot under a new revision", async () => {
  await clearOverride(control, "did:plc:hidden");
  const res = await handleSlotPolicy(get("/slot/control/policy?generation=4242&after=3"), env());
  expect(res.status).toBe(200);
  expect(await body(res)).toEqual({ generation: GENERATION, revision: 4, overrides: [], blocked_domains: ["spam.example"] });
});

test("index requests page by sequence under a bounded limit", async () => {
  for (const [i, did] of ["did:plc:a", "did:plc:b", "did:plc:c"].entries()) await recordIndexRequest(control, did, NOW + i);
  const first = await handleSlotIndexRequests(get("/slot/control/index-requests?after=0&limit=2"), env());
  expect(first.status).toBe(200);
  expect(first.headers.get("Cache-Control")).toBe("no-store");
  expect(await body(first)).toEqual({ generation: GENERATION, requests: [{ seq: 1, did: "did:plc:a" }, { seq: 2, did: "did:plc:b" }], more: true });
  const rest = await handleSlotIndexRequests(get("/slot/control/index-requests?generation=4242&after=2"), env());
  expect(await body(rest)).toEqual({ generation: GENERATION, requests: [{ seq: 3, did: "did:plc:c" }], more: false });
  // a position without its generation, or from another one, starts at the top
  for (const path of ["/slot/control/index-requests?after=2", "/slot/control/index-requests?generation=7&after=2", "/slot/control/index-requests?generation=7&after=900"]) {
    expect((await body(await handleSlotIndexRequests(get(path), env()))).requests.map((r: { seq: number }) => r.seq)).toEqual([1, 2, 3]);
  }
  const all = await handleSlotIndexRequests(get("/slot/control/index-requests"), env());
  expect((await body(all)).requests).toHaveLength(3);
});

test("positions and limits that are not plain numbers are refused", async () => {
  for (const path of ["?after=-1", "?after=1.5", "?after=abc", "?after=", "?after=1e3", "?after=9999999999999999", "?generation=x&after=1", "?generation=-4&after=1"]) {
    expect((await handleSlotPolicy(get(`/slot/control/policy${path}`), env())).status).toBe(400);
    expect((await handleSlotIndexRequests(get(`/slot/control/index-requests${path}`), env())).status).toBe(400);
  }
  for (const path of ["?limit=0", "?limit=501", "?limit=many", "?limit=-5"]) {
    expect((await handleSlotIndexRequests(get(`/slot/control/index-requests${path}`), env())).status).toBe(400);
  }
  expect((await handleSlotIndexRequests(get("/slot/control/index-requests?limit=500"), env())).status).toBe(200);
});

test("only the slot secret opens the endpoints", async () => {
  const refused: Record<string, string>[] = [
    {},
    { Authorization: `Bearer ${ADMIN}` },
    { Authorization: `Bearer ${SECRET}x` },
    { Authorization: `Bearer ${SECRET.slice(0, -1)}` },
    { Authorization: SECRET },
    { Authorization: `Basic ${SECRET}` },
    { Authorization: "Bearer " },
  ];
  for (const headers of refused) {
    for (const [handler, path] of [[handleSlotPolicy, "/slot/control/policy"], [handleSlotIndexRequests, "/slot/control/index-requests"]] as const) {
      const guarded = env();
      const res = await handler(get(path, { ...headers, "CF-Connecting-IP": "203.0.113.9" }), guarded);
      expect(res.status).toBe(401);
      expect(await body(res)).toEqual({ error: "unauthorized" });
      expect(guarded.RATE_LIMITER_STRICT.keys).toEqual(["slot-auth:203.0.113.9"]);
    }
  }
  const open = env();
  expect((await handleSlotPolicy(get("/slot/control/policy"), open)).status).toBe(200);
  expect(open.RATE_LIMITER_STRICT.keys).toEqual([]);
});

test("repeated failures from one address are rate limited", async () => {
  const res = await handleSlotPolicy(get("/slot/control/policy", { Authorization: "Bearer wrong" }), env({ limited: true }));
  expect(res.status).toBe(429);
});

test("without a usable secret the endpoints are off, even to the admin secret", async () => {
  for (const secret of [undefined, "", "short", ADMIN]) {
    for (const offered of [secret ?? "", ADMIN, SECRET]) {
      const res = await handleSlotPolicy(get("/slot/control/policy", { Authorization: `Bearer ${offered}` }), env({ secret }));
      expect(res.status).toBe(503);
      const requests = await handleSlotIndexRequests(get("/slot/control/index-requests", { Authorization: `Bearer ${offered}` }), env({ secret }));
      expect(requests.status).toBe(503);
    }
  }
});

test("a revision that climbs back to the one a slot holds is not answered 304 once the generation is new", async () => {
  const store = await controlStore();
  const restored = { ...env(), CONTROL_DB: store.d1 };
  const poll = (generation: number, after: number) =>
    handleSlotPolicy(get(`/slot/control/policy?generation=${generation}&after=${after}`), restored);
  try {
    await blockDomain(store.control, "spam.example", "", 1);
    expect((await poll(GENERATION, 2)).status).toBe(304);
    // restored to revision 1 with an empty policy, then one unrelated write:
    // revision 2 again, over a different policy
    await store.d1.batch([
      store.d1.prepare("DELETE FROM blocked_domains"),
      store.d1.prepare("UPDATE control_meta SET value = 1 WHERE name = 'policy_revision'"),
    ]);
    await setOverride(store.control, "did:plc:x", "hide", "", 2);
    expect((await poll(GENERATION, 2)).status).toBe(304);

    await store.d1.prepare(NEW_GENERATION_SQL).run();
    const res = await poll(GENERATION, 2);
    expect(res.status).toBe(200);
    const served = await body(res);
    expect(served.generation).not.toBe(GENERATION);
    expect(served).toMatchObject({ revision: 2, overrides: [{ did: "did:plc:x", action: "hide" }], blocked_domains: [] });
    expect(res.headers.get("ETag")).toBe(`"policy-${served.generation}-2"`);
    expect((await poll(served.generation, 2)).status).toBe(304);
    // the old ETag does not match either
    const byTag = await handleSlotPolicy(get("/slot/control/policy", { Authorization: `Bearer ${SECRET}`, "If-None-Match": '"policy-4242-2"' }), restored);
    expect(byTag.status).toBe(200);
  } finally {
    await store.dispose();
  }
});
