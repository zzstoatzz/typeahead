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

The existing transport Worker will read `control/search-routing.json` from R2.
It accepts only these two exact origins. Search, diagnostics, freshness, and the
existing authenticated inline refresh all follow the same route. Updating the
route requires the previous ETag; stale writers fail. The controller needs no
Cloudflare account administration token. The transport remains compatible with
the current direct-origin public configuration during qualification.

Before routing, require full artifact checksum/structural admission, current build
and watermark, global/exact/CJK/PDS probes, replica lag under 600 seconds, and a
cached restart at two CPUs / 1 GiB. The controller observes public endpoints after
switching and restores the previous route on failure. An interrupted or rejected
installation leaves production serving. Unknown or unowned idle resources stop
the run rather than being deleted.

The next verification gates are the real disposable installer tests, router
runtime tests, full snapshot rollout, rollback exercise, actual scheduled
registration and queued parameters, and inventory/cleanup. The publisher is not
complete until all these gates pass and the recurring schedule is active.
