import { describe, test, expect, afterEach } from "bun:test";
import { isProfileIdentical, refreshModeration } from "./cron";
import { extractProfileFields } from "./utils";
import type { Env } from "./types";
import type { SqlValue, Stmt, TursoDB } from "./db";

type Call = { sql: string; binds: SqlValue[]; method: string };
type FakeStmt = Stmt & { sql: string; binds: SqlValue[] };

/** the slice of KVNamespace refreshModeration consumes: the mod_walk_cursor key */
type CursorKv = {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
  delete(key: string): Promise<void>;
};

function envWithKv(kv: CursorKv): Env {
  // SAFETY: refreshModeration touches Env only through KV.get/put/delete on
  // the walk cursor, which CursorKv covers; no other binding is read.
  return { KV: kv } as Env;
}

const realFetch = globalThis.fetch;
function installFetch(fake: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>): void {
  globalThis.fetch = Object.assign(fake, { preconnect: realFetch.preconnect });
}

// regression: refreshModeration used to skip on `hidden === f.hidden &&
// labels === f.labels` only, freezing avatar / display_name / follower
// counts / quality_score forever for any actor whose moderation state
// was stable — i.e. nearly every actor. Especially bad for third-party-PDS
// accounts whose firehose commits the appview never indexed (pckt.blog →
// pds.pckt.cafe). This cron is their ONLY refresh path; the predicate
// must compare every materialized field so a real profile change forces
// a write.

const baseRow = {
  handle: "pckt.blog",
  hidden: 0,
  labels: "[]",
  display_name: "pckt",
  avatar_url: "bafkreicxfuixp6jfteqimt5kwanw2lp24cqocrbchih7c2arosujkoomla",
  created_at: "2024-01-01T00:00:00.000Z",
  associated: "{}",
  description: "",
  banner_url: "",
  profile_source: "bsky",
  followers_count: 100,
  follows_count: 50,
  posts_count: 200,
  quality_score: 276.5,
};

function makeProfile(overrides: Partial<{
  avatar: string; displayName: string; followersCount: number;
  followsCount: number; postsCount: number; createdAt: string;
}> = {}) {
  return {
    handle: "pckt.blog",
    displayName: overrides.displayName ?? "pckt",
    avatar: overrides.avatar ?? `https://cdn.bsky.app/img/avatar/plain/did/${baseRow.avatar_url}`,
    labels: [],
    createdAt: overrides.createdAt ?? baseRow.created_at,
    associated: {},
    followersCount: overrides.followersCount ?? baseRow.followers_count,
    followsCount: overrides.followsCount ?? baseRow.follows_count,
    postsCount: overrides.postsCount ?? baseRow.posts_count,
  };
}

describe("isProfileIdentical", () => {
  test("true when every field matches", () => {
    const f = extractProfileFields(makeProfile());
    expect(isProfileIdentical(baseRow, f)).toBe(true);
  });

  test("avatar change forces a write (the pckt.blog bug)", () => {
    const f = extractProfileFields(makeProfile({
      avatar: "https://cdn.bsky.app/img/avatar/plain/did/bafkreih43bdwlseexqj4ivgvkqfvk3dl2xtnvwsggp2u3md66hzlhyjbny",
    }));
    expect(isProfileIdentical(baseRow, f)).toBe(false);
  });

  test("display_name change forces a write", () => {
    const f = extractProfileFields(makeProfile({ displayName: "pckt (renamed)" }));
    expect(isProfileIdentical(baseRow, f)).toBe(false);
  });

  test("follower count change forces a write (popularity drift)", () => {
    const f = extractProfileFields(makeProfile({ followersCount: 250 }));
    expect(isProfileIdentical(baseRow, f)).toBe(false);
  });

  test("a different profile source forces a write even when every field matches", () => {
    const f = extractProfileFields(makeProfile());
    expect(isProfileIdentical(baseRow, f, "bsky")).toBe(true);
    expect(isProfileIdentical(baseRow, f, "blacksky")).toBe(false);
  });
});

