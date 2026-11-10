import unittest

import arbiter
import corpus
import gates


def corpus_result(**classes):
    result = {
        "complete": True, "stop_reason": "complete", "cursor": "", "rechecked": True, "untracked_differences": 0,
        "arbiter_ran": False,
        "classes": {name: {"count": 0, "sample": []} for name in corpus.CLASSES},
    }
    for name, (count, labels) in classes.items():
        result["classes"][name] = {"count": count, "sample": [{}] * sum(labels.values()), "arbiter_labels": labels}
        result["arbiter_ran"] = result["arbiter_ran"] or bool(labels)
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
        self.assertEqual(gates.corpus_gate(corpus_result())["status"], gates.PASS)

    def test_differences_without_an_arbiter_fail(self):
        verdict = gates.corpus_gate(corpus_result(handle_differs=(3, {})))
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertIn("no arbiter check", verdict["reasons"][0])

    def test_sample_where_shadow_is_never_wrong_passes(self):
        result = corpus_result(handle_differs=(40, {arbiter.SHADOW_RIGHT: 20, arbiter.BOTH_STALE: 5}))
        self.assertEqual(gates.corpus_gate(result)["status"], gates.PASS)

    def test_production_right_or_undetermined_fails(self):
        for label in (arbiter.PRODUCTION_RIGHT, arbiter.UNDETERMINED):
            result = corpus_result(pds_differs=(2, {arbiter.SHADOW_RIGHT: 1, label: 1}))
            verdict = gates.corpus_gate(result)
            self.assertEqual(verdict["status"], gates.FAIL)
            self.assertIn(label, verdict["reasons"][0])

    def test_incomplete_unrechecked_or_truncated_walks_fail(self):
        for change in ({"complete": False, "stop_reason": "budget"}, {"rechecked": False}, {"untracked_differences": 4}):
            result = corpus_result()
            result.update(change)
            self.assertEqual(gates.corpus_gate(result)["status"], gates.FAIL, change)


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
