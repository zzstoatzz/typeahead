import { mean, percentile } from "../../src/eval/model";
import { z } from "zod";

const sampleSchema = z.object({
  phase: z.string(), case: z.number().int(), at_ms: z.number(), http: z.number(),
  elapsed_s: z.number(), engine_ms: z.number().nullable(),
  response: z.object({
    q: z.string(), build_id: z.string(),
    candidates: z.array(z.object({ did: z.string(), handle: z.string() })),
    hydration_ms: z.number(), moderation_ms: z.number(),
    visited_candidates: z.number(), segments: z.number(),
  }).nullable(),
});
type Sample = z.infer<typeof sampleSchema>;
const dir = process.argv[2] ?? ".eval/scan-experiment/";
const output = process.argv[3] ?? "docs/investigations/2026-11-16-standby-scan-results.json";
if ((await Bun.file(dir + "exit-code").text()).trim() !== "0") throw new Error("trial did not finish");
const text = await Bun.file(dir + "samples.jsonl").text();
const rows = text.trim().split("\n").map(line => sampleSchema.parse(JSON.parse(line)));
const phaseSchema = z.object({ event: z.enum(["phase_start", "phase_end", "scan_end"]), phase: z.string(), at_ms: z.number() });
const phases = (await Bun.file(dir + "phases.jsonl").text()).trim().split("\n").map(line => phaseSchema.parse(JSON.parse(line)));
const queries = z.array(z.object({ id: z.number().int(), q: z.string() })).parse(await Bun.file(dir + "queries.json").json());
const labels = ["unpaced-first", "recovery-first", "paced-20ms", "recovery-second", "unpaced-last", "quiet-after"];
const keys = queries.filter(k => labels.every(p => rows.some(x => x.phase === p && x.case === k.id))).map(k => k.id);
if (!keys.length) throw new Error("no matched cases");
const hash = (dids: string[]) => new Bun.CryptoHasher("sha256").update(JSON.stringify(dids)).digest("hex");
const caseRows = (key: number) => rows.filter(x => x.case === key && labels.includes(x.phase));
const stable = keys.filter(k => caseRows(k).every(x => x.http === 200 && x.response !== null)
  && new Set(caseRows(k).map(x => x.response ? hash(x.response.candidates.map(a => a.did)) : "failed")).size === 1);
function summary(samples: Sample[]) {
  const good = samples.flatMap(x => x.http === 200 && x.engine_ms !== null && x.response
    ? [{ ...x, engine_ms: x.engine_ms, response: x.response }] : []);
  const values = good.map(x => x.engine_ms);
  return { requests: samples.length, failures: samples.length - good.length,
    p50Ms: percentile(values, .5), p90Ms: percentile(values, .9),
    maxMs: values.length ? Math.max(...values) : null, meanMs: mean(values),
    hydrationP90Ms: percentile(good.map(x => x.response.hydration_ms), .9) };
}
function balanced(phase: string, cases: number[]) {
  const values = cases.flatMap(key => {
    const samples = rows.flatMap(x => x.phase === phase && x.case === key && x.http === 200 && x.engine_ms !== null ? [x.engine_ms] : []);
    const value = mean(samples);
    return value === null ? [] : [value];
  });
  return { queries: cases.length, successfulQueries: values.length,
    p50Ms: percentile(values, .5), p90Ms: percentile(values, .9), maxMs: values.length ? Math.max(...values) : null };
}
const scanSchema = z.object({ event: z.literal("scan_complete"), page_rest_ms: z.number(), elapsed_ms: z.number(),
  records: z.number(), live: z.number(), with_handles: z.number(), with_avatars: z.number(),
  hidden: z.number(), ranked: z.number(), rest: z.number() });
const scanResults = (await Bun.file(dir + "scan-results.jsonl").text()).trim().split("\n").map(line => scanSchema.parse(JSON.parse(line)));
const evidence = {
  sourceCommit: "21451c2", probeSha256: "dc19e93c3b4ba5df4571dbea4e3f8a6c3449c965928aaa2211919aab31f2375b",
  machine: "781e7e3ad11068", servingOrigin: "https://typeahead-slot-a.fly.dev", queries, phases, scanResults,
  rawHash: new Bun.CryptoHasher("sha256").update(text).digest("hex"),
  summary: Object.fromEntries([...new Set(rows.map(x => x.phase))].map(p => [p, summary(rows.filter(x => x.phase === p))])),
  commonCases: keys.length, stableCases: stable.length,
  balanced: Object.fromEntries(labels.map(p => [p, balanced(p, keys)])),
  balancedStable: Object.fromEntries(labels.map(p => [p, balanced(p, stable)])),
  changedCases: queries.filter(q => keys.includes(q.id) && !stable.includes(q.id)).map(q => ({ ...q,
    variants: caseRows(q.id).map(x => ({ phase: x.phase, dids: x.response?.candidates.map(a => a.did) })) })),
  samples: rows.map(x => ({ phase: x.phase, case: x.case, at_ms: x.at_ms, http: x.http,
    elapsedMs: x.elapsed_s * 1000, engineMs: x.engine_ms, hydrationMs: x.response?.hydration_ms,
    moderationMs: x.response?.moderation_ms, visited: x.response?.visited_candidates, segments: x.response?.segments,
    didsHash: x.response ? hash(x.response.candidates.map(a => a.did)) : null })),
};
await Bun.write(output, JSON.stringify(evidence, null, 2) + "\n");
console.log(JSON.stringify({ summary: evidence.summary, commonCases: keys.length,
  stableCases: stable.length, balanced: evidence.balanced, changed: evidence.changedCases.map(x => x.q) }, null, 2));
