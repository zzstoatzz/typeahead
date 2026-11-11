import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx

import bundle
import corpus
import store
import views
from fakes import FakeStore, store_record, stored_actor, visibility, walk_settings
from transport import Budget, Client, TransportError

SECRET = "store-s3cr3t-0123456789"
LABEL = {"src": "did:plc:mod", "uri": "did:plc:x", "val": "spam", "cts": "2026-01-01T00:00:00Z"}


def did(n):
    return f"did:plc:{n:05}"


def record(n=1, **changes):
    base = {
        "did": did(n), "status": "live", "hidden": False, "handle": f"h{n}.test", "display_name": "Name", "avatar": "bafkavatar",
        "pds": "https://PDS.Test/", "created_at": "2026-01-01T00:00:00Z", "associated": '{"labeler":false,"chat":{"allowIncoming":"all"}}',
        "quality_score": 0.1, "updated_at": 500, "labels": [],
    }
    base.update(changes)
    return base


class RuleTests(unittest.TestCase):
    def test_pds_host_follows_the_index_normalizer(self):
        self.assertEqual(store.pds_host("https://Acme.com./"), "acme.com")
        self.assertEqual(store.pds_host("HTTP://sub.acme-pds.com"), "sub.acme-pds.com")
        self.assertEqual(store.pds_host("acme.com"), "acme.com")
        for bad in ("", "acme", "evil@acme.com", "acme.com/path", "acme.com?x", "acme.com:443", "*.acme.com", "a..com", "-a.com",
                    "acme.com\x1fh:b", "ünï.com", "a." + "b" * 64 + ".com", "https://" + "a." * 130 + "com"):
            self.assertEqual(store.pds_host(bad), "", bad)

    def test_only_expiries_the_index_can_evaluate_are_forwarded(self):
        for good in ("2027-01-01T00:00:00Z", "2027-01-01T00:00:00.000Z", "2024-02-29T23:59:59.123456789+05:30", "2027-01-01T00:00:00-00:00"):
            self.assertTrue(store.supported_expiry(good), good)
        for bad in ("", "2027-01-01", "2027-01-01T00:00:00", "2027-01-01t00:00:00Z", "2027-02-29T00:00:00Z", "0999-01-01T00:00:00Z",
                    "2027-13-01T00:00:00Z", "2027-01-01T24:00:00Z", "2027-01-01T00:00:00.Z", "2027-01-01T00:00:00.1234567890Z",
                    "2027-01-01T00:00:00Zx", "2027-01-01T00:00:00+0530", "2027-01-01T00:00:00+24:00", "２０２７-01-01T00:00:00Z"):
            self.assertFalse(store.supported_expiry(bad), bad)


class ViewTests(unittest.TestCase):
    def test_a_live_record_is_the_document_the_bridge_sends(self):
        labels = [
            {"source": "did:plc:mod", "uri": "did:plc:x", "value": "spam", "created": "2026-01-01T00:00:00Z", "cid": "bafy", "expires": "2027-01-01T00:00:00Z", "negated": False},
            {"source": "did:plc:mod", "uri": "did:plc:x", "value": "gone", "created": "c", "negated": True},
            {"source": "did:plc:x", "uri": "at://x", "value": "self", "created": "c", "cid": "", "expires": "someday"},
        ]
        doc = store.view(record(labels=labels, hidden=True))
        expected = views.from_actor({
            "deleted": False, "hidden": True, "handle": "h1.test", "display_name": "Name", "avatar_url": "bafkavatar",
            "labels": [
                {"src": "did:plc:mod", "uri": "did:plc:x", "val": "spam", "cts": "2026-01-01T00:00:00Z", "cid": "bafy", "exp": "2027-01-01T00:00:00Z"},
                {"src": "did:plc:x", "uri": "at://x", "val": "self", "cts": "c"},
            ],
            "associated": {"chat": {"allowIncoming": "all"}, "labeler": False}, "created_at": "2026-01-01T00:00:00Z",
            "pds": "https://PDS.Test/", "pds_host": "pds.test", "quality_score": 0.1,
        }, sha=doc.sha, changed_at=500)
        self.assertEqual({k: v for k, v in doc.fields.items() if k != "store_status"}, expected.fields)
        self.assertEqual(views.key_of(doc.fields), views.key_of(expected.fields))
        self.assertEqual((doc.state, doc.fields["changed_at"], doc.fields["store_status"]), ("live", 500, "live"))

    def test_records_the_index_would_not_serve_are_tombstones(self):
        for changes in ({"status": "deleted"}, {"status": "deactivated"}, {"handle": ""}):
            doc = store.view(record(updated_at=9, **changes))
            self.assertEqual((doc.state, doc.fields["handle"], doc.fields["changed_at"]), ("deleted", "", 9), changes)
        self.assertEqual(store.view(record(status="deactivated")).fields["store_status"], "deactivated")

    def test_an_empty_associated_is_an_empty_object(self):
        self.assertEqual(store.view(record(associated="")).fields["associated"], "{}")

    def test_refresh_and_identity_times_and_an_override_are_kept_when_the_store_sends_them(self):
        fields = store.view(record(profile_checked_at=700, identity_resolved_at=600, override="hide")).fields
        self.assertEqual((fields["refreshed_at"], fields["identity_at"], fields["override"]), (700, 600, "hide"))
        bare = store.view(record(override="none")).fields
        self.assertFalse({"refreshed_at", "identity_at", "override"} & set(bare))


