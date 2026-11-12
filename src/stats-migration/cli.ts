import { parseArgs } from "node:util";
import { BACKFILL_STEPS, runBackfill, type BackfillPosition, type BackfillRequest, type BackfillResult } from "./backfill";
import { PARITY_SECTIONS, runParity, type ParityReport, type ParityRequest, type ParityResult } from "./parity";
import { DUAL_WRITE_START_BUCKET, RANGED_NAMES, bucketTime, differences, type RowDiff } from "./rows";

/** The operator's side of the statistics move to D1: `scripts/stats-d1.ts`
 *  calls this. It sends positions to the Worker's two admin endpoints and
 *  prints what comes back; the rows themselves stay in Cloudflare. */
export interface CliHost {
  fetch(url: string, init: { method: "POST"; headers: Record<string, string>; body: string }): Promise<Response>;
  print(line: string): void;
  secret: string | undefined;
  now(): number;
}

const USAGE = `usage:
  stats-d1.ts backfill [--apply] [--cut BUCKET] [--from STEP:CURSOR] [--limit ROWS] [--url URL]
      copy the statistics history from Turso into D1. Without --apply nothing
      is written and the differences are reported. --cut is the 5-minute
      bucket D1 keeps its own rows from (default: two buckets ago); pass the
      same one, with --from, to pick up a run that stopped.
  stats-d1.ts parity [--since-dual-write] [--limit ROWS] [--url URL]
      compare Turso and D1. Exits 1 when closed data differs. Before a
      backfill, --since-dual-write compares only the buckets both stores
      counted themselves.
  the admin secret is read from TYPEAHEAD_SECRET.`;

const DEFAULT_URL = "https://typeahead.waow.tech";

async function post<T>(host: CliHost, url: string, body: BackfillRequest | ParityRequest): Promise<T> {
  const response = await host.fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${host.secret ?? ""}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${url} answered ${response.status}: ${(await response.text()).slice(0, 300)}`);
  // the Worker this repository deploys answers with the result type of the step it ran
  return response.json<T>();
}

const counts = (diff: RowDiff) =>
  `turso ${diff.turso_rows} d1 ${diff.d1_rows} same ${diff.same} differing ${diff.differing} only-turso ${diff.only_turso} only-d1 ${diff.only_d1}`;

function examples(print: (line: string) => void, diff: RowDiff, most = 5): void {
  for (const example of diff.examples.slice(0, most)) {
    print(`      turso ${JSON.stringify(example.turso)}  d1 ${JSON.stringify(example.d1)}`);
  }
}

function backfillLine(result: BackfillResult): string {
  const { report } = result;
  const verb = result.dry_run ? "would change" : result.wrote ? "changed" : "left";
  switch (report.kind) {
    case "rows": return `${report.table} [${report.from}, ${report.to}): ${verb} ${result.changes} (${counts(report.diff)})`;
    case "daily": return `${report.table}: ${verb} ${result.changes} (${counts(report.diff)}; d1 buckets turso has rolled up: ${report.rolled_up_in_turso})`;
    case "totals": return `totals: ${verb} ${result.changes} (traffic_sources ${counts(report.traffic_sources)}; actors_total turso ${report.actors_total.turso} `
      + `minus ${report.actors_total.turso_since_cut} since the cut plus d1's ${report.actors_total.d1_since_cut} = ${report.actors_total.target}, d1 holds ${report.actors_total.d1})`;
  }
}

async function backfill(host: CliHost, args: string[]): Promise<number> {
  const { values } = parseArgs({
    args,
    options: { apply: { type: "boolean", default: false }, cut: { type: "string" }, from: { type: "string" }, limit: { type: "string", default: "1000" }, url: { type: "string", default: DEFAULT_URL } },
  });
  const cut = values.cut === undefined ? Math.floor(host.now() / 300_000) - 2 : Number(values.cut);
  const [fromStep, fromCursor] = (values.from ?? `${BACKFILL_STEPS[0]}:0`).split(":");
  const step = BACKFILL_STEPS.find((name) => name === fromStep);
  if (step === undefined || !Number.isInteger(cut) || !Number.isInteger(Number(fromCursor ?? "0")) || !Number.isInteger(Number(values.limit))) {
    host.print(USAGE);
    return 2;
  }
  const dry_run = !values.apply;
  host.print(`${dry_run ? "dry run" : "backfill"}: cut ${cut} (${bucketTime(cut)}), dual write began at ${DUAL_WRITE_START_BUCKET} (${bucketTime(DUAL_WRITE_START_BUCKET)})`);
  let position: BackfillPosition = { step, cursor: Number(fromCursor ?? "0") };
  try {
    const run = await runBackfill(
      (request) => post<BackfillResult>(host, `${values.url}/admin/stats-backfill`, request),
      { cut, dry_run, limit: Number(values.limit), start: position },
      (result) => {
        host.print(`  ${backfillLine(result)}`);
        if (result.next) position = result.next;
      },
    );
    host.print(`${dry_run ? "dry run complete: nothing written" : "backfill complete"}: ${run.calls} calls, ${run.changes} rows ${dry_run ? "differ" : "differed"}`);
    for (const name of BACKFILL_STEPS) host.print(`  ${name}: ${run.steps[name].changes} in ${run.steps[name].calls} calls`);
    return 0;
  } catch (error) {
    host.print(`stopped: ${error instanceof Error ? error.message : String(error)}`);
    host.print(`resume with: backfill ${dry_run ? "" : "--apply "}--cut ${cut} --from ${position.step}:${position.cursor}`);
    return 1;
  }
}

