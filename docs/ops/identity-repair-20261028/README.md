# Invalid identity containment and repair

2026-10-28. Admission and response containment deployed; existing data repair pending. User authorized live admission fixes,
containment and safe data repair, followed by an exclusion count on /stats.

Worker admission version `ddf8866f-e0e9-48da-8cf1-0cfb2ae0979c`;
public-docs follow-up version `15ea44b7-e1df-464e-a728-713bdf516b4a`; ingester image
`sha256:d3f6eed3f06b6e682f110c1526bddfadf7fa05085d7bca86e5f6c648ff14a884`.
Both public aliases now return no actors for `donald.trump`, while exact
`zzstoatzz.io` still returns the correct actor. The PLC schedule is inactive.
The local bulk-script guard is not yet published to upstream.

Admission fixes cover raw relay hints, authenticated ingest verification markers,
manual indexing's forward/reverse checks, and disabling PLC claim handle writes.
Response containment covers both aliases, fresh backend replies and existing edge
cache entries, plus profile APIs. Seven Trump-family handles were checked: both
host A and handle TXT returned NXDOMAIN. `handle.invalid` is excluded too.

Stats will say “excluded from search” and use a dated offline union count of
moderation-hidden and identity-excluded actors. Prior label-observation counts
will not be mislabeled as current exclusions. Until the audit lands, the card
explicitly says “Audit pending”. The serving request and cron paths do not count
the corpus.

Bulk classification must run on a newly owned cloud Machine and independent
inactive-slot disk. Owner: identity-repair-20261028. Scratch /data/identity-repair;
20 GiB volume, 16 GiB payload, 16 MiB local evidence. No corpus travels through
heavypad or the laptop. Preserve bounded receipts/manifests, retire full payloads,
and delete owned Machine and disk on success, failure or cancellation.

The production database may receive only paced, conditional repair deltas after
admission/containment and an independent index/overlay capacity check. Never run
a domain-block backfill or unrestricted mass UPDATE on the live writer.

The user subsequently prioritized the existing performance issue and a qualified
cutover on the reduced backend configuration. Bulk repair and the measured
stats count remain pending; no repair writes or trial resource were launched.

Validation: 107 Worker tests pass; typecheck and changed-file lint pass. The
repository-wide lint command also visits pre-existing generated draft assets
and unrelated transport fixtures, which currently fail. Real workerd containment
and identity-verification fixtures passed, native tests passed with six platform
skips, and both deployment smoke runs passed after updating the obsolete stats
label assertion. Desktop and phone stats layout were visually checked.
