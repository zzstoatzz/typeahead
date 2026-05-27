# i18n: Japanese / CJK search — design plan

Status: **proposal** (not yet built). Supersedes the "non-Latin dropped"
KNOWN LIMITATION in `services/src/index/normalize.zig`.

## Why

A large share of the atmosphere is Japanese (bsky alone skews heavily JP).
Today `normalize.zig` folds only ASCII + Latin-1 + Latin Extended-A and
**drops everything else** — so a display name `佐藤太郎` produces the empty
string, emits no `n:` keys, and `q=日本` normalizes to `""` → `{"actors":[]}`.
That's a whole-population blind spot. We need to index Japanese (Han +
kana) — and structure it to extend to other CJK/scripts later.

## Reality check (so we set the right bar)

bsky's own search (Palomar, Elasticsearch) has **open, unresolved CJK
quality issues** — uni-gram-ish default tokenization
([indigo#302](https://github.com/bluesky-social/indigo/issues/302)) and
Japanese over-normalization
([indigo#628](https://github.com/bluesky-social/indigo/issues/628)). So the
target is **"good, bounded Japanese typeahead at parity-or-better with bsky
+ native-speaker spot-checks,"** not morphological perfection.

## Approach: NFKC-lite normalization → CJK bigrams (dependency-free)

We cannot ship a morphological segmenter (MeCab/Kuromoji + dictionaries) in
the Zig binary. The industry-standard lightweight substitute is **bigram
tokenization** of CJK runs, after width/kana normalization
([OpenSearch cjk_bigram](https://docs.opensearch.org/latest/analyzers/token-filters/cjk-bigram/),
[Elastic CJKWidthCharFilter](https://www.elastic.co/docs/reference/elasticsearch/plugins/analysis-kuromoji)).
It over-matches slightly vs. true word segmentation, but our top-N cap +
downstream scoring absorb that — and crucially, **CJK bigrams map onto the
multi-token `n:` intersection we already have** (`merge.zig`,
`index_search.zig`): a CJK query becomes a set of bigram keys we intersect,
exactly like multi-word Latin queries.

### Explicitly out of scope (v2)
- **Reading-based search** (typing `さとう` to find `佐藤`): needs furigana
  we don't store. Users search by the characters actually present.
- **Morphological word segmentation** (no dictionary).
- Non-Japanese scripts beyond CJK ideographs + kana (Arabic, Hebrew,
  Hangul, Thai…) — design leaves room, but Japanese-first.

## Pipeline changes

### Phase 0 — measure (DONE 2026-05-26)
Windowed sample of 56,000 non-empty `display_name`s across 8 rowid windows
(`/tmp/phase0.py`, kana `U+3040–U+30FF`+`U+FF65–U+FF9F`, Han
`U+4E00–U+9FFF`+`U+3400–U+4DBF`):

| metric | value |
|---|---|
| display names containing CJK | **7.33%** (~434k of 5.92M actors) |
| …kana-bearing (definitively Japanese) | 5.42% |
| …Han-only (JP-kanji or Chinese) | 1.91% |
| avg CJK codepoints / name | 4.1 → ~3 bigrams |
| **added build emissions** | **~1.7M tuples = +0.7%** vs ~244M today |
| served-artifact impact | negligible (distinct bigrams × top-N cap) |

**Conclusion: index all CJK names; NO quality-floor gating needed** — the
+0.7% emission delta is trivial (vs `d:`'s +69%), so there's nothing to
gate. Resolves decision #5.

### 1. Normalization — `normalize.zig` (`VERSION` bump → re-index)
Keep **all** existing Latin behavior. Add, before the fold table:
- **NFKC-lite width fold:** full-width ASCII `Ａ-Ｚ０-９` → ASCII; half-width
  katakana `ｶﾞ` → full-width `ガ`; full-width space → space. (CJKWidth-equivalent.)
- **Katakana → Hiragana fold** (one canonical kana form), so `サトウ` and
  `さとう` collide. (Standard JP equivalence.)
- **Preserve** Han + Hiragana codepoints in the normalized UTF-8 output
  (stop dropping them). Voiced-mark/combining handled by the width/NFKC step.

`VERSION` → e.g. `"v2-cjk-bigram"`. This is **load-bearing**: indexer and
serving must produce identical output; the snapshot loader already refuses
a snapshot whose `index_meta.normalize_version` ≠ the running binary's
`VERSION`, which is exactly the guard that prevents a half-migrated state.

### 2. Tokenization / key generation — `prefixes.zig` + `builder.zig`
Script-aware segmentation of the normalized string into runs:
- **Latin/digit runs:** unchanged (`h:`/`n:`/`e:` prefixes, `d:` suffix).
- **CJK runs (Han + kana, kana already folded to hiragana in step 1):**
  emit **bigram** keys into the `n:` namespace — `日本太郎` → `日本`, `本太`,
  `太郎` — **uniformly across Han and kana** (no per-script special-casing;
  katakana is *not* whole-tokened — that hurts partial typeahead). Plus the
  **leading unigram only** (`n:日`) so a 1-char in-progress query matches.
  **No full per-character unigram indexing** — it explodes broad keys and
  invites spammy matches.

Indexer (`builder.zig`) and query (`prefixes.queryKeys`) **must** call **one
shared script-aware tokenizer** — do not retrofit CJK into the existing
ASCII token-prefix loops. **All slicing must be codepoint-aware:** the
current prefix code is byte-oriented (`tok[0..i]`, `handle LIKE 'term%'`);
once CJK codepoints are preserved, any prefix/bigram boundary must fall on a
valid UTF-8 boundary or we corrupt keys. The bigram window advances by
codepoint, not byte.

### 3. Query path — `index_search.zig`
`normalize → queryKeys`: a CJK query yields its bigram set; intersect the
per-key top-N lists (the existing multi-token `n:` AND path in `merge.zig`).
`q=日本` → key `日本`; `q=佐藤太郎` → `{佐藤,藤太,太郎}` intersected. Single
CJK char → leading-unigram key (hard-capped). Mixed Latin+CJK → both.

### 4. Scoring — `scoring.zig`
Add CJK signals parallel to the Latin ones: exact normalized-name match,
leading-prefix bonus, all-bigrams-present (name_tokens-equivalent). Tune so
a full JP name beats a partial. Keep `quality_score` authority multiplier.

## Cost (the `d:`-style decoupling applies)
Bigram emissions ≈ Σ(len−1) over CJK names — JP display names are short
(~2–6 chars → ~1–5 bigrams each), so the **build-emission** bump scales with
the CJK-name count (Phase 0). The **served artifact** stays bounded by the
existing **top-N cap** — same decoupling the `d:` index showed (+69% build /
+0.9% serve). **Gating only if Phase 0 proves it necessary, and even then
narrowly:** do *not* globally exclude low-quality CJK names from the index
(that silently disappears real accounts). If emissions balloon, gate only
the **broad 1-char unigram keys** behind a quality floor — never the full
bigrams.

## Rollout (careful, reuses the promote pipeline)
1. Land normalize+prefixes+builder+scoring behind the `VERSION` bump.
2. Unit tests (below) green; build a snapshot in a scratch indexer.
3. **Add Japanese sentinel queries** to promote validation (known JP
   accounts that MUST return) alongside the existing manifest/sha256/schema
   checks — a JP-broken snapshot fails the gate, never attaches.
4. Deploy serving binary + indexer **together** (version gate enforces);
   atomic A/B attach swaps the new snapshot in; rollback = prior snapshot.
5. Re-run the smoke battery with a JP corpus, compare vs bsky.

## Testing
- `normalize.zig`: full-width fold, half-width katakana, katakana→hiragana,
  bigram emission, mixed script, idempotence, `VERSION` pin.
- `prefixes`/`queryKeys`: CJK bigram keys; single-char; mixed.
- E2E: JP accounts that currently return `[]` now return; parity vs bsky
  on real Japanese names; latency unchanged (still bounded lookups).

## Decisions (locked — Codex review 2026-05-26)
1. **Unigrams:** **leading-unigram-only** (`n:日`), for 1-char in-progress
   queries. No full per-character unigram indexing (explodes broad keys +
   spam).
2. **Katakana:** **fold katakana→hiragana, then bigram uniformly** with Han.
   No whole-token special-casing (hurts partial typeahead).
3. **Scope:** **Han + kana** in one `VERSION` bump (covers JP + much of ZH).
   Hangul / Arabic / Hebrew / Thai are later versions.
4. **Min CJK query length:** allow 1 char (typeahead needs it) but treat it
   as a **capped/broad prefix**; full quality expects ≥2 chars.
5. **Quality-floor gating:** **NONE.** Phase 0 showed +0.7% emissions —
   nothing to gate. Index all CJK names (bigrams + leading unigram).

Phase 0 is done; all decisions are locked. **Ready for implementation.**

## Hubble-informed guardrails (Codex)
Hubble is a whole-atmosphere sync/mirroring substrate, **not** a replacement
for our typeahead index — `hubble-sync` gives apps hooks for verified
commits / repo resyncs / repo+account status, but explicitly no search
indexes or record-set reconciliation. So CJK search stays our derived-index
responsibility, and Hubble does **not** require pausing it to rewrite sync.
What it reinforces as hard gates for this work:
- **Overlay/status semantics are non-negotiable.** Deletes, deactivations,
  moderation, renames must keep working — our overlay covers this; CJK must
  not regress it.
- **No request-time work proportional to corpus size.** Still bounded
  point-lookups; CJK adds keys, not scans.
- **No broad unbounded keys.** (Why leading-unigram-only + top-N caps.)
- **Treat as another derived-index version** — protected by
  `normalize_version` + promote sentinels, atomically attachable/rollback-able.
- **Measure emissions before building** — `d:` taught us build cost can
  surprise even when the served artifact stays bounded.
