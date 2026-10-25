# Automatic isolated snapshot refresh

Implementation in progress, October 25, 2026. Current production remains on the
verified admitted snapshot; no automatic refresh is enabled by this document.

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
stable URL, pointing to the unchanged qualified backend. Validated route metadata
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
The remaining gates are full snapshot rollout, rollback exercise, actual scheduled
registration and queued parameters, and final inventory/cleanup. The publisher is not
complete until all these gates pass and the recurring schedule is active.

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
