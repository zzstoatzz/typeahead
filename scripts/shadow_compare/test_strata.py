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
from fakes import FakeBackend, FakeStore, Network, admin_document, store_record, stored_actor, walk_settings
from transport import Budget, Client

SECRET = "store-s3cr3t-0123456789"
WATERMARK = 1000
PROFILE = {"followersCount": 1234, "followsCount": 321, "postsCount": 4567, "createdAt": "2023-01-01T00:00:00.000Z"}
SCORE = 442.36


def did(n):
    return f"did:plc:{n:05}"


class Pair(unittest.TestCase):
    """Production as a bundle at a watermark, the shadow as a live store that refreshes on its own timetable."""

    def setUp(self):
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        self.root = Path(stack.name)
        self.rows = [stored_actor(did(n), f"h{n}.test", score=SCORE) for n in range(30)]

    def production(self, rows=None, watermark=WATERMARK):
        raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in (rows or self.rows))
        out = self.root / "sorted.jsonl"
        bundle.sort(io.BytesIO(raw), out, self.root / "runs", run_records=8)
        return bundle.BundleJsonl(out, watermark=watermark, build_id="b1")

    def records(self, refreshed_at=500):
        return {row["did"]: dict(store_record(row, 500), profile_checked_at=refreshed_at) for row in self.rows}

    def shadow(self, records):
        fake = FakeStore(records, SECRET)
        self.addCleanup(fake.close)
        client = Client("shadow store", fake.base, Budget(1000, 10**7), secret=SECRET, secret_prefix=store.SECRET_PREFIX)
        self.addCleanup(client.close)
        return fake, store.StoreHttp(client)

    def run_pair(self, production, shadow, **options):
        return corpus.run_sources(production, shadow, **walk_settings(page_size=7, **options))

    def strata(self, result, name):
        return {stratum: (detail["count"], detail["sampled"]) for stratum, detail in result["classes"][name]["strata"].items()}


