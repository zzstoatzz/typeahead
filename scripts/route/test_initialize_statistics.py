import json
import unittest
from unittest.mock import patch

import initialize_statistics as initialize
import test_slot_route as fixture


class StatisticsInitialization(unittest.TestCase):
    def test_serving_target_and_unavailable_serving_slot_are_refused(self):
        world = fixture.World()
        with self.assertRaisesRegex(initialize.route.Refused, 'serving slot'):
            initialize.guard(fixture.SLOT_B, world.get)
        world.slots[fixture.SLOT_B].down = True
        with self.assertRaises(initialize.route.Refused):
            initialize.guard(fixture.SLOT_A, world.get)

    def test_failure_restores_standby_config(self):
        config = {'init': {}, 'image': 'fixture', 'env': {'A': '1'}}
        calls = []
        def run(args):
            calls.append(args)
            if 'status' in args:
                return json.dumps(config)
            if 'console' in args:
                raise RuntimeError('initializer failed')
            return ''
        with patch.object(initialize, 'guard'), patch.object(initialize, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'initializer failed'):
                initialize.initialize('typeahead-slot-a', 'fixture', True)
        updates = [call for call in calls if 'update' in call]
        self.assertEqual(len(updates), 2)
        self.assertIn('idle.json', ' '.join(updates[0]))
        self.assertIn('original.json', ' '.join(updates[1]))


if __name__ == '__main__':
    unittest.main()
