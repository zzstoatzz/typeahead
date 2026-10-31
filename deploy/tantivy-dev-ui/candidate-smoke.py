#!/usr/bin/env python3
"""Read-only candidate/preview integration smoke; never a load benchmark or admission."""

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import re
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREVIEW = "https://typeahead-tantivy-dev-ui.n8-3e9.workers.dev"
CANDIDATES = {
    "https://typeahead-tantivy-a.fly.dev",
    "https://typeahead-tantivy-b.fly.dev",
}
ALIASES = {
    "canonical": "/xrpc/tech.waow.typeahead.searchActors",
    "compatibility": "/xrpc/app.bsky.actor.searchActorsTypeahead",
}
HEADERS = (
    "x-typeahead-build",
    "x-typeahead-binary-sha256",
    "x-typeahead-retrieval-strategy",
    "x-typeahead-search-backend",
    "x-typeahead-pds",
    "x-typeahead-prefer-pds",
    "x-typeahead-preference",
    "x-typeahead-boost",
    "cache-control",
)


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise Failure("redirect")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def predicates():
    spec = importlib.util.spec_from_file_location(
        "candidate_real_predicates", HERE.parent / "tantivy-dev/qualify-real.py"
    )
    if spec is None or spec.loader is None:
        raise Failure("independent_predicates_unavailable")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def targets(candidate, preview, fixture):
    if not fixture and (candidate not in CANDIDATES or preview != PREVIEW):
        raise Failure("target_not_managed_candidate_and_preview")
    for value in (candidate, preview):
        parsed = urllib.parse.urlsplit(value)
        local = fixture and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        if fixture and not local:
            raise Failure("fixture_target_must_be_loopback")
        if not local and parsed.scheme != "https":
            raise Failure("target_scheme")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise Failure("target_shape")
    return candidate.rstrip("/"), preview.rstrip("/")


