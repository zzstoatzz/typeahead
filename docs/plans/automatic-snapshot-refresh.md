# Automatic isolated snapshot refresh

Live state verified October 27, 2026, 06:06 UTC: native `f67005b` serves on
`typeahead-startup-20261024` at two shared CPUs / 1 GiB; the previous backend
`typeahead-search-refresh` remains available for routing rollback. Both have
independent 20 GB disks. See the [release evidence](../ops/sync-hardening-release-20261026.md).
The configured cadence is 06:00 UTC on odd-numbered days; API/worker migration
and actual schedule activation are owned by `my-prefect-server`. Verify that
repo's current migration state before changing clocks.

The recurring cloud Prefect publisher builds and fully verifies immutable compact
and legacy artifacts. A rollout controller installs compact artifacts onto an idle
Fly app with its own fresh volume. Installation and admission finish before the
search process starts. The legacy twin stays in R2; routing rollback uses the
previous backend and its independent live overlay. The operator retired the
older superseded backends/disks October 25; the October 27 native release
prepared the empty managed slot and retained its predecessor for rollback.

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

Build the native binaries and installer from the checked-out revision with
`sh scripts/snapshot_refresh/build.sh registry.fly.io/typeahead-search-refresh:<tag>`
after `fly auth docker`. The script builds an x86-64 Linux release, copies the
native binaries and bootstrap into a temporary image context, and removes that
context on exit. Pin the resulting digest in the publisher's
`INDEX_REFRESH_IMAGE`; building an image does not authorize routing it.

## Search telemetry

Both managed apps require `LOGFIRE_TOKEN` (or `LOGFIRE_WRITE_TOKEN`); the installer
rejects a missing token before touching storage. Stage the canonical existing
Typeahead write token on both apps with
`python3 scripts/snapshot_refresh/provision_telemetry.py`. The encrypted source is
`logfire.typeahead-write-token` in the local SOPS store. Staging does not restart
running machines. New Machines inherit the app secrets; a running process needs
an explicitly planned update to consume a changed token.

The transport marks `/health` requests with `X-Typeahead-Routed-Health: 1`.
Native `http.health` spans record `production_probe=true`, allowing the
[sync alert query](../../scripts/snapshot_refresh/sync-alert.sql) to select the
most recently probed production slot instead of a retired app name. The 30-minute
query window covers the existing 15-minute fleet sweep and five-minute sync loop.
Missing probes, missing sync spans and errored-only sync passes all alert. Spans
from the rollback slot cannot satisfy the selected production slot's sync count.
After cutover, verify a routed health span under the new slot and an error-free
`sync.incremental` span. A concurrent cutover can leave the last probe on the
previous slot until the next routed probe; the independent public freshness check
still measures the routed backend. No telemetry-only check replaces that probe.

Origin connection errors and timeouts return 503 `SearchBackendUnavailable`.

Turso requests refresh Zig's cached TLS validation time before each subsequent
request under the existing client mutex. Certificate verification remains enabled.
The real HTTPS regression is opt-in with `TYPEAHEAD_TLS_TEST_URL` set to a public
HTTPS URL; it requires no database credential.

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
