import { buildVectors, capturedProfiles, renderVectors } from "../src/profile-vectors";

const dir = new URL("../services/src/store_process/testdata/", import.meta.url);
const captured = capturedProfiles(await Bun.file(new URL("appview_profiles.json", dir)).text());
const output = renderVectors(buildVectors(captured));
const path = new URL("profile_vectors.json", dir);
if (process.argv.includes("--check")) {
  if (await Bun.file(path).text() !== output) throw new Error("Profile vectors differ; run bun scripts/profile-vectors.ts");
} else await Bun.write(path, output);
