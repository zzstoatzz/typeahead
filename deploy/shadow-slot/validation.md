# Shadow slot validation, 2026-11-10

What was run locally against the image, on an arm64 laptop under OrbStack with every stage emulated as linux/amd64. No Fly resource, production service or corpus data was involved; containers ran with networking off.

## Build

- The first build took 60.8 minutes. Nearly all of it was downloads on a slow link that day: Debian packages 13 and 23 minutes, the Zig archive 34 minutes, the crates 21 minutes.
- A rebuild with those layers cached took 14.9 minutes: the Zig build of the store binary 894 s, the normalizer 83 s, and the offline Cargo build 428 s, in parallel. Zig compiles slowly under emulation; a native amd64 builder was not timed.
- The Zig steps and the Cargo compile ran with `--network=none` and succeeded, so the vendored Zig dependencies (including zkv) and the locked crates are sufficient.
- The image is 61 MB. The reader in it reports SHA-256 `84f5e48e…` in `X-Typeahead-Binary-Sha256`, which is not a released production reader.

## Supervisor

The index was a 40-actor synthetic full-source import made with the image's own reader.

| case | result |
|---|---|
| volume without a receipt | starts nothing, waits, exits 0 on SIGTERM |
| both real binaries | the ingester binary refuses `MODE=store` and exits 1; the supervisor stops the reader and exits 1 |
| reader plus a stand-in on the store port | `/ready`, `/health` and `/search` answer on 8080 with the identity headers and `X-Typeahead-Pds`; `/health` answers on 8082 and on the loopback admin port; `/admin/apply` on 8080 is 404 |
| SIGTERM with both running | both receive it, both exit 0, supervisor exits 0 |
| reader killed with SIGKILL | supervisor stops the store side and exits 137 |
| index-only receipt | only the reader starts; it reports not ready once its watermark is older than the lag limit |
| a forbidden credential set | exits 1 before starting anything |

The supervisor's logic was also run with shell stand-ins for both processes: a store exit of 7 gives exit 7, a clean exit of either process on its own gives exit 1, SIGINT is passed on as SIGTERM, a process that ignores SIGTERM is killed at the grace deadline and the exit is 137, and a non-zero exit after a requested stop is propagated.

The stand-in on the store port was a second copy of the reader, because no binary with the store role existed when this ran.

## Bootstrap script

Run in the image against a directory laid out like the release prefix, through the `BOOTSTRAP_REMOTE` override: two complete fixture builds and one with a manifest only. It chose the newer complete build, verified and extracted the index without the fallback database, stopped at the store loader without `--index-only`, wrote the receipt with it, refused a second run, and rejected a bundle whose size differed from its manifest.

## Not exercised

- The store process: its start, its health endpoint, its shutdown and its use of the volume.
- Today's ingester running normally. It needs Turso over TLS and the Worker, which the shadow must not have.
- Anything on Fly: the configuration file, the checks, the restart policy, the volume, the machine size.
- The bootstrap against R2, a real bundle, or a bundle-sized download.
- Memory and disk figures in the sizing document; they are derived from other measurements.
