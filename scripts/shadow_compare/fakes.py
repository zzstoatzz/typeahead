"""In-memory stand-in for one backend's admin and status routes, for unit tests."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import arbiter
import corpus
import identity
import moderation
import store
import views
from transport import Budget, BudgetExceeded, Client, TransportError

RULES = moderation.Rules.load()
NOW_MS = 1_794_355_200_000
PUBLIC_ADDRESSES = {"93.184.216.34"}


def policy(overrides=None, domains=(), source="none"):
    return moderation.Policy(RULES, overrides, domains, source)


def visibility(using=None):
    return corpus.Visibility(using or policy(), NOW_MS)


def walk_settings(**changes):
    """Arguments for `corpus.run_sources` that keep every difference and never wait."""
    settings = dict(
        policy=policy(), checkpoint_path=None, page_size=100, max_pages=None, max_tracked=1000, settle_delay=7, sample_size=5,
        identity_sample_size=5, sleep=lambda seconds: None,
    )
    settings.update(changes)
    return settings


def recorded(entries, *, max_tracked=1000, sample_size=5, identity_sample_size=5):
    """The summary of a walk that found exactly these differences."""
    checkpoint = corpus.Checkpoint(None, {}, max_tracked=max_tracked, sample_size=sample_size)
    for entry in entries:
        checkpoint.record(entry)
    return corpus.summarize(checkpoint, checkpoint.entries, identity_sample_size, settled=False)


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


class Network:
    """plc.directory, a DNS-over-HTTPS resolver, an appview and any number of hosts, answered from dicts."""

    def __init__(self):
        self.documents = {}
        self.dns = {}
        self.well_known = {}
        self.repos = {}
        self.addresses = {}
        self.profiles = {}
        self.fail_profiles = False
        self.requests = []

    def handler(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append((host, path, request.headers.get("accept")))
        if host == "plc.test":
            found = self.documents.get(path.lstrip("/"))
            return httpx.Response(*found) if isinstance(found, tuple) else httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if host == "appview.test":
            wanted = request.url.params.get_list("actors")
            if self.fail_profiles or len(wanted) > arbiter.PROFILE_BATCH:
                return httpx.Response(502)
            return httpx.Response(200, json={"profiles": [dict(self.profiles[did], did=did) for did in wanted if did in self.profiles]})
        if host == "doh.test":
            name = request.url.params["name"]
            if name not in self.dns:
                return httpx.Response(200, json={"Status": 3})
            found = self.dns[name]
            return httpx.Response(found) if isinstance(found, int) else httpx.Response(200, json=found)
        if path == identity.WELL_KNOWN_HANDLE:
            found = self.well_known.get(host)
            if isinstance(found, Exception):
                raise found
            return httpx.Response(404) if found is None else httpx.Response(*found) if isinstance(found, tuple) else httpx.Response(200, text=found)
        if path == "/xrpc/com.atproto.sync.getRepoStatus":
            found = self.repos.get((host, request.url.params["did"]))
            return httpx.Response(*found[:1], json=found[1]) if found else httpx.Response(400, json={"error": "RepoNotFound"})
        if path == "/.well-known/did.json":
            found = self.documents.get("did:web:" + host)
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        return httpx.Response(404)

    def document(self, did, handle, pds="pds.test"):
        self.documents[did] = {
            "id": did, "alsoKnownAs": ["https://not-a-handle.test", "at://" + handle.upper(), "at://second.test"],
            "service": [{"id": did + "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://" + pds}],
        }

    def account(self, did, handle, pds="pds.test", active=True, profile=None):
        """An account whose handle resolves back, whose PDS answers for it, and which the appview serves when given a profile."""
        self.document(did, handle, pds)
        self.well_known[handle.lower()] = did
        self.repos[(pds, did)] = (200, {"did": did, "active": active, **({} if active else {"status": "deactivated"})})
        if profile is not None:
            self.profiles[did] = {"handle": handle, **profile}

    def client(self, name):
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return Client(name, f"https://{name}.test", Budget(10_000, 10**8), retries=0, http=http)

    def resolver(self):
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        origins = identity.Origins(Budget(10_000, 10**8), http=http, addresses=lambda host: self.addresses.get(host, PUBLIC_ADDRESSES))
        return identity.Resolver(self.client("plc"), self.client("doh"), origins)

    def authority(self, using=None, now_ms=NOW_MS):
        return arbiter.Authority(self.resolver(), self.client("appview"), using or policy(), now_ms)
