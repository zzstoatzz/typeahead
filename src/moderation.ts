export const BSKY_MOD_DID = "did:plc:ar7c4by46qjdydhdevvrndac";
/** labels from bluesky's moderation service that hide an actor from search */
export const MOD_HIDE_VALS = new Set(["!hide", "!takedown", "spam"]);

/**
 * returns whether an actor should be hidden from search.
 *
 * only hides actors flagged by bluesky's moderation service (!hide, !takedown, spam).
 * !no-unauthenticated is intentionally NOT filtered — it applies to content, not identity.
 * bluesky's own public typeahead API returns !no-unauthenticated accounts, and so do we.
 */
export function shouldHide(labels?: any[]): boolean {
  if (!labels) return false;
  const now = Date.now();
  return labels.some((l: any) => {
    if (l.neg) return false;
    if (l.exp && new Date(l.exp).getTime() <= now) return false;
    return l.src === BSKY_MOD_DID && MOD_HIDE_VALS.has(l.val);
  });
}
