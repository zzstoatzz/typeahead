import hashlib
import json
import unittest

import corpus
import views

LABEL_A = {"src": "did:plc:mod", "uri": "did:plc:a", "val": "spam", "cts": "2026-01-01T00:00:00Z"}
LABEL_B = {"val": "!hide", "uri": "did:plc:a", "src": "did:plc:mod", "cts": "2026-02-01T00:00:00Z", "cid": "bafy"}


def stored(**changes):
    actor = {
        "did": "did:plc:a", "handle": "a.test", "display_name": 'Ada "L" 名前\n', "avatar_url": "bafkavatar",
        "labels": [LABEL_A], "associated": {"labeler": False, "chat": {"allowIncoming": "all"}},
        "created_at": "2026-01-01T00:00:00Z", "pds": "https://pds.test", "pds_host": "pds.test", "quality_score": 0.1,
        "version": 7, "deleted": False, "hidden": False,
    }
    actor.update(changes)
    return actor


def view(**changes):
    return views.from_actor(stored(**changes), sha="s")


class ProfileHashTests(unittest.TestCase):
    def test_hash_is_over_compact_key_sorted_json_with_unicode_unescaped(self):
        expected = (
            '["Ada \\"L\\" 名前\\n","bafkavatar",'
            '[{"cts":"2026-01-01T00:00:00Z","src":"did:plc:mod","uri":"did:plc:a","val":"spam"}],'
            '{"chat":{"allowIncoming":"all"},"labeler":false},"2026-01-01T00:00:00Z"]'
        ).encode()
        self.assertEqual(view().fields["profile_sha256"], hashlib.sha256(expected).hexdigest())

    def test_labels_and_associated_given_as_json_text_are_parsed(self):
        text = view(labels=json.dumps([LABEL_A]), associated='{"chat":{"allowIncoming":"all"},"labeler":false}')
        self.assertEqual(text.fields, view().fields)

    def test_missing_fields_take_the_index_defaults(self):
        bare = views.from_actor({"did": "did:plc:a", "quality_score": 0, "version": 1, "deleted": True}, sha="s")
        self.assertEqual(bare.state, "deleted")
        self.assertEqual(bare.fields["labels"], [])
        self.assertEqual(bare.fields["profile_sha256"], hashlib.sha256(b'["","",[],{},""]').hexdigest())

    def test_quality_score_is_held_at_single_precision(self):
        self.assertEqual(view().fields["quality_score"], views.f32(0.10000000149011612))
        self.assertNotEqual(view().fields["quality_score"], 0.1)


class LabelTests(unittest.TestCase):
    def test_label_order_and_key_order_do_not_matter(self):
        reordered = [{"val": "spam", "cts": LABEL_A["cts"], "uri": "did:plc:a", "src": "did:plc:mod"}]
        self.assertEqual(view(labels=[LABEL_B, LABEL_A]).fields["labels"], view(labels=reordered + [LABEL_B]).fields["labels"])

    def test_label_difference_is_its_own_class_with_the_labels_named(self):
        classes, other = corpus.classify(view(), view(labels=[LABEL_A, LABEL_B]))
        self.assertEqual((classes, other), ([corpus.LABELS_DIFFER], []))
        entry = corpus.difference("did:plc:a", view(), view(labels=[LABEL_B]))
        self.assertEqual(entry["labels"], {"only_in_production": [LABEL_A], "only_in_shadow": [LABEL_B], "values_agree": False})

    def test_same_values_with_different_detail_are_marked(self):
        later = dict(LABEL_A, cts="2026-03-03T00:00:00Z")
        entry = corpus.difference("did:plc:a", view(), view(labels=[later]))
        self.assertEqual(entry["classes"], [corpus.LABELS_DIFFER])
        self.assertTrue(entry["labels"]["values_agree"])

    def test_profile_difference_beside_a_label_difference_is_reported_separately(self):
        classes, other = corpus.classify(view(), view(labels=[], display_name="Other"))
        self.assertEqual((classes, other), ([corpus.LABELS_DIFFER, corpus.OTHER_FIELD_DIFFERS], ["profile_rest_sha256"]))

    def test_a_side_without_itemised_labels_falls_back_to_the_profile_hash(self):
        hashed = views.Doc("s", {k: v for k, v in view(labels=[]).fields.items() if k not in ("labels", "profile_rest_sha256")})
        self.assertEqual(corpus.classify(view(), hashed), ([corpus.OTHER_FIELD_DIFFERS], ["profile_sha256"]))
        same = views.Doc("s", {k: v for k, v in view().fields.items() if k not in ("labels", "profile_rest_sha256")})
        self.assertEqual(corpus.classify(view(), same), ([], []))

    def test_summary_counts_label_values_per_side(self):
        differences = {
            "did:plc:a": corpus.difference("did:plc:a", view(), view(labels=[LABEL_B])),
            "did:plc:b": corpus.difference("did:plc:b", view(), view(labels=[])),
            "did:plc:c": corpus.difference("did:plc:c", view(), view(labels=[dict(LABEL_A, cts="x")])),
        }
        summary = corpus.summarize(differences, 5)
        self.assertEqual(summary["classes"][corpus.LABELS_DIFFER]["count"], 3)
        self.assertEqual(summary["label_value_counts"], {"only_in_production": {"spam": 3}, "only_in_shadow": {"!hide": 1, "spam": 1}})
        self.assertEqual(summary["label_detail_only"], 1)


class QualityTests(unittest.TestCase):
    def test_widened_and_shortest_forms_of_one_float_agree(self):
        widened = views.Doc("s", dict(view().fields, quality_score=0.10000000149011612))
        self.assertEqual(corpus.classify(view(), widened), ([], []))

    def test_a_different_score_is_its_own_class(self):
        self.assertEqual(corpus.classify(view(), view(quality_score=0.2)), ([corpus.QUALITY_SCORE_DIFFERS], []))


if __name__ == "__main__":
    unittest.main()
