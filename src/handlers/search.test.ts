import { test, expect } from "bun:test";
import { normalizePds } from "./search";

test("PDS scope is an exact normalized hostname, never a handle suffix or URL path", () => {
  for (const input of ["acme.test", "ACME.TEST", "https://Acme.test/", "HTTP://ACME.TEST./"]) {
    expect(normalizePds(input)).toBe("acme.test");
  }
  for (const input of ["", "acme", "*.acme.test", "acme.test/path", "acme.test?x", "user@acme.test", "acme.test:443", "a..test", "-a.test", "a-.test", "acme.test\n", "acme.test\u001fh:b", "é.test"]) {
    expect(normalizePds(input)).toBeNull();
  }
});
