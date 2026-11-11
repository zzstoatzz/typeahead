import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shadow_compare_cloud as runner
import synth

import bundle
import shadow_stage
from fakes import FakeStore, store_record

SECRET = "runner-s3cr3t-0123456789"
WATERMARK = 1_800_000_000
ACTORS = 3000
FAKE_RCLONE = """#!/bin/sh
[ "$1" = cat ] || exit 64
exec cat "$2"
"""


def load_register():
    location = Path(runner.__file__).with_name("shadow_compare_register.py")
    spec = importlib.util.spec_from_file_location("shadow_compare_register", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Bucket:
    """Records what an S3 client is asked to store."""

    def __init__(self, refuse=None):
        self.stored = {}
        self.refuse = refuse

    def upload_file(self, path, bucket, key):
        if self.refuse:
            raise PermissionError(self.refuse)
        self.stored[(bucket, key)] = Path(path).read_bytes()

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.stored[(Bucket, Key)])}


class Orchestration(unittest.TestCase):
    def setUp(self):
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        self.directory = Path(stack.name)
        self.output = self.directory / "published"
        rng = random.Random(43)
        self.actors = [synth.actor(rng, n) for n in range(ACTORS)]
        self.input = self.directory / "actors.jsonl"
        self.input.write_text("".join(json.dumps(actor, ensure_ascii=False, separators=(",", ":")) + "\n" for actor in self.actors))

    def plan(self, **changes):
        settings = dict(
            input_jsonl=str(self.input), watermark=WATERMARK, output_dir=str(self.output), scratch_parent=str(self.directory),
            required_free_gib=1, budget_gib=1, run_records=700,
        )
        settings.update(changes)
        return runner.Plan(**settings)

    def run_plan(self, plan, run_id="dry-run"):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            try:
                summary = runner.build(run_id, runner.HERE, plan)
            except RuntimeError as error:
                summary = error
        return summary, printed.getvalue()

    def test_the_views_are_sorted_packed_and_published_and_no_scratch_is_left(self):
        summary, printed = self.run_plan(self.plan())
        self.assertEqual(summary["status"], "completed", printed[-2000:])
        self.assertEqual((summary["sort"]["lines"], summary["sort"]["actors"], summary["sort"]["runs"]), (ACTORS, ACTORS, 5))
        published = self.output / "dry-run"
        self.assertEqual(sorted(item.name for item in published.iterdir()), sorted(runner.OBJECTS))
        raw = (published / bundle.MANIFEST_NAME).read_bytes()
        self.assertEqual(summary["pack"]["manifest_sha256"], hashlib.sha256(raw).hexdigest())
        manifest = bundle.read_manifest(raw, summary["pack"]["manifest_sha256"])
        self.assertEqual((manifest["run_id"], manifest["build_id"], manifest["source_watermark"], manifest["actors"]), ("dry-run", "local-actor-file", WATERMARK, ACTORS))
        with (published / bundle.VIEWS_NAME).open("rb") as stream:
            bundle.unpack(stream, manifest, self.directory / "views.jsonl")
        dids = [bundle.parse(line)[0] for line in (self.directory / "views.jsonl").read_bytes().splitlines()]
        self.assertEqual(dids, sorted(actor["did"] for actor in self.actors))
        self.assertTrue(summary["cleanup"]["scratch_removed"])
        self.assertEqual(list(self.directory.glob("typeahead-shadow-compare-*")), [])
        records = [json.loads(line) for line in printed.splitlines()]
        self.assertEqual(records[-1]["event"], "shadow_compare_summary")
        announced = next(record for record in records if record.get("event") == "published")
        self.assertEqual((announced["run_id"], announced["manifest_sha256"]), ("dry-run", summary["pack"]["manifest_sha256"]))

    def test_the_shadow_stage_walks_a_store_against_what_the_builder_published(self):
        summary, printed = self.run_plan(self.plan(), run_id="handoff")
        self.assertEqual(summary["status"], "completed", printed[-2000:])
        records = [store_record(actor, WATERMARK - 500) for actor in self.actors]
        live = next(record for record in records if record["status"] == "live" and record["handle"])
        live["handle"] = "diverged.example.test"
        tools = self.directory / "tools"
        tools.mkdir()
        (tools / "rclone").write_text(FAKE_RCLONE)
        (tools / "rclone").chmod(0o755)
        environment = {shadow_stage.STORE_SECRET: SECRET, shadow_stage.REMOTE_OVERRIDE: str(self.output), "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
        root = self.directory / "compare"
        for expected, held in ((0, [store_record(actor, WATERMARK - 500) for actor in self.actors]), (1, records)):
            fake = FakeStore(held, SECRET)
            self.addCleanup(fake.close)
            command = ["--root", str(root / str(expected)), "run", "--run-id", "handoff", "--manifest-sha256", summary["pack"]["manifest_sha256"],
                       "--store", fake.base, "--delay", "0", "--reserve-gib", "0", "--no-arbiter", "--settle-delay", "0"]
            out = io.StringIO()
            with mock.patch.dict(os.environ, environment), mock.patch.object(os, "nice"), contextlib.redirect_stdout(out):
                self.assertEqual(shadow_stage.main(command), expected, out.getvalue())
            self.assertNotIn(SECRET, out.getvalue())
            report = json.loads((root / str(expected) / "handoff" / "report" / "report.json").read_text())
            self.assertEqual((report["corpus"]["dids_walked"], report["corpus"]["differing_dids"]), (ACTORS, expected))
            self.assertEqual(report["corpus_sources"]["production"]["build_id"], "local-actor-file")

    def test_objects_go_under_the_scratch_prefix_with_the_manifest_last(self):
        packed = self.directory / "packed"
        packed.mkdir()
        for name in runner.OBJECTS:
            (packed / name).write_text(name)
        bucket = Bucket()
        published = runner.publish(packed, "run-7", runner.Plan(), (bucket, "bundles"))
        self.assertEqual(published, {"kind": "r2", "prefix": "scratch/shadow-compare/run-7", "objects": list(runner.OBJECTS)})
        self.assertEqual(list(bucket.stored), [("bundles", f"scratch/shadow-compare/run-7/{name}") for name in runner.OBJECTS])
        self.assertEqual(list(bucket.stored)[-1][1], "scratch/shadow-compare/run-7/manifest.json")

    def test_a_refused_write_fails_the_run_without_leaking_credentials(self):
        packed = self.directory / "packed"
        packed.mkdir()
        for name in runner.OBJECTS:
            (packed / name).write_text(name)
        with mock.patch.dict(os.environ, {"INDEX_R2_SECRET_ACCESS_KEY": SECRET}):
            with self.assertRaises(RuntimeError) as caught:
                runner.publish(packed, "run-7", runner.Plan(), (Bucket(refuse=f"AccessDenied for {SECRET}"), "bundles"))
        self.assertEqual(str(caught.exception), "Publishing failed: PermissionError: AccessDenied for [INDEX_R2_SECRET_ACCESS_KEY]")

    def test_an_unreadable_input_fails_removes_scratch_and_publishes_nothing(self):
        self.input.write_text("not json\n")
        summary, printed = self.run_plan(self.plan())
        self.assertIsInstance(summary, RuntimeError)
        final = json.loads(printed.splitlines()[-1])
        self.assertEqual((final["status"], final["cleanup"]["scratch_removed"]), ("failed", True))
        self.assertEqual(list(self.directory.glob("typeahead-shadow-compare-*")), [])
        self.assertFalse(self.output.exists())

    def test_too_little_disk_or_a_bad_run_id_stops_before_any_scratch_exists(self):
        with self.assertRaises(RuntimeError):
            runner.build("refused", runner.HERE, self.plan(required_free_gib=1e9))
        with self.assertRaises(ValueError):
            runner.build("../escape", runner.HERE, self.plan())
        self.assertEqual(list(self.directory.glob("typeahead-shadow-compare-*")), [])


class Planning(unittest.TestCase):
    def test_plan_rejects_bad_build_ids_and_files_without_a_watermark(self):
        for changes in ({"build_id": "../escape"}, {"input_jsonl": "actors.jsonl"}):
            with self.assertRaises(ValueError):
                runner.Plan(**changes)


class Registration(unittest.TestCase):
    def test_deployment_reads_the_bundle_bucket_and_holds_no_shadow_secret(self):
        register = load_register()
        made = register.deployment("a" * 40, register.MPS_PIN)
        self.assertEqual(sorted(name for name in made["job_variables"]["env"] if not name.startswith("PREFECT_")), sorted(runner.cloud.SECRET_NAMES))
        self.assertEqual(made["parameters"], {})
        self.assertEqual(made["entrypoint"], "experiments/zkv-scale/shadow_compare_cloud.py:shadow_compare")
        self.assertGreater(made["job_variables"]["timeout_seconds"], runner.DEADLINE_SECONDS)
        schema = register.flow_schema()
        self.assertNotIn("required", schema)
        self.assertEqual(sorted(schema["properties"]), sorted(runner.Plan.__dataclass_fields__))

    def test_arguments_are_checked(self):
        register = load_register()
        with self.assertRaises(argparse.ArgumentTypeError):
            register.sha("abc")
        self.assertEqual(register.sha("a" * 40), "a" * 40)


if __name__ == "__main__":
    unittest.main()
