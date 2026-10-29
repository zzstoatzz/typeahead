# Production UI against the dev search backend

Live at https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev/.

This Worker serves the deployed production frontend through the `PRODUCTION_UI`
service binding. It changes no HTML, CSS or client JavaScript. Relative search
requests, both XRPC aliases, health and index metadata go to the isolated
`https://typeahead-tantivy-dev.fly.dev` backend. Production routing is unchanged.

Other read-only pages retain their existing production owner. Every non-GET/HEAD
request, including the visible request-indexing form, returns `503 DevReadOnly`
without forwarding to production. The preview holds no write credentials and
blocks administrator requests.
Search results bypass caching so manual checks see the dev engine directly.

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