class RowTests(unittest.TestCase):
    """A row's key is the key of the view it would build, so the view is built only for rows that differ."""

    LABELS = [
        {"source": "did:plc:mod", "uri": "did:plc:x", "value": "spam", "created": "2026-01-01T00:00:00Z", "cid": "bafy", "expires": "2027-01-01T00:00:00Z", "negated": False},
        {"source": "did:plc:mod", "uri": "did:plc:x", "value": "gone", "created": "c", "negated": True},
        {"source": "did:plc:x", "uri": "at://x", "value": "self", "created": "c", "cid": "", "expires": "someday"},
    ]
    CHANGES = (
        {}, {"labels": LABELS}, {"labels": LABELS[::-1]}, {"hidden": True}, {"associated": ""}, {"associated": '{"chat":{"allowIncoming":"all"},"labeler":false}'},
        {"associated": {"labeler": True}}, {"quality_score": 168.02}, {"quality_score": 3}, {"pds": ""}, {"pds": "not a host"}, {"avatar": ""},
        {"display_name": 'Ada "L" 名前\n'}, {"status": "deactivated"}, {"status": "deleted", "handle": ""}, {"handle": ""}, {"created_at": ""},
        {"profile_checked_at": 700, "override": "show", "description": "ignored", "followers_count": 9},
    )

    def test_the_key_is_the_key_of_the_view(self):
        for changes in self.CHANGES:
            item = record(**changes)
            row = store.row(item)
            self.assertEqual(row.key, views.key_of(store.view(item).fields), changes)
            self.assertEqual((row.live, row.changed_at), (store.view(item).live, 500), changes)
            self.assertEqual(row.doc(), store.view(item), changes)

    def test_a_dumped_actor_and_the_store_record_for_it_share_a_key(self):
        for n, labels in enumerate(([], [LABEL])):
            actor = stored_actor(did(n), f"h{n}.test", labels=labels, score=n / 7)
            self.assertEqual(store.row(store_record(actor, 50)).key, views.key_of(views.from_actor(actor, sha="s").fields))

    def test_a_row_carries_the_refresh_time_when_the_store_sends_one(self):
        self.assertEqual(store.row(record(profile_checked_at=700)).refreshed_at, 700)
        self.assertIsNone(store.row(record()).refreshed_at)
        self.assertIsNone(store.row(record(profile_checked_at="soon")).refreshed_at)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.records = [record(n) for n in range(23)] + [record(40, status="deleted", handle="")]
        self.fake = FakeStore(self.records, SECRET, next_change=77)
        self.addCleanup(self.fake.close)

    def source(self, secret=SECRET, **options):
        client = Client("shadow store", self.fake.base, Budget(1000, 10**7), secret=secret, secret_prefix=store.SECRET_PREFIX, retries=0, **options)
        self.addCleanup(client.close)
        return store.StoreHttp(client)

    def test_pages_walk_every_record_in_did_order(self):
        source = self.source()
        seen, after = [], ""
        while True:
            page = source.read(after, 10)
            seen += page
            if len(page) < 10:
                break
            after = page[-1][0]
        self.assertEqual([d for d, _ in seen], sorted(r["did"] for r in self.records))
        self.assertEqual((seen[-1][1].doc().state, seen[-1][1].live, seen[0][1].live), ("deleted", False, True))
        self.assertEqual([body for path, body in self.fake.calls if path == store.PAGE_PATH][1], {"after_did": did(9), "limit": 10})
        with self.assertRaises(TransportError):
            source.read("", 501)

    def test_lookup_answers_each_did_and_marks_the_missing_absent(self):
        found = self.source().lookup([did(3), "did:plc:none", did(40)])
        self.assertEqual({d: doc.state for d, doc in found.items()}, {did(3): "live", "did:plc:none": "absent", did(40): "deleted"})

    def test_lookup_is_sent_in_batches_the_store_accepts(self):
        wanted = [did(n) for n in range(1100)]
        self.assertEqual(len(self.source().lookup(wanted)), 1100)
        self.assertEqual([len(body["dids"]) for path, body in self.fake.calls if path == store.BATCH_PATH], [500, 500, 100])

    def test_marker_records_the_change_log_position(self):
        self.assertEqual(self.source().marker()["next_change"], 77)

    def test_a_wrong_secret_fails_without_printing_it(self):
        with self.assertRaises(TransportError) as raised:
            self.source(secret="wrong-" + SECRET).read("", 5)
        self.assertIn("401", str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))

    def test_the_bearer_goes_only_to_store_paths(self):
        seen = {}

        def handler(request):
            seen[request.url.path] = request.headers.get("authorization")
            return httpx.Response(200, json={"actors": []})

        client = Client("s", "https://store.test", Budget(10, 10**6), secret=SECRET, secret_prefix=store.SECRET_PREFIX,
                        http=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.close)
        store.StoreHttp(client).read("", 5)
        client.get("/ready")
        client.post("/admin/source-state", {"dids": []})
        self.assertEqual(seen, {store.PAGE_PATH: "Bearer " + SECRET, "/ready": None, "/admin/source-state": None})

    def test_replies_that_break_the_contract_are_refused(self):
        def source(reply):
            client = Client("s", "https://store.test", Budget(10, 10**6), http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply))))
            self.addCleanup(client.close)
            return store.StoreHttp(client)

        bad_pages = [
            {}, {"actors": [{"did": did(1)}]}, {"actors": [record(1, status="gone")]}, {"actors": [record(1, updated_at=None)]},
            {"actors": [record(1, quality_score="1")]}, {"actors": [record(1, associated="{")]}, {"actors": [record(1, labels=["x"])]},
        ]
        for reply in bad_pages:
            with self.assertRaises(TransportError, msg=reply):
                source(reply).read("", 5)
        for reply in ({"actors": []}, {"actors": [record(2)]}, {"actors": [None, None]}):
            with self.assertRaises(TransportError, msg=reply):
                source(reply).lookup([did(1)])

    def test_a_page_out_of_did_order_stops_the_walk(self):
        client = Client("s", "https://store.test", Budget(10, 10**6), http=httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"actors": [record(2), record(1)]}))))
        self.addCleanup(client.close)
        side = corpus.Side(store.StoreHttp(client))
        with self.assertRaises(TransportError):
            side.fill("", 5)


