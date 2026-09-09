import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from exporter import probe

class Upstream(BaseHTTPRequestHandler):
    status = 200
    body = b'{"ok":true,"probe_ok":true,"replica_lag_s":30}'
    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)
    def log_message(self,*args):
        pass

class FailureHandling(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
    def test_degraded_health_preserves_reason_metrics(self):
        Upstream.status=503
        Upstream.body=b'{"ok":false,"probe_ok":true,"replica_lag_s":2000}'
        data=probe('freshness',self.url)
        self.assertIn('typeahead_endpoint_up{source="freshness"} 0',data)
        self.assertIn('typeahead_overlay_lag_seconds 2000.0',data)
        self.assertIn('typeahead_freshness_ok 0.0',data)
    def test_bad_response_never_reuses_previous_values(self):
        Upstream.status=200
        Upstream.body=b'{"ok":true,"replica_lag_s":30}'
        self.assertIn('typeahead_overlay_lag_seconds 30.0',probe('freshness',self.url))
        Upstream.body=b'<html>gateway error</html>'
        data=probe('freshness',self.url)
        self.assertIn('typeahead_endpoint_up{source="freshness"} 0',data)
        self.assertNotIn('typeahead_overlay_lag_seconds',data)
        self.assertNotIn('typeahead_probe_timestamp_seconds',data)
    def test_ingest_progress_is_separate_from_identity_queue(self):
        Upstream.status=200
        Upstream.body=b'{"identity":{"queued":0},"ingest":{"queued":4000,"deletes_queued":2,"backpressure_active":true,"backpressure_count":3,"last_write_at":1788630000,"source_event_at":1788620000}}'
        data=probe('ingester',self.url)
        self.assertIn('typeahead_identity_queued 0',data)
        self.assertIn('typeahead_ingest_queue_depth 4000.0',data)
        self.assertIn('typeahead_ingest_backpressure_active 1.0',data)
        self.assertIn('typeahead_ingest_source_event_timestamp_seconds 1788620000.0',data)

    def test_missing_measurement_is_not_zero(self):
        Upstream.status=200
        Upstream.body=b'{}'
        self.assertNotIn('typeahead_overlay_lag_seconds',probe('freshness',self.url))

if __name__=='__main__': unittest.main()
