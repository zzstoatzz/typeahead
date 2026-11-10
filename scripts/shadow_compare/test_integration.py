"""Real typeahead-tantivy servers whose indexes differ in five known ways, and one index read back through dump-actors."""

import copy
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/tantivy/tests"))

import smoke

import arbiter
import bundle
import compare
import corpus
import gates
import sources
import views
from fakes import FakeStore, store_record
from transport import Budget, Client

MISSING = "did:plc:missing2222222222222222"
RENAMED = "did:plc:renamed2222222222222222"
HIDDEN = "did:plc:hidden22222222222222222"
MOVED = "did:plc:moved222222222222222222"
DELETED = "did:plc:deleted2222222222222222"
JUSTIN = "did:plc:ec64xv7n5dszeizw56yr6b5h"
LABELLED = "did:plc:labelled2222222222222222"
URLFORM = "did:plc:urlform22222222222222222"
NEWBORN = "did:plc:newborn22222222222222222"
DUMP_ACTORS = Path(os.environ.get("DUMP_ACTORS_BINARY", str(ROOT / "services/tantivy/target/release/dump-actors")))
STORE_SECRET_ENV = "SHADOW_COMPARE_TEST_STORE"
LABELS = [
    {"val": "bot-test", "src": "did:plc:labeler22222222222222222", "uri": LABELLED, "cts": "2026-10-01T00:00:00.000Z",
     "exp": "2027-10-01T00:00:00.000Z", "cid": "bafyreilabel"},
    {"src": LABELLED, "uri": "at://" + LABELLED + "/app.bsky.actor.profile/self", "val": "self-test", "cts": "2026-10-02T00:00:00Z"},
]
PRODUCTION_SECRET_ENV = "SHADOW_COMPARE_TEST_PRODUCTION_ADMIN"
SHADOW_SECRET_ENV = "SHADOW_COMPARE_TEST_SHADOW_ADMIN"


def actor(did, handle, display, score=1.0, pds="https://pds-one.test", hidden=False):
    return {
        "did": did, "handle": handle, "display_name": display, "quality_score": score, "pds": pds, "hidden": hidden,
        "deleted": False, "version": 100, "labels": [], "associated": {}, "avatar_url": "bafkfixture",
        "created_at": "2026-10-28T00:00:00Z",
    }


def production_rows():
    rows = [
        actor("did:plc:zzstoatzz222222222222222", "zzstoatzz.io", "nate", 9000),
        actor("did:plc:figaka222222222222222222", "bad-example.com", "fig (aka:[phil])", 400),
        actor(JUSTIN, "justinfagnani.test", "Justin Fagnani", 500),
        actor("did:plc:tokyo2222222222222222222", "tokyo.test", "東京タワー", 300),
        actor("did:plc:selfhosted22222222222222", "alpha.selfhosted.test", "Alpha Selfhosted", 200, "https://selfhosted.social"),
        actor(MISSING, "onlyprod.test", "Missing Marker", 50),
        actor(RENAMED, "oldhandle.test", "Renamed Marker", 50),
        actor(HIDDEN, "veiled.test", "Veiled Marker", 50),
        actor(MOVED, "moved.test", "Moved Marker", 50),
        actor(DELETED, "departed.test", "Departed Marker", 50),
        dict(actor(LABELLED, "labelled.example.test", 'Label "Bearer" 🌿 名前\\', 40.1), labels=LABELS,
             associated={"labeler": False, "chat": {"allowIncoming": "following"}, "activitySubscription": {"allowSubscriptions": "followers"}}),
        dict(actor(URLFORM, "urlform.example.test", "Avatar Form", 40, "https://Mixed-Case.Example./"),
             avatar_url=f"https://cdn.bsky.app/img/avatar/plain/{URLFORM}/bafkfixture@jpeg"),
    ]
    for n in range(40):
        name = ["nate", "alex", "bob", "sarah", "paul"][n % 5]
        rows.append(actor(f"did:plc:filler{n:018}", f"{name}{'abcdefgh'[n // 5]}.example.test", f"{name.title()} Person", 100 - n))
    return rows


