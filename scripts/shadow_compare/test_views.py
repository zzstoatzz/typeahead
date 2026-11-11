import hashlib
import json
import unittest

import corpus
import views
from fakes import recorded, visibility

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


def classify(production, shadow):
    return corpus.classify("did:plc:a", production, shadow, visibility())[:2]


def difference(did, production, shadow):
    return corpus.difference(did, production, shadow, visibility())


class ProfileHashTests(unittest.TestCase):
    def test_a_label_version_does_not_make_labels_differ(self):
        base = {"src": "did:plc:mod", "uri": "did:plc:a", "val": "porn", "cts": "2026-01-01T00:00:00Z"}
        with_version = views.from_actor({"labels": [{**base, "ver": 1}]}, sha="a")
        without = views.from_actor({"labels": [base]}, sha="b")
        self.assertEqual(with_version.fields["labels"], without.fields["labels"])
        self.assertNotEqual(with_version.fields["profile_sha256"], without.fields["profile_sha256"])

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
        classes, other = classify(view(), view(labels=[LABEL_A, LABEL_B]))
        self.assertEqual((classes, other), ([corpus.LABELS_DIFFER], []))
        entry = difference("did:plc:a", view(), view(labels=[LABEL_B]))
        self.assertEqual(entry["labels"], {"only_in_production": [LABEL_A], "only_in_shadow": [LABEL_B], "values_agree": False})

    def test_same_values_with_different_detail_are_marked(self):
        later = dict(LABEL_A, cts="2026-03-03T00:00:00Z")
        entry = difference("did:plc:a", view(), view(labels=[later]))
        self.assertEqual((entry["classes"], entry["notes"]), ([], [corpus.LABEL_DETAIL_DIFFERS]))
        self.assertTrue(entry["labels"]["values_agree"])
        other_labeler = difference("did:plc:a", view(), view(labels=[dict(LABEL_A, src="did:plc:other")]))
        self.assertEqual(other_labeler["classes"], [corpus.LABELS_DIFFER])

    def test_profile_difference_beside_a_label_difference_is_reported_separately(self):
        classes, other = classify(view(), view(labels=[], display_name="Other"))
        self.assertEqual((classes, other), ([corpus.LABELS_DIFFER, corpus.DISPLAY_NAME_DIFFERS], []))

    def test_a_side_without_itemised_fields_falls_back_to_the_profile_hash(self):
        hashed = views.Doc("s", {k: v for k, v in view(labels=[]).fields.items() if k not in views.ITEMISED})
        self.assertEqual(classify(view(), hashed), ([corpus.PROFILE_DIFFERS], []))
        same = views.Doc("s", {k: v for k, v in view().fields.items() if k not in views.ITEMISED})
        self.assertEqual(classify(view(), same), ([], []))

    def test_summary_counts_label_values_per_side(self):
        summary = recorded([
            difference("did:plc:a", view(), view(labels=[LABEL_B])),
            difference("did:plc:b", view(), view(labels=[])),
            difference("did:plc:c", view(), view(labels=[dict(LABEL_A, cts="x")])),
        ])
        self.assertEqual(summary["classes"][corpus.LABELS_DIFFER]["count"], 2)
        self.assertEqual(summary["label_value_counts"], {"only_in_production": {"spam": 2}, "only_in_shadow": {"!hide": 1}})
        self.assertEqual(summary["notes"][corpus.LABEL_DETAIL_DIFFERS]["count"], 1)
        self.assertEqual(summary["differing_dids"], 2)


class ProfileFieldTests(unittest.TestCase):
    def test_each_profile_field_is_named(self):
        cases = {
            corpus.DISPLAY_NAME_DIFFERS: view(display_name="Other"),
            corpus.ASSOCIATED_DIFFERS: view(associated={"labeler": True}),
            corpus.CREATED_AT_DIFFERS: view(created_at="2026-01-01T00:00:00.000Z"),
            corpus.AVATAR_DIFFERS: view(avatar_url="bafkother"),
        }
        for name, changed in cases.items():
            self.assertEqual(classify(view(), changed), ([name], []))
        self.assertEqual(classify(view(), view(pds="https://pds.test/")), ([], [corpus.PDS_FORM_DIFFERS]))

    def test_associated_key_order_is_not_a_difference(self):
        reordered = view(associated={"chat": {"allowIncoming": "all"}, "labeler": False})
        self.assertEqual(classify(view(), reordered), ([], []))

    def test_one_avatar_stored_as_a_cid_and_as_a_url_differs_only_in_form(self):
        did = "did:plc:a"
        forms = [
            f"https://cdn.bsky.app/img/avatar/plain/{did}/bafkavatar@jpeg",
            f"https://pds.test/xrpc/com.atproto.sync.getBlob?did={did}&cid=bafkavatar",
            "https://cdn.test/path/bafkavatar",
        ]
        for form in forms:
            self.assertEqual(views.avatar_cid(form), "bafkavatar")
            self.assertEqual(classify(view(), view(avatar_url=form)), ([], [corpus.AVATAR_FORM_DIFFERS]))
        self.assertEqual(views.avatar_cid("https://cdn.test/path/x?y"), "")
        self.assertEqual(views.avatar_cid(""), "")


class QualityTests(unittest.TestCase):
    def test_widened_and_shortest_forms_of_one_float_agree(self):
        widened = views.Doc("s", dict(view().fields, quality_score=0.10000000149011612))
        self.assertEqual(classify(view(), widened), ([], []))

    def test_a_different_score_is_its_own_class(self):
        self.assertEqual(classify(view(), view(quality_score=0.2)), ([corpus.QUALITY_SCORE_DIFFERS], []))


class KeyTests(unittest.TestCase):
    CHANGES = (
        {}, {"handle": "b.test"}, {"hidden": True}, {"pds": "https://pds.test/"}, {"pds_host": "other.test"}, {"quality_score": 0.2},
        {"display_name": "Other"}, {"avatar_url": "bafkother"}, {"associated": {"labeler": True}}, {"created_at": "x"},
        {"labels": []}, {"labels": [dict(LABEL_A, cts="x")]}, {"labels": [LABEL_A, LABEL_B]}, {"version": 9}, {"deleted": True},
        {"associated": {"chat": {"allowIncoming": "all"}, "labeler": False}}, {"quality_score": 0.10000000149011612},
        {"labels": [dict(LABEL_A, ver=1)]}, {"deleted": True, "handle": ""},
    )

    def test_equal_keys_are_exactly_the_pairs_with_no_difference(self):
        for left in self.CHANGES:
            for right in self.CHANGES:
                a, b = view(**left), view(**right)
                same = views.key_of(a.fields) == views.key_of(b.fields)
                self.assertEqual(same, difference("did:plc:a", a, b) is None, (left, right))

    def test_a_key_cannot_be_forged_by_moving_text_between_fields(self):
        self.assertNotEqual(views.key_of(view(display_name='a","b', avatar_url="").fields), views.key_of(view(display_name="a", avatar_url='b","').fields))

    def test_a_row_builds_its_view_once_and_only_when_asked(self):
        built = []
        row = views.Row("k", True, lambda payload: built.append(payload) or view(), "payload", 5, 6)
        self.assertEqual((row.key, row.live, row.changed_at, row.refreshed_at, built), ("k", True, 5, 6, []))
        self.assertIs(row.doc(), row.doc())
        self.assertEqual(built, ["payload"])
        ready = views.Row.of(views.Doc("s", {"deleted": False, "changed_at": 3}))
        self.assertEqual((ready.key, ready.live, ready.changed_at, ready.refreshed_at), (None, True, 3, None))


if __name__ == "__main__":
    unittest.main()
