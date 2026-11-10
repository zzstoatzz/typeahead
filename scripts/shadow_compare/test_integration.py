"""Two real typeahead-tantivy servers whose indexes differ in five known ways."""

import copy
import hashlib
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
import compare
import corpus
import gates

MISSING = "did:plc:missing2222222222222222"
RENAMED = "did:plc:renamed2222222222222222"
HIDDEN = "did:plc:hidden22222222222222222"
MOVED = "did:plc:moved222222222222222222"
DELETED = "did:plc:deleted2222222222222222"
JUSTIN = "did:plc:ec64xv7n5dszeizw56yr6b5h"
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
        if not smoke.BIN.exists():
            raise AssertionError(f"typeahead-tantivy binary missing at {smoke.BIN}; run `just --no-dotenv test-shadow-compare`")
        cls.stack = tempfile.TemporaryDirectory(prefix="shadow-compare-")
        cls.addClassCleanup(cls.stack.cleanup)
        cls.root = Path(cls.stack.name)
        cls.secrets = {"production": secrets.token_urlsafe(32), "shadow": secrets.token_urlsafe(32)}
        cls.environ = {PRODUCTION_SECRET_ENV: cls.secrets["production"], SHADOW_SECRET_ENV: cls.secrets["shadow"]}
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
