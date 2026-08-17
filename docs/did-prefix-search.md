# DID-prefix search (`i:` key family)

Status: **design — not yet implemented**. Phase 0 (emission measurement) is
the first work item. This document is the design record and roadmap for
making DIDs first-class typeahead input.

## Why

`q=did:plc:xbtmt2zjwlrfegqvch7fboei` should find that actor — and, because
this product is a *typeahead*, so should `q=did:plc:xbtm`. DIDs are the
stable identifier in atproto; handles change, DIDs don't. Tooling authors,
moderators, and anyone holding a DID from a log line or an API response
currently has no way to turn it into a profile through us.

### History: the reverted exact-lookup (2026-08-16/17)

A first attempt (`e570c82`, reverted in `f93fbbf`) shipped exact full-DID
lookup only: a complete DID short-circuited to `hydrate()`, partial DIDs
fell through to the prefix pipeline and matched nothing. Architecturally
sound (bounded, moderation-safe, no Turso on the hot path) but it was a
*lookup* bolted onto a *typeahead* — every keystroke before the last one
said "no results". Reverted in full; this design supersedes it. The
exact-lookup behavior falls out of the general design below as a free
special case, so nothing from the revert needs resurrecting.

## Approach: a fifth key namespace, `i:`

Alongside `h:` (handle prefix), `n:` (display token), `e:` (exact handle),
`d:` (domain suffix): emit `i:plc:<prefix>` / `i:web:<prefix>` keys over the
**method-specific id** (the part after `did:plc:` / `did:web:`). Everything
downstream — base ∪ overlay merge, top-N caps, hydrate, tombstones, the
live-actors safety belt, `/debug/search` tracing — is unchanged shared
machinery. DID search inherits ranking and moderation for free.

### The load-bearing insight: DID ids are random base32

Handle prefixes collide heavily (`h:nate` covers thousands of actors), which
is why emitting all 16 lengths per handle is affordable. DID ids don't
collide: at prefix length 4 (~1M possible keys over ~8M actors) each key
covers ~6-8 actors; by length 5 they're nearly unique. Emitting all lengths
like `h:` does would roughly **double `prefix_top_base`** (~77.7M rows at
the 5.9M-actor measurement) with near-worthless singleton keys.

So:

- **Emit lengths 1..L only** (L=5, locked below).
- **Queries longer than L truncate** to the `i:` key for lookup, then
  **post-filter** merged candidates by `startsWith(full queried prefix)`
  before hydrate. Candidate lists are top-N-capped, so the filter runs over
  ≤300 in-memory entries — bounded, invariant-safe.
- A fully pasted DID converges through the same path to exactly one
  candidate. No separate exact-lookup code path.
- Short prefixes (`did:plc:x`) return the top-N-by-`quality_score` actors
  under that prefix — the same typeahead semantics as every other family.

### Query detection

Detection happens on the **pre-normalize** term (`normalize.query()` would
mangle the colons): a term matching `did:(plc|web):<nonempty>` takes the
`i:` path; anything shorter (`did:`, `did:pl`) falls through as an ordinary
handle query. The did-shaped branch and the key derivation live in
`prefixes.zig` so builder and query cannot disagree — same rule the CJK
tokenizer follows.

### Overlay (`sync.zig`)

The overlay derive call gains the DID, so newly indexed actors are
DID-prefix-searchable within minutes of arrival and tombstones suppress
`i:` keys like every other family. DIDs never change, so renames produce no
`i:` diffs — the rename set-arithmetic is untouched.

## Sizing (estimates — Phase 0 must confirm before build code lands)

Per the standing guardrail: **measure emissions before building** (the `d:`
family's +69% build / +0.9% serve taught us build cost and served cost
decouple, in both directions).

Estimated at L=5, ~8M actors, assuming uniform base32 ids:

```
emissions:        +5/actor  → ~+40M tuples  (~+20% vs ~200M)   [d: was +69%]
distinct i: keys: ~32 + 1k + 33k + ~1M + ~7M ≈ ~8M  (~+10% of prefix rows)
served bytes:     ~8M keys × ~41 B (e:-sized rows) ≈ ~330 MB   (~+4% artifact)
```

