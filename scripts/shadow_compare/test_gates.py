import unittest

import arbiter
import corpus
import gates


def corpus_result(arbiter_ran=True, **classes):
    """A complete walk whose classes hold `(count, labels)`, or for a refresh class `(count, refreshed labels, lagging labels)`."""
    result = {
        "complete": True, "stop_reason": "complete", "cursor": "", "rechecked": True, "arbiter_ran": arbiter_ran,
        "identity_differences": {"tracked": 0, "untracked": 0, "cap": 100},
        "notes": {name: {"count": 0, "sample": []} for name in corpus.NOTES},
        "classes": {},
    }
    for name in corpus.CLASSES:
        count, *labels = classes.get(name, (0, {}))
        level = "identity" if name in corpus.IDENTITY_CLASSES else "refresh"
        entry = {"level": level, "count": count, "sample": [{}] * sum(sum(tally.values()) for tally in labels)}
        entry["arbiter_labels"] = {label: sum(tally.get(label, 0) for tally in labels) for tally in labels for label in tally}
        if level == "refresh":
            refreshed, lagging = (*labels, {}, {})[:2]
            entry["strata"] = {
                corpus.REFRESHED: {"count": count, "sampled": sum(refreshed.values()), "arbiter_labels": refreshed},
                corpus.LAGGING: {"count": 0, "sampled": sum(lagging.values()), "arbiter_labels": lagging},
            }
        result["classes"][name] = entry
    return result


def distribution(p50, p95, p99):
    return {"n": 100, "p50": p50, "p95": p95, "p99": p99, "max": p99}


def latency_result(production, shadow, basis="server_ms", queries=50, errors=0):
    def side(values):
        return {phase: {"server_ms": distribution(*values), "client_ms": distribution(*values)} for phase in ("cold", "warm")}

    return {"queries": queries, "errors": errors, "gate_basis": basis, "summary": {"production": side(production), "shadow": side(shadow)}}


def freshness_result(lag=5, ready=True, fresh=True, matched=True, samples=3):
    one = {"ready": ready, "sync_lag_s": lag, "moderation_fresh": fresh}
    return {
        "samples": {"production": [dict(one)] * samples, "shadow": [dict(one)] * samples},
        "must_match": ["plan_version"],
        "agreement": {"plan_version": {"matched": matched, "production": "a", "shadow": "a" if matched else "b"}},
    }


def search_result(*cases):
    return {"cases": [
        {"id": f"q:{n}", "identical": identical, "explained_by": explained, "first_difference_rank": 2,
         "shadow": {"check_failures": failures}}
        for n, (identical, explained, failures) in enumerate(cases)
    ]}


class CorpusGate(unittest.TestCase):
    def test_no_differences_passes(self):
        self.assertEqual(gates.corpus_gate(corpus_result()), {"status": gates.PASS, "reasons": []})
        self.assertEqual(gates.corpus_gate(corpus_result(arbiter_ran=False))["status"], gates.PASS)

    def test_differences_without_an_arbiter_fail(self):
        for name in (corpus.HANDLE_DIFFERS, corpus.QUALITY_SCORE_DIFFERS):
            verdict = gates.corpus_gate(corpus_result(arbiter_ran=False, **{name: (3, {})}))
            self.assertEqual(verdict["status"], gates.FAIL)
            self.assertIn("no arbiter check", verdict["reasons"][0])

    def test_an_identity_sample_where_production_is_never_right_passes(self):
        result = corpus_result(handle_differs=(900, {arbiter.SHADOW_RIGHT: 140, arbiter.BOTH_STALE: 10}), visibility_differs=(4, {arbiter.UNDETERMINED: 4}))
        self.assertEqual(gates.corpus_gate(result)["status"], gates.PASS)

    def test_one_identity_difference_where_production_is_right_fails(self):
        for name in corpus.IDENTITY_CLASSES:
            verdict = gates.corpus_gate(corpus_result(**{name: (900, {arbiter.SHADOW_RIGHT: 149, arbiter.PRODUCTION_RIGHT: 1})}))
            self.assertEqual(verdict["status"], gates.FAIL, name)
            self.assertIn(f"{name}: 900 differences, sample of 150 has 1 production is right", verdict["reasons"][0])

    def test_presence_handle_and_pds_may_not_stay_undetermined(self):
        for name in gates.ARBITER_MUST_JUDGE:
            verdict = gates.corpus_gate(corpus_result(**{name: (5, {arbiter.SHADOW_RIGHT: 4, arbiter.UNDETERMINED: 1})}))
            self.assertEqual(verdict["status"], gates.FAIL, name)
            self.assertIn("1 undetermined", verdict["reasons"][0])
        self.assertEqual(set(corpus.IDENTITY_CLASSES) - set(gates.ARBITER_MUST_JUDGE), {corpus.VISIBILITY_DIFFERS})

    def test_refresh_differences_pass_however_many_when_the_shadow_is_not_wrong(self):
        result = corpus_result(
            quality_score_differs=(650_000, {arbiter.SHADOW_RIGHT: 280, arbiter.BOTH_STALE: 20}, {arbiter.PRODUCTION_RIGHT: 290, arbiter.BOTH_STALE: 10}),
            associated_differs=(260_000, {arbiter.SHADOW_RIGHT: 200, arbiter.UNDETERMINED: 100}, {}),
            profile_differs=(9, {arbiter.UNDETERMINED: 9}, {}),
        )
        self.assertEqual(gates.corpus_gate(result), {"status": gates.PASS, "reasons": []})

    def test_production_right_on_refreshed_records_fails_only_above_the_allowed_share(self):
        allowed = corpus_result(quality_score_differs=(5000, {arbiter.SHADOW_RIGHT: 294, arbiter.PRODUCTION_RIGHT: 6}, {}))
        self.assertEqual(gates.corpus_gate(allowed)["status"], gates.PASS)
        over = corpus_result(quality_score_differs=(5000, {arbiter.SHADOW_RIGHT: 293, arbiter.PRODUCTION_RIGHT: 7}, {arbiter.PRODUCTION_RIGHT: 300}))
        verdict = gates.corpus_gate(over)
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertEqual(verdict["reasons"], ["quality_score_differs: production is right on 7 of 300 sampled records the shadow refreshed after the watermark, more than 2%"])
        self.assertEqual(gates.corpus_gate(corpus_result(labels_differ=(1, {arbiter.PRODUCTION_RIGHT: 1}, {})))["status"], gates.FAIL)

    def test_incomplete_unrechecked_truncated_or_unjudgeable_walks_fail(self):
        changes = (
            {"complete": False, "stop_reason": "budget"}, {"rechecked": False},
            {"identity_differences": {"tracked": 100, "untracked": 4, "cap": 100}},
            {"arbiter_failures": ["getProfiles returned 502"]},
        )
        for change in changes:
            result = corpus_result()
            result.update(change)
            self.assertEqual(gates.corpus_gate(result)["status"], gates.FAIL, change)
        result = corpus_result()
        result["notes"][corpus.VISIBILITY_UNEVALUATED]["count"] = 2
        self.assertIn("2 actors hold labels the server refuses to evaluate", gates.corpus_gate(result)["reasons"][0])
        result = corpus_result()
        result["notes"][corpus.STORED_HIDDEN_DIFFERS]["count"] = 900
        self.assertEqual(gates.corpus_gate(result)["status"], gates.PASS)


