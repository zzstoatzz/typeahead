#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
discover and index all handles under a domain suffix.

discovery methods:
  bsky  — search bluesky's public API (fast, but misses handles bluesky hasn't indexed)
  plc   — stream PLC directory export (comprehensive, slower — use --after to limit range)
  pds   — enumerate repos on a PDS, resolve handles via PLC (best for domains that run their own PDS)

usage:
    ./scripts/index-domain.py tngl.sh --source pds --pds https://tngl.sh
    ./scripts/index-domain.py tngl.sh --source plc --after 2026-01-01
    ./scripts/index-domain.py bsky.team --dry-run
    ./scripts/index-domain.py mycustomdomain.com --concurrency 10
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypedDict


class Actor(TypedDict):
    did: str
    handle: str


class IndexResult(TypedDict, total=False):
    handle: str
    did: str
    hidden: bool
    error: str

BSKY_SEARCH = "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors"
PLC_EXPORT = "https://plc.directory/export"
TYPEAHEAD_URL = "https://typeahead.waow.tech"
SEARCH_LIMIT = 100
PLC_PAGE_SIZE = 1000

DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def search_bsky(suffix: str) -> list[Actor]:
    """paginate through bluesky searchActors for handles ending in .suffix"""
    found = {}
    cursor = None
    page = 0

    while True:
        page += 1
        url = f"{BSKY_SEARCH}?q={urllib.parse.quote(suffix)}&limit={SEARCH_LIMIT}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"{RED}search error page {page}: {e}{RESET}")
            break

        actors = data.get("actors", [])
        if not actors:
            break

        new = 0
        for actor in actors:
            handle = actor.get("handle", "")
            did = actor.get("did", "")
            if handle.endswith(f".{suffix}") and did not in found:
                found[did] = handle
                new += 1

        print(f"{DIM}page {page}: {len(actors)} results, {new} new matches ({len(found)} total){RESET}")

        cursor = data.get("cursor")
        if not cursor or len(actors) < SEARCH_LIMIT:
            break

    return [{"did": did, "handle": handle} for did, handle in found.items()]


def search_plc(suffix: str, after: str | None = None) -> list[Actor]:
    """stream PLC directory export, filtering for handles ending in .suffix"""
    found = {}
    cursor = after or "1970-01-01T00:00:00Z"
    pages = 0
    total_ops = 0

    while True:
        pages += 1
        url = f"{PLC_EXPORT}?count={PLC_PAGE_SIZE}&after={urllib.parse.quote(cursor)}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                lines = resp.read().decode().strip().split("\n")
        except Exception as e:
            print(f"{RED}PLC export error at cursor {cursor}: {e}{RESET}")
            break

        if not lines or lines == [""]:
            break

        batch_new = 0
        last_created = cursor
        for line in lines:
            if not line.strip():
                continue
            total_ops += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            last_created = entry.get("createdAt", last_created)
            did = entry.get("did", "")
            op = entry.get("operation", {})

            # newer format: alsoKnownAs
            for aka in op.get("alsoKnownAs", []):
                handle = aka.removeprefix("at://")
                if handle.endswith(f".{suffix}") and did not in found:
                    found[did] = handle
                    batch_new += 1

            # older format: handle field
            handle = op.get("handle", "")
            if handle.endswith(f".{suffix}") and did not in found:
                found[did] = handle
                batch_new += 1

        if batch_new:
            print(f"{DIM}page {pages}: scanned {total_ops} ops, +{batch_new} new ({len(found)} total){RESET}")
        elif pages % 100 == 0:
            print(f"{DIM}page {pages}: scanned {total_ops} ops, {len(found)} matches so far (at {last_created[:10]}){RESET}")

        if len(lines) < PLC_PAGE_SIZE:
            break

        cursor = last_created

    print(f"{DIM}scanned {total_ops} PLC operations across {pages} pages{RESET}")
    return [{"did": did, "handle": handle} for did, handle in found.items()]


