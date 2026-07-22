import { describe, test, expect, afterEach } from "bun:test";
import { isProfileIdentical, refreshModeration } from "./cron";
import { extractProfileFields } from "./utils";

// regression: refreshModeration used to skip on `hidden === f.hidden &&
// labels === f.labels` only, freezing avatar / display_name / follower
// counts / quality_score forever for any actor whose moderation state
// was stable — i.e. nearly every actor. Especially bad for third-party-PDS
// accounts whose firehose commits don't reach jetstream (pckt.blog →
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
      followers_count: 3041, follows_count: 100, posts_count: 500,
      quality_score: qualityScore,
    };
  }

  // capturing mock db: routes SELECTs by SQL, records every statement issued.
  function mockDb(row: any) {
    const calls: { sql: string; binds: any[]; method: string }[] = [];
    function stmt(sql: string) {
      const s: any = {
        sql, binds: [] as any[],
        bind(...args: any[]) { s.binds = args; return s; },
        async all() {
          calls.push({ sql, binds: s.binds, method: "all" });
          if (sql.includes("ORDER BY (unixepoch()")) return { results: [row] };      // priority pool
          return { results: [] };                                                    // floor pool, overrides, domains
        },
        async run() { calls.push({ sql, binds: s.binds, method: "run" }); return { meta: { changes: 1 } }; },
        async first() { return null; },
      };
      return s;
    }
    return {
      db: {
        prepare: (sql: string) => stmt(sql),
        async batch(stmts: any[]) {
          for (const st of stmts) calls.push({ sql: st.sql, binds: st.binds ?? [], method: "batch" });
          return stmts.map(() => ({ meta: { changes: 1 } }));
        },
      } as any,
      calls,
    };
  }

  function mockGetProfiles(profile: any) {
    globalThis.fetch = (async () => ({
      status: 200, ok: true, json: async () => ({ profiles: [profile] }),
    })) as any;
  }

  function cursorAdvances(calls: { sql: string; binds: any[]; method: string }[]): boolean {
    return calls.some((c) =>
      /UPDATE actors SET profile_checked_at = unixepoch\(\) WHERE did IN/.test(c.sql) &&
      c.binds.includes(DID));
  }

  function materialUpdateAdvancesReplica(calls: { sql: string; binds: any[]; method: string }[]): boolean {
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
    await refreshModeration(db, {} as any);
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
    await refreshModeration(db, {} as any);
    // even though nothing changed, we must record that we checked it — else the
    // priority pool re-selects this same row forever and never walks onward.
    expect(cursorAdvances(calls)).toBe(true);
    expect(materialUpdateAdvancesReplica(calls)).toBe(false);
  });
});
