import { expect, test } from "bun:test";
import { isDidTerm } from "./search";

// mirrors the isDid test in services/src/index/index_search.zig — the worker
// gate and the search service must classify DID terms identically.
test("isDidTerm accepts plc/web DIDs", () => {
  expect(isDidTerm("did:plc:xbtmt2zjwlrfegqvch7fboei")).toBe(true);
  expect(isDidTerm("did:web:zzstoatzz.io")).toBe(true);
});

test("isDidTerm rejects handles, phrases, and unsupported methods", () => {
  expect(isDidTerm("zzstoatzz.io")).toBe(false);
  expect(isDidTerm("did:key:zQ3sh")).toBe(false);
  expect(isDidTerm("did:plc:abc def")).toBe(false);
  expect(isDidTerm("did:plc:")).toBe(false);
  expect(isDidTerm("")).toBe(false);
});
