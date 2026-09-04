import { describe, test, expect } from "bun:test";
import { TURSO_FALLBACK_MAX_LIMIT, tursoFallbackOnEmpty } from "./search";

// an empty engine answer is re-asked of Turso only for interactive-sized
// requests: the fallback is a full-corpus FTS query, and bulk keyword sweeps
// (limit=100, hundreds per minute) must not be able to trigger it at will
describe("tursoFallbackOnEmpty", () => {
  test("off by default: the engine's empty answer is final at every limit", () => {
    expect(tursoFallbackOnEmpty(1, false)).toBe(false);
    expect(tursoFallbackOnEmpty(10, false)).toBe(false);
    expect(tursoFallbackOnEmpty(100, false)).toBe(false);
  });

  test("when enabled, interactive limits keep the second look", () => {
    expect(tursoFallbackOnEmpty(1, true)).toBe(true);
    expect(tursoFallbackOnEmpty(10, true)).toBe(true);
    expect(tursoFallbackOnEmpty(TURSO_FALLBACK_MAX_LIMIT, true)).toBe(true);
  });

  test("when enabled, bulk limits still take the engine's answer as final", () => {
    expect(tursoFallbackOnEmpty(TURSO_FALLBACK_MAX_LIMIT + 1, true)).toBe(false);
    expect(tursoFallbackOnEmpty(100, true)).toBe(false);
  });
});
