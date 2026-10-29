import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


fixture_module = module("source_fixture_current", "test_sync.py")
current = module("current_state_tested", "current_state.py")
sync = fixture_module.sync


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".test-current-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = fixture_module.Fixture(Path(self.tmp.name))
        self.addCleanup(self.fixture.close)
        self.reader = current.CurrentStateSource(self.fixture.source(), sync)

    def test_source_identity_walk_recovers_arbitrary_backdated_new_actor(self):
        f = self.fixture
        f.actor("did:example:backdated", f.floor - 10000)
        self.assertTrue(f.runner().run()["caught_up"])
        self.assertNotIn("did:example:backdated", f.actors)
        self.assertEqual(self.reader.source_page(), ["did:example:backdated"])
        observed = self.reader.observe(["did:example:backdated"])
        self.assertEqual(observed["source_now"], f.now)
        self.assertEqual(
            observed["observations"][0]["actor"]["version"], f.floor - 10000
        )
        self.assertFalse(observed["observations"][0]["actor"]["deleted"])

    def test_current_read_resolves_absence_without_retained_tombstone_and_preserves_hidden(
        self,
    ):
        f = self.fixture
        f.actor("did:example:hidden", f.floor + 10, hidden=True)
        f.actor("did:example:cleared", f.floor + 11, handle="")
        observed = self.reader.observe(
            ["did:example:hidden", "did:example:absent", "did:example:cleared"]
        )
        rows = {item["actor"]["did"]: item for item in observed["observations"]}
        self.assertTrue(rows["did:example:absent"]["actor"]["deleted"])
        self.assertIsNone(rows["did:example:absent"]["source_updated_at"])
        self.assertTrue(rows["did:example:cleared"]["actor"]["deleted"])
        self.assertTrue(rows["did:example:hidden"]["actor"]["hidden"])
        self.assertFalse(rows["did:example:hidden"]["actor"]["deleted"])

    def test_same_second_moderation_and_unversioned_activity_have_fresh_current_payload(
        self,
    ):
        f = self.fixture
        did = "did:example:moderated"
        f.actor(did, f.floor + 10)
        first = self.reader.observe([did])["observations"][0]["actor"]
        with closing(sqlite3.connect(f.path)) as db, db:
            db.execute(
                "UPDATE actors SET hidden=1, last_activity_at=99 WHERE did=?", [did]
            )
        second = self.reader.observe([did])["observations"][0]["actor"]
        self.assertEqual(first["version"], second["version"])
        self.assertFalse(first["hidden"])
        self.assertTrue(second["hidden"])
        self.assertEqual(second["last_activity_at"], 99)
        self.assertNotEqual(first, second)

    def test_selected_identities_are_bounded_unique_and_indexed(self):
        for dids in [
            [],
            ["did:example:x"] * 2,
            ["bad"],
            ["did:example:x" + str(i) for i in range(501)],
        ]:
            with self.assertRaises(sync.SafeError):
                self.reader.observe(dids)
        self.assertEqual(self.fixture.requests, [])
        with closing(sqlite3.connect(self.fixture.path)) as db:
            plan = [
                row[-1]
                for row in db.execute(
                    "EXPLAIN QUERY PLAN " + sync.CURRENT_SQL, ['["did:example:x"]']
                )
            ]
            self.assertTrue(
                any(
                    "SEARCH a USING INDEX sqlite_autoindex_actors" in line
                    for line in plan
                ),
                plan,
            )
            self.assertFalse(any(line.startswith("SCAN a") for line in plan), plan)
            plan = [
                row[-1]
                for row in db.execute(
                    "EXPLAIN QUERY PLAN " + sync.SOURCE_WALK_SQL, ["", 500]
                )
            ]
            self.assertTrue(
                any(
                    "SEARCH actors USING COVERING INDEX sqlite_autoindex_actors" in line
                    for line in plan
                ),
                plan,
            )


