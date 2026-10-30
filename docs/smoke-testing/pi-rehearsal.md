# Pi smoke-prompt rehearsal

Pi 0.87.0, openai-codex provider, default model. Extensions, skills and project
context were disabled; only read/bash tools were enabled. The supplied rehearsal
accounts were zzstoatzz.io, typeahead.waow.tech and bsky.app. The request budget
was ten GETs, at least two seconds apart, ten-second timeout, restricted to the
public Typeahead origin. Headers: `X-Client: smoke-testing` and
`User-Agent: typeahead-community-smoke/1.0`.

The following is Pi's final report; timing is not a performance benchmark.

**API smoke test:** 8 GET requests, including one `llms.txt` read and one alias check. Run October 30, 2026, 17:29:06–17:29:35 UTC. UI interaction was **not tested**; browser tools were unavailable.

| Query | Expected account / behavior | Observed | Result |
|---|---|---|---|
| `zzstoatzz.io` | Exact handle first | `zzstoatzz.io` #1; HTTP 200 | Pass |
| `zzstoatzz` | Prefix presence, exploratory | `zzstoatzz.io` #1; HTTP 200 | Pass |
| `typeahead.waow.tech` | Exact handle first | `typeahead.waow.tech` #1; HTTP 200 | Pass |
| `typeahead` | Prefix presence, exploratory | `typeahead.waow.tech` #1; HTTP 200 | Pass |
| `bsky.app` | Exact handle first | `bsky.app` #1; HTTP 200 | Pass |
| `bsky` | Prefix presence, exploratory | `bsky.app` #1; HTTP 200 | Pass |
| `zzstoatzz.io` (compatibility alias) | Same result as canonical endpoint | HTTP 200; same results and order, checked at 17:29:35 UTC versus 17:29:23 UTC | Pass |

No unexpected results to prepare for reporting. Nothing was published or sent.
