# collection discovery: retiring the hardcoded allowlist

**Status:** design note, not built. Written 2026-08-18 after the
`birdlover.selfhosted.social` incident.

## the problem

`wantedCollection` (`services/src/ingest.zig`) decides which firehose collections
cause us to discover an actor. It is a hardcoded allowlist:

```zig
"app.bsky.actor.profile", "app.bsky.feed.post", "app.bsky.feed.like",
"app.bsky.graph.follow", "sh.tangled.actor.profile"   + "site.standard.*", "place.birds.*"
```

Two consequences, both structural rather than incidental:

1. **Onboarding an app is a code change and a deploy.** Until someone notices and
   ships, that app's users are discoverable only via `#identity`.
2. **`#identity` becomes a single point of failure for those users.** That is
   exactly how birdlover went missing: a birds.place account writes nothing but
   `place.birds.*`, so the commit path was blind to it, and the one `#identity`
   event it ever produced was dropped by a resolver failure. Two independent
   gates, both closed, one actor invisible with no signal anywhere.

The allowlist is not wrong about what it lets in. It is wrong as a *mechanism*:
it fails closed, silently, for every atproto app that isn't bsky or tangled — and
the set of such apps is the whole point of the network.

## the idea: published lexicons as the signal

Ingest activity from any collection whose **NSID authority has a published
lexicon**. The resolution path already exists in the spec, and this repo already
sits on the publishing side of it (`scripts/publish-lexicon.py`):

```
collection NSID          place.birds.sighting
  → authority             birds.place              (NSID domain authority, reversed)
  → DNS TXT               _lexicon.birds.place  →  did=did:plc:...
  → that repo's record    com.atproto.lexicon.schema, rkey = the NSID
```

If that record exists, someone deliberately published a schema for the thing
they're writing. That is a meaningful filter:

- It costs a legitimate app nothing — publishing lexicons is already the norm for
  anyone who wants their NSIDs to resolve.
- It costs a spammer a domain, a DNS record, a DID, and a published schema, all
  of which are attributable and revocable.
- It fails **open** for new apps: build an app, publish your lexicon, get
  discovered. No deploy on our side.

## the honest objection

The user's own framing, and it's right: **a spammer can publish lexicons first.**
Nothing stops a determined actor from doing the setup work to clear the bar. So
this is not a spam filter — it is a *cost floor* and an attribution hook. Its
real value is:

- it converts "unknown collection" from a silent drop into a cheap lookup,
- it gives us a stable key to act on when abuse does appear (the authority
  domain/DID), so a bad app is one domain-block rather than a lexical whack-a-mole,
- it moves the default from deny-unknown to allow-attributable.

Expect to still need reputation on top. The point is to stop the allowlist from
deciding who exists.

## constraints this must respect

- **The serving invariant.** Resolution must never run per-event on the ingest
  path. The firehose is ~4–5 discovered actors/sec against a much larger event
  rate; a DNS + repo fetch per commit is not viable.
- **Therefore: cache by authority, not by NSID.** Authorities are few and change
  rarely. An in-process map of `authority → {published, checked_at}` with a
  negative-cache TTL keeps the hot path to a hash lookup. Unknown authority →
  admit the actor optimistically *or* queue the authority for out-of-band
  resolution; do not block the reader.
- **Failure must fail open, and loudly.** The whole lesson of this incident is
  that a lookup failure silently deciding "not interesting" is how actors
  disappear. A resolution failure should still record the DID — same rule the
  identity resolver now follows.
- **Keep a deny-list.** Allow-by-default plus a deny-list for known-spam
  collections is easier to operate than allow-by-enumeration, and it fails in the
  direction we can see.

## open questions

- Do we admit the actor on first sight of an unresolved authority and reconcile
  later, or hold it? Holding reintroduces the failure mode we just fixed.
- Where does the authority cache live — in-process per ingester, or a shared KV
  the worker can also read for `/stats` attribution?
- Is there a cheaper bulk source for "authorities with published lexicons" than
  resolving them one at a time? (Same shape as the PLC bulk-identity problem;
  see `docs/discovery-and-identity.md`.)
- What does this do to corpus size? Admitting every actor of every published-lexicon
  app is strictly more than today. Needs a measured estimate before it ships,
  per the serving invariant.

## until then

`wantedCollection` stays hardcoded and carries a TODO pointing here. Every entry
added by hand is a reminder that the mechanism is the bug, not the list.
