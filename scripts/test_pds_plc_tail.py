import unittest
import functools
import http.server
import tempfile
import threading
from pathlib import Path
from pds_bundle_download import Budget
from pds_plc_tail import page_result,fetch_page


def row(t):return {'createdAt':t,'did':'did:plc:a','cid':'a','nullified':False}


class TailTests(unittest.TestCase):
    def test_bounded_real_download_and_persistent_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);payload=b'x'*70000;(root/'page').write_bytes(payload)
            handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=tmp)
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}/page'
            try:
                budget=Budget(root/'budget.db',4*65536)
                self.assertEqual(fetch_page(url,budget,10**9),payload)
                self.assertEqual(budget.used(),2*65536)
                with self.assertRaises(ValueError):
                    fetch_page(url,budget,10**9,max_bytes=69999)
                self.assertEqual(budget.used(),2*65536+70000)
                reopened=Budget(root/'budget.db',4*65536)
                with self.assertRaises(RuntimeError):fetch_page(url,reopened,10**9)
                self.assertEqual(reopened.used(),budget.used())
                with self.assertRaises(ValueError):Budget(root/'budget.db',8*65536)
            finally:
                server.shutdown();server.server_close();thread.join()

    def test_boundary_replay_and_verified_completion(self):
        after='2026-10-14T00:00:00.000Z';cutoff='2026-10-15T00:00:00.000Z'
        result=page_result([row('2026-10-14T01:00:00.001Z')],after,cutoff)
        self.assertEqual(result['next_after'],'2026-10-14T01:00:00.000Z')
        self.assertFalse(result['complete'])
        result=page_result([row('2026-10-14T01:00:00.001Z'),row(cutoff)],after,cutoff)
        self.assertTrue(result['complete']);self.assertEqual(len(result['records']),1)

    def test_incomplete_and_stuck_pages_fail(self):
        after='2026-10-14T00:00:00.000Z';cutoff='2026-10-15T00:00:00.000Z'
        for records in [[],[row(after)],[row('2026-10-14T00:00:00.001Z')],[row('2026-10-14T02:00:00.000Z'),row('2026-10-14T01:00:00.000Z')]]:
            with self.assertRaises(ValueError):page_result(records,after,cutoff)


if __name__=='__main__':unittest.main()