// Bluesky answers AccountTakedown for some accounts it will not serve; the
// Blacksky appview still serves them, counts included. The cron must take
// those counts (so the account keeps a rank) and record where they came from.
describe("refreshModeration takes counts from the Blacksky appview on a Bluesky takedown", () => {
  const origFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = origFetch; });

  const DID = "did:plc:link";

  test("takedown → blacksky profile → UPDATE with counts and profile_source='blacksky'", async () => {
    const row = {
      rowid: 1, did: DID, handle: "spacelawshitpost.me", hidden: 0, labels: "[]",
      pds: "https://blacksky.app", display_name: "Łink", avatar_url: "bafOLD",
      created_at: "", associated: "{}", description: "", banner_url: "", profile_source: "pds",
      followers_count: 0, follows_count: 0, posts_count: 0, quality_score: 0,
    };
    const calls: Call[] = [];
    const stmt = (sql: string): FakeStmt => {
      const s: FakeStmt = {
        sql, binds: [],
        bind(...args) { s.binds = args; return s; },
        async all<T>() {
          calls.push({ sql, binds: s.binds, method: "all" });
          // SAFETY: the quality walk selects ACTOR_COLS, which is the shape `row` builds
          return sql.includes("ORDER BY quality_score DESC") ? { results: [row] as T[] } : { results: [] };
        },
        async run() { calls.push({ sql, binds: s.binds, method: "run" }); return { meta: { changes: 1 } }; },
        async first() { return null; },
      };
      return s;
    };
    const db: TursoDB = {
      prepare: stmt,
      async batch(stmts: FakeStmt[]) {
        for (const st of stmts) calls.push({ sql: st.sql, binds: st.binds, method: "batch" });
        return stmts.map(() => ({ results: [], meta: { changes: 1 } }));
      },
    };
    installFetch(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("app.bsky.actor.getProfiles")) return Response.json({ profiles: [] });
      if (url.startsWith("https://public.api.bsky.app/") && url.includes("getProfile?"))
        return Response.json({ error: "AccountTakedown", message: "Account has been suspended" }, { status: 400 });
      if (url.startsWith("https://api.blacksky.community/"))
        return Response.json({ did: DID, handle: "spacelawshitpost.me", displayName: "Łink. ⁂", labels: [], followersCount: 25783, followsCount: 2204, postsCount: 63554, createdAt: "2023-04-01T00:00:00.000Z", associated: {} });
      throw new Error("unexpected fetch " + url);
    });
    await refreshModeration(db, envWithKv({ async get() { return null; }, async put() {}, async delete() {} }));
    const update = calls.find((c) => c.method === "batch" && c.sql.includes("profile_source = ?14"));
    expect(update).toBeDefined();
    expect(update!.binds).toContain("blacksky");
    expect(update!.binds).toContain(25783);
    // the takedown did not turn into a delete
    expect(calls.some((c) => c.sql.startsWith("DELETE FROM actors"))).toBe(false);
  });
});

// regression: refreshModeration never advanced profile_checked_at — not on the
// content UPDATE, not on the skip path. Both selector pools order by that
// column, so the cursor never moved: the same head was re-selected every hour
// and the rest of the corpus starved. Symptom: germnetwork.com et al. froze on
// 2026-05-28 — stale avatar CIDs (404 on bsky CDN) and stale counts that never
// self-healed. The fix bumps profile_checked_at for every actor confirmed alive
// each run, CHANGED OR NOT — that's what makes the walk progress.
describe("refreshModeration advances the freshness cursor", () => {
  const origFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = origFetch; });

  const DID = "did:plc:germ";

  // a stale row the priority pool would surface (old avatar CID).
  function staleRow(qualityScore: number) {
    return {
      rowid: 1, did: DID, handle: "germnetwork.com", hidden: 0, labels: "[]",
      pds: "", display_name: "Germ Network", avatar_url: "bafOLDcid",
      created_at: "2024-01-01T00:00:00.000Z", associated: "{}",
      description: "", banner_url: "", profile_source: "bsky",
      followers_count: 3041, follows_count: 100, posts_count: 500,
      quality_score: qualityScore,
    };
  }

  // capturing mock db: routes SELECTs by SQL, records every statement issued.
  function mockDb(row: ReturnType<typeof staleRow>) {
    const calls: Call[] = [];
    function stmt(sql: string): FakeStmt {
      const s: FakeStmt = {
        sql, binds: [],
        bind(...args) { s.binds = args; return s; },
        async all<T>() {
          calls.push({ sql, binds: s.binds, method: "all" });
          // SAFETY: same contract as Stmt.all — T is the caller's declaration of
          // the columns its SELECT projects, and the quality walk selects ACTOR_COLS,
          // which is exactly the shape staleRow() builds.
          const walkPage = [row] as T[];
          if (sql.includes("ORDER BY quality_score DESC")) return { results: walkPage }; // quality walk
          return { results: [] };                                                       // event queue, overrides, domains
        },
        async run() { calls.push({ sql, binds: s.binds, method: "run" }); return { meta: { changes: 1 } }; },
        async first() { return null; },
      };
      return s;
    }
    const db: TursoDB = {
      prepare: (sql: string) => stmt(sql),
      async batch(stmts: FakeStmt[]) {
        for (const st of stmts) calls.push({ sql: st.sql, binds: st.binds, method: "batch" });
        return stmts.map(() => ({ results: [], meta: { changes: 1 } }));
      },
    };
    return { db, calls };
  }

  // KV mock capturing cursor movement
  function mockEnv(cursor: string | null = null) {
    const puts: { key: string; value: string }[] = [];
    const deletes: string[] = [];
    const kv = {
      puts, deletes,
      async get(_key: string) { return cursor; },
      async put(key: string, value: string) { kv.puts.push({ key, value }); },
      async delete(key: string) { kv.deletes.push(key); },
    };
    return { env: envWithKv(kv), kv };
  }

  function mockGetProfiles(profile: ReturnType<typeof makeProfile> & { did: string }) {
    installFetch(async () => Response.json({ profiles: [profile] }));
  }

  function cursorAdvances(calls: Call[]): boolean {
    return calls.some((c) =>
      /UPDATE actors SET profile_checked_at = unixepoch\(\) WHERE did IN/.test(c.sql) &&
      c.binds.includes(DID));
  }

  function materialUpdateAdvancesReplica(calls: Call[]): boolean {
    return calls.some((c) =>
      c.method === "batch" &&
      c.sql.includes("handle = COALESCE") &&
      c.sql.includes("updated_at = unixepoch()"));
  }

  test("CHANGED profile: cursor advances for the actor", async () => {
    const row = staleRow(488.98);
    const { db, calls } = mockDb(row);
    // bsky returns a NEW avatar CID — forces a content write
    mockGetProfiles({
      did: DID, handle: "germnetwork.com", displayName: "Germ Network",
      avatar: "https://cdn.bsky.app/img/avatar/plain/did/bafNEWcid",
      labels: [], createdAt: "2024-01-01T00:00:00.000Z", associated: {},
      followersCount: 3041, followsCount: 100, postsCount: 500,
    });
    await refreshModeration(db, mockEnv().env);
    expect(cursorAdvances(calls)).toBe(true);
    expect(materialUpdateAdvancesReplica(calls)).toBe(true);
  });

  test("UNCHANGED profile: cursor STILL advances (the starvation bug)", async () => {
    // make the row identical to what bsky returns, so isProfileIdentical → skip.
    const profile = {
      did: DID, handle: "germnetwork.com", displayName: "Germ Network",
      avatar: "https://cdn.bsky.app/img/avatar/plain/did/bafOLDcid",
      labels: [], createdAt: "2024-01-01T00:00:00.000Z", associated: {},
      followersCount: 3041, followsCount: 100, postsCount: 500,
    };
    const f = extractProfileFields(profile);
    const row = staleRow(f.qualityScore); // align score so identical holds
    const { db, calls } = mockDb(row);
    mockGetProfiles(profile);
    await refreshModeration(db, mockEnv().env);
    // even though nothing changed, we must record that we checked it — else the
    // event queue re-selects this same row forever and never drains.
    expect(cursorAdvances(calls)).toBe(true);
    expect(materialUpdateAdvancesReplica(calls)).toBe(false);
  });
});

