# Community smoke testing

[Invitation](invitation.md) and [agent prompt](agent-prompt.md).
The prompt is also available as a [Tangled string](https://next.tangled.org/strings/zzstoatzz.io/3mz4bswbmj322).

The agent asks for accounts and expected queries first, then runs a read-only
20-request maximum at no more than one request every two seconds. UI behavior
requires browser interaction; API requests alone do not verify it. Testers can
write small scripts with the same limits. Reports distinguish unexpected results
from source-coverage uncertainty and subjective ranking preferences.

Pi validation was attempted on October 30, 2026 with extensions, context files
and tools disabled for the intake question. Both configured providers rejected
the request with an expired authentication token; no model response or service
request occurred. This prompt has not yet passed an end-to-end Pi rehearsal.
After refreshing Pi authentication, run from a scratch directory:

```sh
pi --no-extensions --no-skills --no-context-files @/absolute/path/to/agent-prompt.md
```

For scripts, set a descriptive `User-Agent`, for example
`typeahead-community-smoke/1.0`, as well as `X-Client: community-smoke-test`.
Live checks found that Cloudflare rejects Python urllib's default user agent
with 403 / error 1010; the descriptive agent and ordinary curl both passed.
Report edge rejections separately from engine results. Do not disable edge
protections or impersonate another application to run a smoke test.

The shareable prompt is a draft. The invitation has not been posted or sent.
