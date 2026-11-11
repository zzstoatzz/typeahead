import json
import unittest
from pathlib import Path

import moderation
from moderation import ModerationError, Policy, Rules

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "services" / "tantivy" / "tests" / "moderation" / "cases.json"
MOD = "did:plc:ar7c4by46qjdydhdevvrndac"
NOW_MS = 1_794_355_200_000


def outcome(evaluate):
    try:
        return {"ok": True, "value": evaluate()}
    except ModerationError:
        return {"ok": False}


class DifferentialTests(unittest.TestCase):
    """Every case the Worker's own functions answered, which the Rust evaluator is also held to."""

    @classmethod
    def setUpClass(cls):
        cls.rules = Rules.load()
        cls.fixture = json.loads(CASES.read_text())

    def test_the_rules_are_the_ones_the_cases_were_generated_from(self):
        self.assertEqual(self.rules.sha256, self.fixture["canonical_source_sha256"])
        self.assertEqual(self.rules.labeler, MOD)

    def test_every_case_matches_the_worker_or_is_refused_like_the_server(self):
        checked = refused = 0
        for case in self.fixture["cases"]:
            actor, expected, now = case["actor"], case["expected"], case["now_millis"]
            policy = Policy.from_snapshot(self.rules, case["snapshot"])
            found = {
                "slur": outcome(lambda: self.rules.slur(actor["handle"])),
                "label_hide": outcome(lambda: self.rules.labels_hide(actor["labels"], now)),
                "domain": outcome(lambda: policy.domain_blocked(actor["handle"])),
                "hidden": outcome(lambda: policy.hidden(actor["did"], actor["handle"], actor["labels"], now)),
            }
            if case["expiry_unsupported"]:
                refused += 1
                self.assertEqual((found["label_hide"], found["hidden"]["ok"]), ({"ok": False}, False), case["id"])
                continue
            checked += 1
            self.assertEqual(found, expected, case["id"])
        self.assertEqual((checked, refused), (2143, 27))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.rules = Rules.load()

    def label(self, value, source=MOD, **extra):
        return {"src": source, "uri": "did:plc:abc", "val": value, "cts": "2026-01-01T00:00:00Z", **extra}

    def test_only_live_labels_from_the_moderation_service_hide(self):
        hide = self.rules.labels_hide
        for value in ("!hide", "!takedown", "!suspend", "spam"):
            self.assertTrue(hide([self.label(value)], NOW_MS))
        self.assertFalse(hide([self.label("!no-unauthenticated")], NOW_MS))
        self.assertFalse(hide([self.label("spam", "did:plc:someoneelse")], NOW_MS))
        self.assertFalse(hide([], NOW_MS))
        self.assertFalse(hide([self.label("spam", neg=True)], NOW_MS))
        expiring = self.label("!hide", exp="2026-11-11T00:00:00Z")
        self.assertFalse(hide([expiring], NOW_MS))
        self.assertTrue(hide([expiring], NOW_MS - 1))
        self.assertTrue(hide([self.label("spam", neg=True), self.label("x"), self.label("!suspend")], NOW_MS))

    def test_a_domain_block_covers_the_apex_and_everything_under_it(self):
        policy = Policy(self.rules, domains=["Spam.example", "dadavidtseng.com"])
        for handle in ("spam.example", "a.Spam.Example", "a.b.spam.example", "peachisle4555424.pds.dadavidtseng.com", "X.DaDavidTseng.com"):
            self.assertTrue(policy.domain_blocked(handle), handle)
        for handle in ("notspam.example", "spam.example.org", "example", "", "evil-dadavidtseng.com", "dadavidtseng.com.evil.com"):
            self.assertFalse(policy.domain_blocked(handle), handle)
        self.assertFalse(Policy(self.rules).domain_blocked("a.spam.example"))

    def test_the_override_is_the_final_word(self):
        hiding = [self.label("!takedown")]
        policy = Policy(self.rules, {"did:plc:shown": False, "did:plc:hidden": True}, ["blocked.test"])
        self.assertFalse(policy.hidden("did:plc:a", "a.test", [], NOW_MS))
        self.assertTrue(policy.hidden("did:plc:a", "a.test", hiding, NOW_MS))
        self.assertTrue(policy.hidden("did:plc:a", "a.blocked.test", [], NOW_MS))
        self.assertFalse(policy.hidden("did:plc:shown", "a.blocked.test", hiding, NOW_MS))
        self.assertTrue(policy.hidden("did:plc:hidden", "a.test", [], NOW_MS))

    def test_a_records_own_override_applies_only_where_the_policy_is_silent(self):
        policy = Policy(self.rules, {"did:plc:hidden": True})
        self.assertFalse(policy.hidden("did:plc:a", "a.test", [self.label("spam")], NOW_MS, override=False))
        self.assertTrue(policy.hidden("did:plc:a", "a.test", [], NOW_MS, override=True))
        self.assertTrue(policy.hidden("did:plc:hidden", "a.test", [], NOW_MS, override=False))

    def test_a_search_returns_only_a_live_actor_with_a_usable_handle_that_is_not_hidden(self):
        policy = Policy(self.rules)
        self.assertTrue(policy.returned("did:plc:a", False, "a.test", [], NOW_MS))
        self.assertFalse(policy.returned("did:plc:a", True, "a.test", [], NOW_MS))
        self.assertFalse(policy.returned("did:plc:a", False, "", [], NOW_MS))
        self.assertFalse(policy.returned("did:plc:a", False, "Handle.Invalid", [], NOW_MS))
        self.assertFalse(policy.returned("did:plc:a", False, "a.test", [self.label("spam")], NOW_MS))

    def test_a_snapshot_the_server_refuses_is_refused(self):
        good = {"schema_version": 1, "source_observed_at": 5, "complete": True, "overrides": [{"did": "did:plc:a", "action": "show"}], "blocked_domains": ["B.test"]}
        policy = Policy.from_snapshot(self.rules, good)
        self.assertEqual(policy.describe(), {"source": "snapshot", "rules_sha256": self.rules.sha256, "overrides": 1, "blocked_domains": 1})
        self.assertTrue(policy.domain_blocked("x.b.test"))
        for change in (
            {"schema_version": 2}, {"complete": False}, {"source_observed_at": 0}, {"blocked_domains": None},
            {"overrides": [{"did": "did:plc:a", "action": "mute"}]}, {"overrides": [{"did": "plc:a", "action": "show"}]},
            {"overrides": [{"did": "did:plc:a", "action": "show"}, {"did": "did:plc:a", "action": "hide"}]},
        ):
            with self.assertRaises(ModerationError):
                Policy.from_snapshot(self.rules, {**good, **change})
        with self.assertRaises(ModerationError):
            Policy.from_snapshot(self.rules, [])

    def test_expiry_forms(self):
        self.assertEqual(moderation.expiry_ms("1970-01-02"), 86_400_000)
        self.assertEqual(moderation.expiry_ms("1970-01-01T00:00:01.5Z"), 1500)
        self.assertEqual(moderation.expiry_ms("1970-01-01T01:00:00+01:00"), 0)
        self.assertEqual(moderation.expiry_ms("1969-12-31T23:59:59.999999Z"), -1)
        self.assertEqual(moderation.expiry_ms(12.9), 12)
        for value in ("2026", "2026-02-30", "2026-01-01T24:00:00Z", "2026-01-01T00:00:00+0000", "2026-01-01T00:00Z", "2026-01-01T00:00:60Z", {}, 1e17):
            with self.assertRaises(ModerationError):
                moderation.expiry_ms(value)


if __name__ == "__main__":
    unittest.main()
