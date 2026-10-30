"""Actual source-authority no-op projection/replay and bounded synthetic write experiment."""

import argparse
import copy
import hashlib
import http.client
import json
import os
import resource
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from smoke import BIN, actor, port, request, stop, write_seed


def start(binary, data, secret, log, crash=None):
    public, admin = port(), port()
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ["PATH", "TMPDIR", "LANG"]
    }
    env["ADMIN_SECRET"] = secret
    if crash:
        env["TYPEAHEAD_TEST_CRASH"] = crash
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    child = subprocess.Popen(
        [
            str(binary),
            "serve",
            "--data",
            str(data),
            "--writer-memory-mb",
            "32",
            "--allow-partial",
            "true",
            "--allow-stale",
            "true",
            "--listen",
            f"127.0.0.1:{public}",
            "--admin-listen",
            f"127.0.0.1:{admin}",
        ],
        env=env,
        stdout=log,
        stderr=log,
    )
    base, private = f"http://127.0.0.1:{public}", f"http://127.0.0.1:{admin}"
    for _ in range(100):
        if child.poll() is not None:
            raise AssertionError("fixture server exited")
        try:
            if request(base, "/health")[0] == 200:
                return child, base, private
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.025)
    stop(child)
    raise AssertionError("fixture server startup deadline")


def state(admin, rows, secret):
    code, _, body = request(
        admin, "/admin/source-state", {"dids": [a["did"] for a in rows]}, secret
    )
    assert code == 200, body
    return body


def batch(admin, rows, secret, tag):
    old = state(admin, rows, secret)
    result = {
        "actors": rows,
        "expected_documents": old["actors"],
        "expected_source_cursor": old["source_cursor"],
        "source_cursor": {"step": old["source_observation_revision"] + 1},
        "expected_observation_revision": old["source_observation_revision"],
        "batch_id": hashlib.sha256(tag.encode()).hexdigest(),
    }
    return result


def submit(admin, body, secret):
    code, _, receipt = request(admin, "/admin/source-reconcile", body, secret)
    assert code == 200, receipt
    assert (
        receipt["source_observation_revision"]
        == body["expected_observation_revision"] + 1
    )
    assert receipt["source_cursor"] == body["source_cursor"]
    return receipt


