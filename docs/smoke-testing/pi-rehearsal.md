# Pi smoke-prompt rehearsal

Rerun October 30, 2026 with the shortened current `agent-prompt.md`, Pi 0.87.0,
openai-codex provider and default model. Extensions, skills and project context
were disabled. No request-count or pacing cap was added.

The no-tools intake asked which accounts mattered, what the user would type,
and whether they wanted a PDS filter or preference.

The live rehearsal enabled read/bash and supplied zzstoatzz.io,
typeahead.waow.tech and bsky.app, their exact handles and prefixes, and no PDS
preference. Browser tools were unavailable. This verifies the API rehearsal,
not browser behavior or sustained load handling.

## Pi's report

I tested the public API only; browser tools were unavailable. No PDS filter or preference was applied.

| Search | Expected | Observed |
|---|---|---|
| `zzstoatzz.io`, `typeahead.waow.tech`, `bsky.app` | Each exact eligible handle first | Each was **#1** in its search |
| `zzstoatzz`, `typeahead`, `bsky` | Prefix ranking exploratory | The corresponding account was **#1** for each prefix |

The profile API also returned all three accounts, so none was missing. The compatibility alias returned the same results as the canonical endpoint for `zzstoatzz.io`. All requests succeeded with HTTP 200; no rate-limit or other errors occurred.

Reproduce the searches by substituting any of the six terms above for `QUERY`:
`https://typeahead.waow.tech/xrpc/tech.waow.typeahead.searchActors?q=QUERY&limit=10`

Alias checked:
`https://typeahead.waow.tech/xrpc/app.bsky.actor.searchActorsTypeahead?q=zzstoatzz.io&limit=10`

Do these findings match your experience?
