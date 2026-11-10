import { Database } from "bun:sqlite";
import { expect, test } from "bun:test";
import { SNAPSHOT_HISTORY_HOURS, SNAPSHOT_HISTORY_SQL } from "./enrichment";

test("snapshot history keeps the newest hours once the table outgrows the limit", () => {
  // Regression (2026-11-10): the query took the OLDEST 5000 rows. With 4,809
  // hourly snapshots stored, new ones would have stopped reaching /stats
  // about eight days later, freezing the actors-indexed chart.
  const db = new Database(":memory:");
  db.run("CREATE TABLE snapshots (hour INTEGER PRIMARY KEY, total INTEGER, with_handles INTEGER, with_avatars INTEGER, hidden INTEGER)");
  const insert = db.prepare("INSERT INTO snapshots VALUES (?, ?, 0, 0, 0)");
  const stored = SNAPSHOT_HISTORY_HOURS + 25;
  db.transaction(() => {
    for (let hour = 1; hour <= stored; hour++) insert.run(hour, hour * 10);
  })();

  const rows = db.query<{ hour: number; total: number }, []>(SNAPSHOT_HISTORY_SQL).all();

  expect(rows.length).toBe(SNAPSHOT_HISTORY_HOURS);
  expect(rows.at(-1)).toMatchObject({ hour: stored, total: stored * 10 });
  expect(rows[0]!.hour).toBe(26);
  expect(rows.every((row, i) => i === 0 || row.hour > rows[i - 1]!.hour)).toBe(true);
});
