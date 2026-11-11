import unittest

import httpx

from transport import Budget, BudgetExceeded, Client, TransportError, redact, secret_from_env

SECRET = "s3cr3t-value-0123456789"


def client(handler, **options):
    settings = dict(name="backend", base="http://127.0.0.1:9", budget=Budget(100, 10**6), secret=SECRET, retries=0)
    settings.update(options)
    return Client(http=httpx.Client(transport=httpx.MockTransport(handler)), **settings)


class RedactionTests(unittest.TestCase):
    def test_redact_removes_every_occurrence(self):
        self.assertEqual(redact(f"a {SECRET} b {SECRET}", [SECRET, "", None]), "a [redacted] b [redacted]")

    def test_error_body_echoing_the_bearer_is_redacted(self):
        def handler(request):
            return httpx.Response(401, text="bad token " + request.headers["authorization"])

        with self.assertRaises(TransportError) as raised:
            client(handler).post_json("/admin/source-state", {"dids": []})
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIn("[redacted]", str(raised.exception))
        self.assertIn("401", str(raised.exception))

    def test_connection_failure_text_is_redacted_and_unchained(self):
        def handler(request):
            raise httpx.ConnectError("cannot reach host with " + SECRET)

        with self.assertRaises(TransportError) as raised:
            client(handler).post_json("/admin/source-page", {"after_did": "", "limit": 1})
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_bearer_goes_only_to_admin_paths(self):
        seen = {}

        def handler(request):
            seen[request.url.path] = request.headers.get("authorization")
            return httpx.Response(200, json={})

        backend = client(handler)
        backend.get_json("/index/status")
        backend.get("/search", {"q": "a"})
        backend.post_json("/admin/source-state", {"dids": []})
        self.assertEqual(seen, {"/index/status": None, "/search": None, "/admin/source-state": "Bearer " + SECRET})

    def test_bearer_is_refused_over_plain_http_to_a_remote_host(self):
        with self.assertRaises(TransportError) as raised:
            client(lambda request: httpx.Response(200), base="http://example.test")
        self.assertNotIn(SECRET, str(raised.exception))
        client(lambda request: httpx.Response(200), base="https://example.test")
        client(lambda request: httpx.Response(200), base="http://example.test", secret=None)

    def test_secret_comes_from_a_named_variable_and_the_error_names_only_the_variable(self):
        self.assertEqual(secret_from_env("X_ADMIN", {"X_ADMIN": SECRET}), SECRET)
        self.assertIsNone(secret_from_env(None, {}))
        with self.assertRaises(TransportError) as raised:
            secret_from_env("X_ADMIN", {"X_ADMIN": ""})
        self.assertIn("X_ADMIN", str(raised.exception))


class BudgetTests(unittest.TestCase):
    def test_request_budget_stops_before_sending(self):
        sent = []

        def handler(request):
            sent.append(request.url.path)
            return httpx.Response(200, json={})

        backend = client(handler, budget=Budget(2, 10**6))
        backend.get_json("/a")
        backend.get_json("/b")
        with self.assertRaises(BudgetExceeded):
            backend.get_json("/c")
        self.assertEqual(sent, ["/a", "/b"])

    def test_byte_budget_counts_request_and_response_bodies(self):
        backend = client(lambda request: httpx.Response(200, content=b"x" * 600), budget=Budget(10, 1000))
        backend.post("/admin/source-state", {"dids": []})
        self.assertEqual(backend.budget.bytes, 600 + len(b'{"dids":[]}'))
        backend.get("/a")
        with self.assertRaises(BudgetExceeded):
            backend.get("/b")

    def test_retries_are_charged(self):
        replies = iter([httpx.Response(503), httpx.Response(200, json={})])
        backend = client(lambda request: next(replies), retries=1)
        backend.get_json("/a")
        self.assertEqual(backend.budget.requests, 2)


class PacingTests(unittest.TestCase):
    def test_requests_are_spaced_by_the_delay(self):
        now = [0.0]
        slept = []

        def sleep(seconds):
            slept.append(round(seconds, 3))
            now[0] += seconds

        backend = client(lambda request: httpx.Response(200, json={}), delay=0.5, sleep=sleep, clock=lambda: now[0])
        backend.get_json("/a")
        backend.get_json("/b")
        now[0] += 0.2
        backend.get_json("/c")
        now[0] += 2
        backend.get_json("/d")
        self.assertEqual(slept, [0.5, 0.3])

    def test_a_slow_answer_is_followed_by_a_rest_in_proportion(self):
        now = [0.0]
        slept = []
        took = iter([0.01, 0.4, 0.4, 0.01])

        def sleep(seconds):
            slept.append(round(seconds, 3))
            now[0] += seconds

        def answer(request):
            now[0] += next(took)
            return httpx.Response(200, json={})

        backend = client(answer, delay=0.05, rest=1.5, sleep=sleep, clock=lambda: now[0])
        for path in ("/a", "/b", "/c", "/d"):
            backend.get_json(path)
        self.assertEqual(slept, [0.05, 0.6, 0.6])
        self.assertEqual(backend.budget.snapshot()["seconds_in_requests"], 0.8)


if __name__ == "__main__":
    unittest.main()
