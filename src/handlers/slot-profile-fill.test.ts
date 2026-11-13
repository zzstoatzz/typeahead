import { afterAll, expect, test } from "bun:test";
import { tursoPolicyTables } from "../control-d1-fixture";
import { PROFILE_FILL_SQL, bannerCid, handleSlotProfileFill } from "./slot-control";

// The one-time copy of descriptions and banners from Turso to a store slot:
// docs/turso-retirement.md.

const { turso, dispose } = await tursoPolicyTables();
afterAll(dispose);

const SECRET = "slot-control-secret-of-enough-length";
const ADMIN = "worker-admin-secret-of-enough-length";
const CID = "bafkreibannerbannerbannerbannerbannerbannerbannerbannerbanner";

const did = (n: number) => `did:plc:${String(n).padStart(24, "0")}`;

// ten actors in DID order; 3, 4 and 5 hold nothing to copy
const HELD = new Map<number, [description: string | null, banner: string | null]>([
  [1, ["first", ""]],
  [2, ["", CID]],
  [3, ["", ""]],
  [4, [null, null]],
  [5, ["", ""]],
  [6, ["line one\nline two", `https://cdn.bsky.app/img/banner/plain/${did(6)}/${CID}@jpeg`]],
  [7, ["", `https://pds.example/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(did(7))}&cid=${CID}`]],
  [8, ["eighth", ""]],
  [9, ["", "https://pds.example/xrpc/com.atproto.sync.getBlob?cid=%E0%A4%A"]],
  [10, ["tenth", CID]],
]);
// inserted out of DID order, so the page order is the key's and not the rowid's
await turso.batch([10, 3, 7, 1, 9, 5, 2, 8, 4, 6].map((n) => {
  const [description, banner] = HELD.get(n) ?? ["", ""];
  return turso.prepare("INSERT INTO actors (did, handle, description, banner_url) VALUES (?1, ?2, ?3, ?4)")
    .bind(did(n), `actor${n}.test`, description, banner);
}));

const limiter = (success: boolean) => {
  const keys: string[] = [];
  return { keys, limit: async ({ key }: { key: string }) => { keys.push(key); return { success }; } };
};
const env = (fields: { secret?: string; actors?: string; limited?: boolean } = {}) => ({
  ADMIN_SECRET: ADMIN,
  SLOT_CONTROL_SECRET: "secret" in fields ? fields.secret : SECRET,
  TURSO_ACTORS: fields.actors,
  RATE_LIMITER_STRICT: limiter(!fields.limited),
});
const get = (query: string, headers: Record<string, string> = { Authorization: `Bearer ${SECRET}` }) =>
  new Request(`https://typeahead.waow.tech/slot/control/profile-fill${query}`, { headers });
const page = async (query: string) => {
  const res = await handleSlotProfileFill(get(query), env(), turso);
  expect(res.status).toBe(200);
  expect(res.headers.get("Cache-Control")).toBe("no-store");
  return JSON.parse(await res.text());
};

test("a page holds the rows with a description or a banner, in DID order, and names the last DID read", async () => {
  expect(await page("?limit=2")).toEqual({
    rows: [{ did: did(1), description: "first", banner: "" }, { did: did(2), description: "", banner: CID }],
    next: did(2),
  });
});

test("a page with nothing to copy still moves on", async () => {
  expect(await page(`?after=${did(2)}&limit=3`)).toEqual({ rows: [], next: did(5) });
});

test("following next reads every row once and ends with null", async () => {
  const seen: string[] = [];
  let after = "";
  let calls = 0;
  for (;;) {
    const body = await page(`?after=${after}&limit=4`);
    calls += 1;
    for (const row of body.rows) seen.push(row.did);
    if (body.next === null) break;
    after = body.next;
  }
  expect(calls).toBe(3);
  expect(seen).toEqual([1, 2, 6, 7, 8, 10].map(did));
  // a table that ends on a page boundary takes one more, empty, call
  expect(await page(`?after=${did(5)}&limit=5`)).toMatchObject({ next: did(10) });
  expect(await page(`?after=${did(10)}`)).toEqual({ rows: [], next: null });
});

test("a banner is served as its CID whatever shape Turso holds", async () => {
  const { rows } = await page("?limit=1000");
  const banners = Object.fromEntries(rows.map((row: { did: string; banner: string }) => [row.did, row.banner]));
  expect(banners).toEqual({ [did(1)]: "", [did(2)]: CID, [did(6)]: CID, [did(7)]: CID, [did(8)]: "", [did(10)]: CID });
  expect(rows.find((row: { did: string }) => row.did === did(6)).description).toBe("line one\nline two");
  expect(bannerCid("")).toBe("");
  expect(bannerCid(CID)).toBe(CID);
  expect(bannerCid(`https://cdn.bsky.app/img/banner/plain/${did(6)}/${CID}`)).toBe(CID);
  expect(bannerCid("https://pds.example/xrpc/com.atproto.sync.getBlob?cid=%E0%A4%A")).toBe("");
});

test("the limit defaults to 500 and is refused above 1000", async () => {
  expect((await page("")).next).toBeNull();
  expect((await handleSlotProfileFill(get("?limit=1000"), env(), turso)).status).toBe(200);
  for (const query of ["?limit=0", "?limit=1001", "?limit=many", "?limit=-5", "?limit=1.5", "?after=alice.test", "?after=did:plc:a%20b"]) {
    expect((await handleSlotProfileFill(get(query), env(), turso)).status).toBe(400);
  }
});

test("only the slot secret opens it", async () => {
  const refused: Record<string, string>[] = [{}, { Authorization: `Bearer ${ADMIN}` }, { Authorization: `Bearer ${SECRET}x` }, { Authorization: SECRET }];
  for (const headers of refused) {
    const guarded = env();
    const res = await handleSlotProfileFill(get("", { ...headers, "CF-Connecting-IP": "203.0.113.9" }), guarded, turso);
    expect(res.status).toBe(401);
    expect(await res.text()).not.toContain("first");
    expect(guarded.RATE_LIMITER_STRICT.keys).toEqual(["slot-auth:203.0.113.9"]);
  }
  expect((await handleSlotProfileFill(get("", { Authorization: "Bearer wrong" }), env({ limited: true }), turso)).status).toBe(429);
  for (const secret of [undefined, "short", ADMIN]) {
    expect((await handleSlotProfileFill(get(""), env({ secret }), turso)).status).toBe(503);
  }
});

test("with Turso's actor tables retired it answers 410, after the secret is checked", async () => {
  const res = await handleSlotProfileFill(get(""), env({ actors: "off" }), turso);
  expect(res.status).toBe(410);
  expect(await res.text()).not.toContain("first");
  expect((await handleSlotProfileFill(get("", {}), env({ actors: "off" }), turso)).status).toBe(401);
  expect((await handleSlotProfileFill(get(""), env({ actors: "on" }), turso)).status).toBe(200);
});

test("the page query is a search of the primary key, with bound parameters", async () => {
  const { results } = await turso.prepare(`EXPLAIN QUERY PLAN ${PROFILE_FILL_SQL}`).bind(did(5), 500).all<{ detail: string }>();
  expect(results.map((row) => row.detail)).toEqual(["SEARCH actors USING INDEX sqlite_autoindex_actors_1 (did>?)"]);
});
