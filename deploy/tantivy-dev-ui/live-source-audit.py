#!/usr/bin/env python3
"""Bounded current-source witnesses; no source writes, reconciliation or cursor refresh."""

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Pinned audit helper missing")
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def run(
    candidate,
    build_id,
    binary_sha256,
    environment,
    *,
    input_path=None,
    admin_url="http://127.0.0.1:8081",
    known_handles=None,
    fixture=False,
):
    smoke = module("live_audit_smoke", HERE / "candidate-smoke.py")
    if fixture:
        preview = "http://127.0.0.1:1"
    else:
        app = environment.get("FLY_APP_NAME")
        if (
            sys.platform != "linux"
            or app not in ("typeahead-tantivy-a", "typeahead-tantivy-b")
            or not environment.get("FLY_MACHINE_ID")
            or candidate != "https://" + app + ".fly.dev"
            or admin_url != "http://127.0.0.1:8081"
        ):
            raise ValueError("Audit must run on its own managed candidate guest")
        preview = smoke.PREVIEW
    probe = smoke.Probe(candidate, preview, build_id, binary_sha256, fixture=fixture)
    source_root = Path(input_path) if input_path is not None else Path("/app/input")
    replay = module("live_audit_sync", source_root / "sync.py")
    current = module("live_audit_current", source_root / "current_state.py")
    source_url = replay.source_url(environment.get("TURSO_URL", ""), fixture)
    admin_url = replay.local_url(admin_url)
    if admin_url.rsplit(":", 1)[-1].isdigit() is False:
        raise ValueError("Explicit admin loopback port required")
    for key in ("TURSO_AUTH_TOKEN", "ADMIN_SECRET"):
        if (
            not isinstance(environment.get(key), str)
            or not environment[key]
            or "\n" in environment[key]
            or "\r" in environment[key]
        ):
            raise ValueError("Existing guest credential missing")
    known_handles = {} if known_handles is None else known_handles
    if (
        not isinstance(known_handles, dict)
        or len(known_handles) > 16
        or any(
            not isinstance(did, str)
            or not did.startswith("did:")
            or not isinstance(handle, str)
            or not handle
            or len(handle.encode()) > 65535
            for did, handle in known_handles.items()
        )
    ):
        raise ValueError("At most16 known handle witnesses required")
    calls = []
    checks = []
    unavailable = []
    inconclusive = []
    next_request = time.monotonic()

    def check(name, passed):
        checks.append({"check": name, "passed": bool(passed)})

    def call(label, operation, *args):
        nonlocal next_request
        if len(calls) >= 64:
            raise ValueError("Audit request bound exceeded")
        time.sleep(max(0, next_request - time.monotonic()))
        next_request = time.monotonic() + (0 if fixture else 1)
        remaining = probe.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        row = {"operation": label}
        calls.append(row)
        started = time.monotonic()
        signal.setitimer(signal.ITIMER_REAL, min(5, remaining))
        try:
            result = operation(*args)
            row["response_sha256"] = (
                result[0]["body_sha256"]
                if isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[1], bytes)
                else digest(result)
            )
            return result
        except Exception as error:
            row["error_class"] = type(error).__name__
            raise
        finally:
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            signal.setitimer(
                signal.ITIMER_REAL, max(0, probe.deadline - time.monotonic())
            )

    class ReadOnlyHttp(replay.Http):
        def request(self, url, secret, body=None, max_bytes=4 * 1024 * 1024):
            if url == source_url:
                statements = body.get("requests", []) if isinstance(body, dict) else []
                if len(statements) != 2 or statements[0].get("stmt", {}).get(
                    "sql"
                ) not in (
                    replay.CLOCK_SQL,
                    replay.ACTORS_SQL,
                    replay.TOMBSTONES_SQL,
                    replay.CURRENT_POLICY_SQL,
                ):
                    raise ValueError("Audit source SQL denied")
            elif url == admin_url + "/admin/source-state":
                if (
                    not isinstance(body, dict)
                    or set(body) != {"dids"}
                    or len(body["dids"]) > 16
                ):
                    raise ValueError("Audit state request denied")
            else:
                raise ValueError("Audit write or endpoint denied")
            if self.received_bytes >= 16 * 1024**2:
                raise ValueError("Audit source/admin byte bound")
            result = super().request(url, secret, body, max_bytes)
            if self.received_bytes > 16 * 1024**2:
                raise ValueError("Audit source/admin byte bound")
            return result

    http = ReadOnlyHttp(timeout=5)
    source = replay.Turso(
        source_url,
        environment["TURSO_AUTH_TOKEN"],
        http=http,
        allow_test_loopback=fixture,
        retries=1,
        backoff=0,
        max_bytes=16 * 1024**2,
    )
    reader = current.CurrentPolicySource(source, replay)
    admin = current.ObservationAdmin(
        replay.Admin(admin_url, environment["ADMIN_SECRET"], http=http), replay
    )
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(*_):
        raise TimeoutError()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 120)
    fatal = None
    summary = {}
    present = scoped = preferred = moderated = deleted = restored = 0
    try:
        initial = call("backend-status-before", probe.status, candidate, "live-before")
        now = call("source-clock", source.now)
        # Canonical indexed keyset selectors, one bounded page per stream.
        floor = max(initial["watermark"] - 3600, 0)
        upper = min(initial["watermark"], now - 2)
        actors = call(
            "recent-actors",
            source.select,
            replay.ACTORS_SQL,
            [floor, floor, "", upper, 8],
        )
        tombs = call(
            "recent-tombstones",
            source.select,
            replay.TOMBSTONES_SQL,
            [floor, floor, "", upper, 8],
        )
        dids = sorted({row["did"] for row in actors + tombs})
        observed = call(
            "source-policy-projection-before", reader.observe_with_policy, dids
        )
        state, _ = call("backend-document-state-before", admin.state, dids)
        source_now = observed["source_now"]

        def policy_hash(value):
            return digest({**value["moderation"], "complete": True})

        # Fetch full public metadata transiently; only selected freshness fields survive.
        raw_status = call(
            "backend-freshness-before",
            probe.get,
            candidate,
            "/index/status",
            "live-freshness-before",
        )
        metadata = json.loads(raw_status[1])
        cursor = metadata.get("source_cursor") or {}
        check(
            "fresh-source-cursor",
            metadata.get("source_observation_revision", 0) > 0
            and cursor.get("source_policy") == "turso-authoritative-observations-v2"
            and cursor.get("build_id") == build_id
            and cursor.get("through") == metadata.get("watermark")
            and 0 <= source_now - metadata.get("watermark", 0) <= 120,
        )
        moderation = metadata.get("moderation", {})
        check(
            "fresh-canonical-policy",
            moderation.get("enabled") is True
            and moderation.get("complete") is True
            and 0 <= source_now - (moderation.get("source_observed_at") or 0) <= 300,
        )
        if moderation.get("sha256") != policy_hash(observed):
            inconclusive.append("backend-policy-has-not-covered-current-source-policy")
        profiles = {
            item["actor"]["did"]: item["actor"] for item in observed["observations"]
        }
        document_state = {
            item["did"]: item["document_sha256"] for item in state["actors"]
        }
        actions = {
            item["did"]: item["action"] for item in observed["moderation"]["overrides"]
        }
        domains = observed["moderation"]["blocked_domains"]

        def search(handle, case, **params):
            query = {"q": handle, "limit": 100, **params}
            row, raw = call(
                case,
                probe.get,
                candidate,
                "/search?" + urllib.parse.urlencode(query),
                case,
            )
            value = json.loads(raw)
            results = value.get("actors")
            if (
                row["status"] != 200
                or not isinstance(results, list)
                or len(results) > 100
            ):
                raise ValueError("Audit search response rejected")
            ids = [item.get("did") for item in results]
            if any(not isinstance(did, str) for did in ids) or len(ids) != len(
                set(ids)
            ):
                raise ValueError("Audit search identifiers rejected")
            check(
                case + "-identity",
                row["headers"].get("x-typeahead-build") == build_id
                and row["headers"].get("x-typeahead-binary-sha256") == binary_sha256,
            )
            for field, header in (
                ("pds", "x-typeahead-pds"),
                ("prefer_pds", "x-typeahead-prefer-pds"),
                ("preference", "x-typeahead-preference"),
            ):
                if field in params:
                    check(
                        case + "-" + field + "-ack",
                        row["headers"].get(header) == params[field],
                    )
            if "boost" in params:
                check(
                    case + "-boost-ack",
                    row["headers"].get("x-typeahead-boost") == "42c80000",
                )
            return {item["did"]: item for item in results}

        for index, row in enumerate(actors):
            actor = profiles[row["did"]]
            did = actor["did"]
            label = "actor-" + str(index)
            if actor["version"] > metadata["watermark"]:
                unavailable.append(label + "-newer-than-committed-watermark")
                continue
            if actor["deleted"]:
                if did in known_handles:
                    check(
                        label + "-cleared-absence",
                        did not in search(known_handles[did], label + "-cleared"),
                    )
                    deleted += 1
                else:
                    unavailable.append(label + "-cleared-old-handle-unavailable")
                continue
            handle = actor["handle"]
            action = actions.get(did)
            blocked = any(
                handle.lower() == domain or handle.lower().endswith("." + domain)
                for domain in domains
            )
            hidden = action == "hide" or (action != "show" and blocked)
            result = search(handle, label + "-exact")
            if hidden:
                check(label + "-authoritative-moderation-absence", did not in result)
                moderated += 1
                continue
            if did not in result:
                if action == "show":
                    check(label + "-explicit-show-presence", False)
                else:
                    unavailable.append(
                        label + "-not-returned-visibility-not-independently-established"
                    )
                continue
            present += 1
            check(label + "-stored-document", document_state.get(did) is not None)
            actual = result[did]
            check(
                label + "-public-hydration",
                actual.get("handle") == handle
                and actual.get("displayName", "") == actor["display_name"]
                and actual.get("createdAt", "") == actor["created_at"]
                and actual.get("associated", {}) == actor["associated"]
                and actual.get("labels") == actor["labels"],
            )
            host = probe.predicate.canonical_host(actor["pds"])
            if not host:
                unavailable.append(label + "-canonical-pds-unavailable")
                continue
            inside = search(handle, label + "-pds", pds=host)
            outside = (
                "live-audit-outside.invalid"
                if host != "live-audit-outside.invalid"
                else "other-audit-outside.invalid"
            )
            check(label + "-source-backed-pds-positive", did in inside)
            check(
                label + "-source-backed-pds-exclusion",
                did not in search(handle, label + "-outside", pds=outside),
            )
            scoped += 1
            for mode, extra in (("pds_first", {}), ("boost", {"boost": 100})):
                check(
                    label + "-" + mode + "-presence",
                    did
                    in search(
                        handle,
                        label + "-" + mode,
                        prefer_pds=host,
                        preference=mode,
                        **extra,
                    ),
                )
                preferred += 1
        for index, row in enumerate(tombs):
            did = row["did"]
            actor = profiles[did]
            label = "tombstone-" + str(index)
            if not actor["deleted"]:
                restored += 1
                continue
            if did in known_handles:
                check(
                    label + "-known-handle-absence",
                    did not in search(known_handles[did], label + "-absent"),
                )
                deleted += 1
            else:
                unavailable.append(label + "-old-handle-unavailable")
        observed_after = call(
            "source-policy-projection-after", reader.observe_with_policy, dids
        )
        state_after, _ = call("backend-document-state-after", admin.state, dids)
        final = call("backend-status-after", probe.status, candidate, "live-after")
        final_row, final_raw = call(
            "backend-freshness-after",
            probe.get,
            candidate,
            "/index/status",
            "live-freshness-after",
        )
        final_metadata = json.loads(final_raw)
        final_cursor = final_metadata.get("source_cursor") or {}
        final_policy = final_metadata.get("moderation", {})
        check(
            "fresh-source-cursor-after",
            final_row["status"] == 200
            and final_cursor.get("source_policy")
            == "turso-authoritative-observations-v2"
            and final_cursor.get("build_id") == build_id
            and final_cursor.get("through") == final_metadata.get("watermark")
            and 0
            <= observed_after["source_now"] - final_metadata.get("watermark", 0)
            <= 120,
        )
        check(
            "fresh-canonical-policy-after",
            final_policy.get("enabled") is True
            and final_policy.get("complete") is True
            and 0
            <= observed_after["source_now"]
            - (final_policy.get("source_observed_at") or 0)
            <= 300,
        )
        if final_policy.get("sha256") != policy_hash(observed_after):
            inconclusive.append(
                "final-backend-policy-has-not-covered-current-source-policy"
            )
        stable = (
            observed["observations"] == observed_after["observations"]
            and observed["moderation"] == observed_after["moderation"]
            and state["actors"] == state_after["actors"]
        )
        if not stable:
            inconclusive.append(
                "selected-source-documents-or-policy-changed-during-audit"
            )
            for item in checks:
                if not item["passed"]:
                    item.update(passed=None, classification="live-window-changed")
        if not present:
            inconclusive.append("no-returned-live-source-witness")
        if not moderated:
            unavailable.append("no-authoritative-moderation-absence-witness")
        if not deleted:
            unavailable.append("no-deleted-known-handle-absence-witness")
        summary = {
            "sampled_actors": len(actors),
            "sampled_tombstones": len(tombs),
            "sampled_unique_dids": len(dids),
            "present_witnesses": present,
            "source_backed_pds_pairs": scoped,
            "preference_acknowledged_requests": preferred,
            "moderated_absence_witnesses": moderated,
            "deleted_absence_witnesses": deleted,
            "retained_tombstones_with_current_live_actor": restored,
            "source_window": {"lower_exclusive": floor, "upper_inclusive": upper},
            "source_now": source_now,
            "before": initial,
            "after": final,
            "selected_documents_stable": stable,
            "state_digest_before": digest(state["actors"]),
            "state_digest_after": digest(state_after["actors"]),
        }
    except (
        replay.SafeError,
        smoke.Failure,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
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
        "kind": "bounded-live-source-audit",
        "fixture": fixture,
        "passed": fatal is None
        and not inconclusive
        and all(item["passed"] is True for item in checks),
        "fatal_error_class": fatal,
        "checks": checks,
        "inconclusive": inconclusive,
        "unavailable_witnesses": unavailable,
        "requests": len(calls),
        "source_selects": source.requests,
        "source_bytes": source.bytes,
        "source_admin_bytes": http.received_bytes,
        "responses": probe.rows,
        "operations": calls,
        "wall_seconds": round(time.monotonic() - probe.started, 3),
        **summary,
        "backend_writes": 0,
        "source_writes": 0,
        "known_handle_witnesses_sha256": digest(known_handles)
        if known_handles
        else None,
        "full_document_hash_equivalence_verified": False,
        "cursor_refreshed": False,
        "causal_updates_verified": False,
        "preference_rank_effect_verified": False,
        "full_topk_oracle": False,
        "promotion_permitted": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--input", type=Path, default=Path("/app/input"))
    parser.add_argument("--known-handles", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("New receipt required")
    known = {}
    if args.known_handles:
        with args.known_handles.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Known handle metadata exceeds bound")
        known = json.loads(raw)
    report = run(
        args.candidate,
        args.build_id,
        args.binary_sha256,
        dict(os.environ),
        input_path=args.input,
        known_handles=known,
    )
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(report, output, sort_keys=True)
        output.write("\n")
    print(
        json.dumps(
            {
                key: report.get(key)
                for key in (
                    "passed",
                    "fatal_error_class",
                    "requests",
                    "source_selects",
                    "present_witnesses",
                    "source_backed_pds_pairs",
                    "moderated_absence_witnesses",
                    "deleted_absence_witnesses",
                    "wall_seconds",
                )
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
