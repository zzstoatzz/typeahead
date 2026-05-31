import { describe, test, expect } from "bun:test";
import { isProfileIdentical } from "./cron";
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
