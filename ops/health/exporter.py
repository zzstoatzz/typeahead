"""Read-only Typeahead health probes. Failed reads omit values, never reuse them."""
import concurrent.futures
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCES = {
    'freshness': 'https://typeahead.waow.tech/health/freshness',
    'search': 'https://typeahead-search.fly.dev/health',
    'index': 'https://typeahead-search.fly.dev/index/status',
    'ingester': 'https://typeahead-ingester.fly.dev/health',
}
FIELDS = {
    'freshness': {'ok': 'freshness_ok', 'probe_ok': 'search_probe_ok', 'replica_lag_s': 'overlay_lag_seconds', 'snapshot_age_s': 'snapshot_age_seconds'},
    'search': {'ready': 'search_ready', 'rss_kb': 'search_rss_kilobytes'},
    'index': {'attached': 'snapshot_attached', 'actor_count': 'snapshot_actors', 'prefix_count': 'snapshot_prefixes', 'overlay_actor_rows': 'overlay_actors'},
    'ingester': {'rss_kb': 'ingester_rss_kilobytes'},
}
def probe(source, url):
    start = time.monotonic()
    lines = []
    try:
        req = urllib.request.Request(url, headers={'X-Client': 'typeahead-health', 'User-Agent': 'typeahead-health/1'})
        try:
            response = urllib.request.urlopen(req, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError('object required')
        lines.append(f'typeahead_endpoint_up{{source="{source}"}} {int(status == 200)}')
        lines.append(f'typeahead_endpoint_status{{source="{source}"}} {status}')
        for key, metric in FIELDS[source].items():
            value = data.get(key)
            if isinstance(value, (int, float)):
                lines.append(f'typeahead_{metric} {float(value)}')
        if source == 'ingester':
            for key in ('accepted', 'processed', 'dropped', 'queued'):
                value = data.get('identity', {}).get(key)
                if isinstance(value, (int, float)):
                    suffix = '_total' if key != 'queued' else ''
                    lines.append(f'typeahead_identity_{key}{suffix} {value}')
            for key, metric in {
                'queued': 'queue_depth',
                'deletes_queued': 'delete_queue_depth',
                'backpressure_active': 'backpressure_active',
                'backpressure_count': 'backpressure_total',
                'last_write_at': 'last_write_timestamp_seconds',
                'source_event_at': 'source_event_timestamp_seconds',
            }.items():
                value = data.get('ingest', {}).get(key)
                if isinstance(value, (int, float)):
                    lines.append(f'typeahead_ingest_{metric} {float(value)}')
        lines.append(f'typeahead_probe_timestamp_seconds{{source="{source}"}} {time.time()}')
    except (OSError, ValueError, TypeError):
        lines = [f'typeahead_endpoint_up{{source="{source}"}} 0']
    lines.append(f'typeahead_endpoint_duration_seconds{{source="{source}"}} {time.monotonic()-start}')
    return '\n'.join(lines)

def collect():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return '\n'.join(pool.map(lambda item: probe(*item), SOURCES.items())) + '\n'

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/metrics', '/healthz'):
            self.send_error(404)
            return
        body = (collect() if self.path == '/metrics' else 'ok\n').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 9814), Handler).serve_forever()
