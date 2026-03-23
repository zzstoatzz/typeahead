#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
delete dead actors (empty handle, not returned by bsky getProfiles).

pages through actors with handle='', verifies each batch via getProfiles:
  - returned WITH handle → UPDATE handle (was temporary resolution issue)
  - returned WITHOUT handle → leave alone (deactivated but bsky knows it)
  - NOT returned → DELETE (truly dead)

usage:
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/cleanup-dead-actors.py --dry-run --limit 100
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/cleanup-dead-actors.py --limit 1000
    TURSO_URL=... TURSO_AUTH_TOKEN=... ./scripts/cleanup-dead-actors.py --start-rowid 50000
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

BSKY_GET_PROFILES = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"
BSKY_MOD_DID = "did:plc:ar7c4by46qjdydhdevvrndac"
MOD_HIDE_VALS = {"!hide", "!takedown", "spam"}
PAGE_SIZE = 500
BSKY_CONCURRENCY = 5
WRITE_BATCH = 25       # max statements per turso batch (keep write lock short)
WRITE_PAUSE = 0.5      # seconds between write batches (let reads through)

DIM = "\033[2m"
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"


def extract_avatar_cid(url: str) -> str:
    """extract CID from bsky avatar URL (mirrors extractAvatarCid in utils.ts)"""
    import re
    m = re.search(r'/([^/]+?)(?:@[a-z]+)?$', url)
    return m.group(1) if m else ''


