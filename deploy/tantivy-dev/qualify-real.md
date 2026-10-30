# Real-corpus correctness properties

`load.py` measures bounded traffic and checks response shape, exact-handle order,
uniqueness and scope acknowledgements. Those checks do not prove general lexical
correctness, hard-PDS membership or top-K recall. `qualify-real.py` adds a separate
small assertion pass after full import and replay readiness.

Run **on a Fly Linux VM**, targeting only the isolated dev hostname or the dev
VM's own loopback listener. Existing cloud-only Turso environment values enable
small primary-key lookups; do not copy credentials or corpus payloads home.

```sh
python3 /tmp/tantivy-dev-qualify-real.py --url https://typeahead-tantivy-dev.fly.dev --source-lookups
```

On the dev VM itself, `--url http://127.0.0.1:8080` is also accepted. The source
option requires existing `TURSO_URL` and `TURSO_AUTH_TOKEN`; it sends only bounded
`SELECT ... FROM actors WHERE did IN (...)` calls, at most100DIDs per query and
12source queries total. It performs no source writes, administrator mutations,
production requests or full-corpus scan. Without `--source-lookups`, the report
explicitly says `source_verified:false`; that mode does not establish hard-PDS
membership.

Bounds:180seconds,96total HTTP requests, at most10requests/second,512KiB per dev
response,4MiB per source response and16MiB total responses. Redirects are rejected.
Output contains assertion results, counts, response hashes and selected generation
metadata; it never emits raw profile arrays, source rows or credentials.

Assertions include:

- Historical Justin Fagnani witness DID `did:plc:ec64xv7n5dszeizw56yr6b5h` at
  exact handle `justinfagnani.com`, and its presence for `Justin Fa`, reversed
  words and the full name. These are explicit regression expectations, not a
  complete top-K oracle.
- Equivalent ASCII case/word-order results only within an unchanged committed
  cursor/watermark window, with at most3attempts. Persistent live mutations yield
  an inconclusive result, not a guessed failure.
- Independent returned-result lexical soundness for ASCII or pure-Han text.
  The predicate deliberately abstains on other Unicode folds; it does not reuse
  the backend planner or guess a second general normalizer.
- Optional source-backed visibility, hard-PDS membership and returned-row ranking
  using actual f32 scores/DID ties. A source-covered returned actor with a canonical PDS is additionally queried
  by exact handle globally, inside that PDS and outside it. Selection prefers
  already observed hard-scope results and tries at most3 candidates. Global exact
  priority must identify that actor before its scope assertions are admitted;
  the source row is rechecked afterward within the same12-SELECT total budget. Source records newer than the captured
  backend watermark are inconclusive instead of falsely accusing retrieval.
- Complete/fresh dev metadata before and after the pass and unchanged build
  identity. Partial imports cannot be substituted.

Justin's global regression remains mandatory whenever his current source record is
covered and searchable. His PDS is not assumed known: the receipt explicitly
reports `historical_witness_pds_state` as missing/invalid/canonical/unavailable.
A missing Justin PDS does not fail unrelated, source-backed PDS assertions.
A stable positive **and** negative real-PDS pair is still required; absence of
any eligible witness is inconclusive, never a PDS pass. The receipt preserves
generation-wide missing/invalid/unknown/reason counters and adds observed-source
PDS counts; none of these checks claims complete scope coverage for unknown rows.

Historical witness deletion, moderation, handle changes or uncovered source
versions are classified separately when source lookups are enabled. Any failed
assertion or inconclusive required window/coverage exits nonzero; inspect the
receipt before deciding whether the cause is retrieval, source drift or readiness.
The helper does not claim a full top-K oracle, complete absence proof, general
Unicode qualification, update/restart correctness or service latency qualification.
Existing synthetic adversarial/restart/atomicity suites remain required.

Local pure-predicate self-test (zero network requests):

```sh
python3 deploy/tantivy-dev/qualify-real.py --self-test
```

The self-test passes and normal invocation outside Fly fails before networking.
The parent owns upload/invocation and retention of its small cloud result receipt;
this task did not run cloud operations or change the backend/bootstrap.

The conservative lexical self-test also covers Unicode handles whose folding
cannot be independently proved, ASCII controls that disappear between Han
characters, and the unsupported I-Ching range between supported Han blocks.
Those uncertain cases abstain instead of falsely accusing correct retrieval.

`pds_pair_status` separates a failed retrieval assertion from missing eligible
metadata, a nonstable dev view or a changed source record. The positive/exclusion
pair must pass on the same verified actor; unrelated successful scope rows do not
substitute for that pair. Missing PDS on the historical Justin witness remains a
reported source gap rather than an automatic failure of another valid PDS pair.
