import { createHash } from "node:crypto";
import { BSKY_MOD_DID, MOD_HIDE_VALS, explicitSlurRegexes } from "../src/moderation";

const source = await Bun.file(new URL("../src/moderation.ts", import.meta.url)).text();
const artifact = {
  schema_version: 1,
  canonical_source_sha256: createHash("sha256").update(source).digest("hex"),
  explicit_slur_regexes: explicitSlurRegexes.map(regex => regex.source),
  hide_values: [...MOD_HIDE_VALS],
  labeler_did: BSKY_MOD_DID,
};
const output = JSON.stringify(artifact, null, 2) + "\n";
const path = new URL("../src/moderation-regexes.json", import.meta.url);
if (process.argv.includes("--check")) {
  if (await Bun.file(path).text() !== output) throw new Error("Moderation artifact differs; run bun scripts/export-moderation-regexes.ts");
} else await Bun.write(path, output);
