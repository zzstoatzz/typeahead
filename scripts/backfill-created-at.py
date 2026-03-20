#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
bulk backfill created_at by streaming the PLC directory export.

loads all DIDs missing created_at from Turso into a set, then streams
the PLC export (JSONL, chronological) matching creation operations
against our set. batches updates to Turso as matches accumulate.

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-created-at.py
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-created-at.py --dry-run
"""

import argparse
import json
import os
import sys
import time
import urllib.request

PLC_EXPORT_URL = "https://plc.directory/export"
PLC_PAGE_SIZE = 1000
TURSO_BATCH_SIZE = 200
FLUSH_THRESHOLD = 500  # flush to Turso every N matches

DIM = "\033[2m"
RESET = "\033[0m"


# --- turso helpers ---

def get_turso_url() -> str:
    url = os.environ.get("TURSO_URL", "")
    if not url:
        print("error: TURSO_URL not set", file=sys.stderr)
        sys.exit(1)
    return url.replace("libsql://", "https://")


def get_turso_token() -> str:
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not token:
        print("error: TURSO_AUTH_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return token


def turso_query_all(sql: str, turso_url: str, turso_token: str) -> list[dict]:
    """paginated query to fetch all rows."""
    all_rows = []
    offset = 0
    page = 10000
    while True:
        body = json.dumps({
            "requests": [
                {"type": "execute", "stmt": {
                    "sql": f"{sql} LIMIT {page} OFFSET {offset}",
                    "args": [],
                }},
                {"type": "close"},
            ]
        }).encode()
        req = urllib.request.Request(
            f"{turso_url}/v3/pipeline",
            data=body,
            headers={
                "Authorization": f"Bearer {turso_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            res = result["results"][0]
            if res.get("type") == "error":
                print(f"  turso error: {res['error']['message']}", file=sys.stderr)
                break
            cols = [c["name"] for c in res["response"]["result"]["cols"]]
            rows = []
            for row in res["response"]["result"]["rows"]:
                rows.append({c: (v["value"] if v["type"] != "null" else None) for c, v in zip(cols, row)})
            all_rows.extend(rows)
            if len(rows) < page:
                break
            offset += page
            sys.stdout.write(f"\r  loading DIDs... {len(all_rows):,}")
            sys.stdout.flush()
    return all_rows


def turso_batch(stmts: list[dict], turso_url: str, turso_token: str) -> bool:
    requests = [{"type": "execute", "stmt": s} for s in stmts]
    requests.append({"type": "close"})
    body = json.dumps({"requests": requests}).encode()
    req = urllib.request.Request(
        f"{turso_url}/v3/pipeline",
        data=body,
        headers={
            "Authorization": f"Bearer {turso_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            for r in result.get("results", []):
                if r.get("type") == "error":
                    print(f"  turso error: {r.get('error', {}).get('message', 'unknown')}")
                    return False
            return True
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:300]
        print(f"  turso HTTP {e.code}: {err_body}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  turso request failed: {e}", file=sys.stderr)
        return False


# --- PLC export streaming ---

def stream_plc_export(after: str = "") -> list[dict]:
    """fetch one page from PLC export. returns list of ops."""
    url = f"{PLC_EXPORT_URL}?count={PLC_PAGE_SIZE}"
    if after:
        url += f"&after={after}"
    req = urllib.request.Request(url, headers={"User-Agent": "typeahead-backfill/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            lines = resp.read().decode().strip().split("\n")
            return [json.loads(line) for line in lines if line]
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("\n  PLC rate limited — pausing 10s")
            time.sleep(10)
            return stream_plc_export(after)
        raise
    except Exception as e:
        print(f"\n  PLC fetch error: {e} — retrying in 5s")
        time.sleep(5)
        return stream_plc_export(after)


# --- main ---

def main():
    parser = argparse.ArgumentParser(description="bulk backfill created_at from PLC directory export")
    parser.add_argument("--dry-run", action="store_true", help="stream + match but don't write")
    args = parser.parse_args()

    turso_url = get_turso_url()
    turso_token = get_turso_token()

    # step 1: load all DIDs missing created_at
    print("loading DIDs missing created_at from Turso...")
    rows = turso_query_all(
        "SELECT did FROM actors WHERE length(created_at) = 0 ORDER BY rowid ASC",
        turso_url, turso_token,
    )
    wanted = {r["did"] for r in rows}
    print(f"\n  {len(wanted):,} DIDs to backfill")

    if not wanted:
        print("nothing to do.")
        return

    # step 2: stream PLC export, match creations
    matched = 0
    scanned = 0
    pending: list[dict] = []  # buffered Turso statements
    after = ""
    t0 = time.time()

    print(f"streaming PLC export... {'(DRY RUN)' if args.dry_run else ''}")

    while wanted:
        ops = stream_plc_export(after)
        if not ops:
            break

        for op in ops:
            scanned += 1
            did = op.get("did", "")
            created_at = op.get("createdAt", "")

            # only care about creation ops for DIDs we're looking for
            if did not in wanted:
                continue

            # first op for a DID is its creation (prev=null)
            prev = op.get("operation", {}).get("prev")
            if prev is not None:
                continue

            wanted.discard(did)
            matched += 1

            pending.append({
                "sql": "UPDATE actors SET created_at = ?1 WHERE did = ?2",
                "args": [
                    {"type": "text", "value": created_at},
                    {"type": "text", "value": did},
                ],
            })

            # flush when buffer is full
            if len(pending) >= FLUSH_THRESHOLD and not args.dry_run:
                for i in range(0, len(pending), TURSO_BATCH_SIZE):
                    turso_batch(pending[i : i + TURSO_BATCH_SIZE], turso_url, turso_token)
                pending.clear()

        after = ops[-1]["createdAt"]
        elapsed = time.time() - t0
        rate = scanned / elapsed if elapsed > 0 else 0
        remaining = len(wanted)
        tag = "dry" if args.dry_run else "live"
        sys.stdout.write(
            f"\r  [{tag}] scanned={scanned:,}  matched={matched:,}  "
            f"remaining={remaining:,}  "
            f"{DIM}{rate:.0f} ops/s  cursor={after}{RESET}   "
        )
        sys.stdout.flush()

    # flush remaining
    if pending and not args.dry_run:
        for i in range(0, len(pending), TURSO_BATCH_SIZE):
            turso_batch(pending[i : i + TURSO_BATCH_SIZE], turso_url, turso_token)

    elapsed = time.time() - t0
    print(f"\n\ndone in {elapsed:.0f}s. matched={matched:,}, missed={len(wanted):,}, scanned={scanned:,} ops")


if __name__ == "__main__":
    main()
