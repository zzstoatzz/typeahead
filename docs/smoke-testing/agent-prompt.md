Help me test https://typeahead.waow.tech.

Ask which accounts I care about, what I'd type to find them, and whether I want
to filter to a particular PDS. If I've already told you, start testing.

Read /llms.txt. Try exact handles, prefixes, display names and multi-word searches
based on my answers. Check the homepage if you have browser tools; otherwise say
that you only tested the API. Write reusable scripts if useful.

For scripts, send:
X-Client: smoke-testing
User-Agent: typeahead-community-smoke/1.0

Use the public search/profile APIs. Respect 429 responses and Retry-After;
don't work around rate limits. Don't change accounts or call admin endpoints.

Report what I searched, what I expected, what appeared and any errors. Include
reproducible URLs or commands. Distinguish missing accounts from unexpected
ranking, and don't infer someone's PDS from their handle's domain. Ask whether
your findings match my experience. Don't publish anything on my behalf.
