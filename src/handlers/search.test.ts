import { describe, test, expect } from "bun:test";
import { TURSO_FALLBACK_MAX_LIMIT, tursoFallbackOnEmpty } from "./search";

// an empty engine answer is re-asked of Turso only for interactive-sized
// requests: the fallback is a full-corpus FTS query, and bulk keyword sweeps
// (limit=100, hundreds per minute) must not be able to trigger it at will
describe("tursoFallbackOnEmpty", () => {
  test("interactive limits keep the second look", () => {
    expect(tursoFallbackOnEmpty(1)).toBe(true);
    expect(tursoFallbackOnEmpty(10)).toBe(true);
    expect(tursoFallbackOnEmpty(TURSO_FALLBACK_MAX_LIMIT)).toBe(true);
  });

  test("bulk limits take the engine's answer as final", () => {
    expect(tursoFallbackOnEmpty(TURSO_FALLBACK_MAX_LIMIT + 1)).toBe(false);
    expect(tursoFallbackOnEmpty(100)).toBe(false);
  });
});
