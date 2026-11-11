# shadow comparison

decides whether a shadow search backend may take production traffic. it compares the shadow with production on corpus, search results, latency and freshness, and writes a PASS or FAIL per gate. it is the comparison job in step 2 of "proving it before it serves" in the [embedded-store plan](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/plans/embedded-store-service.md), tracked in issue 43.

status, 2026-11-11: tested against local fixtures, local Tantivy servers, an index read back through `dump-actors`, and a fake of the store's read endpoints. the store process serves those endpoints on the shadow machine only, so the corpus phase is split in two: a builder packs production's bundle as views, and the walk runs [on the shadow machine](#corpus-phase-on-the-shadow-machine). one real walk ran on 2026-11-11 and was stopped at 200,000 of 13.2 million accounts; what it showed is why the corpus phase counts refresh-derived differences instead of tracking them and why the arbiter reads handles from DNS and the handle's own host. that design has run against fixtures, against the stopped walk's 12,591 recorded differences, and, for the arbiter, against the live authorities for 12 of those accounts. no walk has completed against the shadow.

both backends are the [Tantivy server](https://tangled.org/zzstoatzz.io/typeahead/blob/main/services/tantivy/README.md). the tool only reads: `/search`, `/debug/search`, `/ready` and `/index/status` on the public listener, `/admin/source-page` and `/admin/source-state` on the admin listener, and the two store read endpoints described under corpus sources.

## what each phase proves

**corpus** walks both sides' actors in DID order and compares every DID. a DID is live on a side when a document exists and is not a tombstone. the question it answers is whether the shadow is ever wrong where production is right, not whether the two are equal: the shadow refreshes profiles and verifies identities on its own timetable, so on the first real walk one account in sixteen differed in a field a refresh writes, and almost all of those were the shadow being newer.

each differing DID falls in one or more classes of two kinds.

identity-level classes are about which accounts exist and how they are found: `only_in_production`, `only_in_shadow`, `handle_differs`, `pds_differs` (the canonical PDS host), `visibility_differs` and, when a server returns no fields, `document_hash_differs`. every DID with one is kept in full, up to `MAX_TRACKED_IDENTITY_DIFFERENCES`, and re-read once after a settle delay; the count is of those that still differ.

`visibility_differs` is not the stored `hidden` flag. with a moderation policy loaded, which both backends have, the server ignores that flag and decides per query from the actor's labels and handle, the blocked domains and the per-DID overrides. [moderation.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/moderation.py) ports that rule from `hidden_at_ms` in the server's `moderation.rs`, reads the same rules artifact the server embeds (`src/moderation-regexes.json`: seven slur patterns, the labeler, the hiding values), and is held to the 2,170 cases in `services/tantivy/tests/moderation/cases.json` that the Rust evaluator is held to. a side would be returned when it is live, has a handle that does not end in `.invalid`, and is not hidden by that rule at the walk's start time. blocked domains and overrides are not in any actor document: `--moderation-policy` takes the snapshot the servers load, in the shape `ModerationPolicy::validate` accepts. without it blocked domains are not applied and a shadow record's own `override` stands in for the policy's, on both sides. two things are not ported: the engine's handle quarantine list and PDS scoping, which are keyed on fields whose differences are classes of their own, and JavaScript's lenient date parsing in the Worker's `shouldHide`; a label expiry the server refuses is counted as `visibility_unevaluated` and fails gate (a) rather than being guessed. a side that does not itemise its labels (an admin listener) is judged by its stored flag, and then so is the side it is compared with.

refresh-derived classes are what a profile refresh writes: `quality_score_differs` (compared at single precision), `labels_differ` (the set of labeler and value pairs), `display_name_differs`, `avatar_differs` (the CID), `associated_differs`, `created_at_differs`, and `profile_differs` when a side gives only the server's profile hash. these are counted in full and never held in memory in full: per class, the count, the breakdowns below, and the `REFRESH_SAMPLE_SIZE` DIDs with the smallest SHA-256 in each of two strata are kept, whatever order the walk met them in. the strata are whether the shadow refreshed the record after the bundle's watermark or not (see corpus sources). score differences are also counted by size: production holding 0, the shadow holding 0, a gap under 1, 1 to 50, 50 or more. the sampled DIDs are re-read after the settle delay and those that no longer differ leave the sample; the counts stay as walked.

five more things are counted for information and gate nothing, with 25 examples each: `stored_hidden_differs` (the flag the server ignores), `label_detail_differs` (the same labeler and value with another `cid` or `cts`), `avatar_form_differs` (a CID on one side, a URL to the same CID on the other), `pds_form_differs` (the same host written differently), and `visibility_unevaluated`, which does fail the gate.

every class is counted by handle kind (under `.bsky.social`, custom domain, none), by PDS kind (Bluesky-run, independent, none) and by its ten most frequent PDS hosts. each backend's build and watermark are recorded at the start and end, so the report states the window the comparison covers. the report also states how long the walk spent reading each side and comparing.

with `--arbiter`, the kept differences are judged against the authority for the field that differs and labelled `shadow is right`, `production is right`, `both stale` or `undetermined`, each with its reason:

| what differs | authority | how |
|---|---|---|
| handle | the DID document and the handle itself | the first `at://` entry of `alsoKnownAs` in the PLC or did:web document, which must resolve back to the DID by a DNS TXT record at `_atproto.<handle>` (exactly one `did=` record) or by `https://<handle>/.well-known/atproto-did` (no redirects, at most 8 KiB); either is proof. a claimed handle that provably does not resolve back means the right value is `handle.invalid`. a failure to ask (a timeout, a 5xx) is `undetermined`, never a verdict |
| PDS | the DID document | the `#atproto_pds` service endpoint |
| presence | the directory and the PDS | a PLC tombstone or missing document means gone; otherwise `com.atproto.sync.getRepoStatus` on the document's PDS. when the PDS cannot be asked, an account the appview serves counts as active |
| visibility | all of the above and the appview | the moderation rule applied to the verified handle and the appview's current labels |
| score, labels, display name, avatar, associated, creation time | the appview | one `app.bsky.actor.getProfiles` per 25 DIDs, mapped to what a refresh would store now by [refresh.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/refresh.py), a port of `extractProfileFields` and `computeAuthorityScore` held to the 2,000 score rows and 56 profiles in `services/src/store_process/testdata/profile_vectors.json`. a score within `SCORE_TOLERANCE` of the recomputed one is right |

the handle rules are those of zat's resolver (`handle_resolver.zig`, zat 0.7.0-alpha.1), with DNS read over DNS-over-HTTPS. the appview is not asked about handles: it answers `handle.invalid` for handles that resolve and keeps a handle after the PLC document has moved on. a profile refresh never replaces a stored value with an empty one, so when the appview's value is empty, or its counts are missing, the difference is `undetermined` and faults neither side.

it does not prove agreement on anything source-state does not return. the source version and the follower, follow, post and activity counters are ignored on purpose: they do not affect ranking or the response. a label's `ver` is ignored too: production's `actors.labels` keeps it only where the profile refresh wrote the label last and drops it where the label stream did, and the index bridge never sends it. the arbiter covers a sample, not every difference. it judges labels by what the appview returns to an unauthenticated caller, which is Bluesky's moderation service and self-labels, so a label from another labeler that one side holds reads as that side being wrong. it cannot judge `profile_differs` or `document_hash_differs`. it checks that a handle's host has a public address before contacting it and then lets the HTTP client resolve the name again, where zat connects to the address it checked. the score uses the platform's `log10` where the Worker uses V8's; the vectors pass on this platform.

## corpus sources

production's admin listener binds loopback and its binary predates the `fields` option, so production is normally read from its published bundle instead. `--production-bundle` takes a view file made from [dump-actors](https://tangled.org/zzstoatzz.io/typeahead/blob/main/experiments/zkv-scale/README.md) output, which prints every stored actor of a bundle's index, tombstones included:

```sh
dump-actors INDEX_DIR | uv run scripts/shadow_compare/bundle.py sort --input - --out views.jsonl --tmp SORT_DIR
```

the dump is in segment order. the sort reduces each line to its comparable view, writes sorted runs of 250,000 views and merges them, so memory holds one run and `SORT_DIR` plus the output hold the views twice at the peak. measured on a laptop with 500,000 synthetic actors (262 MB): 8 s, 374 MB of memory, and 603 MB of disk at the peak, of which 298 MB is the output that stays. scaled linearly to the 13.2 million actors and 6.7 GB of the 2026-11-10 bundle that is about 15 GB of disk at the peak and under four minutes on the same laptop, with memory unchanged because the run size is fixed; no run at that size has been made and a cloud builder's cores are slower. the index refuses to serve two live documents for one DID, so duplicates are not expected; if any appear the highest version wins, then the later line, and the report states the count.

each line of the view file also carries a comparison key, a 12-byte hash of every field the walk compares, and every tombstone carries the same one. the store source computes the same key from a record without building its view, so the walk parses and classifies a view only where the two keys differ. measured on a laptop over 200,000 synthetic actors of which 14,120 differ, against a local stand-in for the store, one process at nice 10: 6.7 s of CPU before and 3.1 s after, three alternating runs each. what remains is mostly decoding the store's JSON pages. the key adds 4% to the view file (119.1 MB to 124.1 MB). a view file or manifest written before the key existed is refused: sort and pack the dump again.

a bundle is production at its watermark, not now, and the shadow keeps refreshing. so each refresh-derived difference is placed in one of two strata by the shadow record's own refresh time, the store's `profile_checked_at`: `refreshed_since_watermark` when it is after the bundle's watermark, `not_refreshed_since_watermark` otherwise. a record the shadow has not refreshed since it was loaded is expected to be behind production wherever production refreshed it later; that is lag, the report estimates how many accounts it covers, and it does not fail the gate. a record the shadow refreshed after the watermark has no such excuse: if production is right about it and the shadow is not, the shadow's refresh wrote something wrong. a bootstrap load stamps every record's `profile_checked_at` with the load time, so when the shadow was loaded after the bundle's watermark, `--shadow-bootstrap-at` gives that time and only refreshes after it count. without it such a shadow has every record in the refreshed stratum, which is the stricter reading. a shadow record with no refresh time, or a comparison with no watermark, is read the same strict way.

`--shadow-store` reads the shadow's store through the store process instead of the shadow's admin listener. it needs two POST endpoints under `/store/`, a page of records after a DID and a batch of records by DID, with a bearer secret named by `--shadow-store-secret-env`; the request and response shapes are stated at the top of [store.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/shadow_compare/store.py). a record is compared as the document the index bridge would send for it. the shadow's admin listener still works as the shadow side, but its documents carry neither a refresh time nor itemised labels, so against a bundle every refresh-derived difference is held to the gate and visibility is the stored flag.

what this cannot detect: a difference in a field neither side stores in the actor; a blocked domain's effect when no policy snapshot is given, though a handle that differs is flagged either way; a store whose `profile_checked_at` moves without a refresh, which would put lagging records in the strict stratum and can only fail the gate, not pass it; a refresh-derived defect rarer than the sample can see (see the constants); and anything production changed after the bundle was built that the shadow missed, until the next bundle.

a server that predates the `fields` option of source-state returns only a document hash. that hash includes the source version, so against such a server every DID two independent backends hold shows as `document_hash_differs` and the corpus gate cannot pass.

**search** runs the canonical sets from [bench.py](https://tangled.org/zzstoatzz.io/typeahead/blob/main/scripts/bench.py), the [recorded candidate and PDS-scope cases](https://tangled.org/zzstoatzz.io/typeahead/blob/main/deploy/tantivy-dev-ui/candidate-smoke-cases.json), and any extra queries, against both backends directly. per query it reports identical ordered DIDs or the first differing rank, set overlap, actors whose response body differs, whether the scope acknowledgment headers agree, and whether bench.py's structural checks and each case's recorded expectations pass on each side. a differing query is run once more after a settle delay. a remaining difference is explained only when a DID the corpus phase found differing appears in either result.

it does not prove agreement on queries outside the set, and an explanation is an association, not a derivation: a flagged DID in the results does not show that it caused the difference. bench.py's spam guard needs Bluesky's answer and runs only with `--bluesky-oracle`. the public Worker path can be compared with `--production-public`, for information only, since its cache may be a minute old.

**latency** sends the same queries one at a time, alternating which backend goes first, once cold and several times warm. it records the backend's own `Server-Timing` search duration and the client-observed time, p50, p95 and p99 for each, and a paired per-query difference. the gate uses server time when every response carried it, otherwise client time. the report lists what was controlled and what was not; client time includes the network path from wherever the tool runs, so it compares the backends only when both are reached from the same region through the same kind of hop. nothing here measures behaviour under concurrent load.

**freshness** samples `/ready` and `/index/status` on both backends over a window: sync lag, watermark movement, moderation freshness, and the normalizer, plan, format and moderation hashes that must agree for results to agree. given a file of recently changed DIDs it also reads them from both admin listeners each sample and reports when the backends first agreed on each.

## load on production

a bundle places no load on production. every network target has its own request and byte budget and a fixed delay between requests; the tool stops when either budget is spent. nothing runs concurrently. with `--corpus-rest N` the walk also waits N times as long as the last admin or store request took, so a slow backend is asked less often: at 1 the backend spends at most half its time answering the walk. the report states the seconds each target spent answering. the defaults allow 2,000 admin requests and 256 MiB per backend, which is a partial walk: a full walk has to be asked for with larger budgets, and resumes from `--checkpoint` when it stops. the checkpoint holds the counts, the identity-level differences and the samples, and is rewritten after each window; `--checkpoint-interval` spaces those writes out, and a walk that stops on a budget, a page limit or completion still writes it. every differing DID is also appended to `<checkpoint>.dids`, one per line, so the search phase can explain a differing query without the walk holding them in memory; the report directory gets a copy as `differing-dids.txt`. search, latency and freshness share 2,000 requests per backend at one request a second.

source-page and source-state each take at most 500 DIDs. a window of 500 DIDs costs one page and one state request per backend. measured on a local server with production-shaped records, that is 17.6 kB for the page and 230 kB for the state request and response with fields (82 kB without). each state request hydrates up to 500 stored documents on the serving machine, which is the real cost to production and the reason for the delay.

the arbiter's requests are a second apart per target by default. for each sampled identity-level difference, at most `IDENTITY_SAMPLE_SIZE` per class: one to plc.directory or the did:web host, and then as the class needs, one to the DNS-over-HTTPS resolver, one to the handle's host, one to the account's PDS. for refresh-derived samples: one to the appview per 25 DIDs, at most 168 requests when every class fills both strata. requests to handle hosts and PDSes share one budget of 8,000 and are not retried.

## inputs

`--production` and `--shadow` are the backends' public origins, needed for search, latency and freshness. `--production-admin` and `--shadow-admin` are their admin listeners, which bind loopback on the machine, so the corpus phase and `--changed-dids` must run where both are reachable. `--production-admin-secret-env` and `--shadow-admin-secret-env` name the environment variables that hold each admin bearer. the values are never printed or written to the report, are sent only to `/admin/` paths over https or loopback, and are redacted from error text.

`--phases` selects among `corpus`, `search`, `latency` and `freshness`. `--extra-queries` is a file with one query, or one JSON object of search parameters, per line. `--changed-dids` is a file with one DID per line. `--flagged-from` reads the differing DIDs from an earlier report, and from `differing-dids.txt` beside it when that is there, so search can run without repeating the walk. `--moderation-policy` and `--shadow-bootstrap-at` are described above; `--doh-url` names the DNS-over-HTTPS resolver, Cloudflare's by default. `--out-dir` is where the report goes; the default is a dated directory under [docs/evals](https://tangled.org/zzstoatzz.io/typeahead/tree/main/docs/evals).

## gates

| gate | passes when |
|---|---|
| (a) corpus | the walk completed and the kept differences were re-read; no identity-level difference went untracked; no actor holds labels the server refuses to evaluate; the appview could be read for every sampled batch; no sampled identity-level difference is `production is right`, and none is `undetermined` for presence, handle, PDS or document hash; and in each refresh-derived class, among the sampled records the shadow refreshed after the watermark, `production is right` is at most `MAX_REFRESHED_PRODUCTION_RIGHT_SHARE`. any class with differences fails when the arbiter did not run |
| (b) search | no check fails on the shadow, and every query whose results differ contains a flagged DID |
| (c) latency | for cold and warm, shadow p50, p95 and p99 are each at most production's value × 1.10 + 2 ms |
| (d) freshness | both backends ready, sync lag at most 120 s and moderation fresh at every sample, and each must-match value equal in at least one sample |

| constant | value | meaning |
|---|---|---|
| `IDENTITY_SAMPLE_SIZE` | 150 | identity-level differences judged per class, the ones with the smallest SHA-256 of the DID |
| `REFRESH_SAMPLE_SIZE` | 300 | refresh-derived differences kept and judged per class and stratum |
| `MAX_TRACKED_IDENTITY_DIFFERENCES` | 20,000 | identity-level differing DIDs kept for the re-read; any beyond it fail gate (a) |
| `ARBITER_MUST_JUDGE` | presence, handle, PDS, document hash | classes that may not stay `undetermined` |
| `MAX_REFRESHED_PRODUCTION_RIGHT_SHARE` | 0.02 | share of a refresh-derived class's refreshed-stratum sample that may be `production is right` |
| `SCORE_TOLERANCE` | 1.0 | points a stored score may be from the recomputed one and still be right |
| `SETTLE_DELAY_S` | 120 | wait before kept DIDs are re-read |
| `LATENCY_NOISE_RATIO` | 0.10 | relative allowance in gate (c) |
| `LATENCY_NOISE_FLOOR_MS` | 2.0 | absolute allowance in gate (c) |
| `LATENCY_MIN_QUERIES` | 20 | fewer measured queries fail gate (c) |
| `FRESH_MAX_SYNC_LAG_S` | 120 | the cutover polling bound the server already reports |
| `FRESH_MIN_SAMPLES` | 3 | fewer samples fail gate (d) |

the sample sizes come from what zero findings can rule out: when none of n sampled differences is `production is right`, the true share is under 3/n with 95% confidence, so 150 bounds an identity-level class at 2% and 300 bounds a refresh-derived stratum at 1%. a class with fewer differences than the sample size is judged whole. an identity-level class fails on a single `production is right` because a wrong handle or a missing account is wrong for every search that would reach it. a refresh-derived class is allowed 2% because the authority is read minutes to hours after either side refreshed: a profile can change and change back, and the appview is eventually consistent. that figure, and the score tolerance, are stated bounds and have not been calibrated against a completed run; 1.0 covers the two-decimal rounding and a 1% change in followers (0.43 points) and is far below the gaps that matter, which on the first walk were production holding 0 for accounts with real follower counts. the tracking cap is about five times the identity-level differences the first walk's rate predicts for the whole corpus (51 in 200,000, so about 3,300 in 13.2 million); a walk that exceeds it is not showing drift.

the latency allowance is a stated bound, not a measured noise level. the first runs against real backends were on 2026-11-11 ([what they found](https://tangled.org/zzstoatzz.io/typeahead/blob/main/docs/evals/shadow-latency-20261111/README.md)); they did not calibrate it, but they show its reference moving: over eighteen runs production's own cold p99, which is the slowest of 58 requests, was between 3.1 and 9.0 ms, so the shadow's cold p99 was allowed between 5.4 and 11.9 ms depending on the run. `both stale` passes because the bar is the shadow being no worse than production. a gate whose phase did not run is `NOT_RUN` and the overall verdict is then `INCOMPLETE`. the exit status is 1 when any gate fails, 2 when the run could not finish, 0 otherwise.

on the stopped walk's recorded differences the redesigned classes read: 12,570 differing DIDs, of which 51 identity-level (6 only in production, 20 only in the shadow, 25 handles, 23 of those also differing in what a search returns because one side holds `handle.invalid`), 9,862 scores (production holding 0 in 7,997), 3,916 associated, 397 creation times, 136 avatars, 69 display names, 56 label sets; 182 label pairs differing only in `cid` or `cts` and 4 stored flags are now notes. 1,589 entries are kept in detail, 2.4 MB, where the old checkpoint held all 12,591 in 19 MB. the arbiter, run against the live authorities for 12 of those accounts, found the shadow right in most and production right for two handles the shadow holds as `handle.invalid` though they resolve by the well-known file, which is a shadow defect the gate now fails on. that walk recorded the shadow's content change time, not its refresh time, so its strata there are an approximation.

## run

```sh
uv run scripts/shadow_compare/compare.py \
  --production https://typeahead-tantivy-a.fly.dev --shadow https://SHADOW \
  --phases search,latency,freshness
```

the corpus phase adds the admin listeners, the two variable names and a budget sized for the walk:

```sh
uv run scripts/shadow_compare/compare.py \
  --production http://127.0.0.1:8080 --production-admin http://127.0.0.1:8081 \
  --shadow http://127.0.0.1:9080 --shadow-admin http://127.0.0.1:9081 \
  --production-admin-secret-env PRODUCTION_ADMIN_SECRET --shadow-admin-secret-env SHADOW_ADMIN_SECRET \
  --phases corpus,search,latency,freshness --arbiter \
  --checkpoint walk.json --corpus-max-requests 60000 --corpus-max-mib 8192
```

against a bundle and the shadow's store, with no production endpoint involved. add `--moderation-policy policy.json` when the servers' policy snapshot is at hand, and `--shadow-bootstrap-at` when the shadow was loaded after the bundle's watermark. the store answers on loopback only, so this runs where the store does; [the next section](#corpus-phase-on-the-shadow-machine) is how it gets there:

```sh
uv run scripts/shadow_compare/compare.py --phases corpus --arbiter \
  --production-bundle views.jsonl --bundle-watermark 1793236000 --bundle-build-id b1793236000-example \
  --shadow-store http://127.0.0.1:8082 --shadow-store-secret-env STORE_READ_SECRET \
  --checkpoint walk.json --corpus-max-requests 80000 --corpus-max-mib 32768
```

## corpus phase on the shadow machine

the shadow's store process has no public service and is not getting one, so the walk runs on the shadow machine against `http://127.0.0.1:8082` and production's views are brought to it. `bundle.py pack` turns a sorted view file into three objects: `views.jsonl.gz`, its sidecar `views.jsonl.meta.json`, and `manifest.json` with the bundle id, watermark, counts and each object's size and SHA-256. they live in the bundle bucket under `scratch/shadow-compare/<run id>/`, put there by the [builder](https://tangled.org/zzstoatzz.io/typeahead/blob/main/experiments/zkv-scale/README.md#shadow-comparison-builder), a temporary deployment on a disposable machine that downloads the bundle, dumps and sorts it, packs the views and prints the run id and the manifest's SHA-256.

the machine has bash, curl, tar, jq and rclone, and no Python. `shadow-stage.sh` installs what the walk needs under `/data/compare` and nowhere else: uv 0.13.4 from its GitHub release, checked against a pinned SHA-256; the Python and httpx that uv downloads; and this directory, `scripts/bench.py`, the recorded cases and the moderation rules artifact, extracted from GitHub's tarball of one full commit SHA. it then runs `shadow_stage.py`, which:

1. takes the store's bearer from `STORE_READ_SECRET` in its own environment, or else from the environment of a running process that has it (`/proc/<pid>/environ`), and stops if neither does;
2. fetches the manifest with `rclone cat` and refuses it unless its bytes hash to `--manifest-sha256`, which the builder printed;
3. checks free space: the unpacked views plus 1 GiB must fit while 12 GiB stays free on the volume, because the store's database needs room to compact;
4. fetches the sidecar and streams the views through gunzip to `inputs/views.jsonl`, checking size and SHA-256 of both the object and the unpacked file; a stream that is anything else leaves no file;
5. runs `compare.py --phases corpus --arbiter` against the loopback store with `--bundle-watermark` and `--bundle-build-id` from the manifest, a budget of 80,000 requests and 32,768 MiB, and a checkpoint written at most once a minute. between requests it waits as long as the store took to answer the last one and never less than 20 ms (`--rest 1 --delay 0.02`), so the store spends at most half its time on the walk however slow the machine is, and a store that answers in milliseconds is not held to a fixed pause.

it prints the gate summary and nothing else; errors go to standard error with credential values replaced by their variable names. the exit status is compare.py's. a second `run` with the same run id finds the checked views in place, fetches nothing, and resumes the walk from `walk.json`, so a dropped session costs at most a minute of walking. arguments it does not know are passed to compare.py, which is how `--moderation-policy` and `--shadow-bootstrap-at` reach it.

with the arbiter on, which is the default, the machine makes outbound requests to plc.directory, cloudflare-dns.com, public.api.bsky.app, and the handle hosts and PDSes of the sampled identity-level differences: at most 150 accounts per class, a second apart per target. `--no-arbiter` leaves them out and the gate then fails on any class with differences.

`/data/compare/<run id>/` holds `inputs/` (manifest, views, sidecar), `walk.json`, `walk.json.dids` once a DID differs, and `report/`. `clean` removes `inputs/` and keeps the rest; removing `/data/compare` removes everything the script made.

rclone reads `COMPARE_R2_ENDPOINT`, `COMPARE_R2_BUCKET`, `COMPARE_R2_ACCESS_KEY_ID`, `COMPARE_R2_SECRET_ACCESS_KEY` and, for temporary credentials, `COMPARE_R2_SESSION_TOKEN`. they need only read the run's prefix. `COMPARE_REMOTE` replaces them with any rclone path that holds the run directories.

```sh
fly ssh console -a typeahead-shadow-a -C "env \
  COMPARE_R2_ENDPOINT=$R2_ENDPOINT COMPARE_R2_BUCKET=$R2_BUCKET COMPARE_R2_ACCESS_KEY_ID=$KEY_ID \
  COMPARE_R2_SECRET_ACCESS_KEY=$KEY_SECRET COMPARE_R2_SESSION_TOKEN=$SESSION_TOKEN \
  bash -c 'curl -fsSL https://raw.githubusercontent.com/zzstoatzz/typeahead/$COMMIT/scripts/shadow_compare/shadow-stage.sh \
    | bash -s -- $COMMIT run --run-id $RUN_ID --manifest-sha256 $MANIFEST_SHA256'"

fly ssh console -a typeahead-shadow-a -C "bash -c 'curl -fsSL https://raw.githubusercontent.com/zzstoatzz/typeahead/$COMMIT/scripts/shadow_compare/shadow-stage.sh \
    | bash -s -- $COMMIT clean --run-id $RUN_ID'"
```

the first command puts the R2 values in the argument list of processes on the laptop and on the machine, which is why they should be temporary, read-only and scoped to the run's prefix.

the other three phases need only the two public origins and run from anywhere. copying the corpus report off the machine first, with the list of differing DIDs beside it, lets the search phase explain differing queries by the DIDs the walk found differing:

```sh
mkdir corpus-report
fly ssh sftp get -a typeahead-shadow-a /data/compare/$RUN_ID/report/report.json corpus-report/report.json
fly ssh sftp get -a typeahead-shadow-a /data/compare/$RUN_ID/report/differing-dids.txt corpus-report/differing-dids.txt
uv run scripts/shadow_compare/compare.py --phases search,latency,freshness \
  --production https://typeahead-tantivy-a.fly.dev --shadow https://typeahead-shadow-a.fly.dev \
  --flagged-from corpus-report/report.json
```

`--production` is whichever production slot the route names at the time. the 61 canonical and recorded queries cost each backend about 500 requests, one a second, inside the default budget of 2,000.

what the stage costs the machine, measured on a laptop with 200,000 synthetic actors of which 14,120 differ and a local stand-in for the store, then scaled linearly to 13.2 million; no run at that size has been made:

| | measured at 200,000 | scaled to 13.2 million |
|---|---|---|
| views on the volume | 124 MB | 8.2 GB |
| views object fetched (never on the volume) | 38 MB | 2.5 GB, if real views compress like synthetic ones |
| uv, Python and httpx, measured in the container | 163 MB | 163 MB |
| checkpoint, nothing walked / 14,120 differing | 5 kB / 1.3 MB | bounded by the caps, not the corpus: about 37 MB at 20,000 identity-level differences and every sample full, at the 1.5 kB an entry took on the real walk; twice that while it is rewritten |
| list of differing DIDs beside the checkpoint | 466 kB | 27 MB at the first walk's rate of one account in sixteen |
| CPU of the walk, one process at nice 10 | 2.9 s | see below |
| memory | 81 MB | unchanged; it grows with kept differences, which are capped |
| store requests and bytes | 405 and 136 MB | 26,400 pages and 8.9 GB for records shaped like the fixture's |

so the volume holds about 8.5 GB more while a run's inputs are there.

laptop CPU does not scale to the machine. the first real walk, on the shared 4 vCPU machine beside the live collector and search engine, covered 200,000 accounts in 8 minutes: 805 pages, 80 s of that the 100 ms pause, the rest unattributed between the store answering and the walk comparing, because that version did not time them. at that rate a full pass is about 9 hours. on the laptop the same walk now takes 2.9 s of CPU where that version took 6.7 s, and the fixed pause is gone; how much of the machine's time that removes depends on a split nobody has measured. the next run's report states it: seconds reading each side, seconds comparing, and seconds the store spent answering. after the walk come the 120 s settle and the arbiter's requests a second apart per target.

status, 2026-11-11: the stage as it was on 2026-11-10 ran end to end in a `debian:bookworm-slim` amd64 container, the shadow image's base, and its walk ran on the shadow machine on 2026-11-11 against the store and a real bundle's packed views, where it was stopped at 200,000 accounts. the stage as it is now has run only in the unit tests and the builder's local test, against a fake store; it has not run in the container or on the machine.

## develop

```sh
just --no-dotenv test-shadow-compare   # builds typeahead-tantivy and dump-actors, runs unit tests and the integration test
```