class SearchGate(unittest.TestCase):
    def test_identical_and_explained_pass(self):
        self.assertEqual(gates.search_gate(search_result((True, [], []), (False, ["did:plc:x"], [])))["status"], gates.PASS)

    def test_unexplained_difference_fails(self):
        verdict = gates.search_gate(search_result((False, [], [])))
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertIn("rank 2", verdict["reasons"][0])

    def test_structural_failure_on_shadow_fails_even_when_identical(self):
        self.assertEqual(gates.search_gate(search_result((True, [], ["[structural] x"])))["status"], gates.FAIL)

    def test_no_queries_fails(self):
        self.assertEqual(gates.search_gate(search_result())["status"], gates.FAIL)


class LatencyGate(unittest.TestCase):
    def test_equal_and_within_noise_pass(self):
        self.assertEqual(gates.latency_gate(latency_result((10, 20, 30), (10, 20, 30)))["status"], gates.PASS)
        allowed = gates.latency_allowed(10)
        self.assertEqual(allowed, 10 * (1 + gates.LATENCY_NOISE_RATIO) + gates.LATENCY_NOISE_FLOOR_MS)
        self.assertEqual(gates.latency_gate(latency_result((10, 20, 30), (allowed, 20, 30)))["status"], gates.PASS)

    def test_each_percentile_beyond_noise_fails(self):
        for index in range(3):
            shadow = [10, 20, 30]
            shadow[index] = gates.latency_allowed(shadow[index]) + 0.01
            verdict = gates.latency_gate(latency_result((10, 20, 30), tuple(shadow)))
            self.assertEqual(verdict["status"], gates.FAIL)
            self.assertEqual(len(verdict["reasons"]), 2)
            self.assertIn(gates.LATENCY_PERCENTILES[index], verdict["reasons"][0])

    def test_errors_and_small_samples_fail(self):
        self.assertEqual(gates.latency_gate(latency_result((1, 1, 1), (1, 1, 1), errors=1))["status"], gates.FAIL)
        few = latency_result((1, 1, 1), (1, 1, 1), queries=gates.LATENCY_MIN_QUERIES - 1)
        self.assertEqual(gates.latency_gate(few)["status"], gates.FAIL)


class FreshnessGate(unittest.TestCase):
    def test_ready_fresh_and_matching_pass(self):
        self.assertEqual(gates.freshness_gate(freshness_result())["status"], gates.PASS)
        self.assertEqual(gates.freshness_gate(freshness_result(lag=gates.FRESH_MAX_SYNC_LAG_S))["status"], gates.PASS)

    def test_each_requirement_fails_alone(self):
        for change in (
            {"lag": gates.FRESH_MAX_SYNC_LAG_S + 1}, {"lag": None}, {"ready": False}, {"fresh": False},
            {"matched": False}, {"samples": gates.FRESH_MIN_SAMPLES - 1},
        ):
            self.assertEqual(gates.freshness_gate(freshness_result(**change))["status"], gates.FAIL, change)


class Overall(unittest.TestCase):
    def test_all_four_must_run_and_pass(self):
        report = {"corpus": corpus_result(), "search": search_result((True, [], [])),
                  "latency": latency_result((1, 2, 3), (1, 2, 3)), "freshness": freshness_result()}
        self.assertEqual(gates.evaluate(report)["overall"], gates.PASS)
        partial = dict(report)
        del partial["latency"]
        verdict = gates.evaluate(partial)
        self.assertEqual(verdict["overall"], "INCOMPLETE")
        self.assertEqual(verdict["gates"]["latency"]["status"], gates.NOT_RUN)
        report["freshness"] = freshness_result(ready=False)
        del report["latency"]
        self.assertEqual(gates.evaluate(report)["overall"], gates.FAIL)


if __name__ == "__main__":
    unittest.main()