def fingerprints(admin, rows, secret):
    values = []
    for index in range(0, len(rows), 500):
        values.extend(state(admin, rows[index : index + 500], secret)["actors"])
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def files(data):
    all_bytes = immutable_bytes = 0
    immutable = {}
    for path in sorted((data / "index").iterdir()):
        if not path.is_file():
            continue
        size = path.stat().st_size
        all_bytes += size
        if path.name not in ["meta.json", ".managed.json"] and not path.name.endswith(
            ".lock"
        ):
            immutable_bytes += size
            immutable[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = json.loads(json.loads((data / "index/meta.json").read_text())["payload"])
    return {
        "total_index_file_bytes": all_bytes,
        "immutable_segment_bytes": immutable_bytes,
        "immutable_segment_file_sha256": hashlib.sha256(
            json.dumps(immutable, sort_keys=True).encode()
        ).hexdigest(),
        "immutable_segment_file_count": len(immutable),
        "durable_observation_revision": payload.get("source_observation_revision", 0),
        "last_request_sha256": (payload.get("last_source_batch") or {}).get(
            "request_sha256"
        ),
    }


def seed(binary, root, count):
    rows = [
        actor(
            index,
            f"version{index}.fixture.test",
            f"Version Fixture {index}",
            score=index % 123,
        )
        for index in range(count)
    ]
    source = root / "input.jsonl"
    write_seed(source, rows)
    data = root / "data"
    subprocess.run(
        [
            str(binary),
            "import",
            "--data",
            str(data),
            "--input",
            str(source),
            "--allow-partial",
            "true",
            "--writer-memory-mb",
            "32",
            "--batch-size",
            "500",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return data, rows


def write_trial(binary, count, optimized):
    with tempfile.TemporaryDirectory(prefix="tantivy-version-write-") as temporary:
        root = Path(temporary)
        data, rows = seed(binary, root, count)
        secret = secrets.token_hex(24)
        initial_files = files(data)
        phases = []
        logical_writes = commits = 0
        with (root / "server.log").open("ab") as log:
            child, public, admin = start(binary, data, secret, log)
            try:
                initial_state = fingerprints(admin, rows, secret)
                initial_status = request(public, "/index/status")[2]
                assert initial_status["ready"]
            finally:
                stop(child)
            for label, version in [
                ("higher", 200),
                ("lower", 50),
                ("equal_to_seed", 100),
            ]:
                child, public, admin = start(binary, data, secret, log)
                try:
                    applied = replayed = 0
                    previous_revision = request(public, "/index/status")[2][
                        "source_observation_revision"
                    ]
                    for offset in range(0, count, 500):
                        incoming = [
                            dict(a, version=version)
                            for a in rows[offset : offset + 500]
                        ]
                        body = batch(admin, incoming, secret, f"{label}:{offset}")
                        receipt = submit(admin, body, secret)
                        applied += receipt["applied"]
                        replayed += receipt["replayed"]
                        commits += 1
                    status = request(public, "/index/status")[2]
                    assert status["ready"]
                    if optimized and "expiry_coverage" in initial_status:
                        assert status["expiry_coverage_ready"]
                        assert (
                            status["expiry_coverage"]
                            == initial_status["expiry_coverage"]
                        )
                    current_fingerprints = fingerprints(admin, rows, secret)
                    if optimized:
                        assert applied == 0 and replayed == count
                        assert current_fingerprints == initial_state
                        assert (
                            status["indexed_memberships"]
                            == initial_status["indexed_memberships"]
                        )
                        assert status["segments"] == initial_status["segments"]
                    else:
                        assert applied == count and replayed == 0
                    assert (
                        status["source_observation_revision"]
                        == previous_revision + (count + 499) // 500
                    )
                    logical_writes += applied
                finally:
                    stop(child)
                physical = files(data)
                if optimized:
                    assert (
                        physical["immutable_segment_bytes"]
                        == initial_files["immutable_segment_bytes"]
                    )
                    assert (
                        physical["immutable_segment_file_sha256"]
                        == initial_files["immutable_segment_file_sha256"]
                    )
                phases.append(
                    {
                        "phase": label,
                        "incoming_version": version,
                        "applied_documents": applied,
                        "replayed_documents": replayed,
                        "indexed_memberships": status["indexed_memberships"],
                        "segments": status["segments"],
                        "expiry_coverage_ready": status.get("expiry_coverage_ready"),
                        "expiry_coverage_live_actors": (
                            status.get("expiry_coverage") or {}
                        )
                        .get("current", {})
                        .get("live_actors"),
                        "document_digest_unchanged": current_fingerprints
                        == initial_state,
                        **physical,
                    }
                )
        return {
            "actors": count,
            "synthetic": True,
            "actual_tantivy_http": True,
            "optimized_expected": optimized,
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "initial": initial_files,
            "metadata_commits": commits,
            "logical_document_writes": logical_writes,
            "phases": phases,
            "timing_claim": False,
            "temporary_payloads_removed": True,
        }


def semantics(binary):
    checks = []
    with tempfile.TemporaryDirectory(prefix="tantivy-version-semantics-") as temporary:
        root = Path(temporary)
        data, rows = seed(binary, root, 8)
        secret = secrets.token_hex(24)
        with (root / "server.log").open("ab") as log:
            child, public, admin = start(binary, data, secret, log)
            try:
                original = rows[0]
                initial = state(admin, [original], secret)["actors"][0]
                # Generic timestamp ordering is unchanged before source-authority migration.
                code, _, r = request(
                    admin,
                    "/admin/apply",
                    {"actors": [dict(original, version=99)]},
                    secret,
                )
                assert code == 200 and r["stale"] == 1
                code, _, r = request(
                    admin,
                    "/admin/apply",
                    {"actors": [dict(original, version=101)]},
                    secret,
                )
                assert code == 200 and r["applied"] == 1
                assert state(admin, [original], secret)["actors"][0] != initial
                code, _, _ = request(
                    admin,
                    "/admin/apply",
                    {
                        "actors": [
                            dict(original, version=101, display_name="conflicting")
                        ]
                    },
                    secret,
                )
                assert code == 409
                checks.append("legacy_strict_timestamps_unchanged_before_migration")
                current = dict(original, version=101)
                digest = state(admin, [current], secret)["actors"][0]
                before = request(public, "/index/status")[2]
                body = batch(
                    admin, [dict(current, version=2)], secret, "timestamp-only-start"
                )
                receipt = submit(admin, body, secret)
                assert receipt["replayed"] == 1 and receipt["applied"] == 0
                assert state(admin, [current], secret)["actors"][0] == digest
                assert (
                    request(public, "/index/status")[2]["indexed_memberships"]
                    == before["indexed_memberships"]
                )
                changed = copy.deepcopy(body)
                changed["actors"][0]["version"] = 3
                assert (
                    request(admin, "/admin/source-reconcile", changed, secret)[0] == 409
                )
                assert (
                    request(admin, "/admin/source-reconcile", body, secret)[2]
                    == receipt
                )
                checks.append(
                    "version_only_preserves_digest_but_request_identity_remains_strict"
                )
                for version in [1, 101, 999999999]:
                    assert (
                        request(
                            admin,
                            "/admin/apply",
                            {"actors": [dict(current, version=version)]},
                            secret,
                        )[0]
                        == 409
                    )
                checks.append("generic_writes_fenced_after_source_migration")
                changes = {
                    "handle": "version-renamed.fixture.test",
                    "display_name": "Changed Projection",
                    "avatar_url": "bafkchanged",
                    "labels": [{"val": "new-label"}],
                    "associated": {"chat": {"allowIncoming": "none"}},
                    "created_at": "2026-10-29T00:00:00Z",
                    "pds": "https://remote.test",
                    "quality_score": 42.5,
                    "followers_count": 1,
                    "follows_count": 2,
                    "posts_count": 3,
                    "last_activity_at": 4,
                    "hidden": True,
                    "deleted": True,
                }
                for field, value in changes.items():
                    old_digest = state(admin, [current], secret)["actors"][0]
                    current[field] = value
                    body = batch(admin, [current.copy()], secret, "actual-" + field)
                    applied = submit(admin, body, secret)
                    assert applied["applied"] == 1 and applied["replayed"] == 0, field
                    assert state(admin, [current], secret)["actors"][0] != old_digest, (
                        field
                    )
                assert not request(public, "/search?q=version-renamed")[2]["actors"]
                current.update(deleted=False, hidden=False)
                body = batch(admin, [current.copy()], secret, "restore-same-version")
                assert submit(admin, body, secret)["applied"] == 1
                assert (
                    request(public, "/search?q=version-renamed")[2]["actors"][0]["did"]
                    == current["did"]
                )
                checks.append(
                    "every_nonversion_field_and_same_second_hide_delete_restore_preserved"
                )
                body = batch(
                    admin,
                    [dict(current, version=0)],
                    secret,
                    "timestamp-before-restart",
                )
                receipt = submit(admin, body, secret)
                assert receipt["replayed"] == 1
                stop(child)
                child, public, admin = start(binary, data, secret, log)
                assert (
                    request(admin, "/admin/source-reconcile", body, secret)[2]
                    == receipt
                )
                checks.append("timestamp_only_retry_receipt_survives_restart")
                for killpoint in ["before_commit", "after_commit"]:
                    old = state(admin, [current], secret)
                    old_status = request(public, "/index/status")[2]
                    body = batch(
                        admin,
                        [dict(current, version=99999)],
                        secret,
                        "crash-" + killpoint,
                    )
                    stop(child)
                    child, public, admin = start(
                        binary, data, secret, log, crash=killpoint
                    )
                    try:
                        request(admin, "/admin/source-reconcile", body, secret)
                        raise AssertionError("killpoint ignored")
                    except (OSError, http.client.HTTPException):
                        pass
                    child.wait(timeout=5)
                    assert child.returncode != 0
                    child, public, admin = start(binary, data, secret, log)
                    recovered = request(public, "/index/status")[2]
                    assert recovered["source_observation_revision"] == old_status[
                        "source_observation_revision"
                    ] + int(killpoint == "after_commit")
                    assert recovered["source_cursor"] == (
                        body["source_cursor"]
                        if killpoint == "after_commit"
                        else old["source_cursor"]
                    )
                    assert state(admin, [current], secret)["actors"] == old["actors"]
                    assert (
                        recovered["indexed_memberships"]
                        == old_status["indexed_memberships"]
                    )
                    receipt = submit(admin, body, secret)
                    assert receipt["replayed"] == 1 and receipt["applied"] == 0
                    stop(child)
                    child, public, admin = start(binary, data, secret, log)
                    assert (
                        request(admin, "/admin/source-reconcile", body, secret)[2]
                        == receipt
                    )
                checks.append(
                    "metadata_only_observation_crash_atomicity_and_durable_retry"
                )
            finally:
                stop(child)
    return checks


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, default=BIN)
    p.add_argument("--baseline-binary", type=Path)
    p.add_argument("--actors", type=int, default=5000)
    p.add_argument(
        "--receipt",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "source-version-only-receipt.json",
    )
    args = p.parse_args()
    assert 1 <= args.actors <= 5000
    with tempfile.TemporaryDirectory(prefix="tantivy-version-binaries-") as frozen:
        binary = Path(frozen) / "optimized"
        shutil.copy2(args.binary.resolve(), binary)
        baseline = None
        if args.baseline_binary:
            baseline = Path(frozen) / "baseline"
            shutil.copy2(args.baseline_binary.resolve(), baseline)
        # Tiny operational pilot before the larger synthetic write comparison.
        pilot = write_trial(binary, 32, True)
        checks = semantics(binary)
        trials = []
        if baseline:
            trials.append(write_trial(baseline, args.actors, False))
        trials.append(write_trial(binary, args.actors, True))
        result = {
            "passed": True,
            "pilot_actors": pilot["actors"],
            "checks": checks,
            "trials": trials,
            "structural_lookup_accounting": {
                "source_state_queries_per_actor": 1,
                "source_cas_queries_per_actor": 1,
                "unchanged_projection_queries_per_actor": 1,
                "additional_old_record_accounting_queries_for_written_actor": 1,
                "observed_runtime_count": False,
            },
            "real_source_unchanged_fraction": "unmeasured; snapshot aggregate fields default zero so nonzero source values are real changes",
            "full_sweep_throughput_claim": False,
            "production_contact": False,
            "temporary_payloads_removed": True,
        }
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
