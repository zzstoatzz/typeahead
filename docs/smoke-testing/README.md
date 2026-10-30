# Community smoke testing

[Invitation](invitation.md) and [agent prompt](agent-prompt.md).
The prompt is also available as a [Tangled string](https://next.tangled.org/strings/zzstoatzz.io/3mz4crmo6db22).

Tagged smoke traffic remains in operational request/error/search totals, but
`smoke-testing` and the earlier `community-smoke-test` label are excluded from
public app lists and app counts. No stored traffic is deleted. Hourly materialized
app counts and link previews adopt the exclusion on their next scheduled refresh.

The agent asks for accounts and expected queries first. The current prompt
sets no request-count or pacing cap; it respects the service's rate-limit responses. UI behavior
requires browser interaction; API requests alone do not verify it. Testers can
write small scripts with the same limits. Reports distinguish unexpected results
from source-coverage uncertainty and subjective ranking preferences.

Pi's cached Codex credential was rejected despite its future stored expiry.
A forced refresh through `pi auth print-bearer-token --provider openai-codex
--min-expiry 168h > /dev/null` recovered it without exposing the token.
The no-tools intake rehearsal then asked for accounts and expectations as intended.
The prior, longer prompt's [API rehearsal](pi-rehearsal.md) completed eight paced GETs,
covering three exact handles, prefixes and alias equivalence. No UI interaction
was claimed.
Run from a scratch directory:

```sh
pi --no-extensions --no-skills --no-context-files @/absolute/path/to/agent-prompt.md
```

For scripts, set a descriptive `User-Agent`, for example
`typeahead-community-smoke/1.0`, as well as `X-Client: smoke-testing`.
Live checks found that Cloudflare rejects Python urllib's default user agent
with 403 / error 1010; the descriptive agent and ordinary curl both passed.
Report edge rejections separately from engine results. Do not disable edge
protections or impersonate another application to run a smoke test.

The shareable prompt is a draft. The invitation has not been posted or sent.
