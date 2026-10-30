import { z } from "zod";

const handleSchema = z.string().max(253).regex(/^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$/i);
export const identitySchema = z.object({ did: z.string().regex(/^did:(plc|web):/), handle: handleSchema, pds: z.string() });
const documentSchema = z.object({ id: z.string(), alsoKnownAs: z.array(z.string()).optional() });
const dnsSchema = z.object({ Status: z.number(), Answer: z.array(z.object({ type: z.number(), data: z.string() })).optional() });

export async function verifyIdentityHandle(identity: z.infer<typeof identitySchema>): Promise<boolean> {
  try {
    const handle = handleSchema.parse(identity.handle).toLowerCase();
    let documentUrl: string;
    if (identity.did.startsWith("did:plc:")) {
      documentUrl = `https://plc.directory/${encodeURIComponent(identity.did)}`;
    } else {
      const [host, ...segments] = identity.did.slice("did:web:".length).split(":").map(decodeURIComponent);
      if (!host) return false;
      const url = new URL(`https://${host}`);
      if (url.username || url.password || !url.hostname.includes(".")) return false;
      url.pathname = segments.length ? `/${segments.map(encodeURIComponent).join("/")}/did.json` : "/.well-known/did.json";
      documentUrl = url.href;
    }
    const documentResponse = await fetch(documentUrl, { signal: AbortSignal.timeout(4000) });
    if (!documentResponse.ok) return false;
    const document = documentSchema.parse(await documentResponse.json());
    if (document.id !== identity.did || !document.alsoKnownAs?.some((name) => name.toLowerCase() === `at://${handle}`)) return false;
    try {
      const dnsResponse = await fetch(`https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(`_atproto.${handle}`)}&type=TXT`, {
        headers: { Accept: "application/dns-json" }, signal: AbortSignal.timeout(4000),
      });
      if (dnsResponse.ok) {
        const dns = dnsSchema.parse(await dnsResponse.json());
        const dids = [...new Set((dns.Answer ?? []).filter((answer) => answer.type === 16)
          .map((answer) => answer.data.replaceAll('"', "")).filter((value) => value.startsWith("did=")).map((value) => value.slice(4)))];
        if (dids.length) return dns.Status === 0 && dids.length === 1 && dids[0] === identity.did;
      }
    } catch { /* HTTPS is the protocol's alternate resolution method. */ }
    const response = await fetch(`https://${handle}/.well-known/atproto-did`, { signal: AbortSignal.timeout(4000) });
    return response.ok && (await response.text()).trim() === identity.did;
  } catch {
    return false;
  }
}
