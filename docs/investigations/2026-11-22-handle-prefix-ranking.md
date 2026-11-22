# Handle-prefix ranking and domain matches

Investigated November 22, 2026, against the serving slot (binary
`05cf63a493789339a8dea1cbe8ce76db7d5f80dd54596a0d296ba20b0717ace8`),
Bluesky's public AppView and Tern at `typeahead.cove.town`. Read-only: live
`/debug/search` candidates reranked offline, no slot change.

## What was wrong

For a one-word query the engine took any account with a matching key and
ordered them by quality score alone. Two weak kinds of match counted the same
as a handle that starts with the query:

- a segment after a dot in the handle: `s` matched every `.bsky.social` handle
  through `social`, `c` every `.com`, `a` every `.app`, `u` every `.us`, so
  the overall top-scored account won those letters;
- a later word of the display name (`h` → Chris *Hayes*).

The first result's handle started with the letter for 11 of 26 letters,
against 19 for Bluesky and 24 for Tern.

## What Tern does

Tern (`tangled.org/jola.dev/tern`, `crates/search/src/reader.rs`) ranks by
`100·log2(1 + followers) + 10·log2(1 + age in days)`, adds 150 when the
handle starts with the query and 500 more when its first label equals the
query, puts an exact handle first, and matches a domain segment only when
the query contains a dot.

## The change

- A domain segment is a match only when the query contains a dot.
- A handle that starts with a one-word query gets +50 on the quality score.
  Tern's +150 on its log2 scale is about +45 on ours.

## Simulation

71 one-word queries: the 26 letters, `RANKING_CORPUS` and the one-word
entries of `FULL_CORPUS` in `scripts/bench.py`. Each query's top 100
candidates from `/debug/search` were reranked by the rule above with the
bonus shown, then compared with Bluesky's and Tern's top 10.

| bonus | letters: #1 handle starts with it | letters: #1 = Tern | all: #1 = Bluesky | all: #1 = Tern | all: Bluesky's #1 in our top 8 |
| --- | ---: | ---: | ---: | ---: | ---: |
| current engine | 11 | 11 | 33 | 29 | 52 |
| 0 (dot rule only) | 13 | 11 | 32 | 29 | 51 |
| 25 | 21 | 15 | 34 | 35 | 52 |
| **50** | **24** | **17** | **35** | **38** | **53** |
| 100 | 26 | 16 | 32 | 40 | 50 |
| 150 | 26 | 16 | 33 | 38 | 47 |
| 250 | 26 | 16 | 31 | 36 | 43 |
| strict tier | 26 | 16 | 29 | 34 | 41 |

At 50 every measure improves or holds. Larger bonuses lift small accounts
over the one Bluesky and Tern both put first for longer queries. Agreement
with Bluesky on single letters stays at 9: Bluesky also matches name
initials (`aoc.bsky.social` for `a` and `o`).
