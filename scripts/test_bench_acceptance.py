import unittest

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


if __name__ == "__main__":
    unittest.main()
