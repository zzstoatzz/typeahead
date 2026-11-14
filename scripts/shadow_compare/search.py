"""Run one query set against both backends and compare ordered results."""

import json
import math
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import bench

CASES_FILE = Path(__file__).resolve().with_name("search-cases.json")
ACK_HEADERS = ("x-typeahead-pds", "x-typeahead-prefer-pds", "x-typeahead-preference", "x-typeahead-boost")
IDENTITY_HEADERS = ("x-typeahead-build", "x-typeahead-format", "x-typeahead-binary-sha256", "x-typeahead-retrieval-strategy")
SCOPE_PARAMS = ("pds", "prefer_pds", "preference", "boost")
PUBLIC_PARAMS = ("q", "limit", "pds")
PUBLIC_PATH = "/xrpc/tech.waow.typeahead.searchActors"
ORACLE_PATH = "/xrpc/app.bsky.actor.searchActorsTypeahead"
SPAM_GUARD_QUERIES = ("alex", "nate", "bob")
SERVER_TIMING = re.compile(r"(?:^|,)\s*search;dur=([0-9.]+)")


def canonical_cases():
    seen, cases = set(), []
    for q in [*bench.RANKING_CORPUS, *bench.FULL_CORPUS, *bench.DISPLAY_NAME_QUERIES]:
        if q in seen:
            continue
        seen.add(q)
        cases.append({"id": "q:" + q, "params": {"q": q, "limit": 10}, "expect": {"status": 200}, "bench": True})
    return cases


def recorded_cases(path=CASES_FILE):
    cases = []
    for entry in json.loads(Path(path).read_text()):
        scoped = any(name in entry["params"] for name in SCOPE_PARAMS)
        expect = {
            key: entry[key]
            for key in ("status", "headers", "first_handle", "contains_handle", "contains_did", "require_nonempty")
            if key in entry
        }
        cases.append({"id": ("scope:" if scoped else "recorded:") + entry["id"], "params": entry["params"], "expect": expect})
    return cases


def extra_cases(path):
    cases = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        params = json.loads(line) if line.startswith("{") else {"q": line, "limit": 10}
        cases.append({"id": f"extra:{number}", "params": params, "expect": {"status": 200}})
    return cases


def server_ms(headers):
    match = SERVER_TIMING.search(headers.get("server-timing", ""))
    return float(match.group(1)) if match else None


def observe(client, case, path="/search"):
    reply = client.get(path, case["params"])
    actors, shape = [], None
    if reply.status == 200:
        try:
            actors = reply.json()["actors"]
            if not isinstance(actors, list) or any(
                not isinstance(a, dict) or not isinstance(a.get("did"), str) or not isinstance(a.get("handle"), str)
                for a in actors
            ):
                actors, shape = [], "actors is not a list of objects with did and handle"
        except (ValueError, KeyError, TypeError):
            shape = "body is not JSON with an actors list"
    return {
        "status": reply.status,
        "actors": actors,
        "dids": [a["did"] for a in actors],
        "headers": {name: reply.headers[name] for name in (*ACK_HEADERS, *IDENTITY_HEADERS) if name in reply.headers},
        "client_ms": reply.elapsed_ms,
        "server_ms": server_ms(reply.headers),
        "shape_error": shape,
    }


def case_checks(case, seen):
    expect, failures = case["expect"], []
    if seen["shape_error"]:
        failures.append("shape: " + seen["shape_error"])
    if seen["status"] != expect.get("status", 200):
        failures.append(f"status {seen['status']}, expected {expect.get('status', 200)}")
        return failures
    for name, value in expect.get("headers", {}).items():
        if seen["headers"].get(name) != value:
            failures.append(f"header {name} is {seen['headers'].get(name)!r}, expected {value!r}")
    if seen["status"] == 200 and "pds" in case["params"] and not seen["headers"].get("x-typeahead-pds"):
        failures.append("PDS-scoped answer lacks the X-Typeahead-Pds acknowledgment")
    handles = [a["handle"].lower() for a in seen["actors"]]
    if "first_handle" in expect and handles[:1] != [expect["first_handle"]]:
        failures.append(f"first handle is {handles[:1]}, expected {expect['first_handle']}")
    if "contains_handle" in expect and expect["contains_handle"] not in handles:
        failures.append(f"handle {expect['contains_handle']} missing")
    if "contains_did" in expect and expect["contains_did"] not in seen["dids"]:
        failures.append(f"DID {expect['contains_did']} missing")
    if expect.get("require_nonempty") and not seen["actors"]:
        failures.append("empty result")
    return failures


