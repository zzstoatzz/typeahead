import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bundle
import shadow_stage as stage
from fakes import FakeStore, store_record, stored_actor

SECRET = "stage-s3cr3t-0123456789"
WATERMARK = 1_800_000_000
RUN = "0d6c1f1e-6f0a-4a57-9d55-3f3c8f1f6f10"
ACTORS = 1200
FAKE_RCLONE = """#!/bin/sh
[ "$1" = cat ] || exit 64
[ -f "$2" ] || { echo "object not found" >&2; exit 3; }
exec cat "$2"
"""


def did(n):
    return f"did:plc:{n:05}"


class Stage(unittest.TestCase):
    def setUp(self):
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        self.directory = Path(stack.name)
        self.root = self.directory / "compare"
        self.remote = self.directory / "remote"
        tools = self.directory / "tools"
        tools.mkdir()
        (tools / "rclone").write_text(FAKE_RCLONE)
        (tools / "rclone").chmod(0o755)
        self.actors = [stored_actor(did(n), f"h{n}.test", display=f"Name {n} 名前") for n in range(ACTORS)]
        dump = b"".join(json.dumps(actor, ensure_ascii=False).encode() + b"\n" for actor in self.actors)
        views = self.directory / "views.jsonl"
        bundle.sort(io.BytesIO(dump), views, self.directory / "runs", run_records=500)
        _, self.sha256, _ = bundle.pack(views, self.remote / RUN, run_id=RUN, build_id="b1800000000-cp-test", watermark=WATERMARK)
        self.environment = {stage.STORE_SECRET: SECRET, stage.REMOTE_OVERRIDE: str(self.remote), "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}
        for patch in (mock.patch.dict(os.environ, self.environment), mock.patch.object(os, "nice")):
            patch.start()
            self.addCleanup(patch.stop)

    def store(self, records=None):
        fake = FakeStore([store_record(actor, WATERMARK - 500) for actor in self.actors] if records is None else records, SECRET)
        self.addCleanup(fake.close)
        return fake

    def stage(self, *arguments, sha256=None, base="http://127.0.0.1:9"):
        out, err = io.StringIO(), io.StringIO()
        command = ["--root", str(self.root), "run", "--run-id", RUN, "--manifest-sha256", sha256 or self.sha256, "--store", base,
                   "--delay", "0", "--reserve-gib", "0", "--no-arbiter", "--settle-delay", "0", *arguments]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = stage.main(command)
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def report(self):
        return json.loads((self.root / RUN / "report" / "report.json").read_text())

    def test_a_matching_store_passes_the_corpus_gate_and_prints_only_the_gate_summary(self):
        fake = self.store()
        code, out, err = self.stage(base=fake.base)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "(a) corpus differences zero or explained: PASS")
        self.assertEqual(lines[-2:], ["overall: INCOMPLETE", f"report: {self.root / RUN / 'report'}"])
        self.assertEqual(len(lines), 6)
        report = self.report()
        self.assertEqual((report["corpus"]["dids_walked"], report["corpus"]["differing_dids"]), (ACTORS, 0))
        self.assertEqual(report["corpus"]["snapshot"]["watermark"], WATERMARK)
        self.assertEqual(report["corpus_sources"]["production"]["build_id"], "b1800000000-cp-test")
        self.assertEqual(report["store_secret_variable"], stage.STORE_SECRET)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_a_diverged_store_fails_the_gate_with_exit_status_one(self):
        records = [store_record(actor, WATERMARK - 500) for actor in self.actors]
        records[7]["handle"] = "diverged.example.test"
        code, out, _ = self.stage(base=self.store(records[:-1]).base)
        self.assertEqual(code, 1)
        self.assertIn("(a) corpus differences zero or explained: FAIL", out)
        self.assertEqual(self.report()["corpus"]["differing_dids"], 2)

    def test_a_second_run_resumes_the_walk_without_fetching_again(self):
        fake = self.store()
        code, _, _ = self.stage("--max-pages", "1", base=fake.base)
        self.assertEqual(code, 1)
        self.assertFalse(self.report()["corpus"]["complete"])
        shutil.rmtree(self.remote)
        code, out, _ = self.stage(base=fake.base)
        self.assertEqual(code, 0)
        self.assertIn("(a) corpus differences zero or explained: PASS", out)
        corpus = self.report()["corpus"]
        self.assertTrue(corpus["resumed"] and corpus["complete"])
        self.assertEqual(corpus["dids_walked"], ACTORS)
        pages = [body["after_did"] for path, body in fake.calls if path.endswith("/page")]
        self.assertEqual(pages.count(""), 1)

    def test_objects_that_are_not_the_expected_ones_stop_the_run_and_leave_no_views(self):
        views = self.remote / RUN / bundle.VIEWS_NAME
        intact = views.read_bytes()
        cases = [
            (lambda: None, "0" * 64, "manifest differs"),
            (lambda: views.write_bytes(intact[:-20]), None, "gzip"),
            (lambda: views.write_bytes(intact[:100] + bytes([intact[100] ^ 1]) + intact[101:]), None, ""),
            (lambda: views.unlink(), None, "object not found"),
            (lambda: (views.write_bytes(intact), (self.remote / RUN / bundle.SIDECAR_NAME).write_bytes(b"{}")), None, "sidecar"),
        ]
        for spoil, sha256, message in cases:
            spoil()
            code, out, err = self.stage(sha256=sha256)
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn(message, err)
            self.assertEqual(list(self.root.rglob("views.jsonl*")), [])
            self.assertFalse((self.root / RUN / "report").exists())

    def test_too_little_free_disk_stops_before_the_views_are_fetched(self):
        code, _, err = self.stage("--reserve-gib", "1e9")
        self.assertEqual(code, 2)
        self.assertIn("GiB free", err)
        self.assertEqual(list((self.root / RUN / "inputs").iterdir()), [])

    def test_an_unreachable_store_ends_with_status_two_and_keeps_the_inputs_for_the_next_run(self):
        code, _, err = self.stage()
        self.assertEqual(code, 2)
        self.assertIn("shadow store", err)
        self.assertTrue((self.root / RUN / "inputs" / "views.jsonl").exists())

    def test_clean_removes_the_inputs_and_keeps_the_report(self):
        self.stage(base=self.store().base)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(stage.main(["--root", str(self.root), "clean", "--run-id", RUN]), 0)
            self.assertEqual(stage.main(["--root", str(self.root), "clean", "--run-id", RUN]), 0)
        self.assertEqual([line.rsplit(": ", 1)[1] for line in printed.getvalue().splitlines()], ["removed", "absent"])
        self.assertEqual(sorted(item.name for item in (self.root / RUN).iterdir()), ["report", "walk.json"])


