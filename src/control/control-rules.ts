/** What a moderation rule's subject may be. These are the checks every store
 *  slot applies to the policy snapshot (`validDid` and `validDomain` in
 *  `services/src/store_process/control.zig`), and a slot refuses a whole
 *  snapshot over one rule that fails them. So the Worker applies the same
 *  checks before a rule is written, and `src/control-rule-vectors.json` holds
 *  values both sides must judge alike: `bun test` and `zig build test` each
 *  read it. */

export const MAX_DID_BYTES = 2048;
export const MAX_DOMAIN_BYTES = 253;

const encoder = new TextEncoder();

/** Printable ASCII with no space: bytes 0x21 to 0x7e. */
function visibleAscii(bytes: Uint8Array): boolean {
  return bytes.every((byte) => byte >= 0x21 && byte <= 0x7e);
}

export function validOverrideDid(did: string): boolean {
  const bytes = encoder.encode(did);
  return did.startsWith("did:") && bytes.length <= MAX_DID_BYTES && visibleAscii(bytes);
}

/** `domain` as it would be stored: the caller has already normalized it. */
export function validBlockedDomain(domain: string): boolean {
  const bytes = encoder.encode(domain);
  return bytes.length >= 1 && bytes.length <= MAX_DOMAIN_BYTES && domain.includes(".") && visibleAscii(bytes);
}
