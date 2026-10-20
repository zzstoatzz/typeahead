import sqlite3
import tempfile
import unittest
from pathlib import Path
from pds_identity_queue import IdentityQueue


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'queue.db'
        self.queue = IdentityQueue(self.path)
        self.queue.enqueue([('did:plc:account', 100)], 1000)

    def claim(self, at=1000):
        return self.queue.claim(at, limit=10, lease_seconds=60)

    def test_retry_survives_restart_and_repeated_discovery(self):
        claim = self.claim()[0]
        self.queue.finish(claim, 1001, verified=False, retry_seconds=300)
        self.queue = IdentityQueue(self.path)
        self.queue.enqueue([('did:plc:account', 100)], 1002)
        self.queue.enqueue([('did:plc:account', 90)], 1003)
        self.assertEqual(self.claim(1100), [])
        self.assertEqual(len(self.claim(1301)), 1)

    def test_crashed_lease_excludes_other_worker_until_expiry(self):
        first = self.claim()[0]
        other = IdentityQueue(self.path)
        self.assertEqual(other.claim(1010, limit=1, lease_seconds=60), [])
        replacement = self.claim(1060)[0]
        with self.assertRaisesRegex(ValueError, 'lease'):
            self.queue.finish(first, 1061, verified=True)
        self.queue.finish(replacement, 1061, verified=True)
        self.assertEqual(self.claim(2000), [])

    def test_new_observation_during_lease_is_not_lost(self):
        first = self.claim()[0]
        self.queue.enqueue([('did:plc:account', 101)], 1001)
        self.queue.finish(first, 1002, verified=True)
        second = self.claim(1002)[0]
        self.assertEqual(second['version'], 101)
        self.queue.finish(second, 1003, verified=True)
        self.queue.enqueue([('did:plc:account', 100)], 1004)
        backdated = self.claim(1004)
        self.assertEqual(len(backdated), 1)
        self.assertEqual(backdated[0]['version'], 101)

    def test_same_second_missing_observation_is_rechecked(self):
        claim = self.claim()[0]
        self.queue.finish(claim, 1001, verified=True)
        self.queue.enqueue([('did:plc:account', 100)], 1002)
        self.assertEqual(len(self.claim(1002)), 1)

    def test_limits_and_expired_receipt(self):
        with self.assertRaises(ValueError):
            self.queue.claim(1000, limit=101, lease_seconds=60)
        claim = self.claim()[0]
        with self.assertRaises(ValueError):
            self.queue.finish(claim, 1060, verified=True)

    def test_due_selector_uses_partial_index(self):
        with sqlite3.connect(self.path) as db:
            plans = db.execute('EXPLAIN QUERY PLAN SELECT did FROM identity_jobs '
                'WHERE version > completed_version AND due <= ? ORDER BY due', (1000,)).fetchall()
        self.assertTrue(any('SEARCH identity_jobs USING INDEX identity_due' in row[3] for row in plans))


if __name__ == '__main__':
    unittest.main()
