# Tantivy artifact and route adapter

This branch adds an isolated adapter; it does not change a production route or
deploy an installer. The existing `install.py` remains the default native
SQLite/prefix installer and rejects a manifest declaring another engine.

Tantivy uses `tantivy_install.py`. Its manifest has `engine: "tantivy"`,
`manifest_version: 1`, `artifact_format: "tantivy-directory-tar-v1"`,
`backend_format: "tantivy-0.27-f32-did-expiry-v4"`, `active_build`,
`normalize_version: "v2-cjk-bigram"`,
`plan_version: "complete-prefix-groups-v1"`,
`source_policy: "turso-authoritative-observations-v2"`, the full initial
`source_actor_count`, `canonical_rules_source_sha256`, `binary_sha256`, and the
bundle's `byte_size`/`sha256`/`object_key`. The object key is
`releases/search-tantivy/<active_build>/index.tar`. `files` lists every archived
regular file as `{path, byte_size, sha256}`. Paths are single files under
`index/`, including `index/meta.json`; no writer locks, links, directories,
traversal paths, duplicate entries, or unlisted files are admitted. Create the
bundle from an inactive, stopped generation, never from serving files.
The portable manifest binds `source_build_id`, `source_manifest_sha256` (the
SHA256 of the exact native manifest bytes), `legacy_build_id`, `source_watermark`,
`source_database_sha256`, `source_prefix_sha256` and `source_actors_sha256`.
The latter must equal the importer's verified raw actor-stream footer. Native
source count, normalization, database/prefix hashes and legacy twin must agree
with that immutable native manifest. Persist the portable manifest at
`releases/search-tantivy/<active_build>/manifest.json` alongside `index.tar`.
Retention resolves these immutable manifests, including previous generations;
an old build cannot be identified by querying a URL now serving its replacement.

The existing cloud publisher can call `freeze_bundle` in this same adapter after
stopping its caught-up inactive checkpoint. It checks the pinned actual reader,
complete source reconciliation and policy, inventories stable regular files,
streams a tar and file checksums, and atomically publishes a new local artifact
directory. Missing coverage fails before packaging; concurrent file changes
reject the artifact. It does not upload, alter routing or claim runtime readiness.
The publisher still owns R2 upload and exact checksum readback. A raw snapshot
import without an authoritative sweep is not a publishable caught-up checkpoint.
The existing publisher accepts `--engine tantivy --generation DIR
--source-manifest FILE`, using its existing R2 configuration. It verifies that
the exact native manifest is already published, then verifies immutable upload
of the bundle and finally its portable manifest. Native and mutable-pointer
arguments are prohibited in this mode; `latest.json` remains untouched.
After a successful publication, the same command with `--verify-only` verifies
the remote source manifest, bundle size/checksum and exact portable manifest
against retained local metadata. It requires no local tar and never uploads or
changes pointers. Publication retries retain their payloads; a separate owned
cleanup step may retire the local tar/index only after verified success.

The manifest pins `retrieval_strategy` (`per-segment` or `global-merge`).
Installation binds the portable manifest to a separate target configuration:
`image_digest` (full `registry.fly.io/...@sha256:...`), `provider_app`,
`guest: {cpu_kind: "shared", cpus: 2, memory_mb: 1024}`, `volume_id`, and
`volume_size_gib: 20`, and `public_service_ports`. Qualification controls pin
`[]`; an owned final `dev-tantivy` instance or managed `tantivy-slot` may pin
exactly `[80, 443]` after
the same 20 GiB and other admission gates. These deployment fields do not belong
in the portable bundle returned by `freeze_bundle`; the installer receives a
merged bound manifest and checks its own admission receipt against that binding.
Provider observation timestamps belong in the separate
fresh attestation, never in this immutable artifact manifest.

```sh
python3 scripts/snapshot_refresh/tantivy_install.py install \
  --manifest /cloud/manifest.json --bundle /cloud/index.tar \
  --binary /app/typeahead-tantivy --destination /data/tantivy-generations/NEW
```

Installation verifies the archive and every file, runs the pinned actual binary
against the staged directory, checks full-source metadata and source/policy
identity, and writes a durable installation receipt only after moving the index
into a new destination. It never overwrites an existing destination or installs
an `index.db`. A crash before the receipt leaves an unadmitted generation;
recovery must inspect that inactive directory rather than treating it as ready.

An old but structurally valid backup can be staged with
`runtime_qualified: false`. Start it on an inactive service, allow its private
source reconciler to catch up, and qualify the actual public endpoints:

```sh
python3 scripts/snapshot_refresh/tantivy_install.py qualify \
  --manifest /cloud/manifest.json \
  --installation /data/tantivy-generations/NEW/installation.json \
  --provider-attestation /cloud/provider-attestation.json \
  --url https://typeahead-tantivy-dev.fly.dev
```