def search_pds(suffix: str, pds_url: str) -> list[Actor]:
    """enumerate all repos on a PDS via com.atproto.sync.listRepos, resolve handles via PLC"""
    # step 1: collect all DIDs from the PDS
    all_dids = []
    cursor = ""
    page = 0

    while True:
        page += 1
        url = f"{pds_url}/xrpc/com.atproto.sync.listRepos?limit=1000"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"{RED}PDS listRepos error page {page}: {e}{RESET}")
            break

        repos = data.get("repos", [])
        all_dids.extend(r["did"] for r in repos)
        print(f"{DIM}page {page}: {len(repos)} repos ({len(all_dids)} total){RESET}")

        cursor = data.get("cursor", "")
        if not cursor or len(repos) == 0:
            break

    print(f"found {len(all_dids)} accounts on PDS, resolving handles via PLC...")

    # step 2: resolve each DID via PLC directory (concurrent)
    found = {}
    errors = 0

    def resolve_did(did):
        req = urllib.request.Request(f"https://plc.directory/{did}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            doc = json.loads(resp.read())
        for aka in doc.get("alsoKnownAs", []):
            handle = aka.removeprefix("at://")
            if handle.endswith(f".{suffix}"):
                return did, handle
        return None, None

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(resolve_did, did): did for did in all_dids}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                did, handle = future.result()
                if did:
                    found[did] = handle
            except Exception:
                errors += 1
            if done % 200 == 0:
                print(f"{DIM}  resolved {done}/{len(all_dids)} ({len(found)} matches){RESET}")

    print(f"{DIM}resolved {len(all_dids)} DIDs: {len(found)} matches, {errors} errors{RESET}")
    return [{"did": did, "handle": handle} for did, handle in found.items()]


def index_one(handle: str, token: str | None = None) -> IndexResult:
    """call /request-indexing for a single handle"""
    url = f"{TYPEAHEAD_URL}/request-indexing?handle={urllib.parse.quote(handle)}"
    headers: dict[str, str] = {"User-Agent": "typeahead-index-domain/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="discover and index handles under a domain suffix")
    parser.add_argument("suffix", help="domain suffix, e.g. tngl.io")
    parser.add_argument("--source", choices=["bsky", "plc", "pds"], default="bsky",
                        help="discovery method (default: bsky)")
    parser.add_argument("--pds", help="for pds source: the PDS URL (e.g. https://tngl.sh)")
    parser.add_argument("--after", help="for plc source: only scan operations after this date (e.g. 2026-01-01)")
    parser.add_argument("--dry-run", action="store_true", help="discover only, don't index")
    parser.add_argument("--token", help="admin token to bypass rate limiting (reads ADMIN_SECRET env var if not set)")
    parser.add_argument("--concurrency", type=int, default=5, help="concurrent indexing requests (default: 5)")
    args = parser.parse_args()

    suffix = args.suffix.lstrip("*.")

    if args.source == "pds":
        if not args.pds:
            print(f"{RED}--pds URL required for pds source{RESET}")
            sys.exit(1)
        print(f"enumerating repos on {args.pds} for *.{suffix} handles...")
        actors = search_pds(suffix, args.pds.rstrip("/"))
    elif args.source == "plc":
        after = args.after
        if after and "T" not in after:
            after += "T00:00:00Z"
        print(f"scanning PLC directory for *.{suffix}" + (f" (after {after})" if after else "") + "...")
        actors = search_plc(suffix, after)
    else:
        print(f"searching bluesky for *.{suffix} handles...")
        actors = search_bsky(suffix)

    if not actors:
        print(f"no handles found matching *.{suffix}")
        if args.source == "bsky":
            print(f"{DIM}tip: try --source plc --after 2025-01-01 for a more comprehensive scan{RESET}")
        return

    print(f"\nfound {len(actors)} handles:")
    for a in sorted(actors, key=lambda x: x["handle"]):
        print(f"  {a['handle']} ({a['did']})")

    if args.dry_run:
        print(f"\n{YELLOW}dry run — skipping indexing{RESET}")
        return

    import os
    token = args.token or os.environ.get("ADMIN_SECRET")

    print(f"\nindexing {len(actors)} handles (concurrency={args.concurrency}" + (", admin auth" if token else ", no auth — may hit rate limit") + ")...")
    indexed = 0
    hidden = 0
    errors = 0
    done = 0
    verbose = len(actors) <= 50

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(index_one, a["handle"], token): a for a in actors}
        for future in as_completed(futures):
            actor = futures[future]
            result = future.result()
            done += 1
            if "error" in result:
                errors += 1
                if verbose:
                    print(f"  {RED}✗ {actor['handle']}: {result['error']}{RESET}")
            elif result.get("hidden"):
                hidden += 1
                if verbose:
                    print(f"  {YELLOW}· {actor['handle']} (hidden){RESET}")
            else:
                indexed += 1
                if verbose:
                    print(f"  {GREEN}✓ {actor['handle']}{RESET}")
            if not verbose and done % 100 == 0:
                print(f"{DIM}  {done}/{len(actors)} ({indexed} ok, {hidden} hidden, {errors} errors){RESET}")

    print(f"\ndone: {indexed} indexed, {hidden} hidden, {errors} errors")


if __name__ == "__main__":
    main()