class PlacementTests(Pair):
    def test_refresh_differences_are_placed_by_when_the_shadow_last_refreshed(self):
        records = self.records()
        records[did(1)].update(quality_score=5.0, profile_checked_at=WATERMARK + 30, updated_at=WATERMARK + 30)
        records[did(2)].update(quality_score=5.0, profile_checked_at=WATERMARK)
        records[did(3)].update(quality_score=5.0, display_name="Renamed", profile_checked_at=WATERMARK + 1)
        records[did(4)].update(quality_score=5.0)
        del records[did(4)]["profile_checked_at"]
        records[did(5)].update(handle="renamed.test", profile_checked_at=400)
        _, shadow = self.shadow(records.values())
        result = self.run_pair(self.production(), shadow)
        self.assertEqual(self.strata(result, corpus.QUALITY_SCORE_DIFFERS), {corpus.REFRESHED: (3, 3), corpus.LAGGING: (1, 1)})
        self.assertEqual(self.strata(result, corpus.DISPLAY_NAME_DIFFERS), {corpus.REFRESHED: (1, 1), corpus.LAGGING: (0, 0)})
        placed = {item["did"]: item["refreshed"] for item in result["classes"][corpus.QUALITY_SCORE_DIFFERS]["sample"]}
        self.assertEqual(placed, {did(1): True, did(2): False, did(3): True, did(4): True})
        self.assertEqual(result["classes"][corpus.HANDLE_DIFFERS]["count"], 1)
        self.assertEqual(result["differing_dids"], 5)
        self.assertEqual(result["snapshot"], {
            "watermark": WATERMARK, "bootstrap_at": None, "refresh_threshold": WATERMARK,
            "shadow": {"records": 30, "with_change_time": 30, "changed_since_watermark": 1, "with_refresh_time": 29, "refreshed_since_watermark": 2},
        })

    def test_a_shadow_loaded_after_the_watermark_counts_refreshes_from_its_load(self):
        records = self.records(refreshed_at=WATERMARK + 100)
        records[did(1)].update(quality_score=5.0)
        records[did(2)].update(quality_score=5.0, profile_checked_at=WATERMARK + 101)
        _, shadow = self.shadow(records.values())
        strict = self.run_pair(self.production(), shadow)
        self.assertEqual(self.strata(strict, corpus.QUALITY_SCORE_DIFFERS), {corpus.REFRESHED: (2, 2), corpus.LAGGING: (0, 0)})
        loaded = self.run_pair(self.production(), shadow, bootstrap_at=WATERMARK + 100)
        self.assertEqual(self.strata(loaded, corpus.QUALITY_SCORE_DIFFERS), {corpus.REFRESHED: (1, 1), corpus.LAGGING: (1, 1)})
        self.assertEqual(loaded["snapshot"]["refresh_threshold"], WATERMARK + 100)
        self.assertEqual(loaded["snapshot"]["shadow"]["refreshed_since_watermark"], 1)

    def test_a_sampled_record_that_agrees_after_the_settle_delay_leaves_the_sample(self):
        records = self.records()
        records[did(1)].update(quality_score=5.0)
        records[did(2)].update(quality_score=6.0)
        records[did(3)].update(handle="lagging.test")
        fake, shadow = self.shadow(records.values())

        def settle(seconds):
            fake.records[did(1)].update(quality_score=SCORE)
            fake.records[did(3)].update(handle="h3.test")

        result = self.run_pair(self.production(), shadow, sleep=settle)
        scores = result["classes"][corpus.QUALITY_SCORE_DIFFERS]
        self.assertEqual((scores["count"], scores["counted"], [item["did"] for item in scores["sample"]]), (2, "during the walk", [did(2)]))
        self.assertEqual(scores["strata"][corpus.LAGGING], {"count": 2, "sampled": 1, "sample_no_longer_differs": 1})
        self.assertEqual((result["transient_differences"], result["classes"][corpus.HANDLE_DIFFERS]["count"]), (1, 0))
        self.assertEqual(result["flagged_dids"], [did(2)])

    def test_a_shadow_without_refresh_times_is_held_to_the_stricter_reading(self):
        rows = [dict(row) for row in self.rows]
        rows[1]["quality_score"] = 5.0
        shadow = sources.TantivyAdmin(FakeBackend("s", [admin_document(row) for row in rows]))
        result = self.run_pair(self.production(), shadow)
        self.assertEqual(self.strata(result, corpus.QUALITY_SCORE_DIFFERS), {corpus.REFRESHED: (1, 1), corpus.LAGGING: (0, 0)})
        self.assertEqual(result["snapshot"]["shadow"]["with_refresh_time"], 0)

    def test_two_live_sources_have_no_watermark_and_no_lagging_records(self):
        docs = [admin_document(row) for row in self.rows]
        changed = [dict(doc, fields=dict(doc["fields"], quality_score=5.0)) for doc in docs]
        result = corpus.run(FakeBackend("p", docs), FakeBackend("s", changed), **walk_settings(page_size=7))
        self.assertNotIn("snapshot", result)
        self.assertEqual(self.strata(result, corpus.QUALITY_SCORE_DIFFERS), {corpus.REFRESHED: (30, 5), corpus.LAGGING: (0, 0)})
        self.assertIn("Both sides are live", "\n".join(report.corpus_section(result)))

    def test_an_interrupted_walk_resumes_with_its_counts_and_samples(self):
        records = self.records(refreshed_at=WATERMARK + 1)
        for n in range(0, 30, 2):
            records[did(n)].update(quality_score=5.0)
        _, shadow = self.shadow(records.values())
        production = self.production()
        whole = self.run_pair(production, shadow)
        path = self.root / "checkpoint.json"
        first = self.run_pair(production, shadow, checkpoint_path=path, max_pages=2)
        self.assertFalse(first["complete"])
        saved = json.loads(path.read_text())
        self.assertEqual(len(saved["entries"]), 5)
        second = self.run_pair(production, shadow, checkpoint_path=path)
        self.assertTrue(second["resumed"] and second["complete"])
        for key in ("classes", "score_magnitude", "snapshot", "flagged_dids", "differing_dids"):
            self.assertEqual(second[key], whole[key], key)
        self.assertEqual(second["snapshot"]["shadow"]["refreshed_since_watermark"], 30)


