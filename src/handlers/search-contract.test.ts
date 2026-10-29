import { describe, expect, test } from "bun:test";
import { f32Bits, parseSearchScope, scopeAcknowledged, scopeParams, searchCacheGeneration, searchCacheUrl } from "./search-contract";

const scope = (query: string) => parseSearchScope(new URLSearchParams(query));

describe("PDS public search contract", () => {
  test("cache generation is a bounded configuration token with stable legacy default", () => {
    expect(searchCacheGeneration()).toBe("legacy");
    for (const value of ["", "../other", "a/b", "a b", "-a", "a?x", "a".repeat(49)]) expect(() => searchCacheGeneration(value)).toThrow();
    expect(searchCacheGeneration("Tantivy-cutover-20261029")).toBe("Tantivy-cutover-20261029");
    expect(searchCacheUrl("preview.test", "a", 10, scope(""), "A")).not.toBe(searchCacheUrl("preview.test", "a", 10, scope(""), "B"));
  });
  test("canonical host aliases and defaults produce one cache identity", () => {
    const a = scope("prefer_pds=HTTP%3A%2F%2FACME.TEST.%2F");
    const b = scope("prefer_pds=acme.test&preference=pds_first");
    expect(a).toEqual(b);
    expect(scopeParams(a).toString()).toBe("prefer_pds=acme.test&preference=pds_first");
    expect(searchCacheUrl("preview.test", "a", 10, a)).toBe(searchCacheUrl("preview.test", "a", 10, b));
  });
  test("hard filtering and soft preference are orthogonal and cannot collide", () => {
    const cases = ["", "pds=acme.test", "prefer_pds=acme.test", "prefer_pds=other.test", "prefer_pds=acme.test&boost=0", "prefer_pds=acme.test&boost=100", "pds=other.test&prefer_pds=acme.test"];
    const keys = cases.map(q => searchCacheUrl("preview.test", "a/b ?", 10, scope(q)));
    expect(new Set(keys).size).toBe(cases.length);
    expect(new Set([keys[0], searchCacheUrl("preview.test", "a/b ?", 25, scope("")), searchCacheUrl("preview.test", "a/b", 10, scope(""))]).size).toBe(3);
  });
  test("finite boost is canonical f32 including zero and equivalent spellings", () => {
    expect(scope("prefer_pds=acme.test&boost=1e2")).toEqual(scope("prefer_pds=acme.test&preference=boost&boost=100.0"));
    expect(scopeParams(scope("prefer_pds=acme.test&boost=0.1")).get("boost")).toBe(String(Math.fround(0.1)));
    expect(f32Bits(100)).toBe("42c80000");
    expect(scope("prefer_pds=acme.test&boost=-0")).toEqual(scope("prefer_pds=acme.test&boost=0"));
  });
  test("unsupported or incomplete semantics fail instead of becoming global search", () => {
    for (const q of ["pds=", "prefer_pds=", "preference=boost", "boost=1", "prefer_pds=acme.test&preference=other", "prefer_pds=acme.test&preference=boost", "prefer_pds=acme.test&preference=pds_first&boost=1", ...["", "NaN", "Infinity", "1e40", "-1", "0x10", " 1"].map(b => `prefer_pds=acme.test&boost=${encodeURIComponent(b)}`)]) expect(() => scope(q)).toThrow();
  });
  test("old backends and wrong scope/boost acknowledgements fail closed", () => {
    const preferred = scope("pds=filter.test&prefer_pds=acme.test&boost=100");
    const headers = new Headers({ "X-Typeahead-Pds": "filter.test", "X-Typeahead-Prefer-Pds": "acme.test", "X-Typeahead-Preference": "boost", "X-Typeahead-Boost": "42c80000" });
    expect(scopeAcknowledged(preferred, headers)).toBe(true);
    for (const key of [...headers.keys()]) {
      const missing = new Headers(headers); missing.delete(key);
      expect(scopeAcknowledged(preferred, missing)).toBe(false);
      const wrong = new Headers(headers); wrong.set(key, "different");
      expect(scopeAcknowledged(preferred, wrong)).toBe(false);
    }
    expect(scopeAcknowledged(scope(""), new Headers())).toBe(true);
    expect(scopeAcknowledged(scope("prefer_pds=acme.test"), new Headers({ "X-Typeahead-Pds": "acme.test" }))).toBe(false);
  });
});
