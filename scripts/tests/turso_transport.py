#!/usr/bin/env python3
"""Exercise the native client's failures against real loopback HTTP sockets."""
import http.server
import os
from pathlib import Path
import socket
import subprocess
import threading


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/disconnect":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["zig", "build", "test", "--summary", "all"],
            cwd=Path(__file__).resolve().parents[2] / "services",
            env={**os.environ, "TYPEAHEAD_TURSO_HTTP_TEST_URL": f"http://127.0.0.1:{server.server_port}"},
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()
    raise SystemExit(result.returncode)
