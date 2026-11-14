import { ROUTE_KEY, parseRoute, type SearchRoute } from "../../src/search-route";

/** Test fixture. Writes the route record from inside workerd the way the
 *  route tool writes it to R2: only if the record still has the etag the
 *  caller read, keeping the replaced target as `previous`. */
export async function conditionalRouteUpdate(bucket: R2Bucket, expectedEtag: string | null, candidate: unknown): Promise<boolean> {
  const current = parseRoute({ version: 1, current: candidate });
  const before = await bucket.get(ROUTE_KEY);
  if ((before?.etag ?? null) !== expectedEtag) return false;
  let previous: SearchRoute | undefined;
  if (before !== null) {
    try { previous = parseRoute(await before.json()); } catch { previous = undefined; }
  }
  const options = expectedEtag === null
    ? { onlyIf: { etagDoesNotMatch: "*" } }
    : { onlyIf: { etagMatches: expectedEtag } };
  const result = await bucket.put(ROUTE_KEY, JSON.stringify({ version: 1, current, previous }), options);
  return result !== null;
}
