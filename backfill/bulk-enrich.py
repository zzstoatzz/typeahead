#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
bulk enrich actors missing handles or avatars via bsky getProfiles API.

only fetches+writes actors that actually need data (handle='' or avatar_url=''),
skipping already-enriched rows entirely. writes in small batches with pauses.

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/bulk-enrich.py
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/bulk-enrich.py --start-rowid 2529
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/bulk-enrich.py --dry-run
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BSKY_GET_PROFILES = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"
PAGE_SIZE = 500  # unenriched DIDs per page
BSKY_CONCURRENCY = 5  # concurrent getProfiles calls
WRITE_BATCH = 25  # stmts per Turso write — tiny to minimize lock time
WRITE_PAUSE = 0.05  # seconds between write batches

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


def _urlopen_with_retry(req, *, timeout=30, attempts=5):
    """retry on transient network errors (DNS, conn reset, timeout, 5xx).
    survives a sleeping laptop / wifi blip without losing the run."""
    delay = 1.0
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            # retry server-side hiccups; bubble client errors
            if 500 <= e.code < 600 and i < attempts - 1:
                print(f"  retry HTTP {e.code} (attempt {i+1}/{attempts})", file=sys.stderr)
            else:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if i < attempts - 1:
                print(f"  retry {type(e).__name__}: {e} (attempt {i+1}/{attempts})", file=sys.stderr)
            else:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 30)


def turso_query(sql, args, turso_url, turso_token):
    body = json.dumps({"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": args}},
        {"type": "close"},
    ]}).encode()
    req = urllib.request.Request(f"{turso_url}/v3/pipeline", data=body, headers={
        "Authorization": f"Bearer {turso_token}", "Content-Type": "application/json",
    })
    raw = _urlopen_with_retry(req, timeout=30)
    result = json.loads(raw)
    res = result["results"][0]
    if res.get("type") == "error":
        print(f"  turso error: {res['error']['message']}", file=sys.stderr)
        return []
    cols = [c["name"] for c in res["response"]["result"]["cols"]]
    return [{c: (v["value"] if v["type"] != "null" else None) for c, v in zip(cols, row)}
            for row in res["response"]["result"]["rows"]]


def turso_batch_write(stmts, turso_url, turso_token):
    reqs = [{"type": "execute", "stmt": s} for s in stmts]
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode()
    req = urllib.request.Request(f"{turso_url}/v3/pipeline", data=body, headers={
        "Authorization": f"Bearer {turso_token}", "Content-Type": "application/json",
    })
    try:
        json.loads(_urlopen_with_retry(req, timeout=30))
        return True
    except Exception as e:
        print(f"\n  turso write failed after retries: {e}", file=sys.stderr)
        return False


def extract_avatar_cid(url):
    if not url: return ""
    m = re.search(r'/([^/]+?)(?:@[a-z]+)?$', url)
    return m.group(1) if m else ""


def clean_associated(assoc):
    if not assoc or not isinstance(assoc, dict): return "{}"
    clean = {k: v for k, v in assoc.items() if v not in (0, False, None)}
    return json.dumps(clean) if clean else "{}"


