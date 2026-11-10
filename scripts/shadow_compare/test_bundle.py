import contextlib
import gzip
import hashlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bundle
import corpus
import sources
from fakes import FakeBackend, admin_document, stored_actor

LABEL = {"src": "did:plc:mod", "uri": "did:plc:x", "val": "spam", "cts": "2026-01-01T00:00:00Z"}


def did(n):
    return f"did:plc:{n:05}"


def dump(actors):
    return b"".join(json.dumps(actor, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for actor in actors)


def actors(count=20, seed=7):
    rows = [stored_actor(did(n), f"h{n}.test", display=f"Name {n} 名前") for n in range(count)]
    random.Random(seed).shuffle(rows)
    return rows


class Scratch(unittest.TestCase):
    def setUp(self):
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        self.root = Path(stack.name)

    def sort(self, rows, **options):
        out = self.root / "sorted.jsonl"
        facts = bundle.sort(io.BytesIO(rows if isinstance(rows, bytes) else dump(rows)), out, self.root / "runs", **options)
        return out, facts

    def source(self, rows, **options):
        out, _ = self.sort(rows, **options)
        return bundle.BundleJsonl(out, watermark=100, build_id="b1")


class SortTests(Scratch):
    def test_unsorted_input_comes_out_in_did_order_with_runs_bounded_in_memory(self):
        held = []
        write = bundle.write_run
        with mock.patch.object(bundle, "write_run", lambda records, path: (held.append(len(records)), write(records, path))[1]):
            out, facts = self.sort(actors(), run_records=3)
        self.assertEqual(held, [3] * 6 + [2])
        self.assertEqual((facts["lines"], facts["actors"], facts["runs"], facts["duplicates"]), (20, 20, 7, 0))
        self.assertEqual([bundle.parse(line)[0] for line in out.read_bytes().splitlines()], [did(n) for n in range(20)])
        self.assertEqual(list((self.root / "runs").iterdir()), [])
        self.assertEqual(facts["output_bytes"], out.stat().st_size)

    def test_more_runs_than_the_fan_in_merge_in_passes_to_the_same_file(self):
        out, once = self.sort(actors(), run_records=2)
        expected = out.read_bytes()
        out, passes = self.sort(actors(), run_records=2, fan_in=2)
        self.assertEqual(out.read_bytes(), expected)
        self.assertEqual((once["merge_passes"], passes["merge_passes"], passes["runs"]), (1, 4, 10))
        self.assertEqual(list((self.root / "runs").iterdir()), [])

    def test_duplicate_dids_keep_the_highest_version_then_the_later_line(self):
        rows = [
            stored_actor(did(1), "first.test", version=9),
            stored_actor(did(2), "other.test"),
            stored_actor(did(1), "stale.test", version=5),
            stored_actor(did(1), "second.test", version=9),
            stored_actor(did(3), "old.test", version=4),
            stored_actor(did(3), "gone.test", version=8, deleted=True),
        ]
        for size in (1, 2, 100):
            source = self.source(rows, run_records=size)
            self.assertEqual((source.facts["lines"], source.facts["actors"], source.facts["duplicates"]), (6, 3, 3))
            found = dict(source.read("", 10))
            self.assertEqual(found[did(1)].fields["handle"], "second.test")
            self.assertEqual(found[did(3)].state, "deleted")
            self.assertEqual(source.marker()["duplicate_dids"], 3)

    def test_deleted_documents_are_kept_as_tombstones(self):
        rows = actors(6) + [stored_actor(did(50), "", deleted=True), stored_actor(did(51), "", deleted=True)]
        source = self.source(rows)
        self.assertEqual((source.facts["live"], source.facts["deleted"]), (6, 2))
        found = dict(source.read("", 10))
        self.assertFalse(found[did(50)].live)
        self.assertEqual(found[did(50)].state, "deleted")
        self.assertEqual(source.marker()["tombstone_count"], 2)

    def test_gzip_input_and_blank_lines_are_read(self):
        packed = self.root / "dump.jsonl.gz"
        packed.write_bytes(gzip.compress(dump(actors(5)) + b"\n\n"))
        with bundle.open_input(packed) as stream:
            facts = bundle.sort(stream, self.root / "sorted.jsonl", self.root / "runs")
        self.assertEqual((facts["lines"], facts["actors"]), (5, 5))

    def test_view_carries_the_document_hash_labels_and_versions(self):
        row = stored_actor(did(1), "a.test", labels=[LABEL], version=42, score=0.1)
        source = self.source([row])
        [(seen, doc)] = source.read("", 5)
        self.assertEqual(seen, did(1))
        self.assertEqual(doc.sha, bundle.hashlib.sha256(dump([row]).rstrip()).hexdigest())
        self.assertEqual([json.loads(text) for text in doc.fields["labels"]], [LABEL])
        self.assertEqual((source.facts["min_version"], source.facts["max_version"]), (42, 42))

    def test_lines_that_are_not_actors_stop_the_sort_naming_the_line(self):
        for bad in (b"not json\n", b"[]\n", b'{"did":"did:plc:a b","version":1}\n', b'{"did":"did:plc:a"}\n'):
            with self.assertRaises(bundle.BundleError) as raised:
                self.sort(dump(actors(2)) + bad)
            self.assertIn("line 3", str(raised.exception))

    def test_command_prints_one_result_line(self):
        source = self.root / "dump.jsonl"
        source.write_bytes(dump(actors(9)))
        printed = io.StringIO()
        with mock.patch("sys.stdout", printed):
            status = bundle.main(["sort", "--input", str(source), "--out", str(self.root / "o.jsonl"), "--tmp", str(self.root / "t"), "--run-records", "4"])
        result = json.loads(printed.getvalue())
        self.assertEqual((status, result["event"], result["actors"], result["runs"]), (0, "sort", 9, 3))
        self.assertNotIn("index", result)


class SourceTests(Scratch):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(bundle, "INDEX_STRIDE", 3)
        patch.start()
        self.addCleanup(patch.stop)

    def test_pages_cover_the_file_once_in_order(self):
        source = self.source(actors(20))
        seen, after = [], ""
        while True:
            page = source.read(after, 6)
            seen += [d for d, _ in page]
            if len(page) < 6:
                break
            after = page[-1][0]
        self.assertEqual(seen, [did(n) for n in range(20)])

    def test_a_new_reader_resumes_after_any_did_present_or_not(self):
        out, _ = self.sort(actors(20))
        for after, first in ((did(7), did(8)), ("did:plc:000075", did(8)), ("", did(0)), (did(19), None), ("did:plc:zzz", None), ("did:a", did(0))):
            page = bundle.BundleJsonl(out).read(after, 2)
            self.assertEqual(page[0][0] if page else None, first, after)

    def test_lookup_finds_present_deleted_and_absent_dids(self):
        source = self.source(actors(20) + [stored_actor(did(30), "", deleted=True)])
        found = source.lookup([did(30), did(4), "did:plc:000045", "did:a", "did:plc:zzz", did(19)])
        self.assertEqual({d: doc.state for d, doc in found.items()}, {
            did(30): "deleted", did(4): "live", "did:plc:000045": "absent", "did:a": "absent", "did:plc:zzz": "absent", did(19): "live",
        })

    def test_a_file_that_differs_from_its_sidecar_is_refused(self):
        out, _ = self.sort(actors(5))
        with out.open("ab") as stream:
            stream.write(b"x")
        with self.assertRaises(bundle.BundleError):
            bundle.BundleJsonl(out)


class WalkTests(Scratch):
    def run_pair(self, production_rows, shadow_rows, **options):
        production = self.source(production_rows, run_records=4)
        shadow = FakeBackend("s", [admin_document(row) for row in shadow_rows])
        settings = dict(checkpoint_path=None, page_size=7, max_pages=None, max_tracked=1000, settle_delay=5, sample_size=5, sleep=lambda s: None)
        settings.update(options)
        return corpus.run_sources(production, sources.TantivyAdmin(shadow), **settings), production, shadow

    def test_a_dump_and_an_admin_listener_holding_the_same_actors_agree(self):
        rows = actors(40) + [stored_actor(did(90), "", deleted=True), stored_actor(did(91), "l.test", labels=[LABEL], score=0.1)]
        result, _, _ = self.run_pair(rows, rows)
        self.assertTrue(result["complete"] and result["rechecked"])
        self.assertEqual((result["differing_dids"], result["dids_walked"]), (0, 42))
        self.assertEqual(result["live"], {"production": 41, "shadow": 41})

    def test_differences_are_classed_and_the_snapshot_is_not_read_again(self):
        rows = actors(40)
        changed = [dict(row) for row in rows if row["did"] != did(3)]
        by_did = {row["did"]: row for row in changed}
        by_did[did(5)]["handle"] = "renamed.test"
        by_did[did(6)]["quality_score"] = 2.0
        by_did[did(7)]["handle"] = "lagging.test"
        production = self.source(rows, run_records=4)
        shadow = FakeBackend("s", [admin_document(row) for row in changed])
        production.lookup = None

        def settle(seconds):
            shadow.documents[did(7)]["handle"] = "h7.test"

        result = corpus.run_sources(production, sources.TantivyAdmin(shadow), checkpoint_path=None, page_size=7, max_pages=None,
                                    max_tracked=1000, settle_delay=5, sample_size=5, sleep=settle)
        counts = {name: entry["count"] for name, entry in result["classes"].items() if entry["count"]}
        self.assertEqual(counts, {corpus.ONLY_IN_PRODUCTION: 1, corpus.HANDLE_DIFFERS: 1, corpus.QUALITY_SCORE_DIFFERS: 1})
        self.assertEqual(result["transient_differences"], 1)
        self.assertEqual(result["flagged_dids"], [did(3), did(5), did(6)])
        self.assertEqual(result["start_markers"]["production"]["watermark"], 100)
        self.assertEqual(result["generation_changed"], {"production": False, "shadow": False})

    def test_walk_resumes_from_a_checkpoint_into_the_sorted_file(self):
        rows = actors(40)
        path = self.root / "checkpoint.json"
        first, _, _ = self.run_pair(rows, rows[:30], checkpoint_path=path, max_pages=2)
        self.assertFalse(first["complete"])
        second, _, _ = self.run_pair(rows, rows[:30], checkpoint_path=path)
        self.assertTrue(second["resumed"] and second["complete"])
        self.assertEqual((second["dids_walked"], second["classes"][corpus.ONLY_IN_PRODUCTION]["count"]), (40, 10))



class PackTests(Scratch):
    def packed(self, rows=None):
        out, _ = self.sort(rows or actors(300), run_records=64)
        manifest, sha256, _ = bundle.pack(out, self.root / "packed", run_id="run-1", build_id="b1794333857-cp-test", watermark=1_794_333_857)
        return out, manifest, sha256

    def unpack(self, raw, manifest):
        target = self.root / "fetched" / "views.jsonl"
        target.parent.mkdir(exist_ok=True)
        bundle.unpack(io.BytesIO(raw), manifest, target)
        return target

    def test_packed_views_unpack_to_the_same_bytes_and_read_as_the_same_source(self):
        out, manifest, sha256 = self.packed()
        raw = (self.root / "packed" / bundle.MANIFEST_NAME).read_bytes()
        self.assertEqual(bundle.read_manifest(raw, sha256), manifest)
        self.assertEqual((manifest["actors"], manifest["live"], manifest["build_id"], manifest["source_watermark"]), (300, 300, "b1794333857-cp-test", 1_794_333_857))
        packed = (self.root / "packed" / bundle.VIEWS_NAME).read_bytes()
        self.assertEqual(gzip.decompress(packed), out.read_bytes())
        self.assertEqual((manifest["views"]["bytes"], manifest["views"]["sha256"]), (len(packed), hashlib.sha256(packed).hexdigest()))
        target = self.unpack(packed, manifest)
        self.assertEqual(target.read_bytes(), out.read_bytes())
        sidecar = (self.root / "packed" / bundle.SIDECAR_NAME).read_bytes()
        self.assertEqual(hashlib.sha256(sidecar).hexdigest(), manifest["sidecar"]["sha256"])
        bundle.meta_path(target).write_bytes(sidecar)
        fetched = bundle.BundleJsonl(target, watermark=manifest["source_watermark"], build_id=manifest["build_id"])
        self.assertEqual(fetched.read("", 500), bundle.BundleJsonl(out).read("", 500))

    def test_a_stream_that_is_not_the_described_object_leaves_no_file(self):
        out, manifest, _ = self.packed()
        packed = (self.root / "packed" / bundle.VIEWS_NAME).read_bytes()
        flipped = bytearray(packed)
        flipped[len(flipped) // 2] ^= 1
        other = gzip.compress(out.read_bytes() + b"extra\n")
        bomb = dict(manifest, views=dict(manifest["views"], bytes=len(other), sha256=hashlib.sha256(other).hexdigest()))
        cases = [(packed[:-9], manifest), (packed + b"x", manifest), (bytes(flipped), manifest), (out.read_bytes(), manifest), (other, bomb), (packed + packed, manifest)]
        for raw, described in cases:
            with self.assertRaises(bundle.BundleError):
                self.unpack(raw, described)
            self.assertEqual(list((self.root / "fetched").iterdir()), [])

    def test_a_manifest_is_refused_unless_its_bytes_and_shape_are_the_expected_ones(self):
        _, manifest, sha256 = self.packed()
        raw = (self.root / "packed" / bundle.MANIFEST_NAME).read_bytes()
        with self.assertRaises(bundle.BundleError):
            bundle.read_manifest(raw + b" ", sha256)
        for change in ({"version": 2}, {"run_id": "../x"}, {"source_watermark": "now"}, {"views": dict(manifest["views"], name="other.gz")}, {"sidecar": None}, {"actors": -1}):
            with self.assertRaises(bundle.BundleError):
                bundle.check_manifest({**manifest, **change})

    def test_pack_command_prints_the_manifest_and_its_hash_and_refuses_a_stale_sidecar(self):
        out, _ = self.sort(actors(40))
        printed = io.StringIO()
        arguments = ["pack", "--views", str(out), "--out-dir", str(self.root / "cli"), "--run-id", "r2", "--build-id", "b1", "--watermark", "100"]
        with contextlib.redirect_stdout(printed):
            self.assertEqual(bundle.main(arguments), 0)
        record = json.loads(printed.getvalue())
        raw = (self.root / "cli" / bundle.MANIFEST_NAME).read_bytes()
        self.assertEqual((record["event"], record["manifest_sha256"], record["actors"]), ("pack", hashlib.sha256(raw).hexdigest(), 40))
        with out.open("ab") as stream:
            stream.write(b"\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bundle.main(arguments), 2)


if __name__ == "__main__":
    unittest.main()
