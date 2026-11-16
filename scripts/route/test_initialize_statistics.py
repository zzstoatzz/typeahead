import copy
import json
import unittest
from pathlib import Path
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

    def test_empty_init_is_restored_explicitly_despite_fly_merge_semantics(self):
        current = {'init': {}, 'image': 'fixture', 'env': {'A': '1'}}
        updates = []
        def run(args):
            if 'status' in args:
                return json.dumps(current)
            if 'update' in args:
                path = args[args.index('--machine-config') + 1]
                wanted = json.loads(Path(path).read_text())
                updates.append(copy.deepcopy(wanted))
                current['init'].update(wanted['init'])
                return ''
            return '{"statistics_initialized":true}'
        with patch.object(initialize, 'guard'), patch.object(initialize, 'run', side_effect=run):
            result = initialize.initialize('typeahead-slot-a', 'fixture', True)
        self.assertTrue(result['written'])
        self.assertEqual(updates[0]['init']['cmd'], ['/bin/sleep', 'infinity'])
        self.assertEqual(current['init']['cmd'], ['/app/supervise'])

    def test_failure_restores_standby_config(self):
        config = {'init': {}, 'image': 'fixture', 'env': {'A': '1'}}
        calls = []
        def run(args):
            calls.append(args)
            if 'status' in args:
                return json.dumps(config)
            if 'console' in args:
                raise RuntimeError('initializer failed')
            if 'update' in args:
                wanted = json.loads(Path(args[args.index('--machine-config') + 1]).read_text())
                config['init'].update(wanted['init'])
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