const plural = (n: number) => `${n} difference${n === 1 ? "" : "s"}`;

function parityLines(print: (line: string) => void, report: ParityReport): void {
  const table = (name: string, diff: RowDiff) => {
    if (differences(diff) === 0) return;
    print(`    ${name}: ${counts(diff)}`);
    examples(print, diff);
  };
  switch (report.kind) {
    case "rows": return table(`[${report.from}, ${report.to})`, report.diff);
    case "daily":
      table("traffic per day", report.tables.traffic_daily);
      return table("requests per day", report.tables.request_daily);
    case "totals":
      table("traffic_sources at the closed bound", report.traffic_sources);
      if (report.actors_total.turso !== report.actors_total.d1) print(`    actors_total at the closed bound: turso ${report.actors_total.turso} d1 ${report.actors_total.d1}`);
      return;
    case "payload":
      for (const [name, diff] of Object.entries(report.series)) {
        table(`${name} (closed)`, diff.closed);
        table(`${name} (still open)`, diff.open);
      }
      for (const [name, value] of Object.entries(report.scalars)) {
        if (value.turso !== value.d1) print(`    ${name}: turso ${value.turso} d1 ${value.d1}`);
      }
      return;
    case "profile":
      if (report.differing_bands.length > 0) print(`    bands: ${report.differing_bands.join(" ")}`);
  }
}

async function parity(host: CliHost, args: string[]): Promise<number> {
  const { values } = parseArgs({
    args,
    options: { "since-dual-write": { type: "boolean", default: false }, limit: { type: "string", default: "2000" }, url: { type: "string", default: DEFAULT_URL } },
  });
  if (!Number.isInteger(Number(values.limit))) {
    host.print(USAGE);
    return 2;
  }
  const sinceDualWrite = values["since-dual-write"];
  const pending: ParityResult[] = [];
  const run = await runParity(
    (request) => post<ParityResult>(host, `${values.url}/admin/stats-parity`, request),
    { from: sinceDualWrite ? DUAL_WRITE_START_BUCKET : 0, limit: Number(values.limit), sections: sinceDualWrite ? RANGED_NAMES : PARITY_SECTIONS },
    (result) => {
      pending.push(result);
      if (result.more !== null) return;
      const closed = pending.reduce((sum, page) => sum + page.differences, 0);
      const open = pending.reduce((sum, page) => sum + page.open_differences, 0);
      host.print(`  ${result.section}: ${closed === 0 ? "equal" : plural(closed)}${open > 0 ? `, ${open} in rows still open` : ""} (${pending.length} calls)`);
      for (const page of pending) parityLines(host.print, page.report);
      pending.length = 0;
    },
  );
  const bound = run.closed_before === null ? "" : ` before bucket ${run.closed_before} (${bucketTime(run.closed_before)})`;
  host.print(run.differences === 0
    ? `parity: the stores agree on all closed data${bound}${sinceDualWrite ? ", time-keyed tables since dual write began only" : ""}`
    : `parity: ${plural(run.differences)} in closed data${bound}`);
  if (run.open_differences > 0) {
    host.print(`  ${run.open_differences} more in KV rows that include a bucket still being written${run.differences === 0 ? "; a later run compares them closed" : ""}`);
  }
  return run.differences === 0 ? 0 : 1;
}

export async function main(argv: string[], host: CliHost): Promise<number> {
  const [command, ...args] = argv;
  if ((command !== "backfill" && command !== "parity") || args.includes("--help")) {
    host.print(USAGE);
    return 2;
  }
  if (!host.secret) {
    host.print("TYPEAHEAD_SECRET is not set");
    return 2;
  }
  return command === "backfill" ? backfill(host, args) : parity(host, args);
}
