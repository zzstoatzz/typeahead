import { expect, test } from "bun:test";
import { computeAuthorityScore, computeAuthorityScoreBreakdown } from "./utils";

const NOW = Date.parse("2026-06-25T00:00:00.000Z");

test("computeAuthorityScoreBreakdown matches the legacy total", () => {
  const input = {
    followersCount: 308_569,
    followsCount: 629,
    postsCount: 37_792,
    createdAt: "2023-05-01T00:00:00.000Z",
  };
  const b = computeAuthorityScoreBreakdown(input, NOW);
  expect(b.score).toBe(computeAuthorityScore(input));
  expect(b.followersInfluence).toBeGreaterThan(500);
  expect(b.postsInfluence).toBeGreaterThan(80);
  expect(b.followerFollowRatioBonus).toBe(150);
  expect(b.matureAccountBonus).toBe(60);
});

test("computeAuthorityScoreBreakdown penalizes very fresh accounts", () => {
  const b = computeAuthorityScoreBreakdown({
    followersCount: 1000,
    followsCount: 0,
    postsCount: 100,
    createdAt: "2026-06-24T12:00:00.000Z",
  }, NOW);
  expect(b.freshness.bucket).toBe("<24h");
  expect(b.freshness.penalty).toBe(-100);
  expect(b.score).toBe(240.13);
});

test("computeAuthorityScoreBreakdown floors fresh-account penalties at zero", () => {
  const b = computeAuthorityScoreBreakdown({
    followersCount: 0,
    followsCount: 0,
    postsCount: 0,
    createdAt: "2026-06-24T12:00:00.000Z",
  }, NOW);
  expect(b.score).toBe(0);
  expect(b.freshness.penalty).toBe(-100);
});