def shadow_rows(rows, differ):
    rows = copy.deepcopy(rows)
    if not differ:
        return rows
    by_did = {row["did"]: row for row in rows}
    by_did[RENAMED]["handle"] = "newhandle.test"
    by_did[HIDDEN]["hidden"] = True
    by_did[MOVED]["pds"] = "https://pds-two.test"
    by_did[DELETED].update(deleted=True, handle="")
    return [row for row in rows if row["did"] != MISSING]


class AuthorityHandler(BaseHTTPRequestHandler):
    documents = {
        MISSING: ("onlyprod.test", "pds-one.test"),
        RENAMED: ("newhandle.test", "pds-one.test"),
        HIDDEN: ("veiled.test", "pds-one.test"),
        MOVED: ("moved.test", "pds-one.test"),
        NEWBORN: ("newborn.test", "pds-one.test"),
    }

    def log_message(self, *args):
        pass

    def answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/xrpc/app.bsky.actor.getProfiles":
            wanted = urllib.parse.parse_qs(parts.query).get("actors", [])
            profiles = [{"did": did, "handle": self.documents[did][0]} for did in wanted if did in self.documents]
            return self.answer(200, {"profiles": profiles})
        did = urllib.parse.unquote(parts.path.lstrip("/"))
        if did not in self.documents:
            return self.answer(404, {"message": "DID not registered"})
        handle, pds = self.documents[did]
        self.answer(200, {
            "id": did, "alsoKnownAs": ["at://" + handle],
            "service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://" + pds}],
        })


