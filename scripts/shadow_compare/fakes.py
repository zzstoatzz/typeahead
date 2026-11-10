"""In-memory stand-in for one backend's admin and status routes, for unit tests."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import store
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
    fields = {name: value for name, value in views.from_actor(actor, sha="unused").fields.items() if name not in views.ITEMISED}
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


def store_record(actor, updated_at):
    """The store record the index bridge would turn into this stored actor."""
    status = "deleted" if actor.get("deleted") else "live"
    return {
        "did": actor["did"], "status": status, "hidden": actor.get("hidden", False), "handle": actor.get("handle", ""),
        "display_name": actor.get("display_name", ""), "avatar": actor.get("avatar_url", ""), "pds": actor.get("pds", ""),
        "created_at": actor.get("created_at", ""),
        "associated": json.dumps(actor["associated"], separators=(",", ":")) if actor.get("associated") else "",
        "quality_score": actor.get("quality_score", 0), "updated_at": updated_at,
        "labels": [
            {"source": label["src"], "uri": label["uri"], "value": label["val"], "created": label["cts"],
             "cid": label.get("cid", ""), "expires": label.get("exp", ""), "negated": False}
            for label in actor.get("labels", [])
        ],
    }


class FakeStore:
    """A loopback HTTP server answering the two store read endpoints from a dict of records."""

    def __init__(self, records, secret, next_change=1):
        self.records = {record["did"]: record for record in records}
        self.secret = secret
        self.next_change = next_change
        self.calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                owner.calls.append((self.path, body))
                if self.headers.get("Authorization") != "Bearer " + owner.secret:
                    return self.answer(401, {"error": "unauthorized " + str(self.headers.get("Authorization"))})
                if self.path == store.PAGE_PATH:
                    if not 1 <= body["limit"] <= store.PAGE_LIMIT:
                        return self.answer(400, {"error": "limit"})
                    dids = [did for did in sorted(owner.records) if did > body["after_did"]][: body["limit"]]
                    return self.answer(200, {"actors": [owner.records[did] for did in dids], "next_change": owner.next_change})
                if self.path == store.BATCH_PATH:
                    if len(body["dids"]) > store.BATCH_LIMIT:
                        return self.answer(400, {"error": "too many DIDs"})
                    return self.answer(200, {"actors": [owner.records.get(did) for did in body["dids"]], "next_change": owner.next_change})
                self.answer(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
