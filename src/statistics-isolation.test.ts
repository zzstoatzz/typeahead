import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { URL } from "node:url";

const source = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("serving statistics have no scanner, reporter, queue or filesystem enumeration", () => {
  for (const file of ["role", "http", "index_bridge"]) {
    const text = source(`services/src/store_process/${file}.zig`);
    expect(text).not.toContain('@import("corpus.zig")');
    expect(text).not.toContain("CorpusReporter");
  }
  const owner = source("services/src/store_process/owner.zig");
  expect(owner).not.toContain("countRange");
  expect(owner).not.toContain("countState");
  expect(owner).not.toContain("maintain.disk(");
  const reads = source("services/tantivy/src/engine/read.rs");
  const corpus = reads.slice(reads.indexOf("pub fn corpus("), reads.indexOf("#[cfg(test)]"));
  for (const prohibited of [".search(", "Count", "segment_readers", "initialize_statistics"]) expect(corpus).not.toContain(prohibited);
  expect(source("services/tantivy/src/server.rs")).not.toContain('"/admin/corpus"');
});