Managed slots use `typeahead-tantivy-a` and `typeahead-tantivy-b`, each with an
independent HTTPS origin and volume. Their provider role is `tantivy-slot`,
owned by `typeahead-snapshot-refresh`; the bound `provider_app` determines the
only admissible public origin. The Worker accepts those two origins and the
retained dev origin. A private qualification app cannot become a serving route.
These contracts do not create or deploy slots; actual lifecycle qualification
is still required before promotion.

Qualification reads only `/index/status` and `/ready`, bounds response size and
time, prohibits redirects, checks engine/build/format acknowledgements, and
requires full source counts, expiry-v4 format, positive observation revision,
the authoritative cursor policy, a completed discovery cycle, polling age at
most 120 seconds, policy age at most 300 seconds with matching canonical rules,
complete source-wide expiry evidence bound to raw actor-stream count/footer hash
and classifier/rules identity with no unsupported or malformed current live actors,
and a completed full source/backend DID sweep whose age and duration are each
at most 86400 seconds. Library callers can tighten these bounds. These are
admission ceilings, not empirically established SLOs. The receipt explicitly
sets `slo_empirically_qualified: false`; performance, real-corpus correctness,
coverage and operational qualification remain separate release gates.
Loopback HTTP tests explicitly record their actual observed origin and
`test_only: true`, `runtime_http_verified: false`; they cannot establish that
the public dev origin was qualified. Metadata-only library calls likewise
cannot establish public HTTP verification.

The qualifier additionally compares actual startup-computed executable SHA and
retrieval strategy in both status and success headers against the manifest.
It requires a separate normalized schema-1 Fly Machines/Volumes API receipt
observed at most 120 seconds ago, matching the pinned immutable image, guest and
encrypted `/data` volume. The provider record must prove a started isolated
owned Tantivy instance with the manifest-pinned role-specific ports and no configured overrides
of reserved `FLY_MACHINE_ID`/`FLY_MACHINE_VERSION` variables. Provider app and runtime machine/instance identity must match the independent observation,
including the successful endpoint headers. Protected production and retained
dev machine IDs are rejected. Synthetic provider test receipts explicitly use
`test_only: true` and cannot set `provider_resources_qualified`; absent runtime
instance evidence likewise cannot qualify final resources. The existing 40 GiB
dev service therefore cannot qualify the final 20 GiB serving shape.

The resulting route has explicit `engine: "tantivy"`, the bounded dev origin,
build, expiry-v4 format, and an admission receipt digest. Legacy route payloads
remain native. Native and Tantivy origins cannot be interchanged. Route and hint
equality include engine, format and admission digest. Successful Tantivy
responses must acknowledge engine/build/format, binary SHA and retrieval
strategy. Native `/admin/refresh` pushes
return 503 `NativeRefreshUnsupported` on a Tantivy route; authoritative source
polling remains responsible for freshness. `/debug/index` is unavailable for
Tantivy and fails explicitly.

`conditionalRouteUpdate` in `src/search-route.ts` is the internal R2 CAS primitive
for a separately authorized controller. It preserves the previous route and
seeds a native-only `native_rollback` anchor on native-to-Tantivy promotion.
Successive Tantivy promotions retain that anchor independently of `previous`.
The anchor preserves the original native target metadata, including source
watermark, machine/volume identity and legacy twin, for refresh and retention.
An invalid or missing native anchor prevents a Tantivy promotion. It
rejects a stale ETag. It exposes no public mutation endpoint and does not itself
establish admission or authorize promotion. A controller must validate the
qualification receipt and remaining release gates before calling it. Rollback
must retain and qualify the independent native slot; an old Tantivy receipt
cannot establish current readiness.

Before any later promotion or rollback, either rotate the nonsecret Worker
`SEARCH_CACHE_GENERATION` token or prove the old 60-second TTL has fully drained.
Tokens contain 1–48 letters/digits/hyphens, begin with a letter or digit, and
default to `legacy`. Invalid configuration fails closed. All query/limit/PDS
cache identities include this token. Backend engine/build/format/binary/strategy
and machine acknowledgements survive caching; a retained old answer identifies
its actual original backend rather than pretending to come from the new route.
An older native backend is labeled native only by validated native transport
routing; missing headers on an unknown direct backend remain unknown.

Worker configuration and route CAS are separate provider updates. Rotating the
token does not make them atomic: an operator/controller must verify uncached
actual backend identity and the expected cache generation after both changes,
then establish the corresponding rollback barrier. Cache hits add no control
read. These are explicit future cutover gates, not a claim of published or
qualified production cutover.

Local verification:

```sh
python3 scripts/snapshot_refresh/test_install.py
python3 scripts/snapshot_refresh/test_tantivy_install.py
bun test scripts/snapshot_refresh/engine-transport.test.ts \
  src/search-transport.test.ts src/validated-search.test.ts
```

The new tests use the actual compiled Tantivy binary and local HTTP, and actual
workerd with filesystem-backed R2 for conditional promotion, stale-writer
rejection and rollback. They do not contact production or any cloud API.