// THE CRON CONTRACT: selection must be O(batch) and index-served — never a
// full-corpus scan or sort. The previous selector ran two full-table ORDER BY
// scans over ~11M rows every hour; these tests pin the replacement's shape.
describe("refreshModeration quality-walk cursor", () => {
  const origFetch = globalThis.fetch;
  afterEach(() => { globalThis.fetch = origFetch; });

  function scanTrap() {
    // db that returns empty pools and captures the walk SQL
    const sqls: string[] = [];
    const stmt = (sql: string): Stmt => {
      const s: Stmt = {
        bind() { return s; },
        async all() { sqls.push(sql); return { results: [] }; },
        async first() { return null; },
        async run() { return { meta: { changes: 0 } }; },
      };
      return s;
    };
    const db: TursoDB = { prepare: stmt, async batch() { return []; } };
    return { db, sqls };
  }

  test("keyset cursor is inlined as literals, not bound params (Turso plan degradation)", async () => {
    const { db, sqls } = scanTrap();
    const { env } = mockEnvWithCursor(JSON.stringify({ qs: 123.45, rowid: 678 }));
    await refreshModeration(db, env);
    const walk = sqls.find((s) => s.includes("ORDER BY quality_score DESC"))!;
    expect(walk).toContain("quality_score < 123.45");
    expect(walk).toContain("rowid < 678");
    expect(walk).not.toContain("?2"); // keyset values never arrive as params
  });

  test("a poisoned cursor cannot inject SQL — non-numeric values are dropped", async () => {
    const { db, sqls } = scanTrap();
    const { env } = mockEnvWithCursor(JSON.stringify({ qs: "1; DROP TABLE actors", rowid: 678 }));
    await refreshModeration(db, env);
    const walk = sqls.find((s) => s.includes("ORDER BY quality_score DESC"))!;
    expect(walk).not.toContain("DROP");
    expect(walk).not.toContain("quality_score <"); // no keyset at all — walk restarts from top
  });

  test("short walk page wraps the cursor (pool exhausted)", async () => {
    const { db } = scanTrap();
    const kvOps: string[] = [];
    const env = envWithKv({
      async get() { return JSON.stringify({ qs: 0.01, rowid: 1 }); },
      async put(key: string) { kvOps.push(`put:${key}`); },
      async delete(key: string) { kvOps.push(`delete:${key}`); },
    });
    await refreshModeration(db, env);
    expect(kvOps).toContain("delete:mod_walk_cursor");
    expect(kvOps).not.toContain("put:mod_walk_cursor");
  });

  function mockEnvWithCursor(cursor: string) {
    return {
      env: envWithKv({
        async get() { return cursor; },
        async put() {},
        async delete() {},
      }),
    };
  }
});
