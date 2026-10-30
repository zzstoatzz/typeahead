# Managed candidate integration smoke

Prepared October 30, 2026. This recipe has not sent traffic. The controller must
supply the newly qualified slot URL and expected build/binary/image identities.
The preview must first be explicitly bound to that slot; its default still points
to the retained older dev service. No production search route changes are involved.

Use only `https://typeahead-tantivy-a.fly.dev` or
`https://typeahead-tantivy-b.fly.dev` as `CANDIDATE`, and
`https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev` as `PREVIEW`. Do not substitute
production, the older dev backend, or a load-test target. The controller owns
invocation, evidence and resource cleanup.

## Run the bounded smoke

After the controller qualifies the candidate and binds the preview to it:

```sh
python3 deploy/tantivy-dev-ui/candidate-smoke.py \
  --candidate "$CANDIDATE" --build-id "$EXPECTED_BUILD_ID" \
  --binary-sha256 "$EXPECTED_BINARY_SHA256" --output "$NEW_RECEIPT_PATH"
```

The runner performs53 GETs on its normal path, stops before search if candidate or
preview identity/readiness is wrong, and enforces the bounds below. It accepts
only the two managed slot origins and the fixed preview origin. Redirects fail.
Its exclusive mode0600 JSON receipt contains counts, checks, hashes, selected
headers and individual timings, with no profile bodies or source credentials.
Stdout is one compact summary. It exits nonzero for failed or inconclusive checks.
Existing output files are never overwritten. No deployed requests have been sent
while preparing the runner.

The runner uses this casefile and the existing independent ASCII/Han predicates;
it replaces the manual selected-smoke invocation below for the normal pass. Its
`source_membership_verified:false` and `resource_verified:false` explicitly leave
those proofs to the source-witness/provider receipts below. The optional witness
checks are separate; this runner never queries the source database.

## Bound the pass

Run the existing request helpers under one supervised 120-second process deadline:
at most80 GETs, sequentially at1request/second,5seconds/request,512KiB/response and
32MiB total. Preserve only checks, counts, timings, body hashes and selected headers;
keep profile bodies transient. No writes, admin lifecycle tests, load phase,
restarts, full source scans or external comparison requests.

Do not run `scripts/smoke.py` main: it submits a valid `/request-indexing` POST even
without an admin secret. Its full profile/moderation probes also consult services
outside this candidate. `scripts/perf/qualify.py`, `deploy/tantivy-dev/load.py` and
`qualify-real.py` CLI retain their evaluator/dev-only guards; do not bypass or relax
those guards to run this recipe.

## Reuse the public checks

This selected subset calls only public search GETs. Run it against `PREVIEW` once,
within the enclosing request/deadline budget:

```sh
python3 - "$PREVIEW" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location('candidate_smoke', 'scripts/smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
for name in ('test_response_shape', 'test_known_handle', 'test_prefix_match'):
    getattr(smoke, name)(sys.argv[1])
raise SystemExit(bool(smoke.failures))
PY
```

The preview must still serve the production UI unchanged through `PRODUCTION_UI`.
Fetch its homepage once and preserve its SHA/header identity. Compare with an
independently current production-UI body receipt if one already exists. Without
that receipt, report forwarding/configuration evidence only; do not label a stale
homepage digest as a current byte-equivalence proof. HTTP checks do not establish
visual browser correctness.

## Exercise both aliases

Use [the ten explicit cases](candidate-smoke-cases.json) on both targets and both
paths, with URL-encoded parameters:

- `/xrpc/tech.waow.typeahead.searchActors`
- `/xrpc/app.bsky.actor.searchActorsTypeahead`

That is40 GETs. Reuse the response-envelope/identifier/limit assertions from
`scripts/smoke.py` and the independent ASCII/Han predicates in
`deploy/tantivy-dev/qualify-real.py`. Do not treat the candidate's own response as
independent expected truth. A missing historical witness or no Han candidates
requires source-witness clarification; it cannot silently become a pass.

For each same-target alias pair require identical successful body bytes and
matching scope acknowledgements. If the committed source cursor changes between
responses, classify equality as inconclusive; do not infer an alias defect from a
live mutation. Record the cursor hash/watermark around the pass. The preview must
return `X-Typeahead-Search-Backend: $CANDIDATE` and `Cache-Control: no-store`.
A scoped200 without the exact required headers is a failure. In the preview,
missing acknowledgements must surface as503 `PdsScopeUnavailable`, never a global200.
The existing workerd tests exercise that failure injection; do not intentionally
break the deployed backend to manufacture it.

For example, the boost case uses this request shape; store its body privately and
retain only hash/count/checks:

```sh
curl --silent --show-error --max-time 5 --max-filesize 524288 \
  --proto '=https' --get \
  --data-urlencode 'q=a' --data-urlencode 'limit=10' \
  --data-urlencode 'prefer_pds=selfhosted.social' \
  --data-urlencode 'preference=boost' --data-urlencode 'boost=100' \
  --dump-header "$PRIVATE_SCRATCH/headers" --output "$PRIVATE_SCRATCH/body" \
  "$PREVIEW/xrpc/tech.waow.typeahead.searchActors"
```

The boost acknowledgement is f32 bits `42c80000`, not the decimal string `100`.
After checking, delete the transient response files.

A hard-PDS header alone does not prove membership. Reuse one source-backed witness
from candidate qualification, or the bounded primary-key lookup logic already in
`qualify-real.py:source_rows`, running cloud-side with existing read-only source
credentials. Verify its global exact result, exact result in its actual canonical
PDS, and absence from a distinct PDS. At most3 additional search GETs and3 indexed
source SELECTs; source changes/newer rows are inconclusive. No eligible witness
means the membership portion remains unverified. Preference/boost ordering on
unrelated live broad results is not a new full top-K proof.

## Retain bounded operational evidence

Capture candidate and preview `/index/status`, `/ready`, and `/health` before/after
as needed within the same80-request cap. Bind build/source build, actual binary
SHA, retrieval strategy, full import, expiry coverage, fresh remote policy/poll,
checkpoint handoff and coverage deadline to the controller's exact release and
provider receipts. Checkpoint-backed D1 correctly keeps traditional
`cutover_source_ready:false`; use its explicit checkpoint admission branch.

Repeat exact/prefix requests once and retain individual elapsed times/max/failures.
These small samples are integration observations, not p95/p99 or a cold-disk/load
qualification. Preview responses bypass edge caching. Root-owned provider/resource
receipts must corroborate2sharedCPUs,1GiB RAM,20GiB independent disk, free space,
OOM/restarts and memory/IO observations; HTTP latency is not resource evidence.

The previously matched full-corpus D1 benchmark applies only when binary SHA,
retrieval strategy and serving shape match its receipts. This pass adds current
checkpoint/import/source-handoff/managed-slot/preview integration evidence. It
neither repeats that performance comparison nor establishes production downtime,
exhaustive recall, complete PDS identity coverage or permission to cut over.

## Audit a few actual source records

`live-source-audit.py` runs on the new managed slot itself, using its existing
`TURSO_URL`, `TURSO_AUTH_TOKEN` and loopback `ADMIN_SECRET`. The controller can stage
this script, `candidate-smoke.py`, and the existing `qualify-real.py` under a small
scratch tree preserving `deploy/tantivy-dev-ui/` and `deploy/tantivy-dev/` relative
paths. It reuses `/app/input/{sync,current_state}.py` from the admitted image; no
image build or corpus copy is needed. Run only after installation/readiness:

```sh
python3 "$SCRATCH/deploy/tantivy-dev-ui/live-source-audit.py" \
  --candidate https://typeahead-tantivy-a.fly.dev \
  --build-id "$EXPECTED_BUILD_ID" --binary-sha256 "$EXPECTED_BINARY_SHA256" \
  --output "$NEW_AUDIT_RECEIPT"
```

It uses the existing indexed keyset SELECTs for at most8 actors and8 tombstones
in the last covered hour, two canonical PK/profile+policy observations of those
at most16 identities, and read-only `/admin/source-state` twice. Five source
SELECTs total; at most64 network requests at1/second,120seconds total,5seconds/call,
16MiB source/admin responses and32MiB public responses. It never calls apply,
reconcile, refresh or a source write, and never advances the serving cursor.

For returned live records it checks exact handle/hydration and a source-backed
positive/exclusion hard-PDS pair, plus PDS preference/boost acknowledgements.
Actual preference rank movement remains covered by the controlled benchmark.
Guaranteed moderation absence uses current DID hide overrides or blocked domains
with no show override. It does not introduce another slur/expiry implementation;
other missing records have unverified visibility rather than a fabricated pass.

Tombstones do not retain their old handle. An optional `--known-handles` private
JSON object with at most16 independently retained DID-to-handle witnesses enables
public deletion/cleared-handle absence checks. Otherwise the receipt labels those
witnesses unavailable. Current live actors coexisting with retained tombstones are
counted separately, not mistaken for deleted actors. Stable hydration divergence
fails; selected source/document/policy changes make affected results inconclusive.
Source cursor/policy freshness is checked before and after without repairing it.
All receipts contain hashes/counts/checks, not profiles, private policy or tokens.
