import unittest

import httpx

import search
from transport import Budget, Client


def backend(answers, timing="search;dur=1.500", status=None):
    def handler(request):
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200 if (status or {}).get("ready", True) else 503, json={})
        if path == "/index/status":
            return httpx.Response(200, json=status)
        q = request.url.params["q"]
        if q not in answers:
            return httpx.Response(400, json={"error": "invalid"})
        actors = [{"did": "did:plc:" + name, "handle": name + ".test"} for name in answers[q]]
        headers = {"server-timing": timing} if timing else {}
        if "pds" in request.url.params:
            headers["x-typeahead-pds"] = request.url.params["pds"]
        return httpx.Response(200, json={"actors": actors}, headers=headers)

    return Client("b", "http://127.0.0.1:1", Budget(10**4, 10**8), retries=0, http=httpx.Client(transport=httpx.MockTransport(handler)))


def case(q, **expect):
    return {"id": "q:" + q, "params": {"q": q, "limit": 10}, "expect": {"status": 200, **expect}, "bench": True}


def observation(names):
    actors = [{"did": "did:plc:" + n, "handle": n + ".test"} for n in names]
    return {"status": 200, "actors": actors, "dids": [a["did"] for a in actors], "headers": {}}


class CompareTests(unittest.TestCase):
    def test_identical_lists(self):
        result = search.compare(observation("abc"), observation("abc"))
        self.assertTrue(result["identical"])
        self.assertIsNone(result["first_difference_rank"])
        self.assertEqual(result["overlap"], 1.0)

    def test_first_differing_rank_and_overlap(self):
        result = search.compare(observation("abcd"), observation("abdc"))
        self.assertEqual(result["first_difference_rank"], 3)
        self.assertEqual(result["overlap"], 1.0)
        self.assertFalse(result["identical"])
        result = search.compare(observation("abc"), observation("abx"))
        self.assertEqual((result["first_difference_rank"], result["overlap"]), (3, 0.5))

    def test_a_shorter_list_differs_at_the_first_missing_rank(self):
        self.assertEqual(search.compare(observation("abc"), observation("ab"))["first_difference_rank"], 3)
        self.assertEqual(search.compare(observation(""), observation(""))["overlap"], 1.0)

    def test_same_order_with_a_changed_actor_body_is_not_identical(self):
        changed = observation("ab")
        changed["actors"][1]["displayName"] = "Other"
        result = search.compare(observation("ab"), changed)
        self.assertTrue(result["ordered_dids_identical"])
        self.assertEqual(result["body_differs_for"], ["did:plc:b"])
        self.assertFalse(result["identical"])