class Backends(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for binary in (smoke.BIN, DUMP_ACTORS):
            if not binary.exists():
                raise AssertionError(f"{binary.name} binary missing at {binary}; run `just --no-dotenv test-shadow-compare`")
        cls.stack = tempfile.TemporaryDirectory(prefix="shadow-compare-")
        cls.addClassCleanup(cls.stack.cleanup)
        cls.root = Path(cls.stack.name)
        cls.secrets = {"production": secrets.token_urlsafe(32), "shadow": secrets.token_urlsafe(32), "store": secrets.token_urlsafe(32)}
        cls.environ = {PRODUCTION_SECRET_ENV: cls.secrets["production"], SHADOW_SECRET_ENV: cls.secrets["shadow"],
                       STORE_SECRET_ENV: cls.secrets["store"]}
        rows = production_rows()
        cls.rows = rows
        cls.servers = {}
        for name, data, secret in [
            ("production", rows, cls.secrets["production"]),
            ("shadow", shadow_rows(rows, True), cls.secrets["shadow"]),
            ("twin", shadow_rows(rows, False), cls.secrets["shadow"]),
        ]:
            cls.servers[name] = cls.launch(name, data, secret)
        cls.authority = ThreadingHTTPServer(("127.0.0.1", 0), AuthorityHandler)
        threading.Thread(target=cls.authority.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.authority.server_close)
        cls.addClassCleanup(cls.authority.shutdown)
        cls.authority_url = f"http://127.0.0.1:{cls.authority.server_address[1]}"

    @classmethod
    def launch(cls, name, rows, secret):
        seed, data = cls.root / (name + ".jsonl"), cls.root / name
        watermark = int(time.time()) - (5 if name == "production" else 0)
        for row in rows:
            row["version"] = watermark
        lines = [json.dumps(a, ensure_ascii=False, separators=(",", ":")).encode() + b"\n" for a in sorted(rows, key=lambda a: a["did"])]
        header = {"type": "manifest", "build_id": "b-" + name, "source_watermark": watermark, "actor_count": len(rows),
                  "normalize_version": "v2-cjk-bigram", "partial": True, "source_actor_count": len(rows)}
        footer = {"type": "end", "build_id": header["build_id"], "actor_count": len(rows), "actors_sha256": hashlib.sha256(b"".join(lines)).hexdigest()}
        seed.write_bytes(json.dumps(header).encode() + b"\n" + b"".join(lines) + json.dumps(footer).encode() + b"\n")
        done = subprocess.run([str(smoke.BIN), "import", "--data", str(data), "--input", str(seed), "--allow-partial", "true"],
                              text=True, capture_output=True, timeout=60)
        assert done.returncode == 0, done.stderr
        if name == "production":
            cls.watermark = watermark
            cls.sorted = cls.root / "production-sorted.jsonl"
            index = next(data.rglob("meta.json")).parent
            dumped = subprocess.run([str(DUMP_ACTORS), str(index)], capture_output=True, timeout=60)
            assert dumped.returncode == 0, dumped.stderr
            cls.dump_facts = bundle.sort(io.BytesIO(dumped.stdout), cls.sorted, cls.root / "runs", run_records=16)
        log = (cls.root / (name + ".log")).open("w")
        cls.addClassCleanup(log.close)
        child, public, admin = smoke.start(data, log, secret)
        cls.addClassCleanup(smoke.stop, child)
        return {"public": public, "admin": admin}

    def run_tool(self, shadow="shadow", extra=(), **flags):
        out = self.root / ("report-" + secrets.token_hex(4))
        argv = [
            "--production", self.servers["production"]["public"], "--shadow", self.servers[shadow]["public"],
            "--production-admin", self.servers["production"]["admin"], "--shadow-admin", self.servers[shadow]["admin"],
            "--production-admin-secret-env", PRODUCTION_SECRET_ENV, "--shadow-admin-secret-env", SHADOW_SECRET_ENV,
            "--corpus-delay", "0", "--search-delay", "0", "--arbiter-delay", "0", "--freshness-interval", "0",
            "--freshness-samples", "3", "--warm-runs", "2", "--page-size", "7", "--out-dir", str(out),
            "--plc-url", self.authority_url, "--appview-url", self.authority_url, *extra,
        ]
        for name, value in flags.items():
            argv += ["--" + name.replace("_", "-"), str(value)]
        args = compare.parser().parse_args(argv)
        report = compare.execute(args, environ=self.environ, sleep=lambda seconds: None)
        compare.reporting.write(report, out)
        return report, out

    def classes(self, report):
        return {name: sorted(i["did"] for i in entry["sample"]) for name, entry in report["corpus"]["classes"].items() if entry["count"]}

    def test_corpus_reports_exactly_the_known_differences(self):
        report, out = self.run_tool(phases="corpus", extra=["--arbiter"])
        result = report["corpus"]
        self.assertTrue(result["complete"] and result["rechecked"] and result["fields_available"])
        self.assertEqual(result["live"], {"production": len(self.rows), "shadow": len(self.rows) - 2})
        self.assertEqual(result["dids_walked"], len(self.rows))
        self.assertEqual(self.classes(report), {
            corpus.ONLY_IN_PRODUCTION: sorted([MISSING, DELETED]),
            corpus.HANDLE_DIFFERS: [RENAMED],
            corpus.VISIBILITY_DIFFERS: [HIDDEN],
            corpus.PDS_DIFFERS: [MOVED],
        })
        self.assertEqual(result["flagged_dids"], sorted([MISSING, RENAMED, HIDDEN, MOVED, DELETED]))
        states = {i["did"]: i["shadow"]["state"] for i in result["classes"][corpus.ONLY_IN_PRODUCTION]["sample"]}
        self.assertEqual(states, {MISSING: "absent", DELETED: "deleted"})
        labels = {name: entry["arbiter_labels"] for name, entry in result["classes"].items() if entry["count"]}
        self.assertEqual(labels, {
            corpus.ONLY_IN_PRODUCTION: {arbiter.PRODUCTION_RIGHT: 1, arbiter.SHADOW_RIGHT: 1},
            corpus.HANDLE_DIFFERS: {arbiter.SHADOW_RIGHT: 1},
            corpus.VISIBILITY_DIFFERS: {arbiter.UNDETERMINED: 1},
            corpus.PDS_DIFFERS: {arbiter.PRODUCTION_RIGHT: 1},
        })
        verdict = report["verdict"]
        self.assertEqual(verdict["gates"]["corpus"]["status"], gates.FAIL)
        self.assertEqual(len(verdict["gates"]["corpus"]["reasons"]), 3)
        self.assertEqual(verdict["overall"], gates.FAIL)
        written = (out / "report.json").read_text() + (out / "README.md").read_text()
        for secret in self.secrets.values():
            self.assertNotIn(secret, written)
        self.assertIn(PRODUCTION_SECRET_ENV, written)

    def test_identical_content_with_different_versions_passes_corpus_and_search(self):
        report, _ = self.run_tool(shadow="twin", phases="corpus,search,latency,freshness")
        self.assertEqual(report["corpus"]["differing_dids"], 0)
        self.assertEqual(report["corpus"]["live"], {"production": len(self.rows), "shadow": len(self.rows)})
        verdict = report["verdict"]["gates"]
        self.assertEqual(verdict["corpus"], {"status": gates.PASS, "reasons": []})
        self.assertEqual(verdict["search"], {"status": gates.PASS, "reasons": []})
        self.assertEqual(report["search"]["identical"], report["search"]["queries"])
        self.assertEqual(report["search"]["production_check_failures"], 0)
        self.assertEqual(report["latency"]["gate_basis"], "server_ms")
        self.assertEqual(report["latency"]["errors"], 0)
        self.assertGreaterEqual(report["latency"]["queries"], gates.LATENCY_MIN_QUERIES)
        self.assertEqual(verdict["latency"], gates.latency_gate(report["latency"]))
        fresh = report["freshness"]
        self.assertTrue(all(s["ready"] and s["sync_lag_s"] <= gates.FRESH_MAX_SYNC_LAG_S for side in fresh["samples"].values() for s in side))
        self.assertEqual(verdict["freshness"]["status"], gates.FAIL)
        self.assertTrue(all("moderation" in reason for reason in verdict["freshness"]["reasons"]), verdict["freshness"]["reasons"])

    def test_hash_only_comparison_cannot_classify(self):
        report, _ = self.run_tool(shadow="twin", phases="corpus", extra=["--no-fields"])
        result = report["corpus"]
        self.assertFalse(result["fields_available"])
        self.assertEqual(result["classes"][corpus.DOCUMENT_HASH_DIFFERS]["count"], len(self.rows))
        self.assertEqual(report["verdict"]["gates"]["corpus"]["status"], gates.FAIL)

    def test_search_differences_are_explained_only_by_flagged_dids(self):
        extra = self.root / "extra.txt"
        extra.write_text('Missing Marker\noldhandle.test\nVeiled Marker\nDeparted Marker\n{"q":"Moved Marker","pds":"pds-one.test"}\n')
        report, _ = self.run_tool(phases="corpus,search", extra_queries=extra)
        rows = {row["id"]: row for row in report["search"]["cases"]}
        expected = {"extra:1": MISSING, "extra:2": RENAMED, "extra:3": HIDDEN, "extra:4": DELETED, "extra:5": MOVED}
        for case_id, did in expected.items():
            self.assertFalse(rows[case_id]["identical"], case_id)
            self.assertEqual(rows[case_id]["explained_by"], [did])
            self.assertEqual(rows[case_id]["first_difference_rank"], 1)
        self.assertEqual(report["search"]["differing_unexplained"], 0)
        self.assertEqual(report["search"]["shadow_check_failures"], 0)
        self.assertEqual(report["verdict"]["gates"]["search"]["status"], gates.PASS)

        alone, _ = self.run_tool(phases="search", extra_queries=extra)
        self.assertEqual(alone["search"]["differing_unexplained"], 5)
        self.assertEqual(alone["verdict"]["gates"]["search"]["status"], gates.FAIL)
        self.assertEqual(alone["verdict"]["gates"]["corpus"]["status"], gates.NOT_RUN)

    def test_walk_resumes_from_its_checkpoint_against_real_servers(self):
        checkpoint = self.root / "checkpoint.json"
        first, _ = self.run_tool(phases="corpus", checkpoint=checkpoint, max_pages=2)
        self.assertFalse(first["corpus"]["complete"])
        self.assertEqual(first["verdict"]["gates"]["corpus"]["status"], gates.FAIL)
        second, _ = self.run_tool(phases="corpus", checkpoint=checkpoint)
        self.assertTrue(second["corpus"]["resumed"] and second["corpus"]["complete"])
        self.assertEqual(second["corpus"]["flagged_dids"], sorted([MISSING, RENAMED, HIDDEN, MOVED, DELETED]))
        self.assertEqual(second["corpus"]["dids_walked"], len(self.rows))

    def run_bundle(self, *shadow, extra=()):
        out = self.root / ("report-" + secrets.token_hex(4))
        args = compare.parser().parse_args([
            "--phases", "corpus", "--production-bundle", str(self.sorted), "--bundle-watermark", str(self.watermark),
            "--bundle-build-id", "b-production", *shadow, "--corpus-delay", "0", "--arbiter-delay", "0", "--page-size", "7",
            "--plc-url", self.authority_url, "--appview-url", self.authority_url, "--out-dir", str(out), *extra,
        ])
        report = compare.execute(args, environ=self.environ, sleep=lambda seconds: None)
        compare.reporting.write(report, out)
        return report, out

    def shadow_admin(self, name):
        return ["--shadow-admin", self.servers[name]["admin"], "--shadow-admin-secret-env", SHADOW_SECRET_ENV]

    def store(self, changes, extra=(), missing=()):
        records = {row["did"]: store_record(row, self.watermark - 1000) for row in self.rows if row["did"] not in missing}
        for did, change in changes.items():
            records[did].update(change)
        for row, updated_at in extra:
            records[row["did"]] = store_record(row, updated_at)
        fake = FakeStore(records.values(), self.secrets["store"], next_change=len(records) + 1)
        self.addCleanup(fake.close)
        return ["--shadow-store", fake.base, "--shadow-store-secret-env", STORE_SECRET_ENV]

    def partition(self, report, name):
        classes = report["corpus"]["timing"][name]["classes"]
        return {key: sorted(i["did"] for i in entry["sample"]) for key, entry in classes.items() if entry["count"]}

    def test_dumped_views_carry_the_servers_own_hashes_and_fields(self):
        self.assertEqual((self.dump_facts["lines"], self.dump_facts["actors"], self.dump_facts["duplicates"]), (len(self.rows), len(self.rows), 0))
        self.assertGreater(self.dump_facts["runs"], 1)
        client = Client("production admin", self.servers["production"]["admin"], Budget(10, 10**7), secret=self.secrets["production"])
        self.addCleanup(client.close)
        dumped = dict(bundle.BundleJsonl(self.sorted).read("", 500))
        served = sources.fetch_states(client, sorted(dumped))
        self.assertEqual(sorted(dumped), sorted(row["did"] for row in self.rows))
        for did, doc in dumped.items():
            self.assertEqual(doc.sha, served[did].sha, did)
            expected = dict(served[did].fields, quality_score=views.f32(served[did].fields["quality_score"]))
            self.assertEqual({name: value for name, value in doc.fields.items() if name not in views.ITEMISED}, expected, did)
        self.assertEqual([json.loads(text)["val"] for text in dumped[LABELLED].fields["labels"]], ["bot-test", "self-test"])
        self.assertEqual(dumped[URLFORM].fields["pds_host"], "mixed-case.example")

    def test_bundle_against_an_index_with_the_same_actors_passes(self):
        report, _ = self.run_bundle(*self.shadow_admin("twin"))
        result = report["corpus"]
        self.assertTrue(result["complete"] and result["rechecked"])
        self.assertEqual((result["differing_dids"], result["dids_walked"]), (0, len(self.rows)))
        self.assertEqual(result["live"], {"production": len(self.rows), "shadow": len(self.rows)})
        self.assertEqual(report["verdict"]["gates"]["corpus"], {"status": gates.PASS, "reasons": []})
        self.assertEqual(report["corpus_sources"]["production"]["kind"], "bundle-jsonl")

    def test_bundle_against_an_index_without_change_times_counts_every_difference_as_divergence(self):
        report, _ = self.run_bundle(*self.shadow_admin("shadow"), extra=["--arbiter"])
        result = report["corpus"]
        self.assertEqual(self.partition(report, corpus.NEWER_IN_SHADOW), {})
        self.assertEqual(self.partition(report, corpus.UNCHANGED_IN_SHADOW), {
            corpus.ONLY_IN_PRODUCTION: sorted([MISSING, DELETED]),
            corpus.HANDLE_DIFFERS: [RENAMED],
            corpus.VISIBILITY_DIFFERS: [HIDDEN],
            corpus.PDS_DIFFERS: [MOVED],
        })
        self.assertEqual(result["snapshot"]["shadow"], {"records": len(self.rows) - 1, "with_change_time": 0, "changed_since_cutoff": 0})
        verdict = report["verdict"]["gates"]["corpus"]
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertEqual(len(verdict["reasons"]), 3)
        self.assertTrue(all(reason.startswith("unchanged in shadow since the watermark, ") for reason in verdict["reasons"]))

    def test_bundle_against_a_store_holding_the_same_actors_passes(self):
        report, _ = self.run_bundle(*self.store({}))
        self.assertEqual((report["corpus"]["differing_dids"], report["corpus"]["dids_walked"]), (0, len(self.rows)))
        self.assertEqual(report["verdict"]["gates"]["corpus"], {"status": gates.PASS, "reasons": []})

    def test_changes_after_the_watermark_pass_when_the_shadow_is_never_wrong(self):
        later = self.watermark + 10
        newborn = actor(NEWBORN, "newborn.test", "Newborn Marker", 5)
        report, out = self.run_bundle(*self.store({
            RENAMED: {"handle": "newhandle.test", "updated_at": later},
            DELETED: {"status": "deleted", "updated_at": later},
            HIDDEN: {"hidden": True, "updated_at": later},
            LABELLED: {"labels": store_record({"did": LABELLED, "labels": LABELS[:1]}, 0)["labels"], "quality_score": 41.5, "updated_at": later},
            URLFORM: {"avatar": "bafkfixture", "display_name": "Avatar Reformed", "updated_at": later},
        }, extra=[(newborn, later)]), extra=["--arbiter"])
        result = report["corpus"]
        self.assertEqual(self.partition(report, corpus.UNCHANGED_IN_SHADOW), {})
        self.assertEqual(self.partition(report, corpus.NEWER_IN_SHADOW), {
            corpus.ONLY_IN_PRODUCTION: [DELETED],
            corpus.ONLY_IN_SHADOW: [NEWBORN],
            corpus.HANDLE_DIFFERS: [RENAMED],
            corpus.VISIBILITY_DIFFERS: [HIDDEN],
            corpus.LABELS_DIFFER: [LABELLED],
            corpus.QUALITY_SCORE_DIFFERS: [LABELLED],
            corpus.OTHER_FIELD_DIFFERS: [URLFORM],
        })
        newer = result["timing"][corpus.NEWER_IN_SHADOW]
        self.assertEqual(newer["other_field_counts"], {"display_name": 1, "avatar_form": 1})
        self.assertEqual(newer["label_value_counts"], {"only_in_production": {"self-test": 1}, "only_in_shadow": {}})
        labels = {name: entry["arbiter_labels"] for name, entry in newer["classes"].items() if entry["count"]}
        self.assertEqual(labels, {
            corpus.ONLY_IN_PRODUCTION: {arbiter.SHADOW_RIGHT: 1},
            corpus.ONLY_IN_SHADOW: {arbiter.SHADOW_RIGHT: 1},
            corpus.HANDLE_DIFFERS: {arbiter.SHADOW_RIGHT: 1},
            corpus.VISIBILITY_DIFFERS: {arbiter.UNDETERMINED: 1},
            corpus.LABELS_DIFFER: {arbiter.UNDETERMINED: 1},
            corpus.QUALITY_SCORE_DIFFERS: {arbiter.UNDETERMINED: 1},
            corpus.OTHER_FIELD_DIFFERS: {arbiter.UNDETERMINED: 1},
        })
        self.assertEqual(result["snapshot"]["shadow"], {"records": len(self.rows) + 1, "with_change_time": len(self.rows) + 1, "changed_since_cutoff": 6})
        breakdown = newer["classes"][corpus.OTHER_FIELD_DIFFERS]["breakdown"]
        self.assertEqual((breakdown["handle_kind"], breakdown["pds_kind"]), ({"custom domain": 1}, {"independent pds": 1}))
        self.assertEqual(breakdown["top_pds_hosts"], [{"host": "mixed-case.example", "count": 1}])
        self.assertEqual(report["verdict"]["gates"]["corpus"], {"status": gates.PASS, "reasons": []})
        self.assertEqual(result["start_markers"]["shadow"]["next_change"], len(self.rows) + 2)
        written = (out / "report.json").read_text() + (out / "README.md").read_text()
        for secret in self.secrets.values():
            self.assertNotIn(secret, written)
        self.assertIn(STORE_SECRET_ENV, written)
        self.assertIn("Production is bundle b-production at watermark", written)

    def test_divergence_before_the_watermark_fails_beside_newer_changes(self):
        report, _ = self.run_bundle(*self.store({
            RENAMED: {"handle": "newhandle.test", "updated_at": self.watermark + 10},
            MOVED: {"pds": "https://pds-two.test"},
        }, missing=[MISSING]), extra=["--arbiter"])
        self.assertEqual(self.partition(report, corpus.NEWER_IN_SHADOW), {corpus.HANDLE_DIFFERS: [RENAMED]})
        self.assertEqual(self.partition(report, corpus.UNCHANGED_IN_SHADOW), {corpus.ONLY_IN_PRODUCTION: [MISSING], corpus.PDS_DIFFERS: [MOVED]})
        verdict = report["verdict"]["gates"]["corpus"]
        self.assertEqual(verdict["status"], gates.FAIL)
        self.assertEqual(sorted(reason.split(":")[0] for reason in verdict["reasons"]), [
            "unchanged in shadow since the watermark, only_in_production", "unchanged in shadow since the watermark, pds_differs",
        ])
        self.assertTrue(all("production is right" in reason for reason in verdict["reasons"]))

    def test_a_shadow_changed_after_the_watermark_without_an_arbiter_fails(self):
        report, _ = self.run_bundle(*self.store({RENAMED: {"handle": "newhandle.test", "updated_at": self.watermark + 10}}))
        verdict = report["verdict"]["gates"]["corpus"]
        self.assertEqual((verdict["status"], verdict["reasons"]), (gates.FAIL, ["newer in shadow, handle_differs: 1 differences and no arbiter sample"]))

    def test_wrong_admin_secret_fails_without_printing_it(self):
        wrong = dict(self.environ, **{SHADOW_SECRET_ENV: "wrong-" + self.secrets["shadow"]})
        args = compare.parser().parse_args([
            "--production", self.servers["production"]["public"], "--shadow", self.servers["shadow"]["public"],
            "--production-admin", self.servers["production"]["admin"], "--shadow-admin", self.servers["shadow"]["admin"],
            "--production-admin-secret-env", PRODUCTION_SECRET_ENV, "--shadow-admin-secret-env", SHADOW_SECRET_ENV,
            "--phases", "corpus", "--corpus-delay", "0",
        ])
        with self.assertRaises(compare.TransportError) as raised:
            compare.execute(args, environ=wrong, sleep=lambda seconds: None)
        self.assertIn("401", str(raised.exception))
        for secret in (*self.secrets.values(), wrong[SHADOW_SECRET_ENV]):
            self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
