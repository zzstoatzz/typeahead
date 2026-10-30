import contextlib
import importlib.util
import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

spec = importlib.util.spec_from_file_location(
    "candidate_smoke", Path(__file__).with_name("candidate-smoke.py")
)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
BUILD = "b1793236000-fixture"
BINARY = "a" * 64


@contextlib.contextmanager
def servers(
    *,
    wrong_preview=False,
    missing_ack=False,
    mismatch=False,
    changing=False,
    redirect=False,
    slow=False,
):
    state: dict[str, Any] = {
        "requests": [],
        "status_reads": {"candidate": 0, "preview": 0},
    }
    instances = []
    for role in ("candidate", "preview"):

        def handler_for(role):
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *_args):
                    pass

                def do_GET(self):
                    url = urllib.parse.urlsplit(self.path)
                    params = dict(urllib.parse.parse_qsl(url.query))
                    state["requests"].append((role, self.path))
                    if redirect and url.path == "/index/status":
                        self.send_response(302)
                        self.send_header("Location", state["candidate"] + "/forbidden")
                        self.end_headers()
                        return
                    headers = {
                        "x-typeahead-build": BUILD,
                        "x-typeahead-binary-sha256": BINARY,
                        "x-typeahead-retrieval-strategy": "global-merge",
                        "cache-control": "no-store",
                    }
                    if role == "preview":
                        headers["x-typeahead-search-backend"] = (
                            "https://typeahead-tantivy-dev.fly.dev"
                            if wrong_preview
                            else state["candidate"]
                        )
                    status = 200
                    if url.path == "/index/status":
                        state["status_reads"][role] += 1
                        revision = (
                            2 if changing and state["status_reads"][role] > 1 else 1
                        )
                        body = {
                            "engine": "tantivy",
                            "build_id": BUILD,
                            "binary_sha256": BINARY,
                            "retrieval_strategy": "global-merge",
                            "full_source": True,
                            "import_complete": True,
                            "ready": True,
                            "source_build_id": BUILD,
                            "source_cursor": {"through": 100, "revision": revision},
                            "source_observation_revision": revision,
                            "watermark": 100,
                            "actor_count": 32,
                            "segments": 1,
                        }
                    elif url.path in ("/health", "/ready"):
                        body = {"ready": True}
                    elif url.path == "/":
                        body = b"<!doctype html><title>Production UI fixture</title>"
                    elif url.path.startswith("/xrpc/"):
                        bad = (
                            params.get("pds") == "bad@host.test"
                            or ("boost" in params and "prefer_pds" not in params)
                            or params.get("boost") == "-1"
                        )
                        if bad:
                            status = 400
                            body = {"error": "InvalidRequest"}
                        elif (
                            missing_ack
                            and params.get("preference") == "boost"
                            and role == "preview"
                        ):
                            status = 503
                            body = {"error": "PdsScopeUnavailable"}
                        else:
                            q = params["q"]
                            actor = {
                                "did": "did:fixture:one",
                                "handle": "alice.test",
                                "displayName": "Alice",
                                "associated": {"private-marker": "not-in-receipts"},
                            }
                            if q in ("zzstoatzz.io", "zzst"):
                                actor.update(handle="zzstoatzz.io", displayName="Nate")
                            if q == "Justin Fa":
                                actor.update(
                                    did="did:plc:ec64xv7n5dszeizw56yr6b5h",
                                    handle="justinfagnani.com",
                                    displayName="Justin Fagnani",
                                )
                            if q == "東京":
                                actor.update(handle="tokyo.test", displayName="東京")
                            actors = [actor]
                            if (
                                mismatch
                                and role == "candidate"
                                and "app.bsky.actor." in url.path
                                and q == "zzstoatzz.io"
                            ):
                                actors.append(
                                    {
                                        "did": "did:fixture:other",
                                        "handle": "zzstoatzz.io.extra",
                                        "displayName": "Other",
                                    }
                                )
                            body = {"actors": actors}
                            if "pds" in params:
                                headers["x-typeahead-pds"] = params["pds"]
                            if "prefer_pds" in params:
                                headers["x-typeahead-prefer-pds"] = params["prefer_pds"]
                                headers["x-typeahead-preference"] = params["preference"]
                                if (
                                    params.get("preference") == "boost"
                                    and not missing_ack
                                ):
                                    headers["x-typeahead-boost"] = "42c80000"
                    else:
                        status = 404
                        body = {"error": "NotFound"}
                    raw = (
                        body
                        if isinstance(body, bytes)
                        else json.dumps(body, separators=(",", ":")).encode()
                    )
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(raw)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        if slow and url.path == "/index/status":
                            for byte in raw:
                                self.wfile.write(bytes([byte]))
                                self.wfile.flush()
                                time.sleep(0.2)
                        else:
                            self.wfile.write(raw)

            return Handler

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(role))
        state[role] = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        instances.append((server, thread))
    try:
        yield state
    finally:
        for server, thread in instances:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class Tests(unittest.TestCase):
    def probe(self, state):
        return smoke.run(
            state["candidate"], state["preview"], BUILD, BINARY, fixture=True
        )

    def test_real_http_cases_aliases_receipts_are_read_only_and_metadata_only(self):
        with servers() as state:
            result = self.probe(state)
            self.assertTrue(result["passed"], result["checks"])
            self.assertEqual(result["requests"], 53)
            self.assertEqual(len(state["requests"]), 53)
            self.assertFalse(result["source_membership_verified"])
            self.assertFalse(result["resource_verified"])
            self.assertNotIn("not-in-receipts", json.dumps(result))
            self.assertNotIn("displayName", json.dumps(result))
            self.assertTrue(
                all(
                    path in ("/index/status", "/health", "/ready", "/")
                    or path.startswith("/xrpc/")
                    for _, path in state["requests"]
                )
            )

    def test_wrong_preview_binding_stops_before_search(self):
        with servers(wrong_preview=True) as state:
            result = self.probe(state)
            self.assertFalse(result["passed"])
            self.assertEqual(result["requests"], 2)
            self.assertFalse(any("/xrpc/" in path for _, path in state["requests"]))

    def test_missing_scope_ack_or_preview_rejection_never_passes_as_global(self):
        with servers(missing_ack=True) as state:
            result = self.probe(state)
            self.assertFalse(result["passed"])
            failed = [
                check["check"] for check in result["checks"] if not check["passed"]
            ]
            self.assertTrue(any("pds-boost" in name for name in failed))

    def test_alias_mismatch_is_failure_in_stable_view_and_inconclusive_if_source_changes(
        self,
    ):
        for changing in (False, True):
            with (
                self.subTest(changing=changing),
                servers(mismatch=True, changing=changing) as state,
            ):
                result = self.probe(state)
                self.assertFalse(result["passed"])
                self.assertEqual(bool(result["inconclusive"]), changing)
                if not changing:
                    self.assertTrue(
                        any(
                            "alias-bytes" in check["check"] and not check["passed"]
                            for check in result["checks"]
                        )
                    )

    def test_redirect_does_not_follow(self):
        with servers(redirect=True) as state:
            result = self.probe(state)
            self.assertFalse(result["passed"])
            self.assertEqual(len(state["requests"]), 1)

    def test_trickled_body_enforces_five_second_request_deadline(self):
        with servers(slow=True) as state:
            started = time.monotonic()
            result = self.probe(state)
            self.assertFalse(result["passed"])
            self.assertEqual(result["fatal_error_class"], "TimeoutError")
            self.assertLess(time.monotonic() - started, 6)

    def test_public_guard_rejects_production_and_old_dev_without_request(self):
        for candidate in (
            "https://typeahead.waow.tech",
            "https://typeahead-tantivy-dev.fly.dev",
        ):
            for fixture in (False, True):
                with self.assertRaises(smoke.Failure):
                    smoke.run(candidate, smoke.PREVIEW, BUILD, BINARY, fixture=fixture)


if __name__ == "__main__":
    unittest.main()
