# Canonical moderation differential

`moderation.rs` embeds the automatically generated canonical Worker regex artifact
through the build script. The artifact fingerprints all of `src/moderation.ts`;
its seven regex sources, trusted labeler and hide values come from actual Bun
exports, with no manual transcription. `TA_MODERATION_RULES_PATH` selects its
cloud build-context copy. Default is `../../src/moderation-regexes.json`.
Existing identity-quarantine embedding remains separate.

```sh
python3 services/tantivy/tests/moderation/run.py
```

This runs the artifact drift check, regenerates expected results through the
actual Bun `shouldHide`, `hasExplicitSlur`, `isDomainBlocked` and `resolveHidden`,
then executes the real Rust evaluator in release mode. Each subprocess has a
360-second bound. It uses no credentials, cloud or source database. Results bind
fixture, runtime source and canonical Worker fingerprints.

Rust compiles the original case-sensitive Unicode character classes with ASCII
word boundaries (`(?-u:\b)`), matching JavaScript's unflagged `\b`. Both raw and
`.`/`-`/`_`-stripped handles are checked. Domain matching uses a sorted canonical
set and handle suffix lookups; it is equivalent to apex/subdomain matching without
a linear scan across all 10,000 domains for every candidate. The differential
includes Unicode surrounds, combining marks, astral characters, leetspeak,
separators, ASCII case, Greek/Turkish lowercase, lookalike domains and empty domains.

A complete schema-1 policy requires source observation time, all overrides and
blocked domains, with at most 10,000 of each. Unknown action, duplicate DID,
incomplete data and exceeded limits are errors. A validated policy computes
hidden from labels/slurs/domain/override, ignoring old `Actor.hidden`. For
supported labels, per-DID `show` is the final word, including slur/domain hides.
Deletion, invalid identity and query matching remain separate eligibility rules.

The evaluator preserves JavaScript truthiness for `neg` and `exp`, source/value
checks, expiry-before-source-check evaluation, null-label errors and early return
on the first active hiding label. Expiry compares milliseconds with `<= now`. Serving must call `hidden_at_ms`
with an actual millisecond clock; the requested `hidden(..., now_epoch)` API is
an explicit whole-second wrapper. Fractional-current-time cases exercise exact
expiry boundaries without up to one second of extra hiding.
It accepts numeric milliseconds, booleans, four-digit-year ISO dates at UTC
midnight, and RFC3339 datetimes with an explicit offset. Fractions truncate to
milliseconds, including before epoch. Empty/falsy expiry skips date parsing.

JavaScript Date also accepts legacy strings, normalized invalid calendar days,
24:00, offset-without-colon and other coercions. These are **not silently
approximated**: unsupported formats produce an error before visibility can be
shown, even if a later DID override could have shown that actor in JavaScript.
This is a declared fail-closed limitation rather than a parity claim. Invalid
strings/objects for which JavaScript yields NaN are also conservatively rejected.
The fixture marks unsupported cases explicitly; successful tests mean exact
supported behavior plus demonstrated rejection of the qualified cases.

`expiry_coverage(labels)` returns `nonnegative_labels`, `candidate_hide_labels`,
`without_expiry`, `supported_expiry` and `unsupported_expiry`. Expiry categories
cover all nonnegative labels, including foreign labelers, because JavaScript
parses date values before checking the source. It scans the entire array rather
than stopping at an earlier hide, giving conservative coverage. Malformed null
labels are errors. Aggregate these counts during a cloud-local source/import or
reconciliation scan, with a separate malformed-label count, before claiming source
compatibility. No raw expiry values need to be logged. The synthetic receipt
establishes no source-wide coverage and does not authorize production cutover.