def ranking_result(case, seen, oracle):
    q = case["params"]["q"]
    return bench.RankingResult(
        query=q,
        bsky_actors=oracle.get(q, []),
        ours_actors=seen["actors"],
        debug=None,
        bsky_status=200 if q in oracle else 0,
        ours_status=seen["status"],
        ours_ms=seen["client_ms"],
    )


def bench_checks(cases, observations, oracle):
    """bench.py's structural acceptance checks, attributed to the query each names."""
    results = {c["id"]: ranking_result(c, observations[c["id"]], oracle) for c in cases if c.get("bench")}
    fig = next((r for r in results.values() if r.query == "fig"), None)
    failures = {}
    for case_id, result in results.items():
        group = [result] + ([fig] if result.query == "fig (" and fig else [])
        marker = f"q={result.query!r}"
        failures[case_id] = [
            f for f in bench.acceptance_checks(group) if f.startswith("[structural]") and marker in f
        ]
    return failures


def compare(production, shadow):
    a, b = production["dids"], shadow["dids"]
    first = next((i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
    if first is None and len(a) != len(b):
        first = min(len(a), len(b)) + 1
    union = set(a) | set(b)
    by_did = {actor["did"]: actor for actor in production["actors"]}
    changed_bodies = sorted(
        actor["did"] for actor in shadow["actors"] if actor["did"] in by_did and by_did[actor["did"]] != actor
    )
    ack_equal = all(production["headers"].get(h) == shadow["headers"].get(h) for h in ACK_HEADERS)
    return {
        "status_equal": production["status"] == shadow["status"],
        "ordered_dids_identical": a == b,
        "first_difference_rank": first,
        "overlap": len(set(a) & set(b)) / len(union) if union else 1.0,
        "body_differs_for": changed_bodies,
        "acknowledgments_equal": ack_equal,
        "identical": production["status"] == shadow["status"] and a == b and not changed_bodies and ack_equal,
    }


def trimmed(seen, failures):
    return {
        "status": seen["status"],
        "dids": seen["dids"],
        "handles": [a["handle"] for a in seen["actors"]],
        "headers": seen["headers"],
        "check_failures": failures,
    }


def run(production, shadow, cases, *, flagged=(), oracle_client=None, public_client=None, settle_delay=30, sleep=time.sleep):
    flagged = set(flagged)
    seen = {"production": {}, "shadow": {}}
    for case in cases:
        seen["production"][case["id"]] = observe(production, case)
        seen["shadow"][case["id"]] = observe(shadow, case)
    differing = [c for c in cases if not compare(seen["production"][c["id"]], seen["shadow"][c["id"]])["identical"]]
    if differing:
        sleep(settle_delay)
        for case in differing:
            seen["production"][case["id"]] = observe(production, case)
            seen["shadow"][case["id"]] = observe(shadow, case)
    oracle = {}
    if oracle_client is not None:
        for q in SPAM_GUARD_QUERIES:
            reply = oracle_client.get(ORACLE_PATH, {"q": q, "limit": 10})
            if reply.status == 200:
                oracle[q] = reply.json().get("actors", [])
    structural = {side: bench_checks(cases, seen[side], oracle) for side in seen}
    rows = []
    for case in cases:
        a, b = seen["production"][case["id"]], seen["shadow"][case["id"]]
        row = {"id": case["id"], "params": case["params"], "rechecked": case in differing}
        row.update(compare(a, b))
        row["production"] = trimmed(a, case_checks(case, a) + structural["production"].get(case["id"], []))
        row["shadow"] = trimmed(b, case_checks(case, b) + structural["shadow"].get(case["id"], []))
        row["explained_by"] = sorted(flagged & (set(a["dids"]) | set(b["dids"])))
        if public_client is not None and a["status"] == 200 and set(case["params"]) <= set(PUBLIC_PARAMS):
            public = observe(public_client, case, PUBLIC_PATH)
            row["public"] = {"status": public["status"], "dids": public["dids"], "matches_production": public["dids"] == a["dids"]}
        rows.append(row)
    return {
        "cases": rows,
        "queries": len(rows),
        "identical": sum(row["identical"] for row in rows),
        "differing_explained": sum(not row["identical"] and bool(row["explained_by"]) for row in rows),
        "differing_unexplained": sum(not row["identical"] and not row["explained_by"] for row in rows),
        "shadow_check_failures": sum(len(row["shadow"]["check_failures"]) for row in rows),
        "production_check_failures": sum(len(row["production"]["check_failures"]) for row in rows),
        "flagged_dids_known": len(flagged),
        "spam_guard_evaluated": sorted(oracle),
        "settle_delay_s": settle_delay if differing else 0,
    }


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def distribution(values):
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values) if values else None,
    }


