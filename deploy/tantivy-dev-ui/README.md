# Production UI against the dev search backend

Live at https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev/.

This Worker serves the deployed production frontend through the `PRODUCTION_UI`
service binding. It changes no HTML, CSS or client JavaScript. Relative search
requests, both XRPC aliases, health and index metadata go to the isolated
`https://typeahead-tantivy-a.fly.dev` backend. Production routing is unchanged.

The deployed `DEV_SEARCH_BACKEND` configuration selects managed slot A. It also
allows the retained `https://typeahead-tantivy-dev.fly.dev` backend or managed slot B
at `https://typeahead-tantivy-b.fly.dev`.
Other origins fail closed. This changes the backend binding and response identity
header; frontend bytes still come unchanged from production. Slot A passed cloud
installation and cached restart qualification before the binding changed. Regenerate
binding types after changing the configuration:

```sh
bunx wrangler types deploy/tantivy-dev-ui/worker-configuration.d.ts --include-runtime false --config deploy/tantivy-dev-ui/wrangler.jsonc
```

Other read-only pages retain their existing production owner. Every non-GET/HEAD
request, including the visible request-indexing form, returns `503 DevReadOnly`
without forwarding to production. The preview holds no write credentials and
blocks administrator requests.
Search results bypass caching so manual checks see the dev engine directly.
Successful scoped searches must acknowledge the normalized PDS filter, preference
mode and exact f32 boost using the shared public search contract. Missing or
incorrect acknowledgements return `503 PdsScopeUnavailable`; malformed scope
parameters return `400 InvalidRequest`. This keeps an older backend from silently
answering a preference request as global search.

Deploy only this configuration:

```sh
bunx tsc --project deploy/tantivy-dev-ui/tsconfig.json --noEmit
bunx wrangler deploy --dry-run --config deploy/tantivy-dev-ui/wrangler.jsonc
bunx wrangler deploy --config deploy/tantivy-dev-ui/wrangler.jsonc
```

`verification.json` records byte-identical production/preview homepage bodies,
successful Tantivy health/search and byte-identical canonical/compatibility XRPC
responses on October 29, 2026. Local browser automation was disabled, so this is
HTTP verification rather than visual browser QA. No indexing form was submitted.

`scope-verification.json` records the deployed scope guard: both aliases still
match, unsupported preferences return 503, malformed scope returns 400, and
production/preview HTML remains byte-identical. This does not qualify the older
backend's preference semantics or the undeployed expiry-v4 candidate.

The [managed-slot smoke](../../docs/evals/tantivy-dev-20261028/checkpoint-public-smoke.json)
records the October 30 switch to qualified slot A: 53 read-only requests passed,
including production-identical homepage bytes, both aliases and PDS scopes.
