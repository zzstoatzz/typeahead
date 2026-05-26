#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
backfill PDS endpoints via slingshot for actors missing them.

reads DIDs from a file (one per line), resolves PDS via slingshot,
writes back to Turso.

generate the input file with:
    turso db shell typeahead << 'SQL' | tail -n +2 | sed 's/ *$//' > /tmp/pds-dids.txt
    SELECT did FROM actors
    WHERE handle != '' AND pds = '' AND profile_checked_at > 0 AND avatar_url = ''
    ORDER BY rowid ASC;
    SQL

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-pds.py /tmp/pds-dids.txt
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-pds.py --dry-run /tmp/pds-dids.txt
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

SLINGSHOT_URL = "https://slingshot.microcosm.blue/xrpc/blue.microcosm.identity.resolveMiniDoc"
BATCH_SIZE = 500
CONCURRENCY = 20
WRITE_BATCH = 50
WRITE_PAUSE = 0.2

DIM = "\033[2m"
RESET = "\033[0m"


def get_turso_url() -> str:
    url = os.environ.get("TURSO_URL", "")
    if not url:
        print("error: TURSO_URL not set", file=sys.stderr); sys.exit(1)
    return url.replace("libsql://", "https://")


def get_turso_token() -> str:
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not token:
        print("error: TURSO_AUTH_TOKEN not set", file=sys.stderr); sys.exit(1)
    return token


def turso_batch_write(stmts, turso_url, turso_token):
    reqs = [{"type": "execute", "stmt": s} for s in stmts]
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode()
    req = urllib.request.Request(f"{turso_url}/v3/pipeline", data=body, headers={
        "Authorization": f"Bearer {turso_token}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            json.loads(resp.read())
            return True
    except Exception as e:
        print(f"\n  turso write failed: {e}", file=sys.stderr)
        return False


def resolve_pds(did):
    url = f"{SLINGSHOT_URL}?identifier={urllib.request.quote(did)}"
    req = urllib.request.Request(url, headers={"User-Agent": "typeahead-maintenance/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return did, data.get("pds", "")
    except Exception:
        return did, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="file with one DID per line")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    turso_url = get_turso_url()
    turso_token = get_turso_token()

    with open(args.file) as f:
        all_dids = [line.strip() for line in f if line.strip().startswith("did:")]

    filled = 0
    checked = 0
    t0 = time.time()

    print(f"backfilling PDS for {len(all_dids):,} DIDs (concurrency={CONCURRENCY})...")
    if args.dry_run: print("  DRY RUN — no writes")

    for batch_start in range(0, len(all_dids), BATCH_SIZE):
        batch = all_dids[batch_start:batch_start + BATCH_SIZE]

        pending = []
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(resolve_pds, did): did for did in batch}
            for future in as_completed(futures):
                checked += 1
                did, pds = future.result()
                if pds:
                    pending.append({
                        "sql": "UPDATE actors SET pds = ?1, identity_checked_at = unixepoch() WHERE did = ?2",
                        "args": [
                            {"type": "text", "value": pds},
                            {"type": "text", "value": did},
                        ],
                    })
                    filled += 1
                else:
                    pending.append({
                        "sql": "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1",
                        "args": [{"type": "text", "value": did}],
                    })

        if pending and not args.dry_run:
            for i in range(0, len(pending), WRITE_BATCH):
                turso_batch_write(pending[i:i+WRITE_BATCH], turso_url, turso_token)
                time.sleep(WRITE_PAUSE)

        elapsed = time.time() - t0
        rate = checked / elapsed if elapsed > 0 else 0
        tag = "dry" if args.dry_run else "live"
        print(
            f"  [{tag}] checked={checked:,}  filled={filled:,}  "
            f"{DIM}{rate:.0f} dids/s  ({checked}/{len(all_dids)}){RESET}"
        )

    elapsed = time.time() - t0
    print(f"\ndone in {elapsed:.0f}s. filled={filled:,}, checked={checked:,}")


if __name__ == "__main__":
    main()
