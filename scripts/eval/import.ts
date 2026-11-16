import { readFile } from "node:fs/promises";
import { reportSchema } from "../../src/eval/model";
import { archiveReport } from "./archive";

const path = process.argv[2];
if (!path) throw new Error("usage: bun scripts/eval/import.ts <report.json>");
const report = reportSchema.parse(JSON.parse(await readFile(path, "utf8")));
await archiveReport(report);
console.log(`retained ${report.run}: ${report.observations.length} observations`);