class WalkTests(unittest.TestCase):
    def test_a_dump_and_a_store_holding_the_same_actors_agree(self):
        rows = [stored_actor(did(n), f"h{n}.test", labels=[LABEL] if n % 3 == 0 else (), score=n / 7) for n in range(30)]
        rows.append(stored_actor(did(50), "", deleted=True))
        fake = FakeStore([store_record(row, 50) for row in rows], SECRET)
        self.addCleanup(fake.close)
        client = Client("shadow store", fake.base, Budget(1000, 10**7), secret=SECRET, secret_prefix=store.SECRET_PREFIX)
        self.addCleanup(client.close)
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "sorted.jsonl"
            raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in reversed(rows))
            bundle.sort(io.BytesIO(raw), out, Path(root) / "runs", run_records=8)
            result = corpus.run_sources(bundle.BundleJsonl(out, watermark=100, build_id="b1"), store.StoreHttp(client), **walk_settings(page_size=7))
        self.assertTrue(result["complete"] and result["rechecked"])
        self.assertEqual((result["differing_dids"], result["dids_walked"]), (0, 31))
        self.assertEqual({name: note["count"] for name, note in result["notes"].items() if note["count"]}, {})
        self.assertEqual(result["live"], {"production": 30, "shadow": 30})
        self.assertEqual(result["start_markers"]["shadow"]["next_change"], 1)


if __name__ == "__main__":
    unittest.main()
