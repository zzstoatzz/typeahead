import { expect, test } from "bun:test";
import { SlotReadError, readMode, slotActors, slotCorpus, type SlotEnv } from "./slot-read";

function slot(answer: (url: string) => Response | Promise<Response>) {
  const asked: string[] = [];
  const transport = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      asked.push(String(input));
      return answer(String(input));
    },
  };
  // SAFETY: the reads use the binding only through fetch(url, init).
  const env: SlotEnv = { SEARCH_BACKEND_URL: "https://transport.test", SEARCH_TRANSPORT: transport as Fetcher };
  return { env, asked };
}

const ACTOR = {
  did: "did:plc:a", handle: "a.test", display_name: "A", avatar_url: "bafk", labels: [{ val: "x" }], created_at: "",
  associated: { labeler: true }, pds: "https://pds.test", description: "d", banner_url: "", followers_count: 1,
  follows_count: 2, posts_count: 3, quality_score: 1.5, updated_at: 10, profile_checked: true, hidden: false, hidden_stored: false,
};
const CORPUS = {
  index: { actors: 10, searchable: 8, handle_invalid: 1, quarantined: 0, watermark: 1794000000 },
  store: { live: 12, total: 11, with_handles: 10, with_avatars: 6, hidden: 1, scanned_at: 1794000000 },
};

async function failure(read: Promise<unknown>): Promise<SlotReadError> {
  try {
    await read;
  } catch (error) {
    if (error instanceof SlotReadError) return error;
    throw error;
  }
  throw new Error("the read did not fail");
}

test("actors are asked for through the transport, each name encoded, and nothing is asked for none", async () => {
  const { env, asked } = slot(() => Response.json({ actors: [ACTOR] }));
  expect(await slotActors(env, [])).toEqual([]);
  expect(asked).toEqual([]);
  expect(await slotActors(env, ["did:plc:a", "b c.test"])).toEqual([ACTOR]);
  expect(asked).toEqual(["https://transport.test/actors?actor=did%3Aplc%3Aa&actor=b%20c.test"]);
});

test("more than 25 actors, a refusal, another shape and a timeout are all a SlotReadError", async () => {
  const many = Array.from({ length: 26 }, (_, n) => `did:plc:${n}`);
  expect((await failure(slotActors(slot(() => Response.json({ actors: [] })).env, many))).status).toBeNull();
  expect((await failure(slotActors(slot(() => Response.json({ error: "not ready" }, { status: 503 })).env, ["a.test"]))).status).toBe(503);
  for (const answer of [
    () => Response.json({ actors: [{ ...ACTOR, hidden: "no" }] }),
    () => Response.json({ profiles: [] }),
    () => new Response("<html>"),
    () => { throw new DOMException("timed out", "TimeoutError"); },
  ]) {
    expect((await failure(slotActors(slot(answer).env, ["a.test"]))).status).toBeNull();
  }
  expect((await failure(slotActors({}, ["a.test"]))).message).toBe("no search backend is configured");
});

test("corpus counts are read with or without the store's count", async () => {
  expect(await slotCorpus(slot(() => Response.json(CORPUS)).env)).toEqual({
    index: { actors: 10, searchable: 8, handle_invalid: 1, quarantined: 0 },
    store: CORPUS.store,
  });
  expect((await slotCorpus(slot(() => Response.json({ ...CORPUS, store: null })).env)).store).toBeNull();
  expect((await failure(slotCorpus(slot(() => Response.json({ index: { actors: -1 } })).env))).status).toBeNull();
});

test("a mode is turso unless it is exactly compare or slot", () => {
  expect([undefined, "", "turso", "Slot", "compare ", "1"].map(readMode)).toEqual(["turso", "turso", "turso", "turso", "turso", "turso"]);
  expect(readMode("compare")).toBe("compare");
  expect(readMode("slot")).toBe("slot");
});
