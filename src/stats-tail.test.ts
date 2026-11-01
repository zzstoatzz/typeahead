import { expect, test } from "bun:test";
import { tailStartBucket, withTail } from "./stats-tail";

test("the tail starts at the top of the hour the KV copy was written", () => {
  const materialized = Date.UTC(2026, 10, 1, 14, 7, 30);
  expect(tailStartBucket(materialized) * 300_000).toBe(Date.UTC(2026, 10, 1, 14, 0, 0));
  const now = Date.UTC(2026, 10, 1, 14, 38);
  expect(tailStartBucket(undefined, now) * 300_000).toBe(Date.UTC(2026, 10, 1, 13, 0, 0));
});

test("live rows replace materialized rows from the start onward and older rows stay", () => {
  const kv = [{ bucket: 10, hits: 1 }, { bucket: 11, hits: 2 }, { bucket: 12, hits: 3 }];
  const live = [{ bucket: 12, hits: 9 }, { bucket: 13, hits: 4 }];
  expect(withTail(kv, live, "bucket", 12)).toEqual([{ bucket: 10, hits: 1 }, { bucket: 11, hits: 2 }, { bucket: 12, hits: 9 }, { bucket: 13, hits: 4 }]);
});
