#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Compare a shadow search backend with production and decide four gates."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import arbiter
import bundle
import corpus
import freshness
import gates
import report as reporting
import search
import sources
import store
from transport import Budget, Client, TransportError, redact, secret_from_env

PHASES = ("corpus", "search", "latency", "freshness")
MIB = 1024 * 1024


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phases", default="search,latency,freshness", help="comma-separated subset of " + ",".join(PHASES))
    p.add_argument("--production", help="production backend origin serving /search and /index/status")
    p.add_argument("--shadow", help="shadow backend origin")
    p.add_argument("--production-admin", help="origin of production's admin listener")
    p.add_argument("--shadow-admin", help="origin of the shadow's admin listener")
    p.add_argument("--production-admin-secret-env", help="name of the variable holding production's admin bearer")
    p.add_argument("--shadow-admin-secret-env", help="name of the variable holding the shadow's admin bearer")
    p.add_argument("--out-dir", help="report directory; default docs/evals/shadow-compare-<UTC date>")
    p.add_argument("--timeout", type=float, default=15.0)

    g = p.add_argument_group("corpus")
    g.add_argument("--production-bundle", help="production's corpus as a view file written by `bundle.py sort`, instead of its admin listener")
    g.add_argument("--bundle-watermark", type=int, help="the bundle's source watermark in unix seconds")
    g.add_argument("--bundle-build-id", help="the bundle's build id, recorded in the report")
    g.add_argument("--shadow-store", help="origin of the shadow's store process, read instead of its admin listener")
    g.add_argument("--shadow-store-secret-env", help="name of the variable holding the store's read bearer")
    g.add_argument("--page-size", type=int, default=corpus.PAGE_LIMIT)
    g.add_argument("--corpus-delay", type=float, default=0.25, help="seconds between requests to one admin listener or store")
    g.add_argument("--corpus-max-requests", type=int, default=2000, help="per backend")
    g.add_argument("--corpus-max-mib", type=int, default=256, help="per backend, request plus response bodies")
    g.add_argument("--max-pages", type=int, help="stop after this many windows in this run")
    g.add_argument("--checkpoint", help="file that makes the walk resumable")
    g.add_argument("--settle-delay", type=float, default=gates.SETTLE_DELAY_S)
    g.add_argument("--no-fields", action="store_true", help="do not ask source-state for comparison fields")
    g.add_argument("--arbiter", action="store_true", help="check sampled differences against plc.directory, did:web hosts and the Bluesky appview")
    g.add_argument("--arbiter-delay", type=float, default=1.0)
    g.add_argument("--plc-url", default="https://plc.directory")
    g.add_argument("--appview-url", default="https://public.api.bsky.app")

    g = p.add_argument_group("search and latency")
    g.add_argument("--search-delay", type=float, default=1.0, help="seconds between requests to one backend")
    g.add_argument("--search-max-requests", type=int, default=2000, help="per backend, shared by search, latency and freshness")
    g.add_argument("--search-max-mib", type=int, default=64)
    g.add_argument("--extra-queries", help="file with one query, or one JSON object of parameters, per line")
    g.add_argument("--cases-file", default=str(search.CASES_FILE))
    g.add_argument("--search-settle-delay", type=float, default=30.0)
    g.add_argument("--flagged-from", help="an earlier report.json whose corpus differences explain search differences")
    g.add_argument("--production-public", help="public Worker origin to compare with production's direct answers")
    g.add_argument("--bluesky-oracle", action="store_true", help="fetch three queries from Bluesky so the spam guard can run")
    g.add_argument("--warm-runs", type=int, default=5)
    g.add_argument("--debug-search", action="store_true", help="also read /debug/search once per query per backend")

    g = p.add_argument_group("freshness")
    g.add_argument("--freshness-samples", type=int, default=5)
    g.add_argument("--freshness-interval", type=float, default=30.0)
    g.add_argument("--changed-dids", help="file of recently changed DIDs, one per line, read from both admin listeners each sample")
    return p


def lines(path):
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


