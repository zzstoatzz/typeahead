import { expect, test } from "bun:test";
import { admittedHandle, filterIdentityBody, filterIdentityResponse } from "./identity-policy";

test("ingest requires verification evidence and accepts explicit invalidation", () => {
  expect(admittedHandle({ handle: "spoof.test" })).toBe("");
  expect(admittedHandle({ handle: "verified.test", handle_verified: true })).toBe("verified.test");
  expect(admittedHandle({ handle: "donald.trump.com", handle_verified: true })).toBe("");
  expect(admittedHandle({ handle: "handle.invalid" })).toBe("handle.invalid");
});

test("containment filters cached and fresh search/profile bodies without changing healthy bytes", async () => {
  for (const key of ["actors", "profiles"] as const) {
    const healthy = '{ "' + key + '": [{"did":"did:plc:good","handle":"valid.test"}] }';
    expect(filterIdentityBody(healthy, key)).toBe(healthy);
    const body = JSON.stringify({ [key]: [{ did: "did:plc:spam", handle: "DONALD.TRUMP.COM" }, { did: "did:plc:invalid", handle: "handle.invalid" }, { did: "did:plc:good", handle: "valid.test" }] });
    const response = await filterIdentityResponse(new Response(body, { headers: { "Server-Timing": "cache;desc=hit", "Content-Length": String(body.length), "ETag": "old" } }), key);
    expect(await response.text()).toBe(JSON.stringify({ [key]: [{ did: "did:plc:good", handle: "valid.test" }] }));
    expect(response.headers.get("Server-Timing")).toBe("cache;desc=hit");
    expect(response.headers.has("Content-Length")).toBe(false);
    expect(response.headers.has("ETag")).toBe(false);
  }
});
