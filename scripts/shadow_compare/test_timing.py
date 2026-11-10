import io
import json
import tempfile
import unittest
from pathlib import Path

import arbiter
import bundle
import corpus
import gates
import report
import sources
import store
from fakes import FakeBackend, FakeStore, admin_document, store_record, stored_actor
from test_arbiter import authority
from transport import Budget, Client

SECRET = "store-s3cr3t-0123456789"
WATERMARK = 1000
SKEW = 120


def did(n):
    return f"did:plc:{n:05}"


class Pair(unittest.TestCase):
    def setUp(self):
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        self.root = Path(stack.name)
        self.rows = [stored_actor(did(n), f"h{n}.test") for n in range(30)]

    def production(self, rows=None, watermark=WATERMARK):
        raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in (rows or self.rows))
        out = self.root / "sorted.jsonl"
        bundle.sort(io.BytesIO(raw), out, self.root / "runs", run_records=8)
        return bundle.BundleJsonl(out, watermark=watermark, build_id="b1")

    def shadow(self, records):
        fake = FakeStore(records, SECRET)
        self.addCleanup(fake.close)
        client = Client("shadow store", fake.base, Budget(1000, 10**7), secret=SECRET, secret_prefix=store.SECRET_PREFIX)
        self.addCleanup(client.close)
        return fake, store.StoreHttp(client)

    def run_pair(self, production, shadow, sleep=lambda seconds: None):
        return corpus.run_sources(production, shadow, checkpoint_path=None, page_size=7, max_pages=None, max_tracked=100,
                                  settle_delay=5, sample_size=5, watermark_skew=SKEW, sleep=sleep)

    def counts(self, result, part):
        return {name: sorted(i["did"] for i in entry["sample"]) for name, entry in result["timing"][part]["classes"].items() if entry["count"]}


class ClassificationTests(Pair):
    def test_changed_after_the_cutoff_is_newer_and_everything_else_is_unchanged(self):
        def entry(changed_at):
            fields = None if changed_at == "absent" else {"deleted": False, "handle": "b.test", **({} if changed_at is None else {"changed_at": changed_at})}
            shadow = corpus.ABSENT if fields is None else corpus.Doc("s", fields)
            return corpus.difference("did:plc:a", corpus.Doc("p", {"deleted": False, "handle": "a.test"}), shadow)

        cutoff = WATERMARK - SKEW
        self.assertEqual(corpus.timing(entry(cutoff + 1), cutoff), corpus.NEWER_IN_SHADOW)
        self.assertEqual(corpus.timing(entry(WATERMARK + 50), cutoff), corpus.NEWER_IN_SHADOW)
        for unchanged in (cutoff, cutoff - 1, 0, None, "absent"):
            self.assertEqual(corpus.timing(entry(unchanged), cutoff), corpus.UNCHANGED_IN_SHADOW, unchanged)

    def test_differences_split_by_when_the_shadow_last_changed(self):
        records = {row["did"]: store_record(row, 500) for row in self.rows}
        records[did(1)].update(handle="renamed-late.test", updated_at=WATERMARK + 30)
        records[did(2)].update(handle="renamed-early.test", updated_at=WATERMARK - SKEW)
        records[did(3)].update(status="deleted", updated_at=WATERMARK + 1)
        records[did(4)].update(quality_score=5.0, updated_at=WATERMARK - SKEW + 1)
        records[did(5)].update(hidden=True, updated_at=400)
        del records[did(6)]
        records[did(40)] = store_record(stored_actor(did(40), "born-late.test"), WATERMARK + 5)
        records[did(41)] = store_record(stored_actor(did(41), "phantom.test"), 300)
        _, shadow = self.shadow(records.values())
        result = self.run_pair(self.production(), shadow)
        self.assertEqual(self.counts(result, corpus.NEWER_IN_SHADOW), {
            corpus.HANDLE_DIFFERS: [did(1)], corpus.ONLY_IN_PRODUCTION: [did(3)],
            corpus.QUALITY_SCORE_DIFFERS: [did(4)], corpus.ONLY_IN_SHADOW: [did(40)],
        })
        self.assertEqual(self.counts(result, corpus.UNCHANGED_IN_SHADOW), {
            corpus.HANDLE_DIFFERS: [did(2)], corpus.VISIBILITY_DIFFERS: [did(5)],
            corpus.ONLY_IN_PRODUCTION: [did(6)], corpus.ONLY_IN_SHADOW: [did(41)],
        })
        self.assertEqual(result["differing_dids"], 8)
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["count"], 2)
        self.assertEqual(result["snapshot"], {
            "watermark": WATERMARK, "skew_s": SKEW, "cutoff": WATERMARK - SKEW,
            "shadow": {"records": 31, "with_change_time": 31, "changed_since_cutoff": 4},
        })
        self.assertEqual(len(result["flagged_dids"]), 8)

    def test_a_record_that_changes_during_the_settle_delay_is_read_again(self):
        records = {row["did"]: store_record(row, 500) for row in self.rows}
        records[did(1)].update(handle="lagging.test")
        records[did(2)].update(handle="stale.test")
        fake, shadow = self.shadow(records.values())

        def settle(seconds):
            fake.records[did(1)].update(handle="h1.test", updated_at=WATERMARK + 9)
            fake.records[did(2)].update(handle="moved-on.test", updated_at=WATERMARK + 9)

        result = self.run_pair(self.production(), shadow, sleep=settle)
        self.assertEqual(result["transient_differences"], 1)
        self.assertEqual(self.counts(result, corpus.NEWER_IN_SHADOW), {corpus.HANDLE_DIFFERS: [did(2)]})
        self.assertEqual(self.counts(result, corpus.UNCHANGED_IN_SHADOW), {})

    def test_a_shadow_without_change_times_has_every_difference_unchanged(self):
        rows = [dict(row) for row in self.rows]
        rows[1]["handle"] = "renamed.test"
        shadow = sources.TantivyAdmin(FakeBackend("s", [admin_document(row) for row in rows]))
        result = self.run_pair(self.production(), shadow)
        self.assertEqual(self.counts(result, corpus.UNCHANGED_IN_SHADOW), {corpus.HANDLE_DIFFERS: [did(1)]})
        self.assertEqual(result["snapshot"]["shadow"], {"records": 30, "with_change_time": 0, "changed_since_cutoff": 0})

    def test_two_live_sources_are_not_split(self):
        docs = [admin_document(row) for row in self.rows]
        result = corpus.run(FakeBackend("p", docs), FakeBackend("s", docs[:-1]), checkpoint_path=None, page_size=7, max_pages=None,
                            max_tracked=100, settle_delay=5, sample_size=5, sleep=lambda seconds: None)
        self.assertNotIn("timing", result)
        self.assertNotIn("snapshot", result)

    def test_an_interrupted_walk_resumes_with_its_counts(self):
        records = [store_record(row, WATERMARK + 1) for row in self.rows]
        _, shadow = self.shadow(records)
        production = self.production()
        path = self.root / "checkpoint.json"
        settings = dict(checkpoint_path=path, page_size=7, max_tracked=100, settle_delay=5, sample_size=5, watermark_skew=SKEW, sleep=lambda s: None)
        first = corpus.run_sources(production, shadow, max_pages=2, **settings)
        self.assertFalse(first["complete"])
        self.assertNotIn("timing", first)
        second = corpus.run_sources(production, shadow, max_pages=None, **settings)
        self.assertEqual(second["snapshot"]["shadow"], {"records": 30, "with_change_time": 30, "changed_since_cutoff": 30})


