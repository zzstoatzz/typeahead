import json
import tempfile
import unittest
from pathlib import Path

import corpus
import report
import views
from corpus import Doc
from fakes import FakeBackend, document, policy, recorded, visibility, walk_settings
from transport import TransportError

MOD = "did:plc:ar7c4by46qjdydhdevvrndac"


def did(n):
    return f"did:plc:{n:05}"


def pair(count=1200):
    rows = [document(did(n), f"h{n}.test") for n in range(count)]
    return [dict(row, fields=dict(row["fields"])) for row in rows], [dict(row, fields=dict(row["fields"])) for row in rows]


def run(production, shadow, **options):
    return corpus.run(production, shadow, **walk_settings(**options))


def classify(production, shadow, using=None):
    return corpus.classify("did:plc:a", production, shadow, using or visibility())[:2]


def itemised(handle="a.test", labels=(), **changes):
    actor = {"did": "did:plc:a", "handle": handle, "labels": list(labels), "pds": "https://pds.test", "pds_host": "pds.test", "quality_score": 1.0}
    actor.update(changes)
    return views.from_actor(actor, sha="s")


def hiding(value="spam", **extra):
    return {"src": MOD, "uri": "did:plc:a", "val": value, "cts": "2026-01-01T00:00:00Z", **extra}


class ClassifyTests(unittest.TestCase):
    def doc(self, **changes):
        return Doc("x" + json.dumps(changes, sort_keys=True), document("did:plc:a", "a.test", **changes)["fields"])

    def test_equal_documents_and_version_only_changes_are_not_differences(self):
        self.assertEqual(classify(self.doc(), self.doc(version=9)), ([], []))

    def test_presence(self):
        live = self.doc()
        self.assertEqual(classify(live, corpus.ABSENT)[0], [corpus.ONLY_IN_PRODUCTION])
        self.assertEqual(classify(corpus.ABSENT, live)[0], [corpus.ONLY_IN_SHADOW])
        self.assertEqual(classify(live, self.doc(deleted=True))[0], [corpus.ONLY_IN_PRODUCTION])
        self.assertEqual(classify(self.doc(deleted=True), live)[0], [corpus.ONLY_IN_SHADOW])
        self.assertEqual(classify(self.doc(deleted=True), corpus.ABSENT), ([], []))

    def test_field_classes(self):
        base = self.doc()
        cases = {
            corpus.PDS_DIFFERS: self.doc(pds_host="other.test"),
            corpus.QUALITY_SCORE_DIFFERS: self.doc(score=2.0),
            corpus.PROFILE_DIFFERS: self.doc(profile="q"),
        }
        for expected, changed in cases.items():
            self.assertEqual(classify(base, changed), ([expected], []))
        moved = Doc("y", document("did:plc:a", "b.test")["fields"])
        self.assertEqual(classify(base, moved)[0], [corpus.HANDLE_DIFFERS])

    def test_one_did_can_carry_several_classes(self):
        changed = Doc("z", document("did:plc:a", "b.test", hidden=True, pds_host="other.test", profile="q")["fields"])
        self.assertEqual(
            classify(self.doc(), changed),
            ([corpus.HANDLE_DIFFERS, corpus.PDS_DIFFERS, corpus.VISIBILITY_DIFFERS, corpus.PROFILE_DIFFERS], [corpus.STORED_HIDDEN_DIFFERS]),
        )

    def test_raw_pds_with_same_host_is_only_a_note(self):
        left = self.doc()
        fields = dict(left.fields, pds="https://pds.test/")
        self.assertEqual(classify(left, Doc("r", fields)), ([], [corpus.PDS_FORM_DIFFERS]))

    def test_without_fields_only_the_hash_is_comparable(self):
        self.assertEqual(classify(Doc("a"), Doc("b"))[0], [corpus.DOCUMENT_HASH_DIFFERS])
        self.assertEqual(classify(Doc("a"), Doc("a"))[0], [])
        self.assertEqual(classify(Doc("a"), corpus.ABSENT)[0], [corpus.ONLY_IN_PRODUCTION])

    def test_every_class_is_identity_level_or_refresh_derived(self):
        self.assertEqual(set(corpus.CLASSES), set(corpus.IDENTITY_CLASSES) | set(corpus.REFRESH_CLASSES))
        self.assertFalse(set(corpus.IDENTITY_CLASSES) & set(corpus.REFRESH_CLASSES))
        self.assertFalse(set(corpus.NOTES) & set(corpus.CLASSES))


