# private directories: what typeahead becomes for organizations

Status: **proposal, 2026-09-18.** Nothing here is implemented. Prompted by
using typeahead as the sign-in completer for a company hosted on habitat's
pear (an organizational data server on the permissioned-spaces alpha); the
measurements are from that integration.

## the two gaps, measured

1. **A host with identities but no sync stream is invisible.** Pear mints
   `did:web:<opaque>.<hive domain>` identities with handles
   `<label>.<org>.<hive domain>`, serves their DID documents and
   `/.well-known/atproto-did`, and answers `resolveHandle`, but exposes no
   `com.atproto.sync.subscribeRepos`. The relay never sees these accounts, so
   the ingester never learns they exist. `getProfile` works once an account is
   known and returns `Profile not found` otherwise. Today the only way in is
   `/request-indexing`, one handle at a time, by someone who already knows the
   handle. [discovery and identity](discovery-and-identity.md) records this as
   a measurable host property.

2. **Membership is private on purpose.** `org.getMembers`, `getAdmins` and
   `listRelatedSpaces` on pear are authenticated. What is public is
   handle → DID and the DID document. A member signing into the company's app
   should still get completion over their colleagues, but the company's
   directory is not the public network's business.

The app-side workaround that ships today: resolve the typed name exactly
against the org host (`<typed>.<org>.<domain>` via `resolveHandle`) and merge
typeahead's already-known members. Whole names work without an index;
prefixes work only for members someone requested. That is the baseline any
design here has to beat.

## what the public index cannot do

Gap 1 looks like a discovery problem, but a public prefix index has no
private mode. Every `h:` and `d:` key is an enumeration: `q=hq.acme.example`
lists every member under that domain. Indexing a private org's members into
the public corpus publishes its directory, even though each handle is
individually resolvable. So "index them like everyone else" is only right
when the org wants public enumeration, and the org has to say so.

This also means `/request-indexing` is a policy hole, not just a manual
path: it indexes anything that resolves. A private org's member is
resolvable. A directory design has to decide whether a host that opted into
private-only listing can be bypassed one handle at a time (today it can).

## three shapes

### A. org-fed public index

The org registers itself and typeahead crawls it with consent: pull the
member list with a credential the org grants, or the org pushes rows to an
org-scoped ingest endpoint. Members land in `actors` and serve from the
public index like everyone else.

Right for orgs that *want* to be found (a university, an open community
host). Wrong for the sign-in case, which is exactly the org that does not.
Cheap: it is `/request-indexing` in bulk plus a registration record. It also
closes gap 1 for public hosts that feed no firehose, which will multiply as
spaces hosts appear (sync there is point-to-point, no relay).

### B. credentialed overlay per directory

Keep the public index public. Add a **directory**: a small per-org prefix
index built from a source the org authorizes, merged into results only when
the caller proves membership.

- **Source.** The org admits typeahead's identity as a *reader* of a
  directory space (habitat's per-member "directory" space is this
  primitive), or hands it a reader credential on the members space. Typeahead
  syncs it on a schedule with `listRepos` + `listRecords`, the way habitat's
  own `sap` syncer does. Consent is the credential; revocation is letting it
  lapse. The org never uploads anything to us and never runs a job.
- **Caller credential.** atproto service auth, not the org's OAuth token:
  the member's app calls `getServiceAuth` on its own PDS with `aud` =
  typeahead's DID (`did:plc:y6kywgnb7xletjpa5hhxmd4k`) and `lxm` =
  `tech.waow.typeahead.searchActors`, and sends the JWT as `Authorization`.
  Typeahead verifies the signature against the member's DID document and
  then asks one question: **which directories list this DID?** The answer
  comes from the directory data we already hold, so authorization derives
  from the same rows that populate the overlay. No call to the org host on
  the request path; no OAuth introspection; no token typeahead has to store.
  A space credential (signed by the space owner's key, `exp` authoritative)
  works as an alternative proof of "I am in this space", but service auth
  is what every PDS already mints and it names us as the audience.
- **Serving.** `execute(norm)` runs the public lookup as today, plus one
  keyed lookup per directory the caller belongs to. Directory hits are
  hydrated from the directory's own rows, marked `source: directory`, and
  merged with the usual exact promotion. A directory is thousands of rows,
  so this is one more bounded key read, not corpus work.
- **What must stay true.**
  - Directory rows never enter `actors` or the public snapshot. Separate
    store, separate build, deleted when the org revokes.
  - Credentialed responses are never edge-cached: `Cache-Control: private`
    and `Vary: Authorization`, and the worker's 60s cache is bypassed. Today
    every search answer is publicly cacheable by URL; this is the one
    request-path change with blast radius.
  - Anonymous results are byte-identical with or without a directory
    registered. No leak by ranking, by count, or by timing.
  - Moderation still applies: labels and the global hide list filter
    directory rows too.
  - The public alias `app.bsky.actor.searchActorsTypeahead` never gains
    directory results. The canonical endpoint is where the credential is
    honored, and smoke's byte-identical check stays true for anonymous calls.
- **Lexicon.** `tech.waow.typeahead.searchActors` gains an optional
  `Authorization` and a per-actor `source` field. Registration is a new
  `tech.waow.typeahead.directory.*` surface: register (org DID, space URI,
  visibility), status (last sync, member count), revoke. Visibility is
  `public` (shape A) or `members` (this shape), chosen by the org.
- **Ranking.** Colleagues first. A directory hit outranks a public hit for
  the same query; inside a directory, rank by handle match then display
  match, since `quality_score` is a public-network signal that private
  members do not have.

### C. per-org search the org host runs itself

Habitat already has a single-org `tsvector` prototype. The terminal state
for a purely private directory may be that the host serves its own
completion and typeahead is not involved. What typeahead adds is the merge:
colleagues and the public network in one box, with one ranking and one
moderation pass. If that merge is not wanted, shape C is fine and we should
say so rather than compete for it.

## recommendation

Build **B** with **A** as its `visibility: public` case, in that order:

1. **Registration + sync** (no request-path change): a directory record, a
   reader credential on a space, a heavypad Prefect job that syncs it into a
   per-directory SQLite prefix index and uploads it beside the snapshot.
   Public-visibility directories flow into `actors` through the existing
   request-indexing write path in bulk. This alone closes gap 1 for
   consenting hosts.
2. **Service-auth verification** in the search service: DID document
   resolution with a cache, signature check, directory membership lookup.
   Measure the added latency on an isolated target before it goes near
   `/search`.
3. **Merge + cache bypass** in the worker and engine, gated by smoke checks
   that prove anonymous answers did not change.

What typeahead becomes: a public actor index with a credentialed overlay per
directory, where the credential is the standard atproto one and the source
is a space the org already keeps. It stays a completer; it does not become an
identity provider, an OAuth client of the org, or a holder of org tokens.

## open questions

- Does the reader credential model survive pear's one-hour space credential
  lifetime? Re-minting needs a delegation token, which needs a session as a
  minted service member. Confirm the headless flow holds up before designing
  around it.
- Should `/request-indexing` refuse handles under a domain registered as
  `visibility: members`? Probably yes; that is the only way the org's choice
  means anything.
- did:web resolution for service-auth verification hits the org host on
  first sight. Cache DID documents with a TTL and decide what a resolution
  failure means (refuse, or serve public-only and say so in the response).
- One directory per org, or one per space? Per space is what the substrate
  offers; per org is what the sign-in box wants. Start per space and let an
  org register several.