def part(**classes):
    table = {name: {"count": 0, "sample": []} for name in corpus.CLASSES}
    for name, (count, labels) in classes.items():
        table[name] = {"count": count, "sample": [{}] * sum(labels.values()), "arbiter_labels": labels}
    return {"differing_dids": sum(count for count, _ in classes.values()), "classes": table}


def snapshot_result(unchanged=None, newer=None, arbiter_ran=True, records=1000, changed=10):
    unchanged, newer = unchanged or part(), newer or part()
    return {
        "complete": True, "stop_reason": "complete", "cursor": "", "rechecked": True, "untracked_differences": 0,
        "arbiter_ran": arbiter_ran, "classes": part()["classes"],
        "snapshot": {"watermark": WATERMARK, "skew_s": SKEW, "cutoff": WATERMARK - SKEW,
                     "shadow": {"records": records, "with_change_time": records, "changed_since_cutoff": changed}},
        "timing": {corpus.UNCHANGED_IN_SHADOW: unchanged, corpus.NEWER_IN_SHADOW: newer},
    }


class GateTests(unittest.TestCase):
    def verdict(self, **options):
        return gates.corpus_gate(snapshot_result(**options))

    def test_no_differences_passes(self):
        self.assertEqual(self.verdict(), {"status": gates.PASS, "reasons": []})

    def test_divergence_must_be_zero_or_explained_in_the_shadows_favour(self):
        self.assertEqual(self.verdict(unchanged=part(handle_differs=(4, {arbiter.SHADOW_RIGHT: 3, arbiter.BOTH_STALE: 1})))["status"], gates.PASS)
        for label in (arbiter.PRODUCTION_RIGHT, arbiter.UNDETERMINED):
            verdict = self.verdict(unchanged=part(handle_differs=(4, {arbiter.SHADOW_RIGHT: 3, label: 1})))
            self.assertEqual(verdict["status"], gates.FAIL)
            self.assertIn("unchanged in shadow since the watermark, handle_differs", verdict["reasons"][0])
        unjudged = self.verdict(unchanged=part(labels_differ=(2, {arbiter.UNDETERMINED: 2})))
        self.assertEqual(unjudged["status"], gates.FAIL)
        self.assertEqual(self.verdict(unchanged=part(handle_differs=(4, {})), arbiter_ran=False)["status"], gates.FAIL)

    def test_newer_in_shadow_passes_when_the_sample_never_shows_the_shadow_wrong(self):
        newer = part(handle_differs=(900, {arbiter.SHADOW_RIGHT: 24, arbiter.BOTH_STALE: 1}), labels_differ=(50, {arbiter.UNDETERMINED: 25}),
                     quality_score_differs=(7, {arbiter.UNDETERMINED: 7}), other_field_differs=(3, {arbiter.UNDETERMINED: 3}))
        self.assertEqual(self.verdict(newer=newer), {"status": gates.PASS, "reasons": []})

    def test_newer_in_shadow_fails_when_production_is_right_or_a_judged_class_is_unjudged(self):
        wrong = self.verdict(newer=part(handle_differs=(900, {arbiter.SHADOW_RIGHT: 24, arbiter.PRODUCTION_RIGHT: 1})))
        self.assertEqual(wrong["status"], gates.FAIL)
        self.assertIn("newer in shadow, handle_differs: 900 differences, sample of 25 has 1 production is right", wrong["reasons"][0])
        for name in gates.ARBITER_JUDGED_CLASSES:
            unjudged = self.verdict(newer=part(**{name: (5, {arbiter.SHADOW_RIGHT: 4, arbiter.UNDETERMINED: 1})}))
            self.assertEqual(unjudged["status"], gates.FAIL, name)
        profile = self.verdict(newer=part(other_field_differs=(5, {arbiter.PRODUCTION_RIGHT: 1})))
        self.assertEqual(profile["status"], gates.FAIL)

    def test_newer_in_shadow_needs_an_arbiter_sample(self):
        verdict = self.verdict(newer=part(pds_differs=(5, {})), arbiter_ran=False)
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertIn("no arbiter sample", verdict["reasons"][0])

    def test_a_shadow_that_mostly_changed_since_the_snapshot_fails(self):
        self.assertEqual(self.verdict(records=1000, changed=250)["status"], gates.PASS)
        verdict = self.verdict(records=1000, changed=251)
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertIn("251 of 1000 shadow records changed", verdict["reasons"][0])

    def test_incomplete_snapshot_walk_fails(self):
        result = snapshot_result()
        del result["timing"]
        result.update(complete=False, stop_reason="budget", rechecked=False)
        self.assertEqual(gates.corpus_gate(result)["status"], gates.FAIL)


