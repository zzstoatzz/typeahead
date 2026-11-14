import { afterAll, afterEach, beforeEach, expect, test } from "bun:test";
import { controlStore } from "../control-d1-fixture";
import { blockDomain, setOverride } from "../control-store";
import type { SlotActor } from "../slot-read";
import type { Env } from "../types";
import { handleReputation } from "./reputation";

// /debug/reputation: the actor from the routed slot, with the policy that
// applies to it from the control database.

const { d1, control, dispose: disposeControl } = await controlStore();
const realFetch = globalThis.fetch;
afterAll(disposeControl);

const DID = "did:plc:reputationreputationrepu";
const SHADOW = "did:plc:shadowshadowshadowshadow";

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

function env(): Env {
  const transport = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      const url = new URL(String(input));
      asked.push(url.pathname + url.search);
      return slotAnswer(url);
    },
  };
  // SAFETY: the handler reads CONTROL_DB and the two search settings, and
  // uses the binding only through fetch.
  return { CONTROL_DB: d1, SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport } as Env;
}

const report = (actor: string) => new Request(`https://typeahead.waow.tech/debug/reputation?actor=${encodeURIComponent(actor)}`);
const body = async (res: Response) => JSON.parse(await res.text());

test("the report is the slot's actor and the control database's policy", async () => {
  slotAnswer = (url) => Response.json({
    actors: url.searchParams.get("actor") === "rep.blocked.test"
      ? [stored({}), stored({ did: SHADOW, hidden: true })]
      : [stored({})],
  });
  const res = await handleReputation(report(DID), env());
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

test("an actor the slot does not hold is a 404, and a slot that does not answer a 503", async () => {
  slotAnswer = () => Response.json({ actors: [] });
  const missing = await handleReputation(report("nobody.test"), env());
  expect(missing.status).toBe(404);
  expect(await body(missing)).toEqual({ error: "actor not found" });

  slotAnswer = () => Response.json({ error: "not ready" }, { status: 503 });
  const down = await handleReputation(report("nobody.test"), env());
  expect(down.status).toBe(503);
  expect(await body(down)).toEqual({ error: "actor lookup is temporarily unavailable" });
});
