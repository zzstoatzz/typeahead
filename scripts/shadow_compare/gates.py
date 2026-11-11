"""Gate thresholds and their evaluation. The README documents this table."""

import arbiter
import corpus

IDENTITY_SAMPLE_SIZE = 150
REFRESH_SAMPLE_SIZE = 300
MAX_TRACKED_IDENTITY_DIFFERENCES = 20_000
ARBITER_MUST_JUDGE = (corpus.ONLY_IN_PRODUCTION, corpus.ONLY_IN_SHADOW, corpus.HANDLE_DIFFERS, corpus.PDS_DIFFERS, corpus.DOCUMENT_HASH_DIFFERS)
MAX_REFRESHED_PRODUCTION_RIGHT_SHARE = 0.02
SCORE_TOLERANCE = arbiter.SCORE_TOLERANCE
SETTLE_DELAY_S = 120
LATENCY_NOISE_RATIO = 0.10
LATENCY_NOISE_FLOOR_MS = 2.0
LATENCY_PERCENTILES = ("p50", "p95", "p99")
LATENCY_MIN_QUERIES = 20
FRESH_MAX_SYNC_LAG_S = 120
FRESH_MIN_SAMPLES = 3

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"
GATES = ("corpus", "search", "latency", "freshness")


def gate(reasons):
    return {"status": FAIL if reasons else PASS, "reasons": reasons}


def identity_reasons(name, entry):
    """Why an identity-level class fails: the sample shows production right, or leaves open what the authorities can settle."""
    labels = entry.get("arbiter_labels", {})
    failing = {arbiter.PRODUCTION_RIGHT: labels.get(arbiter.PRODUCTION_RIGHT, 0)}
    if name in ARBITER_MUST_JUDGE:
        failing[arbiter.UNDETERMINED] = labels.get(arbiter.UNDETERMINED, 0)
    detail = ", ".join(f"{count} {label}" for label, count in sorted(failing.items()) if count)
    return [f"{name}: {entry['count']} differences, sample of {len(entry['sample'])} has {detail}"] if detail else []


def refresh_reasons(name, entry):
    """Why a refresh-derived class fails: production is right too often on records the shadow refreshed after the watermark."""
    stratum = entry["strata"][corpus.REFRESHED]
    judged = stratum["sampled"]
    wrong = stratum.get("arbiter_labels", {}).get(arbiter.PRODUCTION_RIGHT, 0)
    if not judged or wrong <= MAX_REFRESHED_PRODUCTION_RIGHT_SHARE * judged:
        return []
    return [
        f"{name}: production is right on {wrong} of {judged} sampled records the shadow refreshed after the watermark, "
        f"more than {MAX_REFRESHED_PRODUCTION_RIGHT_SHARE:.0%}"
    ]


def corpus_gate(result):
    reasons = []
    if not result["complete"]:
        reasons.append(f"walk incomplete ({result['stop_reason']}) at cursor {result['cursor']!r}")
    if not result.get("rechecked"):
        reasons.append("kept differences were not re-read after the settle delay")
    untracked = result["identity_differences"]["untracked"]
    if untracked:
        reasons.append(f"{untracked} identity-level differences exceeded the tracking cap of {result['identity_differences']['cap']} and were not examined")
    unevaluated = result["notes"][corpus.VISIBILITY_UNEVALUATED]["count"]
    if unevaluated:
        reasons.append(f"{unevaluated} actors hold labels the server refuses to evaluate, so what a search returns for them is unknown")
    ran = result.get("arbiter_ran")
    failures = result.get("arbiter_failures") or []
    if failures:
        reasons.append(f"the appview could not be read for {len(failures)} batches of the sample: {failures[0]}")
    for name, entry in result["classes"].items():
        if entry["count"] == 0:
            continue
        if not ran:
            reasons.append(f"{name}: {entry['count']} differences and no arbiter check")
        elif entry["level"] == "identity":
            reasons += identity_reasons(name, entry)
        else:
            reasons += refresh_reasons(name, entry)
    return gate(reasons)


def search_gate(result):
    reasons = []
    for case in result["cases"]:
        for failure in case["shadow"]["check_failures"]:
            reasons.append(f"{case['id']}: shadow fails {failure}")
        if not case["identical"] and not case["explained_by"]:
            reasons.append(f"{case['id']}: shadow differs from production at rank {case['first_difference_rank']} with no flagged DID in either result")
    if not result["cases"]:
        reasons.append("no queries ran")
    return gate(reasons)


def latency_allowed(production_ms):
    return production_ms * (1 + LATENCY_NOISE_RATIO) + LATENCY_NOISE_FLOOR_MS


def latency_gate(result):
    reasons = []
    if result["queries"] < LATENCY_MIN_QUERIES:
        reasons.append(f"{result['queries']} queries measured, fewer than {LATENCY_MIN_QUERIES}")
    if result["errors"]:
        reasons.append(f"{result['errors']} requests did not return 200")
    basis = result["gate_basis"]
    for phase in ("cold", "warm"):
        production = result["summary"]["production"][phase][basis]
        shadow = result["summary"]["shadow"][phase][basis]
        for name in LATENCY_PERCENTILES:
            if production[name] is None or shadow[name] is None:
                reasons.append(f"{phase} {name}: no samples")
            elif shadow[name] > latency_allowed(production[name]):
                reasons.append(
                    f"{phase} {basis} {name}: shadow {shadow[name]:.2f} ms exceeds "
                    f"{latency_allowed(production[name]):.2f} ms allowed over production {production[name]:.2f} ms"
                )
    return gate(reasons)


def freshness_gate(result):
    reasons = []
    for side in ("production", "shadow"):
        samples = result["samples"][side]
        if len(samples) < FRESH_MIN_SAMPLES:
            reasons.append(f"{side}: {len(samples)} samples, fewer than {FRESH_MIN_SAMPLES}")
        for index, sample in enumerate(samples):
            if not sample["ready"]:
                reasons.append(f"{side}: not ready at sample {index}")
            lag = sample["sync_lag_s"]
            if lag is None or lag > FRESH_MAX_SYNC_LAG_S:
                reasons.append(f"{side}: sync lag {lag} s exceeds {FRESH_MAX_SYNC_LAG_S} s at sample {index}")
            if not sample["moderation_fresh"]:
                reasons.append(f"{side}: moderation policy not fresh at sample {index}")
    for name in result["must_match"]:
        values = result["agreement"][name]
        if not values["matched"]:
            reasons.append(f"{name} never matched: production {values['production']!r}, shadow {values['shadow']!r}")
    return gate(reasons)


EVALUATORS = {"corpus": corpus_gate, "search": search_gate, "latency": latency_gate, "freshness": freshness_gate}


def evaluate(report):
    verdicts = {}
    for name in GATES:
        result = report.get(name)
        verdicts[name] = EVALUATORS[name](result) if result is not None else {"status": NOT_RUN, "reasons": []}
    statuses = {entry["status"] for entry in verdicts.values()}
    overall = FAIL if FAIL in statuses else "INCOMPLETE" if NOT_RUN in statuses else PASS
    return {"overall": overall, "gates": verdicts}
