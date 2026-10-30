import sqlite3
import tempfile
import unittest
from pathlib import Path
from pds_freshness_queue import Queue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'queue.db'
        self.q = Queue(self.path)
        self.tokens = {}

    def claim(self, queue=None, **kwargs):
        claims = (queue or self.q).claim(**kwargs)
        self.tokens.update({c["did"]: c["token"] for c in claims})
        return [c["did"] for c in claims]

    def test_discovery_and_restart_preserve_verification(self):
        self.q.discover(['did:a'], 10, now=1)
        self.assertEqual(self.claim(now=2), ['did:a'])
        self.q.verified('did:a', self.tokens['did:a'], 'https://pds.test', 3, interval=100)
        self.q.discover(['did:a','did:b'], 20, now=4)
        other = Queue(self.path)
        self.assertEqual(other.cursor(), 20)
        self.assertEqual(self.claim(queue=other, now=4), ['did:b'])
        self.assertEqual(other.metrics(now=4)['verified_count'], 1)

    def test_only_read_leases_are_reclaimed(self):
        self.q.discover(['did:a','did:b'], 2, now=1)
        self.assertEqual(len(self.claim(now=2, lease_seconds=3)), 2)
        self.q.begin_write('did:a', self.tokens['did:a'], now=2)
        self.assertEqual(self.claim(now=6), ['did:b'])
        self.q.failed('did:a', self.tokens['did:a'], 'lost response', now=6)
        self.assertEqual(self.claim(now=100000), ['did:b'])
        self.assertEqual(self.q.metrics(now=100000)['states']['uncertain'], 1)

    def test_failure_keeps_prior_verification(self):
        self.q.discover(['did:a'], 1, now=1)
        self.claim(now=2)
        self.q.verified('did:a', self.tokens['did:a'],'https://old.test',3,interval=10)
        self.claim(now=13)
        self.q.failed('did:a', self.tokens['did:a'],'authority unavailable',now=14)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT verified_pds,verified_at FROM identities').fetchone(),('https://old.test',3))
        self.assertEqual(self.claim(now=15), [])

    def test_reclaimed_lease_rejects_stale_owner(self):
        self.q.discover(['did:a'], 1, now=1)
        old = self.q.claim(now=2, lease_seconds=2)[0]
        fresh = self.q.claim(now=5)[0]
        with self.assertRaises(ValueError):
            self.q.begin_write('did:a', old['token'], now=6)
        with self.assertRaises(ValueError):
            self.q.verified('did:a', old['token'], 'https://stale.test', 6)
        self.q.begin_write('did:a', fresh['token'], now=6)


if __name__ == '__main__':
    unittest.main()