class Credentials(unittest.TestCase):
    def test_the_store_secret_comes_from_the_environment_or_from_a_running_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            for name, variables in (("1", b"PATH=/bin\0HOME=/root\0"), ("40", b"MODE=store\0STORE_READ_SECRET=from-process\0"), ("self", b"")):
                (proc / name).mkdir()
                (proc / name / "environ").write_bytes(variables)
            (proc / "7").mkdir()
            self.assertEqual(stage.store_secret({stage.STORE_SECRET: "from-here"}, proc), "from-here")
            self.assertEqual(stage.store_secret({}, proc), "from-process")
            (proc / "40" / "environ").write_bytes(b"STORE_READ_SECRET=\0")
            with self.assertRaises(stage.StageError):
                stage.store_secret({}, proc)

    def test_r2_values_become_an_rclone_remote_and_missing_ones_are_named(self):
        environ = {"COMPARE_R2_ENDPOINT": "https://account.r2.example", "COMPARE_R2_BUCKET": "bucket", "COMPARE_R2_ACCESS_KEY_ID": "key-id",
                   "COMPARE_R2_SECRET_ACCESS_KEY": "key-secret", "COMPARE_R2_SESSION_TOKEN": "session-token"}
        base, env = stage.remote(environ)
        self.assertEqual(base, "compare:bucket/scratch/shadow-compare")
        self.assertEqual(
            {name[len("RCLONE_CONFIG_COMPARE_"):]: value for name, value in env.items() if name.startswith("RCLONE_CONFIG_COMPARE_")},
            {"TYPE": "s3", "PROVIDER": "Cloudflare", "REGION": "auto", "NO_CHECK_BUCKET": "true", "ENDPOINT": "https://account.r2.example",
             "ACCESS_KEY_ID": "key-id", "SECRET_ACCESS_KEY": "key-secret", "SESSION_TOKEN": "session-token"},
        )
        with self.assertRaises(stage.StageError) as caught:
            stage.remote({"COMPARE_R2_BUCKET": "bucket"})
        self.assertEqual(str(caught.exception), "unset: COMPARE_R2_ENDPOINT, COMPARE_R2_ACCESS_KEY_ID, COMPARE_R2_SECRET_ACCESS_KEY")
        text = stage.redact("denied for key-id with key-secret and session-token", {**environ, stage.STORE_SECRET: "bearer"})
        self.assertEqual(text, "denied for [COMPARE_R2_ACCESS_KEY_ID] with [COMPARE_R2_SECRET_ACCESS_KEY] and [COMPARE_R2_SESSION_TOKEN]")


if __name__ == "__main__":
    unittest.main()
