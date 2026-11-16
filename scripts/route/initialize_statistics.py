#!/usr/bin/env -S uv run --no-project
"""Initialize index counters with the standby stopped; restore its supervisor and verify the effective config."""
import argparse
import copy
import json
import os
import subprocess
import tempfile

import slot_route as route


def guard(origin, get=route.http_get):
    serving = route.serving_route(get)
    if serving['origin'] == origin:
        raise route.Refused('statistics initialization cannot target the serving slot')
    reasons = route.serving_slot_reasons(route.observe(serving['origin'], get), serving)
    if reasons:
        raise route.Refused(reasons)
    return serving


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def initialize(app, machine, write=False):
    origin = 'https://' + app + '.fly.dev'
    if origin not in route.STORE_ORIGINS:
        raise route.Refused('not a store slot')
    guard(origin)
    config = json.loads(run(['fly', 'machine', 'status', machine, '-a', app, '--display-config']))
    command = config.get('init', {}).get('cmd')
    if command not in (None, [], ['/app/supervise']):
        raise route.Refused('machine has an unexpected command override')
    # Fly merges init objects: an empty object cannot clear the sleep command.
    config.setdefault('init', {})['cmd'] = ['/app/supervise']
    if not write:
        return {'operation': 'initialize-statistics', 'app': app, 'machine': machine, 'written': False}
    idle = copy.deepcopy(config)
    idle.setdefault('init', {})['cmd'] = ['/bin/sleep', 'infinity']
    with tempfile.TemporaryDirectory(prefix='typeahead-statistics-') as temp:
        paths = []
        for name, body in (('original', config), ('idle', idle)):
            path = os.path.join(temp, name + '.json')
            with open(path, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as output:
                json.dump(body, output)
            paths.append(path)
        guard(origin)
        try:
            run(['fly', 'machine', 'update', machine, '-a', app, '--machine-config', paths[1], '--skip-health-checks', '--yes'])
            guard(origin)
            result = run(['fly', 'ssh', 'console', '-a', app, '--machine', machine, '-C',
                          '/app/typeahead-tantivy initialize-statistics-offline --data /data/tantivy --acknowledge-offline true'])
            if json.loads(result).get('statistics_initialized') is not True:
                raise route.Refused('initializer did not acknowledge success')
        finally:
            run(['fly', 'machine', 'update', machine, '-a', app, '--machine-config', paths[0], '--skip-health-checks', '--yes'])
            restored = json.loads(run(['fly', 'machine', 'status', machine, '-a', app, '--display-config']))
            if restored != config:
                raise route.Refused('standby supervisor/config did not restore; stop and inspect it')
    return {'operation': 'initialize-statistics', 'app': app, 'machine': machine, 'written': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', choices=['typeahead-slot-a', 'typeahead-slot-b'], required=True)
    parser.add_argument('--machine', required=True)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    print(json.dumps(initialize(args.app, args.machine, args.write), indent=2))
