// plain-text service description for LLM agents and curl users, served at /llms.txt
export function llmsTxt(): string {
  return `# typeahead

community-run actor search for atproto (https://atproto.com). unauthenticated, CORS-enabled.
experimental: may break or disappear — don't depend on it for anything critical.

## canonical endpoint

GET https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=<prefix>&limit=10

- q: required. search prefix, matched against handles and display names. a full DID
  (did:plc:… or did:web:…) instead performs an exact lookup and returns that actor alone.
- limit: 1-100, default 10.
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

## other endpoints

- POST https://typeahead.waow.tech/request-indexing?handle=<handle-or-did> — surface an account
  that predates the index. rate limited.
- https://typeahead.waow.tech/docs — migration guide (html)
- https://typeahead.waow.tech/stats — index health, latency, traffic (html)

source: https://tangled.org/zzstoatzz.io/typeahead
contact: @zzstoatzz.io on bluesky
`;
}