Phase 0 = confirm the key-count math against a real sample (a local
snapshot or the next build's emission logs) and record actuals here.

## Version gate (the CJK playbook applies exactly)

An old snapshot has no `i:` keys. A new binary serving an old snapshot
would answer DID queries **silently partially** — only overlay-recent
actors — which is precisely the failure mode this system is built to
refuse (see `docs/i18n-cjk-search.md`, "load-bearing").

- Bump the coupling version to `v3-did-prefix`; the promotion gate already
  refuses a snapshot whose version ≠ the running binary's.
- Add a **DID sentinel probe** to promote validation: a known DID prefix
  must resolve or the snapshot never attaches.
- Rollout order is forced by the coupling: land code → run the
  `typeahead-index` Prefect deployment on heavypad to produce a v3
  snapshot → `just deploy-search` (promotes v3 through the gate) →
  `just deploy-worker`.

## Worker

- **Kill switch, no redeploy**: `DID_SEARCH_DISABLED` secret
  (`bunx wrangler secret put|delete`), same pattern as
  `EMERGENCY_PROXY_BSKY`. When set, did-shaped terms return `{actors:[]}`
  before reaching the backend. Effective within the 60s edge-cache window.
- **Turso fallback** (replica miss/outage only): range predicate on the
  `did` primary key — `did >= ?1 AND did < ?1 || '~'` with a LIMIT — not
  `LIKE`. EXPLAIN-verified live in smoke, given the bound-param
  plan-degradation history (`docs/turso-research.md`).
- Smoke asserts the **enabled** behavior so a deploy with the flag
  accidentally set fails the gate instead of shipping the feature dark.

## Roadmap

1. **Phase 0 — measure.** Confirm emission/key-count math on a real
   sample; record actuals above. Cheap; no build code.
2. **Engine.** `prefixes.zig` (`i:` derivation + query detection),
   `index_search.zig` (truncate + post-filter), `builder.zig` (emission),
   `sync.zig` (overlay), version bump, promote sentinel. Unit tests
   mirroring the CJK set: builder/query agreement, per-length findability,
   post-filter correctness at and beyond L.
3. **Edge.** Worker gate + kill switch + Turso fallback; smoke checks that
   type a known DID keystroke-by-keystroke and assert the actor appears at
   every length ≥ the first `i:`-eligible one; moderation suppression via
   a hidden fixture.
4. **Rollout.** The coupled sequence under "Version gate". Then
   `just bench-search` + `just bench-ranking` (expectation: DID queries at
   or below `h:` latency — fewer keys, no intersection), `just index-status`
   to confirm the v3 `build_id` serving.
5. **Docs sync** (same commit as the edge work): lexicon `q` description,
   `/docs`, `/llms.txt` (`src/pages/llms.ts`), README, homepage placeholder
   — and republish the lexicon record via `scripts/publish-lexicon.py`
   after deploy.

## Decisions (locked)

1. **L=5.** Length 5 is where base32 prefixes go ~unique; deeper emission
   buys nothing the post-filter doesn't already provide. Revisit only if
   Phase 0 contradicts the collision math.
2. **Key text carries the method** (`i:plc:…`, `i:web:…`) — plc and web id
   spaces must not collide. Other DID methods: not indexed until one has
   real presence in the corpus.
3. **Detection threshold**: the `i:` path requires a complete
   `did:plc:` / `did:web:` prefix plus ≥1 id char. `did:p` etc. fall
   through as handle queries (they match the rare handle starting "did"
   and otherwise nothing — correct).
4. **Ranking**: `quality_score`, like every other family. No lexicographic
   special case — a DID-prefix browse should surface notable actors first.
5. **Exact full-DID lookup is not a separate path.** It's the L-truncated
   lookup whose post-filter leaves one candidate.

## Open questions

1. **`did:web:` normalization** — id part is a domain (possibly
   port/path-encoded with `%3A`/`:`); decide lowercase/escape handling in
   Phase 2 alongside the derivation code. plc ids are already lowercase
   base32; index as-is.
2. **Should the home page hint the mode?** (e.g. a subtle "searching by
   DID" affordance once the term goes did-shaped.) UI polish, not blocking;
   decide at rollout.
3. **Sentinel DID choice** for the promote gate — needs to be an actor
   essentially guaranteed to exist in every future snapshot (the service
   account `did:plc:y6kywgnb7xletjpa5hhxmd4k` is a natural candidate).