def fetch_profiles(dids):
    params = "&".join(f"actors={urllib.request.quote(d)}" for d in dids)
    req = urllib.request.Request(
        f"{BSKY_GET_PROFILES}?{params}",
        headers={"User-Agent": "typeahead-enrich/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("profiles", [])
    except urllib.error.HTTPError as e:
        if e.code == 429: return "rate_limited"
        return []
    except Exception:
        return []


def profile_to_stmt(p):
    hide_vals = {"!hide", "!takedown", "!suspend", "spam"}
    mod_did = "did:plc:ar7c4by46qjdydhdevvrndac"
    hidden = 0
    for lbl in (p.get("labels") or []):
        if lbl.get("val", "") in hide_vals or (lbl.get("val") == "!no-unauthenticated" and lbl.get("src") == mod_did):
            hidden = 1; break

    return {
        "sql": """UPDATE actors SET
            handle = COALESCE(NULLIF(?2, ''), handle),
            display_name = COALESCE(NULLIF(?3, ''), display_name),
            avatar_url = COALESCE(NULLIF(?4, ''), avatar_url),
            labels = ?5, hidden = ?6,
            created_at = COALESCE(NULLIF(?7, ''), created_at),
            associated = COALESCE(NULLIF(?8, '{}'), associated),
            profile_checked_at = unixepoch()
          WHERE did = ?1""",
        "args": [
            {"type": "text", "value": p["did"]},
            {"type": "text", "value": p.get("handle", "")},
            {"type": "text", "value": p.get("displayName", "")},
            {"type": "text", "value": extract_avatar_cid(p.get("avatar", ""))},
            {"type": "text", "value": json.dumps(p.get("labels", []))},
            {"type": "integer", "value": str(hidden)},
            {"type": "text", "value": p.get("createdAt", "")},
            {"type": "text", "value": clean_associated(p.get("associated"))},
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--start-rowid", type=int, default=0)
    args = parser.parse_args()

    turso_url = get_turso_url()
    turso_token = get_turso_token()

    enriched = 0
    skipped = 0
    not_found = 0
    t0 = time.time()
    last_rowid = args.start_rowid

    print(f"bulk enriching unenriched actors only (concurrency={BSKY_CONCURRENCY}, write_batch={WRITE_BATCH})...")
    if args.dry_run: print("  DRY RUN — no writes")
    if args.start_rowid: print(f"  resuming from rowid {args.start_rowid}")

    while True:
        # cheap rowid-paginated read — no filter scan, just walk forward
        rows = turso_query(
            "SELECT rowid, did, handle, avatar_url FROM actors WHERE rowid > ?1 ORDER BY rowid ASC LIMIT ?2",
            [{"type": "integer", "value": str(last_rowid)}, {"type": "integer", "value": str(PAGE_SIZE)}],
            turso_url, turso_token,
        )
        if not rows:
            break

        last_rowid = int(rows[-1]["rowid"])

        # filter client-side: only fetch profiles for rows missing data
        need = [r for r in rows if not r["handle"] or not r["avatar_url"]]
        skipped += len(rows) - len(need)

        if not need:
            elapsed = time.time() - t0
            print(f"  skipped {len(rows)} already-enriched  {DIM}rowid={last_rowid}{RESET}")
            continue

        dids = [r["did"] for r in need]

        # getProfiles in concurrent batches of 25
        batches = [dids[i:i+25] for i in range(0, len(dids), 25)]
        pending = []

        with ThreadPoolExecutor(max_workers=BSKY_CONCURRENCY) as pool:
            futures = {pool.submit(fetch_profiles, b): b for b in batches}
            for future in as_completed(futures):
                batch = futures[future]
                result = future.result()

                if result == "rate_limited":
                    print(f"\n  rate limited — pausing 30s...")
                    time.sleep(30)
                    result = fetch_profiles(batch)
                    if result == "rate_limited":
                        print("  still limited, skipping batch")
                        not_found += len(batch)
                        continue

                returned = {p["did"] for p in result}
                not_found += len(batch) - len(returned)

                for p in result:
                    pending.append(profile_to_stmt(p))
                    enriched += 1

        # write in small batches
        if pending and not args.dry_run:
            write_t0 = time.time()
            ok = 0
            for i in range(0, len(pending), WRITE_BATCH):
                if turso_batch_write(pending[i:i+WRITE_BATCH], turso_url, turso_token):
                    ok += len(pending[i:i+WRITE_BATCH])
                time.sleep(WRITE_PAUSE)
            write_ms = int((time.time() - write_t0) * 1000)
            print(f"    wrote {ok}/{len(pending)} stmts ({write_ms}ms)")

        elapsed = time.time() - t0
        rate = enriched / elapsed if elapsed > 0 else 0
        tag = "dry" if args.dry_run else "live"
        print(
            f"  [{tag}] enriched={enriched:,}  skipped={skipped:,}  not_found={not_found:,}  "
            f"{DIM}{rate:.0f}/s  rowid={last_rowid}{RESET}"
        )

    elapsed = time.time() - t0
    print(f"\ndone in {elapsed:.0f}s. enriched={enriched:,}, not_found={not_found:,}")


if __name__ == "__main__":
    main()