class VisibilityTests(unittest.TestCase):
    """What a search returns, not the stored flag, decides `visibility_differs`."""

    def test_a_stored_flag_that_disagrees_is_a_note_when_both_sides_would_be_returned(self):
        self.assertEqual(classify(itemised(), itemised(hidden=True)), ([], [corpus.STORED_HIDDEN_DIFFERS]))

    def test_a_hiding_label_on_one_side_is_a_visibility_difference(self):
        classes, notes = classify(itemised(), itemised(labels=[hiding()]))
        self.assertEqual((classes, notes), ([corpus.VISIBILITY_DIFFERS, corpus.LABELS_DIFFER], []))
        entry = corpus.difference("did:plc:a", itemised(), itemised(labels=[hiding()]), visibility())
        self.assertEqual(entry["returned"], {"production": True, "shadow": False})

    def test_labels_that_do_not_hide_leave_visibility_alone(self):
        for label in (hiding("!no-unauthenticated"), hiding(src="did:plc:other"), hiding(exp="2026-11-10T00:00:00Z")):
            self.assertEqual(classify(itemised(), itemised(labels=[label])), ([corpus.LABELS_DIFFER], []), label)

    def test_an_invalid_handle_is_not_returned(self):
        self.assertEqual(classify(itemised(), itemised("handle.invalid"))[0], [corpus.HANDLE_DIFFERS, corpus.VISIBILITY_DIFFERS])
        self.assertEqual(classify(itemised("handle.invalid", hidden=True), itemised("Handle.Invalid")), ([corpus.HANDLE_DIFFERS], [corpus.STORED_HIDDEN_DIFFERS]))

    def test_a_blocked_domain_hides_both_sides_whatever_their_flags_say(self):
        blocked = visibility(policy(domains=["dadavidtseng.com"], source="snapshot"))
        pair = itemised("x.pds.dadavidtseng.com"), itemised("x.pds.dadavidtseng.com", hidden=True)
        self.assertEqual(classify(*pair, blocked), ([], [corpus.STORED_HIDDEN_DIFFERS]))
        moved = itemised("x.pds.dadavidtseng.com"), itemised("x.elsewhere.test")
        self.assertEqual(classify(*moved, blocked)[0], [corpus.HANDLE_DIFFERS, corpus.VISIBILITY_DIFFERS])
        self.assertEqual(classify(*moved)[0], [corpus.HANDLE_DIFFERS])

    def test_an_override_is_per_did_and_ends_a_label_difference_in_visibility(self):
        sides = itemised(), itemised(labels=[hiding()])
        shown = visibility(policy({"did:plc:a": False}, source="snapshot"))
        self.assertEqual(classify(*sides, shown), ([corpus.LABELS_DIFFER], []))
        hidden = visibility(policy({"did:plc:a": True}, source="snapshot"))
        self.assertEqual(classify(*sides, hidden), ([corpus.LABELS_DIFFER], []))

    def test_a_shadow_records_override_stands_in_only_without_a_policy_snapshot(self):
        shadow = itemised(labels=[hiding()])
        shadow.fields["override"] = "show"
        self.assertEqual(classify(itemised(), shadow), ([corpus.LABELS_DIFFER], []))
        supplied = visibility(policy(source="snapshot"))
        self.assertEqual(classify(itemised(), shadow, supplied)[0], [corpus.VISIBILITY_DIFFERS, corpus.LABELS_DIFFER])

    def test_labels_the_server_refuses_are_a_note_not_a_guess(self):
        refused = itemised(labels=[hiding(exp="2026")])
        self.assertEqual(classify(itemised(), refused), ([corpus.LABELS_DIFFER], [corpus.VISIBILITY_UNEVALUATED]))

    def test_a_view_without_itemised_labels_is_judged_by_its_stored_flag(self):
        plain = Doc("p", document("did:plc:a", "a.test")["fields"])
        flagged = Doc("s", document("did:plc:a", "a.test", hidden=True)["fields"])
        self.assertEqual(classify(plain, flagged), ([corpus.VISIBILITY_DIFFERS], [corpus.STORED_HIDDEN_DIFFERS]))


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
        self.assertEqual(result["notes"][corpus.STORED_HIDDEN_DIFFERS]["count"], 1)
        self.assertEqual(result["differing_dids"], 265)
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

    def test_a_checkpoint_interval_skips_writes_but_stops_still_resume_exactly(self):
        a, b = pair()
        del b[250]
        whole = run(FakeBackend("p", a), FakeBackend("s", b))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            first = run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path, max_pages=4, checkpoint_interval=3600)
            self.assertEqual(json.loads(path.read_text())["cursor"], first["cursor"])
            self.assertEqual(json.loads(path.read_text())["pages"], 4)
            second = run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path, checkpoint_interval=3600)
            self.assertTrue(second["resumed"] and second["complete"])
            self.assertTrue(json.loads(path.read_text())["complete"])
            for key in ("dids_walked", "live", "flagged_dids", "classes"):
                self.assertEqual(second[key], whole[key])
            self.assertEqual(Path(str(path) + ".dids").read_text().split(), [did(250)])

    def test_a_checkpoint_writes_at_most_once_per_interval_unless_told(self):
        now = [0.0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            checkpoint = corpus.Checkpoint(path, {"pair": 1}, interval=60, clock=lambda: now[0])
            written = []
            for step, (seconds, due) in enumerate([(0, True), (30, True), (59, True), (60, True), (61, True), (62, False)]):
                now[0] = seconds
                checkpoint.pages = step
                checkpoint.save(due=due)
                written.append(json.loads(path.read_text())["pages"])
            self.assertEqual(written, [0, 0, 0, 3, 3, 5])

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
            for key in ("dids_walked", "live", "flagged_dids", "classes", "notes", "score_magnitude", "differing_dids"):
                self.assertEqual(second[key], whole[key])
            for change in ({"policy": policy(domains=["blocked.test"], source="snapshot")}, {"sample_size": 6}, {"max_tracked": 7}):
                with self.assertRaises(TransportError):
                    run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path, **change)
            with self.assertRaises(TransportError):
                run(FakeBackend("other", a), FakeBackend("s", b), checkpoint_path=path)

    def test_a_resume_drops_differing_dids_logged_after_the_last_save(self):
        a, b = pair(400)
        for n in (10, 150, 390):
            b[n]["fields"]["handle"] = "moved.test"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            log = Path(str(path) + ".dids")
            run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path, max_pages=2)
            self.assertEqual(log.read_text().split(), [did(10), did(150)])
            with open(log, "a") as stream:
                stream.write("did:plc:written-after-the-save\n")
            done = run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path)
            self.assertEqual(log.read_text().split(), [did(10), did(150), did(390)])
            self.assertEqual(done["flagged_dids"], [did(10), did(150), did(390)])
            log.write_text("")
            path.write_text(path.read_text().replace('"complete":true', '"complete":false'))
            with self.assertRaises(TransportError):
                run(FakeBackend("p", a), FakeBackend("s", b), checkpoint_path=path)

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

    def test_identity_differences_beyond_the_tracking_cap_are_counted(self):
        a, b = pair(300)
        result = run(FakeBackend("p", a), FakeBackend("s", b[:100]), max_tracked=50)
        self.assertEqual(result["differing_dids"], 200)
        self.assertEqual(result["identity_differences"], {"tracked": 50, "untracked": 150, "cap": 50})
        missing = result["classes"][corpus.ONLY_IN_PRODUCTION]
        self.assertEqual((missing["count"], missing["counted"], len(missing["sample"])), (200, "during the walk", 5))

    def test_refresh_differences_are_counted_in_full_and_never_fill_the_cap(self):
        a, b = pair(300)
        for row in b:
            row["fields"]["quality_score"] = 2.0
        b[7]["fields"]["handle"] = "moved.test"
        result = run(FakeBackend("p", a), FakeBackend("s", b), max_tracked=3)
        self.assertEqual(result["identity_differences"], {"tracked": 1, "untracked": 0, "cap": 3})
        scores = result["classes"][corpus.QUALITY_SCORE_DIFFERS]
        self.assertEqual((scores["level"], scores["count"], len(scores["sample"])), ("refresh", 300, 5))
        self.assertEqual([item["did"] for item in scores["sample"]], corpus.sample([did(n) for n in range(300)], 5))
        self.assertEqual(result["differences_kept"], 6)
        self.assertEqual(len(result["flagged_dids"]), 300)

    def test_notes_alone_do_not_make_a_did_differ(self):
        a, b = pair(40)
        for n in range(30):
            b[n]["fields"]["pds"] = "https://pds.test/"
        result = run(FakeBackend("p", a), FakeBackend("s", b))
        self.assertEqual((result["differing_dids"], result["flagged_dids"]), (0, []))
        note = result["notes"][corpus.PDS_FORM_DIFFERS]
        self.assertEqual((note["count"], len(note["sample"])), (30, corpus.NOTE_SAMPLE_SIZE))


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
        self.assertEqual(result["differences_kept"], 3)
        self.assertEqual(result["transient_differences"], 2)
        self.assertEqual(result["flagged_dids"], [did(20)])
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["count"], 1)
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["counted"], "after the re-read")
        self.assertEqual(result["classes"][corpus.ONLY_IN_PRODUCTION]["count"], 0)
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


