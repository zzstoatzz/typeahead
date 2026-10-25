# Automatic isolated snapshot refresh

Live and verified October 25, 2026. Full publication, isolated installation,
cached restart, guarded promotion, rollback and prepared-backend recovery passed.
The active fixed cron runs at 06:00 UTC on odd-numbered days of the month, at most
48 hours apart; the next actual queued run is October 27. See the
[execution record](../ops/automatic-refresh-20261025.md).

The recurring cloud Prefect publisher builds and fully verifies immutable compact
and legacy artifacts. A rollout controller installs compact artifacts onto an idle
Fly app with its own fresh volume. Installation and admission finish before the
search process starts. The legacy twin stays in R2; routing rollback uses the
previous backend and its independent live overlay.

The two managed apps are `typeahead-startup-20261024` and
`typeahead-search-refresh`. The serving app must never be resized, restarted,
downloaded onto, or deleted by the refresh job. Each app follows its own slot
pointer. Only the idle app may have its pointer or infrastructure changed.

The existing transport Worker reads `control/search-routing.json` from R2.
It accepts only these two exact origins. Search, diagnostics, freshness, and the
existing authenticated inline refresh all follow the same route. Updating the
route requires the previous ETag; stale writers fail. The controller needs no
Cloudflare account administration token. The transport remains compatible with
direct-origin configurations for emergency recovery. Public traffic now uses its
stable URL, selecting the current qualified backend. Validated route metadata
may be cached for one second; admin writes and diagnostic route reads bypass it.

Before routing, require full artifact checksum/structural admission, current build
and watermark, global/exact/CJK/PDS probes, replica lag under 600 seconds, and a
cached restart at two CPUs / 1 GiB. The controller observes public endpoints after
switching and restores the previous route on failure. An interrupted or rejected
installation leaves production serving. Unknown or unowned idle resources stop
the run rather than being deleted.

Real disposable installer tests and router runtime checks now pass. They include
corrupt-artifact rejection, recovery, inactive resource cleanup, app credential
scoping, real R2 conditional writes, global/PDS routing and preserved admin auth.
Full-cycle, rollback, retry, final resource and queued-schedule checks also pass.
Cached restart explicitly stops the candidate, waits for stopped state, updates
with `skip_launch`, starts it and waits for started state before HTTP checks.
This prevents the old process from satisfying readiness during an async restart.
A failed candidate remains inactive for diagnosis and retry. The publisher's
rollout task reuses the same verified publication and prepared disk on retry;
`resume_rollout=true` also recovers it without another build or publication.
Cold queries still pay deferred I/O and can exceed the unchanged 2.5-second
qualification budget. Those failures block promotion; the successful recovery
required retries. This is not a guarantee of sub-second cold query latency.

The original shared root `latest.json` remains fixed for legacy recovery. Normal
automation uses the verified build pointer and per-slot pointers; it does not
resume bulk installs on the serving disk. The PLC identity deployment remains
paused independently. Source/configuration and live schedule evidence are owned
by [my-prefect-server](https://tangled.org/zzstoatzz.io/my-prefect-server/blob/main/docs/automatic-typeahead-refresh.md).

## Reproducible installer checks

The installer image uses the already qualified native binaries; only its Python
bootstrap layer changes. Build from `scripts/snapshot_refresh/` with its Dockerfile,
using `fly deploy --app typeahead-search-refresh --build-only --push --remote-only`
and a source-revision build argument. Pin the resulting digest in the publisher's
`INDEX_REFRESH_IMAGE`; building an image does not authorize routing it.

Local checks:

```sh
python3 -m unittest discover -s scripts/snapshot_refresh -p 'test_install.py' -v
bun test src/search-transport.test.ts
bunx --no-install tsc --noEmit
bunx --no-install wrangler deploy --config wrangler.search-transport.jsonc --dry-run --outdir /tmp/typeahead-route-test
node scripts/snapshot_refresh/test_transport.mjs /tmp/typeahead-route-test/search-transport.js
```

The last command uses real workerd/R2 local storage and reads the already healthy
backend. It exercises missing/corrupt route rejection, a one-second metadata cache,
scoped acknowledgment, admin authentication and exact origin restrictions. It does
not change the production routing object. Actual R2 installer/cleanup receipts are
under `docs/evals/startup-refresh-*-20261025.json`.
