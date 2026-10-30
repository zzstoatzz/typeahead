import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from pds_cloud_pilot import apply_prepared, prepare
from pds_repair_ledger import Ledger


class CloudPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE actors(did TEXT PRIMARY KEY,pds TEXT,updated_at INTEGER,handle TEXT,display_name TEXT)')
        self.db.execute("INSERT INTO actors VALUES('did:plc:test','',1,'test.example','Keep me')")
        self.ledger = Ledger(Path(self.temp.name) / 'ledger.db')
        self.document = {'id': 'did:plc:test', 'service': [{'id': '#atproto_pds', 'type': 'AtprotoPersonalDataServer', 'serviceEndpoint': 'https://pds.example'}]}

    def execute(self, sql, params):
        rows = [list(r) for r in self.db.execute(sql, params).fetchall()]
        self.db.commit()
        return rows

    def prepared(self):
        return prepare(self.execute, 'did:plc:test', 'test.example', self.document)

    def test_repair_preserves_other_fields(self):
        receipt = apply_prepared(self.ledger, self.execute, self.prepared())
        self.assertEqual(receipt['state'], 'confirmed')
        self.assertEqual(self.db.execute('SELECT pds,handle,display_name FROM actors').fetchone(), ('https://pds.example', 'test.example', 'Keep me'))

    def test_concurrent_migration_is_not_overwritten(self):
        item = self.prepared()
        self.db.execute("UPDATE actors SET pds='https://new.example',updated_at=2")
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            apply_prepared(self.ledger, self.execute, item)
        self.assertEqual(self.db.execute('SELECT pds FROM actors').fetchone()[0], 'https://new.example')

    def test_concurrent_profile_change_is_not_overwritten(self):
        item = self.prepared()
        self.db.execute("UPDATE actors SET display_name='New name',updated_at=2")
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            apply_prepared(self.ledger, self.execute, item)
        self.assertEqual(self.db.execute('SELECT pds,display_name FROM actors').fetchone(), ('', 'New name'))

    def test_lost_receipt_does_not_retry_write(self):
        item = self.prepared()
        writes = []
        def lost(sql, params):
            rows = self.execute(sql, params)
            if sql.startswith('UPDATE'):
                writes.append(sql)
                raise TimeoutError('Committed but receipt lost')
            return rows
        with self.assertRaises(TimeoutError):
            apply_prepared(self.ledger, lost, item)
        self.assertEqual(len(writes), 1)
        with sqlite3.connect(self.ledger.path) as db:
            attempt, state = db.execute('SELECT id,state FROM attempts').fetchone()
        self.assertEqual(state, 'present_unattributed')
        with self.assertRaises(ValueError):
            self.ledger.retry(attempt, 2, 'https://pds.example')

    def test_stale_or_wrong_authority_is_rejected(self):
        item = self.prepared()
        item['authority_checked_at'] = time.time() - 61
        with self.assertRaisesRegex(ValueError, 'expired'):
            apply_prepared(self.ledger, self.execute, item)
        self.document['id'] = 'did:plc:other'
        with self.assertRaisesRegex(ValueError, 'subject mismatch'):
            self.prepared()


if __name__ == '__main__':
    unittest.main()