class RunTests(unittest.TestCase):
    def test_differences_are_rechecked_once_and_explained_by_flagged_dids(self):
        answers = {"one": "ab", "two": "ab", "three": "ab"}
        shadow_answers = {"one": "ab", "two": "ba", "three": "ac"}
        slept = []
        result = search.run(
            backend(answers), backend(shadow_answers), [case("one"), case("two"), case("three")],
            flagged={"did:plc:c"}, settle_delay=9, sleep=slept.append,
        )
        self.assertEqual(slept, [9])
        rows = {row["id"]: row for row in result["cases"]}
        self.assertEqual([rows[i]["rechecked"] for i in ("q:one", "q:two", "q:three")], [False, True, True])
        self.assertEqual(rows["q:two"]["explained_by"], [])
        self.assertEqual(rows["q:three"]["explained_by"], ["did:plc:c"])
        self.assertEqual((result["identical"], result["differing_explained"], result["differing_unexplained"]), (1, 1, 1))

    def test_a_difference_that_settles_is_not_reported(self):
        shadow_answers = {"one": "ba"}
        shadow = backend(shadow_answers)
        result = search.run(
            backend({"one": "ab"}), shadow, [case("one")], settle_delay=1,
            sleep=lambda seconds: shadow_answers.update(one="ab"),
        )
        self.assertEqual(result["identical"], 1)
        self.assertEqual(result["settle_delay_s"], 1)

    def test_bench_structural_checks_run_per_backend(self):
        good = {"zzstoatzz.io": ["other"], "fig": "ab", "fig (": "ab"}
        cases = [case("zzstoatzz.io"), case("fig"), case("fig (")]
        result = search.run(backend(good), backend(dict(good, **{"fig (": "a"})), cases, sleep=lambda s: None)
        rows = {row["id"]: row for row in result["cases"]}
        self.assertIn("zzstoatzz.io not rank 1", rows["q:zzstoatzz.io"]["production"]["check_failures"][0])
        self.assertEqual(rows["q:fig ("]["production"]["check_failures"], [])
        self.assertIn("diverged", rows["q:fig ("]["shadow"]["check_failures"][0])
        self.assertEqual(rows["q:fig"]["shadow"]["check_failures"], [])

    def test_status_and_scope_acknowledgment_are_checked(self):
        scoped = {"id": "scope:x", "params": {"q": "one", "pds": "host.test"}, "expect": {"status": 200, "headers": {"x-typeahead-pds": "other.test"}}}
        rejected = {"id": "scope:bad", "params": {"q": "nope"}, "expect": {"status": 400}}
        result = search.run(backend({"one": "a"}), backend({"one": "a"}), [scoped, rejected, case("nope")], sleep=lambda s: None)
        rows = {row["id"]: row for row in result["cases"]}
        self.assertIn("header x-typeahead-pds", rows["scope:x"]["shadow"]["check_failures"][0])
        self.assertEqual(rows["scope:bad"]["shadow"]["check_failures"], [])
        self.assertIn("status 400, expected 200", rows["q:nope"]["shadow"]["check_failures"][0])

    def test_query_sets_reuse_bench_and_the_recorded_cases(self):
        ids = [c["id"] for c in search.canonical_cases()]
        self.assertEqual(len(ids), len(set(ids)))
        for q in [*search.bench.RANKING_CORPUS, *search.bench.FULL_CORPUS, *search.bench.DISPLAY_NAME_QUERIES]:
            self.assertIn("q:" + q, ids)
        recorded = {c["id"]: c for c in search.recorded_cases()}
        self.assertEqual(recorded["scope:hard-pds"]["expect"]["headers"], {"x-typeahead-pds": "selfhosted.social"})
        self.assertEqual(recorded["scope:invalid-pds"]["expect"]["status"], 400)


class LatencyTests(unittest.TestCase):
    def test_percentiles_use_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual([search.percentile(values, p) for p in (50, 95, 99)], [50, 95, 99])
        self.assertEqual(search.percentile([7], 99), 7)
        self.assertIsNone(search.percentile([], 50))

    def test_cold_and_warm_samples_and_server_timing_basis(self):
        cases = [case("one"), case("two"), {"id": "bad", "params": {"q": "x"}, "expect": {"status": 400}}]
        answers = {"one": "a", "two": "b"}
        result = search.latency(backend(answers), backend(answers, timing="search;dur=2.500"), cases, warm_runs=3)
        self.assertEqual(result["queries"], 2)
        self.assertEqual(result["gate_basis"], "server_ms")
        self.assertEqual(result["summary"]["production"]["cold"]["server_ms"]["n"], 2)
        self.assertEqual(result["summary"]["shadow"]["warm"]["server_ms"], {"n": 6, "p50": 2.5, "p95": 2.5, "p99": 2.5, "max": 2.5})
        self.assertEqual(result["paired"]["warm_server_ms"]["median_shadow_minus_production"], 1.0)
        self.assertEqual(result["paired"]["warm_server_ms"]["queries_shadow_slower"], 2)
        self.assertEqual([row["order"][0] for row in result["per_query"]], ["production", "shadow"])

    def test_missing_server_timing_falls_back_to_client_time(self):
        answers = {"one": "a"}
        result = search.latency(backend(answers), backend(answers, timing=None), [case("one")], warm_runs=1)
        self.assertEqual(result["gate_basis"], "client_ms")
        self.assertIsNone(result["per_query"][0]["paired"]["warm_server_ms"])


if __name__ == "__main__":
    unittest.main()
