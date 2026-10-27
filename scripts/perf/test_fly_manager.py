import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from fly_manager import Manager, APP, PROTECTED, GUEST, EXPECTED_ROUTE


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='typeahead-manager-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.run_id = '20261027-123456789abc'
        self.image = 'registry.fly.io/typeahead-startup-20261024@sha256:' + 'a' * 64
        self.machines = {PROTECTED: {'id': PROTECTED, 'state': 'started', 'instance_id': 'protected-instance',
            'config': {'image': 'protected-image', 'guest': GUEST, 'mounts': [{'volume': 'vol_old', 'path': '/data'}],
                       'services': [{'protocol': 'tcp'}], 'metadata': {'owner': 'snapshot-controller'}}}}
        self.volumes = {'vol_old': {'id': 'vol_old', 'name': 'protected', 'attached_machine_id': PROTECTED}}
        self.requests = []
        self.lose_machine_response = False
        self.stale_deleted_volume = None
        self.delay_volume_deletion = False
        self.soft_delete_volume = False
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def handle_request(self):
                fixture.requests.append((self.command, self.path))
                if self.path == '/route': return self.reply(EXPECTED_ROUTE)
                if self.headers.get('Authorization') != 'Bearer isolated-fixture': return self.reply({}, 403)
                path = self.path.split('?', 1)[0].removeprefix('/v1/apps/' + APP + '/')
                if self.command == 'GET':
                    if path == 'machines': return self.reply(list(fixture.machines.values()))
                    if path == 'volumes':
                        values = list(fixture.volumes.values())
                        if fixture.stale_deleted_volume:
                            values.append(fixture.stale_deleted_volume); fixture.stale_deleted_volume = None
                        return self.reply(values)
                    kind, key = path.split('/')
                    return self.reply((fixture.machines if kind == 'machines' else fixture.volumes)[key])
                if self.command == 'POST':
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                    if path == 'volumes':
                        volume = {'id': 'vol_trial', 'name': body['name'], 'attached_machine_id': None}
                        fixture.volumes[volume['id']] = volume
                        return self.reply(volume)
                    if path == 'machines':
                        machine = {'id': 'trial-machine', 'state': 'started', 'config': body['config']}
                        fixture.machines[machine['id']] = machine
                        if fixture.lose_machine_response: return self.reply({}, 503)
                        return self.reply(machine)
                if self.command == 'DELETE':
                    kind, key = path.split('/')
                    if kind == 'volumes' and fixture.delay_volume_deletion:
                        fixture.stale_deleted_volume = fixture.volumes[key]
                    if kind == 'volumes' and fixture.soft_delete_volume:
                        fixture.volumes[key]['state'] = 'pending_destroy'
                    else: del (fixture.machines if kind == 'machines' else fixture.volumes)[key]
                    return self.reply({})
                self.reply({}, 404)
            def reply(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            do_GET = handle_request
            do_POST = handle_request
            do_DELETE = handle_request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)
        self.manager = Manager(argparse.Namespace(run=self.run_id, image=self.image, output=str(Path(self.temp.name) / 'evidence')),
                               api_origin='http://127.0.0.1:' + str(self.server.server_port))

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def test_creation_and_retirement_preserve_protected_machine_and_volume(self):
        before = copy.deepcopy(self.machines[PROTECTED])
        self.manager.create()
        self.assertFalse(self.machines['trial-machine']['config']['services'])
        self.manager.retire()
        self.assertEqual(self.machines, {PROTECTED: before})
        self.assertEqual(set(self.volumes), {'vol_old'})
        self.assertTrue(self.manager.report['retired'])

    def test_retirement_waits_for_eventually_consistent_volume_listing(self):
        self.delay_volume_deletion = True
        self.manager.create()
        self.manager.retire()
        self.assertTrue(self.manager.report['retired'])
        self.assertEqual(set(self.volumes), {'vol_old'})

    def test_soft_deleted_volume_is_inactive_and_cleanup_is_idempotent(self):
        self.soft_delete_volume = True
        self.manager.create()
        self.manager.retire()
        self.assertEqual(self.manager.report['volume_retirement_state'], 'pending_destroy')
        deletions = sum(method == 'DELETE' and '/volumes/' in path for method,path in self.requests)
        self.manager.machine = None
        self.manager.retire()
        self.assertEqual(sum(method == 'DELETE' and '/volumes/' in path for method,path in self.requests), deletions)
        self.assertEqual(self.volumes['vol_trial']['state'], 'pending_destroy')

    def test_cleanup_resolves_created_machine_when_post_response_was_lost(self):
        self.lose_machine_response = True
        with self.assertRaises(RuntimeError): self.manager.create()
        self.assertIsNone(self.manager.machine)
        self.assertIn('trial-machine', self.machines)
        self.manager.retire()
        self.assertEqual(set(self.machines), {PROTECTED})
        self.assertEqual(set(self.volumes), {'vol_old'})

    def test_public_services_or_changed_owner_prevent_machine_deletion(self):
        self.manager.create()
        self.machines['trial-machine']['config']['services'] = [{'protocol': 'tcp'}]
        with self.assertRaises(RuntimeError): self.manager.retire()
        self.assertFalse(any(method == 'DELETE' for method, _ in self.requests))
        self.machines['trial-machine']['config']['services'] = []
        self.machines['trial-machine']['config']['metadata']['owner'] = 'someone-else'
        with self.assertRaises(RuntimeError): self.manager.retire()
        self.assertFalse(any(method == 'DELETE' for method, _ in self.requests))

    def test_protected_id_cannot_be_used_for_exec_or_cleanup(self):
        self.manager.create()
        self.manager.machine = self.machines[PROTECTED]
        with self.assertRaises(RuntimeError): self.manager.execute('status')
        with self.assertRaises(RuntimeError): self.manager.retire()
        self.assertFalse(any(method == 'DELETE' for method, _ in self.requests))


if __name__ == '__main__': unittest.main()
