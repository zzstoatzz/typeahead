# Multiword display-name recall incident

2026-10-28. Report: https://bsky.app/profile/chrisshank.com/post/3myxsd5223s25 .
Read the discussion and both screenshots. The reported query is `Justin Fa`;
the user's screenshot uses the distinct single-token query `justinfa`.

Both public aliases and the serving backend return no actors for `Justin Fa`.
The retained rollback backend independently returns the same empty answer.
Both backends find Justin Fagnani for `Justin Fag`, `Justin Fagnani`, `fagnani`
and the handle prefix `justinfa`. This bypasses the October 28 Worker filter
and shows a backend candidate-recall defect, rather than an outage or the
identity quarantine removing this actor. No backend or routing cutover occurred.

The live trace intersects n:justin (200 base / 58 overlay) and n:fa (300 base /
1,231 overlay), with zero merged survivors. Narrowing to n:fag produces Justin
Fagnani. The builder truncates each token's base posting independently before
serving intersects the lists. This can lose a valid multiword match even when
both words exist in its stored display name. Following the moved files places the cap defaults in May 24 builder commit
`786eb0e` and capped-intersection serving in May 25 commit `fa2e37f`; the October 27 performance changes retain
this query/intersection behavior. See serving.json and rollback.json.

No production patch has been applied for this defect. A safe fix must preserve
bounded request work, moderation and PDS semantics; lifting the per-key cap or
scanning the actor corpus on empty queries would violate the serving contract.
The performance candidate failed mixed-load qualification and was not promoted.

The [isolated scope investigation](../../evals/display-name-recall-20261028/README.md)
now reproduces additional failure classes, replay/rebuild recall changes and
records limits of the candidate fixes. No production fix has been implemented.
