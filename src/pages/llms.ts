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
  Tantivy supports every recorded canonical PDS hostname, without a per-host actor-count cutoff.
  Unknown PDS returns { "actors": [] }; accounts with unknown hosting metadata cannot match.
  Unavailable scope support returns 503 PdsScopeUnavailable, never unscoped fallback results.
  Results reflect indexed PDS identity.
  This is a typeahead extension supported on both endpoint names, not a Bluesky API parameter.
- response: { "actors": [ { did, handle, displayName?, avatar?, associated?, labels?, createdAt? } ] }
  (the shape of app.bsky.actor.defs#profileViewBasic minus the authenticated "viewer" field)
- Unscoped search automatically falls back to Bluesky on backend failure. Headers: X-Typeahead-Engine: bluesky, X-Typeahead-Failover: automatic (manual during forced failover). Fallback responses are not cached. Both services unavailable returns 503 SearchUnavailable. PDS-scoped requests never fall back.
- lexicon: https://tangled.org/zzstoatzz.io/typeahead/blob/main/lexicons/tech.waow.typeahead.searchActors.json

## PDS preference

Production supports prefer_pds=<hostname>, independently of the hard pds filter.
With no other preference parameters, same-PDS actors rank first within each exact/ordinary
category. preference=pds_first makes this explicit. For a finite nonnegative score boost,
use preference=boost&boost=<decimal>; boost alone also selects boost mode. Exact eligible
handles retain priority. A boost is rounded to f32; supplying pds_first with boost is invalid.
Preference/boost parameters without prefer_pds, unknown modes, malformed limits and invalid boosts return 400.
Support is acknowledged by the backend and included in edge cache identity. Unsupported
preference returns 503 PdsScopeUnavailable, never silently unpreferred results.
Unknown source PDS cannot match either a hard filter or a preference. Retained native
rollback backends may have narrower scope support; unsupported requests fail explicitly.
Backend query rejection is 400; unavailable, stale or invalid backend responses are 503,
not successful empty results. Canonical and compatibility endpoint bodies remain equal.

## bluesky-compatible alias

GET https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=<prefix>&limit=10

identical behavior and response. exists so switching from bluesky's appview is a base-URL swap;
kept indefinitely. new integrations should prefer the canonical endpoint.

## architecture

Tantivy serves our own index, seeded from verified R2 artifacts and updated continuously
from Turso. The public Worker handles caching and API responses. Offline refresh builds
and qualifies an inactive backend before routing changes; live updates do not wait for a build.
The current 12-hour rebuild cadence renews source-coverage evidence, not a Tantivy requirement.

## etiquette

- smoke tests should set X-Client: smoke-testing. They remain in operational totals
  but are excluded from public app/adoption counts.
- scripts should send a descriptive User-Agent (e.g. typeahead-community-smoke/1.0).
  Cloudflare can reject default library user agents with 403/error 1010 before the API.
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
- https://typeahead.waow.tech/docs — API quickstart and reference (html)
- https://typeahead.waow.tech/stats — index health, latency, traffic (html)

source: https://tangled.org/zzstoatzz.io/typeahead
contact: @zzstoatzz.io on bluesky
`;
}
