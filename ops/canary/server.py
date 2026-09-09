#!/usr/bin/env python3
"""Authenticated, bounded shadow comparisons. Never returns search results."""
from concurrent.futures import ThreadPoolExecutor
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sqlite3
import socket
from pathlib import Path
import threading
import time

from compare import compare_query


class Canary:
    def __init__(self, binary, overlay, snapshot, state_file, expires_at,
                 evaluator=compare_query, clock=time.time):
        self.binary, self.overlay, self.snapshot = binary, overlay, snapshot
        self.state_file, self.expires_at = state_file, expires_at
        self.evaluate, self.clock = evaluator, clock
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.busy = False
        self.last_accepted = 0.0
        self.state = {'paused': True, 'reason': 'initial', 'accepted': 0,
                      'passed': 0, 'failed': 0, 'recent': []}
        if state_file.exists():
            saved = json.loads(state_file.read_text())
            # Fail closed on restart, retaining the total sample budget.
            if not isinstance(saved.get('accepted'), int) or saved['accepted'] < 0:
                raise ValueError('Invalid persisted canary state')
            self.state = saved
            self.state.update(paused=True, reason='restart')
        self.save()

    def save(self):
        temp = self.state_file.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state))
        temp.replace(self.state_file)

    def status(self):
        with self.lock:
            return {**self.state, 'busy': self.busy,
                    'expires_at': self.expires_at,
                    'expired': self.clock() >= self.expires_at,
                    'max_samples': 120, 'min_interval_s': 15}

    def control(self, enabled):
        with self.lock:
            if enabled and (self.clock() >= self.expires_at or self.state['accepted'] >= 120):
                return False
            self.state.update(paused=not enabled, reason='operator')
            self.save()
            return True

    def submit(self, query):
        with self.lock:
            now = self.clock()
            if self.state['paused'] or now >= self.expires_at:
                return 503
            if self.busy or now - self.last_accepted < 15 or self.state['accepted'] >= 120:
                return 429
            self.busy = True
            self.last_accepted = now
            self.state['accepted'] += 1
            self.save()
            self.pool.submit(self.run, query)
            return 202

    def run(self, query):
        try:
            result = self.evaluate(self.binary, self.overlay, self.snapshot, query)
        except Exception:
            result = {'passed': False, 'error': 'supervisor_error'}
        # No query text, actor records, caller identifiers, or credentials retained.
        with self.lock:
            self.busy = False
            result['completed_at'] = self.clock()
            self.state['recent'] = (self.state['recent'] + [result])[-20:]
            if result.get('passed') is True:
                self.state['passed'] += 1
            else:
                self.state['failed'] += 1
                self.state.update(paused=True, reason='comparison_failed')
            self.save()


class CanaryHTTPServer(HTTPServer):
    address_family = socket.AF_INET6


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(1)

    def log_message(self, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        expected = 'Bearer ' + self.server.token
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected.encode()):
            self.reply(401, {'error': 'unauthorized'})
            return False
        return True

    def do_GET(self):
        if self.path == '/health':
            self.reply(200, {'status': 'ok', 'role': 'shadow-canary'})
        elif self.authorized():
            if self.path == '/status':
                self.reply(200, self.server.canary.status())
            else:
                self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 2048 or self.headers.get('Transfer-Encoding'):
                self.reply(400, {'error': 'invalid_length'})
                return
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError('object required')
        except (ValueError, TimeoutError):
            self.reply(400, {'error': 'invalid_json'})
            return
        if self.path == '/compare':
            query = value.get('q')
            if not isinstance(query, str) or '\0' in query:
                self.reply(400, {'error': 'invalid_query'})
                return
            try:
                valid = len(query.encode('utf-8')) <= 256
            except UnicodeEncodeError:
                valid = False
            if not valid:
                self.reply(400, {'error': 'invalid_query'})
                return
            code = self.server.canary.submit(query)
            self.reply(code, {'accepted': code == 202})
        elif self.path == '/control' and type(value.get('enabled')) is bool:
            ok = self.server.canary.control(value['enabled'])
            self.reply(200 if ok else 409, {'updated': ok})
        else:
            self.reply(404, {'error': 'not_found'})


def main():
    token = os.environ['CANARY_TOKEN']
    if len(token) < 32:
        raise ValueError('A dedicated canary token is required')
    expiry = int(os.environ['CANARY_EXPIRES_AT'])
    if not time.time() < expiry <= time.time() + 24 * 3600:
        raise ValueError('Canary expiry must be within the next 24 hours')
    paths = [Path(os.environ[k]) for k in ('CANARY_BINARY', 'CANARY_OVERLAY', 'CANARY_SNAPSHOT')]
    if any(not p.is_file() for p in paths):
        raise ValueError('Missing canary fixture or executable')
    with sqlite3.connect(paths[1].as_uri() + '?mode=ro', uri=True) as overlay:
        if not overlay.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='canary_rank'").fetchone():
            raise ValueError('Ranking index is missing')
        watermark = overlay.execute("SELECT value FROM sync_meta WHERE key='last_sync'").fetchone()[0]
    with sqlite3.connect(paths[2].as_uri() + '?mode=ro', uri=True) as snapshot:
        build = snapshot.execute("SELECT value FROM index_meta WHERE key='build_id'").fetchone()[0]
    state_file = Path(os.environ['CANARY_STATE_FILE'])
    canary = Canary(*paths, state_file, expiry)
    canary.state['fixture'] = {'build_id': build, 'overlay_watermark': watermark, 'frozen': True}
    canary.save()
    server = CanaryHTTPServer(('::', 8080), Handler)
    server.canary, server.token = canary, token
    server.timeout = 1
    try:
        while time.time() < expiry:
            server.handle_request()
    finally:
        canary.control(False)
        server.server_close()
        canary.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
