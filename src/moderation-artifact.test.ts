import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import artifact from "./moderation-regexes.json";
import { BSKY_MOD_DID, MOD_HIDE_VALS, explicitSlurRegexes } from "./moderation";

test("the embedded engine policy matches the current canonical Worker rules", async () => {
  const source = await Bun.file(new URL("./moderation.ts", import.meta.url)).text();
  expect(artifact.canonical_source_sha256).toBe(createHash("sha256").update(source).digest("hex"));
  expect(artifact.explicit_slur_regexes).toEqual(explicitSlurRegexes.map(regex => regex.source));
  expect(artifact.hide_values).toEqual([...MOD_HIDE_VALS]);
  expect(artifact.labeler_did).toBe(BSKY_MOD_DID);
});
