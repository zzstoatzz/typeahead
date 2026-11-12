import { afterAll, afterEach, beforeEach, expect, test } from "bun:test";
import { controlStore, tursoPolicyTables } from "../control-d1-fixture";
import { blockDomain, setOverride } from "../control-store";
import type { SqlDb } from "../db";
import type { SlotActor } from "../slot-read";
import type { Env } from "../types";
import { handleReputation } from "./reputation";

// /debug/reputation under PROFILE_MODE: the actor from Turso, or from the
// routed slot with the policy that applies to it from the control database.

const { d1, control, dispose: disposeControl } = await controlStore();
const { turso, dispose: disposeTurso } = await tursoPolicyTables();
const realFetch = globalThis.fetch;
afterAll(async () => {
  disposeTurso();
  await disposeControl();
});

const DID = "did:plc:reputationreputationrepu";
const SHADOW = "did:plc:shadowshadowshadowshadow";

await turso.batch([
  turso.prepare(
    `INSERT INTO actors (did, handle, display_name, hidden, followers_count, quality_score, profile_checked_at, updated_at, profile_source)
     VALUES (?1, 'rep.blocked.test', 'Turso Rep', 0, 10, 4.5, 1756000000, 1756400000, 'bsky')`).bind(DID),
  turso.prepare("INSERT INTO mod_overrides (did, action, reason, created_at) VALUES (?1, 'hide', 'turso reason', 1750000000)").bind(DID),
]);
await setOverride(control, DID, "show", "control reason", 1760000000);
await blockDomain(control, "blocked.test", "spam wave", 1760000001);

function stored(changes: Partial<SlotActor>): SlotActor {
  return {
    did: DID, handle: "rep.blocked.test", display_name: "Slot Rep", avatar_url: "bafkreirep", labels: [], created_at: "2024-01-01T00:00:00Z",
    associated: {}, pds: "https://pds.test", description: "", banner_url: "", followers_count: 20, follows_count: 2, posts_count: 3,
    quality_score: 6.25, updated_at: 1757000000, profile_checked: true, hidden: false, hidden_stored: true, ...changes,
  };
}

let asked: string[] = [];
let slotAnswer: (url: URL) => Response = () => Response.json({ actors: [] });
beforeEach(() => {
  asked = [];
  // every upstream the report asks besides the slot (labelers, the search
  // positions, the appview) is down: the report degrades and still answers
  const down = async () => new Response("down", { status: 503 });
  globalThis.fetch = Object.assign(down, { preconnect: realFetch.preconnect });
});
afterEach(() => { globalThis.fetch = realFetch; });

function env(mode: string | undefined): Env {
  const transport = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      const url = new URL(String(input));
      asked.push(url.pathname + url.search);
      return slotAnswer(url);
    },
  };
  // SAFETY: the handler reads PROFILE_MODE, CONTROL_DB and the two search
  // settings, and uses the binding only through fetch.
  return { PROFILE_MODE: mode, CONTROL_DB: d1, SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport } as Env;
}

const noTurso: SqlDb = {
  prepare() { throw new Error("Turso was read"); },
  batch() { throw new Error("Turso was read"); },
};
const report = (actor: string) => new Request(`https://typeahead.waow.tech/debug/reputation?actor=${encodeURIComponent(actor)}`);
const body = async (res: Response) => JSON.parse(await res.text());

test("slot mode reports the slot's actor and the control database's policy, without reading Turso", async () => {
  slotAnswer = (url) => Response.json({
    actors: url.searchParams.get("actor") === "rep.blocked.test"
      ? [stored({}), stored({ did: SHADOW, hidden: true })]
      : [stored({})],
  });
  const res = await handleReputation(report(DID), noTurso, env("slot"));
  expect(res.status).toBe(200);
  const answer = await body(res);
  expect(asked).toEqual([`/actors?actor=${encodeURIComponent(DID)}`, "/actors?actor=rep.blocked.test"]);
  expect(answer.actor).toEqual({
    did: DID,
    handle: "rep.blocked.test",
    displayName: "Slot Rep",
    avatar: `https://cdn.bsky.app/img/avatar/plain/${DID}/bafkreirep`,
    avatarFallback: `https://pds.test/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(DID)}&cid=bafkreirep`,
    createdAt: "2024-01-01T00:00:00Z",
    pds: "https://pds.test",
  });
  expect(answer.score.stored).toBe(6.25);
  expect(answer.moderation).toMatchObject({
    hiddenStored: true,
    hiddenComputed: false,
    override: { action: "show", reason: "control reason", createdAt: "2025-10-09T08:53:20.000Z" },
    domainBlocked: { domain: "blocked.test", reason: "spam wave", createdAt: "2025-10-09T08:53:21.000Z" },
  });
  expect(answer.indexing).toEqual({
    enrichedOnView: false,
    updatedAt: "2025-09-04T15:33:20.000Z",
    profileCheckedAt: null,
    profileChecked: true,
    profileSource: "",
    shouldBeInPrefixIndex: true,
    exactHandleLookupFindsIt: true,
    exactHandleLookupDid: DID,
  });
});

test("slot mode answers 404 for an actor the slot does not hold and 503 when the slot does not answer", async () => {
  slotAnswer = () => Response.json({ actors: [] });
  const missing = await handleReputation(report("nobody.test"), noTurso, env("slot"));
  expect(missing.status).toBe(404);
  expect(await body(missing)).toEqual({ error: "actor not found" });

  slotAnswer = () => Response.json({ error: "not ready" }, { status: 503 });
  const down = await handleReputation(report("nobody.test"), noTurso, env("slot"));
  expect(down.status).toBe(503);
  expect(await body(down)).toEqual({ error: "actor lookup is temporarily unavailable" });
});

test("turso and compare modes report Turso's actor and Turso's policy, and never ask the slot", async () => {
  for (const mode of [undefined, "turso", "compare"]) {
    asked = [];
    const res = await handleReputation(report("REP.blocked.test"), turso, env(mode));
    expect(res.status).toBe(200);
    const answer = await body(res);
    expect(answer.actor.displayName).toBe("Turso Rep");
    expect(answer.moderation.override).toEqual({ action: "hide", reason: "turso reason", createdAt: "2025-06-15T15:06:40.000Z" });
    expect(answer.moderation.domainBlocked).toBeNull();
    expect(answer.indexing).toMatchObject({
      profileCheckedAt: "2025-08-24T01:46:40.000Z", profileChecked: true, profileSource: "bsky",
      exactHandleLookupFindsIt: true, exactHandleLookupDid: DID,
    });
    expect(asked).toEqual([]);
  }
});
