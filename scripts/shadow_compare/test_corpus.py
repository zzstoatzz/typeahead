import json
import tempfile
import unittest
from pathlib import Path

import corpus
from corpus import Doc
from fakes import FakeBackend, document
from transport import TransportError


def did(n):
    return f"did:plc:{n:05}"


def pair(count=1200):
    rows = [document(did(n), f"h{n}.test") for n in range(count)]
    return [dict(row, fields=dict(row["fields"])) for row in rows], [dict(row, fields=dict(row["fields"])) for row in rows]


def run(production, shadow, **options):
    settings = dict(
        checkpoint_path=None, page_size=100, max_pages=None, max_tracked=1000, settle_delay=7, sample_size=5,
        sleep=lambda seconds: None,
    )
    settings.update(options)
    return corpus.run(production, shadow, **settings)


class ClassifyTests(unittest.TestCase):
    def doc(self, **changes):
        return Doc("x" + json.dumps(changes, sort_keys=True), document("did:plc:a", "a.test", **changes)["fields"])

    def test_equal_documents_and_version_only_changes_are_not_differences(self):
        self.assertEqual(corpus.classify(self.doc(), self.doc(version=9)), ([], []))

    def test_presence(self):
        live = self.doc()
        self.assertEqual(corpus.classify(live, corpus.ABSENT)[0], [corpus.ONLY_IN_PRODUCTION])
        self.assertEqual(corpus.classify(corpus.ABSENT, live)[0], [corpus.ONLY_IN_SHADOW])
        self.assertEqual(corpus.classify(live, self.doc(deleted=True))[0], [corpus.ONLY_IN_PRODUCTION])
        self.assertEqual(corpus.classify(self.doc(deleted=True), live)[0], [corpus.ONLY_IN_SHADOW])
        self.assertEqual(corpus.classify(self.doc(deleted=True), corpus.ABSENT), ([], []))

    def test_field_classes(self):
        base = self.doc()
        cases = {
            corpus.VISIBILITY_DIFFERS: self.doc(hidden=True),
            corpus.PDS_DIFFERS: self.doc(pds_host="other.test"),
            corpus.QUALITY_SCORE_DIFFERS: self.doc(score=2.0),
            corpus.OTHER_FIELD_DIFFERS: self.doc(profile="q"),
        }
        for expected, changed in cases.items():
            self.assertEqual(corpus.classify(base, changed)[0], [expected])
        moved = Doc("y", document("did:plc:a", "b.test")["fields"])
        self.assertEqual(corpus.classify(base, moved)[0], [corpus.HANDLE_DIFFERS])
        self.assertEqual(corpus.classify(base, self.doc(score=2.0))[1], [])
        self.assertEqual(corpus.classify(base, self.doc(profile="q"))[1], ["profile_sha256"])

    def test_one_did_can_carry_several_classes(self):
        changed = Doc("z", document("did:plc:a", "b.test", hidden=True, pds_host="other.test", profile="q")["fields"])
        classes, other = corpus.classify(self.doc(), changed)
        self.assertEqual(
            classes,
            [corpus.HANDLE_DIFFERS, corpus.VISIBILITY_DIFFERS, corpus.PDS_DIFFERS, corpus.OTHER_FIELD_DIFFERS],
        )
        self.assertEqual(other, ["profile_sha256"])

    def test_raw_pds_with_same_host_is_an_other_field(self):
        left = self.doc()
        fields = dict(left.fields, pds="https://pds.test/")
        self.assertEqual(corpus.classify(left, Doc("r", fields)), ([corpus.OTHER_FIELD_DIFFERS], ["pds"]))

    def test_without_fields_only_the_hash_is_comparable(self):
        self.assertEqual(corpus.classify(Doc("a"), Doc("b"))[0], [corpus.DOCUMENT_HASH_DIFFERS])
        self.assertEqual(corpus.classify(Doc("a"), Doc("a"))[0], [])
        self.assertEqual(corpus.classify(Doc("a"), corpus.ABSENT)[0], [corpus.ONLY_IN_PRODUCTION])


