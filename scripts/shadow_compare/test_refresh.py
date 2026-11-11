import json
import struct
import unittest
from pathlib import Path

import refresh
import views

VECTORS = Path(__file__).resolve().parents[2] / "services" / "src" / "store_process" / "testdata" / "profile_vectors.json"


def f32_bits(value):
    return struct.unpack(">I", struct.pack(">f", value))[0]


class VectorTests(unittest.TestCase):
    """The vectors the Worker generated, which the store's Zig port is also held to."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = json.loads(VECTORS.read_text())

    def test_every_score_row_matches_to_the_stored_bits(self):
        now = self.vectors["now_ms"]
        self.assertEqual(len(self.vectors["scores"]), 2000)
        for followers, follows, posts, created_ms, score, bits in self.vectors["scores"]:
            found = refresh.authority_score(followers, follows, posts, created_ms, now)
            self.assertEqual((found, f32_bits(found)), (score, bits), (followers, follows, posts, created_ms))

    def test_every_profile_case_maps_to_the_fields_the_worker_stores(self):
        now = self.vectors["now_ms"]
        self.assertEqual(len(self.vectors["cases"]), 56)
        for case in self.vectors["cases"]:
            expected, found = case["expected"], refresh.stored_fields(case["profile"], now)
            self.assertEqual(
                (found["handle"], found["display_name"], found["avatar_cid"], found["created_at"], found["labels"], found["associated"]),
                (expected["handle"], expected["displayName"], expected["avatarCid"], expected["createdAt"], json.loads(expected["labels"]),
                 views.text(json.loads(expected["associated"]))),
                case["name"],
            )
            self.assertEqual(
                (found["followers_count"], found["follows_count"], found["posts_count"], found["quality_score"], f32_bits(found["quality_score"])),
                (expected["followersCount"], expected["followsCount"], expected["postsCount"], expected["qualityScore"], expected["qualityScoreF32Bits"]),
                case["name"],
            )


class RuleTests(unittest.TestCase):
    def test_math_round_sends_a_tie_up(self):
        self.assertEqual([refresh.js_round(x) for x in (2.5, -2.5, 0.49999999999999994, 0.5, 1.5, -1.4)], [3, -2, 0, 1, 2, -1])

    def test_a_profile_without_counts_scores_zero_and_says_so(self):
        found = refresh.stored_fields({"did": "did:plc:a", "createdAt": "2020-01-01T00:00:00Z"}, 1_794_300_000_000)
        self.assertEqual((found["quality_score"], found["has_counts"]), (0.0, False))


if __name__ == "__main__":
    unittest.main()
