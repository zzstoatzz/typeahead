"""Bounded private Fly qualification; only newly owned resources may be mutated."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time
import urllib.error
import urllib.request

APP = 'typeahead-startup-20261024'
PROTECTED = '080d415b734708'
BUILD = 'b1792908655-14d5-px05'
EXPECTED_ROUTE = {'origin': 'https://typeahead-search-refresh.fly.dev', 'build_id': 'b1793081565-0228-px05'}
GUEST = {'cpu_kind': 'shared', 'cpus': 2, 'memory_mb': 1024}


class Manager:
    def __init__(self, args, api_origin="https://api.machines.dev"):
        if not re.fullmatch(r'20261027-[a-f0-9]{12}', args.run): raise ValueError('invalid run ID')
        if not re.fullmatch(r'registry.fly.io/typeahead-startup-20261024@sha256:[a-f0-9]{64}', args.image):
            raise ValueError('qualification image must be immutable and app scoped')
        if api_origin != 'https://api.machines.dev' and not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', api_origin):
            raise ValueError('invalid API origin')
        self.api_origin = api_origin
        self.test = api_origin != 'https://api.machines.dev'
        self.args = args
        self.root = Path(args.output)
        if not self.test and (self.root.parent != Path('/var/lib/typeahead-perf-fly') or self.root.name != args.run):
            raise ValueError('invalid manager output root')
        self.root.mkdir(exist_ok=True)
        self.started = time.monotonic()
        self.machine = None
        self.volume = None
        self.protected = None
        self.report = {'run_id': args.run, 'image': args.image, 'goal_qualified': False, 'events': []}
        if getattr(args, 'cleanup_only', False) and (self.root / 'manager.json').exists():
            self.report = json.loads((self.root / 'manager.json').read_text())
            self.protected = self.report.get('protected')

    def save(self, phase, **values):
        self.report.update(phase=phase, **values)
        temporary = self.root / 'manager.json.tmp'
        temporary.write_text(json.dumps(self.report, indent=2) + '\n')
        temporary.replace(self.root / 'manager.json')
        print(json.dumps({'phase': phase, **values}), flush=True)

    def api(self, method, path, body=None):
        token = 'isolated-fixture' if self.test else (Path(os.environ['CREDENTIALS_DIRECTORY']) / 'fly-token').read_text().strip()
        request = urllib.request.Request(self.api_origin + '/v1/apps/' + APP + '/' + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': 'typeahead-perf/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(2 * 1024 * 1024)
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError(f'Fly {method} {path}: HTTP {code}') from None

    @staticmethod
    def identity(machine):
        config = machine['config']
        return {k: machine.get(k) for k in ['id', 'instance_id', 'version', 'state']} | {
            'image': config['image'], 'guest': config['guest'], 'mounts': config.get('mounts'),
            'services': config.get('services'), 'metadata': config.get('metadata')}

    def guard(self, deadline=True):
        route_url = self.api_origin + '/route' if self.test else 'https://typeahead-search-transport.n8-3e9.workers.dev/route'
        with urllib.request.urlopen(urllib.request.Request(route_url, headers={'User-Agent': 'typeahead-perf/1.0'}), timeout=10) as response:
            route = json.load(response)
        if route != EXPECTED_ROUTE: raise RuntimeError('production route changed; stop private qualification')
        protected = self.identity(self.api('GET', 'machines/' + PROTECTED))
        if self.protected is not None and protected != self.protected:
            raise RuntimeError('protected rollback Machine changed')
        self.protected = protected
        if deadline and time.monotonic() - self.started > 5100: raise TimeoutError('private trial deadline')

    def owned(self):
        if not self.machine or self.machine['id'] == PROTECTED: raise RuntimeError('no owned private Machine')
        actual = self.api('GET', 'machines/' + self.machine['id'])
        config = actual['config']
        if (config.get('metadata', {}).get('perf_run') != self.args.run
                or config.get('metadata', {}).get('owner') != 'typeahead-perf-goal' or config.get('services')
                or config['image'] != self.args.image or {k: config['guest'].get(k) for k in GUEST} != GUEST
                or [{k: m[k] for k in ['volume', 'path']} for m in config.get('mounts', [])] != [{'volume': self.volume['id'], 'path': '/data'}]):
            raise RuntimeError('private Machine ownership/configuration mismatch')
        return actual

    def execute(self, operation):
        self.owned()
        result = self.api('POST', 'machines/' + self.machine['id'] + '/exec',
                          {'cmd': 'python3 /app/perf/fly_control.py ' + operation, 'timeout': 10})
        if result.get('exit_code', 0) != 0: raise RuntimeError('private evidence command failed')
        return result.get('stdout', '')

    def create(self):
        self.guard()
        machines = self.api('GET', 'machines')
        if {m['id'] for m in machines} != {PROTECTED}: raise RuntimeError('inactive app has unexpected Machines')
        self.save('creating-private-volume', protected=self.protected, creation_pending='volume')
        self.volume = self.api('POST', 'volumes', {'name': 'perf_' + self.args.run.replace('-', '_'),
            'region': 'ewr', 'size_gb': 20, 'encrypted': True, 'auto_backup_enabled': False, 'compute': GUEST})
        self.report['creation_pending'] = None
        if (self.volume['id'] in {m['volume'] for m in self.protected.get('mounts', [])}
                or self.volume['name'] != 'perf_' + self.args.run.replace('-', '_') or self.volume.get('attached_machine_id')):
            raise RuntimeError('new volume is not independently owned')
        self.guard()
        self.save('creating-private-machine', volume_id=self.volume['id'], creation_pending='machine')
        self.machine = self.api('POST', 'machines', {'name': 'perf-' + self.args.run, 'region': 'ewr',
            'skip_service_registration': True, 'config': {
                'image': self.args.image, 'guest': GUEST, 'services': [],
                'mounts': [{'volume': self.volume['id'], 'path': '/data'}],
                'restart': {'policy': 'no'}, 'stop_config': {'signal': 'SIGTERM', 'timeout': 20000000000},
                'env': {'PERF_RUN_ID': self.args.run, 'REFRESH_BUILD_ID': BUILD,
                        'INDEX_LATEST_KEY': 'control/slots/' + APP + '/latest.json'},
                'metadata': {'owner': 'typeahead-perf-goal', 'perf_run': self.args.run, 'build_id': BUILD}}})
        self.report['creation_pending'] = None
        self.save('private-machine-created', machine_id=self.machine['id'])
        self.owned()

    def collect(self, status):
        if not 0 < status['bytes'] <= 16 * 1024 * 1024: raise RuntimeError('evidence budget exceeded')
        if status['run_id'] != self.args.run: raise RuntimeError('evidence identity mismatch')
        data = bytearray()
        while len(data) < status['bytes']:
            chunk = base64.b64decode(self.execute('read ' + str(len(data))).strip(), validate=True)
            if not chunk or len(chunk) > 24576: raise RuntimeError('invalid evidence chunk')
            data.extend(chunk)
        if len(data) != status['bytes'] or hashlib.sha256(data).hexdigest() != status['sha256']:
            raise RuntimeError('evidence digest mismatch')
        (self.root / 'results.tgz').write_bytes(data)
        self.execute('collected')
        self.save('results-collected', evidence_bytes=len(data), evidence_sha256=status['sha256'])

    def resolve_created(self):
        if self.volume is None:
            matches = [v for v in self.api('GET', 'volumes') if v['name'] == 'perf_' + self.args.run.replace('-', '_')]
            if len(matches) > 1: raise RuntimeError('ambiguous private volumes')
            self.volume = matches[0] if matches else None
        if self.machine is None:
            matches = [m for m in self.api('GET', 'machines')
                       if m['config'].get('metadata', {}).get('perf_run') == self.args.run]
            if len(matches) > 1: raise RuntimeError('ambiguous private Machines')
            self.machine = matches[0] if matches else None

    def retire(self):
        self.resolve_created()
        pending = self.report.get('creation_pending')
        for _ in range(12):
            if (pending == 'volume' and self.volume is None) or (pending == 'machine' and self.machine is None):
                time.sleep(5); self.resolve_created()
            else: break
        else: raise RuntimeError('creation outcome remains unknown; do not declare cleanup complete')
        self.report['creation_pending'] = None
        if self.machine:
            actual = self.owned()
            self.api('DELETE', 'machines/' + actual['id'] + '?force=true')
            for _ in range(30):
                if not any(m['id'] == actual['id'] for m in self.api('GET', 'machines')): break
                time.sleep(2)
            else: raise RuntimeError('private Machine retirement not confirmed')
        if self.volume:
            actual = self.api('GET', 'volumes/' + self.volume['id'])
            protected_volumes = {m['volume'] for m in self.protected.get('mounts', [])} if self.protected else set()
            if (actual['id'] in protected_volumes or actual['name'] != 'perf_' + self.args.run.replace('-', '_')
                    or actual.get('attached_machine_id')):
                raise RuntimeError('unsafe private volume retirement')
            self.api('DELETE', 'volumes/' + actual['id'])
            if any(v['id'] == actual['id'] for v in self.api('GET', 'volumes')):
                raise RuntimeError('private volume retirement not confirmed')
        self.report['retired'] = True
        try: self.guard(deadline=False)
        except (RuntimeError, OSError) as exc: self.report['post_cleanup_guard_error'] = str(exc)
        self.save('retired-private-resources')

    def run(self):
        try:
            self.create()
            while True:
                self.guard()
                machine = self.owned()
                if machine['state'] == 'started':
                    try: status = json.loads(self.execute('status'))
                    except (RuntimeError, ValueError) as exc:
                        self.save('waiting-for-private-status', observation_error=str(exc))
                    else:
                        self.save('private-trial-running', status=status)
                        if status['phase'] == 'results-ready':
                            self.collect(status)
                            break
                elif machine['state'] in ('stopped', 'destroyed'):
                    raise RuntimeError('private Machine stopped before evidence collection')
                else: self.save('waiting-for-private-machine', machine_state=machine['state'])
                time.sleep(25)
        except BaseException as exc:
            self.save('failed', error=type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            self.retire()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cleanup-only', action='store_true')
    args = parser.parse_args()
    def interrupted(*_): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    manager = Manager(args)
    if args.cleanup_only: manager.retire()
    else: manager.run()


if __name__ == '__main__': main()
