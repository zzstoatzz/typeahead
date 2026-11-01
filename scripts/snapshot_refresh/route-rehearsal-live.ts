import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { liveConfiguration, liveOriginAdapter } from "./rehearsal-live-origin";
import { runLiveRehearsal } from "./route-rehearsal";

const [input, evidencePath] = Bun.argv.slice(2);
if (!input || !evidencePath || Bun.file(input).size > 65536 || await Bun.file(evidencePath).exists()) {
  console.error("usage: bun scripts/snapshot_refresh/route-rehearsal-live.ts <config.json (≤64 KiB)> <new-evidence.json>");
  process.exit(2);
}
const raw = await readFile(input);
const configuration = liveConfiguration(JSON.parse(raw.toString()));
await runLiveRehearsal(configuration, liveOriginAdapter(configuration), evidencePath, createHash("sha256").update(raw).digest("hex"));
console.log(`passed; evidence written to ${evidencePath}`);
