import tempfile
import unittest
from pathlib import Path
from pds_bundle_download import Budget


class BudgetTests(unittest.TestCase):
    def test_reservations_survive_restart_and_cannot_expand(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'budget.db'
            Budget(path, 100).reserve(60)
            resumed = Budget(path, 100)
            self.assertEqual(resumed.used(), 60)
            with self.assertRaises(RuntimeError):
                resumed.reserve(41)
            self.assertEqual(Budget(path, 100).used(), 60)
            with self.assertRaises(ValueError):
                Budget(path, 101)
            resumed.reserve(40)
            self.assertEqual(Budget(path, 100).used(), 100)
