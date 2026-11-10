"""Paced, budgeted HTTP with secrets kept out of every message."""

import ipaddress
import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

USER_AGENT = "typeahead-compare/1 (zzstoatzz.io)"
REDACTED = "[redacted]"
RETRY_STATUSES = frozenset({502, 503, 504})


class TransportError(Exception):
    pass


class BudgetExceeded(TransportError):
    pass


def redact(text, secrets):
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def secret_from_env(name, environ=None):
    environ = os.environ if environ is None else environ
    if not name:
        return None
    value = environ.get(name, "")
    if not value:
        raise TransportError(f"environment variable {name} is unset or empty")
    return value


def loopback(host):
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Budget:
    max_requests: int
    max_bytes: int
    requests: int = 0
    bytes: int = 0

    def charge_request(self):
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request budget of {self.max_requests} exhausted")
        if self.bytes >= self.max_bytes:
            raise BudgetExceeded(f"byte budget of {self.max_bytes} exhausted")
        self.requests += 1

    def charge_bytes(self, count):
        self.bytes += count

    def snapshot(self):
        return {
            "requests": self.requests,
            "bytes": self.bytes,
            "max_requests": self.max_requests,
            "max_bytes": self.max_bytes,
        }


@dataclass
class Reply:
    status: int
    headers: dict
    body: bytes
    elapsed_ms: float

    def json(self):
        return json.loads(self.body)


@dataclass
class Client:
    name: str
    base: str
    budget: Budget
    secret: str | None = None
    delay: float = 0.0
    timeout: float = 15.0
    retries: int = 2
    http: httpx.Client | None = None
    sleep: object = time.sleep
    clock: object = time.monotonic
    extra_secrets: tuple = ()
    secret_prefix: str = "/admin/"
    last_finished: float | None = field(default=None, init=False)

    def __post_init__(self):
        self.base = self.base.rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise TransportError(f"{self.name}: base must be an http(s) origin without credentials")
        if self.secret and parts.scheme != "https" and not loopback(parts.hostname):
            raise TransportError(f"{self.name}: a bearer secret is only sent over https or to loopback")
        if self.http is None:
            self.http = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            )

    def secrets(self):
        return tuple(value for value in (self.secret, *self.extra_secrets) if value)

    def close(self):
        self.http.close()

    def pace(self):
        if self.last_finished is None or self.delay <= 0:
            return
        wait = self.delay - (self.clock() - self.last_finished)
        if wait > 0:
            self.sleep(wait)

    def send(self, method, path, *, params=None, body=None):
        headers = {}
        content = None
        if body is not None:
            content = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if self.secret and path.startswith(self.secret_prefix):
            headers["Authorization"] = "Bearer " + self.secret
        url = self.base + path
        failure = None
        for attempt in range(self.retries + 1):
            self.pace()
            self.budget.charge_request()
            self.budget.charge_bytes(len(content or b""))
            started = time.perf_counter()
            try:
                response = self.http.request(method, url, params=params, content=content, headers=headers)
            except httpx.HTTPError as error:
                self.last_finished = self.clock()
                failure = f"{type(error).__name__}: {error}"
                continue
            elapsed = (time.perf_counter() - started) * 1000
            self.last_finished = self.clock()
            self.budget.charge_bytes(len(response.content))
            if response.status_code in RETRY_STATUSES and attempt < self.retries:
                failure = f"status {response.status_code}"
                continue
            return Reply(
                response.status_code,
                {key.lower(): value for key, value in response.headers.items()},
                response.content,
                elapsed,
            )
        raise TransportError(redact(f"{self.name} {method} {path} failed: {failure}", self.secrets())) from None

    def get(self, path, params=None):
        return self.send("GET", path, params=params)

    def post(self, path, body):
        return self.send("POST", path, body=body)

    def expect_json(self, reply, method, path):
        if reply.status != 200:
            detail = reply.body[:200].decode("utf-8", "replace")
            raise TransportError(
                redact(f"{self.name} {method} {path} returned {reply.status}: {detail}", self.secrets())
            )
        try:
            return reply.json()
        except ValueError:
            raise TransportError(f"{self.name} {method} {path} returned a body that is not JSON") from None

    def get_json(self, path, params=None):
        return self.expect_json(self.get(path, params), "GET", path)

    def post_json(self, path, body):
        return self.expect_json(self.post(path, body), "POST", path)