class Probe:
    def __init__(self, candidate, preview, build_id, binary_sha256, *, fixture=False):
        self.candidate, self.preview = targets(candidate, preview, fixture)
        if not re.fullmatch(r"b[0-9]{10}-[A-Za-z0-9_-]+", build_id) or not re.fullmatch(
            r"[0-9a-f]{64}", binary_sha256
        ):
            raise Failure("expected_identity")
        self.build, self.binary, self.fixture = build_id, binary_sha256, fixture
        self.started = time.monotonic()
        self.deadline = self.started + 120
        self.next_request = self.started
        self.requests = self.bytes = 0
        self.rows = []
        self.checks = []
        self.inconclusive = []
        self.opener = urllib.request.build_opener(NoRedirect())
        self.predicate = predicates()

    def check(self, name, condition):
        self.checks.append({"check": name, "passed": bool(condition)})
        return bool(condition)

    def get(self, target, path, case):
        if self.requests >= 80:
            raise Failure("request_budget")
        time.sleep(max(0, self.next_request - time.monotonic()))
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        self.next_request = time.monotonic() + (0 if self.fixture else 1)
        self.requests += 1
        started = time.monotonic()
        raw = bytearray()
        signal.setitimer(signal.ITIMER_REAL, min(5, remaining))
        try:
            request = urllib.request.Request(
                target + path,
                headers={
                    "User-Agent": "typeahead-candidate-smoke/1",
                    "X-Client": "smoke-testing",
                    "Accept": "application/json,text/html",
                },
            )
            try:
                response = self.opener.open(request, timeout=min(5, remaining))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = bytearray()
                while True:
                    block = response.read1(min(65536, 524289 - len(raw)))
                    if not block:
                        break
                    raw.extend(block)
                    self.bytes += len(block)
                    if len(raw) > 524288 or self.bytes > 32 * 1024**2:
                        raise Failure("response_byte_budget")
                headers = {
                    key: response.headers.get(key)
                    for key in HEADERS
                    if response.headers.get(key) is not None
                }
                row = {
                    "case": case,
                    "target": "preview" if target == self.preview else "candidate",
                    "status": response.status,
                    "body_sha256": sha(raw),
                    "bytes": len(raw),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "headers": headers,
                }
            self.rows.append(row)
            return row, bytes(raw)
        except (
            Failure,
            TimeoutError,
            OSError,
            ValueError,
            http.client.HTTPException,
        ) as error:
            self.rows.append(
                {
                    "case": case,
                    "target": "preview" if target == self.preview else "candidate",
                    "status": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "error_class": type(error).__name__,
                    "body_sha256": sha(raw),
                    "bytes": len(raw),
                }
            )
            raise
        finally:
            signal.setitimer(
                signal.ITIMER_REAL, max(0, self.deadline - time.monotonic())
            )

    def status(self, target, phase):
        row, raw = self.get(target, "/index/status", phase)
        value = json.loads(raw)
        valid = (
            row["status"] == 200
            and value.get("engine") == "tantivy"
            and value.get("full_source") is True
            and value.get("import_complete") is True
            and value.get("ready") is True
            and value.get("build_id") == self.build
            and value.get("binary_sha256") == self.binary
            and value.get("retrieval_strategy") == "global-merge"
        )
        if target == self.preview:
            valid = (
                valid
                and row["headers"].get("x-typeahead-search-backend") == self.candidate
                and row["headers"].get("cache-control") == "no-store"
            )
        if not self.check(phase + "-" + row["target"] + "-identity", valid):
            raise Failure("candidate_identity_or_readiness")
        cursor = sha(
            json.dumps(
                value.get("source_cursor"), sort_keys=True, separators=(",", ":")
            ).encode()
        )
        return {
            "build_id": value["build_id"],
            "source_build_id": value.get("source_build_id"),
            "watermark": value.get("watermark"),
            "source_cursor_sha256": cursor,
            "source_observation_revision": value.get("source_observation_revision"),
            "actor_count": value.get("actor_count"),
            "segments": value.get("segments"),
            "full_source": value["full_source"],
            "ready": value["ready"],
        }

    def search(self, target, alias, case):
        params = case["params"]
        path = ALIASES[alias] + "?" + urllib.parse.urlencode(params)
        row, raw = self.get(target, path, case["id"] + "-" + alias)
        value = json.loads(raw)
        valid = row["status"] == case["status"]
        if case["status"] == 200:
            actors = value.get("actors")
            limit = params.get("limit", 10)
            valid = valid and isinstance(actors, list) and len(actors) <= limit
            if not valid:
                actors = []
            ids = [actor.get("did") for actor in actors if isinstance(actor, dict)]
            valid = (
                valid
                and len(ids) == len(actors)
                and len(set(ids)) == len(ids)
                and all(isinstance(did, str) and did.startswith("did:") for did in ids)
            )
            valid = (
                valid
                and row["headers"].get("x-typeahead-build") == self.build
                and row["headers"].get("x-typeahead-binary-sha256") == self.binary
                and row["headers"].get("x-typeahead-retrieval-strategy")
                == "global-merge"
            )
            for key, expected in case.get("headers", {}).items():
                valid = valid and row["headers"].get(key) == expected
            if "first_handle" in case:
                valid = (
                    valid
                    and bool(actors)
                    and actors[0].get("handle") == case["first_handle"]
                )
            if "contains_handle" in case:
                valid = valid and any(
                    actor.get("handle") == case["contains_handle"] for actor in actors
                )
            if "contains_did" in case:
                valid = valid and case["contains_did"] in ids
            if case.get("require_nonempty"):
                valid = valid and bool(actors)
            known = unknown = 0
            for actor in actors:
                sound = self.predicate.ascii_sound(actor, params["q"])
                if sound is None:
                    sound = self.predicate.han_sound(actor, params["q"])
                if sound is None:
                    unknown += 1
                else:
                    known += 1
                    valid = valid and sound
            row.update(
                returned=len(actors), lexical_checked=known, lexical_unverified=unknown
            )
            if case["id"] == "han":
                valid = valid and known > 0
        else:
            valid = (
                valid
                and isinstance(value, dict)
                and isinstance(value.get("error"), str)
            )
            if target == self.preview and "preview_error" in case:
                valid = valid and value.get("error") == case["preview_error"]
        if target == self.preview:
            valid = valid and row["headers"].get("cache-control") == "no-store"
            if row["status"] == 200:
                valid = (
                    valid
                    and row["headers"].get("x-typeahead-search-backend")
                    == self.candidate
                )
        self.check(row["target"] + "-" + row["case"], valid)
        return row

    def run(self):
        cases = json.loads((HERE / "candidate-smoke-cases.json").read_bytes())
        if len(cases) != 10:
            raise Failure("unexpected_case_budget")
        before = {
            target: self.status(target, "before")
            for target in (self.candidate, self.preview)
        }
        for target in (self.candidate, self.preview):
            for path in ("/ready", "/health"):
                row, _ = self.get(target, path, path[1:])
                self.check(row["target"] + path, row["status"] == 200)
        homepage, raw = self.get(self.preview, "/", "homepage")
        self.check(
            "preview-homepage-html",
            homepage["status"] == 200
            and (b"<!doctype html" in raw.lower() or b"<html" in raw.lower())
            and homepage["headers"].get("x-typeahead-search-backend") == self.candidate,
        )
        pairs = []
        for target in (self.candidate, self.preview):
            for case in cases:
                left = self.search(target, "canonical", case)
                right = self.search(target, "compatibility", case)
                if case["status"] == 200:
                    pairs.append(
                        (
                            target,
                            case["id"],
                            left["body_sha256"] == right["body_sha256"],
                        )
                    )
        for target in (self.candidate, self.preview):
            for case in cases[:2]:
                self.search(target, "canonical", {**case, "id": case["id"] + "-repeat"})
        after = {
            target: self.status(target, "after")
            for target in (self.candidate, self.preview)
        }
        for target, case, equal in pairs:
            if not equal and any(
                before[target][key] != after[target][key]
                for key in (
                    "build_id",
                    "source_cursor_sha256",
                    "source_observation_revision",
                    "watermark",
                )
            ):
                self.inconclusive.append(
                    ("preview" if target == self.preview else "candidate")
                    + "-"
                    + case
                    + "-live-alias-window"
                )
            else:
                self.check(
                    ("preview" if target == self.preview else "candidate")
                    + "-"
                    + case
                    + "-alias-bytes",
                    equal,
                )
        return {
            "before": list(before.values()),
            "after": list(after.values()),
            "homepage_sha256": homepage["body_sha256"],
        }


