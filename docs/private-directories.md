# private directories: typeahead stays the public-context service

Status: **decided 2026-09-18.** Typeahead does not track private identities
and does not do credentialed lookups on an organization's behalf. Completion
over a private org's members is something the org host (the PDS or
organizational data server) offers inside private contexts. This document
records the question, the measurements, and why the answer is no, so the
next person who hits the gap does not re-derive it.

## the question

Using typeahead as the sign-in completer for a company hosted on habitat's
pear (an organizational data server on the permissioned-spaces alpha)
surfaced two gaps:

1. **A host with identities but no sync stream is invisible.** Pear mints
   `did:web:<opaque>.<hive domain>` identities with handles
   `<label>.<org>.<hive domain>`, serves their DID documents and
   `/.well-known/atproto-did`, and answers `resolveHandle`, but exposes no
   `com.atproto.sync.subscribeRepos`. The relay never carries these accounts,
   so the ingester never learns they exist. [discovery and
   identity](discovery-and-identity.md) records this as a measurable host
   property.

2. **Membership is private on purpose.** `org.getMembers`, `getAdmins` and
   `listRelatedSpaces` are authenticated on pear. What is public is
   handle → DID and the DID document. The ask was: could a member signing
   into the company's app still get completion over their colleagues, with
   typeahead scoped by a space credential or the org's token to a member
   list the org authorizes?

## why the public index cannot host this

A public prefix index has no private mode. Every `h:` and `d:` key is an
enumeration: `q=hq.acme.example` lists every member under that domain. Indexing
a private org's members into the public corpus publishes its directory, even
though each handle is individually resolvable.

The credentialed alternative (a per-org overlay merged only for callers who
prove membership) is buildable, but it turns typeahead into a holder of
private identity data and a verifier of other people's credentials on the
request path: DID-document resolution against org hosts, cache bypass for
authenticated answers, per-directory sync jobs, revocation. That is a
different service with a different trust boundary. The public index gets no
better for it, and every private row is a row we can leak.

## the decision

- **Typeahead indexes what the public network publishes**, discovered from
  the relay and from `/request-indexing`. It stays anonymous, cacheable, and
  byte-identical for every caller.
- **Private completion lives on the org host.** The host already holds the
  member list and already authenticates the member; it is the only party
  that can answer "who may this person see" without a copy. Habitat's own
  single-org search prototype is the right place for that to grow.
- **An app that wants both** merges them itself: resolve the typed name
  exactly against the org host (`<typed>.<org>.<domain>` via
  `resolveHandle`) or query the host's own completer, and union typeahead's
  public results. This is what the habitat sign-in ships today.

## what this leaves for typeahead

- **Long queries are a plain bug**, fixed 2026-09-18: the prefix index stores
  keys up to 16 bytes and the query path now cuts and post-filters. See
  [the index design](typeahead-index-design.md#queries-longer-than-prefix_max).
- **`/request-indexing` indexes anything that resolves**, including a
  private org's members. When that happens by mistake, the remedy is
  `/admin/delete` (Turso rows plus a tombstone the search overlay replays)
  and a domain block on the org's handle domain, which keeps any future
  request-indexing of that domain hidden. Applied to
  `hq.definitelyarealcompany.com` on 2026-09-18 after five members were
  indexed by a client loop. Tombstones prune after seven days; the domain
  block is the durable part.
- **Public hosts that feed no relay** remain a discovery gap for the public
  index. If a host wants its accounts listed, a consented `listRepos`-style
  crawl is the public-context tool for that, and is not designed here.
