import { expect, test } from "bun:test";
import { isDomainBlocked, resolveHidden, BSKY_MOD_DID } from "./moderation";

const hideLabel = [{ src: BSKY_MOD_DID, val: "!hide" }];

test("isDomainBlocked: apex + subdomains match, look-alikes do not", () => {
  const blocked = new Set(["dadavidtseng.com"]);
  // apex itself
  expect(isDomainBlocked("dadavidtseng.com", blocked)).toBe(true);
  // subdomains (incl. the bot pattern)
  expect(isDomainBlocked("peachisle4555424.pds.dadavidtseng.com", blocked)).toBe(true);
  expect(isDomainBlocked("x.dadavidtseng.com", blocked)).toBe(true);
  // case-insensitive
  expect(isDomainBlocked("X.DaDavidTseng.com", blocked)).toBe(true);
  // look-alikes must NOT match (no subdomain boundary)
  expect(isDomainBlocked("evil-dadavidtseng.com", blocked)).toBe(false);
  expect(isDomainBlocked("dadavidtseng.com.evil.com", blocked)).toBe(false);
  expect(isDomainBlocked("notdadavidtseng.com", blocked)).toBe(false);
  // empty inputs
  expect(isDomainBlocked("", blocked)).toBe(false);
  expect(isDomainBlocked("a.dadavidtseng.com", new Set())).toBe(false);
});

test("resolveHidden: precedence — show override is the final word", () => {
  // nothing → visible
  expect(resolveHidden({})).toBe(0);
  // label hide
  expect(resolveHidden({ labels: hideLabel })).toBe(1);
  // domain block alone (no label) hides
  expect(resolveHidden({ domainBlocked: true })).toBe(1);
  // explicit hide override
  expect(resolveHidden({ override: "hide" })).toBe(1);
  // per-DID show beats a domain block (the false-positive escape hatch)
  expect(resolveHidden({ domainBlocked: true, override: "show" })).toBe(0);
  // per-DID show beats a label hide too
  expect(resolveHidden({ labels: hideLabel, override: "show" })).toBe(0);
  // domain block + label, no override → hidden
  expect(resolveHidden({ labels: hideLabel, domainBlocked: true })).toBe(1);
});
