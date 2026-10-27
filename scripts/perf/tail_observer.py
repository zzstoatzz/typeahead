"""Overlapping, bounded real Worker tails; retain only lifecycle metadata."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import tomllib
import urllib.request

CLI = Path('/tmp/typeahead-observer-wrangler-4.153.0/node_modules/wrangler/wrangler-dist/cli.js')
CLI_SHA256 = '18dbf242b542e171c993d68cc41a4e58bc4f533392fc91b71e29ffeee7bf6c0c'
PREFIX = 'TYPEAHEAD_TAIL '
WORKERS = {'public': 'typeahead', 'transport': 'typeahead-search-transport'}


def instrument():
    raw = CLI.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CLI_SHA256:
        raise RuntimeError('observer CLI provenance changed')
    source = raw.decode()
    start = source.index('async function createTail(')
    end = source.index('var import_https_proxy_agent2', start)
    part = source[start:end]
    marker = '  const deleteUrl = makeDeleteTailUrl(accountId, workerName, tailId);'
    assert part.count(marker) == 1
    emit = lambda obj: 'process.stderr.write(' + json.dumps(PREFIX) + ' + JSON.stringify({' + obj + ', at: Date.now()}) + "\\n");'
    part = part.replace(marker, emit('event: "created", id: tailId') + '\n' + marker)
    marker = 'await fetchResult2(complianceConfig, deleteUrl, { method: "DELETE" });'
    assert part.count(marker) == 1
    part = part.replace(marker, marker + '\n' + emit('event: "deleted", id: tailId'))
    source = source[:start] + part + source[end:]
    start = source.index('"src/tail/index.ts"')
    end = source.index('// src/', start + 100)
    part = source[start:end]
    for marker, event in [
        ('hasOpened = true;', 'event: "connected"'),
        ('tail.on("close", (code) => {', 'event: "disconnected", code'),
        ('async function scheduleReconnect() {', 'event: "disconnected", cause: "reconnect"'),
    ]:
        assert part.count(marker) == 1
        part = part.replace(marker, marker + '\n' + emit(event))
    source = source[:start] + part + source[end:]
    target = CLI.with_name('cli.typeahead-observer.js')
    target.write_text(source)
    return target


class TailObserver:
    def __init__(self):
        self.cli = instrument()
        self.selector = selectors.DefaultSelector()
        self.children = []
        self.events = []
        self.seen = {}
        self.armed = False
        self.started = time.monotonic()
        self.decoder = json.JSONDecoder()
        self.created = {role: set() for role in WORKERS}
        self.deleted = {role: set() for role in WORKERS}

    def add(self, role, slot):
        if role not in WORKERS or slot not in (0, 1) or len(self.children) >= 40:
            raise RuntimeError('observer process budget')
        process = subprocess.Popen(['node', str(self.cli), 'tail', WORKERS[role], '--format', 'json', '--method', 'GET'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        child = {'role': role, 'slot': slot, 'process': process, 'connected': False,
                 'stdout': '', 'stderr': '', 'started': time.monotonic(), 'retired': False}
        self.children.append(child)
        for kind in ('stdout', 'stderr'):
            self.selector.register(getattr(process, kind), selectors.EVENT_READ, (child, kind))
        return child

    def connected(self, role):
        return [c for c in self.children if c['role'] == role and c['connected'] and c['process'].poll() is None]

    def control(self, child, message):
        event = message['event']
        if event in ('created', 'deleted'):
            identity = message['id']
            if not isinstance(identity, str) or not identity.isalnum() or len(identity) > 128:
                raise RuntimeError('invalid owned tail identity')
            (self.created if event == 'created' else self.deleted)[child['role']].add(identity)
        if event in ('connected', 'disconnected'):
            child['connected'] = event == 'connected'
        self.events.append({'role': child['role'], 'slot': child['slot'], **message})
        if len(self.events) > 1000:
            raise RuntimeError('observer lifecycle evidence budget')

    def pump(self, timeout=.5):
        entries = []
        for key, _ in self.selector.select(timeout):
            child, kind = key.data
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self.selector.unregister(key.fileobj)
                continue
            child[kind] += chunk.decode(errors='replace')
            if len(child[kind]) > 1024 * 1024:
                raise RuntimeError('observer stream buffer budget')
            if kind == 'stderr':
                while '\n' in child[kind]:
                    line, child[kind] = child[kind].split('\n', 1)
                    if line.startswith(PREFIX):
                        self.control(child, json.loads(line[len(PREFIX):]))
                continue
            while child[kind]:
                first = child[kind].find('{')
                if first < 0:
                    child[kind] = ''
                    break
                child[kind] = child[kind][first:]
                try:
                    entry, end = self.decoder.raw_decode(child[kind])
                except json.JSONDecodeError:
                    break
                child[kind] = child[kind][end:]
                digest = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(',', ':')).encode()).digest()
                identity = (child['role'], digest)
                if identity not in self.seen:
                    self.seen[identity] = time.monotonic()
                    entries.append((child['role'], entry))
        now = time.monotonic()
        self.seen = {k: at for k, at in self.seen.items() if now - at < 300}
        if len(self.seen) > 20000:
            raise RuntimeError('observer deduplication budget')
        ended = []
        for child in list(self.children):
            if child['retired']:
                continue
            if child['process'].poll() is not None:
                child['connected'] = False
                child['retired'] = True
                ended.append(child)
        if self.armed and any(not self.connected(role) for role in WORKERS):
            raise RuntimeError('all observers lost for a Worker')
        if self.armed:
            for child in ended:
                self.add(child['role'], child['slot'])
        return entries

    def wait_ready(self, count, timeout=45):
        deadline = time.monotonic() + timeout
        while not all(len(self.connected(role)) >= count for role in WORKERS):
            if time.monotonic() > deadline:
                raise RuntimeError('observer connection deadline')
            self.pump()

    def replace(self, child):
        child['connected'] = False
        self.stop(child)
        child['retired'] = True
        return self.add(child['role'], child['slot'])

    @staticmethod
    def stop(child):
        process = child['process']
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def close(self):
        self.armed = False
        for child in self.children:
            self.stop(child)
        for _ in range(10):
            if not self.selector.get_map():
                break
            self.pump(.1)
        self.selector.close()
        remaining = [(role, identity) for role in WORKERS for identity in self.created[role] - self.deleted[role]]
        if remaining:
            token = tomllib.loads((Path.home() / 'Library/Preferences/.wrangler/config/default.toml').read_text())['oauth_token']
            def api(path, method='GET'):
                request = urllib.request.Request('https://api.cloudflare.com/client/v4/' + path,
                    method=method, headers={'Authorization': 'Bearer ' + token})
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.load(response)
            account = api('accounts')['result'][0]['id']
            for role, identity in remaining:
                path = 'accounts/' + account + '/workers/scripts/' + WORKERS[role] + '/tails'
                if any(t['id'] == identity for t in api(path)['result']):
                    api(path + '/' + identity, 'DELETE')
                if any(t['id'] == identity for t in api(path)['result']):
                    raise RuntimeError('owned tail retirement unconfirmed')
                self.deleted[role].add(identity)
        return {'cli_sha256': CLI_SHA256, 'instrumented_cli_sha256': hashlib.sha256(self.cli.read_bytes()).hexdigest(),
                'children': len(self.children), 'events': self.events,
                'all_owned_sessions_retired': all(self.created[r] <= self.deleted[r] for r in WORKERS)}