class BreakdownTests(unittest.TestCase):
    def entry(self, n, handle, pds_host, **shadow):
        production = Doc("p", document(did(n), handle, pds_host=pds_host)["fields"])
        changed = Doc("s", document(did(n), handle, pds_host=pds_host, **shadow)["fields"]) if shadow else corpus.ABSENT
        return corpus.difference(did(n), production, changed, visibility())

    def test_each_class_is_counted_by_handle_kind_pds_kind_and_host(self):
        differences = [
            self.entry(1, "a.bsky.social", "morel.us-east.host.bsky.network", score=3.0),
            self.entry(2, "B.BSKY.SOCIAL", "morel.us-east.host.bsky.network", score=3.0),
            self.entry(3, "c.example.com", "pds.example.com", score=3.0),
            self.entry(4, "d.example.com", "bsky.social", score=3.0),
            self.entry(5, "handle.invalid", "", score=3.0),
            self.entry(6, "e.example.com", "pds.example.com"),
        ]
        gone = Doc("p", document(did(7), "", deleted=True)["fields"]), Doc("s", document(did(7), "f.custom.test", pds_host="solo.test")["fields"])
        differences.append(corpus.difference(did(7), *gone, visibility()))
        summary = recorded(differences)
        scores = summary["classes"][corpus.QUALITY_SCORE_DIFFERS]["breakdown"]
        self.assertEqual(scores["handle_kind"], {"custom domain": 2, "hosted handle": 2, "no handle": 1})
        self.assertEqual(scores["pds_kind"], {"bluesky pds": 3, "independent pds": 1, "no pds": 1})
        self.assertEqual(scores["top_pds_hosts"], [
            {"host": "morel.us-east.host.bsky.network", "count": 2}, {"host": "", "count": 1},
            {"host": "bsky.social", "count": 1}, {"host": "pds.example.com", "count": 1},
        ])
        self.assertEqual(scores["other_pds_hosts"], {"hosts": 0, "count": 0})
        missing = summary["classes"][corpus.ONLY_IN_PRODUCTION]["breakdown"]
        self.assertEqual((missing["handle_kind"], missing["pds_kind"]), ({"custom domain": 1}, {"independent pds": 1}))
        extra = summary["classes"][corpus.ONLY_IN_SHADOW]["breakdown"]
        self.assertEqual(extra["top_pds_hosts"], [{"host": "solo.test", "count": 1}])
        self.assertEqual(summary["classes"][corpus.HANDLE_DIFFERS]["breakdown"]["handle_kind"], {})

    def test_hosts_beyond_the_top_ten_are_summed(self):
        detail = recorded([self.entry(n, f"h{n}.test", f"pds{n % 13:02}.test", score=3.0) for n in range(40)])["classes"][corpus.QUALITY_SCORE_DIFFERS]["breakdown"]
        self.assertEqual(len(detail["top_pds_hosts"]), 10)
        self.assertEqual(detail["top_pds_hosts"][0], {"host": "pds00.test", "count": 4})
        self.assertEqual(detail["other_pds_hosts"], {"hosts": 3, "count": 40 - sum(item["count"] for item in detail["top_pds_hosts"])})

    def test_hash_only_differences_have_unknown_kinds(self):
        summary = recorded([corpus.difference(did(1), Doc("a"), Doc("b"), visibility())])
        detail = summary["classes"][corpus.DOCUMENT_HASH_DIFFERS]["breakdown"]
        self.assertEqual((detail["handle_kind"], detail["pds_kind"]), ({"unknown": 1}, {"unknown": 1}))

    def test_report_lists_the_breakdown_for_classes_with_differences(self):
        differences = [self.entry(1, "a.bsky.social", "morel.us-east.host.bsky.network", score=3.0), self.entry(3, "c.example.com", "pds.example.com", score=3.0)]
        result = {"complete": True, "dids_walked": 2, "pages": 1, "live": {"production": 2, "shadow": 2}, **recorded(differences)}
        text = "\n".join(report.corpus_section(result))
        self.assertIn("| quality_score_differs | 1 | 1 | 0 | 0 | 1 | 1 | 0 | 0 |", text)
        self.assertIn("- quality_score_differs: morel.us-east.host.bsky.network 1, pds.example.com 1", text)
        self.assertNotIn("- handle_differs", text)
        self.assertIn("| quality_score_differs | 2 | 2 | 2 |", text)