class ArbiterAndReportTests(Pair):
    def test_a_real_shadow_defect_fails_and_refresh_lag_does_not(self):
        records = self.records()
        for n in range(10):
            records[did(n)].update(quality_score=0.0)
        records[did(20)].update(handle="handle.invalid", profile_checked_at=WATERMARK + 5)
        records[did(21)].update(handle="moved.test")
        records[did(22)].update(hidden=True)
        _, shadow = self.shadow(records.values())
        network = Network()
        for row in self.rows:
            network.account(row["did"], row["handle"], profile=PROFILE)
        network.account(did(21), "moved.test", profile=PROFILE)
        result = self.run_pair(self.production(), shadow, sample_size=6)
        arbiter.label_samples(result, network.authority(now_ms=1_794_300_000_000))
        handles = result["classes"][corpus.HANDLE_DIFFERS]
        self.assertEqual(handles["arbiter_labels"], {arbiter.SHADOW_RIGHT: 1, arbiter.PRODUCTION_RIGHT: 1})
        self.assertEqual(result["classes"][corpus.VISIBILITY_DIFFERS]["arbiter_labels"], {arbiter.PRODUCTION_RIGHT: 1})
        lagging = result["classes"][corpus.QUALITY_SCORE_DIFFERS]["strata"][corpus.LAGGING]
        self.assertEqual((lagging["count"], lagging["arbiter_labels"], lagging["estimated_production_right"]), (10, {arbiter.PRODUCTION_RIGHT: 6}, 10))
        self.assertEqual(result["notes"][corpus.STORED_HIDDEN_DIFFERS]["count"], 1)
        verdict = gates.corpus_gate(result)
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertEqual([reason.split(":")[0] for reason in verdict["reasons"]], [corpus.HANDLE_DIFFERS, corpus.VISIBILITY_DIFFERS])
        text = "\n".join(report.corpus_section(result))
        self.assertIn("snapshot at watermark 1000", text)
        self.assertIn("1 of 30 shadow records were", text)
        self.assertIn("| handle_differs | 2 | 2 | 1 production is right, 1 shadow is right |", text)
        self.assertIn("| quality_score_differs | 10 | 0 | 0 |  | 10 | 6 | 6 production is right | 10 |", text)
        self.assertIn("| not refreshed by the shadow since the watermark | 0 | 10 | 0 | 0 | 0 |", text)
        self.assertIn("| stored_hidden_differs | 1 |", text)

    def test_refresh_lag_alone_passes(self):
        records = self.records()
        for n in range(10):
            records[did(n)].update(quality_score=0.0)
        records[did(21)].update(handle="moved.test", quality_score=5.0, profile_checked_at=WATERMARK + 5)
        _, shadow = self.shadow(records.values())
        network = Network()
        for row in self.rows:
            network.account(row["did"], row["handle"], profile=PROFILE)
        network.account(did(21), "moved.test", profile={**PROFILE, "followersCount": 0, "postsCount": 0, "createdAt": ""})
        result = self.run_pair(self.production(), shadow)
        arbiter.label_samples(result, network.authority(now_ms=1_794_300_000_000))
        self.assertEqual(gates.corpus_gate(result), {"status": gates.PASS, "reasons": []})

    def test_a_shadow_wrong_about_what_it_just_refreshed_fails(self):
        records = self.records(refreshed_at=WATERMARK + 5)
        records[did(3)].update(quality_score=0.0)
        _, shadow = self.shadow(records.values())
        network = Network()
        network.account(did(3), "h3.test", profile=PROFILE)
        result = self.run_pair(self.production(), shadow)
        arbiter.label_samples(result, network.authority(now_ms=1_794_300_000_000))
        verdict = gates.corpus_gate(result)
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertIn("production is right on 1 of 1 sampled records the shadow refreshed after the watermark", verdict["reasons"][0])


if __name__ == "__main__":
    unittest.main()
