import runpy
import unittest
from pathlib import Path

eligible = runpy.run_path(str(Path(__file__).with_name('reconcile-inactive-recovery.py')))['eligible']


class InactiveEligibility(unittest.TestCase):
    def test_only_confirmed_deletion_or_deactivation(self):
        row = {'profile_source': '', 'override_action': None}
        for status in ('deleted', 'deactivated'):
            self.assertTrue(eligible(row, {'active': False, 'status': status}))
            self.assertFalse(eligible(row, {'active': True, 'status': status}))
            self.assertFalse(eligible(row, {'status': status}))
        for status in ('takendown', 'unknown', None):
            self.assertFalse(eligible(row, {'active': False, 'status': status}))

    def test_explicit_policy_and_alternate_sources_are_preserved(self):
        status = {'active': False, 'status': 'deleted'}
        for action in ('show', 'hide'):
            self.assertFalse(eligible({'profile_source': '', 'override_action': action}, status))
        self.assertFalse(eligible({'profile_source': 'blacksky', 'override_action': None}, status))


if __name__ == '__main__':
    unittest.main()
