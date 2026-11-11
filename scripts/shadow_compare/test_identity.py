import json
import unittest

import httpx

import identity
from transport import Budget, Client

DID = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb"
PUBLIC = {"93.184.216.34"}


def txt(*records, status=0):
    answers = [{"name": "_atproto.x", "type": 16, "TTL": 300, "data": record} for record in records]
    return {"Status": status, "TC": False, "Answer": answers, "Comment": ["EDE(10): RRSIGs Missing"]}


class World:
    """plc.directory, a DNS-over-HTTPS resolver and any number of hosts, answered from dicts."""

    def __init__(self):
        self.documents = {}
        self.dns = {}
        self.well_known = {}
        self.repos = {}
        self.addresses = {}
        self.requests = []

    def handler(self, request):
        host, path = request.url.host, request.url.path
        self.requests.append((host, path, request.headers.get("accept")))
        if host == "plc.test":
            found = self.documents.get(path.lstrip("/"))
            return httpx.Response(*found) if isinstance(found, tuple) else httpx.Response(200, json=found) if found else httpx.Response(404, json={})
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

    def resolver(self):
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        plc = Client("plc", "https://plc.test", Budget(100, 10**6), retries=0, http=http)
        doh = Client("doh", "https://doh.test", Budget(100, 10**6), retries=0, http=http)
        origins = identity.Origins(Budget(100, 10**6), http=http, addresses=lambda host: self.addresses.get(host, PUBLIC))
        return identity.Resolver(plc, doh, origins)


