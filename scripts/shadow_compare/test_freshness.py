import unittest

import freshness
from test_search import backend


class FreshnessTests(unittest.TestCase):
    def status(self, **changes):
        base = {"ready": True, "sync_lag_s": 4, "watermark": 100, "build_id": "b1", "normalize_version": "n", "plan_version": "p",
                "format": "f", "moderation": {"fresh": True, "age_s": 3, "sha256": "policy", "canonical_rules_source_sha256": "rules"}}
        base.update(changes)
        return base

    def test_window_reports_lag_and_agreement(self):
        production, shadow = self.status(), self.status(sync_lag_s=9, plan_version="q")
        slept = []
        result = freshness.watch(backend({}, status=production), backend({}, status=shadow), samples=3, interval=11, sleep=slept.append)
        self.assertEqual(slept, [11, 11])
        self.assertEqual(result["summary"]["shadow"]["max_sync_lag_s"], 9)
        self.assertTrue(result["agreement"]["moderation_policy_sha256"]["matched"])
        self.assertFalse(result["agreement"]["plan_version"]["matched"])
        self.assertTrue(all(s["ready"] and s["moderation_fresh"] for s in result["samples"]["production"]))

    def test_unready_backend_is_recorded(self):
        result = freshness.watch(backend({}, status=self.status()), backend({}, status=self.status(ready=False)), samples=1, interval=0)
        self.assertFalse(result["samples"]["shadow"][0]["ready"])


if __name__ == "__main__":
    unittest.main()
