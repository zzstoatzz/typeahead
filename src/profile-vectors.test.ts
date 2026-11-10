import { expect, test } from "bun:test";
import { buildVectors, capturedProfiles, renderVectors } from "./profile-vectors";

const dir = new URL("../services/src/store_process/testdata/", import.meta.url);
const captured = capturedProfiles(await Bun.file(new URL("appview_profiles.json", dir)).text());

test("the committed profile vectors are what the mapping produces today", async () => {
  const committed = await Bun.file(new URL("profile_vectors.json", dir)).text();
  expect(renderVectors(buildVectors(captured))).toBe(committed);
});

test("the vectors hold every captured profile, a zero-count case and a hidden one", () => {
  const { cases, scores } = buildVectors(captured);
  expect(cases.filter((c) => c.name.startsWith("appview profile")).length).toBe(captured.length);
  expect(cases.some((c) => c.expected.qualityScore === 0 && c.expected.followersCount === 0)).toBe(true);
  expect(cases.some((c) => c.expected.hidden === 1)).toBe(true);
  expect(scores.length).toBe(2000);
});