def execute(args, environ=None, sleep=time.sleep):
    phases = [name.strip() for name in args.phases.split(",") if name.strip()]
    unknown = sorted(set(phases) - set(PHASES))
    if unknown:
        raise TransportError("unknown phases: " + ", ".join(unknown))
    secrets = {
        side: secret_from_env(getattr(args, side + "_admin_secret_env"), environ) for side in ("production", "shadow")
    }
    store_secret = secret_from_env(args.shadow_store_secret_env, environ)
    known = tuple(value for value in (*secrets.values(), store_secret) if value)
    budgets, clients = {}, []

    def client(name, base, max_requests, max_bytes, delay, secret=None, secret_prefix="/admin/"):
        budgets[name] = Budget(max_requests, max_bytes)
        made = Client(name, base, budgets[name], secret=secret, delay=delay, timeout=args.timeout, sleep=sleep, extra_secrets=known,
                      secret_prefix=secret_prefix)
        clients.append(made)
        return made

    def admin(side):
        if not getattr(args, side + "_admin") or not secrets[side]:
            raise TransportError(f"{side}: this run needs --{side}-admin and --{side}-admin-secret-env")
        limits = (args.corpus_max_requests, args.corpus_max_mib * MIB, args.corpus_delay)
        return client(side + " admin", getattr(args, side + "_admin"), *limits, secrets[side])

    public = None
    if set(phases) & {"search", "latency", "freshness"}:
        if not args.production or not args.shadow:
            raise TransportError("search, latency and freshness need --production and --shadow")
        public = {
            side: client(side, getattr(args, side), args.search_max_requests, args.search_max_mib * MIB, args.search_delay)
            for side in ("production", "shadow")
        }
    admins = {side: admin(side) for side in ("production", "shadow")} if args.changed_dids else None
    corpus_sources = None
    if "corpus" in phases:
        if args.production_bundle:
            production = bundle.BundleJsonl(args.production_bundle, watermark=args.bundle_watermark, build_id=args.bundle_build_id)
        else:
            production = sources.TantivyAdmin(admins["production"] if admins else admin("production"), not args.no_fields)
        if args.shadow_store:
            if not store_secret:
                raise TransportError("shadow: --shadow-store needs --shadow-store-secret-env")
            limits = (args.corpus_max_requests, args.corpus_max_mib * MIB, args.corpus_delay)
            shadow = store.StoreHttp(client("shadow store", args.shadow_store, *limits, store_secret, store.SECRET_PREFIX))
        else:
            shadow = sources.TantivyAdmin(admins["shadow"] if admins else admin("shadow"), not args.no_fields)
        corpus_sources = {"production": production, "shadow": shadow}
    now = datetime.now(timezone.utc)
    report = {
        "schema_version": 1,
        "date": now.strftime("%Y-%m-%d"),
        "started_at": int(now.timestamp()),
        "phases": phases,
        "backends": {side: (getattr(args, side) or "").rstrip("/") or None for side in ("production", "shadow")},
        "corpus_sources": {side: source.identity for side, source in corpus_sources.items()} if corpus_sources else None,
        "admin_secret_variables": {
            side: getattr(args, side + "_admin_secret_env") for side in ("production", "shadow")
        },
        "store_secret_variable": args.shadow_store_secret_env,
        "thresholds": {name: getattr(gates, name) for name in dir(gates) if name.isupper() and name not in ("PASS", "FAIL", "NOT_RUN", "GATES", "EVALUATORS")},
    }
    try:
        flagged = set()
        if args.flagged_from:
            flagged |= set(json.loads(Path(args.flagged_from).read_text())["corpus"]["flagged_dids"])
        if "corpus" in phases:
            result = corpus.run_sources(
                corpus_sources["production"],
                corpus_sources["shadow"],
                checkpoint_path=args.checkpoint,
                page_size=args.page_size,
                max_pages=args.max_pages,
                max_tracked=gates.MAX_TRACKED_DIFFERENCES,
                settle_delay=args.settle_delay,
                sample_size=gates.ARBITER_SAMPLE_SIZE,
                watermark_skew=gates.WATERMARK_SKEW_S,
                sleep=sleep,
            )
            if args.arbiter and result["differing_dids"]:
                limits = (1000, 32 * MIB, args.arbiter_delay)
                authority = arbiter.Authority(
                    client("plc directory", args.plc_url, *limits),
                    client("appview", args.appview_url, *limits),
                    web=lambda origin: client("did:web " + origin, origin, 50, MIB, args.arbiter_delay),
                )
                arbiter.label_samples(result, authority)
            flagged |= set(result["flagged_dids"])
            report["corpus"] = result
        cases = []
        if "search" in phases or "latency" in phases:
            cases = search.canonical_cases() + search.recorded_cases(args.cases_file)
            if args.extra_queries:
                cases += search.extra_cases(args.extra_queries)
        if "latency" in phases:
            report["latency"] = search.latency(
                public["production"], public["shadow"], cases, warm_runs=args.warm_runs, debug=args.debug_search
            )
        if "search" in phases:
            oracle = client("bluesky oracle", args.appview_url, 10, MIB, 1.0) if args.bluesky_oracle else None
            worker = (
                client("production public", args.production_public, 200, 16 * MIB, max(1.0, args.search_delay))
                if args.production_public
                else None
            )
            report["search"] = search.run(
                public["production"],
                public["shadow"],
                cases,
                flagged=flagged,
                oracle_client=oracle,
                public_client=worker,
                settle_delay=args.search_settle_delay,
                sleep=sleep,
            )
        if "freshness" in phases:
            report["freshness"] = freshness.watch(
                public["production"],
                public["shadow"],
                samples=args.freshness_samples,
                interval=args.freshness_interval,
                sleep=sleep,
                changed_dids=lines(args.changed_dids) if args.changed_dids else (),
                admins=admins,
                want_fields=not args.no_fields,
            )
    except TransportError as error:
        raise TransportError(redact(str(error), known)) from None
    finally:
        for made in clients:
            made.close()
    report["finished_at"] = int(time.time())
    report["budgets"] = {name: budget.snapshot() for name, budget in budgets.items()}
    report["verdict"] = gates.evaluate(report)
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        report = execute(args)
    except TransportError as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    directory = args.out_dir or str(HERE.parents[1] / "docs/evals" / ("shadow-compare-" + report["date"].replace("-", "")))
    reporting.write(report, directory)
    verdict = report["verdict"]
    for name in gates.GATES:
        entry = verdict["gates"][name]
        print(f"{reporting.GATE_TITLES[name]}: {entry['status']}")
        for reason in entry["reasons"][:10]:
            print("  " + reason)
    print(f"overall: {verdict['overall']}")
    print(f"report: {directory}")
    return 1 if verdict["overall"] == gates.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