class DocumentTests(unittest.TestCase):
    def test_a_document_names_the_first_at_handle_and_the_pds(self):
        world = World()
        world.document(DID, "Alice.Test", "PDS.Test")
        self.assertEqual(world.resolver().document(DID), {"state": "found", "handle": "alice.test", "pds": "https://pds.test", "pds_host": "pds.test"})

    def test_a_first_at_entry_that_is_not_a_handle_claims_nothing(self):
        self.assertIsNone(identity.claimed_handle({"alsoKnownAs": ["at://not a handle", "at://real.test"]}))
        self.assertIsNone(identity.claimed_pds({"service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "http://pds.test"}]}))

    def test_a_tombstone_and_a_missing_document_are_gone_and_an_error_is_unknown(self):
        world = World()
        world.documents[DID] = (410, )
        world.documents[OTHER] = (503, )
        resolver = world.resolver()
        self.assertEqual(resolver.document(DID)["state"], "gone")
        self.assertEqual(resolver.document("did:plc:cccccccccccccccccccccccc")["state"], "gone")
        self.assertEqual(resolver.document(OTHER)["state"], identity.UNKNOWN)
        self.assertEqual(resolver.document("did:key:z6Mk")["state"], identity.UNKNOWN)

    def test_a_document_for_another_did_is_not_believed(self):
        world = World()
        world.document(DID, "alice.test")
        world.documents[OTHER] = world.documents[DID]
        self.assertEqual(world.resolver().document(OTHER)["state"], identity.UNKNOWN)

    def test_did_web_reads_the_hosts_own_document_and_is_read_once(self):
        world = World()
        world.document("did:web:web.test", "web.test")
        resolver = world.resolver()
        self.assertEqual(resolver.document("did:web:web.test")["handle"], "web.test")
        resolver.document("did:web:web.test")
        self.assertEqual(world.requests, [("web.test", "/.well-known/did.json", "*/*")])
        self.assertEqual(resolver.document("did:web:web.test:user")["state"], identity.UNKNOWN)


class HandleTests(unittest.TestCase):
    def test_txt_answers_follow_zats_rules(self):
        self.assertEqual(identity.did_from_txt(txt('"foo=bar"', '"did=did:plc:abc123"')["Answer"]), "did:plc:abc123")
        self.assertEqual(identity.did_from_txt(txt("did=did:plc:abc123")["Answer"]), "did:plc:abc123")
        self.assertIsNone(identity.did_from_txt(txt('"did=did:plc:abc123"', '"did=did:plc:def456"')["Answer"]))
        self.assertIsNone(identity.did_from_txt(txt('"did=not-a-did"')["Answer"]))
        self.assertIsNone(identity.did_from_txt([{"name": "x", "type": 5}, None]))

    def test_either_dns_or_the_well_known_file_proves_a_handle(self):
        world = World()
        world.dns["_atproto.dns.test"] = txt(f'"did={DID}"')
        world.well_known["http.test"] = f"  {DID}\n"
        resolver = world.resolver()
        self.assertEqual((resolver.handle_proof("dns.test", DID)["state"], resolver.handle_proof("dns.test", DID)["via"]), (identity.PROVEN, "dns"))
        self.assertEqual((resolver.handle_proof("http.test", DID)["state"], resolver.handle_proof("http.test", DID)["via"]), (identity.PROVEN, "https"))
        self.assertIn(("doh.test", "/dns-query", "application/dns-json"), world.requests)
        self.assertEqual(sum(host == "doh.test" for host, _, _ in world.requests), 2)

    def test_a_handle_that_resolves_elsewhere_or_nowhere_is_disproven(self):
        world = World()
        world.dns["_atproto.moved.test"] = txt(f'"did={OTHER}"')
        world.well_known["redirect.test"] = (301, )
        world.well_known["big.test"] = "x" * (identity.MAX_HANDLE_BODY + 1)
        world.well_known["junk.test"] = "<html>"
        world.addresses["gone.test"] = set()
        world.addresses["inside.test"] = {"10.0.0.7"}
        world.well_known["inside.test"] = DID
        resolver = world.resolver()
        for handle in ("moved.test", "redirect.test", "big.test", "junk.test", "gone.test", "inside.test", "nothing.test", "not a handle"):
            self.assertEqual(resolver.handle_proof(handle, DID)["state"], identity.DISPROVEN, handle)
        self.assertNotIn(("inside.test", identity.WELL_KNOWN_HANDLE, "*/*"), world.requests)
        self.assertIn("resolves to " + OTHER, resolver.handle_proof("moved.test", DID)["reason"])

    def test_a_failure_to_ask_leaves_the_question_open(self):
        world = World()
        world.dns["_atproto.dnsdown.test"] = 502
        world.dns["_atproto.servfail.test"] = {"Status": 2}
        world.well_known["down.test"] = httpx.ConnectTimeout("timed out")
        world.well_known["error.test"] = (503, )
        resolver = world.resolver()
        for handle in ("dnsdown.test", "servfail.test", "down.test", "error.test"):
            self.assertEqual(resolver.handle_proof(handle, DID)["state"], identity.UNKNOWN, handle)
        world.dns["_atproto.dnsdown.test"] = 502
        world.well_known["dnsdown.test"] = DID
        self.assertEqual(world.resolver().handle_proof("dnsdown.test", DID)["state"], identity.PROVEN)

    def test_the_current_handle_is_the_claimed_one_only_when_it_resolves_back(self):
        world = World()
        world.document(DID, "alice.test")
        world.well_known["alice.test"] = DID
        world.document(OTHER, "stale.test")
        world.documents["did:plc:cccccccccccccccccccccccc"] = {"id": "did:plc:cccccccccccccccccccccccc", "alsoKnownAs": []}
        world.document("did:plc:dddddddddddddddddddddddd", "down.test")
        world.well_known["down.test"] = httpx.ConnectError("refused")
        resolver = world.resolver()
        self.assertEqual(resolver.handle(DID)["value"], "alice.test")
        self.assertEqual(resolver.handle(OTHER)["value"], identity.INVALID_HANDLE)
        self.assertEqual(resolver.handle("did:plc:cccccccccccccccccccccccc")["value"], identity.INVALID_HANDLE)
        self.assertEqual((resolver.handle("did:plc:dddddddddddddddddddddddd")["value"], resolver.handle("did:plc:dddddddddddddddddddddddd")["claimed"]), (None, "down.test"))
        self.assertIsNone(resolver.handle("did:plc:eeeeeeeeeeeeeeeeeeeeeeee")["value"])


class RepoTests(unittest.TestCase):
    def test_the_pds_says_whether_the_account_is_active(self):
        world = World()
        for did, answer in ((DID, (200, {"did": DID, "active": True})), (OTHER, (200, {"did": OTHER, "active": False, "status": "deactivated"}))):
            world.document(did, "a.test")
            world.repos[("pds.test", did)] = answer
        world.document("did:plc:cccccccccccccccccccccccc", "c.test")
        world.document("did:plc:dddddddddddddddddddddddd", "d.test")
        world.repos[("pds.test", "did:plc:dddddddddddddddddddddddd")] = (500, {"error": "InternalServerError"})
        world.documents["did:plc:ffffffffffffffffffffffff"] = (410, )
        resolver = world.resolver()
        self.assertEqual(resolver.repo(DID), {"active": True, "reason": "pds.test says the repo is active"})
        self.assertEqual(resolver.repo(OTHER), {"active": False, "reason": "pds.test says the repo is deactivated"})
        self.assertEqual(resolver.repo("did:plc:cccccccccccccccccccccccc"), {"active": False, "reason": "pds.test has no such repo"})
        self.assertIsNone(resolver.repo("did:plc:dddddddddddddddddddddddd")["active"])
        self.assertFalse(resolver.repo("did:plc:ffffffffffffffffffffffff")["active"])
        self.assertIsNone(resolver.repo("did:key:z6Mk")["active"])

    def test_a_pds_without_a_public_address_is_not_asked(self):
        world = World()
        world.document(DID, "a.test", "inside.test")
        world.addresses["inside.test"] = {"127.0.0.1"}
        self.assertIsNone(world.resolver().repo(DID)["active"])
        self.assertEqual([host for host, _, _ in world.requests], ["plc.test"])


if __name__ == "__main__":
    unittest.main()
