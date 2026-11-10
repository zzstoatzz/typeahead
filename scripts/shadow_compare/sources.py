"""Corpus sources: each yields comparable actor views in DID order.

A source has:

- `name` and `identity`, the latter stored in the checkpoint so a walk only resumes
  against the same pair;
- `fixed`, true when the content cannot change during the run, so differing DIDs
  are not read from it again;
- `marker()`, the source's build, watermark and the time it was observed;
- `read(after, limit)`, at most `limit` `(did, Doc)` pairs with DIDs strictly
  ascending and greater than `after`; fewer than `limit` means the source is exhausted;
- `lookup(dids)`, a `Doc` for every requested DID, `ABSENT` where it has none.
"""

import time

from transport import TransportError
from views import Doc

PAGE_LIMIT = 500
STATE_LIMIT = 500
STATUS_MARKERS = (
    "build_id",
    "watermark",
    "source_observation_revision",
    "sync_lag_s",
    "actor_count",
    "searchable_actor_count",
    "tombstone_count",
)


def fetch_page(client, after, limit):
    reply = client.post_json("/admin/source-page", {"after_did": after, "limit": limit})
    dids = reply.get("dids")
    if not isinstance(dids, list) or len(dids) > limit or any(not isinstance(d, str) for d in dids):
        raise TransportError(f"{client.name} source-page returned an invalid page")
    if any(not left < right for left, right in zip([after, *dids], dids)):
        raise TransportError(f"{client.name} source-page is not strictly increasing after the cursor")
    return dids


def fetch_states(client, dids, want_fields=True):
    result = {}
    for start in range(0, len(dids), STATE_LIMIT):
        chunk = dids[start : start + STATE_LIMIT]
        body = {"dids": chunk}
        if want_fields:
            body["fields"] = True
        reply = client.post_json("/admin/source-state", body)
        actors = reply.get("actors")
        if not isinstance(actors, list) or [a.get("did") for a in actors] != chunk:
            raise TransportError(f"{client.name} source-state did not answer the requested DIDs in order")
        for actor in actors:
            fields = actor.get("fields")
            result[actor["did"]] = Doc(actor.get("document_sha256"), fields if isinstance(fields, dict) else None)
    return result


def status_marker(client):
    status = client.get_json("/index/status")
    marker = {name: status.get(name) for name in STATUS_MARKERS}
    marker["observed_at"] = int(time.time())
    return marker


class TantivyAdmin:
    """A Tantivy server's admin listener: source-page for identities, source-state for fields."""

    fixed = False

    def __init__(self, client, want_fields=True):
        self.client = client
        self.name = client.name
        self.want_fields = want_fields
        self.identity = {"kind": "tantivy-admin", "base": client.base, "fields": want_fields}

    def marker(self):
        return status_marker(self.client)

    def read(self, after, limit):
        if limit > PAGE_LIMIT:
            raise TransportError(f"page size must be 1..{PAGE_LIMIT}")
        dids = fetch_page(self.client, after, limit)
        states = fetch_states(self.client, dids, self.want_fields)
        return [(did, states[did]) for did in dids]

    def lookup(self, dids):
        return fetch_states(self.client, dids, self.want_fields)
