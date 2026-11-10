import argparse
import contextlib
import importlib.util
import io
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shadow_compare_cloud as runner
import synth

sys.path.insert(0, str(runner.TOOLING))

from fakes import FakeStore, store_record

SECRET_ENV = "SHADOW_COMPARE_CLOUD_TEST_SECRET"
SECRET = "runner-s3cr3t-0123456789"
WATERMARK = 1_800_000_000
ACTORS = 3000


def load_register():
    location = Path(runner.__file__).with_name("shadow_compare_register.py")
    spec = importlib.util.spec_from_file_location("shadow_compare_register", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Orchestration(unittest.TestCase):
    def setUp(self):
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        self.directory = Path(stack.name)
        rng = random.Random(43)
        self.actors = [synth.actor(rng, n) for n in range(ACTORS)]
        self.input = self.directory / "actors.jsonl"
        self.input.write_text("".join(json.dumps(actor, ensure_ascii=False, separators=(",", ":")) + "\n" for actor in self.actors))
        patch = mock.patch.dict(os.environ, {SECRET_ENV: SECRET})
        patch.start()
        self.addCleanup(patch.stop)

    def store(self, records):
        fake = FakeStore(records, SECRET)
        self.addCleanup(fake.close)
        return fake

    def plan(self, url, **changes):
        settings = dict(
            shadow_url=url, shadow_secret_env=SECRET_ENV, input_jsonl=str(self.input), watermark=WATERMARK,
            scratch_parent=str(self.directory), required_free_gib=1, budget_gib=1, run_records=700, request_delay=0,
            settle_delay=0, arbiter=False,
        )
        settings.update(changes)
        return runner.Plan(**settings)

    def run_plan(self, plan):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            try:
                summary = runner.compare("dry-run", runner.HERE, plan)
            except RuntimeError as error:
                summary = error
        return summary, printed.getvalue()

    def test_a_store_holding_the_dumped_actors_passes_and_leaves_no_scratch(self):
        fake = self.store([store_record(actor, WATERMARK - 500) for actor in self.actors])
        summary, printed = self.run_plan(self.plan(fake.base))
        self.assertEqual(summary["status"], "completed", printed[-2000:])
        self.assertEqual(summary["verdict"], {"status": "PASS", "reasons": []})
        self.assertEqual((summary["sort"]["lines"], summary["sort"]["actors"], summary["sort"]["runs"]), (ACTORS, ACTORS, 5))
        corpus = summary["report"]["corpus"]
        self.assertEqual((corpus["dids_walked"], corpus["differing_dids"]), (ACTORS, 0))
        self.assertEqual(corpus["snapshot"]["watermark"], WATERMARK)
        self.assertEqual(summary["report"]["corpus_sources"]["shadow"], {"kind": "store-http", "base": fake.base})
        self.assertTrue(summary["cleanup"]["scratch_removed"])
        self.assertEqual(list(self.directory.glob("typeahead-shadow-compare-*")), [])
        self.assertNotIn(SECRET, printed)
        self.assertIn("# Shadow comparison", printed)
        self.assertEqual(json.loads(printed.splitlines()[-1])["event"], "shadow_compare_summary")

    def test_divergence_completes_with_a_failing_verdict(self):
        records = [store_record(actor, WATERMARK - 500) for actor in self.actors]
        live = next(record for record in records if record["status"] == "live" and record["handle"])
        live["handle"] = "diverged.example.test"
        fake = self.store(records[1:] if records[0] is not live else records[:-1])
        summary, printed = self.run_plan(self.plan(fake.base))
        self.assertEqual(summary["status"], "completed", printed[-2000:])
        self.assertEqual(summary["verdict"]["status"], "FAIL")
        self.assertEqual(summary["report"]["corpus"]["timing"]["unchanged_in_shadow"]["differing_dids"], summary["report"]["corpus"]["differing_dids"])
        self.assertEqual(summary["report"]["corpus"]["classes"]["handle_differs"]["count"], 1)
        self.assertTrue(summary["cleanup"]["scratch_removed"])

    def test_an_unreachable_store_fails_removes_scratch_and_keeps_the_secret_out(self):
        fake = self.store([])
        fake.close()
        summary, printed = self.run_plan(self.plan(fake.base))
        self.assertIsInstance(summary, RuntimeError)
        final = json.loads(printed.splitlines()[-1])
        self.assertEqual((final["status"], final["cleanup"]["scratch_removed"]), ("failed", True))
        self.assertEqual(list(self.directory.glob("typeahead-shadow-compare-*")), [])
        self.assertNotIn(SECRET, printed)

    def test_a_missing_secret_or_too_little_disk_stops_before_any_scratch_exists(self):
        for plan in (self.plan("https://store.test", shadow_secret_env="SHADOW_COMPARE_CLOUD_TEST_UNSET"), self.plan("https://store.test", required_free_gib=1e9)):
            with self.assertRaises(RuntimeError):
                runner.compare("refused", runner.HERE, plan)
            self.assertEqual(list(self.directory.glob("typeahead-shadow-compare-*")), [])


class Reporting(unittest.TestCase):
    def test_plan_rejects_bad_build_ids_secret_names_and_files_without_a_watermark(self):
        for changes in ({"build_id": "../escape"}, {"shadow_secret_env": "not a name"}, {"input_jsonl": "actors.jsonl"}):
            with self.assertRaises(ValueError):
                runner.Plan(shadow_url="https://store.test", **changes)

    def test_long_lists_of_differing_dids_are_cut_with_their_count_and_hash(self):
        short = {"corpus": {"flagged_dids": ["did:plc:a"]}}
        self.assertIs(runner.trimmed(short), short)
        dids = [f"did:plc:{n:05}" for n in range(runner.REPORTED_DIDS + 5)]
        cut = runner.trimmed({"corpus": {"flagged_dids": dids}})["corpus"]
        self.assertEqual((len(cut["flagged_dids"]), cut["flagged_dids_total"]), (runner.REPORTED_DIDS, len(dids)))
        self.assertEqual(len(cut["flagged_dids_sha256"]), 64)
        self.assertEqual(runner.trimmed({"search": {}}), {"search": {}})

    def test_redaction_covers_the_store_secret(self):
        with mock.patch.dict(os.environ, {SECRET_ENV: SECRET}):
            plan = runner.Plan(shadow_url="https://store.test", shadow_secret_env=SECRET_ENV)
            self.assertEqual(runner.redact("bearer " + SECRET, plan), f"bearer [{SECRET_ENV}]")


class Registration(unittest.TestCase):
    def test_deployment_maps_the_secret_block_and_passes_the_shadow_as_parameters(self):
        register = load_register()
        made = register.deployment("a" * 40, register.MPS_PIN, "https://shadow-store.example", "shadow-store-read")
        self.assertEqual(made["job_variables"]["env"][register.SECRET_ENV], "prefect-block://shadow-store-read")
        self.assertEqual(made["parameters"], {"shadow_url": "https://shadow-store.example", "shadow_secret_env": register.SECRET_ENV})
        self.assertEqual(made["entrypoint"], "experiments/zkv-scale/shadow_compare_cloud.py:shadow_compare")
        self.assertGreater(made["job_variables"]["timeout_seconds"], runner.DEADLINE_SECONDS)
        schema = register.flow_schema()
        self.assertEqual(schema["required"], ["shadow_url"])
        self.assertTrue(set(made["parameters"]) <= set(schema["properties"]))

    def test_arguments_are_checked(self):
        register = load_register()
        for check, bad in ((register.origin, "http://store.test"), (register.origin, "https://store.test/path"), (register.block, "Not A Block"), (register.sha, "abc")):
            with self.assertRaises(argparse.ArgumentTypeError):
                check(bad)
        self.assertEqual(register.origin("https://store.example:8443"), "https://store.example:8443")


if __name__ == "__main__":
    unittest.main()