class WalkTests(unittest.TestCase):
    def test_identical_corpora_have_no_differences(self):
        a, b = pair()
        result = run(FakeBackend("p", a), FakeBackend("s", b))
        self.assertTrue(result["complete"])
        self.assertEqual(result["dids_walked"], 1200)
        self.assertEqual(result["live"], {"production": 1200, "shadow": 1200})
        self.assertEqual(result["differing_dids"], 0)

    def test_every_difference_is_found_across_page_boundaries(self):
        a, b = pair()
        b[5]["fields"]["handle"] = "moved.test"
        b[1199]["fields"]["hidden"] = True
        del b[640:900]
        del a[100]
        del b[99]
        a.append(document("did:plc:99999", "tail.test"))
        result = run(FakeBackend("p", a), FakeBackend("s", b), max_tracked=5000)
        classes = {name: entry["count"] for name, entry in result["classes"].items()}
        self.assertEqual(classes[corpus.ONLY_IN_PRODUCTION], 262)
        self.assertEqual(classes[corpus.ONLY_IN_SHADOW], 1)
        self.assertEqual(classes[corpus.HANDLE_DIFFERS], 1)
        self.assertEqual(classes[corpus.VISIBILITY_DIFFERS], 1)
        self.assertEqual(result["dids_walked"], 1201)
        self.assertIn(did(99), result["flagged_dids"])
        self.assertIn("did:plc:99999", result["flagged_dids"])

    def test_pages_and_state_batches_respect_the_server_limits(self):
        a, b = pair(1300)
        production, shadow = FakeBackend("p", a), FakeBackend("s", b[:10])
        run(production, shadow, page_size=500)
        for backend in (production, shadow):
            for path, body in backend.calls:
                if path == "/admin/source-page":
                    self.assertLessEqual(body["limit"], 500)
                if path == "/admin/source-state":
                    self.assertLessEqual(len(body["dids"]), 500)
        with self.assertRaises(TransportError):
            run(production, shadow, page_size=501)

    def test_stale_index_terms_count_as_absent(self):
        a, b = pair(50)
        result = run(FakeBackend("p", a, stale_terms={"did:plc:zzzzz"}), FakeBackend("s", b))
        self.assertEqual(result["differing_dids"], 0)
        self.assertEqual(result["live"]["production"], 50)

    def test_resume_from_checkpoint_matches_an_uninterrupted_walk(self):
        a, b = pair()
        del b[250]
        b[900]["fields"]["pds_host"] = "moved.test"
        whole = run(FakeBackend("p", a), FakeBackend("s", b))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            first = run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path, max_pages=4)
            self.assertFalse(first["complete"])
            self.assertEqual(first["stop_reason"], "page_limit")
            self.assertEqual(first["cursor"], json.loads(path.read_text())["cursor"])
            production = FakeBackend("p", a)
            second = run(production, FakeBackend("s", b), checkpoint_path=path)
            self.assertTrue(second["resumed"] and second["complete"])
            first_page = next(body for route, body in production.calls if route == "/admin/source-page")
            self.assertEqual(first_page["after_did"], first["cursor"])
            for key in ("dids_walked", "live", "flagged_dids", "classes"):
                self.assertEqual(second[key], whole[key])
            with self.assertRaises(TransportError):
                run(FakeBackend("other", a), FakeBackend("s", b), checkpoint_path=path)

    def test_budget_exhaustion_stops_incomplete_and_resumable(self):
        a, b = pair()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            result = run(FakeBackend("p", a, max_requests=7), FakeBackend("s", b), checkpoint_path=path)
            self.assertFalse(result["complete"])
            self.assertEqual(result["stop_reason"], "budget")
            self.assertFalse(result["rechecked"])
            done = run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path)
            self.assertTrue(done["complete"])
            self.assertEqual(done["dids_walked"], 1200)

    def test_differences_beyond_the_tracking_cap_are_counted(self):
        a, b = pair(300)
        result = run(FakeBackend("p", a), FakeBackend("s", b[:100]), max_tracked=50)
        self.assertEqual(result["differing_dids"], 50)
        self.assertEqual(result["untracked_differences"], 150)


