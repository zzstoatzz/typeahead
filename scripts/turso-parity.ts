// Compare what Turso answers with what a store slot answers, before a
// feature's mode is moved from compare to slot. Reads only.
//
//   bun scripts/turso-parity.ts profiles [--sample 500] [--max-rate 0.01] [--seed 1] [--slot URL]
//   bun scripts/turso-parity.ts corpus   [--max-rate 0.05] [--slot URL]
//
// `profiles` draws visible actors from Turso, asks the slot's /actors for the
// same DIDs, builds both answers with the Worker's own view code, and prints
// how they differ. `corpus` compares the hourly snapshot's counts. Both exit
// non-zero when the comparison does not pass.
//
// Turso comes from TURSO_URL and TURSO_AUTH_TOKEN. --slot is an origin that
// serves /actors and /corpus: the transport (the default, which answers for
// the routed slot) or one slot's own origin, such as
// https://typeahead-shadow-b.fly.dev, to judge a standby before it is routed.
// docs/turso-retirement.md.

import { createClient } from "@libsql/client";
import { parseArgs } from "node:util";
import { slotSnapshot } from "../src/corpus";
import { rowsAs, tursoDb } from "../src/db";
import { countSnapshot } from "../src/enrichment";
import { profileView, slotProfiles } from "../src/handlers/profiles";
import { isSearchExcludedHandle } from "../src/identity-policy";
import { SLOT_ACTORS_MAX, slotCorpus } from "../src/slot-read";
import { SAMPLE_RUN, SAMPLE_SQL, corpusParity, profileParity, sampleStarts } from "../src/turso-parity";
import type { ProfileRow } from "../src/types";

const TRANSPORT = "https://typeahead-search-transport.n8-3e9.workers.dev";

const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    sample: { type: "string", default: "500" },
    "max-rate": { type: "string" },
    seed: { type: "string", default: String(Date.now() % 0x7fff_ffff) },
    slot: { type: "string", default: TRANSPORT },
  },
});

function turso() {
  const url = process.env.TURSO_URL;
  if (!url) throw new Error("TURSO_URL is not set");
  return createClient({ url, authToken: process.env.TURSO_AUTH_TOKEN });
}

const slot = { SEARCH_BACKEND_URL: values.slot.replace(/\/$/, "") };

async function profiles(): Promise<boolean> {
  const wanted = Number(values.sample);
  const seed = Number(values.seed);
  const client = turso();
  let rows: ProfileRow[];
  try {
    const db = tursoDb(client);
    const top = await db.prepare("SELECT max(rowid) AS top FROM actors").first<{ top: number | null }>();
    const starts = sampleStarts(Number(top?.top ?? 0), Math.ceil(wanted / SAMPLE_RUN), seed);
    const runs = await db.batch(starts.map((start) => db.prepare(SAMPLE_SQL).bind(start, SAMPLE_RUN)), "read");
    const drawn = new Map<string, ProfileRow>();
    for (const run of runs) {
      for (const actor of rowsAs<ProfileRow>(run.results)) {
        if (!isSearchExcludedHandle(actor.handle)) drawn.set(actor.did, { ...actor });
      }
    }
    rows = [...drawn.values()].slice(0, wanted);
  } finally {
    client.close();
  }

  const fromSlot = [];
  for (let at = 0; at < rows.length; at += SLOT_ACTORS_MAX) {
    fromSlot.push(...await slotProfiles(slot, rows.slice(at, at + SLOT_ACTORS_MAX).map((row) => row.did)));
  }
  const report = profileParity(rows.map(profileView), fromSlot, Number(values["max-rate"] ?? "0.01"));
  console.log(JSON.stringify({ check: "profiles", slot_origin: slot.SEARCH_BACKEND_URL, seed, ...report }, null, 2));
  return report.passed;
}

async function corpus(): Promise<boolean> {
  const client = turso();
  let counted;
  try {
    counted = await countSnapshot(tursoDb(client));
  } finally {
    client.close();
  }
  const answer = await slotCorpus(slot);
  const report = corpusParity(counted, slotSnapshot(answer), Number(values["max-rate"] ?? "0.05"));
  console.log(JSON.stringify({ check: "corpus", slot_origin: slot.SEARCH_BACKEND_URL, index: answer.index, store: answer.store, ...report }, null, 2));
  return report.passed;
}

const checks = { profiles, corpus };
const check = Object.entries(checks).find(([name]) => name === positionals[0])?.[1];
if (!check) {
  console.error("usage: bun scripts/turso-parity.ts profiles|corpus [--sample N] [--max-rate R] [--seed N] [--slot URL]");
  process.exit(2);
}
process.exit(await check() ? 0 : 1);
