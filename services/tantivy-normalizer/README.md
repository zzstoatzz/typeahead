# Native normalization adapter for the Tantivy dev backend

This isolated adapter imports the existing Zig modules directly:

- `src/index/normalize.zig`: `v2-cjk-bigram` Latin fold, punctuation, width/kana
  handling and normalized-output limit.
- `src/search.zig`: the native512-byte query sanitizer.
- `src/index/pds.zig`: strict PDS host normalization and validation.

There is no independent Rust normalization implementation or experimental
NFKC fallback. `normalize_version` is `v2-cjk-bigram`; the distinct
`plan_version` is `complete-prefix-groups-v1` because retrieval fixes defects in
legacy capped key generation. Both versions must be recorded in the built
index metadata and checked by the serving reader.

## Build and ABI

Requires Zig0.17.0. From the repository root:

```sh
python3 services/tantivy-normalizer/build.py
python3 services/tantivy-normalizer/build.py --dynamic
python3 services/tantivy-normalizer/build.py --target x86_64-linux-gnu
```

The static output is `libtypeahead_normalizer.a`; cross-builds overwrite this
path, so build in a target-specific/container workspace. The header is
[`adapter.h`](adapter.h). `ta_plan` takes one UTF-8 JSON request and returns an
owned UTF-8 JSON reply. ABI status0 means a reply exists; check the JSON `status`
separately. Nonzero ABI status means malformed/oversized JSON or allocation
failure. The caller must free a successful result with `ta_result_free`, after
copying/deserializing its bytes. Input buffers are not retained. Each call owns
an independent arena; callers can use separate results concurrently.

Operations:

```json
{"op":"metadata"}
{"op":"actor","handle":"Example.test","display":"Éric 日本太郎","score":0.1}
{"op":"query","q":"Éric 日本太"}
{"op":"matches","handle":"Example.test","display":"Éric 日本太郎","q":"Éric 日本太"}
{"op":"pds","pds":"HTTPS://ACME.com./"}
```

Actor replies include `normalized_handle`, `normalized_display`, sorted unique
`keys`, `display_runs`, and optional `score_f32`/`score_bits`. Score input is
rounded with native f64-to-f32 conversion; reject nonfinite inputs or values
that overflow f32. `score_bits` is authoritative for reconstructing the f32.
Numeric ranking must treat signed zeros as equal and use DID ties when two
f64 inputs collapse to the same f32. This adapter does not validate ownership or
ATProto handle identity; identity admission remains the importer's responsibility.

PDS replies contain the lowercase host or `invalid_input`. Canonical validation
rejects credentials, ports, paths, wildcards and malformed labels. An empty
PDS from an actor should remain absent metadata; do not turn it into an enabled
scope or confuse it with a requested invalid PDS filter.

## Query plan

A successful query reply contains:

```json
{
  "status":"ok",
  "normalize_version":"v2-cjk-bigram",
  "plan_version":"complete-prefix-groups-v1",
  "normalized":"eric 日本太",
  "mode":"and",
  "display_mode":"all",
  "keys":["n:eric","n:日本","n:本太"],
  "groups":[
    {"kind":"latin","text":"eric","keys":["n:eric"]},
    {"kind":"cjk","text":"日本太","keys":["n:日本","n:本太"]}
  ],
  "exact":null,
  "handle_key":null,
  "domain_key":null,
  "needs_verification":true
}
```

All keys inside each group are conjunctive; all groups inside the display
branch are conjunctive. With `mode:"or"` (one normalized whitespace token),
the overall expression is `handle_key OR domain_key OR display_branch`.
`exact` is separate for exact-handle priority. With `mode:"and"`, only the
display branch is used. `keys` is a compatibility flattening of display keys;
blindly OR-ing those keys is incorrect for CJK and mixed scripts.

The actor emits every UTF-8-safe handle and domain-suffix prefix, full exact
handle, every complete Latin-run prefix and all display words/script runs.
There is no16-byte prefix cut or eight-token truncation. The query emits each
full Latin constraint rather than a shortened prefix.

For CJK, actor keys stay linear: adjacent bigrams plus the leading unigram for
one-character typeahead. A64-codepoint CJK name emits64 keys, not2,016 substring
keys. There is no legacy24-codepoint run truncation. Queries of two or more
CJK codepoints match a contiguous substring inside one normalized CJK run;
one-character queries match only a leading unigram. Katakana/Hiragana and width
folding come from the canonical normalizer; half-width kana retain its known
v2 limitations.

A query with three or more CJK codepoints needs contiguous verification, because
all of its bigrams could exist across unrelated name runs. `op:"matches"`
returns `{status:"ok",matches:true|false}` using the same grouped semantics.
It normalizes text without generating all actor keys. Run this predicate on the
candidate from the same captured index view **before** counting it toward top-K.
Stream/refill until K eligible verified candidates are known; never verify only
an arbitrary preselected50. Reaching the backend's verification/work budget is
an explicit budget error, not proof that the query has no matches.

Mixed Latin/CJK strings require all script groups; they are not phrase queries
across script boundaries. Different whitespace words remain order independent.

## Explicit outcomes and bounds

- `empty`: punctuation/emoji/whitespace produces no normalized terms.
- `unsupported_query`: known unsupported scripts are present (Greek, Cyrillic,
  Hebrew, Arabic, Thai or Hangul). This prevents silent dropping from broadening
  such mixed queries. These scripts are not claimed supported by v2.
- `budget_exceeded`: raw query >512bytes; normalized text would exceed256bytes;
  >64 script groups/prepared display keys; actor raw field >1,024bytes; or
  actor key-emission budget >8,192. No truncated answer is returned.
- `invalid_input`: invalid UTF-8, invalid PDS or invalid score/request fields.

The canonical normalizer itself truncates at its cap; the adapter detects
remaining meaningful normalized content and returns a budget error instead.
Exactly256ASCII bytes are accepted;257 meaningful bytes fail. Longer raw strings
that fold below the cap are accepted. The canonical CJK byte-boundary slack can
produce more than256 bytes; this adapter rejects that overflow explicitly.
These input-policy differences are part of `plan_version`.

## Verification

```sh
python3 services/tantivy-normalizer/build.py --test
python3 services/tantivy-normalizer/build.py --dynamic
python3 services/tantivy-normalizer/test_adapter.py -v
zig test services/src/index/normalize.zig -O ReleaseSafe
zig test services/src/index/pds.zig -O ReleaseSafe
zig test services/src/search.zig -O ReleaseSafe
```

The native and actual C-ABI tests cover canonical Latin/width/kana behavior,
sanitation and scopes, long prefixes, ninth-word failures, exact CJK contiguous
matching, cross-run false candidates, mixed-script grouping,64-codepoint CJK
emission, explicit bounds, f32 ties/nonfinite rejection and result lifetimes.
No serving or production resources are created by these tests.
