# Invalid duplicate identity claims

2026-10-28. A bounded follow-up checked three DIDs retained in the handle-sidecar
results. All three PLC documents claim the same seven Trump-family handles and
`https://pds.trump.com`. `_atproto.donald.trump.com` TXT and `pds.trump.com` A
queries return NXDOMAIN; the handle HTTPS endpoint fails name resolution.
A single public Bluesky `getProfiles` batch returns no profiles for the three.
See [the observation record](identity-sample-20261028.json).

These are currently invalid handle associations. The repeated aliases, absent
hosting endpoint and absent appview profiles strongly suggest fabricated identity
spam. The observations do not establish who created the DIDs or their historical
activity. They also do not establish the total affected corpus count. The
92/638 systematic-sample frequency is a warning, not a measured network-wide rate.

[AT Protocol requires bidirectional handle verification](https://atproto.com/specs/handle#handle-resolution).
A DID's `alsoKnownAs` is a claim; publishing someone else's domain there does not
establish control of that domain.

The repository's `scripts/plc-identity-sync.py` takes the first `alsoKnownAs`
entry and writes it into `actors.handle` without resolving the handle back to
the DID. This is a concrete admission gap consistent with the observed data;
we have not proven which historical write populated these particular rows.
The live identity-event resolver performs verification, but that does not verify
all rows previously filled by the bulk path. The July 31 revert `9b504fd` removed
a reconciliation path whose implementation explicitly distinguished claims from
verified handles.

Thus the snapshot contains non-user identity claims that consume actor/profile
records and candidate/index storage. The exact excess storage and actor count
remain unmeasured. The experiment's “valid” handle filter meant syntactically
accepted ASCII handles, not verified ownership; its counts must not be read as
verified-account coverage.

A repair should first prevent unverified bulk claims becoming searchable handles,
then measure affected rows offline and revalidate ownership before changing them.
Blindly deleting every duplicate DID would conflate bad handle associations with
account validity. No production changes, database audit or corpus transfer were
performed in this follow-up.
