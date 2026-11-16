import { collect } from "./collect";
import { publishLocal } from "./publish";
import { upload } from "./upload";

if (process.argv.slice(2).some(arg => arg !== "--remote")) throw new Error("usage: schedule.ts [--remote]");
await collect({ scheduled: true });
await publishLocal();
if (process.argv.includes("--remote")) await upload(true);