def run(candidate, preview, build_id, binary_sha256, *, fixture=False):
    probe = Probe(candidate, preview, build_id, binary_sha256, fixture=fixture)
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(*_):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 120)
    detail = {}
    fatal = None
    try:
        detail = probe.run()
    except (
        Failure,
        TimeoutError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        http.client.HTTPException,
    ) as error:
        fatal = type(error).__name__
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, old_timer[0] - (time.monotonic() - probe.started)),
                old_timer[1],
            )
    return {
        "kind": "managed-candidate-readonly-smoke",
        "fixture": fixture,
        "passed": fatal is None
        and not probe.inconclusive
        and all(check["passed"] for check in probe.checks),
        "fatal_error_class": fatal,
        "expected_build_id": build_id,
        "expected_binary_sha256": binary_sha256,
        "candidate": probe.candidate,
        "preview": probe.preview,
        "requests": probe.requests,
        "received_bytes": probe.bytes,
        "wall_seconds": round(time.monotonic() - probe.started, 3),
        "checks": probe.checks,
        "inconclusive": probe.inconclusive,
        "responses": probe.rows,
        **detail,
        "resource_verified": False,
        "source_membership_verified": False,
        "full_topk_oracle": False,
        "performance_qualified": False,
        "promotion_permitted": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--preview", default=PREVIEW)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise Failure("output_exists")
    report = run(args.candidate, args.preview, args.build_id, args.binary_sha256)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(report, output, sort_keys=True)
        output.write("\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "requests": report["requests"],
                "wall_seconds": report["wall_seconds"],
                "failed_checks": sum(not check["passed"] for check in report["checks"]),
                "inconclusive": len(report["inconclusive"]),
                "fatal_error_class": report["fatal_error_class"],
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
