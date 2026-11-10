"""In-memory stand-in for one backend's admin and status routes, for unit tests."""

import hashlib
import json

import views
from transport import BudgetExceeded, TransportError


def stored_actor(did, handle, *, version=100, deleted=False, hidden=False, pds_host="pds.test", score=1.0, labels=(), display="Name"):
    """One actor as the Tantivy index stores it and `dump-actors` prints it."""
    return {
        "did": did, "handle": "" if deleted else handle, "display_name": display, "avatar_url": "bafkavatar",
        "labels": list(labels), "associated": {}, "created_at": "2026-01-01T00:00:00Z",
        "pds": "https://" + pds_host if pds_host else "", "pds_host": pds_host, "quality_score": score, "version": version,
        "deleted": deleted, "hidden": hidden, "followers_count": 0, "follows_count": 0, "posts_count": 0, "last_activity_at": 0,
    }


def admin_document(actor):
    """What a Tantivy admin listener's source-state returns for a stored actor."""
    fields = dict(views.from_actor(actor, sha="unused").fields)
    del fields["labels"], fields["profile_rest_sha256"]
    return {"did": actor["did"], "fields": fields}


def document(did, handle, *, hidden=False, deleted=False, pds_host="pds.test", score=1.0, profile="p", version=1):
    fields = {
        "deleted": deleted,
        "hidden": hidden,
        "handle": handle,
        "pds": "https://" + pds_host if pds_host else "",
        "pds_host": pds_host,
        "quality_score": score,
        "version": version,
        "profile_sha256": hashlib.sha256(profile.encode()).hexdigest(),
    }
    return {"did": did, "fields": fields}


class FakeBackend:
    def __init__(self, name, documents, *, fields=True, stale_terms=(), max_requests=None):
        self.name = name
        self.base = "http://" + name + ".test"
        self.documents = {entry["did"]: entry["fields"] for entry in documents}
        self.fields = fields
        self.stale_terms = set(stale_terms)
        self.max_requests = max_requests
        self.calls = []
        self.status = {"build_id": "b-" + name, "watermark": 100, "source_observation_revision": 1, "sync_lag_s": 3}

    def sha(self, did):
        return hashlib.sha256(json.dumps(self.documents[did], sort_keys=True).encode()).hexdigest()

    def charge(self, path, body):
        if self.max_requests is not None and len(self.calls) >= self.max_requests:
            raise BudgetExceeded("request budget exhausted")
        self.calls.append((path, body))

    def post_json(self, path, body):
        self.charge(path, body)
        if path == "/admin/source-page":
            terms = sorted(set(self.documents) | self.stale_terms)
            return {"dids": [did for did in terms if did > body["after_did"]][: body["limit"]]}
        if path == "/admin/source-state":
            if len(body["dids"]) > 500:
                raise TransportError("too many DIDs")
            actors = []
            for did in body["dids"]:
                entry = {"did": did, "document_sha256": self.sha(did) if did in self.documents else None}
                if body.get("fields") and self.fields:
                    entry["fields"] = self.documents.get(did)
                actors.append(entry)
            return {"actors": actors}
        raise TransportError("unknown path " + path)

    def get_json(self, path, params=None):
        self.charge(path, params)
        if path == "/index/status":
            return dict(self.status)
        raise TransportError("unknown path " + path)