class RecheckTests(unittest.TestCase):
    def test_differences_that_settle_are_dropped_after_the_delay(self):
        a, b = pair(300)
        b[10]["fields"]["handle"] = "lagging.test"
        b[20]["fields"]["handle"] = "wrong.test"
        del b[200]
        production, shadow = FakeBackend("p", a), FakeBackend("s", b)
        slept = []

        def settle(seconds):
            slept.append(seconds)
            shadow.documents[did(10)]["handle"] = "h10.test"
            shadow.documents[did(200)] = dict(production.documents[did(200)])

        result = run(production, shadow, sleep=settle)
        self.assertEqual(slept, [7])
        self.assertEqual(result["differences_before_recheck"], 3)
        self.assertEqual(result["transient_differences"], 2)
        self.assertEqual(result["flagged_dids"], [did(20)])
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["count"], 1)
        self.assertTrue(result["rechecked"])

    def test_recheck_reclassifies_with_the_later_state(self):
        a, b = pair(20)
        b[3]["fields"]["handle"] = "x.test"
        production, shadow = FakeBackend("p", a), FakeBackend("s", b)

        def settle(seconds):
            shadow.documents[did(3)]["handle"] = "h3.test"
            shadow.documents[did(3)]["hidden"] = True

        result = run(production, shadow, sleep=settle)
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["count"], 0)
        self.assertEqual(result["classes"][corpus.VISIBILITY_DIFFERS]["count"], 1)

    def test_no_differences_means_no_settle_wait(self):
        a, b = pair(20)
        slept = []
        run(FakeBackend("p", a), FakeBackend("s", b), sleep=slept.append)
        self.assertEqual(slept, [])

    def test_markers_bound_the_walk_in_time(self):
        a, b = pair(20)
        production, shadow = FakeBackend("p", a), FakeBackend("s", b)
        b[1]["fields"]["hidden"] = True
        shadow.documents[did(1)]["hidden"] = True

        def settle(seconds):
            shadow.status["build_id"] = "b-next"
            shadow.status["watermark"] = 140

        result = run(production, shadow, sleep=settle)
        self.assertEqual(result["start_markers"]["shadow"]["watermark"], 100)
        self.assertEqual(result["end_markers"]["shadow"]["watermark"], 140)
        self.assertEqual(result["generation_changed"], {"production": False, "shadow": True})


class HashOnlyTests(unittest.TestCase):
    def test_servers_without_fields_yield_presence_and_hash_classes(self):
        a, b = pair(30)
        b[4]["fields"]["handle"] = "x.test"
        del b[9]
        result = run(FakeBackend("p", a, fields=False), FakeBackend("s", b, fields=False))
        self.assertFalse(result["fields_available"])
        self.assertEqual(result["classes"][corpus.DOCUMENT_HASH_DIFFERS]["count"], 1)
        self.assertEqual(result["classes"][corpus.ONLY_IN_PRODUCTION]["count"], 1)
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["count"], 0)


class SampleTests(unittest.TestCase):
    def test_sample_is_deterministic_and_order_independent(self):
        dids = [did(n) for n in range(200)]
        chosen = corpus.sample(dids, 10)
        self.assertEqual(chosen, corpus.sample(list(reversed(dids)), 10))
        self.assertEqual(len(set(chosen)), 10)
        self.assertEqual(corpus.sample(dids[:3], 10), sorted(dids[:3], key=corpus.sample_key))


if __name__ == "__main__":
    unittest.main()