class ArbiterAndReportTests(Pair):
    def test_partition_samples_are_labelled_and_reported(self):
        records = {row["did"]: store_record(row, 500) for row in self.rows}
        records[did(1)].update(handle="new.test", updated_at=WATERMARK + 30)
        records[did(2)].update(handle="wrong.test", updated_at=200)
        _, shadow = self.shadow(records.values())
        result = self.run_pair(self.production(), shadow)
        truth = authority(
            {did(1): ("new.test", "pds.test"), did(2): ("h2.test", "pds.test")},
            {did(1): {"handle": "new.test"}, did(2): {"handle": "h2.test"}},
        )
        arbiter.label_samples(result, truth)
        newer = result["timing"][corpus.NEWER_IN_SHADOW]["classes"][corpus.HANDLE_DIFFERS]
        unchanged = result["timing"][corpus.UNCHANGED_IN_SHADOW]["classes"][corpus.HANDLE_DIFFERS]
        self.assertEqual(newer["arbiter_labels"], {arbiter.SHADOW_RIGHT: 1})
        self.assertEqual(unchanged["arbiter_labels"], {arbiter.PRODUCTION_RIGHT: 1})
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["arbiter_labels"], {arbiter.SHADOW_RIGHT: 1, arbiter.PRODUCTION_RIGHT: 1})
        verdict = gates.corpus_gate(result)
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertEqual(len(verdict["reasons"]), 1)
        self.assertIn("unchanged in shadow", verdict["reasons"][0])
        text = "\n".join(report.corpus_section(result))
        self.assertIn("snapshot at watermark 1000", text)
        self.assertIn("1 of 30 shadow records changed since then", text)
        self.assertIn("### Unchanged in the shadow since the watermark", text)
        self.assertIn("### Newer in the shadow than the snapshot", text)


if __name__ == "__main__":
    unittest.main()
