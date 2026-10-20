import functools
import hashlib
import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from pds_bundle_download import Budget,download


class DownloadTests(unittest.TestCase):
    def test_real_transfer_cache_corruption_and_persistent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);payload=b'actual loopback transfer\n'*200
            (root/'source').write_bytes(payload)
            handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=tmp)
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                url=f'http://127.0.0.1:{server.server_port}/source'
                sha=hashlib.sha256(payload).hexdigest();size=len(payload)
                ledger=root/'budget.db';limit=(size+1)*2
                budget=Budget(ledger,limit)
                first=download(url,root/'saved',size,sha,budget)
                self.assertFalse(first['cached'])
                self.assertTrue(download(url,root/'saved',size,sha,budget)['cached'])
                self.assertEqual(budget.used(),size+1)
                with self.assertRaises(ValueError):download(url,root/'corrupt',size,'0'*64,budget)
                self.assertFalse((root/'corrupt').exists())
                self.assertTrue((root/'corrupt.part').exists())
                reopened=Budget(ledger,limit)
                self.assertEqual(reopened.used(),limit)
                with self.assertRaises(RuntimeError):download(url,root/'third',size,sha,reopened)
                self.assertFalse((root/'third.part').exists())
                with self.assertRaises(ValueError):Budget(ledger,limit+1)
                (root/'saved').write_bytes(b'changed')
                with self.assertRaises(ValueError):download(url,root/'saved',size,sha,reopened)
            finally:
                server.shutdown();server.server_close();thread.join()