class PolicyTests(unittest.TestCase):
    def setUp(self):
        Tests.setUp(self)
        self.policy_reader = current.CurrentPolicySource(self.fixture.source(), sync)

    def test_policy_and_actor_are_one_statement_and_ignore_materialization_lag(self):
        f = self.fixture
        did = "did:example:policy"
        stamp = f.floor + 10
        f.actor(did, stamp, handle="x.blocked.test", hidden=False)
        with closing(sqlite3.connect(f.path)) as db, db:
            db.execute("INSERT INTO blocked_domains(domain) VALUES ('blocked.test')")
            db.execute("INSERT INTO mod_overrides(did,action) VALUES (?,'show')", [did])
        f.requests.clear()
        first = self.policy_reader.observe_with_policy([did])
        self.assertEqual(len(f.requests), 1)
        self.assertEqual(f.requests[0]["sql"], sync.CURRENT_POLICY_SQL)
        self.assertEqual(first["observations"][0]["actor"]["version"], stamp)
        self.assertFalse(first["observations"][0]["actor"]["hidden"])
        self.assertEqual(
            first["moderation"]["overrides"], [{"did": did, "action": "show"}]
        )
        self.assertEqual(first["moderation"]["blocked_domains"], ["blocked.test"])
        with closing(sqlite3.connect(f.path)) as db, db:
            db.execute("DELETE FROM mod_overrides WHERE did=?", [did])
        second = self.policy_reader.observe_with_policy([did])
        self.assertEqual(second["observations"], first["observations"])
        self.assertNotEqual(second["moderation_sha256"], first["moderation_sha256"])
        self.assertEqual(second["moderation"]["overrides"], [])
        with closing(sqlite3.connect(f.path)) as db, db:
            db.execute("DELETE FROM blocked_domains")
        third = self.policy_reader.observe_with_policy([])
        self.assertEqual(third["observations"], [])
        self.assertEqual(third["moderation"]["blocked_domains"], [])
        self.assertNotEqual(third["moderation_sha256"], second["moderation_sha256"])

    def test_policy_cap_is_complete_or_rejected_and_unknown_action_fails_closed(self):
        f = self.fixture
        with closing(sqlite3.connect(f.path)) as db, db:
            db.executemany(
                "INSERT INTO mod_overrides(did,action) VALUES (?,?)",
                [(f"did:example:p{i:05d}", "hide") for i in range(10001)],
            )
        with self.assertRaisesRegex(sync.SafeError, "exceeds bounded admission"):
            self.policy_reader.observe_with_policy([])
        with closing(sqlite3.connect(f.path)) as db, db:
            db.execute("DELETE FROM mod_overrides")
            db.execute(
                "INSERT INTO mod_overrides(did,action) VALUES ('did:example:invalid','invalid')"
            )
        with self.assertRaisesRegex(sync.SafeError, "ambiguous moderation override"):
            self.policy_reader.observe_with_policy([])

    def test_domain_policy_cap_rejects_without_dropping_any_rule(self):
        f = self.fixture
        with closing(sqlite3.connect(f.path)) as db, db:
            db.executemany(
                "INSERT INTO blocked_domains(domain) VALUES (?)",
                [(f"blocked{i}.test",) for i in range(10001)],
            )
        with self.assertRaisesRegex(sync.SafeError, "exceeds bounded admission"):
            self.policy_reader.observe_with_policy([])

    def test_policy_envelope_profile_lookup_is_indexed(self):
        with closing(sqlite3.connect(self.fixture.path)) as db:
            plan = [
                row[-1]
                for row in db.execute(
                    "EXPLAIN QUERY PLAN " + sync.CURRENT_POLICY_SQL,
                    ['["did:example:x"]'],
                )
            ]
        self.assertTrue(
            any(
                "SEARCH a USING INDEX sqlite_autoindex_actors" in line for line in plan
            ),
            plan,
        )
        self.assertFalse(any(line.startswith("SCAN a ") for line in plan), plan)


if __name__ == "__main__":
    unittest.main()
