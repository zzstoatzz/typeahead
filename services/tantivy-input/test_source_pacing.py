"""Actual HTTP/SQLite source + frozen Rust backend; no external service access."""

import copy
import hashlib
import importlib.util
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = Path(
    os.environ.get(
        "TA_BINARY", ROOT.parent / "tantivy/target/release/typeahead-tantivy"
    )
)


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


fixture = module("pacing_fixture", "test_sync.py")
sync = fixture.sync
current = module("pacing_current", "current_state.py")


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(
    BIN.is_file(), "requires prebuilt actual Tantivy binary; no build performed"
)
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".test-pacing-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f = fixture.Fixture(self.root, now=int(time.time()))
        self.addCleanup(self.f.close)
        self.ids = ["did:example:" + name for name in ["a", "b", "c"]]
        for did in self.ids:
            self.f.actor(did, self.f.floor - 1)
        reader = current.CurrentStateSource(self.f.source(), sync)
        actors = [
            row["actor"] | {"version": self.f.floor}
            for row in reader.observe(self.ids)["observations"]
        ]
        lines = [sync.json_bytes(actor) + b"\n" for actor in actors]
        header = {
            "type": "manifest",
            "build_id": "fixture-source-pacing",
            "source_watermark": self.f.floor,
            "actor_count": 3,
            "source_actor_count": 3,
            "partial": False,
            "normalize_version": "v2-cjk-bigram",
        }
        seed = self.root / "seed.jsonl"
        seed.write_bytes(
            sync.json_bytes(header)
            + b"\n"
            + b"".join(lines)
            + sync.json_bytes(
                {
                    "type": "end",
                    "build_id": header["build_id"],
                    "actor_count": 3,
                    "actors_sha256": hashlib.sha256(b"".join(lines)).hexdigest(),
                }
            )
            + b"\n"
        )
        result = subprocess.run(
            [
                str(BIN),
                "import",
                "--data",
                str(self.root / "index"),
                "--input",
                str(seed),
                "--writer-memory-mb",
                "32",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        public, admin = port(), port()
        self.base = f"http://127.0.0.1:{public}"
        private = f"http://127.0.0.1:{admin}"
        self.secret = "local-pacing-secret"
        self.log = (self.root / "backend.log").open("wb")
        self.addCleanup(self.log.close)
        self.child = subprocess.Popen(
            [
                str(BIN),
                "serve",
                "--data",
                str(self.root / "index"),
                "--listen",
                f"127.0.0.1:{public}",
                "--admin-listen",
                f"127.0.0.1:{admin}",
                "--writer-memory-mb",
                "32",
                "--allow-stale",
                "true",
            ],
            env={"PATH": os.environ.get("PATH", ""), "ADMIN_SECRET": self.secret},
            stdout=self.log,
            stderr=self.log,
        )
        self.addCleanup(self.stop)
        self.admin = sync.Admin(
            private, self.secret, self.base + "/index/status", http=sync.Http(3)
        )
        for _ in range(100):
            if self.child.poll() is not None:
                self.fail("real Rust backend exited")
            try:
                self.admin.status()
                break
            except OSError:
                time.sleep(0.02)
            except sync.Retryable:
                time.sleep(0.02)
        else:
            self.fail("real Rust backend did not start")
        self.source = self.f.source()
        self.runner(anti_entropy_pages=0).run()

    def stop(self):
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=3)

    def runner(self, source=None, **settings):
        defaults = {
            "page_size": 2,
            "max_pages": 100,
            "max_seconds": 10,
            "retries": 1,
            "backoff": 0,
            "policy_mode": "snapshot",
            "anti_entropy_pages": 0,
            "sweep_cooldown_seconds": 0,
            "max_sweep_seconds": 5,
        }
        defaults.update(settings)
        return current.ObservedSynchronizer(
            source or self.source, self.admin, sync, **defaults
        )

    def test_cursor_only_known_source_page_does_not_refresh_policy_or_watermark(self):
        before = self.admin.status()
        cursor = copy.deepcopy(before["source_cursor"])
        cursor["reconciliation"] = {
            "schema_version": 1,
            "round": 1,
            "source_after_did": self.ids[-1],
            "backend_after_did": "",
            "source_done": False,
            "backend_done": False,
            "round_started_at": self.f.now,
            "last_completed_at": None,
            "last_completed_started_at": None,
        }
        count = len(self.f.requests)
        self.f.now += 1
        writer = current.ObservationWriter(
            self.source, self.admin, sync, policy_mode="snapshot"
        )
        reply = writer.commit(
            self.ids, before["source_cursor"], cursor, only_missing=True
        )
        after = self.admin.status()
        self.assertEqual(len(self.f.requests), count)
        self.assertEqual(reply["applied"], 0)
        self.assertEqual(
            after["source_observation_revision"],
            before["source_observation_revision"] + 1,
        )
        self.assertEqual(after["source_cursor"], cursor)
        self.assertEqual(after["watermark"], before["watermark"])
        self.assertEqual(
            after["moderation"]["source_observed_at"],
            before["moderation"]["source_observed_at"],
        )
        self.assertEqual(after["moderation"]["sha256"], before["moderation"]["sha256"])
        self.assertEqual(after["indexed_memberships"], before["indexed_memberships"])

    def test_missing_or_changed_actor_still_observes_policy_in_same_sql_statement(self):
        did = "did:example:new-old"
        self.f.actor(did, self.f.floor - 5, handle="new-old.fixture.test")
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute(
                "INSERT INTO mod_overrides(did,action) VALUES(?,?)", (did, "hide")
            )
        writer = current.ObservationWriter(
            self.source, self.admin, sync, policy_mode="snapshot"
        )
        before = self.admin.status()
        count = len(self.f.requests)
        writer.commit(
            [self.ids[0], did],
            before["source_cursor"],
            before["source_cursor"],
            only_missing=True,
        )
        self.assertEqual(
            [request["sql"] for request in self.f.requests[count:]],
            [sync.CURRENT_POLICY_SQL],
        )
        self.assertEqual(
            sync.Http(3).request(self.base + "/search?q=new-old", self.secret)[0][
                "actors"
            ],
            [],
        )
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute("DELETE FROM mod_overrides WHERE did=?", [did])
            db.execute(
                "UPDATE actors SET handle=? WHERE did=?",
                ("changed-old.fixture.test", did),
            )
        self.f.now += 1
        before = self.admin.status()
        count = len(self.f.requests)
        reply = writer.commit([did], before["source_cursor"], before["source_cursor"])
        self.assertEqual(reply["applied"], 1)
        self.assertEqual(
            [request["sql"] for request in self.f.requests[count:]],
            [sync.CURRENT_POLICY_SQL],
        )
        actors = sync.Http(3).request(self.base + "/search?q=changed-old", self.secret)[
            0
        ]["actors"]
        self.assertEqual([actor["did"] for actor in actors], [did])

    def test_exhausted_sweep_budget_preserves_polling_and_never_resets_counters(self):
        for i in range(64):
            self.f.actor("did:example:budget-" + str(i) + "x" * 96, self.f.floor - 1)
        source = sync.Turso(
            self.f.url,
            self.f.source_secret,
            allow_test_loopback=True,
            retries=1,
            max_bytes=24000,
            poll_reserve_bytes=22000,
        )
        with source.budget_scope("sweep"), self.assertRaises(sync.SweepBudgetExhausted):
            current.CurrentStateSource(source, sync).source_page("", 500)
        spent = source.sweep_bytes
        requests = source.requests
        self.assertEqual(spent, 2000)
        self.assertEqual(source.bytes, 2000)
        with source.budget_scope("sweep"), self.assertRaises(sync.SweepBudgetExhausted):
            source.now()
        self.assertEqual(source.requests, requests)
        self.f.now += 1
        result = self.runner(source=source, anti_entropy_pages=2).run()
        self.assertTrue(result["caught_up"])
        self.assertEqual(result["sweep_pause"], "budget")
        self.assertEqual(self.admin.status()["watermark"], self.f.now - 2)
        self.assertEqual(
            self.admin.status()["moderation"]["source_observed_at"], self.f.now
        )
        self.assertEqual(source.sweep_bytes, spent)
        self.assertGreater(source.poll_bytes, 0)
        self.assertEqual(source.bytes, source.poll_bytes + source.sweep_bytes)
        self.assertLessEqual(source.bytes, source.max_bytes)
        self.assertIsNone(result["reconciliation"])

    def test_timeslice_progress_and_completed_round_cooldown_survive_recreated_runner(
        self,
    ):
        base = self.source

        class SlowSource:
            def __getattr__(self, name):
                return getattr(base, name)

            def select(self, sql, args):
                rows = base.select(sql, args)
                if sql == sync.SOURCE_WALK_SQL:
                    time.sleep(0.03)
                return rows

        first = self.runner(
            source=SlowSource(), anti_entropy_pages=20, max_sweep_seconds=0.01
        ).run()
        self.assertEqual(first["anti_entropy_pages"], 1)
        self.assertEqual(first["sweep_pause"], "time-slice")
        started = first["reconciliation"]["round_started_at"]
        self.assertIsNone(first["reconciliation"]["last_completed_at"])
        self.f.now += 1
        complete = self.runner(anti_entropy_pages=100).run()
        self.assertEqual(
            complete["reconciliation"]["last_completed_started_at"], started
        )
        completed = complete["reconciliation"]["last_completed_at"]
        count = len(self.f.requests)
        paused = self.runner(anti_entropy_pages=100, sweep_cooldown_seconds=100).run()
        self.assertEqual(paused["anti_entropy_pages"], 0)
        self.assertEqual(paused["sweep_pause"], "cooldown")
        self.assertNotIn(
            sync.SOURCE_WALK_SQL,
            [request["sql"] for request in self.f.requests[count:]],
        )
        self.f.now += 101
        next_round = self.runner(anti_entropy_pages=1, sweep_cooldown_seconds=100).run()
        self.assertEqual(next_round["reconciliation"]["round_started_at"], self.f.now)
        self.assertEqual(next_round["reconciliation"]["last_completed_at"], completed)
        self.assertEqual(
            self.admin.status()["source_cursor"], next_round["source_cursor"]
        )

    def test_midpage_budget_exhaustion_commits_no_partial_cursor_or_documents(self):
        did = "did:example:0-missing"
        self.f.actor(did, self.f.floor - 1)
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute("UPDATE actors SET avatar_url=? WHERE did=?", ["x" * 5000, did])
        source = sync.Turso(
            self.f.url,
            self.f.source_secret,
            allow_test_loopback=True,
            retries=1,
            max_bytes=24000,
            poll_reserve_bytes=22500,
        )
        before = self.admin.status()
        result = self.runner(source=source, anti_entropy_pages=2).run()
        after = self.admin.status()
        self.assertEqual(result["sweep_pause"], "budget")
        self.assertEqual(result["anti_entropy_pages"], 0)
        self.assertIsNone(result["reconciliation"])
        self.assertEqual(after["actor_count"], before["actor_count"])
        self.assertEqual(after["indexed_memberships"], before["indexed_memberships"])
        self.assertEqual(source.sweep_bytes, 1500)
        self.assertTrue(self.runner(source=source).run()["caught_up"])

    def test_total_budget_exhaustion_remains_explicit_without_reset(self):
        source = sync.Turso(
            self.f.url,
            self.f.source_secret,
            allow_test_loopback=True,
            retries=1,
            max_bytes=512,
            poll_reserve_bytes=256,
        )
        with self.assertRaises(sync.SourceBudgetExhausted):
            for _ in range(20):
                source.now()
        self.assertLessEqual(source.bytes, 512)
        requests = source.requests
        with self.assertRaises(sync.SourceBudgetExhausted):
            source.now()
        self.assertEqual(source.requests, requests)


if __name__ == "__main__":
    unittest.main()
