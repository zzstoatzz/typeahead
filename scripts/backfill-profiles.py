#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
bulk enrichment: walk actors table and populate labels, handle, display_name,
avatar_url via bsky's public getProfiles API (25 DIDs per call).

targets actors with labels='[]' (default). also recomputes hidden from full
label data, fixing actors incorrectly hidden by stale !no-unauthenticated logic.

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-profiles.py
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-profiles.py --dry-run --limit 100
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/backfill-profiles.py --limit 1000 --offset 50000
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

GET_PROFILES_URL = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"
BSKY_MOD_DID = "did:plc:ar7c4by46qjdydhdevvrndac"
MOD_HIDE_VALS = {"!hide", "!takedown", "spam"}

BATCH_SIZE = 25       # getProfiles limit
PAGE_SIZE = 500       # actors per Turso query
TURSO_BATCH_SIZE = 200  # statements per pipeline call
DELAY = 0.3           # seconds between getProfiles calls

DIM = "\033[2m"
RESET = "\033[0m"


# --- turso helpers (from migrate-to-turso.py) ---

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


def turso_query(sql: str, args: list, turso_url: str, turso_token: str) -> list[dict]:
    """single query via pipeline API, returns rows as dicts."""
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": args}},
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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            res = result["results"][0]
            if res.get("type") == "error":
                print(f"  turso error: {res['error']['message']}", file=sys.stderr)
                return []
            cols = [c["name"] for c in res["response"]["result"]["cols"]]
            rows = []
            for row in res["response"]["result"]["rows"]:
                rows.append({c: (v["value"] if v["type"] != "null" else None) for c, v in zip(cols, row)})
            return rows
    except Exception as e:
        print(f"  turso query failed: {e}", file=sys.stderr)
        return []


def turso_batch(stmts: list[dict], turso_url: str, turso_token: str) -> bool:
    """execute a batch of statements against Turso via HTTP pipeline API."""
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


# --- bsky helpers ---

def fetch_profiles(dids: list[str]) -> list[dict]:
    params = "&".join(f"actors={d}" for d in dids)
    url = f"{GET_PROFILES_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "typeahead-profile-backfill/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("profiles", [])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("\n  rate limited — pausing 60s")
            time.sleep(60)
            return fetch_profiles(dids)  # retry once
        print(f"\n  HTTP {e.code}")
        return []
    except Exception as e:
        print(f"\n  error: {e}")
        return []


_AVATAR_CID_RE = re.compile(r"/([^/]+)@jpeg$")


def extract_avatar_cid(url: str) -> str:
    """extract CID from bsky CDN avatar URL — matches worker's extractAvatarCid."""
    m = _AVATAR_CID_RE.search(url)
    return m.group(1) if m else ""


def _parse_ts(s: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return 0


def should_hide(labels: list | None) -> bool:
    """matches worker's shouldHide (src/index.ts:133) — only bsky mod service labels."""
    if not labels:
        return False
    now = time.time() * 1000
    for l in labels:
        if l.get("neg"):
            continue
        if l.get("exp") and _parse_ts(l["exp"]) <= now:
            continue
        if l.get("src") == BSKY_MOD_DID and l.get("val") in MOD_HIDE_VALS:
            return True
    return False


# --- main ---

def main():
    parser = argparse.ArgumentParser(description="bulk profile backfill via getProfiles")
    parser.add_argument("--dry-run", action="store_true", help="query + fetch but don't write")
    parser.add_argument("--limit", type=int, default=0, help="stop after N actors (0 = all)")
    parser.add_argument("--offset", type=int, default=0, help="start from rowid offset")
    args = parser.parse_args()

    turso_url = get_turso_url()
    turso_token = get_turso_token()

    # stats
    total_queried = 0
    total_enriched = 0
    total_hidden = 0
    total_missing = 0  # DIDs not returned by getProfiles
    offset = args.offset

    print(f"backfill-profiles: {'DRY RUN' if args.dry_run else 'LIVE'}")
    if args.limit:
        print(f"  limit: {args.limit} actors")
    if args.offset:
        print(f"  starting at offset: {args.offset}")

    while True:
        # check limit
        if args.limit and total_queried >= args.limit:
            break

        page_limit = PAGE_SIZE
        if args.limit:
            page_limit = min(PAGE_SIZE, args.limit - total_queried)

        rows = turso_query(
            "SELECT did FROM actors WHERE labels = '[]' ORDER BY rowid ASC LIMIT ? OFFSET ?",
            [{"type": "integer", "value": str(page_limit)}, {"type": "integer", "value": str(offset)}],
            turso_url, turso_token,
        )
        if not rows:
            break

        page_enriched = 0
        page_hidden = 0
        page_missing = 0
        stmts = []

        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            dids = [r["did"] for r in batch]

            if i > 0 or total_queried > 0:
                time.sleep(DELAY)

            profiles = fetch_profiles(dids)
            returned_dids = {p["did"] for p in profiles}

            for p in profiles:
                handle = p.get("handle", "")
                display_name = p.get("displayName", "")
                avatar_cid = extract_avatar_cid(p.get("avatar", ""))
                labels = p.get("labels", [])
                hide = 1 if should_hide(labels) else 0

                if hide:
                    page_hidden += 1

                stmts.append({
                    "sql": (
                        "UPDATE actors SET "
                        "handle = COALESCE(NULLIF(?2, ''), handle), "
                        "display_name = COALESCE(NULLIF(?3, ''), display_name), "
                        "avatar_url = COALESCE(NULLIF(?4, ''), avatar_url), "
                        "labels = ?5, "
                        "hidden = ?6, "
                        "updated_at = unixepoch() "
                        "WHERE did = ?1"
                    ),
                    "args": [
                        {"type": "text", "value": p["did"]},
                        {"type": "text", "value": handle},
                        {"type": "text", "value": display_name},
                        {"type": "text", "value": avatar_cid},
                        {"type": "text", "value": json.dumps(labels)},
                        {"type": "integer", "value": str(hide)},
                    ],
                })
                page_enriched += 1

            # mark missing DIDs so cron skips them
            for did in dids:
                if did not in returned_dids:
                    page_missing += 1
                    stmts.append({
                        "sql": "UPDATE actors SET identity_checked_at = unixepoch() WHERE did = ?1",
                        "args": [{"type": "text", "value": did}],
                    })

        # flush writes
        if stmts and not args.dry_run:
            for i in range(0, len(stmts), TURSO_BATCH_SIZE):
                batch = stmts[i : i + TURSO_BATCH_SIZE]
                if not turso_batch(batch, turso_url, turso_token):
                    print(f"\n  batch write failed at offset={offset}")
                    return

        total_queried += len(rows)
        total_enriched += page_enriched
        total_hidden += page_hidden
        total_missing += page_missing
        offset += len(rows)

        tag = "dry" if args.dry_run else "live"
        sys.stdout.write(
            f"\r  [{tag}] queried={total_queried}  enriched={total_enriched}  "
            f"hidden={total_hidden}  missing={total_missing}  "
            f"{DIM}offset={offset}{RESET}   "
        )
        sys.stdout.flush()

        if len(rows) < page_limit:
            break

    print(f"\n\ndone. queried={total_queried}, enriched={total_enriched}, hidden={total_hidden}, missing={total_missing}")


if __name__ == "__main__":
    main()
