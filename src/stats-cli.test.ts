import { afterEach, beforeEach, expect, setSystemTime, test } from "bun:test";
import { handleStatsBackfill, handleStatsParity } from "./handlers/stats-admin";
import { CUT, NOW, backfilled, seed } from "./stats-backfill-fixture";
import { main, type CliHost } from "./stats-cli";
import { statsRows, statsStores } from "./stats-d1-fixture";
import type { Env } from "./types";

// The operator's command against the two admin endpoints, over the same
// requests and responses it exchanges with the deployed Worker.

let stores: Awaited<ReturnType<typeof statsStores>>;
let lines: string[];
let requests: number;
let failAfter: number;

beforeEach(async () => {
  stores = await statsStores();
  lines = [];
  requests = 0;
  failAfter = Infinity;
  setSystemTime(new Date(NOW));
});

afterEach(async () => {
  setSystemTime();
  await stores.dispose();
});

function host(secret: string | undefined = "s3cret"): CliHost {
  const fields: Partial<Env> = { ADMIN_SECRET: "s3cret", STATS_DB: stores.d1, RATE_LIMITER_STRICT: { limit: async () => ({ success: true }) } };
  // SAFETY: the two handlers read ADMIN_SECRET, STATS_DB and, on a refused
  // request, the strict rate limiter.
  const env = fields as Env;
  return {
    secret,
    now: () => NOW,
    print: (line) => { lines.push(line); },
    async fetch(url, init) {
      if (++requests > failAfter) throw new Error("network down");
      const request = new Request(url, init);
      return new URL(url).pathname === "/admin/stats-backfill"
        ? handleStatsBackfill(request, stores.turso, env)
        : handleStatsParity(request, stores.turso, env);
    },
  };
}

const d1Rows = async () => Object.fromEntries((await statsRows(stores.mirror)).map((entry) => [entry.table, entry.rows]));
const cut = ["--cut", String(CUT), "--limit", "80"];

test("backfill is a dry run unless --apply is given, and parity gates on the result", async () => {
  const seeded = await seed(stores.turso, stores.mirror, { agree: true });
  const before = await d1Rows();

  expect(await main(["parity", "--since-dual-write"], host())).toBe(0);
  expect(lines.at(-1)).toContain("time-keyed tables since dual write began only");
  expect(await main(["parity"], host())).toBe(1);

  lines.length = 0;
  expect(await main(["backfill", ...cut], host())).toBe(0);
  expect(lines[0]).toStartWith(`dry run: cut ${CUT} (`);
  expect(lines.join("\n")).toContain("dry run complete: nothing written");
  expect(await d1Rows()).toEqual(before);

  expect(await main(["backfill", "--apply", ...cut], host())).toBe(0);
  expect(await d1Rows()).toEqual(backfilled(seeded));

  lines.length = 0;
  expect(await main(["parity"], host())).toBe(0);
  expect(lines.filter((line) => line.endsWith("calls)")).every((line) => line.includes(": equal"))).toBe(true);
  expect(lines.at(-1)).toStartWith("parity: the stores agree on all closed data before bucket");

  await stores.mirror.prepare("UPDATE request_buckets SET hits = hits + 1 WHERE bucket = ?1").bind(CUT - 1).run();
  lines.length = 0;
  expect(await main(["parity"], host())).toBe(1);
  expect(lines.join("\n")).toContain(`"bucket":${CUT - 1}`);
  expect(lines.some((line) => /^parity: \d+ differences? in closed data before bucket/.test(line))).toBe(true);
  expect(lines.some((line) => /^  request_buckets: \d+ differences? \(/.test(line))).toBe(true);
});

test("a run that loses its connection says how to resume, and the resumed run completes it", async () => {
  const seeded = await seed(stores.turso, stores.mirror, { agree: false });
  failAfter = 9;
  expect(await main(["backfill", "--apply", ...cut], host())).toBe(1);
  expect(lines.at(-2)).toBe("stopped: network down");
  const resume = lines.at(-1)!;
  expect(resume).toMatch(new RegExp(`^resume with: backfill --apply --cut ${CUT} --from [a-z_]+:\\d+$`));
  expect(await d1Rows()).not.toEqual(backfilled(seeded));

  failAfter = Infinity;
  expect(await main([...resume.replace("resume with: ", "").split(" "), "--limit", "80"], host())).toBe(0);
  expect(await d1Rows()).toEqual(backfilled(seeded));
});

test("without the secret, or with the wrong one, nothing is sent or changed", async () => {
  expect(await main(["backfill", "--apply"], host(""))).toBe(2);
  expect(requests).toBe(0);
  expect(await main(["backfill", "--apply", ...cut], host("wrong"))).toBe(1);
  expect(lines.join("\n")).toContain("answered 401");
  expect(await main(["nonsense"], host())).toBe(2);
});
