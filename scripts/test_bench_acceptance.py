import asyncio
import unittest
from unittest import mock

import bench


def result(query, handles):
    actors = [{"did": "did:plc:" + handle, "handle": handle} for handle in handles]
    return bench.RankingResult(query=query, bsky_actors=[], ours_actors=actors, debug=None, bsky_status=0, ours_status=200, ours_ms=1.0)


class PunctuationDivergence(unittest.TestCase):
    def test_diverging_result_sets_are_reported_rather_than_raising(self):
        failures = bench.acceptance_checks([result("fig", ["a.test", "b.test"]), result("fig (", ["a.test"])])
        self.assertEqual(len(failures), 1)
        self.assertIn("q='fig (' diverged from q='fig'", failures[0])

    def test_equal_result_sets_pass(self):
        self.assertEqual(bench.acceptance_checks([result("fig", ["a.test"]), result("fig (", ["a.test"])]), [])


class LatencyWarmUp(unittest.TestCase):
    def test_the_first_query_is_not_charged_for_opening_the_connections(self):
        seen = []

        async def fetch(client, url, timeout=30.0):
            seen.append(url)
            return {}, 500.0 if len(seen) <= 2 else 40.0, 200

        async def no_sleep(_):
            return None

        with mock.patch.object(bench, "timed_fetch", fetch), mock.patch.object(bench.asyncio, "sleep", no_sleep), mock.patch.object(bench, "progress"), mock.patch.object(bench, "clear_line"):
            stats = asyncio.run(bench.run_latency(None, "https://ours.test", ["a", "b"], runs=2))
        self.assertIn("limit=1", seen[0])
        self.assertTrue(seen[1].startswith(bench.BSKY))
        self.assertEqual(stats[0].ours_cold_ms, [40.0, 40.0])
        self.assertEqual(stats[0].bsky_ms, [40.0, 40.0])


if __name__ == "__main__":
    unittest.main()