DEBUG_FIELDS = ("hydration_ms", "moderation_ms", "visited_candidates", "hydrated_candidates", "segments_visited", "retrieval_strategy")


def latency(production, shadow, cases, *, warm_runs=5, debug=False):
    """Sequential cold and warm timings; the order of the two backends alternates per query."""
    clients = {"production": production, "shadow": shadow}
    samples = {side: {"cold": {"client_ms": [], "server_ms": []}, "warm": {"client_ms": [], "server_ms": []}} for side in clients}
    per_query, errors, timed = [], 0, True
    cases = [c for c in cases if c["expect"].get("status", 200) == 200]
    for index, case in enumerate(cases):
        order = ("production", "shadow") if index % 2 == 0 else ("shadow", "production")
        row = {"id": case["id"], "order": list(order)}
        runs = {side: [] for side in clients}
        for round_number in range(1 + warm_runs):
            for side in order:
                seen = observe(clients[side], case)
                if seen["status"] != 200:
                    errors += 1
                    continue
                if seen["server_ms"] is None:
                    timed = False
                runs[side].append(seen)
                phase = "cold" if round_number == 0 else "warm"
                samples[side][phase]["client_ms"].append(seen["client_ms"])
                if seen["server_ms"] is not None:
                    samples[side][phase]["server_ms"].append(seen["server_ms"])
        for side in clients:
            warm = runs[side][1:]
            row[side] = {
                "cold_client_ms": runs[side][0]["client_ms"] if runs[side] else None,
                "cold_server_ms": runs[side][0]["server_ms"] if runs[side] else None,
                "warm_client_ms": statistics.median(s["client_ms"] for s in warm) if warm else None,
                "warm_server_ms": statistics.median(s["server_ms"] for s in warm)
                if warm and all(s["server_ms"] is not None for s in warm)
                else None,
            }
            if debug:
                reply = clients[side].get("/debug/search", case["params"])
                row[side]["debug"] = (
                    {name: reply.json().get(name) for name in DEBUG_FIELDS} if reply.status == 200 else {"status": reply.status}
                )
        row["paired"] = {
            name: row["shadow"][name] - row["production"][name]
            if row["shadow"][name] is not None and row["production"][name] is not None
            else None
            for name in ("warm_client_ms", "warm_server_ms")
        }
        per_query.append(row)
    paired = {}
    for name in ("warm_client_ms", "warm_server_ms"):
        diffs = [row["paired"][name] for row in per_query if row["paired"][name] is not None]
        paired[name] = {
            "n": len(diffs),
            "median_shadow_minus_production": statistics.median(diffs) if diffs else None,
            "max_shadow_minus_production": max(diffs) if diffs else None,
            "queries_shadow_slower": sum(d > 0 for d in diffs),
        }
    return {
        "queries": len(cases),
        "warm_runs": warm_runs,
        "errors": errors,
        "gate_basis": "server_ms" if timed and not errors else "client_ms",
        "summary": {
            side: {phase: {name: distribution(values) for name, values in group.items()} for phase, group in phases.items()}
            for side, phases in samples.items()
        },
        "paired": paired,
        "per_query": per_query,
        "controlled": [
            "the same queries, parameters and limits on both backends",
            "one request at a time with a fixed delay; no concurrency",
            "the order of the two backends alternates per query",
            "server_ms is the backend's own Server-Timing search duration and excludes the network",
        ],
        "not_controlled": [
            "client_ms includes network distance and TLS from the caller, which differs per backend unless both are reached from the same region and through the same kind of hop",
            "cold means the first request for a query in this run, not a cold process or a cold page cache",
            "hardware, co-tenant load and concurrent production traffic on each backend",
            "index generation, segment count and corpus differences between the backends",
            "with fewer than 100 samples p99 is the largest sample",
        ],
    }
