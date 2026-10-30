import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


install_tests = module(
    "live_audit_install_tests",
    ROOT / "scripts/snapshot_refresh/test_tantivy_install.py",
)
audit = module(
    "bounded_live_source_audit", Path(__file__).with_name("live-source-audit.py")
)
BINARY = Path(
    os.environ.get(
        "TA_BINARY", ROOT / "services/tantivy/target/release/typeahead-tantivy"
    )
)


@unittest.skipUnless(
    BINARY.is_file() and os.environ.get("TA_MPS_REPOSITORY"),
    "requires prebuilt D1 and existing MPS checkpoint helpers",
)
class Tests(unittest.TestCase):
    def exercise(self, *, known=True, drift=False):
        with (
            tempfile.TemporaryDirectory() as temp,
            install_tests.checkpoint_runtime_fixture(Path(temp), BINARY) as fixture,
        ):
            source = fixture["source"]
            source.now = int(time.time())
            source.actor("did:plc:cleared", source.now - 3, handle="")
            source.tombstone("did:plc:deleted", source.now - 3)
            with sqlite3.connect(source.path) as db:
                db.execute(
                    "INSERT INTO mod_overrides(did,action) VALUES('did:plc:fixture000002','hide')"
                )
            public = "http://127.0.0.1:" + str(install_tests.smoke.port())
            admin = "http://127.0.0.1:" + str(install_tests.smoke.port())
            child = subprocess.Popen(
                [
                    str(BINARY),
                    "serve",
                    "--data",
                    str(fixture["data"]),
                    "--listen",
                    public[len("http://") :],
                    "--admin-listen",
                    admin[len("http://") :],
                    "--retrieval",
                    "global-merge",
                    "--max-sync-lag-s",
                    "120",
                ],
                env=dict(os.environ, ADMIN_SECRET=fixture["secret"]),
                stdout=fixture["log"],
                stderr=fixture["log"],
            )
            for _ in range(100):
                try:
                    if install_tests.smoke.request(public, "/health")[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.02)
            else:
                raise AssertionError("strict global reader failed to start")
            try:
                replay = fixture["replay"]
                current = fixture["current"]
                control = replay.Admin(
                    admin, fixture["secret"], public + "/index/status"
                )
                result = current.ObservedSynchronizer(
                    source.source(),
                    control,
                    replay,
                    policy_mode="snapshot",
                    anti_entropy_pages=0,
                    safety_lag=0,
                    max_pages=10,
                    max_seconds=10,
                    retries=1,
                    backoff=0,
                ).run()
                self.assertTrue(result["caught_up"])
                if drift:
                    with sqlite3.connect(source.path) as db:
                        db.execute(
                            "UPDATE actors SET display_name='Private changed marker' WHERE did='did:plc:fixture000001'"
                        )
                before = control.status()
                source.requests.clear()
                receipt = audit.run(
                    public,
                    fixture["manifest"]["active_build"],
                    hashlib.sha256(BINARY.read_bytes()).hexdigest(),
                    {
                        "TURSO_URL": source.url,
                        "TURSO_AUTH_TOKEN": source.source_secret,
                        "ADMIN_SECRET": fixture["secret"],
                    },
                    input_path=ROOT / "services/tantivy-input",
                    admin_url=admin,
                    known_handles={
                        "did:plc:cleared": "cleared.fixture.test",
                        "did:plc:deleted": "deleted.fixture.test",
                    }
                    if known
                    else {},
                    fixture=True,
                )
                after = control.status()
                self.assertEqual(before["source_cursor"], after["source_cursor"])
                self.assertEqual(
                    before["source_observation_revision"],
                    after["source_observation_revision"],
                )
                self.assertIsNone(receipt["fatal_error_class"], receipt)
                self.assertEqual(receipt["source_selects"], 5)
                self.assertLessEqual(receipt["sampled_actors"], 8)
                self.assertLessEqual(receipt["sampled_tombstones"], 8)
                self.assertLessEqual(receipt["requests"], 64)
                self.assertEqual(receipt["backend_writes"], 0)
                self.assertEqual(receipt["source_writes"], 0)
                self.assertFalse(receipt["cursor_refreshed"])
                self.assertNotIn("Private changed marker", json.dumps(receipt))
                self.assertNotIn(source.source_secret, json.dumps(receipt))
                return receipt
            finally:
                install_tests.smoke.stop(child)

    def test_real_source_d1_scope_moderation_and_deleted_witnesses_without_cursor_writes(
        self,
    ):
        receipt = self.exercise()
        self.assertTrue(receipt["passed"], receipt)
        self.assertGreater(receipt["present_witnesses"], 0)
        self.assertGreater(receipt["source_backed_pds_pairs"], 0)
        self.assertGreater(receipt["moderated_absence_witnesses"], 0)
        self.assertEqual(receipt["deleted_absence_witnesses"], 2)

    def test_missing_historical_handles_are_unavailable_not_a_deleted_pass(self):
        receipt = self.exercise(known=False)
        self.assertTrue(receipt["passed"], receipt)
        self.assertEqual(receipt["deleted_absence_witnesses"], 0)
        self.assertIn(
            "no-deleted-known-handle-absence-witness", receipt["unavailable_witnesses"]
        )

    def test_stable_source_hydration_drift_is_detected_without_repair(self):
        receipt = self.exercise(drift=True)
        self.assertFalse(receipt["passed"])
        self.assertIsNone(receipt["fatal_error_class"])
        self.assertTrue(
            any(
                "public-hydration" in item["check"] and item["passed"] is False
                for item in receipt["checks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
