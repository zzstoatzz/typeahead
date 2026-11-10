import datetime
import hashlib
import io
import json
import os
import random
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cloud
import synth


def bundle(directory, members, listed=None):
    path = Path(directory) / "index.tar"
    with tarfile.open(path, "w:") as archive:
        for name, data in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    raw = path.read_bytes()
    manifest = {
        "active_build": "b1700000000-test",
        "engine": "tantivy",
        "object_key": "releases/search-tantivy/b1700000000-test/index.tar",
        "byte_size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "files": [
            {"path": name, "byte_size": len(data)}
            for name, data in (listed or members).items()
        ],
    }
    return path, manifest


MEMBERS = {
    "index/meta.json": b"{}",
    "index/segment.store": b"stored" * 1000,
    "pds-fallback.sqlite": b"not needed",
}


class Bundle(unittest.TestCase):
    def test_extracts_only_index_files_after_verifying_the_whole_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            path, manifest = bundle(directory, MEMBERS)
            cloud.validate_manifest(manifest, "b1700000000-test")
            facts = cloud.extract_index(path, manifest, Path(directory) / "out")
            extracted = sorted(
                p.name for p in (Path(directory) / "out/index").iterdir()
            )
            self.assertEqual(extracted, ["meta.json", "segment.store"])
            self.assertFalse((Path(directory) / "out/pds-fallback.sqlite").exists())
            self.assertEqual(facts["index_files"], 2)
            self.assertEqual(facts["index_bytes"], 6002)
            self.assertTrue(facts["bundle_sha256_verified"])

    def test_rejects_a_bundle_that_differs_from_its_manifest(self):
        cases = {
            "checksum": lambda manifest: manifest.update(sha256="0" * 64),
            "size": lambda manifest: manifest["files"][1].update(byte_size=5),
            "missing": lambda manifest: manifest["files"].append(
                {"path": "index/absent", "byte_size": 1}
            ),
            "unsafe": lambda manifest: manifest["files"].append(
                {"path": "index/../escape", "byte_size": 1}
            ),
        }
        for name, corrupt in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                path, manifest = bundle(directory, MEMBERS)
                corrupt(manifest)
                with self.assertRaises(ValueError):
                    cloud.extract_index(path, manifest, Path(directory) / "out")

    def test_manifest_must_describe_the_requested_build(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest = bundle(directory, MEMBERS)
            with self.assertRaises(ValueError):
                cloud.validate_manifest(manifest, "b1700000001-other")
            with self.assertRaises(ValueError):
                cloud.validate_manifest(
                    {**manifest, "sha256": "short"}, "b1700000000-test"
                )
            listed = [
                item for item in manifest["files"] if item["path"] != "index/meta.json"
            ]
            with self.assertRaises(ValueError):
                cloud.validate_manifest(
                    {**manifest, "files": listed}, "b1700000000-test"
                )

    def test_newest_build_is_the_latest_manifest_with_a_bundle(self):
        def item(build, name, day):
            return {
                "Key": f"releases/search-tantivy/{build}/{name}",
                "LastModified": datetime.datetime(2026, 11, day, tzinfo=datetime.UTC),
            }

        pages = [
            {
                "Contents": [
                    item("b1700000001-old", "index.tar", 1),
                    item("b1700000001-old", "manifest.json", 1),
                    item("b1700000003-unfinished", "index.tar", 9),
                ]
            },
            {
                "Contents": [
                    item("b1700000002-new", "index.tar", 5),
                    item("b1700000002-new", "manifest.json", 5),
                    item("not-a-build", "manifest.json", 10),
                    {"Key": "releases/search-tantivy/stray", "LastModified": None},
                ]
            },
        ]
        client = mock.Mock()
        client.get_paginator.return_value.paginate.return_value = pages
        self.assertEqual(cloud.newest_build(client, "bucket"), "b1700000002-new")
        client.get_paginator.return_value.paginate.return_value = [{}]
        with self.assertRaises(RuntimeError):
            cloud.newest_build(client, "bucket")


class Supervision(unittest.TestCase):
    def supervisor(self, directory, seconds=30, budget=cloud.GIB):
        return cloud.Supervisor(Path(directory), time.monotonic() + seconds, budget)

    def test_collects_json_lines_and_labels_other_output(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory)
            script = 'print(\'{"event":"x","n":1}\'); print("plain")'
            with mock.patch.object(cloud, "emit") as emitted:
                records = supervisor.run("demo", [sys.executable, "-c", script])
            self.assertEqual(records, [{"event": "x", "n": 1}, {"line": "plain"}])
            self.assertEqual(emitted.call_args_list[0].args[0]["phase"], "demo")
            self.assertEqual(supervisor.jobs, [])

    def test_failure_raises_unless_the_caller_records_it(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory)
            command = [sys.executable, "-c", "import sys; sys.exit('broken')"]
            with mock.patch.object(cloud, "emit"):
                with self.assertRaises(RuntimeError):
                    supervisor.run("demo", command)
                records = supervisor.run("demo", command, check=False)
            self.assertEqual(records[-1]["event"], "command_failed")
            self.assertIn("broken", records[-1]["stderr_tail"])

    def test_deadline_kills_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, seconds=1)
            marker = Path(directory) / "grandchild.pid"
            script = (
                "import subprocess, sys, time;"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                f"open({str(marker)!r}, 'w').write(str(child.pid));"
                "time.sleep(60)"
            )
            with self.assertRaises(TimeoutError):
                supervisor.run("demo", [sys.executable, "-c", script])
            grandchild = int(marker.read_text())
            for _ in range(50):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail("grandchild survived its process group")
            self.assertEqual(supervisor.jobs, [])

    def test_disk_budget_stops_work(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(directory, budget=0)
            supervisor.free_at_start += 1
            with self.assertRaises(RuntimeError):
                supervisor.run("demo", [sys.executable, "-c", "pass"])


class Reporting(unittest.TestCase):
    def test_plan_rejects_unknown_phases_and_build_ids(self):
        self.assertFalse(cloud.Plan(skip=("storm",)).runs("storm"))
        with self.assertRaises(ValueError):
            cloud.Plan(skip=("load",))
        with self.assertRaises(ValueError):
            cloud.Plan(build_id="../escape")

    def test_emitted_text_never_carries_r2_configuration(self):
        secret = 'https://account.example/"quoted"'
        with mock.patch.dict(os.environ, {"INDEX_R2_ENDPOINT": secret}):
            self.assertNotIn("account.example", cloud.redact(f"failed for {secret}"))
            self.assertNotIn(
                "account.example", cloud.redact(json.dumps({"error": secret}))
            )

    def test_scan_summary_counts_failures_and_retries(self):
        scans = [
            {
                "event": "scan",
                "wall_ms": 5,
                "totals": {"missing_errors": 1, "retries": 1, "entries": 9},
            },
            {
                "event": "scan",
                "wall_ms": 9,
                "totals": {"missing_errors": 0, "retries": 0, "entries": 9},
            },
            {"event": "command_failed"},
        ]
        summary = cloud.scan_summary(scans)
        self.assertEqual(
            summary,
            {
                "scans": 2,
                "failed_scans": 1,
                "missing_errors": 1,
                "retries": 1,
                "slowest_ms": 9,
                "fastest_ms": 5,
                "entries": [9],
            },
        )


class Orchestration(unittest.TestCase):
    def test_every_phase_runs_on_synthetic_actors_and_leaves_no_scratch(self):
        root = Path(cloud.__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            actors = Path(directory) / "actors.jsonl"
            rng = random.Random(32)
            with actors.open("w") as output:
                for n in range(20_000):
                    line = json.dumps(
                        synth.actor(rng, n), ensure_ascii=False, separators=(",", ":")
                    )
                    output.write(line + "\n")
            plan = cloud.Plan(
                input_jsonl=str(actors),
                scratch_parent=directory,
                required_free_gib=2,
                budget_gib=2,
                get_reads=2000,
                churn_seconds=6,
                churn_commits_per_minute=600,
                churn_keys_per_commit=60,
                burst_every_seconds=2,
                burst_keys=1200,
                storm_keys=8000,
            )
            summary = cloud.measure("dry-run", root, plan)
            phases = summary["phases"]
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(phases["load"]["records"], 20_000)
            self.assertEqual(summary["input"]["documents"], 20_000)
            self.assertEqual(phases["count"]["entries"], phases["load"]["entries"])
            self.assertEqual(
                phases["count_settled"]["entries"], phases["load"]["entries"]
            )
            self.assertTrue(phases["count_final"]["matches_first_count"])
            self.assertEqual(len(phases["stat"]), 4)
            self.assertEqual(phases["settle"]["shape"]["compaction_phase"], "idle")
            self.assertEqual(phases["settle"]["shape"]["unflushed_segments"], 0)
            self.assertEqual(phases["get"]["point"]["count"], 2000)
            self.assertEqual(phases["get"]["point_misses"], 0)
            self.assertEqual(phases["get_stride32"]["fence_stride"], 32)
            self.assertGreater(phases["churn"]["updates"], 0)
            self.assertGreater(phases["churn_scan"]["scans"], 0)
            self.assertEqual(phases["churn_scan"]["failed_scans"], 0)
            self.assertEqual(phases["churn_scan"]["entries"], [20_000])
            self.assertEqual(phases["storm"]["actors"], 8000)
            self.assertEqual(phases["second_load"]["options"]["block_bytes"], 65536)
            self.assertEqual(phases["second_load"]["records"], 20_000)
            self.assertIn("second_settle", phases)
            self.assertTrue(summary["cleanup"]["scratch_removed"])
            self.assertEqual(list(Path(directory).glob("typeahead-zkv-scale-*")), [])

    def test_failure_still_removes_scratch_and_reports(self):
        root = Path(cloud.__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            plan = cloud.Plan(
                scratch_parent=directory, required_free_gib=0, budget_gib=1
            )
            broken = RuntimeError("tools unavailable")
            with (
                mock.patch.object(cloud, "build_tools", side_effect=broken),
                mock.patch.object(cloud, "emit") as emitted,
                self.assertRaises(RuntimeError),
            ):
                cloud.measure("failing", root, plan)
            summary = emitted.call_args_list[-1].args[0]
            self.assertEqual(summary["status"], "failed")
            self.assertIn("tools unavailable", summary["error"])
            self.assertTrue(summary["cleanup"]["scratch_removed"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_insufficient_free_disk_stops_before_any_scratch_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = cloud.Plan(scratch_parent=directory, required_free_gib=1e9)
            with self.assertRaises(RuntimeError):
                cloud.measure("no-space", Path(directory), plan)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
