// plain-text service description for LLM agents and curl users, served at /llms.txt
export function llmsTxt(): string {
  return `# typeahead

community-run actor search for atproto (https://atproto.com). unauthenticated, CORS-enabled.

## canonical endpoint

GET https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=<prefix>&limit=10

- q: required. search prefix, matched against handles and display names.
- limit: 1-100, default 10.
- pds: optional exact hosting hostname, e.g. acme.com. Filters before ranking and caps;
  custom-domain handles are included, subdomains are separate. Case-insensitive;
  HTTP(S) origin and trailing slash/dot accepted; ports, paths and empty values rejected (400).
  Scopes cover PDSs with at most 10,000 known actors at build time
  (configurable). Unknown PDS returns { "actors": [] }. Larger PDSs, pending replay,
  or missing backend/snapshot support return
  503 PdsScopeUnavailable, never unscoped fallback results. Results reflect indexed PDS identity.
  This is a typeahead extension supported on both endpoint names, not a Bluesky API parameter.
- response: { "actors": [ { did, handle, displayName?, avatar?, associated?, labels?, createdAt? } ] }
  (the shape of app.bsky.actor.defs#profileViewBasic minus the authenticated "viewer" field)
- lexicon: https://tangled.org/zzstoatzz.io/typeahead/blob/main/lexicons/tech.waow.typeahead.searchActors.json

## bluesky-compatible alias

GET https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=<prefix>&limit=10

identical behavior and response. exists so switching from bluesky's appview is a base-URL swap;
kept indefinitely. new integrations should prefer the canonical endpoint.

## etiquette

- rate limit: 60 req/min per IP. responses are edge-cached for 60s.
- set an X-Client header (ideally your domain, e.g. "my-app.example.com") so your app is
  attributed on https://typeahead.waow.tech/stats.

## profiles

GET https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfiles?actors=<did-or-handle>&actors=...   (max 25)
GET https://typeahead.waow.tech/xrpc/app.bsky.actor.getProfile?actor=<did-or-handle>

a profile-card subset of app.bsky.actor.defs#profileViewDetailed: did, handle, displayName,
description, avatar, banner, followersCount, followsCount, postsCount, associated, labels,
createdAt, indexedAt. counts are sourced from the bluesky appview by the enrichment path and
are omitted (not zero) until that path has seen the actor. no viewer state, verification, or
status — those need the caller's graph, which typeahead does not hold. edge-cached 5 minutes.

## other endpoints

- POST https://typeahead.waow.tech/request-indexing?handle=<handle-or-did> — surface an account
  that predates the index. rate limited. Requires authority DID-document and reverse
  handle verification; unverified claims return 422.
- https://typeahead.waow.tech/docs — migration guide (html)
- https://typeahead.waow.tech/stats — index health, latency, traffic (html)

source: https://tangled.org/zzstoatzz.io/typeahead
contact: @zzstoatzz.io on bluesky
`;
}