def should_hide(labels: list) -> bool:
    """mirrors shouldHide in moderation.ts"""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    for l in labels:
        if l.get("neg"): continue
        exp = l.get("exp")
        if exp:
            try:
                if datetime.datetime.fromisoformat(exp.replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                continue
        if l.get("src") == BSKY_MOD_DID and l.get("val") in MOD_HIDE_VALS:
            return True
    return False


def extract_profile_fields(profile: dict) -> dict:
    """mirrors extractProfileFields in utils.ts — full profile extraction"""
    labels = profile.get("labels", [])
    associated = profile.get("associated", {})
    # clean associated: strip zero/false/null values
    clean_assoc = {k: v for k, v in (associated or {}).items()
                   if v not in (0, False, None)}
    return {
        "handle": profile.get("handle", ""),
        "display_name": profile.get("displayName", ""),
        "avatar_cid": extract_avatar_cid(profile.get("avatar", "")),
        "labels": json.dumps(labels),
        "hidden": 1 if should_hide(labels) else 0,
        "created_at": profile.get("createdAt", ""),
        "associated": json.dumps(clean_assoc) if clean_assoc else "{}",
    }


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


def turso_query(sql, args, turso_url, turso_token):
    body = json.dumps({"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": args}},
        {"type": "close"},
    ]}).encode()
    req = urllib.request.Request(f"{turso_url}/v3/pipeline", data=body, headers={
        "Authorization": f"Bearer {turso_token}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            json.loads(resp.read())
            return True
    except Exception as e:
        print(f"\n  turso write failed: {e}", file=sys.stderr)
        return False


def fetch_profiles(dids):
    params = "&".join(f"actors={urllib.request.quote(d)}" for d in dids)
    req = urllib.request.Request(
        f"{BSKY_GET_PROFILES}?{params}",
        headers={"User-Agent": "typeahead-cleanup/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("profiles", [])
    except urllib.error.HTTPError as e:
        if e.code == 429: return "rate_limited"
        return []
    except Exception:
        return []


def main():
    parser = argparse.ArgumentParser(description="delete dead actors with empty handles")
    parser.add_argument("--dry-run", action="store_true", help="log what would happen, no writes")
    parser.add_argument("--limit", type=int, default=0, help="cap actors processed (0 = unlimited)")
    parser.add_argument("--start-rowid", type=int, default=0, help="resume from this rowid")
    args = parser.parse_args()

    turso_url = get_turso_url()
    turso_token = get_turso_token()

    checked = 0
    deleted = 0
    updated = 0
    alive = 0
    t0 = time.time()
    last_rowid = args.start_rowid

    tag = "dry" if args.dry_run else "live"
    print(f"cleanup dead actors (concurrency={BSKY_CONCURRENCY}, write_batch={WRITE_BATCH}, write_pause={WRITE_PAUSE}s)")
    if args.dry_run: print("  DRY RUN — no writes")
    if args.limit: print(f"  limit: {args.limit:,}")
    if args.start_rowid: print(f"  resuming from rowid {args.start_rowid}")

    while True:
        if args.limit and checked >= args.limit:
            break

        rows = turso_query(
            "SELECT rowid, did, handle, display_name FROM actors WHERE rowid > ?1 ORDER BY rowid ASC LIMIT ?2",
            [{"type": "integer", "value": str(last_rowid)}, {"type": "integer", "value": str(PAGE_SIZE)}],
            turso_url, turso_token,
        )
        if not rows:
            break

        last_rowid = int(rows[-1]["rowid"])

        # only check actors with empty handles — skip the rest client-side
        empty = [r for r in rows if r.get("handle", "") == ""]
        if not empty:
            continue
        rows = empty

        dids = [r["did"] for r in rows]
        did_to_row = {r["did"]: r for r in rows}

        batches = [dids[i:i+25] for i in range(0, len(dids), 25)]
        to_delete = []
        to_update = []

        with ThreadPoolExecutor(max_workers=BSKY_CONCURRENCY) as pool:
            futures = {pool.submit(fetch_profiles, b): b for b in batches}
            for future in as_completed(futures):
                batch_dids = futures[future]
                result = future.result()

                if result == "rate_limited":
                    print(f"\n  rate limited — pausing 30s...")
                    time.sleep(30)
                    result = fetch_profiles(batch_dids)
                    if result == "rate_limited":
                        print("  still limited, skipping batch")
                        checked += len(batch_dids)
                        continue

                checked += len(batch_dids)
                returned = {p["did"]: p for p in result}

                for did in batch_dids:
                    if did in returned:
                        p = returned[did]
                        handle = p.get("handle", "")
                        if handle:
                            fields = extract_profile_fields(p)
                            fields["did"] = did
                            to_update.append(fields)
                        else:
                            alive += 1  # deactivated but bsky knows it
                    else:
                        row = did_to_row[did]
                        to_delete.append({"did": did, "display_name": row.get("display_name", "")})

        # log deletions
        for d in to_delete:
            name = d["display_name"] or "(no name)"
            print(f"  {RED}DELETE{RESET} {d['did']}  {DIM}{name}{RESET}")

        for u in to_update:
            avatar = "+" if u.get("avatar_cid") else "-"
            print(f"  {GREEN}UPDATE{RESET} {u['did']}  → {u['handle']}  avatar={avatar}")

        if not args.dry_run:
            # all writes go through throttled_write to keep lock hold times short
            if to_delete:
                del_stmts = [
                    {"sql": "DELETE FROM actors WHERE did = ?1",
                     "args": [{"type": "text", "value": d["did"]}]}
                    for d in to_delete
                ]
                for i in range(0, len(del_stmts), WRITE_BATCH):
                    turso_batch_write(del_stmts[i:i+WRITE_BATCH], turso_url, turso_token)
                    time.sleep(WRITE_PAUSE)

            if to_update:
                upd_stmts = [
                    {"sql": """UPDATE actors SET
                        handle = COALESCE(NULLIF(?2, ''), handle),
                        display_name = COALESCE(NULLIF(?3, ''), display_name),
                        avatar_url = COALESCE(NULLIF(?4, ''), avatar_url),
                        labels = ?5, hidden = ?6,
                        created_at = COALESCE(NULLIF(?7, ''), created_at),
                        associated = COALESCE(NULLIF(?8, '{}'), associated),
                        profile_checked_at = unixepoch()
                     WHERE did = ?1""",
                     "args": [
                         {"type": "text", "value": u["did"]},
                         {"type": "text", "value": u["handle"]},
                         {"type": "text", "value": u.get("display_name", "")},
                         {"type": "text", "value": u.get("avatar_cid", "")},
                         {"type": "text", "value": u.get("labels", "[]")},
                         {"type": "integer", "value": str(u.get("hidden", 0))},
                         {"type": "text", "value": u.get("created_at", "")},
                         {"type": "text", "value": u.get("associated", "{}")},
                     ]}
                    for u in to_update
                ]
                for i in range(0, len(upd_stmts), WRITE_BATCH):
                    turso_batch_write(upd_stmts[i:i+WRITE_BATCH], turso_url, turso_token)
                    time.sleep(WRITE_PAUSE)

        deleted += len(to_delete)
        updated += len(to_update)

        elapsed = time.time() - t0
        rate = checked / elapsed if elapsed > 0 else 0
        print(
            f"  [{tag}] checked={checked:,}  deleted={deleted:,}  "
            f"updated={updated:,}  alive={alive:,}  "
            f"{DIM}{rate:.0f} dids/s  rowid={last_rowid}{RESET}"
        )

    elapsed = time.time() - t0
    print(f"\ndone in {elapsed:.0f}s. checked={checked:,}, deleted={deleted:,}, updated={updated:,}, alive={alive:,}")


if __name__ == "__main__":
    main()