class StrataTests(unittest.TestCase):
    """Refresh-derived differences are placed by whether the shadow refreshed the record after the threshold."""

    def entry(self, n, production, shadow, refreshed):
        sides = (Doc("p", document(did(n), f"h{n}.test", score=production)["fields"]), Doc("s", document(did(n), f"h{n}.test", score=shadow)["fields"]))
        return corpus.difference(did(n), *sides, visibility(), refreshed)

    def test_counts_samples_and_score_sizes_are_kept_per_stratum(self):
        entries = [self.entry(n, 0.0, 250.0, True) for n in range(40)]
        entries += [self.entry(n, 10.0, 10.5, False) for n in range(40, 60)]
        entries += [self.entry(60, 300.0, 0.0, False), self.entry(61, 300.0, 320.0, True), self.entry(62, 300.0, 380.0, True)]
        summary = recorded(entries, sample_size=8)
        scores = summary["classes"][corpus.QUALITY_SCORE_DIFFERS]
        self.assertEqual(scores["strata"][corpus.REFRESHED], {"count": 42, "sampled": 8, "sample_no_longer_differs": 0})
        self.assertEqual(scores["strata"][corpus.LAGGING], {"count": 21, "sampled": 8, "sample_no_longer_differs": 0})
        self.assertEqual([item["refreshed"] for item in scores["sample"]], [True] * 8 + [False] * 8)
        self.assertEqual(summary["score_magnitude"][corpus.REFRESHED], {"production_zero": 40, "shadow_zero": 0, "under_1": 0, "1_to_50": 1, "50_or_more": 1})
        self.assertEqual(summary["score_magnitude"][corpus.LAGGING], {"production_zero": 0, "shadow_zero": 1, "under_1": 20, "1_to_50": 0, "50_or_more": 0})

    def test_a_sample_is_the_smallest_hashes_however_the_walk_ordered_them(self):
        entries = [self.entry(n, 1.0, 2.0, True) for n in range(200)]
        expected = corpus.sample([did(n) for n in range(200)], 10)
        for order in (entries, list(reversed(entries))):
            found = recorded(order, sample_size=10)["classes"][corpus.QUALITY_SCORE_DIFFERS]["sample"]
            self.assertEqual([item["did"] for item in found], expected)

    def test_only_sampled_and_identity_entries_are_kept_in_detail(self):
        checkpoint = corpus.Checkpoint(None, {}, max_tracked=10, sample_size=4)
        for n in range(500):
            checkpoint.record(self.entry(n, 1.0, 2.0, n % 2 == 0))
        checkpoint.prune()
        self.assertEqual((checkpoint.differing, len(checkpoint.entries)), (500, 8))

    def test_a_shadow_record_without_a_refresh_time_or_a_threshold_counts_as_refreshed(self):
        row = views.Row("k", True, None, None, refreshed_at=50)
        self.assertEqual([corpus.refreshed_after(row, threshold) for threshold in (None, 49, 50)], [True, True, False])
        self.assertTrue(corpus.refreshed_after(views.Row("k", True, None, None), 100))


class SampleTests(unittest.TestCase):
    def test_sample_is_deterministic_and_order_independent(self):
        dids = [did(n) for n in range(200)]
        chosen = corpus.sample(dids, 10)
        self.assertEqual(chosen, corpus.sample(list(reversed(dids)), 10))
        self.assertEqual(len(set(chosen)), 10)
        self.assertEqual(corpus.sample(dids[:3], 10), sorted(dids[:3], key=corpus.sample_key))


if __name__ == "__main__":
    unittest.main()
