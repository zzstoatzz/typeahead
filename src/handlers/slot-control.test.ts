import { afterAll, expect, test } from "bun:test";
import { controlStore } from "../control-d1-fixture";
import { blockDomain, clearOverride, recordIndexRequest, setOverride } from "../control-store";
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
  expect(res.headers.get("ETag")).toBe('"policy-3"');
  expect(res.headers.get("Cache-Control")).toBe("no-store");
  expect(res.headers.get("Access-Control-Allow-Origin")).toBeNull();
  const text = await res.text();
  expect(JSON.parse(text)).toEqual({
    revision: 3,
    overrides: [{ did: "did:plc:hidden", action: "hide" }],
    blocked_domains: ["spam.example"],
  });
  expect(text).not.toContain("note");
});

test("a slot that holds the current revision is answered 304, by after or by ETag", async () => {
  const byAfter = await handleSlotPolicy(get("/slot/control/policy?after=3"), env());
  expect(byAfter.status).toBe(304);
  expect(byAfter.headers.get("ETag")).toBe('"policy-3"');
  expect(await byAfter.text()).toBe("");

  const byTag = await handleSlotPolicy(
    get("/slot/control/policy", { Authorization: `Bearer ${SECRET}`, "If-None-Match": '"policy-3"' }), env());
  expect(byTag.status).toBe(304);

  for (const path of ["/slot/control/policy?after=2", "/slot/control/policy?after=0", "/slot/control/policy?after=9"]) {
    const res = await handleSlotPolicy(get(path), env());
    expect(res.status).toBe(200);
    expect((await body(res)).revision).toBe(3);
  }
  const stale = await handleSlotPolicy(
    get("/slot/control/policy", { Authorization: `Bearer ${SECRET}`, "If-None-Match": '"policy-2"' }), env());
  expect(stale.status).toBe(200);
});

test("a removal reaches the next snapshot under a new revision", async () => {
  await clearOverride(control, "did:plc:hidden");
  const res = await handleSlotPolicy(get("/slot/control/policy?after=3"), env());
  expect(res.status).toBe(200);
  expect(await body(res)).toEqual({ revision: 4, overrides: [], blocked_domains: ["spam.example"] });
});

test("index requests page by sequence under a bounded limit", async () => {
  for (const [i, did] of ["did:plc:a", "did:plc:b", "did:plc:c"].entries()) await recordIndexRequest(control, did, NOW + i);
  const first = await handleSlotIndexRequests(get("/slot/control/index-requests?after=0&limit=2"), env());
  expect(first.status).toBe(200);
  expect(first.headers.get("Cache-Control")).toBe("no-store");
  expect(await body(first)).toEqual({ requests: [{ seq: 1, did: "did:plc:a" }, { seq: 2, did: "did:plc:b" }], more: true });
  const rest = await handleSlotIndexRequests(get("/slot/control/index-requests?after=2"), env());
  expect(await body(rest)).toEqual({ requests: [{ seq: 3, did: "did:plc:c" }], more: false });
  const all = await handleSlotIndexRequests(get("/slot/control/index-requests"), env());
  expect((await body(all)).requests).toHaveLength(3);
});

test("positions and limits that are not plain numbers are refused", async () => {
  for (const path of ["?after=-1", "?after=1.5", "?after=abc", "?after=", "?after=1e3", "?after=9999999999999999"]) {
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
