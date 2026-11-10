import unittest

import httpx

import arbiter
import corpus
from fakes import document
from transport import Budget, Client

DID = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"


def authority(documents, profiles, fail_profiles=False):
    def handler(request):
        if request.url.path.endswith("getProfiles"):
            if fail_profiles:
                return httpx.Response(502)
            wanted = request.url.params.get_list("actors")
            return httpx.Response(200, json={"profiles": [dict(profiles[d], did=d) for d in wanted if d in profiles]})
        did = request.url.path.lstrip("/")
        if request.url.host == "web.test":
            did = "did:web:web.test"
        if did not in documents:
            return httpx.Response(404, json={})
        handle, pds = documents[did]
        return httpx.Response(200, json={
            "id": did, "alsoKnownAs": ["https://not-a-handle.test", "at://" + handle.upper()],
            "service": [{"id": did + "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://" + pds + "/"}],
        })

    def make(base):
        return Client("authority", base, Budget(100, 10**6), retries=0, http=httpx.Client(transport=httpx.MockTransport(handler)))

    return arbiter.Authority(make("https://plc.test"), make("https://appview.test"), web=make)


def entry(production, shadow, did=DID):
    def doc(fields):
        return corpus.ABSENT if fields is None else corpus.Doc("h", fields)

    return corpus.difference(did, doc(production), doc(shadow))


def fields(handle="a.test", **changes):
    return document(DID, handle, **changes)["fields"]


class JudgeTests(unittest.TestCase):
    def label(self, difference, source, name):
        fact = source.facts([difference["did"]])[difference["did"]]
        return arbiter.judge(difference, fact)[name]["label"]

    def test_handle_is_judged_against_the_verified_authority_handle(self):
        source = authority({DID: ("new.test", "pds.test")}, {DID: {"handle": "new.test"}})
        cases = [
            (fields("old.test"), fields("new.test"), arbiter.SHADOW_RIGHT),
            (fields("new.test"), fields("old.test"), arbiter.PRODUCTION_RIGHT),
            (fields("old.test"), fields("older.test"), arbiter.BOTH_STALE),
        ]
        for production, shadow, expected in cases:
            self.assertEqual(self.label(entry(production, shadow), source, corpus.HANDLE_DIFFERS), expected)

    def test_handle_the_appview_did_not_verify_is_undetermined(self):
        source = authority({DID: ("new.test", "pds.test")}, {DID: {"handle": "handle.invalid"}})
        difference = entry(fields("old.test"), fields("new.test"))
        self.assertEqual(self.label(difference, source, corpus.HANDLE_DIFFERS), arbiter.UNDETERMINED)

    def test_pds_is_judged_against_the_did_document_service(self):
        source = authority({DID: ("a.test", "two.test")}, {DID: {"handle": "a.test"}})
        difference = entry(fields(pds_host="one.test"), fields(pds_host="two.test"))
        self.assertEqual(self.label(difference, source, corpus.PDS_DIFFERS), arbiter.SHADOW_RIGHT)
        difference = entry(fields(pds_host="two.test"), fields(pds_host="one.test"))
        self.assertEqual(self.label(difference, source, corpus.PDS_DIFFERS), arbiter.PRODUCTION_RIGHT)

    def test_presence_follows_whether_the_account_is_active(self):
        active = authority({DID: ("a.test", "pds.test")}, {DID: {"handle": "a.test"}})
        gone = authority({}, {})
        only_production, only_shadow = entry(fields(), None), entry(None, fields())
        self.assertEqual(self.label(only_production, active, corpus.ONLY_IN_PRODUCTION), arbiter.PRODUCTION_RIGHT)
        self.assertEqual(self.label(only_production, gone, corpus.ONLY_IN_PRODUCTION), arbiter.SHADOW_RIGHT)
        self.assertEqual(self.label(only_shadow, active, corpus.ONLY_IN_SHADOW), arbiter.SHADOW_RIGHT)
        self.assertEqual(self.label(only_shadow, gone, corpus.ONLY_IN_SHADOW), arbiter.PRODUCTION_RIGHT)

    def test_unreadable_appview_leaves_presence_undetermined(self):
        source = authority({DID: ("a.test", "pds.test")}, {}, fail_profiles=True)
        self.assertEqual(self.label(entry(fields(), None), source, corpus.ONLY_IN_PRODUCTION), arbiter.UNDETERMINED)

    def test_visibility_and_other_fields_have_no_authority(self):
        source = authority({DID: ("a.test", "pds.test")}, {DID: {"handle": "a.test"}})
        self.assertEqual(self.label(entry(fields(), fields(hidden=True)), source, corpus.VISIBILITY_DIFFERS), arbiter.UNDETERMINED)
        self.assertEqual(self.label(entry(fields(), fields(score=2.0)), source, corpus.OTHER_FIELD_DIFFERS), arbiter.UNDETERMINED)

    def test_did_web_resolves_from_its_own_host(self):
        did = "did:web:web.test"
        source = authority({did: ("web.test", "pds.test")}, {did: {"handle": "web.test"}})
        fact = source.facts([did])[did]
        self.assertEqual((fact["handle"], fact["pds_host"], fact["active"]), ("web.test", "pds.test", True))
        self.assertEqual(arbiter.did_document_location("did:web:web.test:path"), (None, None))

    def test_label_samples_tallies_per_class(self):
        source = authority({DID: ("new.test", "pds.test")}, {DID: {"handle": "new.test"}})
        summary = corpus.summarize({DID: entry(fields("old.test"), fields("new.test", hidden=True))}, 5)
        arbiter.label_samples(summary, source)
        self.assertEqual(summary["classes"][corpus.HANDLE_DIFFERS]["arbiter_labels"], {arbiter.SHADOW_RIGHT: 1})
        self.assertEqual(summary["classes"][corpus.VISIBILITY_DIFFERS]["arbiter_labels"], {arbiter.UNDETERMINED: 1})
        self.assertTrue(summary["arbiter_ran"])


if __name__ == "__main__":
    unittest.main()
