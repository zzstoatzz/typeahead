"""Gate thresholds and their evaluation. The README documents this table."""

import arbiter
import corpus

ARBITER_SAMPLE_SIZE = 25
ARBITER_PASSING_LABELS = (arbiter.SHADOW_RIGHT, arbiter.BOTH_STALE)
ARBITER_JUDGED_CLASSES = (corpus.ONLY_IN_PRODUCTION, corpus.ONLY_IN_SHADOW, corpus.HANDLE_DIFFERS, corpus.PDS_DIFFERS)
SETTLE_DELAY_S = 120
WATERMARK_SKEW_S = 120
MAX_SHADOW_CHANGED_SHARE = 0.25
MAX_TRACKED_DIFFERENCES = 50_000
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


def unexplained(classes, arbiter_ran, prefix=""):
    """Reasons for every class with differences that the arbiter did not label in the shadow's favour."""
    reasons = []
    for name, entry in classes.items():
        if entry["count"] == 0:
            continue
        if not arbiter_ran:
            reasons.append(f"{prefix}{name}: {entry['count']} differences and no arbiter check")
            continue
        failing = {
            label: count
            for label, count in entry.get("arbiter_labels", {}).items()
            if label not in ARBITER_PASSING_LABELS
        }
        if failing:
            detail = ", ".join(f"{count} {label}" for label, count in sorted(failing.items()))
            reasons.append(f"{prefix}{name}: {entry['count']} differences, sample of {len(entry['sample'])} has {detail}")
    return reasons


def shadow_wrong(classes, arbiter_ran, prefix):
    """Reasons for every class whose sample shows the shadow wrong, or unjudged where the arbiter can judge."""
    reasons = []
    for name, entry in classes.items():
        if entry["count"] == 0:
            continue
        if not arbiter_ran:
            reasons.append(f"{prefix}{name}: {entry['count']} differences and no arbiter sample")
            continue
        labels = entry.get("arbiter_labels", {})
        failing = {arbiter.PRODUCTION_RIGHT: labels.get(arbiter.PRODUCTION_RIGHT, 0)}
        if name in ARBITER_JUDGED_CLASSES:
            failing[arbiter.UNDETERMINED] = labels.get(arbiter.UNDETERMINED, 0)
        detail = ", ".join(f"{count} {label}" for label, count in sorted(failing.items()) if count)
        if detail:
            reasons.append(f"{prefix}{name}: {entry['count']} differences, sample of {len(entry['sample'])} has {detail}")
    return reasons


def corpus_gate(result):
    reasons = []
    if not result["complete"]:
        reasons.append(f"walk incomplete ({result['stop_reason']}) at cursor {result['cursor']!r}")
    if not result.get("rechecked"):
        reasons.append("differences were not re-read after the settle delay")
    if result["untracked_differences"]:
        reasons.append(f"{result['untracked_differences']} differences exceeded the tracking cap and were not re-read")
    ran = result.get("arbiter_ran")
    if "snapshot" not in result:
        return gate(reasons + unexplained(result["classes"], ran))
    shadow = result["snapshot"]["shadow"]
    if shadow["records"] and shadow["changed_since_cutoff"] > MAX_SHADOW_CHANGED_SHARE * shadow["records"]:
        reasons.append(
            f"{shadow['changed_since_cutoff']} of {shadow['records']} shadow records changed after the snapshot cutoff, "
            f"more than {MAX_SHADOW_CHANGED_SHARE:.0%}: the snapshot is too old or the change times are not content changes"
        )
    if "timing" in result:
        parts = result["timing"]
        reasons += unexplained(parts[corpus.UNCHANGED_IN_SHADOW]["classes"], ran, "unchanged in shadow since the watermark, ")
        reasons += shadow_wrong(parts[corpus.NEWER_IN_SHADOW]["classes"], ran, "newer in shadow, ")
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
