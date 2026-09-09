#!/usr/bin/env python3
"""Time-limited private shadow pilot using Wrangler's existing live event stream.

No Worker deployment, user-response changes, raw-log files, or recurring job.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


SEARCH_PATHS = {'/xrpc/tech.waow.typeahead.searchActors',
                '/xrpc/app.bsky.actor.searchActorsTypeahead'}


def extract_query(event):
    if not isinstance(event, dict) or event.get('outcome') != 'ok':
        return None
    payload = event.get('event')
    if not isinstance(payload, dict):
        return None
    request = payload.get('request')
    if not isinstance(request, dict) or request.get('method') != 'GET':
        return None
    url = request.get('url')
    if not isinstance(url, str) or len(url) > 8192:
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.path not in SEARCH_PATHS:
            return None
        params = urllib.parse.parse_qs(parsed.query, max_num_fields=30)
        query = (params.get('q') or params.get('term') or [''])[0]
        # Match src/utils.ts sanitize; no actor or caller metadata leaves memory.
        query = ''.join(c for c in query if c not in '\\%_"' and unicodedata.category(c) != 'Cc').strip()
        if not query or len(query.encode('utf-8')) > 256:
            return None
        return query
    except (ValueError, UnicodeError):
        return None


class Events:
    def __init__(self):
        self.buffer = ''

    def feed(self, chunk):
        self.buffer += chunk
        if len(self.buffer) > 2_000_000:
            raise ValueError('Tail record exceeded memory limit')
        values = []
        while self.buffer:
            self.buffer = self.buffer.lstrip()
            if not self.buffer.startswith('{'):
                newline = self.buffer.find('\n')
                if newline < 0:
                    break
                self.buffer = self.buffer[newline + 1:]
                continue
            try:
                value, end = json.JSONDecoder().raw_decode(self.buffer)
            except json.JSONDecodeError:
                break
            values.append(value)
            self.buffer = self.buffer[end:]
        return values


class Client:
    def __init__(self):
        result = subprocess.run(['sops', '-d', '--output-type', 'json',
                                 str(Path.home() / 'tangled.org/zzstoatzz.io/secrets/prod.yaml')],
                                capture_output=True, check=True)
        self.token = json.loads(result.stdout)['local']['typeahead_canary_token']

    def request(self, path, body=None):
        request = urllib.request.Request('http://127.0.0.1:8898' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)


def sample(client, seconds, count, output):
    code, initial = client.request('/status')
    if code != 200 or initial['failed'] or initial['expired'] or initial['busy']:
        raise RuntimeError('Canary is not ready for a pilot')
    if client.request('/control', {'enabled': True})[0] != 200:
        raise RuntimeError('Canary refused activation')
    started = time.time()
    process = None
    accepted = seen = 0
    next_submit = 0
    report = {'started_at': started, 'initial': initial,
              'source': 'wrangler-tail-10-percent', 'duration_limit_s': seconds,
              'sample_limit': count, 'includes_cache_hits': True}
    logs = tempfile.TemporaryDirectory(prefix='typeahead-canary-logs-')
    log_path = Path(logs.name) / 'discard.log'
    log_path.symlink_to('/dev/null')
    try:
        env = dict(os.environ, WRANGLER_LOG_PATH=str(log_path), WRANGLER_SEND_METRICS='false')
        process = subprocess.Popen(['bunx', 'wrangler', 'tail', 'typeahead', '--format', 'json',
                                    '--method', 'GET', '--status', 'ok', '--sampling-rate', '0.1'],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True, env=env)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        events = Events()
        import codecs
        decoder = codecs.getincrementaldecoder('utf-8')()
        event_count = 0
        next_progress = time.time() + 30
        while time.time() - started < seconds and accepted < count:
            if time.time() >= next_progress:
                print(json.dumps({'tail_events': event_count, 'eligible_searches': seen,
                                  'accepted': accepted}), flush=True)
                next_progress = time.time() + 30
            if process.poll() is not None:
                raise RuntimeError('Wrangler tail ended before the pilot completed')
            for key, _ in selector.select(timeout=1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    raise RuntimeError('Wrangler tail stream closed')
                for event in events.feed(decoder.decode(chunk)):
                    event_count += 1
                    query = extract_query(event)
                    if query is None:
                        continue
                    seen += 1
                    if time.time() < next_submit or accepted >= count:
                        continue
                    code, status = client.request('/status')
                    if code != 200 or status['paused'] or status['failed'] or status['expired']:
                        raise RuntimeError('Canary stopped or failed; ending pilot')
                    if status['busy']:
                        continue
                    code, _ = client.request('/compare', {'q': query})
                    next_submit = time.time() + 16
                    if code == 202:
                        accepted += 1
                        print(json.dumps({'accepted_live_samples': accepted,
                                          'previous_passed': status['passed'],
                                          'failed': status['failed']}), flush=True)
                    elif code != 429:
                        raise RuntimeError('Canary rejected pilot sample')
        report['completed'] = True
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        logs.cleanup()
        client.request('/control', {'enabled': False})
        for _ in range(20):
            code, final = client.request('/status')
            if code != 200:
                raise RuntimeError('Cannot verify canary shutdown')
            if not final['busy']:
                break
            time.sleep(1)
        report.update(ended_at=time.time(), accepted_live_samples=accepted,
                      seen_sampled_searches=seen, final=final)
        output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'pilot_report': str(output), 'accepted': accepted,
                          'passed': final['passed'], 'failed': final['failed'],
                          'paused': final['paused'], 'busy': final['busy']}), flush=True)
    if accepted == 0 or final['failed'] or final['busy'] or final['passed'] - initial['passed'] != accepted:
        raise RuntimeError('Pilot did not pass all admitted samples')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'pause', 'resume', 'sample'])
    parser.add_argument('--seconds', type=int, default=600)
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--output', type=Path, default=Path('.codex/evals/live-shadow-pilot.json'))
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.count <= 100:
        parser.error('Use 1–3600 seconds and 1–100 samples')
    client = Client()
    if args.action == 'sample':
        sample(client, args.seconds, args.count, args.output)
    elif args.action == 'status':
        print(json.dumps(client.request('/status')[1]))
    else:
        code, result = client.request('/control', {'enabled': args.action == 'resume'})
        print(json.dumps({'status': code, **result}))
        if code != 200:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
